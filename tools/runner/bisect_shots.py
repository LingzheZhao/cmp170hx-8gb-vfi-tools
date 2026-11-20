#!/usr/bin/env python3
"""One-boot bisection step (poison is sticky until a power cycle on some cards): single back-to-back shots along a
sequence ordered from weakest to strongest effect; stop at the FIRST shot that is not 'under'. Goldilocks -> hold.
Controller checked after every shot. Never below FLOOR mV.

Usage (root, on the glitch boot, card parked, with the patched ga100vfi package importable):
  VFI_BDF=0000:05:00.0 python3 -I bisect_shots.py '<tag>' '<SEQ json>'
SEQ is a list of steps [v_mV, td_us, pulses, pw_us, v_restore_mV, fast, reps]; omitted tail fields default to
3 pulses, 198 us, 750 mV, fast 1, reps REPEAT. Every shot: gap 150 us, delay 117 us, revert 0 (the rail stays held
under PMBus control between shots).

Environment:
  VFI_BDF (required)   the card's PCI address        VFI_PORT      Pico serial port (default /dev/ttyACM0)
  VFI_PARK             park command (default: park.py next to this file, run with this Python and -I)
  VFI_TRACE_DIR        where TRACE=1 writes its JSON (default: current directory)
  FLOOR (381)          lowest dip allowed, mV        REPEAT (1)    shots per step; > 1 = cell mode
  SHOTCAP (450)        shots per run                 LOSTRUN (8)   lost shots in a row that end a cell-mode run
  CELLLOST (0)         lost shots in a row that skip the rest of a cell (0 = off)
  RESTORE_WAIT (8.0)   seconds to wait for the link after a shot
  SENTINEL (0x820410)  fuse alias register read after an "under" shot; 0xBAD..... stops the run (0 = off)
  PARK_EACH=1          park before every shot (first-shot mode)       TRACE=1  NVVDD traces (debug firmware)
  RETRAIN=1, SBR=1     link retrain / secondary bus reset after a lost shot. Off by default: do not use during
                       a campaign.
  CLASSIFY_WRITE=1     let classify() write EN_SW_OVERRIDE (0x820040) after every shot (firmware/0005; off by
                       default)
Exit codes: 0 sequence done or goldilocks held, 2 unlock failed, 7 park failed before a step, 9 controller off
(power cycle), 10 tipped (first non-under shot, or a failed park after a lost shot), 12 no dip (the controller
refused the shot)."""
import sys, time, subprocess, json, shlex, tempfile
from ga100vfi.glitch import Attempt, Card
from ga100vfi.bar0 import Bar0, restore
from ga100vfi.classify import classify
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
PARK_EACH = os.environ.get("PARK_EACH") == "1"
RESTORE_WAIT = float(os.environ.get("RESTORE_WAIT", "8.0"))
RETRAIN = os.environ.get("RETRAIN", "0") == "1"
REPEAT = int(os.environ.get("REPEAT", "1")); CONT = REPEAT > 1
SHOTCAP = int(os.environ.get("SHOTCAP", "450")); LOSTRUN = int(os.environ.get("LOSTRUN", "8"))
SEQX = [(i, s_) for i, s_ in enumerate(SEQ) for _ in range(s_[6] if len(s_) > 6 else REPEAT)]; CONT = CONT or any(len(s_) > 6 and s_[6] > 1 for s_ in SEQ); cells = {}; nshots = [0]; lostrun = [0]; ended = [""]
SENT = int(os.environ.get("SENTINEL", "0x820410"), 16); CELLLOST = int(os.environ.get("CELLLOST", "0")); skip = [-1]; clost = [0]
SBR_USED = [False]
card = Card(PORT)
if not _ensure_unlocked(card): print("unlock failed"); sys.exit(2)
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
        v, td = step[0], step[1]; np_ = step[2] if len(step) > 2 else 3; pw = step[3] if len(step) > 3 else 198; vr = step[4] if len(step) > 4 else 750; fs = step[5] if len(step) > 5 else 1
        assert 575 <= vr <= 825
        assert v >= FLOOR
        a = Attempt(v_dip_mv=v, t_dip_us=td, v_restore_mv=vr, t_restore_us=-20, t_revert_us=0, fast=fs, hold_ms=0,
                    pulses=np_, pw=pw, gap=150, delay=117, revert=0)
        try: rep = card.attempt(a)
        except IOError: print("step %d v=%d td=%d pw=%d vr=%d f=%d: nack" % (i, v, td, pw, vr, fs)); _ensure_unlocked(card); continue
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
                fc = bar.rd32(0x820000)
                if fc != 0xFFFFFFFF and (fc >> 16) == 0xBADF:
                    out = "poisoned FUSECTRL=0x%08x (no further BAR0 reads)" % fc
                elif fc != 0xFFFFFFFF:
                    c = classify(bar)
                    badf = [k for k, x in (("dsov", c.dsov), ("crc", c.crc_status), ("priv", c.priv_mask)) if isinstance(x, int) and (x >> 16) == 0xBADF]
                    if badf: out = "poisoned (%s read 0xBADF; no re-read)" % ",".join(badf)
                    elif c.priv_mask == 0 and c.crc_status == 0: out = "poisoned/over0"
                    elif c.dsov == 1: out = "under"
                    elif c.dsov == 0 and c.crc_status == 0x55 and c.priv_mask == 0xFFFFFFFF:
                        print("step %d v=%d td=%d pw=%d vr=%d f=%d: GOLDILOCKS" % (i, v, td, pw, vr, fs)); print(">>> GOLDILOCKS HELD (%s) v=%d td=%d" % (TAG, v, td)); tr_dump(); sys.exit(0)
                    else: out = ("dsov0" if c.dsov == 0 else "dsov=0x%x" % c.dsov) + " crc=0x%02x priv=0x%08x" % (c.crc_status, c.priv_mask)
                    if out == "under" and SENT:
                        sv = bar.rd32(SENT)
                        if (sv >> 16) == 0xBADF or (sv >> 20) == 0xBAD: out = "hidden-poison alias 0x%x=0x%08x" % (SENT, sv)
                    if out == "under":
                        t_g = time.monotonic(); gw = bar.rd32(0x118234)
                        while (gw & 0xFF) != 0xFF and (gw >> 16) != 0xBADF and time.monotonic() - t_g < 3.0: time.sleep(0.05); gw = bar.rd32(0x118234)
                        if (gw & 0xFF) != 0xFF: out = "gfw-not-done 0x%08x" % gw
                        elif time.monotonic() - t_g > 0.3: print("   GFW done after %.2f s" % (time.monotonic() - t_g), flush=True)
                    if out != "under":
                        try: print("   classify fields: %s" % ({k: (hex(x) if isinstance(x, int) else x) for k, x in vars(c).items()}), flush=True)
                        except Exception: print("   classify: %r" % (c,), flush=True)
                    if not badf and ((c.crc_status >> 20) == 0xbad or (c.priv_mask >> 20) == 0xbad):
                        time.sleep(2.0); c2 = classify(bar)
                        print("   half-boot (0xBAD.. reads); re-read after 2 s: dsov=%s crc=0x%x priv=0x%08x" % (c2.dsov, c2.crc_status, c2.priv_mask), flush=True)
                        if c2.dsov == 0 and c2.crc_status == 0x55 and c2.priv_mask == 0xFFFFFFFF:
                            print("step %d v=%d td=%d pw=%d vr=%d f=%d: GOLDILOCKS (after re-read)" % (i, v, td, pw, vr, fs)); print(">>> GOLDILOCKS HELD (%s) v=%d td=%d" % (TAG, v, td)); tr_dump(); sys.exit(0)
                        out = out + " | re-read dsov=%s crc=0x%x priv=0x%08x" % (c2.dsov, c2.crc_status, c2.priv_mask)
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
