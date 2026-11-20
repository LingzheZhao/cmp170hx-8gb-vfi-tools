"""Offline tests for tools/runner/bisect_shots.py against the fake ga100vfi package in tests/mock (no serial port, no
BAR0, no park: VFI_PARK points at a shell stub that logs each call). Also checks the shell scripts' syntax.

Run: python3 -I tests/test_runner.py
"""
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
RUNNER = os.path.join(ROOT, "tools", "runner", "bisect_shots.py")
MOCK = os.path.join(HERE, "mock")
STD = [450, -60, 3, 198, 750, 1]


def run(seq, shots, **env):
    with tempfile.TemporaryDirectory() as tmp:
        log = os.path.join(tmp, "park.log")
        e = {k: v for k, v in os.environ.items() if not k.startswith(("VFI_", "MOCK", "REPEAT", "SENTINEL", "FLOOR",
                                                                       "PARK_EACH", "TRACE", "CELLLOST", "LOSTRUN"))}
        e.update(PYTHONPATH=MOCK, VFI_BDF="0000:05:00.0", VFI_PORT="/dev/null", VFI_TRACE_DIR=tmp, FLOOR="340",
                 MOCK=",".join(shots), MOCK_LOG=log,
                 VFI_PARK="sh -c 'echo PARK >> \"$MOCK_LOG\"; echo park: mock rail held 760 mV; exit ${MOCK_PARK_RC:-0}'")
        e.update({k: str(v) for k, v in env.items()})
        p = subprocess.run([sys.executable, RUNNER, "card T", json.dumps(seq)], env=e, cwd=tmp,
                           capture_output=True, text=True, timeout=60)
        parks = 0
        if os.path.exists(log):
            with open(log) as f:
                parks = f.read().count("PARK")
    return p.returncode, p.stdout + p.stderr, parks


class RunnerTest(unittest.TestCase):
    def test_all_under(self):
        rc, out, parks = run([STD, [438] + STD[1:]], "uu")
        self.assertEqual(rc, 0, out)
        self.assertIn("step 1 v=438 td=-60 pw=198 vr=750 f=1: under", out)
        self.assertIn("sequence exhausted, all under", out)
        self.assertEqual(parks, 1)                                   # the final park

    def test_lost_single_shot_mode_tips(self):
        rc, out, parks = run([STD, STD], "l")
        self.assertEqual(rc, 10, out)
        self.assertIn("step 0 v=450 td=-60 pw=198 vr=750 f=1: lost", out)
        self.assertIn("TIPPED at step 0", out)
        self.assertNotIn("step 1", out)

    def test_cell_mode_parks_after_lost_and_goes_on(self):
        rc, out, parks = run([STD + [3]], "lu")
        self.assertEqual(rc, 0, out)
        self.assertIn("park after lost shot: park: mock rail held 760 mV", out)
        self.assertIn("cell 0 v=450 td=-60 np=3 pw=198 vr=750: {'under': 2, 'lost': 1, 'partial': 0, 'other': 0}", out)
        self.assertEqual(parks, 2)

    def test_cell_mode_failed_park_tips(self):
        rc, out, parks = run([STD + [3]], "l", MOCK_PARK_RC=4)
        self.assertEqual(rc, 10, out)
        self.assertIn("lost and the park failed", out)

    def test_lostrun_ends_a_cell_run(self):
        rc, out, parks = run([STD + [5]], "lll", LOSTRUN=2)
        self.assertEqual(rc, 10, out)
        self.assertIn("2 lost in a row", out)

    def test_celllost_skips_the_rest_of_a_cell(self):
        rc, out, parks = run([STD + [4], [438] + STD[1:] + [1]], "ll", CELLLOST=2)
        self.assertEqual(rc, 0, out)
        self.assertIn("cell 0 (skipped after 2 lost in a row)", out)
        self.assertIn("cell 1 v=438", out)

    def test_partial_counts_and_continues(self):
        rc, out, parks = run([STD + [2]], "pu")
        self.assertEqual(rc, 0, out)
        self.assertIn("dsov0 crc=0x55 priv=0xfffffefe", out)
        self.assertIn("'partial': 1", out)

    def test_poisoned_fusectrl_stops(self):
        rc, out, parks = run([STD], "x")
        self.assertEqual(rc, 10, out)
        self.assertIn("poisoned FUSECTRL=0xbadf1701 (no further BAR0 reads)", out)
        self.assertIn("sticky poison", out)

    def test_poisoned_dsov_stops_without_reread(self):
        rc, out, parks = run([STD], "d")
        self.assertEqual(rc, 10, out)
        self.assertIn("poisoned (dsov read 0xBADF; no re-read)", out)
        self.assertNotIn("half-boot", out)

    def test_over0_stops(self):
        rc, out, parks = run([STD], "o")
        self.assertEqual(rc, 10, out)
        self.assertIn(": poisoned/over0", out)

    def test_goldilocks_is_held_not_parked(self):
        rc, out, parks = run([STD, [438] + STD[1:]], "ug")
        self.assertEqual(rc, 0, out)
        self.assertIn(">>> GOLDILOCKS HELD (card T) v=438 td=-60", out)
        self.assertEqual(parks, 0)

    def test_goldilocks_needs_dsov_exactly_zero(self):
        for code, text in (("f", "dsov=0xffffffff crc=0x55 priv=0xffffffff"), ("t", "dsov=0x2 crc=0x55 priv=0xffffffff")):
            with self.subTest(code=code):
                rc, out, parks = run([STD], code)
                self.assertEqual(rc, 10, out)
                self.assertNotIn("GOLDILOCKS", out)
                self.assertIn(text, out)

    def test_hidden_poison_sentinel(self):
        rc, out, parks = run([STD], "h")
        self.assertEqual(rc, 10, out)
        self.assertIn("hidden-poison alias 0x820410=0xbadf1020", out)
        rc, out, parks = run([STD], "h", SENTINEL="0")
        self.assertEqual(rc, 0, out)

    def test_gfw_gate(self):
        rc, out, parks = run([STD], "b")
        self.assertEqual(rc, 10, out)
        self.assertIn("gfw-not-done 0x00000001", out)

    def test_no_dip_stops(self):
        rc, out, parks = run([STD, STD], "n")
        self.assertEqual(rc, 12, out)
        self.assertIn("NO DIP (controller pwd=False e1=0xc2fa): the shot did not run; stop", out)

    def test_controller_off_stops(self):
        rc, out, parks = run([STD], "c")
        self.assertEqual(rc, 9, out)
        self.assertIn("CONTROLLER OFF (0x2042, 41 mV)", out)
        self.assertIn("STOP: power cycle needed", out)

    def test_floor_and_recovery_bounds(self):
        rc, out, parks = run([STD, [330] + STD[1:]], "uu")
        self.assertNotEqual(rc, 0, out)
        self.assertIn("AssertionError", out)
        rc, out, parks = run([[450, -60, 3, 198, 900]], "u")
        self.assertNotEqual(rc, 0, out)
        self.assertIn("AssertionError", out)

    def test_shot_cap(self):
        rc, out, parks = run([STD + [10]], "", SHOTCAP=4)
        self.assertEqual(rc, 0, out)
        self.assertIn("SHOT CAP 4 reached", out)

    def test_needs_bdf_and_arguments(self):
        p = subprocess.run([sys.executable, RUNNER], capture_output=True, text=True,
                           env={"PYTHONPATH": MOCK, "PATH": os.environ.get("PATH", "")})
        self.assertEqual(p.returncode, 2)
        p = subprocess.run([sys.executable, RUNNER, "t", "[]"], capture_output=True, text=True,
                           env={"PYTHONPATH": MOCK, "PATH": os.environ.get("PATH", "")})
        self.assertNotEqual(p.returncode, 0)
        self.assertIn("VFI_BDF", p.stderr)


class ShellTest(unittest.TestCase):
    def test_syntax(self):
        d = os.path.join(ROOT, "tools", "runner")
        for f in ("bootrun.sh", "cycle.sh", "power.sh", "bench.conf.example"):
            with self.subTest(f=f):
                p = subprocess.run(["bash", "-n", os.path.join(d, f)], capture_output=True, text=True)
                self.assertEqual(p.returncode, 0, p.stderr)

    def test_bootrun_refuses_without_config(self):
        d = os.path.join(ROOT, "tools", "runner")
        p = subprocess.run(["bash", os.path.join(d, "bootrun.sh"), "x", "[]"], capture_output=True, text=True,
                           env={"BENCH_CONF": "/nonexistent/bench.conf", "PATH": os.environ.get("PATH", "")})
        self.assertEqual(p.returncode, 2, p.stdout + p.stderr)

    def test_bootrun_unknown_card(self):
        d = os.path.join(ROOT, "tools", "runner")
        with tempfile.TemporaryDirectory() as tmp:
            conf = os.path.join(tmp, "bench.conf")
            shutil.copy(os.path.join(d, "bench.conf.example"), conf)
            p = subprocess.run(["bash", os.path.join(d, "bootrun.sh"), "nosuchcard", "[]"], capture_output=True,
                               text=True, env={"BENCH_CONF": conf, "PATH": os.environ.get("PATH", "")})
        self.assertEqual(p.returncode, 2, p.stdout + p.stderr)
        self.assertIn("define card_nosuchcard", p.stdout)


if __name__ == "__main__":
    unittest.main(verbosity=2)
