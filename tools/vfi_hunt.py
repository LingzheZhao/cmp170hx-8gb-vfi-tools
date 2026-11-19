#!/usr/bin/env python3
"""Stop-and-hold hunter: fire shots at one setting and stop on the first clean goldilocks, leaving it held.

Clean goldilocks = all five checks of the tool's classifier pass: DSOV 0, CRC_STATUS 0x55, privilege mask
0xffffffff, FPF row readable, DSOV write-back works. A held state keeps the rail under PMBus control at stock with
no reset and no handback, so it persists for read-only checks (vfi_snap.py). Run vfi_park.py when done.

--stop-on hit also stops on DSOV 0 + CRC 0x55 with the privilege mask not fully open. Such states looked close on
one card here but were not burnable (a sense on one poisoned the controller); hold them only to study them.

--first-shot parks before every shot. The first shot after a park starts from the GPU's own rail control and dips
shallower than a back-to-back shot at the same setting, so the two modes behave like two different settings.

No hit within --budget: the card is parked and the exit code is 1.
"""
import argparse
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import vfi_common as vc  # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    vc.add_bench_args(ap)
    vc.add_shot_args(ap)
    vc.add_limit_args(ap)
    ap.add_argument("--budget", type=int, default=100, help="shots in this run")
    ap.add_argument("--first-shot", action="store_true", help="park before every shot")
    ap.add_argument("--stop-on", choices=("goldilocks", "hit"), default="goldilocks")
    ap.add_argument("--log", help="append one JSON line per shot to this file (0600)")
    a = ap.parse_args()
    att = vc.attempt_from(a)
    stats = dict.fromkeys(vc.OUTCOMES, 0)
    with vc.Bench(a) as b:
        print(f"setting: {att.command()}  first-shot={a.first_shot}  temp_raw={vc.temp_raw(b.card)}")
        b.park()
        for n in range(1, a.budget + 1):
            if a.first_shot and n > 1:
                b.park()
            s = b.shot(att)
            stats[s.outcome] += 1
            vc.append_jsonl(a.log, {"n": n, "t": vc.stamp(), "cmd": att.command(), "outcome": s.outcome,
                                    "dsov": s.dsov, "crc": s.crc, "priv": s.priv})
            if s.outcome in ("hit", "over", "goldilocks"):
                print(f"[{n}] {s.brief()}", flush=True)
            if s.outcome == "goldilocks" or (a.stop_on == "hit" and s.outcome == "hit"):
                b.hold(f"{s.outcome.upper()} at shot {n} ({vc.stamp()}): {s.brief()}  {stats}  {b.ledger}")
                return 0
            if n % 25 == 0:
                print(f"[{n}] {stats} temp_raw={vc.temp_raw(b.card)}", flush=True)
        print(f"no {a.stop_on} in {a.budget} shots: {stats}; {b.ledger}")
    return 1


if __name__ == "__main__":
    sys.exit(vc.main_guard(main))
