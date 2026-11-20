#!/usr/bin/env python3
"""Park the card safely after a campaign, or between shots (bisect_shots.py runs this after a lost shot).
A crashed GA100 under PWM-VID control browns itself out (NVVDD ~0.3 V) and can sit half-booted answering PCIe
config reads with CRS; an Intel root complex retries those forever and the host freezes (seen three times on the
lab's bench). So: NO config read first. Hold NVVDD at stock 787.5 mV under PMBus, pulse PEX_RST so the GPU reboots
healthy, wait, only then read config space, then hand the rail back to the GPU and check again.

Usage (root): VFI_BDF=0000:05:00.0 [VFI_PORT=/dev/ttyACM0] python3 -I park.py   (or: park.py <bdf> [port])
Exit codes: 0 parked (config reads 0x20c210de after the handback), 3 rail not at stock under PMBus control (handed
back, config not read), 4 the link did not come back: either config space did not read the card while the rail was
held (the rail stays held under PMBus control and the card is not handed back), or the rail collapsed after the
handback (PMBus control is taken back and config space is not read again). tools/vfi_park.py is a fuller version."""
import os
import sys
import time
from ga100vfi.glitch import Card
BDF = (sys.argv[1] if len(sys.argv) > 1 else os.environ.get("VFI_BDF")) or sys.exit("set VFI_BDF (or pass the BDF)")
PORT = sys.argv[2] if len(sys.argv) > 2 else os.environ.get("VFI_PORT", "/dev/ttyACM0")
P = "/sys/bus/pci/devices/%s/config" % BDF
def cfg():
    with open(P, "rb") as f: return int.from_bytes(f.read(4), "little")
with Card(PORT) as c:
    c.cmd("u")
    c.cmd("@w 25 21 7e 00")
    c.cmd("@w 25 e1 fa c2")
    time.sleep(0.3)
    d = c.read(0x25, 0x8B, 2); v = d[0] | d[1] << 8
    if not 700 <= v <= 830:
        print("park: rail %d mV under PMBus control is not stock; handing back, NOT reading config" % v); c.cmd("w"); raise SystemExit(3)
    c.cmd("r")
    time.sleep(1.5)
    a = cfg()
    if a != 0x20c210de:
        print("park: config %#x while rail is held; keeping PMBus control and stopping" % a)
        raise SystemExit(4)
    c.cmd("w"); time.sleep(1.0)
    d = c.read(0x25, 0x8B, 2); after_mv = d[0] | d[1] << 8
    if after_mv < 500:
        c.cmd("@w 25 21 7e 00"); c.cmd("@w 25 e1 fa c2")
        print("park: rail collapsed to %d mV after handback; PMBus control requested, no config read" % after_mv)
        raise SystemExit(4)
    b = cfg()
    print("park: rail held %d mV, PEX_RST, config %#x; after handback config %#x, GPU rail %d mV" % (v, a, b, after_mv))
    raise SystemExit(0 if b == 0x20c210de else 4)
