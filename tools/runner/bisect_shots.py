#!/usr/bin/env python3
"""Per-boot runner: shots along a sequence on one boot of the bench host, from the gentlest step to the strongest.
Single mode (REPEAT 1 and no reps field above 1) stops at the first shot that is not "under": poison is sticky until
a power cycle on some cards. Cell mode parks after a lost shot and counts outcomes per step. A goldilocks is held,
not parked. The controller is checked after every shot. Never below FLOOR mV.

Usage (root, on the glitch boot, card parked, with the patched ga100vfi package importable):
  VFI_BDF=0000:05:00.0 python3 -I bisect_shots.py '<tag>' '<SEQ json>'
A step is [v_mV, td_us, pulses, pw_us, v_restore_mV, fast, reps, hold_ms, t_restore_us, revert, delay_us, gap_us].
Omitted tail fields default to pulses 3, pw 198, v_restore 750, fast 1, reps REPEAT, hold 0, t_restore -20, revert 0,
delay 117, gap 150, which is the stock command byte for byte. fast 2..7, hold > 0, t_restore <= -1000 and revert 2
are firmware knobs (firmware/0008); revert 1 is refused because the NO DIP check needs E1 0xC2FA after the shot. The
firmware uses td and delay only as td - delay: delay 330 at td -60 is the same dip as delay 117 at td -273.

Needs push1 or push2 firmware (fw=lab-psr1-push1, fw=lab-psr1-push2; push2 releases the first shot of a boot on
time). After each shot BAR0 is read passively (passive_snapshot.py rules, firmware/0004 and 0010: FUSECTRL first,
stop at the first 0xBADF/0xBAD0 or bad read, no writes), then the SENTINEL fuse-alias register, which must equal this
boot's pre-shot baseline. A shot whose reset release was not 0..4 us after t = 0 by the alarm, or that had a PMBus
write across the release, is MISTIMED: never scored as under or goldilocks; poison and loss still show; the boot
stops (exit 14).

Environment:
  VFI_BDF (required)   the card's PCI address        VFI_PORT      Pico serial port (default /dev/ttyACM0)
  VFI_PARK             park command (default: park.py next to this file, run with this Python and -I)
  VFI_TRACE_DIR        where TRACE=1 writes its JSON (default: current directory)
  FLOOR (381)          lowest dip allowed, mV        REPEAT (1)    shots per step; > 1 = cell mode
  SHOTCAP (450)        shots per run                 LOSTRUN (8)   lost shots in a row that end a cell-mode run
  CELLLOST (0)         lost shots in a row that skip the rest of a cell (0 = off)
  RESTORE_WAIT (8.0)   seconds to wait for the link after a shot
  SENTINEL (0x820410)  fuse alias register read after every clean read (0 = off)
  SENTINEL_EXPECT      the card's healthy sentinel value; the run stops if this boot's baseline reads otherwise
  PARK_EACH=1          park before every shot after the first, so every shot starts like the first shot of a boot
  TRACE=1              NVVDD traces (debug firmware)
  BAD0_FOLLOWUP=1      log only: after a 0xBAD0 FUSECTRL read, read FUSECTRL again 3 s later (and the passive set
                       if it is back at idle); the outcome is not changed
  RETRAIN, SBR         refused: no link retrain or secondary bus reset during a campaign
Exit codes: 0 sequence done or goldilocks held, 2 usage, firmware, unlock readback or a step the firmware refused,
7 park failed before a step, 9 controller off or unsafe baseline (power cycle), 10 tipped, 11 nack or failed writes
(the shot's state is unknown), 12 no dip (the controller dropped the shot), 13 partial held, 14 mistimed."""
import re, sys, time, subprocess, json, shlex, tempfile
from ga100vfi.glitch import Attempt, Card
from ga100vfi.bar0 import Bar0, restore
import os
HERE = os.path.dirname(os.path.abspath(__file__)); TMP = tempfile.gettempdir()
if len(sys.argv) != 3: print(__doc__); sys.exit(2)
BDF = os.environ.get("VFI_BDF") or sys.exit("set VFI_BDF to the card's PCI address (lspci -Dn -d 10de:20c2)")
PORT = os.environ.get("VFI_PORT", "/dev/ttyACM0")
PARK = shlex.split(os.environ["VFI_PARK"]) if os.environ.get("VFI_PARK") else [sys.executable, "-I", os.path.join(HERE, "park.py")]
TAG = sys.argv[1]; SEQ = json.loads(sys.argv[2]); FLOOR = int(os.environ.get("FLOOR", "381"))
TRACE = os.environ.get("TRACE") == "1"; TR_OUT = os.path.join(os.environ.get("VFI_TRACE_DIR", "."), "traces-bisect-%s.json" % time.strftime("%Y%m%dT%H%M%S")); traces = []
def tr_dump():
    if TRACE and traces: json.dump(traces, open(TR_OUT, "w"))
def tr_summary(mv, ns=240.0):
    import statistics as stx
    if len(mv) < 50: return {"n": len(mv)}
    sm = [stx.median(mv[max(0, k - 4):k + 5]) for k in range(len(mv))]
    base = stx.median(sm[:40]); lo = min(sm)
    return {"n": len(mv), "base_mv": round(base), "trough_mv": round(lo),
            "us_below_base_minus_100": round(sum(1 for x in sm if x < base - 100) * ns / 1000, 1),
            "us_below_450": round(sum(1 for x in sm if x < 450) * ns / 1000, 1),
            "us_below_420": round(sum(1 for x in sm if x < 420) * ns / 1000, 1)}
def st(c):
    sw = c.read(0x25, 0x79, 2); vo = c.read(0x25, 0x8B, 2)
    return (sw[0] | sw[1] << 8), (vo[0] | vo[1] << 8)
# Passive BAR0 observer: the reads and stop rules of passive_snapshot.observe (firmware/0004 and 0010), copied because
# the bench runs this file with python -I where that module is not importable (tests/test_runner.py checks they agree).
# FUSECTRL first; stop at the first 0xBADF or 0xBAD0 value, I/O error, unexpected all-ones, FUSECTRL not idle,
# GFW not ready or DSOV other than 0/1.
# No BAR0 writes: the old classify() went on to write DSOV_SET even after a 0xBADF DSOV read.
OBS_REGS = (("FUSECTRL", 0x820000), ("GFW", 0x118234), ("DSOV", 0x820084), ("CRC", 0x820070), ("PLM", 0x8200FC))
def observe(read32):
    result = {"state": "unknown", "reason": None, "registers": {name: None for name, _ in OBS_REGS}}
    values = result["registers"]
    for name, address in OBS_REGS:
        try:
            value = read32(address)
        except OSError as exc:
            result["reason"] = f"{name}: {exc}"
            return result
        values[name] = value
        if value & 0xFFFF0000 == 0xBADF0000:
            result.update(state="poisoned", reason=f"{name}: BADF sentinel")
            return result
        if value >> 20 == 0xBAD:
            result.update(state="poisoned", reason=f"{name}: BAD0 error read")
            return result
        if value == 0xFFFFFFFF and name != "PLM":
            result["reason"] = f"{name}: all-ones response"
            return result
        if name == "FUSECTRL" and value != 0xE0040000:
            result["reason"] = f"FUSECTRL: unexpected non-idle value 0x{value:08x}"
            return result
        if name == "GFW" and value & 0xFF != 0xFF:
            result.update(state="booting", reason=f"GFW low byte is 0x{value & 0xFF:02x}, not 0xff")
            return result
        if name == "DSOV" and value not in (0, 1):
            result["reason"] = "DSOV: unexpected value"
            return result
    if values["DSOV"] == 1 and values["CRC"] == 0x55 and values["PLM"] == 0xFFFFFFFF:
        result["state"] = "healthy_locked"
    elif values["DSOV"] == 0 and values["CRC"] == 0x55 and values["PLM"] == 0xFFFFFFFF:
        result.update(state="candidate", reason="Passive signature only; active validation not performed")
    else:
        result.update(state="partial_or_corrupt", reason="Diagnostic signature is not healthy")
    return result
def bad(x): return isinstance(x, int) and (x >> 20) == 0xBAD   # 0xBADF.... poison or 0xBAD0.... error read
SENT_BASE = [None]   # the alias value of this boot's healthy pre-shot read
def look(bar):
    """observe(), then the SENTINEL alias register once the five read cleanly. After a run scored all "under", a later
    preflight on the same boot once found fuse alias registers (0x820324, 0x820410, per-GPC TPC/PES) reading 0xbadf1020
    / 0xbad00100 while FUSECTRL, DSOV, CRC and PLM were normal (2026-11-20; cause not known). The value has to match
    this boot's pre-shot baseline (0x1 on one card here); 0 and all-ones are failed reads."""
    o = observe(bar.rd32); r = o["registers"]
    if SENT and o["state"] in ("healthy_locked", "candidate", "partial_or_corrupt") and not any(bad(x) for x in r.values()):
        try: r["SENTINEL"] = sv = bar.rd32(SENT)
        except OSError as e: o.update(state="unknown", reason="SENTINEL: %s" % e); return o
        if bad(sv) or sv in (0, 0xFFFFFFFF) or (SENT_BASE[0] is not None and sv != SENT_BASE[0]):
            o.update(state="hidden_poison", reason="SENTINEL 0x%x: 0x%08x, boot baseline %s" % (
                SENT, sv, "unset" if SENT_BASE[0] is None else "0x%08x" % SENT_BASE[0]))
    if o["state"] == "candidate" and "SENTINEL" not in r:
        o.update(state="unknown", reason="SENTINEL: missing candidate check")
    return o
def verdict(o):
    """The runner's outcome strings. GOLDILOCKS only for DSOV 0, CRC 0x55, PLM 0xFFFFFFFF and a clean sentinel."""
    r = o["registers"]; s_ = o["state"]
    if s_ == "poisoned":
        name = o["reason"].split(":")[0]
        return "poisoned %s=0x%08x (no further BAR0 reads)" % (name, r[name])
    if s_ == "hidden_poison": return "hidden-poison alias 0x%x=0x%08x" % (SENT, r["SENTINEL"])
    if r["FUSECTRL"] == 0xFFFFFFFF: return "lost"
    if s_ == "booting": return "booting GFW=0x%08x (no further BAR0 reads)" % r["GFW"]
    if s_ == "unknown":
        if r["DSOV"] is not None: return "dsov=0x%08x (no further BAR0 reads)" % r["DSOV"]
        return "unread (%s)" % o["reason"]
    if r["PLM"] == 0 and r["CRC"] == 0: return "poisoned/over0"
    if s_ == "healthy_locked": return "under"
    if s_ == "candidate": return "GOLDILOCKS"
    return "dsov%d crc=0x%02x priv=0x%08x" % (r["DSOV"], r["CRC"], r["PLM"])
def fmt_obs(o):
    return " ".join("%s=%s" % (k, "-" if x is None else "0x%08x" % x) for k, x in o["registers"].items()) + " state=%s reason=%s" % (o["state"], o["reason"])
def park(c):
    """Run the park command (park.py); it opens the same tty, so ours is closed first. Returns (rc, last line). After a
    failed park the root port's link and error state is logged, read-only: on one bench 4 of 5 failed parks had the GPU
    rail at ~695 mV and config 0xffffffff, and the root port tells a link that is down from a GPU that is stuck."""
    if c is not None: c.close()
    pr = subprocess.run(PARK, cwd=TMP, capture_output=True, text=True)
    pl = ((pr.stdout or "").strip().splitlines() or ["(no output)"])[-1]
    if pr.returncode != 0:
        try:
            rp = os.path.basename(os.path.dirname(os.path.realpath("/sys/bus/pci/devices/" + BDF)))
            lv = subprocess.run(["lspci", "-vv", "-s", rp], capture_output=True, text=True, timeout=10).stdout
            keep = [x.strip() for x in lv.splitlines() if re.search(r"LnkSta|LnkCtl|DevSta|UESta|CESta|AERCap|RootSta|ErrorSrc|SltSta", x)]
            print("   root port %s after the failed park (lspci -vv, read-only): %s" % (rp, " | ".join(keep) or "(no lines)"), flush=True)
        except Exception as e: print("   root port read failed: %s" % e, flush=True)
    return pr.returncode, pl
def release_check(raw):
    """Why this shot's reset release cannot be scored, or None. Exactly one release_at line, 0..4 us after t = 0, by
    the alarm, and no PMBus write across the release. push1 on one bench (2026-11-20): the first shot of every boot
    released at +9..+10 us, later ones at 0..2 us, +4..+6 on some second shots; push2 released first shots at 0."""
    rel = re.findall(r"(?m)^release_at=(-?\d+) scheduled=0 via=(\w+)", raw)
    if len(rel) != 1: return "%d release_at lines" % len(rel)
    if rel[0][1] != "alarm": return "release via %s" % rel[0][1]
    if not 0 <= int(rel[0][0]) <= 4: return "release_at=%s us" % rel[0][0]
    if "overlaps the PEX_RST release" in raw: return "a PMBus write overlaps the release"
    return None
def step_fields(s_):
    return (s_[0], s_[1], s_[2] if len(s_) > 2 else 3, s_[3] if len(s_) > 3 else 198, s_[4] if len(s_) > 4 else 750,
            s_[5] if len(s_) > 5 else 1, s_[7] if len(s_) > 7 else 0, s_[8] if len(s_) > 8 else -20, s_[9] if len(s_) > 9 else 0,
            s_[10] if len(s_) > 10 else 117, s_[11] if len(s_) > 11 else 150)
PARK_EACH = os.environ.get("PARK_EACH") == "1"
RESTORE_WAIT = float(os.environ.get("RESTORE_WAIT", "8.0"))
REPEAT = int(os.environ.get("REPEAT", "1")); CONT = REPEAT > 1
SHOTCAP = int(os.environ.get("SHOTCAP", "450")); LOSTRUN = int(os.environ.get("LOSTRUN", "8"))
SEQX = [(i, s_) for i, s_ in enumerate(SEQ) for _ in range(s_[6] if len(s_) > 6 else REPEAT)]; CONT = CONT or any(len(s_) > 6 and s_[6] > 1 for s_ in SEQ); cells = {}; nshots = [0]; lostrun = [0]; ended = [""]
SENT = int(os.environ.get("SENTINEL", "0x820410"), 16); CELLLOST = int(os.environ.get("CELLLOST", "0")); skip = [-1]; clost = [0]
if os.environ.get("RETRAIN", "0") != "0" or os.environ.get("SBR", "0") != "0":
    print("STOP: RETRAIN and SBR are not allowed during a campaign (no setpci retrain, no secondary bus reset)"); sys.exit(2)
for s_ in SEQ:   # every step checked before the first shot, not when it comes up
    v, td, np_, pw, vr, fs, hold, tr, rev, dly, gp = step_fields(s_)
    assert 0 <= dly <= 900 and 36 <= gp <= 2000, s_
    assert 575 <= vr <= 825 and v >= FLOOR, s_
    # revert 1 (hand-back) would leave E1 = 0xC2EA, which the NO DIP check below reads as a shot that did not run
    assert 0 <= fs <= 7 and 0 <= hold <= 20 and rev in (0, 2) and (tr == -20 or -20000 <= tr <= -1000), s_
card = Card(PORT)
pico = card.status(); print("pico: %s" % " | ".join(pico.splitlines()), flush=True)
if not re.search(r"fw=lab-psr1-push[12]\b", pico):
    print("STOP: this runner requires lab-psr1-push1 or -push2 firmware"); sys.exit(2)
# `u` every boot, not only when locked: push firmware rewrites reg 0x13 = 0x00 and config ID 0x46 = 0x1229 on every call
# and prints the readback (older firmware skipped both when CML already showed MATCH).
def unlock_checked(c):
    ur = c.cmd("u"); print("unlock: %s" % " | ".join(ur.splitlines()), flush=True)
    m_ = re.search(r"(?m)^unlock MATCH wp=(\S+) cfg=(\S+)(?:\s|$)", ur)
    if not m_ or m_.groups() != ("00", "1229") or "write=ERR" in ur:
        print("STOP: unlock/config readback failed"); sys.exit(2)
unlock_checked(card)
bl = []
for reg, n in ((0x13, 1), (0x46, 2), (0x7E, 1), (0x79, 2), (0x88, 2), (0x8B, 2), (0x8D, 2)):
    try: bl.append("%02x=%s" % (reg, bytes(card.read(0x25, reg, n))[::-1].hex()))
    except IOError: bl.append("%02x=ERR" % reg)
print("boot pmbus (@r 25 <reg> <n>; words shown high byte first): " + " ".join(bl), flush=True)
try: sw0, vo0 = st(card)
except IOError as e: print("STOP: unreadable controller before first shot: %s" % e); sys.exit(9)
if sw0 & 0x40 or not 500 <= vo0 <= 850:
    print("STOP: controller not in a healthy voltage band before first shot: 0x%04x, %d mV" % (sw0, vo0)); sys.exit(9)
with Bar0(BDF) as bar:
    baseline = look(bar)
if baseline["state"] != "healthy_locked":
    print("STOP: unsafe pre-shot fuse baseline: %s" % fmt_obs(baseline)); sys.exit(9)
if SENT:
    SENT_BASE[0] = baseline["registers"]["SENTINEL"]
    want = os.environ.get("SENTINEL_EXPECT", "")
    print("sentinel baseline 0x%x=0x%08x%s" % (SENT, SENT_BASE[0], " (expected %s)" % want if want else ""), flush=True)
    if want and SENT_BASE[0] != int(want, 16):
        print("STOP: sentinel 0x%x reads 0x%08x on this fresh boot, this card's healthy value is %s" % (SENT, SENT_BASE[0], want)); sys.exit(9)
if True:
    for k, (i, step) in enumerate(SEQX):
        if CONT and skip[0] == i: continue
        if CONT and k > 0 and SEQX[k - 1][0] != i: clost[0] = 0
        if PARK_EACH and k > 0:
            if park(card)[0] != 0:
                time.sleep(20)
                rc_, pl_ = park(None)
                if rc_ != 0: print("PARK FAILED before step %d: %s; stop" % (i, pl_)); sys.exit(7)
            card = Card(PORT)
            unlock_checked(card)
        v, td, np_, pw, vr, fs, hold, tr, rev, dly, gp = step_fields(step)
        assert 575 <= vr <= 825
        assert v >= FLOOR
        a = Attempt(v_dip_mv=v, t_dip_us=td, v_restore_mv=vr, t_restore_us=tr, t_revert_us=0, fast=fs, hold_ms=hold,
                    pulses=np_, pw=pw, gap=gp, delay=dly, revert=rev)
        try: rep = card.attempt(a)
        except IOError as e:
            ab = [x.strip() for x in str(e).splitlines() if x.startswith("glitch ABORT")]
            if ab: print("STOP: firmware refused step %d, no shot fired: %s" % (i, ab[0])); tr_dump(); sys.exit(2)
            print("step %d v=%d td=%d pw=%d vr=%d f=%d: nack" % (i, v, td, pw, vr, fs))
            print("   reply: %s" % " | ".join(str(e).splitlines()[-6:]), flush=True)
            print("STOP: shot execution is unknown; power cycle before another shot"); tr_dump(); sys.exit(11)
        # firmware echo (resolved pw/gap and knobs), WARN lines, timing records, release_at, rail_code
        for ln in (getattr(rep, "raw", "") or "").splitlines():
            if ln.startswith(("glitch ", "timing ", "prime_at=", "release_at=", "rail_code=")): print("   " + ln.strip(), flush=True)
        if not getattr(rep, "pwd", False) or getattr(rep, "e1", None) != 0xC2FA:
            print("step %d v=%d td=%d pw=%d vr=%d f=%d: NO DIP (controller pwd=%s e1=0x%04x): the shot did not run; stop" % (i, v, td, pw, vr, fs, getattr(rep, "pwd", "?"), getattr(rep, "e1", 0)), flush=True); tr_dump(); sys.exit(12)
        if getattr(rep, "writes_failed", None) != 0:
            print("STOP: PMBus/setup writes failed or were not reported on step %d: %s" % (i, getattr(rep, "writes_failed", None)), flush=True)
            tr_dump(); sys.exit(11)
        mistimed = release_check(getattr(rep, "raw", "") or "")
        if TRACE:
            try:
                mv = [round(x * 3300.0 / 4096.0, 1) for x in card.trace()]; s_ = tr_summary(mv)
                print("trace step %d v=%d td=%d pw=%d vr=%d f=%d: %s" % (i, v, td, pw, vr, fs, s_), flush=True)
                traces.append({"step": i, "v": v, "td": td, "pw": pw, "vr": vr, "fast": fs, "summary": s_, "trace_mv": mv}); del traces[:-5]
            except Exception as e: print("trace step %d failed: %s" % (i, e))
        time.sleep(0.05)
        try: sw, vo = st(card)
        except IOError as e: print("step %d v=%d td=%d pw=%d vr=%d f=%d: controller unreadable after the shot (%s)" % (i, v, td, pw, vr, fs, e)); print("STOP: power cycle needed"); tr_dump(); sys.exit(9)
        if (sw & 0x40) or vo < 500: print("step %d v=%d td=%d pw=%d vr=%d f=%d: CONTROLLER OFF (0x%04x, %d mV)" % (i, v, td, pw, vr, fs, sw, vo)); print("STOP: power cycle needed"); sys.exit(9)
        out = "lost"
        t_rs = time.monotonic(); ok_rs = restore(BDF, wait_s=RESTORE_WAIT); t_rs = time.monotonic() - t_rs
        if ok_rs and t_rs > 0.4: print("   link back after %.2f s (slow retrain)" % t_rs, flush=True)
        if ok_rs:
            with Bar0(BDF) as bar:
                o = look(bar)
                for _ in range(20):   # GFW boot status 0x01: never go on while it boots; poll up to 5 s
                    if o["state"] != "booting": break
                    time.sleep(0.25); o = look(bar)
                out = verdict(o)
                if os.environ.get("BAD0_FOLLOWUP") == "1" and out.startswith("poisoned FUSECTRL=0xbad0"):
                    # log-only follow-up (2026-11-20): PRI error at FUSECTRL may be a slow boot; one FUSECTRL re-read
                    # after 3 s, and the passive set only if FUSECTRL is back at exact idle. Never changes out.
                    time.sleep(3.0); fc2 = bar.rd32(0x820000)
                    print("   bad0 follow-up (log only, 3 s): FUSECTRL=0x%08x" % fc2, flush=True)
                    if fc2 == 0xE0040000:
                        print("   bad0 follow-up (log only): %s" % fmt_obs(look(bar)), flush=True)
                if mistimed and out in ("under", "GOLDILOCKS"):
                    print("   observed, not scored: %s" % fmt_obs(o), flush=True)
                    out = "MISTIMED (%s; observed %s)" % (mistimed, out)
                if out == "GOLDILOCKS":
                    print("step %d v=%d td=%d pw=%d vr=%d f=%d: GOLDILOCKS passive candidate" % (i, v, td, pw, vr, fs)); print(">>> GOLDILOCKS HELD (%s) v=%d td=%d (passive candidate; validate it actively before anything else)" % (TAG, v, td)); tr_dump(); sys.exit(0)
                if out.startswith("dsov0"):
                    if mistimed: print("   MISTIMED: %s" % mistimed, flush=True)
                    print("step %d v=%d td=%d pw=%d vr=%d f=%d: %s" % (i, v, td, pw, vr, fs, out)); print(">>> PARTIAL HELD (%s); no sense or next shot" % TAG); tr_dump(); sys.exit(13)
                if out not in ("under", "lost") and not out.startswith("MISTIMED"): print("   observed: %s" % fmt_obs(o), flush=True)
        nshots[0] += 1
        if mistimed:
            print("step %d shot %d v=%d td=%d pw=%d vr=%d f=%d: %s" % (i, k, v, td, pw, vr, fs, out), flush=True)
            print("MISTIMED at step %d shot %d (v=%d td=%d pw=%d vr=%d f=%d): %s; outcome %s. Not scored: power cycle (%d shots this boot)" % (i, k, v, td, pw, vr, fs, mistimed, out, nshots[0]))
            tr_dump()
            if "observed GOLDILOCKS" in out: print("   no park: the state stays for review, no burn"); sys.exit(14)
            if out.startswith("booting"): print("   no park: GFW is still booting"); sys.exit(14)
            rc_, pl_ = park(card); print("   park: rc %d %s" % (rc_, pl_), flush=True); sys.exit(14)
        if CONT:
            c_ = cells.setdefault(i, {"under": 0, "lost": 0, "partial": 0, "other": 0})
            kind = "under" if out == "under" else "lost" if out == "lost" else "partial" if (out.startswith("dsov0") and "0xbad" not in out) else "other"
            c_[kind] += 1; lostrun[0] = lostrun[0] + 1 if kind == "lost" else 0
            if kind == "lost":
                prc, pl = park(card)
                if prc != 0:
                    print("   park after lost shot FAILED (rc %d): %s" % (prc, pl), flush=True)
                    print("TIPPED at step %d shot %d (v=%d td=%d pw=%d vr=%d f=%d): lost and the park failed. Power cycle (%d shots this boot)" % (i, k, v, td, pw, vr, fs, nshots[0]))
                    tr_dump(); sys.exit(10)
                card = Card(PORT)
                unlock_checked(card)
                print("   park after lost shot: %s" % pl, flush=True)
            if kind != "under": print("step %d shot %d v=%d td=%d pw=%d vr=%d f=%d: %s" % (i, k, v, td, pw, vr, fs, out), flush=True)
            if k + 1 == len(SEQX) or SEQX[k + 1][0] != i:
                print("cell %d v=%d td=%d np=%d pw=%d vr=%d: %s" % (i, v, td, np_, pw, vr, c_), flush=True)
            clost[0] = clost[0] + 1 if kind == "lost" else 0
            if kind == "lost" and CELLLOST and clost[0] >= CELLLOST and lostrun[0] < LOSTRUN and (k + 1 < len(SEQX) and SEQX[k + 1][0] == i):
                print("cell %d (skipped after %d lost in a row) v=%d td=%d np=%d pw=%d vr=%d: %s" % (i, clost[0], v, td, np_, pw, vr, c_), flush=True); skip[0] = i; continue
            if kind == "under" or (kind == "lost" and lostrun[0] < LOSTRUN):
                if nshots[0] >= SHOTCAP:
                    print("cell %d (cut) v=%d td=%d np=%d pw=%d vr=%d: %s" % (i, v, td, np_, pw, vr, c_), flush=True); ended[0] = "SHOT CAP %d reached" % SHOTCAP; break
                continue
            why = ("%d lost in a row" % lostrun[0]) if kind == "lost" else out
            print("TIPPED at step %d shot %d (v=%d td=%d pw=%d vr=%d f=%d): %s. Power cycle (%d shots this boot)" % (i, k, v, td, pw, vr, fs, why, nshots[0]))
            tr_dump()
            if kind == "lost": sys.exit(10)   # parked right after the shot already
            if out.startswith("booting"): print("   no park: GFW is still booting"); sys.exit(10)
            rc_, pl_ = park(card); print("   park: rc %d %s" % (rc_, pl_), flush=True); sys.exit(10)
        print("step %d v=%d td=%d pw=%d vr=%d f=%d: %s" % (i, v, td, pw, vr, fs, out), flush=True)
        if out != "under":
            print("TIPPED at step %d (v=%d td=%d pw=%d vr=%d f=%d): %s. Rest of this boot is unreliable (sticky poison): power cycle, next boot starts just before this step" % (i, v, td, pw, vr, fs, out))
            tr_dump()
            if out.startswith("booting"): print("   no park: GFW is still booting"); sys.exit(10)
            rc_, pl_ = park(card); print("   park: rc %d %s" % (rc_, pl_), flush=True); sys.exit(10)
tr_dump()
rc_, pl_ = park(card); print("   park: rc %d %s" % (rc_, pl_), flush=True)
print("sequence exhausted, no hold (%d shots%s)" % (nshots[0], "; " + ended[0] if ended[0] else "") if CONT else "sequence exhausted, all under")
