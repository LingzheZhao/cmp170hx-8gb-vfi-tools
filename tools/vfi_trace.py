#!/usr/bin/env python3
"""Capture the NVVDD rail during shots (debug firmware build, ADC on the NVVDD sense pin) with each shot's outcome.

Cells are given as v_dip:t_dip[:pw[:pulses[:fast]]], for example 338:-60 331:-60:198:3:1 331:0.
Two modes:
  back-to-back (default)  the shots of a cell follow each other, the card is parked at the end of each cell;
  first-shot              the card is parked before every shot (each shot starts from the GPU's own rail control,
                          as the first shot of a campaign does).
Each shot records the trace in mV (12-bit ADC at 3.3 V), its trough (9-sample running median, which drops the
single-sample ADC speckle) and the outcome. A clean goldilocks stops the run and is held (no park).

The Pico ADC read about 25 mV below a meter on the lab's card; treat absolute values as approximate.
"""
import argparse
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import vfi_common as vc  # noqa: E402


def cell_type(s: str) -> dict:
    parts = s.split(":")
    if not 2 <= len(parts) <= 5:
        raise argparse.ArgumentTypeError(f"cell {s!r}: want v_dip:t_dip[:pw[:pulses[:fast]]]")
    keys = ("v_dip", "t_dip", "pw", "pulses", "fast")
    try:
        return {k: int(v) for k, v in zip(keys, parts)}
    except ValueError:
        raise argparse.ArgumentTypeError(f"cell {s!r}: numbers only")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0],
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    vc.add_bench_args(ap)
    vc.add_shot_args(ap, with_dip=False)
    vc.add_limit_args(ap)
    ap.add_argument("cells", nargs="+", type=cell_type)
    ap.add_argument("--shots", type=int, default=5, help="shots per cell")
    ap.add_argument("--mode", choices=("back-to-back", "first-shot"), default="back-to-back")
    ap.add_argument("--out", required=True, help="JSON file for the traces (0600)")
    a = ap.parse_args()
    atts = [vc.attempt_from(a, **c) for c in a.cells]   # check every cell against the bounds first
    a.fast = max([a.fast] + [c.get("fast", 0) for c in a.cells])   # firmware check covers every cell
    out = {"mode": a.mode, "cells": []}
    with vc.Bench(a, need_trace=True) as b:
        try:
            for cell, att in zip(a.cells, atts):
                b.park()
                tally, troughs = [], []
                for k in range(a.shots):
                    if a.mode == "first-shot" and k > 0:
                        b.park()
                    s = b.shot(att)
                    tro = vc.trough_mv(s.trace_mv)
                    tally.append(vc.SHORT[s.outcome])
                    if tro is not None:
                        troughs.append(tro)
                    out["cells"].append({**cell, "cmd": att.command(), "shot": k, "outcome": s.outcome,
                                         "dsov": s.dsov, "crc": s.crc, "priv": s.priv, "trough_mv": tro,
                                         "samples": len(s.trace_mv), "trace_mv": s.trace_mv})
                    if s.outcome == "goldilocks":
                        b.hold(f"GOLDILOCKS in cell {cell} shot {k}: {s.brief()}")
                        return 0
                print(f"{cell}: {' '.join(tally)}  troughs {sorted(troughs)}  "
                      "(u under, L lost, P poisoned, O over, H hit, n nack)", flush=True)
        finally:
            vc.write_private(a.out, out)
            print("saved", a.out)
    return 1


if __name__ == "__main__":
    sys.exit(vc.main_guard(main))
