#!/usr/bin/env python3
"""hunt.py: autonomous plan -> run -> ingest -> plan loop over the tools/runner launchers.

DRY RUN BY DEFAULT: it plans once and prints the launcher command it would run. Only --execute runs anything, and
then only the launchers (firstshot.sh, or bootrun.sh with PARK_EACH=1); they own the bench, the power cycles and the
optional ON_HOLD hook. hunt.py never talks to the bench, the Pico or a card itself.

  python3 hunt.py --card e                                    # dry run: next batch + command
  python3 hunt.py --card e --family families/e-slow-push2.json --execute --max-boots 12 --max-hours 2

The launchers are found in --runner-dir, else the card config's launcher.runner_dir, else $VFI_RUNNER_DIR, else
../runner next to this file (tools/runner in the repository).

Stop rules (checked per boot, in order): GOLDILOCKS HELD / HOLD SEEN (bootrun holds the card and runs its ON_HOLD
hook if one is set; the hunt stops), NO DIP, identity / preflight / plan-SHA / fresh-boot / power / runner-transfer failure, Traceback, a runner
STOP line not covered below, CONTROLLER OFF the second time in this hunt, park failure in two consecutive boots (by default only a park that fails
without a lost shot before it: after a lost shot the link is dead and the park fails by design; --park-fail-mode
strict counts every one),
MISTIMED / ABORT in two consecutive boots, TEMP GATE FAILED three times in a row, an unexplained launcher exit, a
launcher that outlives --boot-timeout-min (killed by its process group, then stop). Budgets: --max-boots,
--max-hours (checked before every launch; a running launcher is never cut short for budget). Every decision goes to
<run-root>/hunt-<ts>/decisions.jsonl and to stdout.
"""
import argparse
import datetime as _dt
import json
import os
import re
import signal
import subprocess
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
from planner_auto import launch, ledger, parse, planner, steps as S  # noqa: E402

RE_BOOT_HDR = re.compile(r"^######## first-shot boot \d+/\d+:")
RE_LOST = re.compile(r"^(step \d+ .*: lost|TIPPED at step .*: (\d+ )?lost)", re.M)
RULES = [
    ("goldilocks", re.compile(r"HOLD SEEN|>>> GOLDILOCKS HELD|HOLD LINE BUT RUNNER RC")),
    ("no_dip", re.compile(r"NO DIP")),
    ("boot_failure", re.compile(r"IDENTITY MISMATCH|PREFLIGHT FAILED|PLAN SHA MISMATCH|PLAN SHA FAILED|NOT A FRESH BOOT|"
                                r"never came up|did not go down|POWER STILL ON|RUNNER TRANSFER FAILED")),
    ("traceback", re.compile(r"Traceback|AssertionError")),
    ("controller_off", re.compile(r"CONTROLLER OFF|controller unreadable after the shot")),
    ("park_fail", re.compile(r"PARK FAILED|park: rc [1-9]|park after lost shot FAILED")),
    ("mistimed", re.compile(r"MISTIMED|glitch ABORT|firmware refused step")),
    ("temp_gate", re.compile(r"TEMP GATE FAILED")),
    ("partial", re.compile(r"PARTIAL HELD")),
]
KNOWN_STOP = re.compile(r"STOP: (power cycle needed|firmware refused step)")
OK_RC = {0, 2, 10, 13, 14, 16}


class Hunt:
    def __init__(self, a):
        self.a = a
        self.cfg = launch.load_card(a.card)
        self.card = str(self.cfg["card"])
        self.family_path = a.family or os.path.join(HERE, self.cfg["default_family"])
        self.family = planner.load_family(self.family_path)
        self.ledger = launch.ledger_path(self.cfg, self.card, a.ledger)
        self.runner = launch.runner_dir(self.cfg, a.runner_dir)
        ts = _dt.datetime.now().strftime("%Y%m%dT%H%M%S")
        self.dir = os.path.join(a.run_root or os.path.join(HERE, "runs"), "hunt-%s-c%s" % (ts, self.card))
        self.t0 = time.time()
        self.boots = 0
        self.n_ctrl_off = 0
        self.consec = {"park_fail": 0, "mistimed": 0, "temp_gate": 0}
        self.iteration = 0
        self.stop_reason = None

    # ---- logging
    def log(self, event, **kw):
        rec = {"ts": ledger.now_iso(), "event": event, "iteration": self.iteration}
        rec.update(kw)
        if self.a.execute or self.a.log_dry_run:
            os.makedirs(self.dir, exist_ok=True)
            ledger.append(os.path.join(self.dir, "decisions.jsonl"), [rec])
        msg = kw.get("msg") or ""
        print("[%s] %-8s %s" % (rec["ts"][11:19], event, msg), flush=True)

    # ---- budgets
    def budget_left(self):
        left_boots = self.a.max_boots - self.boots
        left_s = self.a.max_hours * 3600 - (time.time() - self.t0)
        return left_boots, left_s

    # ---- one iteration
    def plan(self):
        recs = ledger.read(self.ledger)
        p = planner.plan(recs, self.family_path, self.cfg, batch=self.a.batch)
        self.log("plan", msg="mode %s, %d proposal(s)%s" % (p.get("mode"), len(p["proposals"]),
                 (", not before " + p["not_before"]) if p.get("not_before") else ""),
                 family=p.get("family"), mode=p.get("mode"), phase=p.get("phase"), report=p.get("report"),
                 proposals=[{"step": x["step_list"], "reason": x["reason"], "expected": x.get("expected")}
                            for x in p["proposals"]], ledger_records=len(recs))
        for line in p.get("report") or []:
            print("           " + line)
        return p

    def run_launcher(self, lc):
        os.makedirs(os.path.dirname(lc["out_path"]), exist_ok=True)
        env = dict(os.environ)
        env.update(lc["env"])
        timeout = self.a.boot_timeout_min * 60 * max(1, lc["boots"])
        t = time.time()
        with open(lc["out_path"], "a") as out:
            proc = subprocess.Popen(lc["argv"], cwd=lc["cwd"], env=env, stdout=subprocess.PIPE,
                                    stderr=subprocess.STDOUT, text=True, start_new_session=True)
            self.log("launch", msg="pid %d: %s (%d boot(s), %d shot(s))" % (proc.pid, lc["mode"], lc["boots"], lc["shots"]),
                     pid=proc.pid, argv=lc["argv"], env=lc["env"], steps=lc["steps"])
            timed_out = False
            try:
                for line in proc.stdout:
                    out.write(line)
                    out.flush()
                    print("    | " + line.rstrip("\n"), flush=True)
                    if time.time() - t > timeout:
                        timed_out = True
                        break
            finally:
                if timed_out:
                    os.killpg(proc.pid, signal.SIGTERM)   # our own process group, by exact id
                rc = proc.wait()
                proc.stdout.close()
        return rc, timed_out, time.time() - t

    def ingest(self, lc):
        recs, rep = parse.parse_file(lc["ingest"]["path"], self.card, firmware=None,
                                     date=_dt.date.today().isoformat(), park_each=lc["ingest"]["park_each"])
        fw_missing = [r for r in recs if not r.get("firmware")]
        for r in fw_missing:
            r["firmware"] = lc["ingest"]["firmware"]
        added, skipped = ledger.append_new(self.ledger, recs)
        outs = {}
        for r in recs:
            outs[r.get("outcome")] = outs.get(r.get("outcome"), 0) + 1
        self.log("ingest", msg="+%d record(s) (%d already present): %s" % (added, skipped, outs),
                 added=added, skipped=skipped, outcomes=outs, warnings=rep["warnings"])
        return recs, rep

    def judge(self, text, rc, timed_out, mode):
        """Apply the stop rules boot by boot. Returns a stop reason or None."""
        if timed_out:
            return "launcher exceeded %d min per boot (killed)" % self.a.boot_timeout_min
        lines = text.splitlines()
        segs, cur = [], []
        for ln in lines:
            if RE_BOOT_HDR.match(ln) and cur:
                segs.append(cur)
                cur = []
            cur.append(ln)
        if cur:
            segs.append(cur)
        if mode != "firstshot":
            segs = [lines]
        nboots = sum(1 for ln in lines if RE_BOOT_HDR.match(ln)) if mode == "firstshot" else 1
        self.boots += nboots
        for seg in segs:
            if mode == "firstshot" and not any(RE_BOOT_HDR.match(x) for x in seg):
                continue
            blob = "\n".join(seg)
            hit = {name for name, rx in RULES if rx.search(blob)}
            if "goldilocks" in hit:
                self.log("hold", msg="GOLDILOCKS held: %s" %
                         "; ".join(x for x in seg if "HOLD" in x or "ON_HOLD" in x))
                return "goldilocks held (review the held state and the ON_HOLD output, if any)"
            for name in ("no_dip", "boot_failure", "traceback"):
                if name in hit:
                    return "%s: %s" % (name, next(x for x in seg if RULES[[r[0] for r in RULES].index(name)][1].search(x)))
            if "controller_off" in hit:
                self.n_ctrl_off += 1
                self.log("event", msg="controller off (%d this hunt)" % self.n_ctrl_off)
                if self.n_ctrl_off >= 2:
                    return "controller off twice in this hunt"
            if "park_fail" in hit and self.a.park_fail_mode != "strict" and RE_LOST.search(blob) \
                    and "PARK FAILED before" not in blob:
                # a lost shot leaves the link dead; the park after it fails by design (config 0xffffffff) and the
                # next fresh boot recovers. Only a park that fails without a lost shot counts by default.
                hit.discard("park_fail")
                self.log("event", msg="park failed after a lost shot (expected; not counted)")
            for name, limit in (("park_fail", 2), ("mistimed", 2), ("temp_gate", 3)):
                if name in hit:
                    self.consec[name] += 1
                    self.log("event", msg="%s (%d in a row)" % (name, self.consec[name]))
                    if self.consec[name] >= limit:
                        return "%s %d boots in a row" % (name, self.consec[name])
                else:
                    self.consec[name] = 0
            if "partial" in hit:
                self.log("event", msg="PARTIAL HELD (no sense; the next boot power-cycles); planner will densify")
                if self.a.stop_on_partial:
                    return "partial held (--stop-on-partial)"
            stops = [x for x in seg if x.startswith("STOP:") and not KNOWN_STOP.search(x)]
            if stops and not hit & {"controller_off", "mistimed"}:
                return "runner stop: %s" % stops[0]
        if rc not in OK_RC:
            return "launcher exit %s" % rc
        return None

    def run(self):
        a = self.a
        self.log("start", msg="%s hunt, family %s, ledger %s, runner dir %s, max %d boots / %.1f h" % (
            "EXECUTE" if a.execute else "DRY-RUN", self.family.get("name"), self.ledger, self.runner,
            a.max_boots, a.max_hours), execute=a.execute, family=self.family_path)
        while True:
            self.iteration += 1
            left_boots, left_s = self.budget_left()
            if left_boots <= 0:
                self.stop_reason = "boot budget used (%d)" % self.boots
                break
            if left_s <= 0:
                self.stop_reason = "time budget used (%.1f h)" % a.max_hours
                break
            p = self.plan()
            if not p["proposals"]:
                self.stop_reason = "planner has nothing to propose (mode %s)" % p.get("mode")
                break
            if p.get("not_before"):
                wait = (_dt.datetime.fromisoformat(p["not_before"]) - _dt.datetime.now().astimezone()).total_seconds()
                if wait > 0:
                    wait = min(wait, a.max_wait_s, max(0, left_s))
                    self.log("wait", msg="drift round spacing: %.0f s" % wait, seconds=wait)
                    if a.execute and wait > 0:
                        time.sleep(wait)
            run_dir = os.path.join(self.dir, "it%03d" % self.iteration)
            lc = launch.build(self.cfg, self.family, p, run_dir, runner=self.runner, max_boots=left_boots)
            if not lc["steps"]:
                self.stop_reason = "nothing fits the remaining budget"
                break
            self.log("command", msg=lc["display"], display=lc["display"], mode=lc["mode"], boots=lc["boots"])
            if not a.execute:
                self.stop_reason = "dry run (pass --execute to run it)"
                break
            rc, timed_out, dt = self.run_launcher(lc)
            with open(lc["out_path"]) as f:
                text = f.read()
            self.log("result", msg="launcher rc %s after %.0f s" % (rc, dt), rc=rc, seconds=round(dt))
            self.ingest(lc)
            why = self.judge(text, rc, timed_out, lc["mode"])
            if why:
                self.stop_reason = why
                break
            if a.once:
                self.stop_reason = "--once"
                break
        self.log("stop", msg="%s (%d boot(s), %.1f min)" % (self.stop_reason, self.boots, (time.time() - self.t0) / 60),
                 reason=self.stop_reason, boots=self.boots)
        return 0 if (self.stop_reason or "").startswith(("dry run", "boot budget", "time budget", "planner has", "--once",
                                                          "goldilocks")) else 3


def parser():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--card", required=True, help="card id (cards/<id>.json) or a card config file")
    ap.add_argument("--family")
    ap.add_argument("--ledger", help="default: the card config's ledger")
    ap.add_argument("--runner-dir", help="launcher directory (default: the card config's launcher.runner_dir, "
                                         "else $VFI_RUNNER_DIR, else ../runner)")
    ap.add_argument("--run-root", help="default: runs/ next to this file")
    ap.add_argument("--execute", action="store_true", help="really run the launchers (default: dry run)")
    ap.add_argument("--max-boots", type=int, default=20)
    ap.add_argument("--max-hours", type=float, default=4.0)
    ap.add_argument("--batch", type=int)
    ap.add_argument("--once", action="store_true", help="one plan/run/ingest iteration")
    ap.add_argument("--boot-timeout-min", type=float, default=30.0)
    ap.add_argument("--max-wait-s", type=float, default=900.0, help="cap on a drift-round wait")
    ap.add_argument("--stop-on-partial", action="store_true")
    ap.add_argument("--park-fail-mode", choices=("unexpected", "strict"), default="unexpected",
                    help="unexpected (default): count park failures that do not follow a lost shot; strict: count all")
    ap.add_argument("--log-dry-run", action="store_true", help="write decisions.jsonl in dry-run mode too")
    return ap


def main(argv=None):
    return Hunt(parser().parse_args(argv)).run()


if __name__ == "__main__":
    sys.exit(main())
