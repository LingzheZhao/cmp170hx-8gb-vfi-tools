#!/usr/bin/env python3
"""plan_run.py: print the next batch for a card and the exact launcher command. Runs nothing on the bench.

  python3 plan_run.py --card e                        # default family from cards/e.json
  python3 plan_run.py --card e --family families/e-std3x80-first-push1.json --batch 6
  python3 plan_run.py --card e --json                 # machine-readable plan

An operator runs the printed command by hand, then ingests the run (the second printed command).
"""
import argparse
import datetime as _dt
import json
import os
import shlex
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
from planner_auto import launch, ledger, planner  # noqa: E402
from planner_auto.vid import mv_to_code  # noqa: E402


def build_command(cfg, plan, run_dir, family=None, runner=None):
    """(display command, ingest command) via planner_auto.launch: firstshot.sh (one fresh boot per step) or, for a
    family with launcher mode park_each, one bootrun.sh boot with PARK_EACH=1 and reps."""
    lc = launch.build(cfg, family or {}, plan, run_dir, runner=runner)
    ing = "python3 %s --card %s --firmware %s%s %s" % (os.path.join(HERE, "ingest.py"), plan["card"],
                                                       cfg.get("firmware_expected", "UNKNOWN"),
                                                       " --park-each" if lc["ingest"]["park_each"] else "",
                                                       shlex.quote(lc["out_path"]))
    return lc["display"], ing


def render(plan, cfg, cmd, ing, n_records):
    L = []
    L.append("family %s  card %s  state %s  method %s  (%d ledger records)" % (
        plan["family"], plan["card"], plan["state_class"], plan.get("method", "edge"), n_records))
    L.append("never-rules:")
    L.extend("  " + r for r in plan["rules"])
    if plan["excluded"]:
        L.append("not counted in this family (no information under its policy):")
        for k, v in sorted(plan["excluded"].items()):
            L.append("  %3d x %s" % (v, k))
    if plan["pending"]:
        L.append("pending (ARMED, no outcome yet): %s" % ", ".join(plan["pending"]))
    for c in plan["columns"]:
        L.append("column %d [%s]: %s" % (c["idx"], c["label"], c["status"]))
        for r in c["rows"]:
            L.append("    %s %3d (%s mV): %s" % (plan["knob"], r["value"], "/".join(str(x) for x in r["mv"]), r["tag"]))
        if c["prior_rows"]:
            L.append("    prior firmware (hint only):")
            for r in c["prior_rows"]:
                L.append("      %s %3d (%s mV): %s" % (plan["knob"], r["value"], "/".join(str(x) for x in r["mv"]), r["tag"]))
    if plan["lockin"]:
        lk = plan["lockin"]
        L.append("LOCK-IN %s: %d/%d goldilocks, Wilson lower %.3f at %.0f%% (target %.2f; %s; %s first-shot hits in a row reach it)"
                 % (lk["recipe"], lk["k"], lk["n"], lk["wilson_lower"], 100 * lk["confidence"], lk["target"],
                    "LOCKED" if lk["locked"] else "not yet", lk["runs_if_all_hit"]))
    for line in plan.get("report") or []:
        L.append(line)
    if plan.get("not_before"):
        L.append("next drift round not before %s" % plan["not_before"])
    for w in plan["warnings"]:
        L.append("WARNING " + w)
    L.append("MODE %s: %d proposal(s)" % (plan["mode"], len(plan["proposals"])))
    for i, p in enumerate(plan["proposals"], 1):
        L.append("  %d. %s  code %d  [%s]  %s%s" % (i, json.dumps(p["step_list"]), p["code"], p["label"], p["reason"],
                                                    ("; expect %s" % p["expected"]) if p["expected"] else ""))
    if plan["proposals"]:
        L.append("NOTE: on a GOLDILOCKS HELD, bootrun.sh keeps the card held and runs its ON_HOLD hook if one is set;"
                 " the launcher stops there.")
        if plan["mode"] == "lock-in":
            L.append("NOTE: lock-in repeats end at the first hit while the launcher stops on a hold; hit-rate tracking"
                     " needs a launcher that parks after a hit.")
        L.append("run (by hand, on the machine that runs the launchers):")
        L.append("  " + cmd)
        L.append("then ingest:")
        L.append("  " + ing)
        L.append("and plan again:")
        L.append("  python3 %s --card %s%s" % (os.path.join(HERE, "plan_run.py"), plan["card"],
                                               "" if not plan.get("_family_arg") else " --family " + plan["_family_arg"]))
    return "\n".join(L)


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--card", required=True, help="card id (cards/<id>.json) or a card config file")
    ap.add_argument("--family")
    ap.add_argument("--ledger", help="default: the card config's ledger")
    ap.add_argument("--runner-dir", help="launcher directory (default: the card config's launcher.runner_dir, "
                                         "else $VFI_RUNNER_DIR, else ../runner)")
    ap.add_argument("--batch", type=int)
    ap.add_argument("--run-dir", help="where the launcher keeps its output (default runs/<timestamp>-c<card>)")
    ap.add_argument("--json", action="store_true")
    a = ap.parse_args(argv)
    cfg = launch.load_card(a.card)
    card = str(cfg["card"])
    fam_path = a.family or os.path.join(HERE, cfg["default_family"])
    led = launch.ledger_path(cfg, card, a.ledger)
    recs = ledger.read(led)
    plan = planner.plan(recs, fam_path, cfg, batch=a.batch)
    plan["_family_arg"] = a.family
    run_dir = a.run_dir or os.path.join(HERE, "runs", "%s-c%s" % (_dt.datetime.now().strftime("%Y%m%dT%H%M%S"), card))
    cmd, ing = (build_command(cfg, plan, run_dir, planner.load_family(fam_path), runner=a.runner_dir)
                if plan["proposals"] else (None, None))
    if a.json:
        plan["command"], plan["ingest"] = cmd, ing
        print(json.dumps(plan, indent=1, sort_keys=True, default=str))
    else:
        print(render(plan, cfg, cmd, ing, len(recs)))
    return 0


if __name__ == "__main__":
    sys.exit(main())
