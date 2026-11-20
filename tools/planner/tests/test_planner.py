"""Edge-method planner on synthetic ledgers."""
import copy
import unittest

from helpers import CARD, FAMILY, PUSH2, S, ago, rec
from planner_auto import planner
from planner_auto.vid import canonical_mv, mv_to_code


def fam(**kw):
    f = copy.deepcopy(FAMILY)
    f.update(kw)
    return f


def simulate(threshold_code, family, max_boots=40, p_lost=None):
    """Fake card: code > threshold -> under, code <= threshold -> lost (p_lost: per-code alternating outcomes)."""
    recs, boots, counter = [], 0, {}
    while boots < max_boots:
        out = planner.plan(recs, family, CARD)
        if not out["proposals"]:
            return out, recs, boots
        for p in out["proposals"]:
            code = p["code"]
            if p_lost and code in p_lost:
                n = counter.get(code, 0)
                counter[code] = n + 1
                o = "lost" if p_lost[code][n % len(p_lost[code])] else "under"
            else:
                o = "under" if code > threshold_code else "lost"
            st = p["step"]
            recs.append(rec(st["v"], o, td=st["td"], pulses=st["pulses"], pw=st["pw"], vr=st["vr"]))
            boots += 1
    return planner.plan(recs, family, CARD), recs, boots


class TestBisection(unittest.TestCase):
    def test_converges_to_one_code_edge_with_repeats(self):
        for thr in (54, 60, 66, 49):
            out, recs, boots = simulate(thr, fam())
            col = out["columns"][0]
            self.assertTrue(col["status"].startswith("EDGE"), (thr, col["status"]))
            self.assertEqual((col["edge"]["U"], col["edge"]["S"]), (thr + 1, thr))
            at = lambda c: [r for r in recs if mv_to_code(r["step"]["v"]) == c]
            self.assertGreaterEqual(len(at(thr + 1)), 2)
            self.assertGreaterEqual(len(at(thr)), 2)
            self.assertLessEqual(boots, 12, thr)
            self.assertTrue(all(r["step"]["v"] >= 305 for r in recs))

    def test_repeat_requirement(self):
        recs = [rec(344), rec(338, "lost")]
        out = planner.plan(recs, fam(batch=4), CARD)
        self.assertIn("repeats needed", out["columns"][0]["status"])
        got = sorted(p["code"] for p in out["proposals"])
        self.assertEqual(got, [54, 55])
        exp = {p["code"]: p["expected"] for p in out["proposals"]}
        self.assertEqual(exp, {55: "under", 54: "lost"})
        recs += [rec(344), rec(338, "lost")]
        out = planner.plan(recs, fam(), CARD)
        self.assertEqual(out["columns"][0]["status"], "EDGE 55|54")
        self.assertEqual(out["proposals"], [])
        # repeats must come from independent boots: a second shot in the same boot does not count
        same = [rec(344, boot="bx", shot=0), rec(344, boot="bx", shot=1), rec(338, "lost"), rec(338, "lost")]
        out = planner.plan(same, fam(), CARD)
        self.assertIn("repeats needed", out["columns"][0]["status"])
        self.assertEqual([p["code"] for p in out["proposals"]], [55])

    def test_stochastic_edge_is_declared_with_tip_rate(self):
        recs = [rec(344), rec(344), rec(338, "poisoned"), rec(338), rec(338)]
        out = planner.plan(recs, fam(), CARD)
        self.assertEqual(out["columns"][0]["edge"]["p_tip_at_S"], round(1 / 3, 3))

    def test_no_information_is_rerun(self):
        recs = [rec(344, release_at=9), rec(338, "lost", release_at=9)]   # push2 window 0..4
        out = planner.plan(recs, fam(), CARD)
        self.assertEqual(out["columns"][0]["status"], "start")
        self.assertEqual(out["proposals"][0]["code"], 72)
        recs = [rec(344), rec(344), rec(338, "lost"), rec(338, "no_dip"), rec(338, "invalid")]
        out = planner.plan(recs, fam(), CARD)
        self.assertEqual([p["code"] for p in out["proposals"]], [54])

    def test_inverted_band(self):
        recs = [rec(350, "lost"), rec(344), rec(338, "lost")]
        out = planner.plan(recs, fam(batch=8), CARD)
        self.assertIn("inverted band", out["columns"][0]["status"])
        self.assertEqual(sorted({p["code"] for p in out["proposals"]}), [55, 56])

    def test_floor_and_all_under(self):
        recs = [rec(canonical_mv(c)) for c in range(72, 48, -1) for _ in range(1)]
        out = planner.plan(recs, fam(), CARD)
        self.assertIn("all under to the floor", out["columns"][0]["status"])
        self.assertEqual(out["proposals"], [])
        out = planner.plan([], fam(primary={"knob": "code", "start_mv": 320, "coarse_step": 8}), CARD)
        self.assertEqual(out["proposals"][0]["code"], 51)
        out = planner.plan([rec(319)], fam(primary={"knob": "code", "start_mv": 320, "coarse_step": 8}), CARD)
        self.assertEqual(out["proposals"][0]["step"]["v"], 306)   # clamped to the deepest code above FLOOR 305

    def test_latch_rule_limits_a_wide_family(self):
        f = fam(base=dict(FAMILY["base"], pw=198, td=200))
        recs = [rec(381, "controller_off", pw=198, td=-60, state="later-shot-same-boot")]
        out = planner.plan(recs + [rec(canonical_mv(c), td=200, pw=198) for c in range(72, 62, -1)], f, CARD)
        self.assertIn("all under to the floor / never-rules", out["columns"][0]["status"])
        self.assertTrue(any("latch" in r for r in out["rules"]))

    def test_pending_shot_blocks_its_column(self):
        recs = [rec(344), rec(338, "lost"), rec(338, "lost"), rec(344, phase="ARMED", armed_at=ago(30))]
        out = planner.plan(recs, fam(), CARD)
        self.assertEqual(out["proposals"], [])
        self.assertTrue(out["pending"])
        recs[-1]["armed_at"] = ago(7200)
        out = planner.plan(recs, fam(), CARD)
        self.assertEqual([p["code"] for p in out["proposals"]], [55])


class TestColumnsAndLookahead(unittest.TestCase):
    def test_batch_lookahead_opens_columns_after_expected_repeat(self):
        f = fam(columns=[{"vr": 625}, {"pw": 100}], batch=4)
        recs = [rec(344), rec(338, "lost"), rec(338, "lost")]
        out = planner.plan(recs, f, CARD)
        props = [(p["label"], p["code"]) for p in out["proposals"]]
        self.assertEqual(props[0], ("base", 55))
        self.assertIn(("vr 625", 54), props)          # a fresh column starts at the base edge's tipping side
        self.assertIn(("vr 625", 55), props)
        self.assertTrue(all(p["step_list"][6] == 1 for p in out["proposals"]))

    def test_overlap_column_is_blocked(self):
        f = fam(columns=[{"revert": 2}])
        out = planner.plan([rec(344), rec(344), rec(338, "lost"), rec(338, "lost")], f, CARD)
        self.assertTrue(out["columns"][1]["status"].startswith("blocked"))
        self.assertEqual(out["proposals"], [])

    def test_delay_column_pools_equivalent_td(self):
        f = fam(columns=[{"delay": 330}])
        # td -273 with delay 117 is the same waveform as td -60 with delay 330
        recs = [rec(344), rec(344), rec(338, "lost"), rec(338, "lost"), rec(338, td=-273)]
        out = planner.plan(recs, f, CARD)
        rows = {r["value"]: r["tag"] for r in out["columns"][1]["rows"]}
        self.assertEqual(rows, {54: "under 1"})

    def test_prior_firmware_hint(self):
        f = fam(prior_policy={"firmware": ["lab-psr1-push1-debug"], "release_window_us": [-15, 15]}, batch=4)
        p1 = "lab-psr1-push1-debug"
        recs = [rec(344, firmware=p1, release_at=9), rec(338, "poisoned", firmware=p1, release_at=10)]
        out = planner.plan(recs, f, CARD)
        self.assertEqual(sorted(p["code"] for p in out["proposals"]), [54, 55])
        self.assertIn("prior firmware", out["columns"][0]["status"])


class TestSignalsAndLockin(unittest.TestCase):
    def test_partial_densifies(self):
        recs = [rec(338, "partial")]
        out = planner.plan(recs, fam(batch=8), CARD)
        self.assertEqual(out["mode"], "densify")
        steps = [tuple(p["step_list"]) for p in out["proposals"]]
        self.assertEqual(steps[0][:2], (338, -60))
        reasons = " | ".join(p["reason"] for p in out["proposals"])
        self.assertIn("one code weaker", reasons)
        self.assertIn("one code stronger", reasons)
        self.assertIn("td +2", reasons)
        # a neighbour already in the ledger is not proposed again
        out = planner.plan([rec(344), rec(338, "partial")], fam(batch=8), CARD)
        self.assertNotIn("one code weaker", " | ".join(p["reason"] for p in out["proposals"]))

    def test_crc_intermediate_counts_as_signal(self):
        out = planner.plan([rec(338, "crc_intermediate", crc=0x75)], fam(), CARD)
        self.assertEqual(out["mode"], "densify")

    def test_lockin_wilson(self):
        recs = [rec(338, "goldilocks") for _ in range(10)]
        out = planner.plan(recs, fam(batch=3), CARD)
        self.assertEqual(out["mode"], "lock-in")
        self.assertAlmostEqual(out["lockin"]["wilson_lower"], 0.787, places=3)
        self.assertEqual(len(out["proposals"]), 3)
        self.assertEqual(out["proposals"][0]["code"], 54)
        recs = [rec(338, "goldilocks") for _ in range(25)]
        out = planner.plan(recs, fam(), CARD)
        self.assertEqual(out["mode"], "locked")
        self.assertEqual(out["proposals"], [])
        recs = [rec(338, "goldilocks") for _ in range(20)] + [rec(338, "lost") for _ in range(5)]
        out = planner.plan(recs, fam(), CARD)
        self.assertEqual((out["lockin"]["k"], out["lockin"]["n"], out["mode"]), (20, 25, "lock-in"))


if __name__ == "__main__":
    unittest.main()
