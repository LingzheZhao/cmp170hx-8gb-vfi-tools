#!/usr/bin/env python3
"""Edge tracker: walk the train timing (t_dip) along a card's lethal edge in small steps, in short cells.

Each cell fires --cell shots at one t_dip (the card is parked before and after every cell; with --first-shot also
before every shot). Then:
  mostly under (>= --under N)  -> t_dip += step   (toward the edge)
  mostly lost  (>= --lost N)   -> t_dip -= step   (back to the safe side)
  mixed                        -> stay
With the default step +1 us the edge is assumed to lie at LATER timing (the train's first pulse running into the
PEX_RST release). If your card dies at earlier timing, use --step -1. Start on the safe side.

The run stops on a clean goldilocks (held, no park), when the lost shots of this run reach --crash-budget, after
--max-cells cells, or at the per-boot limits. --v-dip takes a list that rotates per cell, e.g. 356,356,362.
Timing is a continuous knob: depth moves in 6.25 mV codes, t_dip in 1 us steps.
"""
import argparse
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import vfi_common as vc  # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0],
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    vc.add_bench_args(ap)
    vc.add_shot_args(ap, with_dip=False)
    vc.add_limit_args(ap)
    ap.add_argument("--v-dips", required=True, help="dip voltage(s), mV, comma-separated, rotating per cell")
    ap.add_argument("--t-dip-start", type=int, required=True, help="first t_dip, us (on the safe side)")
    ap.add_argument("--step", type=int, default=1, help="t_dip change per decision, us (sign = direction)")
    ap.add_argument("--cell", type=int, default=10, help="shots per cell")
    ap.add_argument("--under", type=int, default=8, help="under count that moves toward the edge")
    ap.add_argument("--lost", type=int, default=8, help="lost count that moves back")
    ap.add_argument("--crash-budget", type=int, default=60, help="lost shots allowed in this run")
    ap.add_argument("--max-cells", type=int, default=45)
    ap.add_argument("--first-shot", action="store_true", help="park before every shot")
    ap.add_argument("--wait-temp-raw", help="LO:HI raw READ_TEMP1 window to wait for before the first cell")
    ap.add_argument("--log", help="append one JSON line per cell to this file (0600)")
    a = ap.parse_args()
    v_dips = [int(x) for x in a.v_dips.split(",") if x.strip()]
    if a.step == 0 or not v_dips:
        ap.error("need a nonzero --step and at least one --v-dips value")
    td = a.t_dip_start
    for v in v_dips:   # bounds check up front
        vc.attempt_from(a, v_dip=v, t_dip=td)
    lost_total = 0
    with vc.Bench(a) as b:
        if a.wait_temp_raw:
            lo, hi = (int(x) for x in a.wait_temp_raw.split(":"))
            print(f"waiting for READ_TEMP1 raw {lo}-{hi}")
            vc.wait_temp(b.card, lo, hi, 600)
        for n in range(1, a.max_cells + 1):
            if lost_total >= a.crash_budget:
                print(f"crash budget reached ({lost_total} lost)")
                break
            v = v_dips[(n - 1) % len(v_dips)]
            att = vc.attempt_from(a, v_dip=v, t_dip=td)
            b.park()
            c = dict.fromkeys(vc.OUTCOMES, 0)
            for k in range(a.cell):
                if a.first_shot and k > 0:
                    b.park()
                s = b.shot(att)
                c[s.outcome] += 1
                if s.outcome in ("hit", "over", "goldilocks"):
                    print(f"   REAL DSOV0: {s.brief()}", flush=True)
                if s.outcome == "goldilocks":
                    b.hold(f"GOLDILOCKS in cell {n} (v_dip {v}, t_dip {td}) shot {k + 1} at {vc.stamp()}")
                    return 0
            lost_total += c["lost"]
            t = vc.temp_raw(b.card)
            print(f"cell {n} {vc.stamp()} v_dip={v} t_dip={td}: " + " ".join(f"{k}={c[k]}" for k in vc.OUTCOMES
                  if c[k]) + f"  lost_run={lost_total}/{a.crash_budget} temp_raw={t}", flush=True)
            vc.append_jsonl(a.log, {"cell": n, "t": vc.stamp(), "v_dip": v, "t_dip": td, "counts": c,
                                    "temp_raw": t, "cmd": att.command()})
            if c["under"] >= a.under:
                td += a.step
            elif c["lost"] >= a.lost:
                td -= a.step
            vc.attempt_from(a, v_dip=v, t_dip=td)   # stop at the bounds
        print(f"no goldilocks: last t_dip {td}, {b.ledger}")
    return 1


if __name__ == "__main__":
    sys.exit(vc.main_guard(main))
