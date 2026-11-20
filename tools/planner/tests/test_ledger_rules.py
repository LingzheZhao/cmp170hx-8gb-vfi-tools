import os
import tempfile
import unittest

from helpers import CARD, PUSH2, S, ago, ledger, rec
from planner_auto import stats
from planner_auto.rules import Rules


class TestLedgerIO(unittest.TestCase):
    def test_append_read_and_torn_line(self):
        d = tempfile.mkdtemp()
        p = os.path.join(d, "l.jsonl")
        ledger.append(p, [rec(344), rec(338, "lost")])
        with open(p, "a") as f:
            f.write('{"attempt_id": "torn"')  # crash mid-write
        warn = []
        rs = ledger.read(p, warn)
        self.assertEqual(len(rs), 2)
        self.assertEqual(len(warn), 1)

    def test_arm_then_complete_api(self):
        d = tempfile.mkdtemp()
        p = os.path.join(d, "l.jsonl")
        a = ledger.arm(p, rec(344, phase="ARMED", boot="b1"))
        res = ledger.resolve(ledger.read(p))
        self.assertEqual(res[0]["outcome"], "pending")
        ledger.complete(p, a["attempt_id"], "lost", release_at=1)
        res = ledger.resolve(ledger.read(p))
        self.assertEqual(len(res), 1)
        self.assertEqual(res[0]["outcome"], "lost")
        self.assertEqual(res[0]["step"]["v"], 344)
        with self.assertRaises(ValueError):
            ledger.complete(p, a["attempt_id"], "maybe")

    def test_armed_pending_then_unknown(self):
        young = rec(344, phase="ARMED", armed_at=ago(60))
        old = rec(338, phase="ARMED", armed_at=ago(3600))
        res = {r["step"]["v"]: r["outcome"] for r in ledger.resolve([young, old])}
        self.assertEqual(res, {344: "pending", 338: "unknown"})
        self.assertEqual(ledger.effective_outcome(dict(young, outcome="pending"))[0], "pending")


class TestPolicy(unittest.TestCase):
    pol = {"firmware": [PUSH2], "release_window_us": [0, 4]}

    def test_release_window_firmware_overlap_probe(self):
        self.assertEqual(ledger.effective_outcome(rec(344, release_at=2), self.pol)[0], "under")
        self.assertEqual(ledger.effective_outcome(rec(344, release_at=9), self.pol)[0], "mistimed")
        self.assertEqual(ledger.effective_outcome(rec(344, "lost", release_at=9), self.pol)[0], "mistimed")
        self.assertEqual(ledger.effective_outcome(rec(344, firmware="lab-psr1-push1-debug"), self.pol)[0], "excluded")
        dith = rec(350, revert=2)
        self.assertEqual(dith["write_overlaps_release"], [-17])
        self.assertEqual(ledger.effective_outcome(dith, self.pol)[0], "mistimed")
        probe = rec(344, conditions={"nvvdd_probe": True})
        self.assertEqual(ledger.effective_outcome(probe, self.pol)[0], "excluded")
        self.assertEqual(ledger.effective_outcome(rec(344, release_at=None), dict(self.pol, require_release=True))[0], "mistimed")
        self.assertEqual(ledger.effective_outcome(rec(344, release_at=None), self.pol)[0], "under")
        # a hit is never discarded on timing; it is kept for review
        self.assertEqual(ledger.effective_outcome(rec(344, "goldilocks", release_at=9), self.pol)[0], "goldilocks")


class TestRules(unittest.TestCase):
    def test_floor_pw_runner_limits(self):
        r = Rules(CARD)
        self.assertTrue(r.allowed(S.from_list([344, -60, 3, 80, 750, 1])))
        self.assertIn("below FLOOR", r.check(S.from_list([300, -60, 3, 80, 750, 1]))[0])
        self.assertTrue(any("write floor" in w for w in r.check(S.from_list([344, -60, 3, 40, 750, 1]))))
        self.assertTrue(any("outside runner limits" in w for w in r.check(S.from_list([344, -60, 3, 80, 850, 1]))))
        self.assertTrue(any("revert" in w for w in r.check(S.from_list([344, -60, 3, 80, 750, 1, 1, 0, -20, 1]))))
        self.assertTrue(any("t_restore" in w for w in r.check(S.from_list([344, -60, 3, 80, 750, 1, 1, 0, -500]))))

    def test_overlap_and_overwritten_are_never(self):
        r = Rules(CARD)
        w = r.check(S.from_list([344, -100, 3, 198, 750, 1]))   # restore at -19
        self.assertTrue(any("in flight across the PEX_RST release" in x for x in w), w)
        self.assertTrue(r.check(S.from_list([350, -60, 3, 80, 750, 1, 1, 0, -20, 2])))   # dither at -17
        w = r.check(S.from_list([344, -1000, 1, 2000, 700]))
        self.assertTrue(any("precedes the setup write" in x for x in w), w)

    def test_latch_energy_learned_from_controller_off(self):
        latched = [rec(381, "controller_off", pw=198), rec(388, "controller_off", pw=220)]
        r = Rules(CARD, latched)
        self.assertEqual([(x["code"], x["energy"]) for x in r.latches], [(61, 594), (62, 660)])
        # td +200 keeps every write clear of the release, so only the latch rule decides
        self.assertFalse(r.allowed(S.from_list([388, 200, 3, 198, 750, 1])))  # code 62 <= 61+1, 594 >= 534.6
        self.assertFalse(r.allowed(S.from_list([381, 200, 3, 180, 750, 1])))  # 540 >= 534.6
        self.assertTrue(r.allowed(S.from_list([381, 200, 3, 170, 750, 1])))   # 510 < 534.6
        self.assertTrue(r.allowed(S.from_list([381, -60, 3, 120, 750, 1])))   # the short train that did not latch
        self.assertFalse(r.allowed(S.from_list([394, 200, 3, 198, 750, 1])))  # code 63 <= 62+1 and 594 >= 594
        self.assertTrue(r.allowed(S.from_list([400, 200, 3, 198, 750, 1])))   # code 64: shallower than every latch
        self.assertTrue(r.allowed(S.from_list([344, 200, 1, 198, 750, 1])))   # 1 x 198 = 198
        self.assertTrue(any("latch energy" in w for w in r.check(S.from_list([338, 200, 3, 200, 750, 1]))))
        none = Rules(CARD, [rec(381, "lost", pw=198)])
        self.assertEqual(none.latches, [])

    def test_never_list(self):
        cfg = dict(CARD, never=[{"match": {"pw": [1001, 10 ** 6]}, "note": "long"},
                                {"match": {"code": [0, 62], "energy": [594, 10 ** 6]}, "note": "latch copy"},
                                {"match": {"vr": 825}, "note": "overshoot"}])
        r = Rules(cfg)
        self.assertTrue(any("long" in w for w in r.check(S.from_list([450, -880, 1, 15000, 700]))))
        self.assertTrue(any("latch copy" in w for w in r.check(S.from_list([381, -60, 3, 198, 750]))))
        self.assertTrue(any("overshoot" in w for w in r.check(S.from_list([338, -60, 3, 80, 825]))))
        self.assertTrue(r.allowed(S.from_list([338, -60, 3, 80, 800])))


class TestStats(unittest.TestCase):
    def test_wilson(self):
        self.assertAlmostEqual(stats.wilson_lower(10, 10, 0.95), 10 / (10 + stats.z_one_sided(0.95) ** 2), 9)
        self.assertEqual(stats.wilson_lower(0, 0), 0.0)
        self.assertLess(stats.wilson_lower(14, 100), 0.14)
        self.assertGreater(stats.wilson_upper(14, 100), 0.14)
        self.assertEqual(stats.runs_needed(0.9, 0.95), 25)
        self.assertGreater(stats.wilson_lower(59, 59, 0.95), 0.95)


if __name__ == "__main__":
    unittest.main()


class TestDuplicatesAndVoid(unittest.TestCase):
    def test_void_withdraws_a_record(self):
        a, b = rec(344), rec(338, "lost")
        recs = [a, b, {"phase": "VOID", "attempt_id": b["attempt_id"], "reason": "test"}]
        self.assertEqual([r["attempt_id"] for r in ledger.resolve(recs)], [a["attempt_id"]])

    def test_filtered_and_full_log_of_one_boot_collapse(self):
        full = rec(331, boot="e@2026-11-20T10:13:40B", launch_time="2026-11-20T04:11:33", release_at=0)
        filt = rec(331, boot="e@2026-11-20T04:11:33L", launch_time="2026-11-20T04:11:33", release_at=None)
        res = ledger.resolve([filt, full])
        self.assertEqual([r["attempt_id"] for r in res], [full["attempt_id"]])
        d = tempfile.mkdtemp()
        p = os.path.join(d, "l.jsonl")
        ledger.append(p, [full])
        self.assertEqual(ledger.append_new(p, [filt]), (0, 1))

    def test_distinct_boots_are_kept(self):
        a = rec(331, boot="e@x-T10:00:00B", launch_time="2026-11-20T04:00:00", host_boot_id="aaa")
        b = rec(331, boot="e@x-T10:02:30B", launch_time="2026-11-20T04:00:02", host_boot_id="bbb")
        self.assertEqual(len(ledger.resolve([a, b])), 2)
        a.pop("host_boot_id"), b.pop("host_boot_id")
        self.assertEqual(len(ledger.resolve([a, b])), 2)   # two bench START ids are two boots
