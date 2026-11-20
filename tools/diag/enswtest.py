#!/usr/bin/env python3
"""Does EN_SW_OVERRIDE (0x820040) survive PEX_RST? The stock classify() wrote 1 there after every shot. Gentle 700 mV
shots only (the GPU idles near 695 mV); stops at the first 0xBADF read.

This test WRITES 0x820040 = 1 (the same write the stock classifier makes after every shot); nothing else is written.
Run as root on the glitch boot, card parked and idle:
  VFI_BDF=0000:05:00.0 [VFI_PORT=/dev/ttyACM0] python3 enswtest.py
Then power-cycle: the register keeps its value across PEX_RST."""
import os, sys, subprocess, tempfile
from ga100vfi.glitch import Card
from ga100vfi.bar0 import restore, Bar0
from ga100vfi.cli import _ensure_unlocked
HERE = os.path.dirname(os.path.abspath(__file__))
BDF = os.environ.get("VFI_BDF") or sys.exit("set VFI_BDF to the card's PCI address")
PORT = os.environ.get("VFI_PORT", "/dev/ttyACM0")
G = "g 700 -60 750 -20 0 1 0 3 198 150 117 0"
def rd(tag):
    with Bar0(BDF) as b:
        fc = b.rd32(0x820000); out = {"FUSECTRL": hex(fc)}
        if (fc >> 16) == 0xBADF: print(tag, out, "STOP (poisoned)", flush=True); sys.exit(3)
        for n, a in (("EN_SW", 0x820040), ("DSOV", 0x820084), ("CRC", 0x820070), ("PLM", 0x8200fc), ("alias410", 0x820410)):
            v = b.rd32(a); out[n] = hex(v)
            if (v >> 16) == 0xBADF: print(tag, out, "STOP (BADF)", flush=True); sys.exit(3)
    print(tag, out, flush=True); return out
def shot(card, tag):
    r = card.cmd(G, wait=0.8, total=20.0, until="glitch done")
    assert "glitch done" in r, r
    assert restore(BDF, wait_s=3.0), "link lost"
    return rd(tag)
card = Card(PORT); assert _ensure_unlocked(card)
rd("0 fresh boot")
shot(card, "1 after shot (no EN_SW write)")
with Bar0(BDF) as b: b.wr32(0x820040, 1)
rd("2 after EN_SW=1 write")
s = shot(card, "3 after shot following EN_SW=1")
shot(card, "4 after second shot")
if s["EN_SW"] != "0x1":
    with Bar0(BDF) as b: b.wr32(0x820040, 1)
    rd("5 EN_SW=1 again, no reset")
print("6 running vfi_snap.py (read-only) with EN_SW=1", flush=True)
with tempfile.TemporaryDirectory() as d:
    p = subprocess.run([sys.executable, "-E", "-s", os.path.join(HERE, "..", "vfi_snap.py"), "--bdf", BDF,
                        "--label", "enswtest", "--outdir", d], capture_output=True, text=True, cwd=d)
print("\n".join(l for l in p.stdout.splitlines() if any(k in l for k in ("EN_SW", "GPC dis", "WARN", "verdict"))))
rd("7 after vfi_snap")
print("w:", card.cmd("w", wait=0.8, total=3.0).strip())
