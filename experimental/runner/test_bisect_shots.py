"""End-to-end tests of the experimental bisect_shots.py against the fake ga100vfi package in fake/ (no serial port,
no BAR0, no park).

Run: GA100VFI_HOST=<pico-ga100vfi clone with firmware/0004 applied>/host python3 -I experimental/runner/test_bisect_shots.py
(GA100VFI_HOST is needed only for the observer copy check against passive_snapshot.py; without it that test is
skipped.)"""
import ast
import json
import os
import random
import subprocess
import sys
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
RUNNER = os.path.join(HERE, "bisect_shots.py")
DRIVE = os.path.join(HERE, "fake", "drive.py")
if os.environ.get("GA100VFI_HOST"):
    sys.path.insert(0, os.environ["GA100VFI_HOST"])
try:
    from passive_snapshot import observe as passive_observe  # noqa: E402
except ImportError:
    passive_observe = None

FUSECTRL, GFW, DSOV, CRC, PLM, SENT = "RD 820000", "RD 118234", "RD 820084", "RD 820070", "RD 8200fc", "RD 820410"
STD = [450, -60, 3, 198, 750, 1]


def run(seq, shots, **env):
    with tempfile.TemporaryDirectory() as tmp:
        log = os.path.join(tmp, "events")
        e = {k: v for k, v in os.environ.items() if not k.startswith(("FAKE_", "REPEAT", "SENTINEL", "FLOOR", "VFI_"))}
        e.update(FAKE_LOG=log, FAKE_SHOTS=",".join(shots), FLOOR="381", VFI_BDF="0000:05:00.0")
        e.update({k: str(v) for k, v in env.items()})
        p = subprocess.run([sys.executable, "-I", DRIVE, RUNNER, "card T", json.dumps(seq)], env=e, cwd=tmp,
                           capture_output=True, text=True, timeout=60)
        events = []
        if os.path.exists(log):
            with open(log) as f:
                events = f.read().splitlines()
    return p.returncode, p.stdout + p.stderr, events


def after_last_shot(events):
    last = max(i for i, x in enumerate(events) if x.startswith("SHOT"))
    return events[last + 1:]


def bar0_reads(events):
    return [x for x in events if x.startswith(("RD", "WR"))]


class RunnerTest(unittest.TestCase):
    def assertNoBar0Writes(self, events):
        self.assertFalse([x for x in events if x.startswith("WR")])

    def test_under_default_step_is_the_old_command(self):
        rc, out, ev = run([STD], "u")
        self.assertEqual(rc, 0, out)
        self.assertIn("step 0 v=450 td=-60 pw=198 vr=750 f=1: under", out)
        self.assertIn("sequence exhausted, all under", out)
        self.assertIn("SHOT u g 450 -60 750 -20 0 1 0 3 198 150 117 0", ev)   # what the old runner sent
        self.assertEqual(bar0_reads(after_last_shot(ev)), [FUSECTRL, GFW, DSOV, CRC, PLM, SENT])
        self.assertEqual(ev.count("PARK"), 1)                                   # final park, as before

    def test_boot_log_unlock_and_firmware_lines(self):
        rc, out, ev = run([STD], "u")
        self.assertEqual(rc, 0, out)
        self.assertIn("SER u", ev)
        self.assertNotIn("SER z", ev[:ev.index("SER u")])                      # `u` sent without asking `z` first
        self.assertIn("unlock: unlock already MATCH (CML 0x08) | unlock MATCH wp=00 cfg=1229", out)
        self.assertNotIn("WARNING", out)
        self.assertIn("boot pmbus (@r 25 <reg> <n>; words shown high byte first): "
                      "13=00 46=1229 7e=08 79=2c00 88=2f00 8b=02c9 8d=0170", out)
        boot = [x for x in ev if x.startswith("PMBUS")][:7]
        self.assertEqual(boot, ["PMBUS r 25 13 01", "PMBUS r 25 46 02", "PMBUS r 25 7e 01", "PMBUS r 25 79 02",
                                "PMBUS r 25 88 02", "PMBUS r 25 8b 02", "PMBUS r 25 8d 02"])
        self.assertLess(ev.index("PMBUS r 25 8d 02"), min(i for i, x in enumerate(ev) if x.startswith("SHOT")))
        for line in ("   glitch vd=450 td=-60 vr=750 tr=-20 tv=0 fast=1 hold=0 pw=198 gap=150",
                     "   timing i=0 scheduled=-1300", "   release_at=0 scheduled=0 via=alarm", "   glitch done pwd=1"):
            self.assertIn(line, out)

    def test_lost_then_park_continues_the_cell(self):
        rc, out, ev = run([STD[:5] + [1, 3]], "lu")
        self.assertEqual(rc, 0, out)
        self.assertIn("park after lost shot: park: rail held 787 mV", out)
        self.assertIn("cell 0 v=450 td=-60 np=3 pw=198 vr=750: {'under': 2, 'lost': 1, 'partial': 0, 'other': 0}", out)
        first = ev.index("SHOT l g 450 -60 750 -20 0 1 0 3 198 150 117 0")
        second = [i for i, x in enumerate(ev) if x.startswith("SHOT")][1]
        self.assertEqual(ev[first + 1:second].count("PARK"), 1)
        self.assertFalse(bar0_reads(ev[first + 1:second]))                     # nothing read on a dead link
        self.assertEqual(ev.count("PARK"), 2)

    def test_lost_in_single_shot_mode_tips(self):
        rc, out, ev = run([STD], "l")
        self.assertEqual(rc, 10, out)
        self.assertIn("step 0 v=450 td=-60 pw=198 vr=750 f=1: lost", out)
        self.assertIn("TIPPED at step 0", out)
        self.assertEqual(ev.count("PARK"), 1)

    def test_lost_park_failure_stops(self):
        rc, out, ev = run([STD[:5] + [1, 3]], "l", FAKE_PARK_RC=4)
        self.assertEqual(rc, 10, out)
        self.assertIn("lost and the park failed", out)

    def test_poisoned_dsov_stops_reads(self):
        rc, out, ev = run([STD], "d")
        self.assertEqual(rc, 10, out)
        self.assertIn("poisoned DSOV=0xbadf1701 (no further BAR0 reads)", out)
        self.assertEqual(bar0_reads(after_last_shot(ev)), [FUSECTRL, GFW, DSOV])
        self.assertNoBar0Writes(ev)

    def test_poisoned_dsov_in_a_cell_tips(self):
        rc, out, ev = run([STD[:5] + [1, 5]], "ud")
        self.assertEqual(rc, 10, out)
        self.assertIn("step 0 shot 1 v=450 td=-60 pw=198 vr=750 f=1: poisoned DSOV=0xbadf1701", out)
        self.assertEqual(bar0_reads(after_last_shot(ev)), [FUSECTRL, GFW, DSOV])
        self.assertEqual(len([x for x in ev if x.startswith("SHOT")]), 2)

    def test_poisoned_fusectrl_keeps_the_old_string(self):
        rc, out, ev = run([STD], "x")
        self.assertEqual(rc, 10, out)
        self.assertIn("poisoned FUSECTRL=0xbadf1701 (no further BAR0 reads)", out)
        self.assertEqual(bar0_reads(after_last_shot(ev)), [FUSECTRL])

    def test_goldilocks(self):
        rc, out, ev = run([STD, [438] + STD[1:]], "ug")
        self.assertEqual(rc, 0, out)
        self.assertIn("step 1 v=438 td=-60 pw=198 vr=750 f=1: GOLDILOCKS", out)
        self.assertIn(">>> GOLDILOCKS HELD (card T) v=438 td=-60", out)
        self.assertEqual(bar0_reads(after_last_shot(ev)), [FUSECTRL, GFW, DSOV, CRC, PLM, SENT])
        self.assertNoBar0Writes(ev)
        self.assertNotIn("PARK", ev)                                            # held, not parked

    def test_bad_dsov_is_never_goldilocks(self):
        for code, text in (("f", "dsov=0xffffffff (no further BAR0 reads)"), ("t", "dsov=0x00000002 (no further BAR0 reads)")):
            with self.subTest(code=code):
                rc, out, ev = run([STD], code)
                self.assertEqual(rc, 10, out)
                self.assertNotIn("GOLDILOCKS", out)
                self.assertIn(text, out)
                self.assertEqual(bar0_reads(after_last_shot(ev)), [FUSECTRL, GFW, DSOV])
                rc, out, ev = run([STD[:5] + [1, 3]], code)
                self.assertEqual(rc, 10, out)
                self.assertNotIn("GOLDILOCKS", out)

    def test_partial_continues_and_over0_tips(self):
        rc, out, ev = run([STD[:5] + [1, 2]], "pu")
        self.assertEqual(rc, 0, out)
        self.assertIn("step 0 shot 0 v=450 td=-60 pw=198 vr=750 f=1: dsov0 crc=0x55 priv=0xfffffefe", out)
        self.assertIn("'partial': 1", out)
        rc, out, ev = run([STD], "o")
        self.assertEqual(rc, 10, out)
        self.assertIn(": poisoned/over0", out)

    def test_hidden_poison_sentinel(self):
        for code, value in (("h", "0xbadf1020"), ("a", "0xbad00100")):
            with self.subTest(code=code):
                rc, out, ev = run([STD], code)
                self.assertEqual(rc, 10, out)
                self.assertIn("hidden-poison alias 0x820410=%s" % value, out)
        rc, out, ev = run([STD], "h", SENTINEL="0")
        self.assertEqual(rc, 0, out)
        self.assertNotIn(SENT, ev)

    def test_gfw_booting_is_waited_for_or_stops(self):
        rc, out, ev = run([STD], "B")
        self.assertEqual(rc, 0, out)
        self.assertEqual(bar0_reads(after_last_shot(ev)), [FUSECTRL, GFW, FUSECTRL, GFW, DSOV, CRC, PLM, SENT])
        rc, out, ev = run([STD], "b")
        self.assertEqual(rc, 10, out)
        self.assertIn("booting GFW=0x00000301 (no further BAR0 reads)", out)
        self.assertEqual(after_last_shot(ev).count(FUSECTRL), 21)

    def test_half_boot_reread(self):
        rc, out, ev = run([STD], "q")
        self.assertEqual(rc, 0, out)
        self.assertIn("half-boot (0xBAD.. reads); re-read after 2 s", out)
        self.assertIn("GOLDILOCKS (after re-read)", out)
        self.assertEqual(bar0_reads(after_last_shot(ev)), [FUSECTRL, GFW, DSOV, CRC, PLM, FUSECTRL, GFW, DSOV, CRC, PLM, SENT])

    def test_controller_off_exits_9(self):
        rc, out, ev = run([STD], "c")
        self.assertEqual(rc, 9, out)
        self.assertIn("CONTROLLER OFF (0x2042, 41 mV)", out)
        self.assertIn("STOP: power cycle needed", out)
        self.assertIn("   rail_code=41 status 7a=10 7b=00 7c=00 7d=00 7f=00 80=ERR 78=42", out)
        self.assertFalse(bar0_reads(after_last_shot(ev)))

    def test_no_dip_stop_kept(self):
        rc, out, ev = run([STD], "u", FAKE_E1="0xc2ea")
        self.assertEqual(rc, 12, out)
        self.assertIn("NO DIP", out)

    def test_new_step_fields_reach_the_firmware(self):
        seq = [[388, 30, 8, 50, 825, 1, 1, 0, -20, 2], [400, -4000, 1, 12000, 700, 0, 1, 0, -8000],
               [400, 15, 1, 65, 825, 3, 1, 3], [450, -60, 3, 25, 750, 1, 1]]
        rc, out, ev = run(seq, "uuuu")
        self.assertEqual(rc, 0, out)
        shots = [x for x in ev if x.startswith("SHOT")]
        self.assertEqual(shots, ["SHOT u g 388 30 825 -20 0 1 0 8 50 150 117 2",
                                 "SHOT u g 400 -4000 700 -8000 0 0 0 1 12000 150 117 0",
                                 "SHOT u g 400 15 825 -20 0 3 3 1 65 150 117 0",
                                 "SHOT u g 450 -60 750 -20 0 1 0 3 25 150 117 0"])
        self.assertIn("   glitch vd=450 td=-60 vr=750 tr=-20 tv=0 fast=1 hold=0 pw=36 gap=150", out)   # resolved pw

    def test_push1_knobs_refused_on_older_firmware(self):
        old = "clk=400 MHz  nvvdd=0.0000 V  sample_ns=240.0  last reset: x  fw=lab-psr1-timing1-debug"
        for step in ([450, -60, 3, 198, 750, 3], [450, -60, 3, 198, 750, 1, 1, 2], [450, -4000, 1, 9000, 750, 0, 1, 0, -8000],
                     [450, -60, 3, 198, 750, 1, 1, 0, -20, 2]):
            with self.subTest(step=step):
                rc, out, ev = run([step], "u", FAKE_V=old)
                self.assertEqual(rc, 2, out)
                self.assertIn("STOP: the sequence uses push1 knobs", out)
                self.assertFalse([x for x in ev if x.startswith("SHOT")])
        rc, out, ev = run([STD], "u", FAKE_V=old)
        self.assertEqual(rc, 0, out)

    def test_every_step_validated_before_the_first_shot(self):
        for bad_step in ([300, -60], [450, -60, 3, 198, 900], [450, -60, 3, 198, 750, 8], [450, -60, 3, 198, 750, 1, 1, 21],
                         [450, -60, 3, 198, 750, 1, 1, 0, -500], [450, -60, 3, 198, 750, 1, 1, 0, -25000],
                         [450, -60, 3, 198, 750, 1, 1, 0, -20, 1]):
            with self.subTest(step=bad_step):
                rc, out, ev = run([STD, bad_step], "uu")
                self.assertNotEqual(rc, 0, out)
                self.assertIn("AssertionError", out)
                self.assertFalse([x for x in ev if x.startswith(("SHOT", "OPEN"))])

    def test_unlock_readback(self):
        rc, out, ev = run([STD], "u", FAKE_UNLOCK="unlock already MATCH (CML 0x08)\r\nunlock MATCH wp=00 cfg=0000")
        self.assertEqual(rc, 0, out)
        self.assertIn("WARNING: unlock readback 00 0000, expected wp=00 cfg=1229", out)
        rc, out, ev = run([STD], "u", FAKE_UNLOCK="unlock already MATCH (CML 0x08)")         # older firmware
        self.assertEqual(rc, 0, out)
        self.assertIn("WARNING: unlock readback missing", out)
        rc, out, ev = run([STD], "u", FAKE_UNLOCK="unlock CML 0x00 LOCKED\r\nunlock LOCKED wp=ERR cfg=ERR")
        self.assertEqual(rc, 2, out)
        self.assertIn("unlock failed", out)
        self.assertFalse([x for x in ev if x.startswith("SHOT")])


@unittest.skipIf(passive_observe is None, "set GA100VFI_HOST to a pico-ga100vfi host/ dir with firmware/0004 applied")
class ObserverCopyTest(unittest.TestCase):
    """The runner carries a copy of passive_snapshot.observe (python -I on the bench); both must agree."""

    def test_same_reads_and_results(self):
        with open(RUNNER) as f:
            tree = ast.parse(f.read())
        keep = [n for n in tree.body if (isinstance(n, ast.FunctionDef) and n.name == "observe") or
                (isinstance(n, ast.Assign) and any(getattr(t, "id", "") == "OBS_REGS" for t in n.targets))]
        ns = {}
        exec(compile(ast.Module(body=keep, type_ignores=[]), RUNNER, "exec"), ns)
        rng = random.Random(1)
        pool = [0, 1, 2, 0x55, 0x3FF, 0x301, 0xFF, 0xE0040000, 0xFFFFFFFF, 0xFFFFFEFE, 0xBADF1701, 0xBADF5040,
                0xBAD00100, OSError("read failed")]
        for _ in range(4000):
            values = [rng.choice(pool) for _ in range(5)]

            def reader(calls):
                def read(address):
                    calls.append(address)
                    v = values[len(calls) - 1]
                    if isinstance(v, Exception):
                        raise v
                    return v
                return read
            a_calls, b_calls = [], []
            a = passive_observe(reader(a_calls))
            b = ns["observe"](reader(b_calls))
            self.assertEqual(a_calls, b_calls, values)
            self.assertEqual(a, b, values)


if __name__ == "__main__":
    unittest.main()
