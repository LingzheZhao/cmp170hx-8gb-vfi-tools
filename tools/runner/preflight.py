#!/usr/bin/env python3
"""Read-only Pico + MP2888B preflight (serial commands v, z, @r only). Usage: preflight.py [port] (default
VFI_PORT, then /dev/ttyACM0). bootrun.sh runs it before every run. tools/vfi_preflight.py is a fuller version
(host, driver, config space and FUSECTRL checks too).
Requires the lab firmware (fw=lab-psr1): it captures each card's own six VRM profile registers before the
fast profile and writes those back after every shot (the stock firmware writes its author's card values).
Records this card's values. Fails on: wrong firmware, VRM left under PMBus control, any profile register
holding its fast value (an interrupted shot: power the card fully off), or read errors."""
import os
import sys
from ga100vfi.glitch import Card

AUTHOR = {0xBA: 0x0019, 0xB9: 0x0032, 0x30: 0xCE64, 0x31: 0x0072, 0x32: 0x7F81, 0x45: 0x7018}
FAST = {0xBA: 0x0001, 0xB9: 0x0001, 0x30: 0xCE04, 0x31: 0x0002, 0x32: 0x0F81, 0x45: 0x7218}
port = sys.argv[1] if len(sys.argv) > 1 else os.environ.get("VFI_PORT", "/dev/ttyACM0")
bad = 0
with Card(port) as c:
    st = c.status()
    print("pico v:", st.replace("\n", " | ")[:300])
    if "fw=lab-psr1" not in st:
        print("FAIL: the Pico is not running the lab firmware (fw=lab-psr1). The stock firmware would write its author's VRM profile onto this card."); bad += 1
    pwd, e1 = c.lock_status()
    print(f"pico z: pwd_unlocked={pwd} e1={e1:#06x} ({'GPU PWM-VID control (stock)' if e1 == 0xC2EA else 'PMBus control!' if e1 == 0xC2FA else 'unexpected'})")
    if e1 != 0xC2EA:
        print("FAIL: VRM is not under GPU PWM-VID control: hand it back (firmware command 'w', or park.py) and review"); bad += 1
    vals = {}
    for reg in AUTHOR:
        try:
            d = c.read(0x25, reg, 2); v = d[0] | d[1] << 8
        except Exception as ex:
            print(f"  MP2888B reg {reg:#04x}: read failed ({ex})"); bad += 1; continue
        vals[reg] = v
        if v == FAST[reg]:
            note = "HOLDS ITS FAST VALUE: interrupted shot? power the card fully off before glitching"; bad += 1
        elif v == AUTHOR[reg]:
            note = "same as the firmware author's card"
        else:
            note = f"differs from the author's card ({AUTHOR[reg]:#06x}); fine, the lab firmware restores this card's own value"
        print(f"  MP2888B reg {reg:#04x} = {v:#06x}  {note}")
    print("card profile:", " ".join(f"{r:02x}={v:04x}" for r, v in vals.items()))
    for name, reg in (("VOUT_MIN 0x1F", 0x1F), ("VOUT_COMMAND 0x21", 0x21), ("READ_VOUT 0x8B", 0x8B), ("READ_TEMP1 0x8D", 0x8D), ("STATUS_WORD 0x79", 0x79)):
        try:
            d = c.read(0x25, reg, 2); v = d[0] | d[1] << 8
            extra = ""
            if reg in (0x21, 0x1F): extra = f" (~{v * 6.25:.1f} mV at 6.25 mV/LSB)"
            if reg == 0x8D: extra = f" (raw {v}: {v * 0.125:.2f} C if 0.125 C/LSB, {v * 0.1:.1f} C if 0.1 C/LSB; format unconfirmed, watch the trend)"
            print(f"  {name} = {v:#06x}{extra}")
        except Exception as ex:
            print(f"  {name}: read failed ({ex})")
print("PREFLIGHT", "OK" if bad == 0 else f"FAILED ({bad} problem(s)): do not glitch until reviewed")
sys.exit(0 if bad == 0 else 1)
