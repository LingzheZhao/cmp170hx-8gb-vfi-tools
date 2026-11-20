#!/usr/bin/env python3
"""ledger_tools.py: maintenance on the append-only ledger (nothing is rewritten; withdrawals are VOID records).

  python3 ledger_tools.py dedupe --card e             # list shots ingested twice (filtered + full log of one boot)
  python3 ledger_tools.py dedupe --card e --apply     # append VOID records for the poorer copy of each pair
  python3 ledger_tools.py void --card e ATTEMPT_ID --reason "why"
"""
import argparse
import json
import os
import re
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
from planner_auto import launch, ledger  # noqa: E402

RE_CYC = re.compile(r"^cycle \(\w+\): clean shutdown (\d\d:\d\d:\d\d)")


def launch_time(r, cache={}):
    """launch_time of older records that lack it: the L time inside armed_key/attempt_id, else the 'cycle (' line
    of the full per-boot log the record came from."""
    if r.get("launch_time"):
        return r["launch_time"]
    for k in ("armed_key", "attempt_id"):
        m = re.search(r"@(\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d)L#", str(r.get(k) or ""))
        if m:
            return m.group(1)
    src = r.get("source_file")
    if src and os.path.exists(src):
        if src not in cache:
            with open(src, errors="replace") as f:
                cache[src] = [m.group(1) for m in (RE_CYC.match(l) for l in f) if m]
        times = cache[src]
        date = str(r.get("attempt_id", "")).split("@")[1][:10] if "@" in str(r.get("attempt_id", "")) else None
        if len(times) == 1 and date:
            return "%sT%s" % (date, times[0])
    return None


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    d = sub.add_parser("dedupe")
    d.add_argument("--card", required=True)
    d.add_argument("--ledger")
    d.add_argument("--apply", action="store_true")
    v = sub.add_parser("void")
    v.add_argument("--card", required=True)
    v.add_argument("--ledger")
    v.add_argument("attempt_id")
    v.add_argument("--reason", required=True)
    a = ap.parse_args(argv)
    cfg = launch.load_card(a.card) if os.path.exists(launch.card_path(a.card)) else {"card": a.card}
    path = launch.ledger_path(cfg, str(cfg["card"]), a.ledger)
    if a.cmd == "void":
        ledger.void(path, a.attempt_id, a.reason)
        print("VOID %s appended" % a.attempt_id)
        return 0
    recs = ledger.read(path)
    voided = {r["attempt_id"] for r in recs if r.get("phase") == "VOID"}
    done = [dict(r, launch_time=launch_time(r)) for r in recs
            if r.get("phase", "COMPLETE") == "COMPLETE" and r.get("attempt_id") not in voided]
    pairs, dropped = [], set()
    for i, x in enumerate(done):
        for y in done[i + 1:]:
            if x["attempt_id"] != y["attempt_id"] and x["attempt_id"] not in dropped and y["attempt_id"] not in dropped \
                    and ledger.same_shot(x, y):
                keep, drop = sorted([x, y], key=ledger._richness, reverse=True)
                pairs.append((keep, drop))
                dropped.add(drop["attempt_id"])
    for keep, drop in pairs:
        print("duplicate: %s (%s) duplicates %s (%s), %s" % (drop["attempt_id"], os.path.basename(drop.get("source_file") or ""),
                                                             keep["attempt_id"], os.path.basename(keep.get("source_file") or ""),
                                                             drop.get("outcome")))
        if a.apply:
            ledger.void(path, drop["attempt_id"], "duplicate of %s (same boot from another log)" % keep["attempt_id"])
    print("%d duplicate(s)%s" % (len(pairs), ", VOID appended" if a.apply and pairs else ""))
    return 0


if __name__ == "__main__":
    sys.exit(main())
