"""End-to-end tests of tools/runner/bisect_shots.py against the fake ga100vfi package in tests/fake (no serial port,
no BAR0, no park: fake/drive.py stubs subprocess and time.sleep). Also checks the shell scripts' syntax.

Run:  python3 -I tests/test_runner.py
The check that the runner's observer matches passive_snapshot.observe needs the patched host package (firmware/,
patches 0004 and 0010): GA100VFI_HOST=/path/to/pico-ga100vfi/host python3 -I tests/test_runner.py"""
import ast
import json
import os
import random
import shutil
import subprocess
import sys
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
RUNNER = os.path.join(ROOT, "tools", "runner", "bisect_shots.py")
DRIVE = os.path.join(HERE, "fake", "drive.py")
HOST = os.environ.get("GA100VFI_HOST")
passive_observe = None
if HOST:
    sys.path.insert(0, HOST)
    from passive_snapshot import observe as passive_observe  # noqa: E402

FUSECTRL, GFW, DSOV, CRC, PLM, SENT = "RD 820000", "RD 118234", "RD 820084", "RD 820070", "RD 8200fc", "RD 820410"
FULL = [FUSECTRL, GFW, DSOV, CRC, PLM, SENT]
STD = [450, -60, 3, 198, 750, 1]
CELL3 = STD + [3]


def run(seq, shots, **env):
    with tempfile.TemporaryDirectory() as tmp:
        log = os.path.join(tmp, "events")
        e = {k: v for k, v in os.environ.items() if not k.startswith(("FAKE_", "REPEAT", "SENTINEL", "FLOOR", "RETRAIN", "SBR", "PARK_EACH", "LOSTRUN", "VFI_", "BAD0_", "TRACE"))}
        e.update(FAKE_LOG=log, FAKE_SHOTS=",".join(shots), FLOOR="381", VFI_BDF="0000:05:00.0", VFI_TRACE_DIR=tmp)
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


def shots(events):
    return [x for x in events if x.startswith("SHOT")]


class RunnerTest(unittest.TestCase):
    def test_under_default_step_is_the_old_command(self):
        rc, out, ev = run([STD], "u")
        self.assertEqual(rc, 0, out)
        self.assertIn("step 0 v=450 td=-60 pw=198 vr=750 f=1: under", out)
        self.assertIn("sequence exhausted, all under", out)
        self.assertEqual(shots(ev), ["SHOT u g 450 -60 750 -20 0 1 0 3 198 150 117 0"])
        self.assertEqual(bar0_reads(after_last_shot(ev)), FULL)
        self.assertEqual(ev.count("PARK"), 1)
        self.assertNotIn("PARK WITH TTY OPEN", ev)

    def test_boot_lines_and_sentinel_baseline(self):
        rc, out, ev = run([STD], "u")
        self.assertEqual(rc, 0, out)
        self.assertIn("unlock: unlock already MATCH (CML 0x08) | unlock MATCH wp=00 cfg=1229", out)
        self.assertIn("boot pmbus (@r 25 <reg> <n>; words shown high byte first): "
                      "13=00 46=1229 7e=08 79=2c00 88=2f00 8b=02c9 8d=0170", out)
        self.assertIn("sentinel baseline 0x820410=0x00000001", out)
        first = ev.index([x for x in ev if x.startswith("SHOT")][0])
        self.assertEqual(bar0_reads(ev[:first]), FULL)             # the pre-shot baseline read
        for line in ("   glitch vd=450 td=-60 vr=750", "   prime_at=0", "   release_at=0 scheduled=0 via=alarm"):
            self.assertIn(line, out)

    def test_sentinel_baseline_gates(self):
        rc, out, ev = run([STD], "u", FAKE_BASE="z")
        self.assertEqual(rc, 9, out)
        self.assertIn("STOP: unsafe pre-shot fuse baseline", out)
        self.assertFalse(shots(ev))
        rc, out, ev = run([STD], "u", SENTINEL_EXPECT="0x1")
        self.assertEqual(rc, 0, out)
        self.assertIn("sentinel baseline 0x820410=0x00000001 (expected 0x1)", out)
        rc, out, ev = run([STD], "u", SENTINEL_EXPECT="0x2")
        self.assertEqual(rc, 9, out)
        self.assertIn("this card's healthy value is 0x2", out)
        self.assertFalse(shots(ev))

    def test_hidden_poison_sentinel(self):
        for code, value in (("h", "0xbadf1020"), ("a", "0xbad00100"), ("z", "0x00000000"), ("w", "0xffffffff"),
                            ("m", "0x00000002")):
            with self.subTest(code=code):
                rc, out, ev = run([STD], code)
                self.assertEqual(rc, 10, out)
                self.assertIn("hidden-poison alias 0x820410=%s" % value, out)
        rc, out, ev = run([STD], "g", FAKE_BASE="u")
        self.assertIn("GOLDILOCKS HELD", out)                      # control: a clean sentinel still holds
        rc, out, ev = run([STD], "h", SENTINEL="0")
        self.assertEqual(rc, 0, out)
        self.assertNotIn(SENT, ev)

    def test_lost_then_park_continues_the_cell(self):
        rc, out, ev = run([CELL3], "lu")
        self.assertEqual(rc, 0, out)
        self.assertIn("park after lost shot: park: rail held 787 mV", out)
        self.assertIn("cell 0 v=450 td=-60 np=3 pw=198 vr=750: {'under': 2, 'lost': 1, 'partial': 0, 'other': 0}", out)
        self.assertEqual(ev.count("PARK"), 2)
        self.assertNotIn("PARK WITH TTY OPEN", ev)

    def test_lostrun_1_parks_once(self):
        rc, out, ev = run([CELL3], "l", LOSTRUN=1)
        self.assertEqual(rc, 10, out)
        self.assertIn("1 lost in a row", out)
        self.assertEqual(ev.count("PARK"), 1)

    def test_lost_in_single_shot_mode_tips_and_parks(self):
        rc, out, ev = run([STD], "l")
        self.assertEqual(rc, 10, out)
        self.assertIn("step 0 v=450 td=-60 pw=198 vr=750 f=1: lost", out)
        self.assertEqual(ev.count("PARK"), 1)
        self.assertNotIn("PARK WITH TTY OPEN", ev)

    def test_failed_park_logs_root_port_and_stops_before_reopening(self):
        rc, out, ev = run([CELL3], "l", FAKE_PARK_RC=4)
        self.assertEqual(rc, 10, out)
        self.assertIn("park after lost shot FAILED (rc 4)", out)
        self.assertIn("after the failed park (lspci -vv, read-only): LnkCtl:", out)
        self.assertTrue([x for x in ev if x.startswith("LSPCI")])
        park = ev.index("PARK")
        self.assertNotIn("OPEN /dev/ttyACM0", ev[park:])               # no reopen or unlock after a failed park

    def test_park_each_closes_the_tty(self):
        rc, out, ev = run([STD, [444] + STD[1:]], "uu", PARK_EACH=1)
        self.assertEqual(rc, 0, out)
        self.assertEqual(ev.count("PARK"), 2)
        self.assertNotIn("PARK WITH TTY OPEN", ev)

    def test_poisoned_dsov_stops_reads(self):
        rc, out, ev = run([STD], "d")
        self.assertEqual(rc, 10, out)
        self.assertIn("poisoned DSOV=0xbadf1701 (no further BAR0 reads)", out)
        self.assertEqual(bar0_reads(after_last_shot(ev)), [FUSECTRL, GFW, DSOV])

    def test_bad0_read_stops_reads_without_a_reread(self):
        rc, out, ev = run([STD], "q")
        self.assertEqual(rc, 10, out)
        self.assertIn("poisoned CRC=0xbad00100 (no further BAR0 reads)", out)
        self.assertEqual(bar0_reads(after_last_shot(ev)), [FUSECTRL, GFW, DSOV, CRC])
        self.assertNotIn("re-read", out)

    def test_poisoned_fusectrl_and_nonidle_fusectrl(self):
        rc, out, ev = run([STD], "x")
        self.assertEqual(rc, 10, out)
        self.assertIn("poisoned FUSECTRL=0xbadf1701 (no further BAR0 reads)", out)
        self.assertEqual(bar0_reads(after_last_shot(ev)), [FUSECTRL])
        rc, out, ev = run([STD], "i")
        self.assertEqual(rc, 10, out)
        self.assertIn("FUSECTRL: unexpected non-idle value 0xe0040001", out)
        self.assertEqual(bar0_reads(after_last_shot(ev)), [FUSECTRL])

    def test_goldilocks_prints_the_hold_marker(self):
        rc, out, ev = run([STD, [438] + STD[1:]], "ug")
        self.assertEqual(rc, 0, out)
        self.assertIn("step 1 v=438 td=-60 pw=198 vr=750 f=1: GOLDILOCKS passive candidate", out)
        self.assertIn(">>> GOLDILOCKS HELD (card T) v=438 td=-60", out)
        self.assertEqual(bar0_reads(after_last_shot(ev)), FULL)
        self.assertNotIn("PARK", ev)
        self.assertFalse([x for x in ev if x.startswith("WR")])

    def test_bad_dsov_is_never_goldilocks(self):
        for code, text in (("f", "dsov=0xffffffff (no further BAR0 reads)"), ("t", "dsov=0x00000002 (no further BAR0 reads)")):
            with self.subTest(code=code):
                rc, out, ev = run([STD], code)
                self.assertEqual(rc, 10, out)
                self.assertNotIn("GOLDILOCKS", out)
                self.assertIn(text, out)
                self.assertEqual(bar0_reads(after_last_shot(ev)), [FUSECTRL, GFW, DSOV])

    def test_partial_is_held_in_cell_mode_and_over0_tips(self):
        rc, out, ev = run([CELL3], "pu")
        self.assertEqual(rc, 13, out)
        self.assertIn(">>> PARTIAL HELD (card T); no sense or next shot", out)
        self.assertEqual(len(shots(ev)), 1)
        self.assertNotIn("PARK", ev)
        rc, out, ev = run([STD], "o")
        self.assertEqual(rc, 10, out)
        self.assertIn(": poisoned/over0", out)

    def test_gfw_booting_waits_or_stops_without_a_park(self):
        rc, out, ev = run([STD], "B")
        self.assertEqual(rc, 0, out)
        self.assertEqual(bar0_reads(after_last_shot(ev)), [FUSECTRL, GFW] + FULL)
        rc, out, ev = run([STD], "b")
        self.assertEqual(rc, 10, out)
        self.assertIn("booting GFW=0x00000301", out)
        self.assertIn("no park: GFW is still booting", out)
        self.assertNotIn("PARK", ev)

    def test_controller_off_exits_9(self):
        rc, out, ev = run([STD], "c")
        self.assertEqual(rc, 9, out)
        self.assertIn("CONTROLLER OFF (0x2042, 41 mV)", out)
        self.assertIn("   rail_code=41 status 7a=10", out)
        self.assertFalse(bar0_reads(after_last_shot(ev)))

    def test_no_dip_and_missing_pwd(self):
        rc, out, ev = run([STD], "u", FAKE_E1="0xc2ea")
        self.assertEqual(rc, 12, out)
        self.assertIn("NO DIP", out)
        rc, out, ev = run([STD], "D")
        self.assertEqual(rc, 12, out)
        self.assertIn("NO DIP", out)

    def test_mistimed_is_never_under_or_goldilocks(self):
        for code, why in (("L", "release_at=9 us"), ("F", "release via fallback"), ("N", "0 release_at lines"),
                          ("W", "a PMBus write overlaps the release")):
            with self.subTest(code=code):
                rc, out, ev = run([STD, [444] + STD[1:]], code + "u")
                self.assertEqual(rc, 14, out)
                self.assertIn("MISTIMED at step 0 shot 0", out)
                self.assertIn(why, out)
                self.assertIn("observed under", out)
                self.assertNotIn(": under", out)
                self.assertEqual(len(shots(ev)), 1)
                self.assertEqual(ev.count("PARK"), 1)
        rc, out, ev = run([STD], "G")
        self.assertEqual(rc, 14, out)
        self.assertNotIn("GOLDILOCKS HELD", out)
        self.assertIn("observed GOLDILOCKS", out)
        self.assertNotIn("PARK", ev)
        rc, out, ev = run([CELL3], "Lu")
        self.assertEqual(rc, 14, out)
        self.assertEqual(len(shots(ev)), 1)

    def test_mistimed_poison_and_loss_still_show(self):
        rc, out, ev = run([STD], "K")
        self.assertEqual(rc, 14, out)
        self.assertIn("step 0 shot 0 v=450 td=-60 pw=198 vr=750 f=1: lost", out)
        self.assertIn("outcome lost", out)
        self.assertEqual(ev.count("PARK"), 1)
        rc, out, ev = run([STD], "X")
        self.assertEqual(rc, 14, out)
        self.assertIn("poisoned DSOV=0xbadf1701", out)
        self.assertEqual(bar0_reads(after_last_shot(ev)), [FUSECTRL, GFW, DSOV])
        rc, out, ev = run([STD], "P")
        self.assertEqual(rc, 13, out)
        self.assertIn("MISTIMED: release_at=9 us", out)
        self.assertIn(">>> PARTIAL HELD", out)

    def test_nack_abort_and_write_failures_stop_before_another_shot(self):
        rc, out, ev = run([STD, [444] + STD[1:]], "nu")
        self.assertEqual(rc, 11, out)
        self.assertIn("STOP: shot execution is unknown", out)
        self.assertEqual(len(shots(ev)), 1)
        rc, out, ev = run([STD, [444] + STD[1:]], "Au")
        self.assertEqual(rc, 2, out)
        self.assertIn("STOP: firmware refused step 0, no shot fired: glitch ABORT", out)
        self.assertEqual(len(shots(ev)), 1)
        rc, out, ev = run([STD, [444] + STD[1:]], "Eu")
        self.assertEqual(rc, 11, out)
        self.assertIn("STOP: PMBus/setup writes failed", out)
        self.assertEqual(len(shots(ev)), 1)

    def test_bad_unlock_readback_stops(self):
        for reply in ("unlock already MATCH (CML 0x08)\r\nunlock MATCH wp=00 cfg=0000",
                      "unlock already MATCH (CML 0x08)",
                      "unlock already MATCH (CML 0x08)\r\nunlock MATCH wp=00 cfg=1229 write=ERR",
                      "unlock CML 0x00 LOCKED\r\nunlock LOCKED wp=ERR cfg=ERR"):
            with self.subTest(reply=reply):
                rc, out, ev = run([STD], "u", FAKE_UNLOCK=reply)
                self.assertEqual(rc, 2, out)
                self.assertIn("STOP: unlock/config readback failed", out)
                self.assertFalse(shots(ev))

    def test_retrain_and_sbr_are_startup_errors(self):
        for var in ("RETRAIN", "SBR"):
            with self.subTest(var=var):
                rc, out, ev = run([STD], "u", **{var: "1"})
                self.assertEqual(rc, 2, out)
                self.assertIn("STOP: RETRAIN and SBR are not allowed", out)
                self.assertFalse([x for x in ev if x.startswith(("OPEN", "SHOT"))])

    def test_firmware_marker(self):
        rc, out, ev = run([STD], "u", FAKE_V="clk=400 MHz  last reset: x  fw=lab-psr1-timing1-debug")
        self.assertEqual(rc, 2, out)
        self.assertFalse(shots(ev))
        rc, out, ev = run([STD], "u", FAKE_V="clk=400 MHz  last reset: x  fw=lab-psr1-push1-debug")
        self.assertEqual(rc, 0, out)

    def test_new_step_fields_reach_the_firmware(self):
        seq = [[388, -500, 8, 50, 825, 1, 1, 0, -20, 2], [400, -4000, 1, 12000, 700, 0, 1, 0, -8000],
               [400, 0, 1, 65, 825, 3, 1, 3], [450, -60, 3, 25, 750, 1, 1]]
        rc, out, ev = run(seq, "uuuu")
        self.assertEqual(rc, 0, out)
        self.assertEqual(shots(ev), ["SHOT u g 388 -500 825 -20 0 1 0 8 50 150 117 2",
                                     "SHOT u g 400 -4000 700 -8000 0 0 0 1 12000 150 117 0",
                                     "SHOT u g 400 0 825 -20 0 3 3 1 65 150 117 0",
                                     "SHOT u g 450 -60 750 -20 0 1 0 3 25 150 117 0"])

    def test_every_step_validated_before_the_first_shot(self):
        for bad_step in ([300, -60], [450, -60, 3, 198, 900], [450, -60, 3, 198, 750, 8], [450, -60, 3, 198, 750, 1, 1, 21],
                         [450, -60, 3, 198, 750, 1, 1, 0, -500], [450, -60, 3, 198, 750, 1, 1, 0, -20, 1]):
            with self.subTest(step=bad_step):
                rc, out, ev = run([STD, bad_step], "uu")
                self.assertNotEqual(rc, 0, out)
                self.assertIn("AssertionError", out)
                self.assertFalse([x for x in ev if x.startswith(("SHOT", "OPEN"))])


    def test_delay_and_gap_step_fields(self):
        seq = [[338, -60, 3, 120, 750, 1, 1, 0, -20, 0, 300], [331, -60, 3, 198, 588, 0, 1, 0, -20, 0, 400, 220]]
        rc, out, ev = run(seq, "uu", FLOOR=325)
        self.assertEqual(rc, 0, out)
        self.assertEqual(shots(ev), ["SHOT u g 338 -60 750 -20 0 1 0 3 120 150 300 0",
                                     "SHOT u g 331 -60 588 -20 0 0 0 3 198 220 400 0"])

    def test_delay_and_gap_are_validated_before_the_first_shot(self):
        for bad_step in ([450, -60, 3, 198, 750, 1, 1, 0, -20, 0, 901], [450, -60, 3, 198, 750, 1, 1, 0, -20, 0, -1],
                         [450, -60, 3, 198, 750, 1, 1, 0, -20, 0, 117, 35], [450, -60, 3, 198, 750, 1, 1, 0, -20, 0, 117, 2001]):
            with self.subTest(step=bad_step):
                rc, out, ev = run([STD, bad_step], "uu")
                self.assertNotEqual(rc, 0, out)
                self.assertIn("AssertionError", out)
                self.assertFalse([x for x in ev if x.startswith(("SHOT", "OPEN"))])

    def test_bad0_followup_is_log_only(self):
        rc, out, ev = run([STD], "y")
        self.assertEqual(rc, 10, out)
        self.assertIn("poisoned FUSECTRL=0xbad00100 (no further BAR0 reads)", out)
        self.assertNotIn("bad0 follow-up", out)
        self.assertEqual(bar0_reads(after_last_shot(ev)), [FUSECTRL])
        rc, out, ev = run([STD], "y", BAD0_FOLLOWUP=1)
        self.assertEqual(rc, 10, out)                                 # the outcome is unchanged
        self.assertIn("bad0 follow-up (log only, 3 s): FUSECTRL=0xe0040000", out)
        self.assertIn("bad0 follow-up (log only): FUSECTRL=0xe0040000", out)
        self.assertIn("step 0 v=450 td=-60 pw=198 vr=750 f=1: poisoned FUSECTRL=0xbad00100", out)
        self.assertEqual(bar0_reads(after_last_shot(ev)), [FUSECTRL, FUSECTRL] + FULL)
        self.assertFalse([x for x in ev if x.startswith("WR")])
        rc, out, ev = run([STD], "Y", BAD0_FOLLOWUP=1)
        self.assertEqual(rc, 10, out)
        self.assertIn("bad0 follow-up (log only, 3 s): FUSECTRL=0xbad00100", out)
        self.assertNotIn("bad0 follow-up (log only): ", out)
        self.assertEqual(bar0_reads(after_last_shot(ev)), [FUSECTRL, FUSECTRL])

    def test_usage_and_required_bdf(self):
        with tempfile.TemporaryDirectory() as tmp:
            env = {"PATH": os.environ.get("PATH", ""), "FAKE_LOG": os.path.join(tmp, "events")}
            p = subprocess.run([sys.executable, "-I", DRIVE, RUNNER], capture_output=True, text=True, timeout=30, env=env)
            self.assertEqual(p.returncode, 2, p.stdout + p.stderr)
            self.assertIn("Usage", p.stdout)
            p = subprocess.run([sys.executable, "-I", DRIVE, RUNNER, "card T", "[]"], capture_output=True, text=True,
                               timeout=30, env=env)
            self.assertNotEqual(p.returncode, 0)
            self.assertIn("set VFI_BDF", p.stdout + p.stderr)
            self.assertFalse(os.path.exists(env["FAKE_LOG"]))            # nothing was opened


@unittest.skipUnless(passive_observe, "set GA100VFI_HOST to the patched pico-ga100vfi host directory")
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
        pool = [0, 1, 2, 0x55, 0x3FF, 0x301, 0x302, 0xFF, 0xE0040000, 0xE0040001, 0xFFFFFFFF, 0xFFFFFEFE,
                0xBADF1701, 0xBADF5040, 0xBAD00100, OSError("read failed")]
        for _ in range(6000):
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


class ShellTest(unittest.TestCase):
    def test_syntax(self):
        d = os.path.join(ROOT, "tools", "runner")
        for f in ("bootrun.sh", "firstshot.sh", "campaign.sh", "cycle.sh", "power.sh", "bench.conf.example"):
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
    unittest.main()
