#!/usr/bin/env python3
"""Gentle timing controls (700 mV, the GPU idles near 695 mV): fire shots whose PMBus writes do or do not overlap the
PEX_RST release, print the firmware's timeline records for each, and stop at the first state that is not healthy.

Needs the timing build of the firmware (firmware/0004, fw=lab-psr1-timing1) and passive_snapshot.py from the same
series. Run as root on the glitch boot, card parked and idle, NVVDD lead off:
  VFI_BDF=0000:05:00.0 VFI_HOST_DIR=<pico-ga100vfi clone>/host [VFI_PORT=/dev/ttyACM0] python3 -I timing_controls.py
Exit codes: 0 done, 3 a state after a shot was not healthy (stop and look), 1 setup error."""
import os, sys, json
if os.environ.get("VFI_HOST_DIR"): sys.path.insert(0, os.environ["VFI_HOST_DIR"])
from ga100vfi.glitch import Card
from ga100vfi.bar0 import restore
from ga100vfi.cli import _ensure_unlocked
try:
    from passive_snapshot import snapshot
except ImportError:
    sys.exit("passive_snapshot.py not found: set VFI_HOST_DIR=<pico-ga100vfi clone with firmware/0004 applied>/host")
BDF = os.environ.get("VFI_BDF") or sys.exit("set VFI_BDF to the card's PCI address")
PORT = os.environ.get("VFI_PORT", "/dev/ttyACM0")
SHOTS = [("single pw36 td%+d" % td, "g 700 %d 750 -20 0 1 0 1 36 150 117 0" % td) for td in (60, 80, 81, 82, 100)]
SHOTS += [("std train td-60", "g 700 -60 750 -20 0 1 0 3 198 150 117 0"),
          ("slow single td+30 vr825", "g 700 30 825 -20 0 0 0 1 36 150 117 0"),
          ("std train td-85", "g 700 -85 750 -20 0 1 0 3 198 150 117 0")]
card = Card(PORT)
assert _ensure_unlocked(card), "unlock failed"
print("pico:", card.status().strip(), flush=True)
print("pre", json.dumps(snapshot(BDF)["registers"]), flush=True)
for name, cmd in SHOTS:
    r = card.cmd(cmd, wait=0.8, total=20.0, until="glitch done")
    lines = [l for l in r.splitlines() if l.startswith(("glitch", "timing"))]
    print("==", name, "|", cmd, flush=True)
    for l in lines:
        if l.startswith("glitch") or " act=4 " in l or " act=1 " in l or " act=3 " in l: print("  ", l)
    ok = restore(BDF, wait_s=3.0)
    st = snapshot(BDF) if ok else {"state": "lost"}
    z = card.cmd("z", wait=0.4, total=3.0)
    print("   state:", st.get("state"), st.get("reason"), "| z:", z.strip(), flush=True)
    if st.get("state") != "healthy_locked":
        print("STOP: not healthy after", name); sys.exit(3)
print("w:", card.cmd("w", wait=0.8, total=3.0).strip())
print("done")
