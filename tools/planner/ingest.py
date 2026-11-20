#!/usr/bin/env python3
"""ingest.py LOG [LOG ...]: parse runner logs into ledger records and append the new ones (fsynced, deduplicated).

LOG may be firstshot / campaign / abtest / queue wrapper output, plain bootrun output (bootrun-<card>.last,
logs/<run>/boot-N.log) or a directory of boot-N.log files. push2 wrapper output names its full per-boot logs
('full logs: DIR'); those are read in place of the grep. Reads logs only; never touches the bench.

  python3 ingest.py --card e runs/20261120T0310-ce/launcher.out
  python3 ingest.py --card e --seq seq.json campaign.out       # campaign/abtest output needs the step file
  python3 ingest.py --card e --fresh bootrun-e.last             # .last files lack the cycle line
"""
import argparse
import glob
import json
import os
import re
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
from planner_auto import launch, ledger, parse, steps as S  # noqa: E402


def card_cfg(card):
    if os.path.exists(launch.card_path(card)):
        return launch.load_card(card)
    return {"card": card}


def expand_paths(paths):
    out = []
    for p in paths:
        if os.path.isdir(p):
            files = [f for f in glob.glob(os.path.join(p, "boot*.log")) if re.search(r"boot-?(\d+)\.log$", f)]
            files.sort(key=lambda f: int(re.search(r"boot-?(\d+)\.log$", f).group(1)))
            out.extend(files)
        else:
            out.append(p)
    return out


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("logs", nargs="+")
    ap.add_argument("--card", required=True, help="card id (cards/<id>.json) or a card config file")
    ap.add_argument("--ledger", help="default: the card config's ledger, else ledger/card-<card>.jsonl")
    ap.add_argument("--seq", help="step list (file or JSON) for campaign/abtest/bootrun output")
    ap.add_argument("--firmware", help="firmware tag when the log does not show it (wrapper greps hide the pico line)")
    ap.add_argument("--date", help="YYYY-MM-DD for boot ids (default: the log's mtime date)")
    g = ap.add_mutually_exclusive_group()
    g.add_argument("--nocycle", action="store_true", help="the boot was not power-cycled (NOCYCLE=1)")
    g.add_argument("--fresh", action="store_true", help="treat a bare bootrun log as a fresh boot (no 'cycle (' line)")
    ap.add_argument("--park-each", action="store_true", help="the run used PARK_EACH=1")
    ap.add_argument("--header-seq", action="append", default=[], metavar="REGEX=JSON",
                    help="step list for wrapper sections whose header matches REGEX (queue scripts), repeatable")
    ap.add_argument("--condition", action="append", default=[], help="k=v recorded on every record, e.g. nvvdd_probe=true")
    ap.add_argument("--dry-run", action="store_true", help="print the records, append nothing")
    ap.add_argument("--quiet", action="store_true")
    a = ap.parse_args(argv)

    cfg = card_cfg(a.card)
    card = str(cfg["card"])
    path = launch.ledger_path(cfg, card, a.ledger)
    conds = {}
    for kv in a.condition:
        k, _, v = kv.partition("=")
        conds[k] = True if v.lower() == "true" else False if v.lower() == "false" else (int(v) if v.lstrip("-").isdigit() else v)
    allrecs = []
    for p in expand_paths(a.logs):
        hseq = dict(x.split("=", 1) for x in a.header_seq)
        recs, rep = parse.parse_file(p, card, seq=a.seq, firmware=a.firmware, date=a.date, nocycle=a.nocycle,
                                     park_each=a.park_each, conditions=conds, header_seq=hseq)
        if a.fresh:
            for r in recs:
                if r["shot_in_boot"] == 0 and r["state_class"] == "later-shot-same-boot":
                    r["state_class"], r["fresh_boot"], r["cycle"] = "first-shot-fresh-boot", True, "asserted"
        allrecs.extend(recs)
        if not a.quiet:
            print("== %s: %d boot(s)%s" % (p, len(rep["boots"]), (", full logs " + ", ".join(rep["full_logs"])) if rep["full_logs"] else ""))
            for b in rep["boots"]:
                print("  boot %s [%s] fw=%s fresh=%s shots=%d armed=%d result=%s"
                      % (b["boot_id"], b["launcher"], b["firmware"], b["fresh"], b["shots"], b["armed"], b["result"]))
            for w in rep["warnings"]:
                print("  WARNING " + w)
            for r in recs:
                if r["phase"] == "ARMED":
                    print("    ARMED  %s %s (no outcome yet)" % (r["attempt_id"], S.short(r["step"])))
                elif r["outcome"] != "under" or r["shot_in_boot"] == 0 or len(recs) <= 12:
                    print("    %-14s %s %-40s %-22s release_at=%s%s" % (
                        r["outcome"], r["attempt_id"].split("@")[1], S.short(r["step"]), r["state_class"],
                        r["release_at"], " WARN" if r["warns"] else ""))
    if a.dry_run:
        for r in allrecs:
            print(json.dumps(r, sort_keys=True))
        return 0
    added, skipped = ledger.append_new(path, allrecs)
    print("ledger %s: +%d record(s), %d already present" % (path, added, skipped))
    return 0


if __name__ == "__main__":
    sys.exit(main())
