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
        e = {k: v for k, v in os.environ.items() if not k.startswith(("FAKE_", "REPEAT", "SENTINEL", "SENT0_", "FLOOR", "RETRAIN", "SBR", "PARK_EACH", "LOSTRUN", "VFI_", "BAD0_", "TRACE",
                                                                     "LOST_NOPARK", "PARTIAL_CONTINUE", "SHOOT_THROUGH", "STALLRUN", "GFW_WAIT", "CELLLOST", "SHOTCAP"))}
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


def shot_lines(out):
    """The runner's SHOT lines as dicts (fields before ' | ') plus the outcome text."""
    res = []
    for line in out.splitlines():
        if line.startswith("SHOT "):
            head, _, text = line[5:].partition(" | ")
            d = dict(x.split("=", 1) for x in head.split())
            d["out"] = text
            res.append(d)
    return res


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

    def test_tv_field_reaches_the_firmware(self):
        # 13th field: t_revert_us (tv), when the stock VOUT is written after the train (docs/OVERLAP_EDGE.md)
        rc, out, ev = run([[381, -60, 1, 137, 625, 1, 1, 0, -20, 0, 117, 150, 171], [450, -60, 3, 198, 750, 1, 1]], "uu",
                          FLOOR="338")
        self.assertEqual(rc, 0, out)
        self.assertEqual(shots(ev), ["SHOT u g 381 -60 625 -20 171 1 0 1 137 150 117 0",
                                     "SHOT u g 450 -60 750 -20 0 1 0 3 198 150 117 0"])
        self.assertIn(" tv=171 kind=", out)
        self.assertIn(" tv=0 kind=", out)
        rc, out, ev = run([STD, [450, -60, 3, 198, 750, 1, 1, 0, -20, 0, 117, 150, 20001]], "uu")
        self.assertNotEqual(rc, 0, out)
        self.assertIn("AssertionError", out)
        self.assertFalse(shots(ev))

    def test_push3_marker_accepted(self):
        rc, out, ev = run([STD], "u", FAKE_V="clk=400 MHz  last reset: x  fw=lab-psr1-push3-debug")
        self.assertEqual(rc, 0, out)
        rc, out, ev = run([STD], "u", FAKE_V="clk=400 MHz  last reset: x  fw=lab-psr1-push4-debug")
        self.assertEqual(rc, 2, out)

    def test_sent0_continue_is_opt_in(self):
        rc, out, ev = run([CELL3], "zuu")
        self.assertEqual(rc, 10, out)                                   # default: sentinel 0 with DSOV 1 = hidden poison
        self.assertIn("hidden-poison alias 0x820410=0x00000000", out)
        rc, out, ev = run([CELL3], "zuu", SENT0_CONTINUE="1")
        self.assertEqual(rc, 0, out)                                    # opt-in: logged, the cell goes on
        self.assertIn("SENT0 frame: DSOV=0x00000001 CRC=0x00000055", out)
        self.assertEqual(len(shots(ev)), 3)
        rc, out, ev = run([CELL3], "huu", SENT0_CONTINUE="1")
        self.assertEqual(rc, 10, out)                                   # an error value still poisons
        self.assertIn("hidden-poison alias 0x820410=0xbadf1020", out)

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


STALL_READ = [FUSECTRL, GFW, DSOV, CRC, PLM, SENT]


class RunnerStallAndRecoveryTest(unittest.TestCase):
    """The paths ported from the lab's runner of 2026-11-20: stall reads, compass frames, shoot-through stalls,
    the sentinel rule for DSOV 0, LOST_NOPARK recovery and the per-shot SHOT lines."""

    def test_sentinel_zero_with_dsov0_is_a_goldilocks(self):
        rc, out, ev = run([STD], "e")
        self.assertEqual(rc, 0, out)
        self.assertIn(">>> GOLDILOCKS HELD (card T) v=450 td=-60", out)
        self.assertNotIn("hidden-poison", out)
        self.assertNotIn("PARK", ev)
        rc, out, ev = run([STD], "z")                                     # DSOV 1 with sentinel 0 stays poison
        self.assertEqual(rc, 10, out)
        self.assertIn("hidden-poison alias 0x820410=0x00000000", out)

    def test_stall_read_is_read_only_and_ends_the_boot_by_default(self):
        rc, out, ev = run([CELL3], "s")
        self.assertEqual(rc, 10, out)
        self.assertIn("stall read (GFW still booting after 5 s; reads only, no g/r/j): FUSECTRL=0xe0040000 "
                      "GFW=0x00000000 DSOV=0x00000001 CRC=0x00000075 PLM=0xffffffff SENTINEL=0x00000000", out)
        self.assertIn("no park: GFW is still booting", out)
        self.assertNotIn("PARK", ev)
        self.assertEqual(len(shots(ev)), 1)
        reads = bar0_reads(after_last_shot(ev))
        self.assertEqual(reads[-6:], STALL_READ)
        self.assertEqual(reads[:-6], [FUSECTRL, GFW] * 21)
        self.assertFalse([x for x in ev if x.startswith("WR")])
        sl = shot_lines(out)
        self.assertEqual((sl[0]["kind"], sl[0]["stall"], sl[0]["CRC"], sl[0]["SENTINEL"]),
                         ("stall", "1", "0x00000075", "0x00000000"))
        self.assertNotIn("hidden-poison", out)                            # sentinel 0 in a stall is logged, not scored

    def test_gfw_wait_sets_the_poll(self):
        rc, out, ev = run([STD], "s", GFW_WAIT=1)
        self.assertEqual(rc, 10, out)
        self.assertIn("GFW still booting after 1 s", out)
        self.assertEqual(bar0_reads(after_last_shot(ev))[:-6], [FUSECTRL, GFW] * 5)

    def test_shoot_through_stalls_is_opt_in_with_a_warning_and_a_cap(self):
        seq = [STD + [3]]
        rc, out, ev = run(seq, "ssu", SHOOT_THROUGH_STALLS=1)
        self.assertEqual(rc, 0, out)
        self.assertIn("WARNING: SHOOT_THROUGH_STALLS=1", out)
        self.assertIn("rule 2 of upstream pico-ga100vfi", out)
        self.assertIn("{'under': 1, 'lost': 0, 'partial': 0, 'other': 0, 'stall': 2}", out)
        self.assertEqual(len(shots(ev)), 3)
        rc, out, ev = run(seq, "sss", SHOOT_THROUGH_STALLS=1, STALLRUN=1)
        self.assertEqual(rc, 10, out)
        self.assertIn("2 GFW stalls in a row", out)
        self.assertEqual(len(shots(ev)), 2)
        self.assertNotIn("PARK", ev)
        rc, out, ev = run([STD + [2]], "us", SHOOT_THROUGH_STALLS=1)       # a stall as the last shot: no final park
        self.assertEqual(rc, 0, out)
        self.assertIn("no park: GFW is still booting after the last shot", out)
        self.assertNotIn("PARK", ev)
        rc, out, ev = run([STD], "s", SHOOT_THROUGH_STALLS=1)              # single mode still stops at a stall
        self.assertEqual(rc, 10, out)

    def test_dsov0_in_a_stall_is_held_unless_continuing_is_asked_for(self):
        rc, out, ev = run([CELL3], "S")
        self.assertEqual(rc, 15, out)
        self.assertIn(">>> STALL DSOV0 HELD (card T): DSOV 0 while GFW is stalled", out)
        self.assertNotIn("PARK", ev)
        self.assertEqual(shot_lines(out)[0]["kind"], "partial")
        rc, out, ev = run([CELL3], "Suu", SHOOT_THROUGH_STALLS=1)          # shoot-through alone does not continue
        self.assertEqual(rc, 15, out)
        rc, out, ev = run([CELL3], "Suu", SHOOT_THROUGH_STALLS=1, PARTIAL_CONTINUE=1)
        self.assertEqual(rc, 0, out)
        self.assertIn("STALL PARTIAL FUSECTRL=0xe0040000 GFW=0x00000000 DSOV=0x00000000 CRC=0x00000077", out)
        self.assertIn("'partial': 1", out)
        self.assertEqual(len(shots(ev)), 3)

    def test_goldilocks_signature_in_a_stall_is_always_held_without_the_hold_hook_line(self):
        rc, out, ev = run([CELL3], "Hu", SHOOT_THROUGH_STALLS=1, PARTIAL_CONTINUE=1)
        self.assertEqual(rc, 15, out)
        self.assertIn("a goldilocks signature (DSOV 0, CRC 0x55, full mask) while GFW is stalled", out)
        self.assertNotIn(">>> GOLDILOCKS HELD", out)                      # bootrun's ON_HOLD hook must not fire
        self.assertEqual((shot_lines(out)[0]["kind"], shot_lines(out)[0]["stall"]), ("goldilocks", "1"))
        self.assertEqual(len(shots(ev)), 1)

    def test_poison_in_a_stall_read_ends_the_boot(self):
        rc, out, ev = run([CELL3], "ju", GFW_WAIT=0.5, SHOOT_THROUGH_STALLS=1)
        self.assertEqual(rc, 10, out)
        self.assertIn("poisoned FUSECTRL=0xbadf510e (stall read)", out)
        self.assertEqual(bar0_reads(after_last_shot(ev)), [FUSECTRL, GFW] * 3 + [FUSECTRL])
        self.assertEqual(len(shots(ev)), 1)
        self.assertNotIn("PARK", ev)
        self.assertEqual(shot_lines(out)[0]["kind"], "poison")

    def test_compass_frames_are_logged_and_the_cell_goes_on(self):
        rc, out, ev = run([CELL3], "kuu")
        self.assertEqual(rc, 0, out)
        self.assertIn("step 0 shot 0 v=450 td=-60 pw=198 vr=750 f=1: dsov1 crc=0x57 priv=0xffffffff", out)
        self.assertIn("{'under': 2, 'lost': 0, 'partial': 0, 'other': 0, 'compass': 1}", out)
        self.assertEqual(shot_lines(out)[0]["kind"], "compass")
        rc, out, ev = run([STD], "k")                                     # single mode stops at it
        self.assertEqual(rc, 10, out)

    def test_lost_nopark_sends_one_reset_pulse_and_needs_a_healthy_read(self):
        rc, out, ev = run([CELL3], "vuu", LOST_NOPARK=1)
        self.assertEqual(rc, 0, out)
        self.assertIn("recovery reset after lost shot: healthy_locked", out)
        self.assertIn("{'under': 2, 'lost': 1, 'partial': 0, 'other': 0}", out)
        self.assertEqual(ev.count("SER r"), 1)
        self.assertEqual(ev.count("PARK"), 1)                              # only the end-of-run park
        r = ev.index("SER r")
        self.assertEqual(bar0_reads(ev[r:ev.index([x for x in ev if x.startswith("SHOT")][1])]), FULL)
        for code, why in (("l", "link down"), ("V", "poisoned")):
            with self.subTest(code=code):
                rc, out, ev = run([CELL3], code + "uu", LOST_NOPARK=1)
                self.assertEqual(rc, 10, out)
                self.assertIn("lost and not recovered by a plain reset (%s)" % why, out)
                self.assertNotIn("PARK", ev)
                self.assertEqual(len(shots(ev)), 1)
        rc, out, ev = run([CELL3], "vuu", LOST_NOPARK=1, FAKE_R_REPLY="PEX_RST_BUF* low 100 ms (PCIe reset)")
        self.assertEqual(rc, 10, out)
        self.assertIn("no 'released' reply to r", out)
        rc, out, ev = run([CELL3], "vvv", LOST_NOPARK=1, LOSTRUN=2)        # LOSTRUN still ends the run
        self.assertEqual(rc, 10, out)
        self.assertIn("2 lost in a row", out)

    def test_shot_lines_carry_the_geometry_and_mark_the_first_shot(self):
        seq = [[381, 62, 1, 198, 750, 0, 2, 0, -20, 0, 117, 150], [375, 60, 3, 120, 700, 1, 1, 0, -20, 0, 300, 100]]
        rc, out, ev = run(seq, "uku", FLOOR=375)
        self.assertEqual(rc, 0, out)
        sl = shot_lines(out)
        self.assertEqual(len(sl), 3)
        self.assertEqual([(d["n"], d["step"], d["first"], d["kind"]) for d in sl],
                         [("0", "0", "1", "under"), ("1", "0", "0", "compass"), ("2", "1", "0", "under")])
        self.assertEqual((sl[0]["v"], sl[0]["td"], sl[0]["np"], sl[0]["pw"], sl[0]["fast"], sl[0]["delay"], sl[0]["s1"]),
                         ("381", "62", "1", "198", "0", "117", "-55"))
        self.assertEqual((sl[2]["np"], sl[2]["pw"], sl[2]["vr"], sl[2]["gap"], sl[2]["s1"]), ("3", "120", "700", "100", "-240"))
        self.assertEqual(sl[0]["DSOV"], "0x00000001")
        self.assertEqual(sl[1]["out"], "dsov1 crc=0x57 priv=0xffffffff")
        self.assertIn("note: shot 0 of this run starts from the GPU's own rail control", out)
        rc, out, ev = run([CELL3], "lu")                                   # a parked lost shot marks the next one
        self.assertEqual([d["after_park"] for d in shot_lines(out)], ["0", "1", "0"])

    def test_shot_lines_for_shots_that_end_the_boot(self):
        for codes, env, kind in (("c", {}, "latch"), ("u", {"FAKE_E1": "0xc2ea"}, "nodip"), ("n", {}, "unknown"),
                                 ("x", {}, "poison"), ("h", {}, "poison"), ("l", {}, "lost"), ("p", {}, "partial"),
                                 ("L", {}, "mistimed"), ("g", {}, "goldilocks")):
            with self.subTest(codes=codes):
                rc, out, ev = run([STD], codes, **env)
                sl = shot_lines(out)
                self.assertEqual(len(sl), 1, out)
                self.assertEqual(sl[0]["kind"], kind)
        rc, out, ev = run([STD], "A")                                      # refused: nothing fired, no SHOT line
        self.assertEqual(shot_lines(out), [])

    def test_planner_parser_reads_the_new_output(self):
        sys.path.insert(0, os.path.join(ROOT, "tools", "planner"))
        from planner_auto import parse as P
        seq = [STD + [3], [444] + STD[1:] + [2]]
        rc, out, ev = run(seq, "ukuuu", REPEAT=2)
        self.assertEqual(rc, 0, out)
        recs, rep = P.parse_text(out, card="t", seq=seq)
        self.assertEqual(len(recs), len(shot_lines(out)))


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
