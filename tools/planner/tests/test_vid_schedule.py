import unittest

from helpers import S, schedule  # noqa: F401
from planner_auto.vid import canonical_mv, code_to_mv, mv_to_code


class TestVid(unittest.TestCase):
    def test_firmware_rounding_of_lab_values(self):
        table = {344: 55, 338: 54, 331: 53, 325: 52, 319: 51, 312: 50, 350: 56, 356: 57, 362: 58, 369: 59,
                 375: 60, 381: 61, 388: 62, 394: 63, 431: 69, 437: 70, 438: 70, 450: 72, 600: 96, 625: 100,
                 750: 120, 825: 132}
        for mv, code in table.items():
            self.assertEqual(mv_to_code(mv), code, mv)

    def test_code_boundaries(self):
        # x.125 boundaries: 340.625 -> code 54.5; 340 is still 54, 341 is 55
        self.assertEqual(mv_to_code(340), 54)
        self.assertEqual(mv_to_code(341), 55)
        self.assertEqual(code_to_mv(55), 343.75)

    def test_canonical_round_trip(self):
        for code in range(30, 140):
            self.assertEqual(mv_to_code(canonical_mv(code)), code)
        self.assertEqual(canonical_mv(70, prefer=[437]), 437)
        self.assertEqual(canonical_mv(70), 438)


class TestSchedule(unittest.TestCase):
    def test_matches_live_bench_timeline(self):
        # push1 bench shot 325/-60/3x80/vr625 (timing lines as the push1 firmware printed them)
        b = schedule.build_for_step(S.from_list([325, -60, 3, 80, 625, 1, 1]))
        self.assertEqual(b["steps"], [(-1300, 8, 32), (-1200, 7, 0), (-1000, 3, 0), (-1000, 1, 96), (-177, 1, 52),
                                      (-97, 1, 100), (53, 1, 52), (133, 1, 100), (283, 1, 52), (363, 1, 100),
                                      (523, 1, 126), (573, 8, 48)])

    def test_push2_std_198_timeline(self):
        b = schedule.build_for_step(S.from_list([450, -60, 3, 198, 750, 1, 1]))
        self.assertEqual([t for t, _, _ in b["steps"]], [-1300, -1200, -1000, -1000, -177, 21, 171, 369, 519, 717, 877, 927])

    def test_dither_overlap_and_hold_placement(self):
        c = schedule.check_step(S.from_list([350, -60, 3, 80, 750, 1, 3, 0, -20, 2]))
        self.assertEqual(c["overlaps"], [-17])
        c = schedule.check_step(S.from_list([350, -60, 3, 80, 750, 1, 3, 3]))
        self.assertEqual(c["resolved"]["hold_at"], "post")
        self.assertIn((613, schedule.GA_SCL_LOW, 0), c["steps"])
        c = schedule.check_step(S.from_list([350, -400, 1, 80, 750, 1, 1, 3]))
        self.assertEqual(c["resolved"]["hold_at"], "release")
        self.assertIn((0, schedule.GA_SCL_LOW, 0), c["steps"])

    def test_overwritten_and_clamp(self):
        self.assertEqual(schedule.check_step(S.from_list([300, -1000, 1, 15000, 700]))["overwritten"], [-1117])
        self.assertEqual(schedule.check_step(S.from_list([300, -880, 1, 15000, 700]))["overwritten"], [])
        self.assertEqual(schedule.check_step(S.from_list([406, 30, 1, 27, 825, 0]))["resolved"]["pw"], 36)
        lead = schedule.check_step(S.from_list([350, -3000, 1, 1000, 700, 1, 1, 0, -4000]))
        self.assertEqual(lead["resolved"]["reset_at"], -4000)
        self.assertEqual(lead["overwritten"], [])

    def test_overlap_rule_equals_simple_formula(self):
        """dip cmd = td - 117, restore = dip + pw, pulse k at k*(pw+150): overlap iff t <= 0 < t + 47."""
        for pulses in (1, 2, 3):
            for pw in (47, 60, 80, 100, 120, 198, 220):
                for td in range(-400, 260, 3):
                    d = S.from_list([350, td, pulses, pw, 750, 1, 1])
                    got = sorted(schedule.check_step(d)["overlaps"])
                    want = sorted(schedule.simple_overlap_times(td, pw, pulses))
                    self.assertEqual(got, want, (td, pw, pulses))

    def test_delay_and_td_enter_only_as_their_difference(self):
        a = schedule.build_for_step(S.from_list([338, -60, 3, 120, 750, 1, 1, 0, -20, 0, 330, 150]))
        b = schedule.build_for_step(S.from_list([338, -273, 3, 120, 750, 1, 1, 0, -20, 0, 117, 150]))
        self.assertEqual(a["steps"], b["steps"])
        self.assertEqual(S.t0(S.from_list([338, -60, 3, 120, 750, 1, 1, 0, -20, 0, 330])), -390)

    def test_delay_gap_fields_and_exclusions(self):
        d = S.from_list([344, -60, 3, 80, 750, 1])
        self.assertEqual((d["delay"], d["gap"]), (117, 150))
        self.assertEqual(len(S.to_list(d)), 12)
        # 3 x 80, gap 150, td -60: writes at -60 - delay + {0,80,230,310,460,540}; excluded delays in 120..460
        bad = [dl for dl in range(120, 461) if schedule.check_step(dict(d, delay=dl))["overlaps"]]
        ranges = []
        for dl in bad:
            if ranges and dl == ranges[-1][1] + 1:
                ranges[-1][1] = dl
            else:
                ranges.append([dl, dl])
        self.assertEqual(ranges, [[170, 216], [250, 296], [400, 446]])
        # community card B (pw 198): delay 330 crosses the release on our firmware, 340 does not
        self.assertTrue(schedule.check_step(S.from_list([302, -60, 3, 198, 750, 1, 1, 0, -20, 0, 330]))["overlaps"])
        self.assertFalse(schedule.check_step(S.from_list([302, -60, 3, 198, 750, 1, 1, 0, -20, 0, 340]))["overlaps"])

    def test_queued_writes(self):
        steps = [(-80, schedule.GA_VOUT, 50), (-60, schedule.GA_VOUT, 120)]
        self.assertEqual(schedule.queued(steps), [(-80, -80, 1), (-60, -33, 1)])
        self.assertEqual(schedule.release_overlaps(steps), [(-60, 1, 120)])   # scheduled -60 is clear, queued -33 is not
        self.assertEqual(schedule.late_writes(steps), [(-60, -33)])

    def test_overlap_edges(self):
        # restore at td-117+80: overlaps for -47 < t <= 0  ->  td in (-10, 37] for the first restore
        self.assertEqual(schedule.simple_overlap_times(-10, 80, 1), [])          # t = -47: clear
        self.assertEqual(schedule.simple_overlap_times(-9, 80, 1), [-46])
        self.assertEqual(schedule.simple_overlap_times(37, 80, 1), [0])          # write due exactly at t = 0
        self.assertEqual(schedule.simple_overlap_times(38, 80, 1), [])


if __name__ == "__main__":
    unittest.main()
