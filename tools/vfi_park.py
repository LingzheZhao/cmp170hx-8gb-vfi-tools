#!/usr/bin/env python3
"""Park the card: hold NVVDD at stock under PMBus, pulse PEX_RST, wait, read config space, hand the rail back.

Run it at the end of every campaign cell, after a crashed ("lost") shot, and before any other config or BAR0
access. A crashed GA100 under its own PWM-VID control asks for the ~0.3 V floor and every PEX_RST reboots it into a
brown-out; a half-booted card can also answer config reads with CRS, which an Intel root complex retries forever
(host freeze). Park never reads config space before the rail is held at stock and READ_VOUT confirms it.

Exit 0 = parked and handed back healthy. Nonzero = read the message; a failed park leaves the rail held at stock
when that is the safe state, and running park again is the next step.
"""
import argparse
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import vfi_common as vc  # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    vc.add_bench_args(ap)
    ap.add_argument("--expect", type=lambda s: int(s, 0), default=vc.DEVICE_8GB,
                    help="config dword 0 of a healthy card (default 0x20c210de)")
    ap.add_argument("--settle", type=float, default=1.5, help="seconds after PEX_RST before the config read")
    ap.add_argument("--extra-wait", type=float, default=3.0, help="more seconds to poll a slow link while held")
    ap.add_argument("-v", "--verbose", action="store_true")
    a = ap.parse_args()
    vc.check_card(a.bdf)
    with vc.open_card(a.port) as card:
        r = vc.park(card, a.bdf, a.expect, a.settle, a.extra_wait, log=print if a.verbose else None)
    print(r.line())
    return 0 if r.ok else 4


if __name__ == "__main__":
    sys.exit(vc.main_guard(main))
