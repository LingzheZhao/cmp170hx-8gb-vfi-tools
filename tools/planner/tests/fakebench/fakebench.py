#!/usr/bin/env python3
"""Fake bench for hunt.py tests: stands in for tools/runner/firstshot.sh and bootrun.sh.

No ssh, no network, no hardware. It prints the lines the real launchers and bisect_shots.py (push2) print, from a
card model in $FAKE_MODEL (JSON):
  {"edges": [{"match": {"fast": 0}, "lost_at_or_below": 52}], "default_lost_at_or_below": 53,
   "signals": [{"v": 331, "delay": 300, "outcome": "goldilocks" | "crc75" | "bad0"}],
   "events": {"3": "no_dip" | "controller_off" | "temp_gate" | "park_fail" | "mistimed" | "traceback" | "identity"},
   "fw": "lab-psr1-push2-debug"}
Boots are counted in $FAKE_STATE (JSON), which also lists every call (tests check a dry run made none).
"""
import datetime as dt
import json
import os
import sys
import uuid

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, ROOT)
from planner_auto import schedule, steps as S  # noqa: E402
from planner_auto.vid import mv_to_code  # noqa: E402

PARK_OK = "park: rail held 760 mV, PEX_RST, config 0x20c210de; after handback config 0x20c210de, GPU rail 695 mV"
PARK_BAD = "park: config 0xffffffff while rail is held; keeping PMBus control and stopping"
REST = "Rest of this boot is unreliable (sticky poison): power cycle, next boot starts just before this step"


def load(p, default):
    try:
        with open(p) as f:
            return json.load(f)
    except (OSError, ValueError):
        return default


def outcome(model, st):
    for s in model.get("signals", []):
        if all(st.get(k) == v for k, v in s.items() if k != "outcome"):
            return s["outcome"]
    code = mv_to_code(st["v"])
    for e in model.get("edges", []):
        if all(st.get(k) == v for k, v in e["match"].items()):
            return "lost" if code <= e["lost_at_or_below"] else "under"
    return "lost" if code <= model.get("default_lost_at_or_below", 53) else "under"


def bootrun(card, seq, model, state, out):
    state["boots"] = state.get("boots", 0) + 1
    n = state["boots"]
    ev = model.get("events", {}).get(str(n))
    now = dt.datetime.now().strftime("%H:%M:%S")
    bench = (dt.datetime(2026, 11, 20, 12, 0, 0) + dt.timedelta(seconds=150 * n)).strftime("%H:%M:%S")
    fw = model.get("fw", "lab-psr1-push2-debug")
    park_each = os.environ.get("PARK_EACH") == "1"
    p = out.append
    if os.environ.get("NOCYCLE", "0") != "1":
        p("cycle (wol): clean shutdown %s" % now)
        p("bench off the network; waiting 60s for soft-off")
        p("power on (Wake-on-LAN) %s" % now)
    p("bench up 16s boot_id %s" % uuid.uuid4())
    if ev == "identity":
        p("PLAN SHA MATCH (card %s)" % card)
        p("IDENTITY MISMATCH: stop")
        return 5
    p("PLAN SHA MATCH (card %s)" % card)
    p("pico v: clk=400 MHz  nvvdd=1.0 V  sample_ns=240.0  last reset: power-on / external reset  fw=%s" % fw)
    p("PREFLIGHT OK")
    p("IDENTITY OK: card %s" % card)
    if ev == "temp_gate":
        p("TEMP GATE FAILED: temp_raw 350 did not reach TMIN 355 within TCAP 300s: no shot")
        p("RESULT: TEMP GATE FAILED: temp_raw 350 did not reach TMIN 355 within TCAP 300s: no shot (runner rc 16)")
        return 16
    p("START temp_raw=362 %s" % bench)
    repeat = int(os.environ.get("REPEAT", "1"))
    p("run env: REPEAT=%d SHOTCAP=450 LOSTRUN=%s RESTORE_WAIT=8.0 CELLLOST=1 CLASSIFY_WRITE=0 SENTINEL_EXPECT=0x1 timeout 900s"
      % (repeat, os.environ.get("LOSTRUN", "8")))
    p("pico: clk=400 MHz  nvvdd=1.0 V  sample_ns=240.0  last reset: power-on / external reset  fw=%s" % fw)
    p("unlock: unlock CML 0x08 MATCH | unlock MATCH wp=00 cfg=1229")
    p("boot pmbus (@r 25 <reg> <n>; words shown high byte first): 13=00 46=1229 7e=08 79=2c00 88=ed83 8b=02b5 8d=016a")
    p("sentinel baseline 0x820410=0x00000001 (expected 0x1)")
    steps = [S.from_list(s, repeat_default=repeat) for s in seq]
    cont = repeat > 1 or any(len(s) > 6 and s[6] > 1 for s in seq)
    seqx = [(i, st) for i, st in enumerate(steps) for _ in range(st["reps"] or 1)]
    cells = {}
    rc = 0
    for k, (i, st) in enumerate(seqx):
        lab = "v=%d td=%d pw=%d vr=%d f=%d" % (st["v"], st["td"], st["pw"], st["vr"], st["fast"])
        if park_each and k > 0 and ev == "park_fail":
            p("PARK FAILED before step %d: %s; stop" % (i, PARK_BAD))
            return finish(out, 7)
        b = schedule.build_for_step(st)
        r = b["resolved"]
        p("   glitch vd=%d td=%d vr=%d tr=%d tv=0 fast=%d hold=%d pw=%d gap=%d delay=%d revert=%d reset_at=%d dither=%d hold_at=%s"
          % (st["v"], st["td"], st["vr"], -20 if st["t_restore"] <= -1000 else st["t_restore"], st["fast"], st["hold"],
             r["pw"], r["gap"], r["delay"], r["revert"], r["reset_at"], r["dither"], r["hold_at"]))
        for j, (t, act, arg) in enumerate(b["steps"]):
            p("   timing i=%d scheduled=%d act=%d arg=%d start=%d end=%d ok=1" % (j, t, act, arg, t, t + 47))
        p("   prime_at=0")
        if ev == "no_dip":
            p("   release_at=0 scheduled=0 via=alarm")
            p("   glitch done pwd=0 e1=0xc2ea nvvdd=1.0 writes_failed=0")
            p("step %d %s: NO DIP (controller pwd=0 e1=0xc2ea): the shot did not run; stop" % (i, lab))
            return finish(out, 12)
        p("   release_at=%d scheduled=0 via=alarm" % (9 if ev == "mistimed" else 0))
        p("   glitch done pwd=1 e1=0xc2fa nvvdd=1.0 writes_failed=0")
        if ev == "controller_off":
            p("step %d %s: CONTROLLER OFF (0x2042, 41 mV)" % (i, lab))
            p("STOP: power cycle needed")
            return finish(out, 9)
        if ev == "traceback":
            p("Traceback (most recent call last):")
            p("ValueError: fake failure")
            return finish(out, 1)
        o = outcome(model, st)
        if ev == "mistimed":
            obs = "MISTIMED (release_at=9 us; observed under)" if o == "under" else o
            p("   observed, not scored: FUSECTRL=0xe0040000 GFW=0x000000ff DSOV=0x00000001 CRC=0x00000055 PLM=0xffffffff state=healthy_locked reason=None")
            p("step %d shot %d %s: %s" % (i, k, lab, obs))
            p("MISTIMED at step %d shot %d (%s): release_at=9 us; outcome %s. Not scored: power cycle (%d shots this boot)" % (i, k, lab, obs, k + 1))
            p("   park: rc 0 " + PARK_OK)
            return finish(out, 14)
        if o == "goldilocks":
            p("step %d %s: GOLDILOCKS passive candidate" % (i, lab))
            p(">>> GOLDILOCKS HELD (card %s) v=%d td=%d (passive candidate)" % (card, st["v"], st["td"]))
            return finish(out, 0, hold=True)
        c = cells.setdefault(i, {"under": 0, "lost": 0, "partial": 0, "other": 0})
        if o == "under":
            c["under"] += 1
            if not cont:
                p("step %d %s: under" % (i, lab))
        elif o == "lost":
            c["lost"] += 1
            if cont:
                if ev == "park_fail":
                    p("   park after lost shot FAILED (rc 4): " + PARK_BAD)
                    p("TIPPED at step %d shot %d (%s): lost and the park failed. Power cycle (%d shots this boot)" % (i, k, lab, k + 1))
                    return finish(out, 10)
                p("   park after lost shot: " + PARK_OK)
                p("step %d shot %d %s: lost" % (i, k, lab))
                p("cell %d v=%d td=%d np=%d pw=%d vr=%d: %s" % (i, st["v"], st["td"], st["pulses"], st["pw"], st["vr"], c))
                p("TIPPED at step %d shot %d (%s): 1 lost in a row. Power cycle (%d shots this boot)" % (i, k, lab, k + 1))
                return finish(out, 10)
            p("step %d %s: lost" % (i, lab))
            p("TIPPED at step %d (%s): lost. %s" % (i, lab, REST))
            p("   park: rc 4 " + PARK_BAD)
            return finish(out, 10)
        else:
            c["other"] += 1
            if o == "crc75":
                p("   observed: FUSECTRL=0xe0040000 GFW=0x000000ff DSOV=0x00000001 CRC=0x00000075 PLM=0xffffffff SENTINEL=0x00000001 state=partial_or_corrupt reason=x")
                txt = "dsov1 crc=0x75 priv=0xffffffff"
            else:
                p("   observed: FUSECTRL=0xbad00100 GFW=- DSOV=- CRC=- PLM=- state=poisoned reason=FUSECTRL: BADF sentinel")
                txt = "poisoned FUSECTRL=0xbad00100 (no further BAR0 reads)"
            if cont:
                p("step %d shot %d %s: %s" % (i, k, lab, txt))
                p("cell %d v=%d td=%d np=%d pw=%d vr=%d: %s" % (i, st["v"], st["td"], st["pulses"], st["pw"], st["vr"], c))
                p("TIPPED at step %d shot %d (%s): %s. Power cycle (%d shots this boot)" % (i, k, lab, txt, k + 1))
            else:
                p("step %d %s: %s" % (i, lab, txt))
                p("TIPPED at step %d (%s): %s. %s" % (i, lab, txt, REST))
            p("   park: rc 0 " + PARK_OK)
            return finish(out, 10)
        if cont and (k + 1 == len(seqx) or seqx[k + 1][0] != i):
            p("cell %d v=%d td=%d np=%d pw=%d vr=%d: %s" % (i, st["v"], st["td"], st["pulses"], st["pw"], st["vr"], c))
    p("   park: rc 4 " + PARK_BAD if ev == "park_fail" else "   park: rc 0 " + PARK_OK)
    p("sequence exhausted, no hold (%d shots)" % len(seqx) if cont else "sequence exhausted, all under")
    return finish(out, 0)


def finish(out, rc, hold=False):
    out.append("end temp_raw=366 runner_rc=%d" % rc)
    if hold:
        out.append("HOLD SEEN %s: fake card is held in the goldilocks state (rail under PMBus control, not parked)"
                   % dt.datetime.now().strftime("%H:%M:%S"))
        out.append("No ON_HOLD hook set: stopping here.")
        return 0
    first = next((l for l in out if l.startswith(("TIPPED", "MISTIMED at", "sequence exhausted", "STOP", "CONTROLLER OFF"))
                  or "FAILED" in l or "NO DIP" in l), "see log")
    out.append("RESULT: %s (runner rc %d)" % (first, rc))
    return rc


def main():
    mode, card, js = sys.argv[1], sys.argv[2], sys.argv[3]
    model = load(os.environ["FAKE_MODEL"], {})
    sp = os.environ["FAKE_STATE"]
    state = load(sp, {})
    state.setdefault("calls", []).append({"mode": mode, "steps": json.loads(js),
                                          "park_each": os.environ.get("PARK_EACH") == "1"})
    rc = 0
    try:
        if mode == "bootrun":
            out = []
            rc = bootrun(card, json.loads(js), model, state, out)
            text = "\n".join(out) + "\n"
            sys.stdout.write(text)
            if os.environ.get("BOOTRUN_LOG"):
                with open(os.environ["BOOTRUN_LOG"], "w") as f:
                    f.write(text)
        else:
            logdir = os.environ.get("LOGDIR") or "."
            os.makedirs(logdir, exist_ok=True)
            print("full logs: %s" % logdir)
            steps = json.loads(js)
            for i, s in enumerate(steps):
                s = list(s) + [3, 198, 750, 1][max(0, len(s) - 2):]
                s = s[:max(7, len(s))]
                if len(s) < 7:
                    s.append(1)
                s[6] = 1
                print("######## first-shot boot %d/%d: %s %s" % (i + 1, len(steps), json.dumps([s]), dt.datetime.now().strftime("%H:%M:%S")))
                out = []
                os.environ["NOCYCLE"] = "0"
                brc = bootrun(card, [s], model, state, out)
                with open(os.path.join(logdir, "boot-%d.log" % (i + 1)), "w") as f:
                    f.write("\n".join(out) + "\n")
                for l in out:
                    if not l.startswith("   timing"):
                        print(l)
                blob = "\n".join(out)
                if "HOLD SEEN" in blob:
                    print("FIRSTSHOT: goldilocks held, see the hold report (bootrun rc %d)" % brc)
                    rc = brc
                    break
                if "PARTIAL HELD" in blob:
                    rc = 13
                    break
                if any(x in blob for x in ("NO DIP", "IDENTITY MISMATCH", "CONTROLLER OFF", "park after lost shot FAILED",
                                           "PARK FAILED", "MISTIMED", "Traceback", "STOP:", "TEMP GATE FAILED")):
                    print("FIRSTSHOT: stop for review (bootrun rc %d)" % brc)
                    rc = 2
                    break
                if brc not in (0, 10):
                    print("FIRSTSHOT: bootrun exit %d: stop for review" % brc)
                    rc = 2
                    break
            else:
                print("FIRSTSHOT: done")
    finally:
        with open(sp, "w") as f:
            json.dump(state, f)
    sys.stdout.flush()
    return rc


if __name__ == "__main__":
    sys.exit(main())
