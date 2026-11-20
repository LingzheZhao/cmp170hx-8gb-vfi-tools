#!/usr/bin/env python3
"""One-boot bisection step (poison is sticky until a power cycle on some cards): single back-to-back shots along a
sequence ordered from weakest to strongest effect; stop at the FIRST shot that is not 'under'. Goldilocks -> hold.
Controller checked after every shot. Never below FLOOR mV.
Step: [v_mV, td_us, pulses, pw_us, v_restore_mV, fast, reps, hold_ms, t_restore_us, revert]. Omitted tail fields keep the
old behaviour (pulses 3, pw 198, v_restore 750, fast 1, reps REPEAT, hold 0, t_restore -20, revert 0). fast 2..7,
hold > 0, t_restore <= -1000 and revert 2 are lab-psr1-push1 firmware knobs (experimental/firmware); the runner refuses them
on other firmware; revert 1 is refused. After each shot BAR0 is read passively (passive_snapshot.py rules: FUSECTRL first, stop at the
first 0xBADF or bad read, no writes), then the SENTINEL fuse-alias register.

EXPERIMENTAL: not run on hardware. Usage and environment as tools/runner/bisect_shots.py (VFI_BDF required,
VFI_PORT, VFI_PARK, VFI_TRACE_DIR, FLOOR, REPEAT, SHOTCAP, LOSTRUN, CELLLOST, RESTORE_WAIT, SENTINEL, PARK_EACH,
TRACE, RETRAIN, SBR)."""
import re, sys, time, subprocess, json, shlex, tempfile
from ga100vfi.glitch import Attempt, Card
from ga100vfi.bar0 import Bar0, restore
from ga100vfi.cli import _ensure_unlocked
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
# Passive BAR0 observer: the reads and stop rules of passive_snapshot.observe (firmware/0004), copied because the bench
# runs this file with python -I where that module is not importable (test_bisect_shots.py checks they agree).
# FUSECTRL first; stop at the first 0xBADF value, I/O error, unexpected all-ones, GFW booting or DSOV other than 0/1.
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
        if value == 0xFFFFFFFF and name != "PLM":
            result["reason"] = f"{name}: all-ones response"
            return result
        if name == "GFW" and value & 0xFF == 1:
            result.update(state="booting", reason="GFW low byte is 0x01")
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
def look(bar):
    """observe(), then the SENTINEL alias register once the five read cleanly. After a run scored all "under", the bench
    preflight once found fuse alias registers (0x820324, 0x820410, per-GPC TPC/PES) reading 0xbadf1020 / 0xbad00100
    while FUSECTRL, DSOV, CRC and PLM were normal (2026-11-20; cause not known)."""
    o = observe(bar.rd32); r = o["registers"]
    if SENT and o["state"] in ("healthy_locked", "candidate", "partial_or_corrupt") and not any(bad(x) for x in r.values()):
        try: r["SENTINEL"] = sv = bar.rd32(SENT)
        except OSError as e: o.update(state="unknown", reason="SENTINEL: %s" % e); return o
        if bad(sv): o.update(state="hidden_poison", reason="SENTINEL 0x%x: 0x%08x" % (SENT, sv))
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
    if r["DSOV"] == 1: return "under"
    if s_ == "candidate": return "GOLDILOCKS"
    return "dsov0 crc=0x%02x priv=0x%08x" % (r["CRC"], r["PLM"])
def fmt_obs(o):
    return " ".join("%s=%s" % (k, "-" if x is None else "0x%08x" % x) for k, x in o["registers"].items()) + " state=%s reason=%s" % (o["state"], o["reason"])
def step_fields(s_):
    return (s_[0], s_[1], s_[2] if len(s_) > 2 else 3, s_[3] if len(s_) > 3 else 198, s_[4] if len(s_) > 4 else 750,
            s_[5] if len(s_) > 5 else 1, s_[7] if len(s_) > 7 else 0, s_[8] if len(s_) > 8 else -20, s_[9] if len(s_) > 9 else 0)
PARK_EACH = os.environ.get("PARK_EACH") == "1"
RESTORE_WAIT = float(os.environ.get("RESTORE_WAIT", "8.0"))
RETRAIN = os.environ.get("RETRAIN", "0") == "1"
REPEAT = int(os.environ.get("REPEAT", "1")); CONT = REPEAT > 1
SHOTCAP = int(os.environ.get("SHOTCAP", "450")); LOSTRUN = int(os.environ.get("LOSTRUN", "8"))
SEQX = [(i, s_) for i, s_ in enumerate(SEQ) for _ in range(s_[6] if len(s_) > 6 else REPEAT)]; CONT = CONT or any(len(s_) > 6 and s_[6] > 1 for s_ in SEQ); cells = {}; nshots = [0]; lostrun = [0]; ended = [""]
SENT = int(os.environ.get("SENTINEL", "0x820410"), 16); CELLLOST = int(os.environ.get("CELLLOST", "0")); skip = [-1]; clost = [0]
SBR_USED = [False]
for s_ in SEQ:   # every step checked before the first shot, not when it comes up
    v, td, np_, pw, vr, fs, hold, tr, rev = step_fields(s_)
    assert 575 <= vr <= 825 and v >= FLOOR, s_
    # revert 1 (hand-back) would leave E1 = 0xC2EA, which the NO DIP check below reads as a shot that did not run
    assert 0 <= fs <= 7 and 0 <= hold <= 20 and rev in (0, 2) and (tr == -20 or -20000 <= tr <= -1000), s_
card = Card(PORT)
pico = card.status(); print("pico: %s" % " | ".join(pico.splitlines()), flush=True)
if "fw=lab-psr1-push" not in pico and any(f_[5] > 1 or f_[6] > 0 or f_[7] <= -1000 or f_[8] == 2 for f_ in map(step_fields, SEQ)):
    print("STOP: the sequence uses push1 knobs (fast 2..7, hold, t_restore <= -1000, revert 2) and the Pico is not on lab-psr1-push firmware"); sys.exit(2)
# `u` every boot, not only when locked: push1 firmware rewrites reg 0x13 = 0x00 and config ID 0x46 = 0x1229 on every call
# and prints the readback (older firmware skipped both when CML already showed MATCH).
ur = card.cmd("u"); print("unlock: %s" % " | ".join(ur.splitlines()), flush=True)
if "MATCH" not in ur: print("unlock failed"); sys.exit(2)
m_ = re.search(r"unlock MATCH wp=(\S+) cfg=(\S+)", ur)
if not m_ or m_.groups() != ("00", "1229"): print("WARNING: unlock readback %s, expected wp=00 cfg=1229" % (" ".join(m_.groups()) if m_ else "missing"), flush=True)
bl = []
for reg, n in ((0x13, 1), (0x46, 2), (0x7E, 1), (0x79, 2), (0x88, 2), (0x8B, 2), (0x8D, 2)):
    try: bl.append("%02x=%s" % (reg, bytes(card.read(0x25, reg, n))[::-1].hex()))
    except IOError: bl.append("%02x=ERR" % reg)
print("boot pmbus (@r 25 <reg> <n>; words shown high byte first): " + " ".join(bl), flush=True)
if True:
    for k, (i, step) in enumerate(SEQX):
        if CONT and skip[0] == i: continue
        if CONT and k > 0 and SEQX[k - 1][0] != i: clost[0] = 0
        if PARK_EACH and k > 0:
            card.close()
            if subprocess.call(PARK, cwd=TMP, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL) != 0:
                time.sleep(20)
                if subprocess.call(PARK, cwd=TMP, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL) != 0: print("PARK FAILED before step %d: stop" % i); sys.exit(7)
            card = Card(PORT)
            if not _ensure_unlocked(card): print("unlock failed after park"); sys.exit(2)
        v, td, np_, pw, vr, fs, hold, tr, rev = step_fields(step)
        assert 575 <= vr <= 825
        assert v >= FLOOR
        a = Attempt(v_dip_mv=v, t_dip_us=td, v_restore_mv=vr, t_restore_us=tr, t_revert_us=0, fast=fs, hold_ms=hold,
                    pulses=np_, pw=pw, gap=150, delay=117, revert=rev)
        try: rep = card.attempt(a)
        except IOError as e:
            print("step %d v=%d td=%d pw=%d vr=%d f=%d: nack" % (i, v, td, pw, vr, fs))
            print("   reply: %s" % " | ".join(str(e).splitlines()[-6:]), flush=True); _ensure_unlocked(card); continue
        # firmware echo (resolved pw/gap and knobs), WARN lines, timing records, release_at, rail_code
        for ln in (getattr(rep, "raw", "") or "").splitlines():
            if ln.startswith(("glitch ", "timing ", "release_at=", "rail_code=")): print("   " + ln.strip(), flush=True)
        if not getattr(rep, "pwd", True) or getattr(rep, "e1", 0xC2FA) != 0xC2FA:
            print("step %d v=%d td=%d pw=%d vr=%d f=%d: NO DIP (controller pwd=%s e1=0x%04x): the shot did not run; stop" % (i, v, td, pw, vr, fs, getattr(rep, "pwd", "?"), getattr(rep, "e1", 0)), flush=True); tr_dump(); sys.exit(12)
        if getattr(rep, "writes_failed", 0): print("   PMBus writes failed on step %d: %d" % (i, rep.writes_failed), flush=True)
        if TRACE:
            try:
                mv = [round(x * 3300.0 / 4096.0, 1) for x in card.trace()]; s_ = tr_summary(mv)
                print("trace step %d v=%d td=%d pw=%d vr=%d f=%d: %s" % (i, v, td, pw, vr, fs, s_), flush=True)
                traces.append({"step": i, "v": v, "td": td, "pw": pw, "vr": vr, "fast": fs, "summary": s_, "trace_mv": mv}); del traces[:-5]
            except Exception as e: print("trace step %d failed: %s" % (i, e))
        time.sleep(0.05); sw, vo = st(card)
        if (sw & 0x40) or vo < 500: print("step %d v=%d td=%d pw=%d vr=%d f=%d: CONTROLLER OFF (0x%04x, %d mV)" % (i, v, td, pw, vr, fs, sw, vo)); print("STOP: power cycle needed"); sys.exit(9)
        out = "lost"
        t_rs = time.monotonic(); ok_rs = restore(BDF, wait_s=RESTORE_WAIT); t_rs = time.monotonic() - t_rs
        if ok_rs and t_rs > 0.4: print("   link back after %.2f s (slow retrain)" % t_rs, flush=True)
        if not ok_rs and RETRAIN:
            try:
                rp = os.path.basename(os.path.dirname(os.path.realpath("/sys/bus/pci/devices/" + BDF)))
                subprocess.call(["setpci", "-s", rp, "CAP_EXP+10.w=0020:0020"])
                time.sleep(0.3)
                ok_rs = restore(BDF, wait_s=2.0)
                print("   link retrain on %s after the shot: %s" % (rp, "RECOVERED" if ok_rs else "still down"), flush=True)
                if not ok_rs and os.environ.get("SBR", "0") == "1":
                    subprocess.call(["setpci", "-s", rp, "BRIDGE_CONTROL=0040:0040"]); time.sleep(0.01)
                    subprocess.call(["setpci", "-s", rp, "BRIDGE_CONTROL=0000:0040"]); time.sleep(0.5)
                    ok_rs = restore(BDF, wait_s=2.0)
                    print("   secondary bus reset on %s: %s" % (rp, "RECOVERED (classifying the post-hot-reset state)" if ok_rs else "still down"), flush=True)
                    if ok_rs: SBR_USED[0] = True
            except Exception as e: print("   retrain failed: %s" % e)
        if ok_rs:
            with Bar0(BDF) as bar:
                o = look(bar)
                for _ in range(20):   # GFW boot status 0x01: never go on while it boots; poll up to 5 s
                    if o["state"] != "booting": break
                    time.sleep(0.25); o = look(bar)
                out = verdict(o)
                if out == "GOLDILOCKS":
                    print("step %d v=%d td=%d pw=%d vr=%d f=%d: GOLDILOCKS" % (i, v, td, pw, vr, fs)); print(">>> GOLDILOCKS HELD (%s) v=%d td=%d" % (TAG, v, td)); tr_dump(); sys.exit(0)
                if out not in ("under", "lost"): print("   observed: %s" % fmt_obs(o), flush=True)
                if o["state"] != "poisoned" and any(bad(o["registers"][k_]) for k_ in ("DSOV", "CRC", "PLM")):
                    time.sleep(2.0); o2 = look(bar); out2 = verdict(o2)
                    print("   half-boot (0xBAD.. reads); re-read after 2 s: %s" % fmt_obs(o2), flush=True)
                    if out2 == "GOLDILOCKS":
                        print("step %d v=%d td=%d pw=%d vr=%d f=%d: GOLDILOCKS (after re-read)" % (i, v, td, pw, vr, fs)); print(">>> GOLDILOCKS HELD (%s) v=%d td=%d" % (TAG, v, td)); tr_dump(); sys.exit(0)
                    out = out + " | re-read " + out2
        if SBR_USED[0] and out != "lost": out = "after-hot-reset " + out
        nshots[0] += 1
        if CONT:
            c_ = cells.setdefault(i, {"under": 0, "lost": 0, "partial": 0, "other": 0})
            kind = "under" if out == "under" else "lost" if out == "lost" else "partial" if (out.startswith("dsov0") and "0xbad" not in out) else "other"
            c_[kind] += 1; lostrun[0] = lostrun[0] + 1 if kind == "lost" else 0
            if kind == "lost":
                card.close()
                pr = subprocess.run(PARK, cwd=TMP, capture_output=True, text=True)
                pl = ((pr.stdout or "").strip().splitlines() or ["(no output)"])[-1]
                card = Card(PORT)
                if not _ensure_unlocked(card): print("unlock failed after park"); sys.exit(2)
                if pr.returncode != 0:
                    print("   park after lost shot FAILED (rc %d): %s" % (pr.returncode, pl), flush=True)
                    print("TIPPED at step %d shot %d (v=%d td=%d pw=%d vr=%d f=%d): lost and the park failed. Power cycle (%d shots this boot)" % (i, k, v, td, pw, vr, fs, nshots[0]))
                    tr_dump(); sys.exit(10)
                print("   park after lost shot: %s" % pl, flush=True)
            if kind != "under": print("step %d shot %d v=%d td=%d pw=%d vr=%d f=%d: %s" % (i, k, v, td, pw, vr, fs, out), flush=True)
            if k + 1 == len(SEQX) or SEQX[k + 1][0] != i:
                print("cell %d v=%d td=%d np=%d pw=%d vr=%d: %s" % (i, v, td, np_, pw, vr, c_), flush=True)
            clost[0] = clost[0] + 1 if kind == "lost" else 0
            if kind == "lost" and CELLLOST and clost[0] >= CELLLOST and lostrun[0] < LOSTRUN and (k + 1 < len(SEQX) and SEQX[k + 1][0] == i):
                print("cell %d (skipped after %d lost in a row) v=%d td=%d np=%d pw=%d vr=%d: %s" % (i, clost[0], v, td, np_, pw, vr, c_), flush=True); skip[0] = i; continue
            if kind in ("under", "partial") or (kind == "lost" and lostrun[0] < LOSTRUN):
                if nshots[0] >= SHOTCAP:
                    print("cell %d (cut) v=%d td=%d np=%d pw=%d vr=%d: %s" % (i, v, td, np_, pw, vr, c_), flush=True); ended[0] = "SHOT CAP %d reached" % SHOTCAP; break
                continue
            why = ("%d lost in a row" % lostrun[0]) if kind == "lost" else out
            print("TIPPED at step %d shot %d (v=%d td=%d pw=%d vr=%d f=%d): %s. Power cycle (%d shots this boot)" % (i, k, v, td, pw, vr, fs, why, nshots[0]))
            tr_dump(); subprocess.call(PARK, cwd=TMP, stdout=subprocess.DEVNULL); sys.exit(10)
        print("step %d v=%d td=%d pw=%d vr=%d f=%d: %s" % (i, v, td, pw, vr, fs, out), flush=True)
        if out != "under":
            print("TIPPED at step %d (v=%d td=%d pw=%d vr=%d f=%d): %s. Rest of this boot is unreliable (sticky poison): power cycle, next boot starts just before this step" % (i, v, td, pw, vr, fs, out))
            tr_dump(); subprocess.call(PARK, cwd=TMP, stdout=subprocess.DEVNULL); sys.exit(10)
tr_dump(); card.close()
subprocess.call(PARK, cwd=TMP, stdout=subprocess.DEVNULL)
print("sequence exhausted, no hold (%d shots%s)" % (nshots[0], "; " + ended[0] if ended[0] else "") if CONT else "sequence exhausted, all under")
