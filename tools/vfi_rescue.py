#!/usr/bin/env python3
"""Rescue a card whose link is gone after a crash: the park sequence, step by step, with diagnostics.

Symptom: no config space (0xffffffff) after a lost shot, and a sysfs remove / PEX_RST / rescan does not bring it
back. Cause: the rail is under the crashed GPU's PWM-VID control and sits at the ~0.3 V floor, so every reset
reboots the GPU into a brown-out. Fix: take the rail under PMBus control at stock (787.5 mV), pulse PEX_RST, wait for
config space while the rail is held, then hand the rail back.

This tool does NOT read config space before the rail is held. It prints the rail state first (I2C only).
"""
import argparse
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import vfi_common as vc  # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    vc.add_bench_args(ap)
    ap.add_argument("--expect", type=lambda s: int(s, 0), default=vc.DEVICE_8GB)
    ap.add_argument("--wait", type=float, default=6.0, help="seconds to poll config space while the rail is held")
    a = ap.parse_args()
    vc.check_card(a.bdf)
    with vc.open_card(a.port) as card:
        e1, mv = vc.rail(card)
        mode = {vc.E1_GPU: "GPU PWM-VID control", vc.E1_PMBUS: "PMBus control"}.get(e1, "unexpected")
        print(f"before: E1={e1:#06x} ({mode}), READ_VOUT {mv} mV, z: {card.cmd('z').strip()}")
        if e1 == vc.E1_GPU and mv < vc.HEALTHY_MV:
            print(f"the GPU is asking for {mv} mV: brown-out loop, as expected after a crash")
        r = vc.park(card, a.bdf, a.expect, settle_s=0.2, extra_s=a.wait, log=print)
        e1, mv = vc.rail(card)
        print(f"after: E1={e1:#06x}, READ_VOUT {mv} mV, z: {card.cmd('z').strip()}")
    print(r.line())
    if not r.ok:
        print("not rescued: run it again; if the link never returns with the rail held at stock, power the host "
              "fully off (not a reboot) and check NVVDD with a meter")
    return 0 if r.ok else 4


if __name__ == "__main__":
    sys.exit(vc.main_guard(main))
