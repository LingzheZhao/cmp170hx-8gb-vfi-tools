"""CLI integration: ingest.py, plan_run.py and summary.py on a synthetic log and on samples/sample-ledger.jsonl."""
import json
import os
import subprocess
import sys
import tempfile
import unittest

from helpers import ROOT, ledger
from planner_auto import planner

LOG = """######## first-shot boot 1/2: [[344, -60, 3, 80, 750, 1, 1]] 03:20:00
START temp_raw=362 09:20:00
   prime_at=0
   release_at=0 scheduled=0 via=alarm
step 0 v=344 td=-60 pw=80 vr=750 f=1: under
RESULT: sequence exhausted, all under (runner rc 0)
######## first-shot boot 2/2: [[338, -60, 3, 80, 750, 1, 1]] 03:24:00
START temp_raw=362 09:24:00
   prime_at=0
   release_at=1 scheduled=0 via=alarm
step 0 v=338 td=-60 pw=80 vr=750 f=1: lost
TIPPED at step 0 (v=338 td=-60 pw=80 vr=750 f=1): lost. Rest of this boot is unreliable (sticky poison): power cycle, next boot starts just before this step
FIRSTSHOT: done
"""


def run(*args):
    return subprocess.run([sys.executable] + list(args), cwd=ROOT, capture_output=True, text=True, timeout=120)


class TestCli(unittest.TestCase):
    def setUp(self):
        self.d = tempfile.mkdtemp()
        self.log = os.path.join(self.d, "launcher.out")
        with open(self.log, "w") as f:
            f.write(LOG)
        self.led = os.path.join(self.d, "l.jsonl")

    def test_ingest_dry_run_then_append_then_plan(self):
        r = run("ingest.py", "--card", "e", "--firmware", "lab-psr1-push2-debug", "--date", "2026-11-20",
                "--ledger", self.led, "--dry-run", self.log)
        self.assertEqual(r.returncode, 0, r.stderr)
        js = [json.loads(x) for x in r.stdout.splitlines() if x.startswith("{")]
        self.assertEqual([x["outcome"] for x in js], ["under", "lost"])
        self.assertFalse(os.path.exists(self.led))
        r = run("ingest.py", "--card", "e", "--firmware", "lab-psr1-push2-debug", "--date", "2026-11-20",
                "--ledger", self.led, self.log)
        self.assertIn("+2 record(s)", r.stdout)
        r = run("ingest.py", "--card", "e", "--firmware", "lab-psr1-push2-debug", "--date", "2026-11-20",
                "--ledger", self.led, self.log)
        self.assertIn("+0 record(s), 2 already present", r.stdout)
        r = run("plan_run.py", "--card", "e", "--ledger", self.led, "--family", "families/e-std3x80-first-push2.json",
                "--run-dir", os.path.join(self.d, "run1"))
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("bash firstshot.sh e '[[", r.stdout)
        self.assertIn("FLOOR=305", r.stdout)
        self.assertIn("LOGDIR=", r.stdout)
        self.assertIn("ingest.py --card e --firmware lab-psr1-push2-debug", r.stdout)
        self.assertIn("repeat at the under side", r.stdout)
        r = run("plan_run.py", "--card", "e", "--ledger", self.led, "--run-dir", os.path.join(self.d, "run3"))
        self.assertIn("PARK_EACH=1", r.stdout)            # default family: slow slew, park-normalized shots
        self.assertIn("bash bootrun.sh e '[[", r.stdout)
        self.assertIn("ingest.py --card e --firmware lab-psr1-push2-debug --park-each", r.stdout)
        r = run("plan_run.py", "--card", "e", "--ledger", self.led, "--json", "--family",
                "families/e-compass-push2.json", "--run-dir", os.path.join(self.d, "run2"))
        plan = json.loads(r.stdout)
        self.assertEqual(plan["method"], "compass")
        for p in plan["proposals"]:
            self.assertEqual(len(p["step_list"]), 12)
            self.assertEqual(p["step_list"][6], 1)
        cmd = plan["command"]
        steps = json.loads(cmd.split("firstshot.sh e ")[1].split(" 2>&1")[0].strip("'"))
        self.assertEqual(steps, [p["step_list"] for p in plan["proposals"]])


class TestSample(unittest.TestCase):
    """samples/sample-ledger.jsonl: synthetic records in the ledger format (no measurements)."""

    @classmethod
    def setUpClass(cls):
        cls.path = os.path.join(ROOT, "samples", "sample-ledger.jsonl")
        cls.recs = ledger.read(cls.path)
        with open(os.path.join(ROOT, "cards", "e.json")) as f:
            cls.card = json.load(f)

    def test_sample_is_synthetic_and_valid(self):
        self.assertGreaterEqual(len(self.recs), 10)
        self.assertEqual({r["source"] for r in self.recs}, {"synthetic"})
        for r in self.recs:
            self.assertIn(r["outcome"], ledger.OUTCOMES)
            self.assertIn(r["state_class"], ledger.STATE_CLASSES)
            self.assertEqual(len(r["step_list"]), 12)
        outs = [r["outcome"] for r in ledger.resolve(self.recs)]
        self.assertEqual(outs.count("controller_off"), 1)
        self.assertEqual(outs.count("lost"), 2)

    def test_empty_shipped_ledger(self):
        self.assertEqual(ledger.read(os.path.join(ROOT, "ledger", "card-e.jsonl")), [])

    def test_summary_on_the_sample(self):
        r = run("summary.py", "--card", "e", "--ledger", self.path)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("card e: 14 resolved records", r.stdout)
        self.assertIn("== e-slow-push2: 13 shot(s)", r.stdout)
        self.assertIn("2L", r.stdout)

    def test_plan_on_the_sample(self):
        d = tempfile.mkdtemp()
        r = run("plan_run.py", "--card", "e", "--ledger", self.path, "--json", "--run-dir", os.path.join(d, "run"))
        self.assertEqual(r.returncode, 0, r.stderr)
        plan = json.loads(r.stdout)
        self.assertEqual(plan["family"], "e-slow-push2")
        self.assertTrue(plan["proposals"])
        self.assertTrue(any("latch: code <= 60" in x for x in plan["rules"]), plan["rules"])
        rules = __import__("planner_auto.rules", fromlist=["Rules"]).Rules(self.card, ledger.resolve(self.recs))
        for p in plan["proposals"]:
            self.assertGreaterEqual(p["step"]["v"], self.card["floor_mv"])
            self.assertTrue(rules.allowed(p["step"]), p)
        self.assertIn("bootrun.sh e '[[", plan["command"])
        self.assertIn("PARK_EACH=1", plan["command"])


if __name__ == "__main__":
    unittest.main()
