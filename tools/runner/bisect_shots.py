#!/usr/bin/env python3
"""Per-boot runner: shots along a sequence on one boot of the bench host, from the gentlest step to the strongest.
Single mode (REPEAT 1 and no reps field above 1) stops at the first shot that is not "under": poison is sticky until
a power cycle on some cards. Cell mode parks after a lost shot (or, with LOST_NOPARK=1, sends one 100 ms PEX_RST
pulse) and counts outcomes per step. A goldilocks is held, not parked. The controller is checked after every shot.
Never below FLOOR mV.

Usage (root, on the glitch boot, card parked, with the patched ga100vfi package importable):
  VFI_BDF=0000:05:00.0 python3 -I bisect_shots.py '<tag>' '<SEQ json>'
A step is [v_mV, td_us, pulses, pw_us, v_restore_mV, fast, reps, hold_ms, t_restore_us, revert, delay_us, gap_us,
t_revert_us]. t_revert_us (tv, optional, default 0) is when the firmware writes the stock 787.5 mV VOUT after the train;
pin it on overlap scans so only the restore edge moves (docs/OVERLAP_EDGE.md).
Omitted tail fields default to pulses 3, pw 198, v_restore 750, fast 1, reps REPEAT, hold 0, t_restore -20, revert 0,
delay 117, gap 150, tv 0, which is the stock command byte for byte. fast 2..7, hold > 0, t_restore <= -1000 and revert 2
are firmware knobs (firmware/0008); revert 1 is refused because the NO DIP check needs E1 0xC2FA after the shot. The
firmware uses td and delay only as td - delay: delay 330 at td -60 is the same dip as delay 117 at td -273. The log
calls that difference s1, the time of the first dip write relative to the PEX_RST release.

Needs push1, push2 or push3 firmware (fw=lab-psr1-push1, -push2, -push3; push2 releases the first shot of a boot on
time; push3 also runs a PMBus write that is in flight across the release, firmware/0011, docs/PUSH3.md). After each shot BAR0 is read passively (passive_snapshot.py rules, firmware/0004 and 0010: FUSECTRL first,
stop at the first 0xBADF/0xBAD0 or bad read, no writes), then the SENTINEL fuse-alias register. With DSOV 1 the
sentinel must equal this boot's pre-shot baseline; with DSOV 0 (option shadows not sensed, as in a real goldilocks)
it may read 0, and only an error value counts. A shot whose reset release was not 0..4 us after t = 0 by the alarm,
or that had a PMBus write across the release, is MISTIMED: never scored as under or goldilocks; poison and loss still
show; the boot stops (exit 14).

GFW still booting after a shot (a "stall") is polled for GFW_WAIT seconds, then read once more, read-only: FUSECTRL
first (0xBADF/0xBAD0 = poisoned, stop), then GFW, DSOV, CRC, PLM and the sentinel. A stall that shows DSOV 0 is held
(exit 15), unless it is not a goldilocks signature and PARTIAL_CONTINUE=1 and SHOOT_THROUGH_STALLS=1 (cell mode).

Every fired shot gets one machine-readable line for the hunter's tally (tools/hunter/goldilocks_hunt.py):
  SHOT n=<shot of this run> step=<i> v= td= np= pw= vr= fast= delay= gap= s1= first=<0|1> after_park=<0|1>
       stall=<0|1> kind=<under|compass|stall|partial|goldilocks|lost|poison|latch|mistimed|nodip|unknown|other>
       FUSECTRL= GFW= DSOV= CRC= PLM= SENTINEL= | <outcome text>
first=1 marks the first shot of the run: it starts from the GPU's own rail control (PWM-VID, about 694 mV on one
card here) where every later shot starts from a rail held under PMBus, and it behaved differently on the lab's cards.

Environment:
  VFI_BDF (required)   the card's PCI address        VFI_PORT      Pico serial port (default /dev/ttyACM0)
  VFI_PARK             park command (default: park.py next to this file, run with this Python and -I)
  VFI_TRACE_DIR        where TRACE=1 writes its JSON (default: current directory)
  FLOOR (381)          lowest dip allowed, mV        REPEAT (1)    shots per step; > 1 = cell mode
  SHOTCAP (450)        shots per run                 LOSTRUN (8)   lost shots in a row that end a cell-mode run
  CELLLOST (0)         lost shots in a row that skip the rest of a cell (0 = off)
  RESTORE_WAIT (8.0)   seconds to wait for the link after a shot
  GFW_WAIT (5)         seconds to poll a GFW that is still booting after a shot, before the stall read
  SENTINEL (0x820410)  fuse alias register read after every clean read (0 = off)
  SENTINEL_EXPECT      the card's healthy sentinel value; the run stops if this boot's baseline reads otherwise
  PARK_EACH=1          park before every shot after the first, so every shot starts like the first shot of a boot
  LOST_NOPARK=1        cell mode: no park after a lost shot; one 100 ms PEX_RST pulse ('r'), then the link must
                       come back with a healthy locked read (FUSECTRL idle, DSOV 1, CRC 0x55, full mask, sentinel),
                       or the boot ends (exit 10). On the lab's bench the park's hand-back left BAR0 dead.
  POISON_RESET=1       cell mode, opt-in: after a FUSECTRL poison (0xBADF/0xBAD0), poll GFW for up to GFW_WAIT
                       (reads only: rule 2 forbids r while the GFW boot status is 0x01), then one 100 ms PEX_RST
                       pulse ('r') at the stock rail and the same healthy locked read as LOST_NOPARK. Healthy: the
                       boot goes on. Anything else ends the boot (exit 10) as before. Hidden poison and the
                       readable poisoned/over0 frame are not reset. On the lab's card C a sticky 0xBADF510E
                       cleared this way every time (about 6 s instead of a power cycle).
  PARTIAL_CONTINUE=1   cell mode: a readable DSOV 0 frame that is not goldilocks is logged and shooting goes on
                       (default: held, exit 13). Nothing is ever sensed or written by this runner.
  SENT0_CONTINUE=1     opt-in: a clean DSOV 1 frame whose sentinel reads exactly 0 (every other register healthy, an
                       init-phase partial) is logged as a SENT0 frame and the boot goes on; default: hidden poison.
                       Error values and any other mismatch still stop the boot.
  SHOOT_THROUGH_STALLS=1  cell mode, opt-in, default off: fire the next shot while GFW is still stalled. This BREAKS
                       rule 2 of upstream pico-ga100vfi's AGENTS.md (never g, r/j or FLR while GFW boot status is
                       0x01): it can wedge the GFW boot sequencer. A clean host power cycle recovered every time on
                       the lab's bench. Ask the card's owner first.
  STALLRUN (8)         with SHOOT_THROUGH_STALLS=1: stalls in a row that end the boot
  TRACE=1              NVVDD traces (debug firmware)
  BAD0_FOLLOWUP=1      log only: after a 0xBAD0 FUSECTRL read, read FUSECTRL again 3 s later (and the passive set
                       if it is back at idle); the outcome is not changed
  RETRAIN, SBR         refused: no link retrain or secondary bus reset during a campaign
Poison (0xBADF/0xBAD0, hidden poison; a FUSECTRL poison only without POISON_RESET=1) and a controller latch
(STATUS_WORD OFF bit, READ_VOUT below 500 mV) always
end the boot: power cycle before the next shot.
Exit codes: 0 sequence done or goldilocks held, 2 usage, firmware, unlock readback or a step the firmware refused,
7 park failed before a step, 9 controller off or unsafe baseline (power cycle), 10 tipped, 11 nack or failed writes
(the shot's state is unknown), 12 no dip (the controller dropped the shot), 13 partial held, 14 mistimed,
15 DSOV 0 during a GFW stall held (a goldilocks signature in a stall included)."""
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
    / 0xbad00100 while FUSECTRL, DSOV, CRC and PLM were normal (2026-11-20; cause not known). With DSOV 1 the value
    has to match this boot's pre-shot baseline (0x1 on several cards here); 0 and all-ones are failed reads.
    With DSOV 0 the option shadows have not been sensed and the alias reads 0 in a real goldilocks: on one card a
    GFW-ready DSOV 0 / CRC 0x55 / full-mask frame was thrown away as hidden poison for that reason (2026-11-20).
    So with DSOV 0 only an error value (0xBAD....) or all-ones counts as poison."""
    o = observe(bar.rd32); r = o["registers"]
    if SENT and o["state"] in ("healthy_locked", "candidate", "partial_or_corrupt") and not any(bad(x) for x in r.values()):
        try: r["SENTINEL"] = sv = bar.rd32(SENT)
        except OSError as e: o.update(state="unknown", reason="SENTINEL: %s" % e); return o
        dsov0 = r.get("DSOV") == 0
        if SENT0_CONTINUE and sv == 0 and not dsov0 and o["state"] == "healthy_locked":
            print("   SENT0 frame: DSOV=0x%08x CRC=0x%08x PLM=0x%08x sentinel 0 (init-phase partial; boot continues)" % (
                r.get("DSOV", 0), r.get("CRC", 0), r.get("PLM", 0)), flush=True)
        elif bad(sv) or sv == 0xFFFFFFFF or (not dsov0 and (sv == 0 or (SENT_BASE[0] is not None and sv != SENT_BASE[0]))):
            o.update(state="hidden_poison", reason="SENTINEL 0x%x: 0x%08x, boot baseline %s" % (
                SENT, sv, "unset" if SENT_BASE[0] is None else "0x%08x" % SENT_BASE[0]))
    if o["state"] == "candidate" and SENT and "SENTINEL" not in r:
        o.update(state="unknown", reason="SENTINEL: missing candidate check")
    return o
def stall_read(bar):
    """GFW still booting after GFW_WAIT. Upstream's rule 2 forbids g, r/j and FLR in that state, not reads. One
    read-only pass: FUSECTRL first, then GFW, DSOV, CRC, PLM and the sentinel, stopping at the first error value.
    Returns (kind, regs, text): 'ready' (GFW finished meanwhile: read the shot normally), 'poison' (a 0xBADF/0xBAD0
    value), 'unknown' (read error, all-ones, FUSECTRL not idle), 'dsov0' (a readable DSOV 0), else 'stalled'. The
    sentinel is logged here, never scored: a stall read is not a finished boot."""
    regs = {}; txt = []; names = list(OBS_REGS) + ([("SENTINEL", SENT)] if SENT else [])
    for name, addr in names:
        try: v_ = bar.rd32(addr)
        except OSError as e: txt.append("%s: %s" % (name, e)); return "unknown", regs, " ".join(txt)
        regs[name] = v_; txt.append("%s=0x%08x" % (name, v_))
        if bad(v_): txt.append("(poison: stop reads)"); return "poison", regs, " ".join(txt)
        if v_ == 0xFFFFFFFF and name != "PLM": txt.append("(all-ones: stop reads)"); return "unknown", regs, " ".join(txt)
        if name == "FUSECTRL" and v_ != 0xE0040000: txt.append("(FUSECTRL not idle: stop reads)"); return "unknown", regs, " ".join(txt)
        if name == "GFW" and v_ & 0xFF == 0xFF: txt.append("(GFW finished)"); return "ready", regs, " ".join(txt)
    if regs.get("DSOV") == 0: return "dsov0", regs, " ".join(txt)
    return "stalled", regs, " ".join(txt)
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
def is_compass(out):
    """A readable DSOV 1 frame with the privilege mask all ones and CRC other than 0x55 (the community's CRC compass:
    0x57, 0x75, 0x77, ...). Seen fresh on every reset and not sticky, so cell mode logs it and keeps shooting."""
    return out.startswith("dsov1 crc=0x") and out.endswith(" priv=0xffffffff")
def kind_of(out):
    """Outcome text -> the tally's class (the 'kind=' field of the SHOT line)."""
    if out == "under": return "under"
    if out == "lost": return "lost"
    if out == "GOLDILOCKS": return "goldilocks"
    if out.startswith(("poisoned", "hidden-poison")): return "poison"
    if out.startswith("MISTIMED"): return "mistimed"
    if out.startswith("dsov0"): return "partial"
    if is_compass(out): return "compass"
    if out.startswith("booting"): return "stall"
    return "other"
SHOT_REGS = ("FUSECTRL", "GFW", "DSOV", "CRC", "PLM", "SENTINEL")
def shot_line(n, i, f, kind, regs, out, stall=False, after_park=False):
    """One line per fired shot for tools/hunter/goldilocks_hunt.py tally."""
    v, td, np_, pw, vr, fs, hold, tr, rev, dly, gp, tv = f
    rv = " ".join("%s=%s" % (k, "-" if regs.get(k) is None else "0x%08x" % regs[k]) for k in SHOT_REGS)
    print("SHOT n=%d step=%d v=%d td=%d np=%d pw=%d vr=%d fast=%d delay=%d gap=%d s1=%d first=%d after_park=%d stall=%d tv=%d kind=%s %s | %s"
          % (n, i, v, td, np_, max(pw, 36), vr, fs, dly, max(gp, 36), td - dly, int(n == 0), int(after_park), int(stall), tv, kind, rv, out), flush=True)
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
            s_[10] if len(s_) > 10 else 117, s_[11] if len(s_) > 11 else 150, s_[12] if len(s_) > 12 else 0)
PARK_EACH = os.environ.get("PARK_EACH") == "1"
RESTORE_WAIT = float(os.environ.get("RESTORE_WAIT", "8.0"))
GFW_WAIT = float(os.environ.get("GFW_WAIT", "5"))
REPEAT = int(os.environ.get("REPEAT", "1")); CONT = REPEAT > 1
SHOTCAP = int(os.environ.get("SHOTCAP", "450")); LOSTRUN = int(os.environ.get("LOSTRUN", "8"))
SEQX = [(i, s_) for i, s_ in enumerate(SEQ) for _ in range(s_[6] if len(s_) > 6 else REPEAT)]; CONT = CONT or any(len(s_) > 6 and s_[6] > 1 for s_ in SEQ); cells = {}; nshots = [0]; lostrun = [0]; ended = [""]
SENT = int(os.environ.get("SENTINEL", "0x820410"), 16); CELLLOST = int(os.environ.get("CELLLOST", "0")); skip = [-1]; clost = [0]
LOST_NOPARK = os.environ.get("LOST_NOPARK") == "1"
PARTIAL_CONTINUE = os.environ.get("PARTIAL_CONTINUE") == "1"
SENT0_CONTINUE = os.environ.get("SENT0_CONTINUE") == "1"
STALL_THROUGH = os.environ.get("SHOOT_THROUGH_STALLS") == "1"
STALLRUN = int(os.environ.get("STALLRUN", "8")); stallrun = [0]
POISON_RESET = os.environ.get("POISON_RESET") == "1"; npreset = [0]


def poison_reset(card):
    """POISON_RESET: wait (reads only) for GFW to be ready, one 100 ms PEX_RST pulse, then the passive set. Returns the
    passive state ('healthy_locked' means the boot may go on) or why no reset was sent."""
    try:
        g_ = None
        if restore(BDF, wait_s=RESTORE_WAIT):
            with Bar0(BDF) as bar:
                g_ = bar.rd32(0x118234)
                for _ in range(int(GFW_WAIT * 4)):
                    if bad(g_) or (g_ & 0xFF) == 0xFF: break
                    time.sleep(0.25); g_ = bar.rd32(0x118234)
        if g_ is None: return "config not restored before the reset"
        if bad(g_) or (g_ & 0xFF) != 0xFF: return "GFW not ready (0x%08x): no reset (rule 2)" % g_
        rr = card.cmd("r", 0.3, total=2.0, until="released")
        if "released" not in (rr or ""): return "no 'released' reply to r"
        if not restore(BDF, wait_s=3.0): return "link down"
        with Bar0(BDF) as bar:
            o2 = look(bar)
            for _ in range(int(GFW_WAIT * 4)):
                if o2["state"] != "booting": break
                time.sleep(0.25); o2 = look(bar)
        return o2["state"]
    except Exception as e:
        return "reset failed: %s" % e
fired = [0]; parked_before = [False]; last_stall = [False]
if os.environ.get("RETRAIN", "0") != "0" or os.environ.get("SBR", "0") != "0":
    print("STOP: RETRAIN and SBR are not allowed during a campaign (no setpci retrain, no secondary bus reset)"); sys.exit(2)
for s_ in SEQ:   # every step checked before the first shot, not when it comes up
    v, td, np_, pw, vr, fs, hold, tr, rev, dly, gp, tv = step_fields(s_)
    assert 0 <= tv <= 20000, s_
    assert 0 <= dly <= 900 and 36 <= gp <= 2000, s_
    assert 575 <= vr <= 825 and v >= FLOOR, s_
    # revert 1 (hand-back) would leave E1 = 0xC2EA, which the NO DIP check below reads as a shot that did not run
    assert 0 <= fs <= 7 and 0 <= hold <= 20 and rev in (0, 2) and (tr == -20 or -20000 <= tr <= -1000), s_
assert GFW_WAIT >= 0 and STALLRUN >= 1, "GFW_WAIT and STALLRUN"
if STALL_THROUGH:
    print("WARNING: SHOOT_THROUGH_STALLS=1: shots fire while GFW is still booting. This breaks rule 2 of upstream "
          "pico-ga100vfi (never g, r/j or FLR while GFW boot status is 0x01) and can wedge the GFW boot sequencer; a "
          "host power cycle recovered it on the lab's bench. STALLRUN=%d stalls in a row end the boot." % STALLRUN, flush=True)
card = Card(PORT)
pico = card.status(); print("pico: %s" % " | ".join(pico.splitlines()), flush=True)
if not re.search(r"fw=lab-psr1-push[123]\b", pico):
    print("STOP: this runner requires lab-psr1-push1, -push2 or -push3 firmware"); sys.exit(2)
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
print("note: shot 0 of this run starts from the GPU's own rail control (first=1 on its SHOT line); a tally does not count it", flush=True)
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
            parked_before[0] = True
        f = step_fields(step)
        v, td, np_, pw, vr, fs, hold, tr, rev, dly, gp, tv = f
        assert 575 <= vr <= 825
        assert v >= FLOOR
        a = Attempt(v_dip_mv=v, t_dip_us=td, v_restore_mv=vr, t_restore_us=tr, t_revert_us=tv, fast=fs, hold_ms=hold,
                    pulses=np_, pw=pw, gap=gp, delay=dly, revert=rev)
        n_this = fired[0]; ap = parked_before[0]; parked_before[0] = False
        def shot(kind, regs, out, stall=False):
            shot_line(n_this, i, f, kind, regs, out, stall, ap)
        try: rep = card.attempt(a)
        except IOError as e:
            ab = [x.strip() for x in str(e).splitlines() if x.startswith("glitch ABORT")]
            if ab: print("STOP: firmware refused step %d, no shot fired: %s" % (i, ab[0])); tr_dump(); sys.exit(2)
            fired[0] += 1
            print("step %d v=%d td=%d pw=%d vr=%d f=%d: nack" % (i, v, td, pw, vr, fs))
            print("   reply: %s" % " | ".join(str(e).splitlines()[-6:]), flush=True)
            shot("unknown", {}, "nack")
            print("STOP: shot execution is unknown; power cycle before another shot"); tr_dump(); sys.exit(11)
        fired[0] += 1
        # firmware echo (resolved pw/gap and knobs), WARN lines, timing records, release_at, rail_code
        for ln in (getattr(rep, "raw", "") or "").splitlines():
            if ln.startswith(("glitch ", "timing ", "prime_at=", "release_at=", "rail_code=")): print("   " + ln.strip(), flush=True)
        if not getattr(rep, "pwd", False) or getattr(rep, "e1", None) != 0xC2FA:
            print("step %d v=%d td=%d pw=%d vr=%d f=%d: NO DIP (controller pwd=%s e1=0x%04x): the shot did not run; stop" % (i, v, td, pw, vr, fs, getattr(rep, "pwd", "?"), getattr(rep, "e1", 0)), flush=True)
            shot("nodip", {}, "NO DIP"); tr_dump(); sys.exit(12)
        if getattr(rep, "writes_failed", None) != 0:
            print("STOP: PMBus/setup writes failed or were not reported on step %d: %s" % (i, getattr(rep, "writes_failed", None)), flush=True)
            shot("unknown", {}, "writes failed"); tr_dump(); sys.exit(11)
        mistimed = release_check(getattr(rep, "raw", "") or "")
        if TRACE:
            try:
                mv = [round(x * 3300.0 / 4096.0, 1) for x in card.trace()]; s_ = tr_summary(mv)
                print("trace step %d v=%d td=%d pw=%d vr=%d f=%d: %s" % (i, v, td, pw, vr, fs, s_), flush=True)
                traces.append({"step": i, "v": v, "td": td, "pw": pw, "vr": vr, "fast": fs, "summary": s_, "trace_mv": mv}); del traces[:-5]
            except Exception as e: print("trace step %d failed: %s" % (i, e))
        time.sleep(0.05)
        try: sw, vo = st(card)
        except IOError as e:
            print("step %d v=%d td=%d pw=%d vr=%d f=%d: controller unreadable after the shot (%s)" % (i, v, td, pw, vr, fs, e))
            shot("unknown", {}, "controller unreadable"); print("STOP: power cycle needed"); tr_dump(); sys.exit(9)
        if (sw & 0x40) or vo < 500:
            print("step %d v=%d td=%d pw=%d vr=%d f=%d: CONTROLLER OFF (0x%04x, %d mV)" % (i, v, td, pw, vr, fs, sw, vo))
            shot("latch", {}, "CONTROLLER OFF (0x%04x, %d mV)" % (sw, vo)); print("STOP: power cycle needed"); sys.exit(9)
        out = "lost"; regs = {}; stalled = False; last_stall[0] = False
        t_rs = time.monotonic(); ok_rs = restore(BDF, wait_s=RESTORE_WAIT); t_rs = time.monotonic() - t_rs
        if ok_rs and t_rs > 0.4: print("   link back after %.2f s (slow retrain)" % t_rs, flush=True)
        if ok_rs:
            with Bar0(BDF) as bar:
                o = look(bar)
                for _ in range(int(GFW_WAIT * 4)):   # GFW boot status 0x01: never go on while it boots; poll up to GFW_WAIT
                    if o["state"] != "booting": break
                    time.sleep(0.25); o = look(bar)
                sk = None
                if o["state"] == "booting":
                    sk, sregs, stxt = stall_read(bar)
                    print("   stall read (GFW still booting after %g s; reads only, no g/r/j): %s" % (GFW_WAIT, stxt), flush=True)
                    if sk == "ready": o = look(bar); sk = None
                regs = dict(o["registers"])
                out = verdict(o)
                if sk is not None:
                    stalled = True; regs.update(sregs)
                    if sk == "poison":
                        name = [n_ for n_ in SHOT_REGS if bad(sregs.get(n_))][0]
                        out = "poisoned %s=0x%08x (stall read)" % (name, sregs[name])
                    elif sk == "unknown":
                        out = "unread (stall read: %s)" % stxt
                    elif sk == "dsov0":
                        gold = sregs.get("CRC") == 0x55 and sregs.get("PLM") == 0xFFFFFFFF
                        if not gold and PARTIAL_CONTINUE and CONT and STALL_THROUGH and not mistimed:
                            out = "dsov0 crc=0x%02x priv=0x%08x" % (sregs.get("CRC", 0) & 0xFF, sregs.get("PLM", 0))
                            print("step %d shot %d v=%d td=%d pw=%d vr=%d f=%d: STALL PARTIAL %s" % (i, k, v, td, pw, vr, fs, stxt), flush=True)
                        else:
                            what = "a goldilocks signature (DSOV 0, CRC 0x55, full mask)" if gold else "DSOV 0"
                            print("step %d shot %d v=%d td=%d pw=%d vr=%d f=%d: STALL DSOV0 %s" % (i, k, v, td, pw, vr, fs, stxt), flush=True)
                            if mistimed: print("   MISTIMED: %s" % mistimed, flush=True)
                            shot("goldilocks" if gold else "partial", regs, "STALL DSOV0 " + stxt, stall=True)
                            print(">>> STALL DSOV0 HELD (%s): %s while GFW is stalled; no sense, no next shot, no g/r/j; review it" % (TAG, what))
                            tr_dump(); sys.exit(15)
                    else:   # stalled: GFW still booting, DSOV not 0
                        out = "booting GFW=0x%08x (GFW stall)" % sregs.get("GFW", o["registers"]["GFW"] or 0)
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
                if out == "GOLDILOCKS" and npreset[0]:
                    print("   note: this boot had %d poison reset(s) before this hold" % npreset[0], flush=True)
                if out == "GOLDILOCKS":
                    shot("goldilocks", regs, out)
                    print("step %d v=%d td=%d pw=%d vr=%d f=%d: GOLDILOCKS passive candidate" % (i, v, td, pw, vr, fs)); print(">>> GOLDILOCKS HELD (%s) v=%d td=%d (passive candidate; validate it actively before anything else)" % (TAG, v, td)); tr_dump(); sys.exit(0)
                if out.startswith("dsov0") and not (PARTIAL_CONTINUE and CONT and not mistimed):
                    if mistimed: print("   MISTIMED: %s" % mistimed, flush=True)
                    shot("partial", regs, out, stalled)
                    print("step %d v=%d td=%d pw=%d vr=%d f=%d: %s" % (i, v, td, pw, vr, fs, out)); print(">>> PARTIAL HELD (%s); no sense or next shot" % TAG); tr_dump(); sys.exit(13)
                if out not in ("under", "lost") and not out.startswith("MISTIMED"): print("   observed: %s" % fmt_obs(o), flush=True)
        nshots[0] += 1; last_stall[0] = stalled
        kind = kind_of(out)
        shot(kind, regs, out, stalled)
        if mistimed:
            print("step %d shot %d v=%d td=%d pw=%d vr=%d f=%d: %s" % (i, k, v, td, pw, vr, fs, out), flush=True)
            print("MISTIMED at step %d shot %d (v=%d td=%d pw=%d vr=%d f=%d): %s; outcome %s. Not scored: power cycle (%d shots this boot)" % (i, k, v, td, pw, vr, fs, mistimed, out, nshots[0]))
            tr_dump()
            if "observed GOLDILOCKS" in out: print("   no park: the state stays for review, no burn"); sys.exit(14)
            if stalled: print("   no park: GFW is still booting"); sys.exit(14)
            rc_, pl_ = park(card); print("   park: rc %d %s" % (rc_, pl_), flush=True); sys.exit(14)
        if CONT:
            c_ = cells.setdefault(i, {"under": 0, "lost": 0, "partial": 0, "other": 0})
            ck = kind if kind in ("under", "lost", "partial") else "compass" if kind == "compass" else "stall" if (kind == "stall" and STALL_THROUGH) else "other"
            c_[ck] = c_.get(ck, 0) + 1; lostrun[0] = lostrun[0] + 1 if ck == "lost" else 0
            if ck == "lost" and not LOST_NOPARK:
                prc, pl = park(card)
                if prc != 0:
                    print("   park after lost shot FAILED (rc %d): %s" % (prc, pl), flush=True)
                    print("TIPPED at step %d shot %d (v=%d td=%d pw=%d vr=%d f=%d): lost and the park failed. Power cycle (%d shots this boot)" % (i, k, v, td, pw, vr, fs, nshots[0]))
                    tr_dump(); sys.exit(10)
                card = Card(PORT)
                unlock_checked(card)
                parked_before[0] = True
                print("   park after lost shot: %s" % pl, flush=True)
            elif ck == "lost":
                # LOST_NOPARK: one plain 100 ms PEX_RST pulse, no voltage change, then the passive set. A card that does
                # not come back healthy and locked ends the boot for a power cycle instead of feeding later shots.
                hr = "link down"
                try: rr = card.cmd("r", 0.3, total=2.0, until="released")
                except Exception as e: rr = ""; print("   recovery reset failed: %s" % e, flush=True)
                if "released" not in (rr or ""):
                    hr = "no 'released' reply to r"
                elif restore(BDF, wait_s=3.0):
                    with Bar0(BDF) as bar:
                        o2 = look(bar)
                        for _ in range(int(GFW_WAIT * 4)):
                            if o2["state"] != "booting": break
                            time.sleep(0.25); o2 = look(bar)
                    hr = o2["state"]
                print("   recovery reset after lost shot: %s" % hr, flush=True)
                if hr != "healthy_locked":
                    print("step %d shot %d v=%d td=%d pw=%d vr=%d f=%d: %s" % (i, k, v, td, pw, vr, fs, out), flush=True)
                    print("TIPPED at step %d shot %d (v=%d td=%d pw=%d vr=%d f=%d): lost and not recovered by a plain reset (%s). Power cycle (%d shots this boot)" % (i, k, v, td, pw, vr, fs, hr, nshots[0]))
                    tr_dump(); sys.exit(10)
            if ck != "under": print("step %d shot %d v=%d td=%d pw=%d vr=%d f=%d: %s" % (i, k, v, td, pw, vr, fs, out), flush=True)
            if k + 1 == len(SEQX) or SEQX[k + 1][0] != i:
                print("cell %d v=%d td=%d np=%d pw=%d vr=%d: %s" % (i, v, td, np_, pw, vr, c_), flush=True)
            clost[0] = clost[0] + 1 if ck == "lost" else 0
            if ck == "lost" and CELLLOST and clost[0] >= CELLLOST and lostrun[0] < LOSTRUN and (k + 1 < len(SEQX) and SEQX[k + 1][0] == i):
                print("cell %d (skipped after %d lost in a row) v=%d td=%d np=%d pw=%d vr=%d: %s" % (i, clost[0], v, td, np_, pw, vr, c_), flush=True); skip[0] = i; continue
            stallrun[0] = stallrun[0] + 1 if stalled else 0
            if stalled and ck in ("stall", "partial") and stallrun[0] > STALLRUN:
                print("TIPPED at step %d shot %d (v=%d td=%d pw=%d vr=%d f=%d): %d GFW stalls in a row. Power cycle (%d shots this boot)" % (i, k, v, td, pw, vr, fs, stallrun[0], nshots[0]))
                print("   no park: GFW is still booting"); tr_dump(); sys.exit(10)
            if ck in ("under", "compass", "stall") or (ck == "partial" and PARTIAL_CONTINUE) or (ck == "lost" and lostrun[0] < LOSTRUN):
                if nshots[0] >= SHOTCAP:
                    print("cell %d (cut) v=%d td=%d np=%d pw=%d vr=%d: %s" % (i, v, td, np_, pw, vr, c_), flush=True); ended[0] = "SHOT CAP %d reached" % SHOTCAP; break
                continue
            if POISON_RESET and out.startswith("poisoned "):
                hr = poison_reset(card)
                print("   poison reset (100 ms PEX_RST): %s" % hr, flush=True)
                if hr == "healthy_locked":
                    npreset[0] += 1
                    if nshots[0] >= SHOTCAP:
                        print("cell %d (cut) v=%d td=%d np=%d pw=%d vr=%d: %s" % (i, v, td, np_, pw, vr, c_), flush=True); ended[0] = "SHOT CAP %d reached" % SHOTCAP; break
                    continue
            why = ("%d lost in a row" % lostrun[0]) if ck == "lost" else out
            print("TIPPED at step %d shot %d (v=%d td=%d pw=%d vr=%d f=%d): %s. Power cycle (%d shots this boot)" % (i, k, v, td, pw, vr, fs, why, nshots[0]))
            tr_dump()
            if ck == "lost": sys.exit(10)   # parked right after the shot, or recovered by the reset pulse: power cycle next
            if stalled: print("   no park: GFW is still booting"); sys.exit(10)
            rc_, pl_ = park(card); print("   park: rc %d %s" % (rc_, pl_), flush=True); sys.exit(10)
        print("step %d v=%d td=%d pw=%d vr=%d f=%d: %s" % (i, v, td, pw, vr, fs, out), flush=True)
        if out != "under":
            print("TIPPED at step %d (v=%d td=%d pw=%d vr=%d f=%d): %s. Rest of this boot is unreliable (sticky poison): power cycle, next boot starts just before this step" % (i, v, td, pw, vr, fs, out))
            tr_dump()
            if stalled: print("   no park: GFW is still booting"); sys.exit(10)
            rc_, pl_ = park(card); print("   park: rc %d %s" % (rc_, pl_), flush=True); sys.exit(10)
tr_dump()
if last_stall[0]:
    print("   no park: GFW is still booting after the last shot (power cycle)", flush=True)
else:
    rc_, pl_ = park(card); print("   park: rc %d %s" % (rc_, pl_), flush=True)
print("sequence exhausted, no hold (%d shots%s)" % (nshots[0], "; " + ended[0] if ended[0] else "") if CONT else "sequence exhausted, all under")
