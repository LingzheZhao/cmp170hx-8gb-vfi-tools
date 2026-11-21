#!/usr/bin/env python3
"""push3 vs push2 host simulation: push2-legal shots unchanged; overlap shots run with the release at t0 by the alarm."""
import os, re, subprocess, sys
H = os.path.dirname(os.path.abspath(__file__)); fails = []
def run(which, cmd):
    evf = os.path.join(H, which, "ev.txt")
    e = {k: v for k, v in os.environ.items() if not k.startswith("SIM_")}
    out = subprocess.run([os.path.join(H, which, "sim"), evf, cmd], capture_output=True, text=True, check=True, env=e).stdout
    return out, open(evf).read().splitlines()
def t(l): return int(l.split()[0])
def check(name, cond, detail=""):
    print(("PASS " if cond else "FAIL ") + name + ("" if cond else "  " + str(detail)[:900]))
    if not cond: fails.append(name)
def writes(ev): return [l for l in ev if " W " in l]
def wbody(l): return " ".join(l.split()[3:])
def t0_of(ev, reset_at=-1000): return t([l for l in ev if l.endswith("W 25 reg=1f 20 00")][0]) - (reset_at - 300)
TL = ("reg=21", "reg=1f", "reg=e1")
def timeline(ev, t0): return [(t(l) - t0, wbody(l)) for l in writes(ev) if wbody(l).startswith(TL)]
def gpio22(ev, t0): return [(t(l) - t0, l.split()[-1]) for l in ev if " GPIO 22 " in l]
def rel(out): m = re.search(r"release_at=(-?\d+) scheduled=0 via=(\w+)", out); return (int(m.group(1)), m.group(2)) if m else None
LEGAL = ["g 354 -60 750 -20 0 1 0 3 198 150 117 0", "g 431 -60 750 -20 0 1 0 3 198 150 117 0",
         "g 356 -60 750 -20 0 1 0 3 80 150 117 0", "g 388 -60 825 -20 0 0 0 1 36 150 117 0",
         "g 354 -60 750 -20 0 1 2 3 198 150 117 0", "g 300 -100 650 -20 0 1 0 1 40 150 117 0",
         "g 369 -60 625 -20 0 1 0 3 198 150 117 0", "g 369 -60 750 -20 0 1 0 3 198 150 110 0",
         "g 381 -60 625 -20 151 1 0 1 131 150 70 0", "g 381 -60 625 -20 0 1 0 1 131 150 70 0",
         "g 381 -60 625 -20 151 1 0 1 83 150 70 0", "g 381 -60 625 -20 171 1 0 1 198 150 117 0",
         "g 381 -60 625 -20 171 1 0 1 130 150 117 0", "g 381 -60 625 -20 1000 1 0 2 75 150 70 2",
         "g 375 -60 750 -20 0 0 0 1 198 150 55 0", "g 325"]
for cmd in LEGAL:
    bo, be = run("base", cmd); no, ne = run("new", cmd)
    check("identical output, push2-legal: " + cmd, bo == no, (bo[-300:], no[-300:]))
    if "ABORT" not in bo:
        check("identical timeline + reset pin: " + cmd, timeline(be, t0_of(be)) == timeline(ne, t0_of(ne))
              and gpio22(be, t0_of(be)) == gpio22(ne, t0_of(ne)))
OVER = [("g 381 -60 625 -20 151 1 0 1 %d 150 70 0" % pw, -130, pw) for pw in (90, 100, 110, 120, 126, 130)] + \
       [("g 381 -60 625 -20 171 1 0 1 %d 150 117 0" % pw, -177, pw) for pw in (137, 147, 157, 167, 177)]
for cmd, s1, pw in OVER:
    bo, be = run("base", cmd); no, ne = run("new", cmd); nt0 = t0_of(ne)
    check("push2 refused: " + cmd, "glitch ABORT: PMBus write" in bo, bo[-200:])
    check("push3 runs, no ABORT, NOTE present: " + cmd, "ABORT" not in no and "glitch NOTE: push3" in no, no[-300:])
    check("push3 never prints the runner MISTIMED overlap marker: " + cmd, "overlaps the PEX_RST release" not in no)
    check("release exactly at t0 by the alarm: " + cmd, rel(no) == (0, "alarm") and (0, "IN") in gpio22(ne, nt0),
          (rel(no), gpio22(ne, nt0)))
    tl = [x for x in timeline(ne, nt0) if x[1].startswith("reg=21") and x[0] > -1000]   # skip the 600 mV setup write
    check("dip write at s1 and restore write pw later: " + cmd, len(tl) >= 2 and tl[1][0] - tl[0][0] == pw, tl[:4])
for cmd in ("g 381 -60 625 -20 1000 1 0 3 50 150 70 2", "g 381 0 625 -20 1000 1 0 1 150 150 30 0"):
    bo, be = run("base", cmd); no, ne = run("new", cmd)
    check("other overlap geometry runs with release at t0: " + cmd, "glitch ABORT" in bo and rel(no) == (0, "alarm"), (bo[-150:], rel(no)))
for cmd in ("g 381 -2000 625 -20 0 1 0 1 100 150 0 0",):
    bo, be = run("base", cmd); no, ne = run("new", cmd)
    check("setup-overwrite refusal kept in push3: " + cmd, "overwritten by the setup write" in no and bo == no, no[-200:])
cmd = OVER[2][0]; no, ne = run("new", cmd); nt0 = t0_of(ne)
print("\nexample push3 overlap shot (%s):" % cmd)
for x in timeline(ne, nt0): print("   write at %+5d us  %s" % x)
print("   " + "\n   ".join(l for l in no.splitlines() if l.startswith(("glitch NOTE", "release_at", "timing i=4", "timing i=5"))))
print("\n%d failures" % len(fails)); sys.exit(1 if fails else 0)
