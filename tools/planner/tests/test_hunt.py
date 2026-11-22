"""hunt.py against the fake bench (tests/fakebench): no hardware, no ssh, never the real launchers."""
import contextlib
import io
import json
import os
import tempfile
import unittest

from helpers import ROOT, ledger, rec
import hunt  # noqa: E402  (ROOT is on sys.path via helpers)
import summary  # noqa: E402

FAKE = os.path.join(ROOT, "tests", "fakebench")
EDGE_FAM = {"name": "t-edge", "card": "e", "state_class": "first-shot-fresh-boot", "equivalent_states": ["post-park"],
            "base": {"td": -60, "pulses": 3, "pw": 80, "vr": 750, "fast": 1},
            "primary": {"knob": "code", "start_mv": 362, "coarse_step": 4, "column_step": 1},
            "repeats": 2, "batch": 2, "launcher": {"mode": "firstshot"},
            "policy": {"firmware": ["lab-psr1-push2-debug"], "release_window_us": [0, 4]}}
PARK_FAM = dict(EDGE_FAM, name="t-park", method="compass", batch=6,
                launcher={"mode": "park_each", "max_shots_per_boot": 12},
                compass={"delay": {"lo": 120, "hi": 460, "coarse": 30, "fine": 2},
                         "drift": {"set": 5, "offsets": [-4, -2, 0, 2, 4], "shots": 2, "round_interval_s": 0}})


class HuntCase(unittest.TestCase):
    def setUp(self):
        self.d = tempfile.mkdtemp()
        self.led = os.path.join(self.d, "card-e.jsonl")
        self.state = os.path.join(self.d, "fake_state.json")
        os.environ["FAKE_STATE"] = self.state
        os.environ["FAKE_MODEL"] = os.path.join(self.d, "model.json")
        for k in ("PARK_EACH", "NOCYCLE", "LOGDIR", "BOOTRUN_LOG"):
            os.environ.pop(k, None)
        self.model({})

    def model(self, m):
        m.setdefault("default_lost_at_or_below", 54)
        with open(os.environ["FAKE_MODEL"], "w") as f:
            json.dump(m, f)

    def fam(self, f):
        p = os.path.join(self.d, "fam-%s.json" % f["name"])
        with open(p, "w") as fh:
            json.dump(f, fh)
        return p

    def hunt(self, fam, *extra):
        argv = ["--card", "e", "--family", self.fam(fam), "--ledger", self.led, "--runner-dir", FAKE,
                "--run-root", self.d, "--boot-timeout-min", "2"] + list(extra)
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            h = hunt.Hunt(hunt_args(argv))
            rc = h.run()
        self.out = buf.getvalue()
        self.h = h
        return rc

    def calls(self):
        try:
            with open(self.state) as f:
                return json.load(f).get("calls", [])
        except OSError:
            return []

    def decisions(self):
        with open(os.path.join(self.h.dir, "decisions.jsonl")) as f:
            return [json.loads(x) for x in f]


def hunt_args(argv):
    return hunt.parser().parse_args(argv)


class TestHunt(HuntCase):
    def test_dry_run_runs_nothing(self):
        rc = self.hunt(EDGE_FAM)
        self.assertEqual(rc, 0)
        self.assertEqual(self.calls(), [])
        self.assertIn("DRY-RUN", self.out)
        self.assertIn("bash firstshot.sh e '[[362,-60,3,80,750,1,1,0,-20,0,117,150]]'", self.out)
        self.assertEqual(self.h.stop_reason, "dry run (pass --execute to run it)")
        self.assertFalse(os.path.exists(self.led))

    def test_execute_converges_to_the_edge_and_logs_every_decision(self):
        rc = self.hunt(EDGE_FAM, "--execute", "--max-boots", "14")
        self.assertEqual(rc, 0, self.out)
        res = ledger.resolve(ledger.read(self.led))
        by = {}
        for r in res:
            by.setdefault(r["vid_code"], []).append(r["outcome"])
        self.assertGreaterEqual(by[55].count("under"), 2, by)
        self.assertGreaterEqual(by[54].count("lost"), 2, by)
        self.assertTrue(all(r["state_class"] == "first-shot-fresh-boot" for r in res))
        self.assertTrue(self.h.stop_reason.startswith("planner has nothing"), self.h.stop_reason)
        ev = [d["event"] for d in self.decisions()]
        for e in ("start", "plan", "command", "launch", "result", "ingest", "stop"):
            self.assertIn(e, ev)
        self.assertTrue(all(c["mode"] == "firstshot" for c in self.calls()))
        self.assertLessEqual(self.h.boots, 14)

    def test_boot_budget(self):
        self.hunt(EDGE_FAM, "--execute", "--max-boots", "3")
        self.assertEqual(self.h.boots, 3)
        self.assertTrue(self.h.stop_reason.startswith("boot budget"), self.h.stop_reason)
        self.assertEqual(sum(len(c["steps"]) for c in self.calls()), 3)

    def test_goldilocks_stops_after_the_hand_off(self):
        self.model({"signals": [{"v": 362, "outcome": "goldilocks"}]})
        rc = self.hunt(EDGE_FAM, "--execute")
        self.assertEqual(rc, 0)
        self.assertTrue(self.h.stop_reason.startswith("goldilocks held"), self.h.stop_reason)
        self.assertEqual(len(self.calls()), 1)
        res = ledger.resolve(ledger.read(self.led))
        self.assertEqual([r["outcome"] for r in res], ["goldilocks"])

    def test_no_dip_identity_traceback_stop_at_once(self):
        for ev, why in (("no_dip", "no_dip"), ("identity", "boot_failure"), ("traceback", "traceback")):
            self.setUp()
            self.model({"events": {"1": ev}})
            rc = self.hunt(EDGE_FAM, "--execute")
            self.assertEqual(rc, 3)
            self.assertTrue(self.h.stop_reason.startswith(why), (ev, self.h.stop_reason))
            self.assertEqual(self.h.boots, 1)

    def test_stall_hold_host_guard_and_die_id_stop_at_once(self):
        # firstshot.sh output: a held stall frame exits 13 and a host guard or die ID stop exits 2, both in OK_RC
        self.hunt(EDGE_FAM)
        hdr = "######## first-shot boot 1/2: [[362,-60,3,80,750,1,1]] 01:02:03"
        for lines, rc, why in (
                ([">>> STALL DSOV0 HELD (card e): a goldilocks signature (DSOV 0, CRC 0x55, full mask) while GFW is "
                  "stalled; no sense, no next shot, no g/r/j; review it", "FIRSTSHOT: partial held for read-only inspection"],
                 13, "stall DSOV 0 held"),
                ([">>> STALL DSOV0 HELD (card e): DSOV 0 while GFW is stalled; no sense, no next shot, no g/r/j; review it",
                  "FIRSTSHOT: partial held for read-only inspection"], 13, "stall DSOV 0 held"),
                (["HOST GUARD: the nvidia module is loaded: stop", "RESULT: HOST GUARD: the nvidia module is loaded: stop "
                  "(runner rc 3)", "FIRSTSHOT: stop for review (bootrun rc 3)"], 2, "boot_failure"),
                (["  WARN: DIE ID MISMATCH: fuse rows [8] differ from /opt/vfi/plan/dump.json: not the card that dump "
                  "came from", "DIE ID MISMATCH or unreadable: stop", "FIRSTSHOT: bootrun exit 5: stop for review"],
                 2, "boot_failure")):
            with self.subTest(why=why, line=lines[0][:40]):
                got = self.h.judge("\n".join([hdr] + lines), rc, False, "firstshot")
                self.assertTrue(got and got.startswith(why), got)
        got = self.h.judge(">>> STALL DSOV0 HELD (card e): a goldilocks signature (DSOV 0, CRC 0x55, full mask) while "
                           "GFW is stalled", 15, False, "bootrun")
        self.assertIn("goldilocks signature", got)

    def test_controller_off_twice(self):
        # the first latch at 362 (3x80) forbids deeper 3x80 shots; the planner climbs to 388 and latches again
        self.model({"events": {"1": "controller_off", "2": "controller_off"}})
        self.hunt(dict(EDGE_FAM, batch=1), "--execute")
        self.assertEqual(self.h.stop_reason, "controller off twice in this hunt")
        self.assertEqual(self.h.boots, 2)
        self.assertEqual([c["steps"][0][0] for c in self.calls()], [362, 388])

    def test_temp_gate_three_in_a_row(self):
        self.model({"events": {"1": "temp_gate", "2": "temp_gate", "3": "temp_gate"}})
        self.hunt(dict(EDGE_FAM, batch=1), "--execute")
        self.assertEqual(self.h.stop_reason, "temp_gate 3 boots in a row")
        self.setUp()
        self.model({"events": {"1": "temp_gate", "2": "temp_gate", "4": "temp_gate"}})
        self.hunt(dict(EDGE_FAM, batch=1), "--execute", "--max-boots", "5")
        self.assertTrue(self.h.stop_reason.startswith("boot budget"), self.h.stop_reason)

    def test_mistimed_twice(self):
        self.model({"events": {"1": "mistimed", "2": "mistimed"}})
        self.hunt(dict(EDGE_FAM, batch=1), "--execute")
        self.assertEqual(self.h.stop_reason, "mistimed 2 boots in a row")
        res = ledger.resolve(ledger.read(self.led))
        self.assertEqual({r["outcome"] for r in res}, {"mistimed"})

    def test_park_failures(self):
        fam = dict(PARK_FAM, method="edge", batch=4)
        self.model({"events": {"1": "park_fail", "2": "park_fail"}, "default_lost_at_or_below": 40})
        self.hunt(fam, "--execute")
        self.assertEqual(self.h.stop_reason, "park_fail 2 boots in a row")
        # a park that fails after a lost shot is expected and does not count by default
        deep = dict(EDGE_FAM, batch=1, primary={"knob": "code", "start_mv": 450, "coarse_step": 4})
        self.setUp()
        self.model({"default_lost_at_or_below": 70})
        self.hunt(deep, "--execute", "--max-boots", "4")
        self.assertTrue(self.h.stop_reason.startswith("boot budget"), self.h.stop_reason)
        self.setUp()
        self.model({"default_lost_at_or_below": 70})
        self.hunt(deep, "--execute", "--max-boots", "4", "--park-fail-mode", "strict")
        self.assertEqual(self.h.stop_reason, "park_fail 2 boots in a row")


class TestParkEachAndDrift(HuntCase):
    def test_park_each_cells(self):
        fam = dict(PARK_FAM, method="edge", batch=4)
        ledger.append(self.led, [rec(331, "under"), rec(325, "lost")])
        self.model({"default_lost_at_or_below": 52})
        self.hunt(fam, "--execute", "--once")
        c = self.calls()
        self.assertEqual(len(c), 1)
        self.assertTrue(c[0]["park_each"])
        self.assertEqual([(s[0], s[6]) for s in c[0]["steps"]], [(331, 1), (325, 1)])   # expected loss last
        res = [r for r in ledger.resolve(ledger.read(self.led)) if r.get("park_each")]
        self.assertEqual([(r["step"]["v"], r["state_class"], r["outcome"]) for r in res],
                         [(331, "first-shot-fresh-boot", "under"), (325, "post-park", "lost")])

    def test_drift_rounds_around_a_sporadic_bad0(self):
        ledger.append(self.led, [rec(331, "under"), rec(331, "under"), rec(325, "lost"), rec(325, "lost"),
                                 rec(331, "poisoned", delay=330, crc=None), rec(331, "under", delay=330)])
        self.model({"default_lost_at_or_below": 52})
        self.hunt(PARK_FAM, "--execute", "--once", "--max-wait-s", "0")
        call = self.calls()[0]
        self.assertTrue(call["park_each"])
        self.assertEqual([s[10] for s in call["steps"]], [326, 328, 330, 332, 334])
        self.assertTrue(all(s[6] == 2 for s in call["steps"]))
        plans = [d for d in self.decisions() if d["event"] == "plan"]
        self.assertEqual(plans[0]["mode"], "drift loop")

    def test_summary_map(self):
        ledger.append(self.led, [rec(331, "under"), rec(331, "under", delay=300), rec(331, "poisoned", delay=300),
                                 rec(325, "lost"), rec(331, "crc_intermediate", delay=330, crc=0x75)])
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            summary.main(["--card", "e", "--ledger", self.led, "--family", self.fam(PARK_FAM)])
        out = buf.getvalue()
        self.assertIn("pw 80 us:", out)
        self.assertIn("d300", out)
        row = [l for l in out.splitlines() if l.strip().startswith("53 (331)")][0]
        self.assertIn("1P 1u", row)
        self.assertIn("1i", row)


if __name__ == "__main__":
    unittest.main()
