"""Offline tests of tools/hunter/goldilocks_hunt.py: every emitted step against an independent model of push2's
release rule, the tally on runner output from the fake bench (tests/fake) and on older log lines, the next-phase
suggestions, and the `run` driver against a fake bootrun.sh. No serial port, no BAR0, no ssh, no power control.

Run:  python3 -I tests/test_hunter.py"""
import contextlib
import io
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
HUNTER = os.path.join(ROOT, "tools", "hunter", "goldilocks_hunt.py")
sys.path.insert(0, os.path.join(ROOT, "tools", "hunter"))
sys.path.insert(0, HERE)
import goldilocks_hunt as GH  # noqa: E402
from test_runner import run as run_runner, shot_lines  # noqa: E402

W = 47


def ref_legal(step):
    """An independent model of push2's shot timeline for runner steps with hold 0, revert 0 and t_restore -20: setup
    writes at -1300, -1200 and -1000, dip k at td - delay + k (pw + gap), its restore pw later, then the return to
    stock and VMIN after the train. Writes queue behind each other, 47 us each; a write that starts at or before the
    release (t = 0) and is still running at t = 0 is refused. Returns (legal, first dip write start)."""
    v, td, np_, pw, vr, fast, reps, hold, tr, rev, delay, gap = step
    pw, gap = max(pw, 36), max(gap, 36)
    ws = [-1300, -1200, -1000]
    t, te = td, 0
    for _ in range(np_):
        ws += [t - delay, t + pw - delay]
        te = t + pw - delay
        t += pw + gap
    post = te + gap if te > 0 else 0
    ws += [post + 10, post + 60]
    free = -10 ** 9
    for x in sorted(ws):
        st = max(x, free)
        if st <= 0 < st + W:
            return False, td - delay
        free = st + W
    return min(ws[3:]) >= -1000, td - delay


def hunt(*args):
    """Run the hunter's main() in-process; returns (exit code, stdout)."""
    out = io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(io.StringIO()):
        try:
            rc = GH.main(list(args))
        except SystemExit as e:
            rc = e.code if isinstance(e.code, int) else 1
    return rc, out.getvalue()


def plan_of(*args):
    rc, out = hunt(*(list(args) + ["--json"]))
    assert rc == 0, out
    return json.loads(out)


def all_steps(plan):
    return [st for c in plan["columns"] for st in c["steps"]] + ([plan["warmup"]] if plan.get("warmup") else [])


class LegalityTest(unittest.TestCase):
    def assert_all_legal(self, plan):
        steps = all_steps(plan)
        self.assertTrue(steps)
        for st in steps:
            ok, s1 = ref_legal(st)
            self.assertTrue(ok, st)
            self.assertFalse(-W < s1 <= 0, st)
            self.assertEqual(len(st), 12)
            self.assertEqual(GH.problems(st, plan["env"]["FLOOR"]), [], st)
            self.assertGreaterEqual(st[0], plan["env"]["FLOOR"])

    def test_every_phase_with_defaults(self):
        for args in (["train"], ["readmap", "--v", "375"], ["init"], ["slow", "--v", "375"],
                     ["dwell", "--v", "375", "--s1", "-55", "--fast", "0", "--reps", "2", "--boots", "3"],
                     ["dwell", "--v", "369", "--s1", "-177", "--np", "3"]):
            with self.subTest(args=args):
                self.assert_all_legal(plan_of(*args))

    def test_phase_geometry(self):
        p = plan_of("train")
        self.assertEqual({tuple(st[1:6]) + tuple(st[10:]) for st in all_steps(p)}, {(-60, 3, 198, 750, 1, 117, 150)})
        self.assertEqual([st[0] for st in all_steps(p)], [400, 394, 388, 381, 375, 369, 362, 356, 350])
        p = plan_of("init")
        s1s = sorted({GH.s1_of(st) for st in all_steps(p)}, reverse=True)
        self.assertEqual(s1s[0], -47)                           # the earliest legal: the write ends at the release
        self.assertTrue(all(st[2] == 1 and st[5] == 1 for st in all_steps(p)))
        self.assertEqual(p["on_poison"], "next_column")
        self.assertEqual([c["steps"][0][0] for c in p["columns"]], [450, 425, 400, 388, 381, 375])
        p = plan_of("slow", "--v", "375")
        self.assertTrue(all(st[5] == 0 for st in all_steps(p)))
        p = plan_of("readmap", "--v", "375")
        self.assertEqual(sorted(GH.s1_of(st) for st in all_steps(p))[:2], [40, 45])
        self.assertEqual(max(GH.s1_of(st) for st in all_steps(p)), 160)
        p = plan_of("dwell", "--v", "375", "--s1", "-55", "--fast", "0", "--reps", "2")
        self.assertEqual(all_steps(p), [[375, 62, 1, 198, 750, 0, 2, 0, -20, 0, 117, 150]])   # card H's hit

    def test_random_scans_never_emit_an_illegal_step(self):
        rng = random.Random(7)
        for _ in range(60):
            a, b = rng.randint(-200, 200), rng.randint(-200, 200)
            step = rng.randint(1, 9)
            for phase in ("readmap", "slow"):
                rc, out = hunt(phase, "--v", "375", "--s1-from", str(a), "--s1-to", str(b), "--s1-step", str(step),
                               "--pw", str(rng.choice([47, 80, 120, 198, 220])), "--json")
                if rc != 0:
                    continue                                    # nothing legal in that range
                self.assert_all_legal(json.loads(out))

    def test_problems_agree_with_the_reference_model(self):
        rng = random.Random(3)
        for _ in range(3000):
            st = [375, rng.randint(-400, 400), rng.randint(1, 4), rng.choice([47, 60, 80, 120, 198, 220]), 750,
                  rng.randint(0, 1), 1, 0, -20, 0, rng.randint(0, 400), rng.choice([47, 100, 150, 220])]
            self.assertEqual(GH.problems(st, 338) == [], ref_legal(st)[0], st)

    def test_the_release_rule_boundary(self):
        for s1, legal in ((-48, True), (-47, True), (-46, False), (-1, False), (0, False), (1, True)):
            with self.subTest(s1=s1):
                st = GH.make_step(375, s1, 1, 198, 750, 1, 1)
                self.assertEqual(GH.problems(st, 338) == [], legal)
                self.assertEqual(ref_legal(st)[0], legal)

    def test_illegal_cells_are_skipped_and_reported(self):
        rc, out = hunt("readmap", "--v", "375", "--s1-from", "-50", "--s1-to", "5", "--s1-step", "5")
        self.assertEqual(rc, 0, out)
        self.assertIn("skipped (illegal) [375,72,1,198,750,1,3,0,-20,0,117,150]", out)
        self.assertNotIn("skipped (illegal) [375,67,", out)     # s1 -50: legal
        rc, out = hunt("dwell", "--v", "375", "--s1", "-20")
        self.assertNotEqual(rc, 0)
        rc, out = hunt("readmap", "--v", "375", "--s1-from", "-40", "--s1-to", "0")
        self.assertNotEqual(rc, 0)                              # nothing legal left
        rc, out = hunt("train", "--from", "331", "--to", "325")
        self.assertNotEqual(rc, 0)                              # below the default floor (338)
        p = plan_of("train", "--from", "344", "--to", "331", "--floor", "331")
        self.assertEqual(p["env"]["FLOOR"], 331)

    def test_check_subcommand(self):
        rc, out = hunt("check", json.dumps([[375, 62, 1, 198, 750, 0, 2, 0, -20, 0, 117, 150]]), "-v")
        self.assertEqual(rc, 0, out)
        self.assertIn("train writes -55..-8, +143..+190", out)
        rc, out = hunt("check", json.dumps([[375, 97, 1, 198, 750, 1, 2, 0, -20, 0, 117, 150]]))
        self.assertEqual(rc, 1, out)
        self.assertIn("in flight across the PEX_RST release", out)
        rc, out = hunt("check", json.dumps([350, 30, 1, 36, 825, 0, 3, 0, -20, 0, 124, 150]), "--pw-min", "36")
        self.assertEqual(rc, 0, out)                            # card G's single slow dip: legal at the firmware floor
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "p.json")
            rc, out = hunt("init", "--out", path)
            self.assertEqual(rc, 0, out)
            rc, out = hunt("check", path)
            self.assertEqual(rc, 0, out)

    def test_warmup_options(self):
        p = plan_of("init", "--warmup", "[369,-60,3,198,750,1]")
        self.assertEqual(p["warmup"], [369, -60, 3, 198, 750, 1, 1, 0, -20, 0, 117, 150])
        self.assertFalse(p["extra_first_rep"])
        self.assertTrue(plan_of("init")["extra_first_rep"])
        self.assertEqual(plan_of("init", "--warmup", "[450,-60,3,198,750,1,10,0,-20,0,225,150]")["warmup"][6], 10)
        self.assertIsNone(plan_of("init", "--warmup", "none")["warmup"])
        self.assertNotEqual(hunt("init", "--warmup", "[369,100,1,198,750,1]")[0], 0)   # s1 -17: illegal

    def test_shoot_through_and_partials_are_opt_in(self):
        p = plan_of("train")
        self.assertEqual((p["env"]["SHOOT_THROUGH_STALLS"], p["env"]["PARTIAL_CONTINUE"], p["env"]["LOST_NOPARK"]), (0, 0, 1))
        p = plan_of("train", "--shoot-through", "--continue-partials")
        self.assertEqual((p["env"]["SHOOT_THROUGH_STALLS"], p["env"]["PARTIAL_CONTINUE"]), (1, 1))
        self.assertTrue(any("rule 2" in n for n in p["notes"]))


LEGACY = """bench up 120s boot_id x
pico: clk=400 MHz  last reset: power-on / external reset  fw=lab-psr1-push2-debug
unlock: unlock MATCH wp=00 cfg=1229
sentinel baseline 0x820410=0x00000001
   glitch vd=381 td=70 vr=750 tr=-20 tv=0 fast=1 hold=0 pw=198 gap=150 delay=117 revert=0 reset_at=-1000 dither=0 hold_at=none
   release_at=0 scheduled=0 via=alarm
   glitch done pwd=1 e1=0xc2fa nvvdd=1.01 writes_failed=0
   glitch vd=381 td=70 vr=750 tr=-20 tv=0 fast=1 hold=0 pw=198 gap=150 delay=117 revert=0 reset_at=-1000 dither=0 hold_at=none
   release_at=0 scheduled=0 via=alarm
   glitch done pwd=1 e1=0xc2fa nvvdd=1.02 writes_failed=0
   stall read (GFW still booting after 1 s; reads only, no g/r/j): FUSECTRL=0xe0040000 GFW=0x00000000 DSOV=0x00000001 CRC=0x00000055 PLM=0xffffffff SENTINEL=0x00000000
   observed: FUSECTRL=0xe0040000 GFW=0x00000000 DSOV=- CRC=- PLM=- state=booting reason=GFW low byte is 0x00, not 0xff
step 0 shot 1 v=381 td=70 pw=198 vr=750 f=1: booting GFW=0x00000000 (no further BAR0 reads)
   glitch vd=381 td=70 vr=750 tr=-20 tv=0 fast=1 hold=0 pw=198 gap=150 delay=117 revert=0 reset_at=-1000 dither=0 hold_at=none
   release_at=0 scheduled=0 via=alarm
   glitch done pwd=1 e1=0xc2fa nvvdd=1.03 writes_failed=0
cell 0 v=381 td=70 np=1 pw=198 vr=750: {'under': 2, 'lost': 0, 'partial': 0, 'other': 0, 'booting': 1}
   glitch vd=369 td=-60 vr=750 tr=-20 tv=0 fast=1 hold=0 pw=198 gap=150 delay=117 revert=0 reset_at=-1000 dither=0 hold_at=none
   release_at=0 scheduled=0 via=alarm
   glitch done pwd=1 e1=0xc2fa nvvdd=1.04 writes_failed=0
   stall read (GFW still booting after 1 s; reads only, no g/r/j): FUSECTRL=0xe0040000 GFW=0x00000000 DSOV=0x00000000 CRC=0x00000077 PLM=0xffffffff SENTINEL=0x00000000
step 1 shot 3 v=369 td=-60 pw=198 vr=750 f=1: STALL PARTIAL FUSECTRL=0xe0040000 GFW=0x00000000 DSOV=0x00000000 CRC=0x00000077 PLM=0xffffffff SENTINEL=0x00000000
   observed: FUSECTRL=0xe0040000 GFW=0x00000000 DSOV=0x00000000 CRC=0x00000077 PLM=0xffffffff SENTINEL=0x00000000 state=partial_or_corrupt reason=DSOV 0 during a GFW stall
step 1 shot 3 v=369 td=-60 pw=198 vr=750 f=1: dsov0 crc=0x77 priv=0xffffffff
   glitch vd=369 td=-60 vr=750 tr=-20 tv=0 fast=1 hold=0 pw=198 gap=150 delay=117 revert=0 reset_at=-1000 dither=0 hold_at=none
   timing i=4 scheduled=-177 act=1 arg=59 start=-177 end=-130 ok=1
   timing i=5 scheduled=21 act=1 arg=120 start=21 end=68 ok=1
   timing i=6 scheduled=171 act=1 arg=59 start=171 end=218 ok=1
   timing i=7 scheduled=369 act=1 arg=120 start=369 end=416 ok=1
   timing i=8 scheduled=519 act=1 arg=59 start=519 end=566 ok=1
   release_at=0 scheduled=0 via=alarm
   glitch done pwd=1 e1=0xc2fa nvvdd=1.05 writes_failed=0
   observed: FUSECTRL=0xbadf510e GFW=- DSOV=- CRC=- PLM=- state=poisoned reason=FUSECTRL: BADF sentinel
step 1 shot 4 v=369 td=-60 pw=198 vr=750 f=1: poisoned FUSECTRL=0xbadf510e (no further BAR0 reads)
TIPPED at step 1 shot 4 (v=369 td=-60 pw=198 vr=750 f=1): poisoned FUSECTRL=0xbadf510e (no further BAR0 reads). Power cycle (5 shots this boot)
RESULT: TIPPED at step 1 shot 4 (v=369 td=-60 pw=198 vr=750 f=1): poisoned (runner rc 10)
"""


class TallyTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp)

    def write(self, name, text):
        path = os.path.join(self.tmp, name)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w") as f:
            f.write(text)
        return path

    def tally(self, *paths):
        rc, out = hunt("tally", "--json", *paths)
        self.assertEqual(rc, 0, out)
        return json.loads(out)

    @staticmethod
    def cell(t, v, s1, **kw):
        for c in t["cells"]:
            if c["v"] == v and c["s1"] == s1 and all(c[k] == x for k, x in kw.items()):
                return c
        raise AssertionError("no cell %s %s %s in %s" % (v, s1, kw, [(c["v"], c["s1"]) for c in t["cells"]]))

    def runner_log(self, name, seq, codes, **env):
        rc, out, ev = run_runner(seq, codes, **env)
        return rc, self.write(name, out), out

    def test_shot_lines_first_shot_and_stall_reads(self):
        seq = [GH.make_step(381, -47, 1, 198, 750, 1, 5)]
        rc, path, out = self.runner_log("b1/boot-01.log", seq, "ussIk", SHOOT_THROUGH_STALLS=1, FLOOR=375)
        self.assertEqual(rc, 0, out)
        t = self.tally(path)
        self.assertEqual((t["shots"], t["first_shots"]), (5, 1))
        c = self.cell(t, 381, -47)
        self.assertEqual(c["counted"], 4)                       # the first shot of the run is not counted
        self.assertEqual(c["first_shots"], {"under": 1})
        self.assertEqual(c["counts"], {"stall": 3, "compass": 1})
        self.assertEqual(c["codes"]["CRC"], {"0x75": 2, "0x55": 1, "0x57": 1})
        self.assertEqual(c["codes"]["SENTINEL"], {"0x0": 3, "0x1": 1})
        self.assertIn("sentinel 0 with CRC 0x55 and DSOV 1: init disturbed", c["flags"])
        self.assertEqual(c["phase"], "init")

    def test_first_shot_poison_is_not_counted_but_ends_nothing_in_the_tally(self):
        seq = [GH.make_step(375, -58, 1, 198, 750, 1, 3)]
        rc, path, out = self.runner_log("b/boot-01.log", seq, "x", FLOOR=375)
        self.assertEqual(rc, 10, out)
        t = self.tally(path)
        c = self.cell(t, 375, -58)
        self.assertEqual((c["counted"], c["first_shots"]), (0, {"poison": 1}))

    def test_runs_are_split_and_each_has_its_own_first_shot(self):
        seq = [GH.make_step(381, -47, 1, 198, 750, 1, 2)]
        _, _, out1 = self.runner_log("x", seq, "ku")
        _, _, out2 = self.runner_log("y", seq, "uk")
        path = self.write("two/boots.txt", out1 + "\n" + out2)
        t = self.tally(path)
        c = self.cell(t, 381, -47)
        self.assertEqual((c["counted"], c["counts"], c["first_shots"]), (2, {"under": 1, "compass": 1},
                                                                         {"compass": 1, "under": 1}))

    def test_the_same_run_in_a_log_and_its_launcher_output_counts_once(self):
        seq = [GH.make_step(381, -47, 1, 198, 750, 1, 3)]
        _, _, out = self.runner_log("x", seq, "uks")
        d = os.path.join(self.tmp, "logs")
        self.write("logs/boot-01.log", out)
        self.write("logs/boot-01.out", "cycle (wol): clean shutdown\n" + out + "RESULT: x\n")
        t = self.tally(d)
        self.assertEqual((t["shots"], t["stats"]["dup_runs"]), (3, 1))
        self.write("logs/boot-02.log", out)                     # the same text in another boot's log: a second run
        t = self.tally(d)
        self.assertEqual(t["shots"], 6)

    def test_older_runner_lines(self):
        path = self.write("old/c-b01.raw", LEGACY)
        t = self.tally(path)
        self.assertEqual((t["shots"], t["first_shots"]), (5, 1))
        c = self.cell(t, 381, -47)
        self.assertEqual(c["np"], 1)                            # from the cell line
        self.assertEqual((c["counted"], c["counts"]), (2, {"stall": 1, "under": 1}))
        self.assertIn("sentinel 0 with CRC 0x55 and DSOV 1: init disturbed", c["flags"])
        c = self.cell(t, 369, -177)
        self.assertEqual(c["np"], 3)                            # from the timing lines (no cell line: the cell tipped)
        self.assertEqual(c["counts"], {"partial": 1, "poison": 1})
        self.assertEqual(c["codes"]["CRC"], {"0x77": 1})
        self.assertIn("DSOV 0 with a full mask", c["flags"])
        self.write("old/c-b01.raw.launcher", LEGACY)            # the launcher copy of the same boot
        t = self.tally(os.path.join(self.tmp, "old"))
        self.assertEqual((t["shots"], t["stats"]["dup_runs"]), (5, 1))

    def test_goldilocks_stops_everything(self):
        seq = [GH.make_step(375, -55, 1, 198, 750, 0, 3)]
        rc, path, out = self.runner_log("g/boot-01.log", seq, "ug", FLOOR=375)
        self.assertEqual(rc, 0, out)
        t = self.tally(path)
        self.assertTrue(t["next"]["headline"].startswith("STOP: a goldilocks frame was held (375 mV 1x198 s1 -55 fast 0"))
        self.assertEqual(t["next"]["commands"], [])
        rc, text = hunt("tally", path)
        self.assertIn("HOLD", text)
        self.assertIn("Nothing in this repository senses, arms or burns fuses", text)
        rc, path, out = self.runner_log("h/boot-01.log", [GH.make_step(375, -55, 1, 198, 750, 0, 3)], "uH",
                                        FLOOR=375, SHOOT_THROUGH_STALLS=1)
        self.assertEqual(rc, 15, out)
        t = self.tally(path)
        self.assertTrue(t["next"]["headline"].startswith("STOP: a goldilocks frame was held"))
        self.assertTrue(any("GFW stall" in d for d in t["next"]["details"]))

    def test_partial_hold_suggests_a_dwell_with_partials_allowed(self):
        rc, path, out = self.runner_log("p/boot-01.log", [GH.make_step(369, -177, 3, 198, 750, 1, 4)], "uS", FLOOR=369)
        self.assertEqual(rc, 15, out)
        t = self.tally(path)
        self.assertTrue(t["next"]["headline"].startswith("STOP: a DSOV-0 frame is held"))
        self.assertIn("--continue-partials", t["next"]["commands"][0])

    def synth(self, name, cells):
        """A runner log made of SHOT lines: cells = [(v, s1, np, fast, [kinds...], regs or None)]."""
        lines = ["pico: fw=lab-psr1-push2"]
        n = 0
        for v, s1, np_, fast, kinds, regs in cells:
            for k in kinds:
                r = regs or {}
                rv = " ".join("%s=%s" % (x, ("0x%08x" % r[x]) if x in r else "-") for x in GH.REG_NAMES)
                lines.append("SHOT n=%d step=0 v=%d td=%d np=%d pw=198 vr=750 fast=%d delay=117 gap=150 s1=%d first=%d "
                             "after_park=0 stall=0 kind=%s %s | x" % (n, v, s1 + 117, np_, fast, s1, int(n == 0), k, rv))
                n += 1
        return self.write(name, "\n".join(lines) + "\n")

    def test_next_phase_follows_the_strategy(self):
        warm = (450, -177, 3, 1, ["under"], None)
        # phase 2 -> 3: a non-monotonic train map (card H) -> map the read at the poison code
        p = self.synth("t1/a.log", [warm] + [(388, -177, 3, 1, ["lost"] * 3, None), (381, -177, 3, 1, ["stall"] * 3, {"CRC": 0x75}),
                                             (375, -177, 3, 1, ["poison"] * 3, None), (369, -177, 3, 1, ["under"] * 3, None),
                                             (362, -177, 3, 1, ["lost"] * 3, None)])
        t = self.tally(p)
        self.assertEqual(t["next"]["commands"], ["goldilocks_hunt.py readmap --v 375"])
        self.assertTrue(t["nonmonotonic"])
        # all under -> deeper
        p = self.synth("t2/a.log", [warm] + [(v, -177, 3, 1, ["under"] * 3, None) for v in (400, 394, 388)])
        self.assertEqual(self.tally(p)["next"]["headline"], "All under: go deeper.")
        # phase 3 -> 4: a read map -> init
        p = self.synth("t3/a.log", [warm] + [(375, s1, 1, 1, ["stall"] * 3, {"CRC": 0x57}) for s1 in (60, 70, 80)] +
                       [(375, s1, 1, 1, ["under"] * 3, None) for s1 in (40, 45, 150)])
        t = self.tally(p)
        self.assertEqual(t["next"]["commands"], ["goldilocks_hunt.py init --depths 450,425,400,388,381,375"])
        # phase 4 -> 5: under at -47 and poison a few us earlier at 375 (card H) -> slow at 375
        cells = [warm] + [(381, s1, 1, 1, ["stall"] * 3, {"DSOV": 1, "CRC": 0x55, "SENTINEL": 0}) for s1 in (-47, -52, -59)]
        cells += [(381, -60, 1, 1, ["poison"] * 3, None)]
        cells += [(375, s1, 1, 1, ["under"] * 3, None) for s1 in (-47, -52, -57)] + [(375, -58, 1, 1, ["poison"] * 3, None)]
        t = self.tally(self.synth("t4/a.log", cells))
        self.assertEqual(t["next"]["commands"], ["goldilocks_hunt.py slow --v 375"])
        # a wide edge is pinned first
        cells = [warm] + [(375, s1, 1, 1, ["under"] * 3, None) for s1 in (-47, -52)] + [(375, -62, 1, 1, ["poison"] * 3, None)]
        t = self.tally(self.synth("t5/a.log", cells))
        self.assertEqual(t["next"]["commands"][0], "goldilocks_hunt.py init --depths 375 --s1-from -52 --s1-to -62 --s1-step 1 --reps 3")
        # phase 5 -> 6: slow scan with a poison edge -> dwell on the cell next to it, in fresh boots
        cells = [warm] + [(375, s1, 1, 0, ["under"] * 2, None) for s1 in (-47, -50)] + \
                [(375, -55, 1, 0, ["stall"] * 2, {"DSOV": 1, "CRC": 0x55, "SENTINEL": 0}), (375, -60, 1, 0, ["poison"] * 2, None)]
        t = self.tally(self.synth("t6/a.log", cells))
        self.assertTrue(t["next"]["commands"][0].startswith("goldilocks_hunt.py dwell --v 375 --s1 -55 --np 1 --pw 198 --fast 0"))
        # a DSOV-0 frame with a full mask anywhere -> dwell there (card G)
        cells = [warm, (369, -177, 3, 1, ["partial", "under"], {"DSOV": 0, "CRC": 0x77, "PLM": 0xFFFFFFFF, "SENTINEL": 0})]
        t = self.tally(self.synth("t7/a.log", cells))
        self.assertTrue(t["next"]["headline"].startswith("Dwell: DSOV-0 frames with a full mask at 369 mV"))
        # DSOV 0 but over (CRC 0x00, mask 0xfffffefe) -> too deep, one code shallower
        cells = [warm, (350, -94, 1, 0, ["partial"] * 2, {"DSOV": 0, "CRC": 0, "PLM": 0xFFFFFEFE, "SENTINEL": 0})]
        t = self.tally(self.synth("t8/a.log", cells))
        self.assertIn("over", t["next"]["headline"])
        self.assertIn("--v 356", t["next"]["commands"][0])
        # nothing yet -> the train
        t = self.tally(self.write("empty/a.log", "nothing here\n"))
        self.assertEqual(t["next"]["commands"], ["goldilocks_hunt.py train --out plan-train.json"])

    def test_crc_fields(self):
        self.assertEqual(GH.crc_label(0x57), "block 0 failed")
        self.assertEqual(GH.crc_label(0x75), "block 2 failed")
        self.assertEqual(GH.crc_label(0x77), "block 0 failed, block 2 failed")
        self.assertEqual(GH.crc_label(0x74), "block 0 reads 00, block 2 failed")
        for code, partial in ((0x57, False), (0x75, False), (0x77, False), (0x74, True), (0x76, True), (0x67, True),
                              (0x65, True), (0x55, False), (0x00, False)):
            self.assertEqual(GH.crc_partial(code), partial, hex(code))

    def test_text_report(self):
        seq = [GH.make_step(381, -47, 1, 198, 750, 1, 3), GH.make_step(381, -52, 1, 198, 750, 1, 3)]
        rc, path, out = self.runner_log("r/boot-01.log", seq, "uuusss", SHOOT_THROUGH_STALLS=1, FLOOR=375)
        rc, text = hunt("tally", path)
        self.assertEqual(rc, 0, text)
        self.assertIn("Not counted: 1 first shots of a run", text)
        self.assertIn("1x198 us, restore 750, fast slew (init)", text)
        self.assertIn("s1 -47 under | s1 -52 stall/0x75", text)
        self.assertIn("0x75 block 2 failed", text)


FAKE_BOOTRUN = r'''#!/bin/bash
n=$(cat "$FAKE_DIR/count" 2>/dev/null || echo 0); echo $((n+1)) > "$FAKE_DIR/count"
printf '%s\t%s\t%s\t%s\t%s\n' "$1" "$2" "${NO_ON_HOLD:-unset}" "${NOCYCLE:-unset}" "${REPEAT:-unset} ${FLOOR:-unset} ${LOST_NOPARK:-unset}" >> "$FAKE_DIR/calls"
[ -n "${BOOTRUN_LOG:-}" ] && echo "runner log" > "$BOOTRUN_LOG"
exec python3 -c 'import json,sys; b=json.load(open(sys.argv[1]))[int(sys.argv[2])]; sys.stdout.write(b["out"]+"\n"); sys.exit(b["rc"])' "$FAKE_DIR/boots.json" "$n"
'''
DONE = ("sequence exhausted, no hold (9 shots)\nRESULT: sequence exhausted (runner rc 0)", 0)


class RunDriverTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp)
        self.bootrun = os.path.join(self.tmp, "bootrun.sh")
        with open(self.bootrun, "w") as f:
            f.write(FAKE_BOOTRUN)

    def reset_fake(self):
        for f in ("count", "calls"):
            if os.path.exists(os.path.join(self.tmp, f)):
                os.remove(os.path.join(self.tmp, f))

    def plan(self, *args):
        path = os.path.join(self.tmp, "plan.json")
        rc, out = hunt(*(list(args) + ["--out", path]))
        self.assertEqual(rc, 0, out)
        return path

    def drive(self, plan, boots, *extra, env=None):
        with open(os.path.join(self.tmp, "boots.json"), "w") as f:
            json.dump([{"out": o, "rc": rc} for o, rc in boots], f)
        e = dict(os.environ, FAKE_DIR=self.tmp, NOCYCLE="1")
        e.update(env or {})
        p = subprocess.run([sys.executable, "-I", HUNTER, "run", plan, "--card", "t", "--bootrun", self.bootrun,
                            "--log-dir", os.path.join(self.tmp, "logs")] + list(extra), env=e, capture_output=True,
                           text=True, timeout=60)
        calls = []
        if os.path.exists(os.path.join(self.tmp, "calls")):
            with open(os.path.join(self.tmp, "calls")) as f:
                calls = [ln.rstrip("\n").split("\t") for ln in f]
        return p.returncode, p.stdout + p.stderr, calls

    def test_dry_run_runs_nothing(self):
        plan = self.plan("init", "--depths", "450,425")
        rc, out, calls = self.drive(plan, [])
        self.assertEqual(rc, 0, out)
        self.assertIn("DRY RUN", out)
        self.assertEqual(calls, [])
        self.assertIn("boot 1 (if nothing tips)", out)

    def test_boots_carry_the_plan_env_no_hook_and_a_power_cycle(self):
        plan = self.plan("init", "--depths", "450,425")
        rc, out, calls = self.drive(plan, [DONE, DONE], "--execute")
        self.assertEqual(rc, 0, out)
        self.assertIn("RUN: plan done after 1 boots", out)
        self.assertEqual(len(calls), 1)
        card, seq, no_hold, nocycle, env = calls[0]
        self.assertEqual((card, no_hold, nocycle, env), ("t", "1", "unset", "2 338 1"))
        seq = json.loads(seq)
        self.assertEqual(seq[0][6], 4)                          # the extra first shot
        self.assertEqual(len(seq), 22)
        self.assertTrue(os.path.exists(os.path.join(self.tmp, "logs", "boot-01.log")))
        self.assertTrue(os.path.exists(os.path.join(self.tmp, "logs", "boot-01.out")))

    def test_shots_per_boot_split_and_warmup_prefix(self):
        plan = self.plan("init", "--depths", "450,425", "--max-shots-per-boot", "30", "--warmup", "[450,-60,3,198,750,1]")
        rc, out, calls = self.drive(plan, [DONE] * 3, "--execute")
        self.assertEqual(rc, 0, out)
        self.assertEqual(len(calls), 3)
        seqs = [json.loads(c[1]) for c in calls]
        self.assertTrue(all(s[0] == [450, -60, 3, 198, 750, 1, 1, 0, -20, 0, 117, 150] for s in seqs))
        self.assertEqual(sum(len(s) - 1 for s in seqs), 22)
        self.assertTrue(all(sum(st[6] for st in s[1:]) <= 30 for s in seqs))

    def test_poison_stops_by_default(self):
        plan = self.plan("init", "--depths", "450,425")
        tip = ("TIPPED at step 3 shot 9 (v=450 td=55 pw=198 vr=750 f=1): poisoned FUSECTRL=0xbadf510e (no further BAR0 reads).", 10)
        rc, out, calls = self.drive(plan, [tip, DONE], "--execute")
        self.assertEqual(rc, 3, out)
        self.assertIn("stopped: poison at plan step 3", out)
        self.assertEqual(len(calls), 1)

    def test_poison_skips_the_column_when_asked_to_continue(self):
        plan = self.plan("init", "--depths", "450,425")
        tip = ("TIPPED at step 3 shot 9 (v=450 td=55 pw=198 vr=750 f=1): poisoned FUSECTRL=0xbadf510e (no further BAR0 reads).", 10)
        rc, out, calls = self.drive(plan, [tip, DONE], "--execute", "--on-poison", "continue")
        self.assertEqual(rc, 0, out)
        self.assertIn("the rest of column 0", out)
        second = json.loads(calls[1][1])
        self.assertEqual([st[0] for st in second], [425] * 11)  # straight to the next depth
        self.assertEqual(second[0][6], 4)                       # a new boot: its first step gets the extra shot

    def test_lost_and_stall_tips_continue_with_the_next_step(self):
        plan = self.plan("slow", "--v", "375")
        for text in ("TIPPED at step 2 shot 5 (v=375): lost and not recovered by a plain reset (link down). Power cycle",
                     "TIPPED at step 2 shot 5 (v=375): booting GFW=0x00000000 (GFW stall). Power cycle (6 shots this boot)"):
            with self.subTest(text=text[:40]):
                self.reset_fake()
                rc, out, calls = self.drive(plan, [(text, 10), DONE], "--execute")
                self.assertEqual(rc, 0, out)
                self.assertEqual(len(json.loads(calls[1][1])), 11 - 3)

    def test_holds_latch_and_unknown_outcomes_stop(self):
        plan = self.plan("slow", "--v", "375")
        for boot, rc_want, text in (
                ((">>> GOLDILOCKS HELD (card t) v=375 td=62\nHOLD SEEN 01:02:03: held", 0), 4, "GOLDILOCKS HELD"),
                ((">>> STALL DSOV0 HELD (card t): a goldilocks signature (DSOV 0, CRC 0x55, full mask) while GFW is stalled", 15), 4, "GOLDILOCKS HELD"),
                ((">>> STALL DSOV0 HELD (card t): DSOV 0 while GFW is stalled", 15), 4, "a DSOV-0 frame is held"),
                ((">>> PARTIAL HELD (card t); no sense or next shot", 13), 4, "a DSOV-0 frame is held"),
                (("step 0 v=375 td=62 pw=198 vr=750 f=0: CONTROLLER OFF (0x2042, 41 mV)\nSTOP: power cycle needed", 9), 3, "controller latch"),
                (("MISTIMED at step 0 shot 0 (v=375): release_at=9 us; outcome under. Not scored", 14), 3, "mistimed"),
                (("TIPPED at step 0 shot 1 (v=375): unread (FUSECTRL: unexpected non-idle value). Power cycle", 10), 3, "unknown outcome"),
                (("TEMP GATE FAILED: temp_raw 300", 16), 3, "unknown outcome"),
                (("Traceback (most recent call last):", 1), 3, "unknown outcome")):
            with self.subTest(text=boot[0][:30]):
                self.reset_fake()
                rc, out, calls = self.drive(plan, [boot, DONE], "--execute")
                self.assertEqual(rc, rc_want, out)
                self.assertIn(text, out)
                self.assertEqual(len(calls), 1)
                if text == "GOLDILOCKS HELD":
                    self.assertIn("This tool never burns", out)

    def test_shoot_through_needs_the_owner(self):
        plan = self.plan("train", "--shoot-through")
        rc, out, calls = self.drive(plan, [DONE], "--execute")
        self.assertEqual(rc, 2, out)
        self.assertIn("owner", out)
        self.assertEqual(calls, [])
        rc, out, calls = self.drive(plan, [DONE], "--execute", "--owner-ok-shoot-through")
        self.assertEqual(rc, 0, out)

    def test_stop_file_budget_and_an_illegal_plan(self):
        plan = self.plan("slow", "--v", "375")
        os.makedirs(os.path.join(self.tmp, "logs"))
        open(os.path.join(self.tmp, "logs", "STOP"), "w").close()
        rc, out, calls = self.drive(plan, [DONE], "--execute")
        self.assertEqual((rc, calls), (3, []), out)
        os.remove(os.path.join(self.tmp, "logs", "STOP"))
        tip = ("TIPPED at step 0 shot 1 (v=375): lost and not recovered by a plain reset (link down). Power cycle", 10)
        rc, out, calls = self.drive(plan, [tip] * 5, "--execute", "--max-boots", "2")
        self.assertEqual(rc, 3, out)
        self.assertIn("--max-boots 2 reached", out)
        with open(plan) as f:
            p = json.load(f)
        p["columns"][0]["steps"][0][1] = 97                     # s1 -20: a write across the release
        with open(plan, "w") as f:
            json.dump(p, f)
        rc, out, calls = self.drive(plan, [DONE], "--execute")
        self.assertEqual(rc, 2, out)
        self.assertIn("ILLEGAL", out)


class EndToEndTest(unittest.TestCase):
    def test_every_emitted_boot_runs_on_the_runner_and_tallies(self):
        """The first boot of each phase's plan goes through the real runner against the fake bench (every step is
        accepted by the runner's own checks), and its output goes through the tally."""
        for args in (["train"], ["readmap", "--v", "375"], ["init"], ["slow", "--v", "375"],
                     ["dwell", "--v", "375", "--s1", "-55", "--fast", "0", "--reps", "2"]):
            with self.subTest(args=args):
                p = plan_of(*args)
                flat = GH.flatten(p)
                seq, off, n = GH.boot_steps(p, flat, 0, p["max_shots_per_boot"])
                env = {k: v for k, v in p["env"].items() if k in ("REPEAT", "FLOOR", "LOST_NOPARK", "GFW_WAIT", "STALLRUN",
                                                                  "SHOOT_THROUGH_STALLS", "PARTIAL_CONTINUE", "LOSTRUN")}
                rc, out, ev = run_runner(seq, "", **env)
                self.assertEqual(rc, 0, out)
                self.assertEqual(len(shot_lines(out)), sum(st[6] for st in seq))
                with tempfile.TemporaryDirectory() as tmp:
                    path = os.path.join(tmp, "boot-01.log")
                    with open(path, "w") as f:
                        f.write(out)
                    rc, text = hunt("tally", "--json", path)
                    t = json.loads(text)
                    self.assertEqual(t["shots"], sum(st[6] for st in seq))
                    self.assertEqual(sum(c["counted"] for c in t["cells"]), t["shots"] - 1)


if __name__ == "__main__":
    unittest.main()
