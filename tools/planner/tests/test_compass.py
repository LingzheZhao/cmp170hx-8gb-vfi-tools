"""Compass method (community CRC method) on synthetic ledgers."""
import copy
import unittest

from helpers import CARD, FAMILY, S, rec, schedule
from planner_auto import compass, planner
from planner_auto.rules import Rules


def cfam(**kw):
    f = copy.deepcopy(FAMILY)
    f.update(method="compass", batch=6,
             compass={"delay": {"lo": 120, "hi": 460, "coarse": 30, "fine": 2},
                      "pw": {"lo": 50, "hi": 240, "step": 10, "fine": 2}, "codes_extra": 1})
    f.update(kw)
    return f


EDGE = [rec(344), rec(344), rec(338, "lost"), rec(338, "lost")]     # loss island 55|54 at delay 117


def at(v, delay, outcome="under", pw=80, **kw):
    return rec(v, outcome, delay=delay, pw=pw, **kw)


class TestCompass(unittest.TestCase):
    def test_spread_order(self):
        self.assertEqual(compass.spread_order([1, 2, 3, 4, 5]), [1, 5, 3, 2, 4])
        self.assertEqual(sorted(compass.spread_order(list(range(10)))), list(range(10)))

    def test_loss_island_first(self):
        out = planner.plan([], cfam(), CARD)
        self.assertEqual(out["phase"], "loss island")
        self.assertEqual(out["proposals"][0]["code"], 72)

    def test_delay_sweep_at_shallow_side_skips_release_crossings(self):
        out = planner.plan(EDGE, cfam(batch=20), CARD)
        self.assertEqual(out["phase"], "delay sweep")
        delays = [p["step"]["delay"] for p in out["proposals"]]
        self.assertTrue(all(p["code"] == 55 for p in out["proposals"]))
        self.assertEqual(delays[:3], [460, 300, 150])     # spread order; 117 (the island's delay) is sampled
        self.assertEqual(sorted(delays), [120, 150, 240, 300, 330, 360, 390, 450, 460])
        for p in out["proposals"]:
            self.assertFalse(schedule.check_step(p["step"])["overlaps"])
        self.assertTrue(any("skipped" in line and "180" in line for line in out["report"]))

    def test_isolated_strong_delay_gives_two_band_edges(self):
        sweep = [at(344, d) for d in (120, 150, 240, 300, 330, 390, 450, 460)] + [at(344, 360, "lost")]
        out = planner.plan(EDGE + sweep, cfam(), CARD)
        got = sorted(p["step"]["delay"] for p in out["proposals"])
        self.assertEqual(len(got), 2)
        self.assertTrue(330 < got[0] < 360 < got[1] < 390, got)

    def test_band_edge_bisects_delay_then_tunes_pw(self):
        sweep = [at(344, d) for d in (120, 150, 240, 300, 330)] + [at(344, d, "lost") for d in (360, 390, 450, 460)]
        out = planner.plan(EDGE + sweep, cfam(), CARD)
        self.assertEqual(out["phase"], "band edge")
        p = out["proposals"][0]
        self.assertTrue(330 < p["step"]["delay"] < 360)
        # converge: weak at 330..348, strong from 350
        fine = [at(344, d) for d in range(332, 350, 2)] + [at(344, 350, "lost")]
        out = planner.plan(EDGE + sweep + fine, cfam(), CARD)
        self.assertTrue(any("fine delay edge at code 55: weak 348 | strong 350" in l for l in out["report"]), out["report"])
        self.assertEqual(out["phase"], "pw tune")
        p = out["proposals"][0]
        self.assertEqual((p["step"]["delay"], p["step"]["pw"]), (348, 90))

    def test_pw_tune_capped_by_latch_rule_then_drift(self):
        latch = [rec(381, "controller_off", pw=198, td=200, state="later-shot-same-boot")]
        sweep = [at(344, d) for d in (120, 150, 240, 300, 330)] + [at(344, d, "lost") for d in (360, 390, 450, 460)]
        fine = [at(344, d) for d in range(332, 350, 2)] + [at(344, 350, "lost")]
        rules = Rules(CARD, latch)
        pws = [p for p in range(80, 241, 10) if rules.allowed(S.from_list([344, -60, 3, p, 750, 1, 1, 0, -20, 0, 348]))]
        self.assertEqual(max(pws), 170)                 # 3 x 180 = 540 >= 534.6 at code 55
        tuned = [at(344, 348, pw=p) for p in pws]
        out = planner.plan(latch + EDGE + sweep + fine + tuned, cfam(), CARD)
        self.assertTrue(any("never-rules cap" in l for l in out["report"]), out["report"])
        self.assertEqual(out["phase"], "drift loop")
        self.assertTrue(out["proposals"])
        self.assertTrue(all(p["step"]["pw"] <= 170 for p in out["proposals"]))

    def test_crc_signal_starts_drift_rounds(self):
        recs = EDGE + [at(344, 330, "crc_intermediate", crc=0x75)]
        out = planner.plan(recs, cfam(batch=12), CARD)
        self.assertEqual(out["phase"], "drift loop")
        self.assertEqual([p["step"]["delay"] for p in out["proposals"]], [326, 328, 330, 332, 334])
        self.assertTrue(all(p["code"] == 55 for p in out["proposals"]))
        self.assertTrue(any("CRC 0x75" in l for l in out["report"]), out["report"])
        for p in out["proposals"]:
            self.assertFalse(schedule.check_step(p["step"])["overlaps"])

    def test_drift_prefers_crc_signal_and_skips_illegal_delays(self):
        recs = EDGE + [at(338, 300, "poisoned"), at(344, 248, "crc_intermediate", crc=0x57)]
        out = planner.plan(recs, cfam(batch=12), CARD)
        self.assertTrue(all(p["code"] == 55 for p in out["proposals"]))          # CRC 0x57 outranks a BAD0
        self.assertEqual([p["step"]["delay"] for p in out["proposals"]], [244, 246, 248])   # 250, 252 cross the release
        self.assertTrue(any("skipped" in l for l in out["report"]))

    def test_drift_round_spacing(self):
        import datetime as dt
        r = at(344, 330, "crc_intermediate", crc=0x75)
        r["ingested_at"] = dt.datetime.now().astimezone().isoformat(timespec="seconds")
        out = planner.plan(EDGE + [r], cfam(), CARD)
        self.assertIn("not_before", out)

    def test_widen_when_nothing_flips(self):
        sweep = [at(344, d) for d in (120, 150, 240, 300, 330, 360, 390, 450, 460)]
        out = planner.plan(EDGE + sweep, cfam(batch=3), CARD)
        self.assertTrue(any("widen to code 54" in l for l in out["report"]), out["report"])
        self.assertTrue(all(p["code"] == 54 for p in out["proposals"]))

    def test_equivalent_td_counts_in_the_sweep(self):
        # td -273 / delay 117 is the sweep point delay 330 at td -60
        out = planner.plan(EDGE + [rec(344, td=-273)], cfam(batch=20), CARD)
        self.assertNotIn(330, [p["step"]["delay"] for p in out["proposals"]])

    def test_lockin_takes_over(self):
        out = planner.plan(EDGE + [at(344, 348, "goldilocks")], cfam(), CARD)
        self.assertEqual(out["phase"], "lock-in")
        self.assertEqual(out["proposals"][0]["step"]["delay"], 348)


if __name__ == "__main__":
    unittest.main()
