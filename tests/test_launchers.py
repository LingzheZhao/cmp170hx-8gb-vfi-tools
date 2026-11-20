"""Offline tests of tools/runner/firstshot.sh and campaign.sh (against a fake bootrun.sh) and of bootrun.sh (against
fake ssh, scp, ping, sleep and python3 on PATH and a bench.conf in a temp dir). The fake ssh answers the remote half
of bootrun.sh with a canned runner output, so these tests cover the controller side: the fresh-boot guard, the plan
checksum, the runner copy, status propagation, the hold hook and the power cycle. Nothing connects anywhere; when
`unshare -n` works the scripts also run without a network.

Run:  python3 -I tests/test_launchers.py"""
import json
import os
import shutil
import subprocess
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
RUNNER_DIR = os.path.join(os.path.dirname(HERE), "tools", "runner")


def _netless():
    try:
        return subprocess.run(["unshare", "-n", "true"], capture_output=True).returncode == 0
    except OSError:
        return False


NETLESS = ["unshare", "-n"] if _netless() else []

FAKE_BOOTRUN = r'''#!/bin/bash
n=$(cat "$FAKE_DIR/count" 2>/dev/null || echo 0); echo $((n+1)) > "$FAKE_DIR/count"
printf '%s\t%s\t%s\n' "$1" "$2" "${NOCYCLE:-unset}" >> "$FAKE_DIR/calls"
exec python3 -c 'import json,sys; b=json.load(open(sys.argv[1]))[int(sys.argv[2])]; sys.stdout.write(b["out"]+"\n"); sys.exit(b["rc"])' "$FAKE_DIR/boots.json" "$n"
'''

SAMPLE = "\n".join([
    "pico: clk=400 MHz fw=lab-psr1-push2-debug", "unlock: unlock MATCH wp=00 cfg=1229",
    "boot pmbus (@r 25 <reg> <n>; words shown high byte first): 13=00 46=1229",
    "   glitch vd=350 td=-60 vr=750 tr=-20 tv=0 fast=1 hold=0 pw=80 gap=150 delay=117 revert=0 reset_at=-1000 dither=0 hold_at=none",
    "   timing i=0 scheduled=-1300 act=8 arg=32 start=-1300 end=-1253 ok=1", "   prime_at=0",
    "   release_at=0 scheduled=0 via=alarm", "   rail_code=41 status 7a=10", "   observed: FUSECTRL=0xe0040000"])
HELD = (">>> GOLDILOCKS HELD (card e) v=344 td=-60\nHOLD SEEN 01:02:03: card e is held in the goldilocks state\n"
        "No ON_HOLD hook set: stopping here.", 0)


class Fixture:
    def __init__(self, test, boots, env=None):
        self.tmp = tempfile.mkdtemp()
        test.addCleanup(shutil.rmtree, self.tmp)
        for f in ("campaign.sh", "firstshot.sh"):
            shutil.copy(os.path.join(RUNNER_DIR, f), self.tmp)
        with open(os.path.join(self.tmp, "bootrun.sh"), "w") as f:
            f.write(FAKE_BOOTRUN)
        with open(os.path.join(self.tmp, "boots.json"), "w") as f:
            json.dump([{"out": o, "rc": rc} for o, rc in boots], f)
        self.env = dict(os.environ, FAKE_DIR=self.tmp, LOGDIR=os.path.join(self.tmp, "logs"))
        self.env.update(env or {})

    def run(self, script, *args):
        p = subprocess.run(NETLESS + ["bash", os.path.join(self.tmp, script)] + list(args), env=self.env,
                           capture_output=True, text=True, timeout=60)
        calls = []
        if os.path.exists(os.path.join(self.tmp, "calls")):
            with open(os.path.join(self.tmp, "calls")) as f:
                calls = [line.rstrip("\n").split("\t") for line in f]
        return p.returncode, p.stdout + p.stderr, calls


EXHAUSTED = ("sequence exhausted, all under\nRESULT: sequence exhausted, all under (runner rc 0)", 0)
TIPPED = ("step 0 v=344 td=-60 pw=80 vr=750 f=1: lost\nTIPPED at step 0 (v=344 td=-60): lost.", 10)


class FirstShotTest(unittest.TestCase):
    def test_pads_steps_and_forces_a_fresh_cycle(self):
        fx = Fixture(self, [EXHAUSTED, EXHAUSTED], {"NOCYCLE": "1"})
        rc, out, calls = fx.run("firstshot.sh", "e", json.dumps([[344, -60], [350, -60, 3, 80, 750, 1, 5, 0, -20, 0, 300]]))
        self.assertEqual(rc, 0, out)
        self.assertIn("FIRSTSHOT: done", out)
        self.assertEqual([json.loads(c[1]) for c in calls],
                         [[[344, -60, 3, 198, 750, 1, 1]], [[350, -60, 3, 80, 750, 1, 1, 0, -20, 0, 300]]])
        self.assertEqual([c[2] for c in calls], ["0", "0"])
        self.assertIn("######## first-shot boot 1/2: [[344, -60, 3, 198, 750, 1, 1]]", out)

    def test_a_tipped_boot_goes_on_also_after_a_failed_park(self):
        for first in (TIPPED, ("step 0 v=325: lost\n   park: rc 4 park: config 0xffffffff while rail is held", 10)):
            with self.subTest(first=first[0][:20]):
                fx = Fixture(self, [first, EXHAUSTED])
                rc, out, calls = fx.run("firstshot.sh", "e", json.dumps([[344, -60, 3, 80], [338, -60, 3, 80]]))
                self.assertEqual(rc, 0, out)
                self.assertEqual(len(calls), 2)

    def test_stop_lines_and_unexpected_exit_codes(self):
        for text, code in (("Traceback (most recent call last):", 1), ("AssertionError: [300, -60]", 1),
                           ("step 0: CONTROLLER OFF (0x2042, 41 mV)\nSTOP: power cycle needed", 9),
                           ("MISTIMED at step 0 shot 0 (v=344): release_at=9 us", 14),
                           ("PARK FAILED before step 1: park: x; stop", 7),
                           ("STOP: unlock/config readback failed", 2), ("TEMP GATE FAILED: temp_raw 300", 16),
                           ("RUNNER TRANSFER FAILED: stop", 8), ("PLAN SHA FAILED (bench hash): stop", 4),
                           ("HOST GUARD: the nvidia module is loaded: stop", 3), ("step 0: nack\nsomething else", 11)):
            with self.subTest(text=text):
                fx = Fixture(self, [(text, code), EXHAUSTED])
                rc, out, calls = fx.run("firstshot.sh", "e", json.dumps([[344, -60, 3, 80], [338, -60, 3, 80]]))
                self.assertEqual(rc, 2, out)
                self.assertEqual(len(calls), 1)

    def test_goldilocks_and_partial(self):
        fx = Fixture(self, [HELD, EXHAUSTED])
        rc, out, calls = fx.run("firstshot.sh", "e", json.dumps([[344, -60], [338, -60]]))
        self.assertEqual((rc, len(calls)), (0, 1), out)
        self.assertIn("FIRSTSHOT: goldilocks held", out)
        fx = Fixture(self, [(">>> PARTIAL HELD (card e); no sense or next shot", 13), EXHAUSTED])
        rc, out, calls = fx.run("firstshot.sh", "e", json.dumps([[344, -60], [338, -60]]))
        self.assertEqual((rc, len(calls)), (13, 1), out)

    def test_summary_keeps_the_diagnostic_lines_and_full_log_is_saved(self):
        fx = Fixture(self, [(SAMPLE + "\nsequence exhausted, all under", 0)])
        rc, out, calls = fx.run("firstshot.sh", "e", json.dumps([[350, -60, 3, 80]]))
        self.assertEqual(rc, 0, out)
        for frag in ("glitch vd=", "hold_at=none", "boot pmbus", "observed:", "rail_code=", "unlock:", "prime_at=", "release_at="):
            self.assertIn(frag, out)
        self.assertNotIn("timing i=0", out)
        logs = [os.path.join(dp, f) for dp, _, fs in os.walk(os.path.join(fx.tmp, "logs")) for f in fs]
        self.assertEqual(len(logs), 1)
        with open(logs[0]) as f:
            self.assertIn("timing i=0", f.read())


class CampaignTest(unittest.TestCase):
    SEQ = [[362, -60, 3, 80], [356, -60, 3, 80], [350, -60, 3, 80], [344, -60, 3, 80]]

    def fixture(self, boots):
        fx = Fixture(self, boots)
        path = os.path.join(fx.tmp, "seq.json")
        with open(path, "w") as f:
            json.dump(self.SEQ, f)
        return fx, path

    def test_tip_advances_to_the_next_step_and_exhausted_is_done(self):
        fx, seq = self.fixture([("step 0 v=362: under\nTIPPED at step 1 (v=356 td=-60): poisoned", 10), EXHAUSTED])
        rc, out, calls = fx.run("campaign.sh", "e", seq)
        self.assertEqual(rc, 0, out)
        self.assertIn("CAMPAIGN: sequence done", out)
        self.assertEqual(json.loads(calls[1][1]), self.SEQ[2:])

    def test_a_tip_on_the_last_step_is_not_sequence_done(self):
        fx, seq = self.fixture([("TIPPED at step 3 (v=344 td=-60): lost", 10)])
        rc, out, calls = fx.run("campaign.sh", "e", seq)
        self.assertEqual(rc, 2, out)
        self.assertIn("CAMPAIGN: ended on a tip past the last step", out)
        self.assertNotIn("sequence done", out)

    def test_stops(self):
        for text, code in (("step 1: CONTROLLER OFF (0x2042, 41 mV)\nSTOP: power cycle needed", 9),
                           ("   park after lost shot FAILED (rc 4): x\nTIPPED at step 1 shot 3: lost and the park failed", 10),
                           ("MISTIMED at step 0 shot 0: release_at=10 us; outcome lost", 14),
                           ("Traceback (most recent call last):\nKeyError", 1), ("TIPPED at step 1: lost", 124)):
            with self.subTest(text=text):
                fx, seq = self.fixture([(text, code), EXHAUSTED])
                rc, out, calls = fx.run("campaign.sh", "e", seq)
                self.assertEqual(rc, 2, out)
                self.assertEqual(len(calls), 1)

    def test_max_boots_partial_and_goldilocks(self):
        fx, seq = self.fixture([("TIPPED at step 0: lost", 10)] * 2)
        rc, out, calls = fx.run("campaign.sh", "e", seq, "2")
        self.assertEqual(rc, 2, out)
        self.assertIn("max boots reached", out)
        fx, seq = self.fixture([(">>> PARTIAL HELD (card e)", 13)])
        rc, out, calls = fx.run("campaign.sh", "e", seq)
        self.assertEqual(rc, 13, out)
        fx, seq = self.fixture([HELD])
        rc, out, calls = fx.run("campaign.sh", "e", seq)
        self.assertEqual(rc, 0, out)
        self.assertIn("CAMPAIGN: goldilocks held", out)

    def test_summary_keeps_the_diagnostic_lines(self):
        fx, seq = self.fixture([(SAMPLE + "\nsequence exhausted, no hold (4 shots)", 0)])
        rc, out, calls = fx.run("campaign.sh", "e", seq)
        self.assertEqual(rc, 0, out)
        for frag in ("glitch vd=", "hold_at=none", "boot pmbus", "observed:", "rail_code="):
            self.assertIn(frag, out)


FAKE_BIN = {
    "ssh": r'''#!/bin/bash
while [ $# -gt 0 ]; do case "$1" in -o) shift 2;; -*) shift;; *) break;; esac; done
shift; cmd="$*"; printf 'SSH %.200s\n' "$cmd" >> "$FAKE_DIR/ssh.log"
case "$cmd" in
  "bash -s --"*) cat > "$FAKE_DIR/remote.sh"; printf '%s' "$cmd" > "$FAKE_DIR/remote.args"
                 printf '%b\n' "${FAKE_REMOTE_OUT:-sequence exhausted, all under}"
                 echo "end temp_raw=380 runner_rc=${FAKE_REMOTE_RC:-0}"; exit "${FAKE_REMOTE_RC:-0}" ;;
  *uptime*) echo "${FAKE_UPTIME:-120}" ;;
  *boot_id*) echo "fake-boot-id" ;;
  *sha256sum*) exec bash -c "$cmd" ;;
  *poweroff*) echo POWEROFF >> "$FAKE_DIR/ssh.log" ;;
esac
exit 0''',
    "scp": '#!/bin/bash\necho "SCP $*" >> "$FAKE_DIR/ssh.log"; exit "${FAKE_SCP_RC:-0}"',
    "ping": "#!/bin/bash\nexit 1",
    "sleep": "#!/bin/bash\nexit 0",
    "python3": '#!/bin/bash\nif [ "${1:-}" = - ]; then cat > /dev/null; echo "WOL ${*:2}" >> "$FAKE_DIR/ssh.log"; exit 0; fi\nexec /usr/bin/env -u PATH python3 "$@"',
}

CONF = r'''BENCH_SSH=root@bench.example.lan
BENCH_DIR=/opt/vfi
BENCH_PY=/opt/vfi/venv/bin/python
PICO_PORT=/dev/ttyACM0
POWER=${POWER:-wol}
WOL_MAC=02:00:00:00:00:01
LOG_DIR=$FAKE_DIR/logs
ON_HOLD=${ON_HOLD:-}
card_e() {
  TAG="card e"; GPC=0x00000000; SMC="SM now=70 ceiling=0"; SENTINEL_EXPECT=0x1
  PLAN_LOCAL=$FAKE_DIR/plan-local; PLAN_REMOTE=$FAKE_DIR/plan-remote; PLAN_FILES="plan.json dump.json"
}
card_x() {
  TAG="card x"; GPC=0x00000000; SMC="SM now=70 ceiling=0"
}
'''


class BootrunTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp)
        os.mkdir(os.path.join(self.tmp, "bin"))
        for name, body in FAKE_BIN.items():
            p = os.path.join(self.tmp, "bin", name)
            with open(p, "w") as f:
                f.write(body + "\n")
            os.chmod(p, 0o755)
        with open(os.path.join(self.tmp, "bench.conf"), "w") as f:
            f.write(CONF)
        for side in ("plan-local", "plan-remote"):
            os.mkdir(os.path.join(self.tmp, side))
            for name in ("plan.json", "dump.json"):
                with open(os.path.join(self.tmp, side, name), "w") as f:
                    f.write(name)

    def run_bootrun(self, card="e", **env):
        real_path = os.environ["PATH"]
        e = dict(os.environ, PATH="%s/bin:%s" % (self.tmp, real_path), FAKE_DIR=self.tmp, NOCYCLE="1",
                 BENCH_CONF=os.path.join(self.tmp, "bench.conf"))
        e.update({k: str(v) for k, v in env.items()})
        p = subprocess.run(NETLESS + ["bash", os.path.join(RUNNER_DIR, "bootrun.sh"), card, "[[350,-60,3,80]]"], env=e,
                           capture_output=True, text=True, timeout=60)
        logs = {}
        for name in ("ssh.log", "remote.sh", "remote.args"):
            path = os.path.join(self.tmp, name)
            logs[name] = ""
            if os.path.exists(path):
                with open(path) as f:
                    logs[name] = f.read()
        return p.returncode, p.stdout + p.stderr, logs

    def test_fakes_only_boot_id_and_plan_checksum(self):
        rc, out, logs = self.run_bootrun()
        self.assertEqual(rc, 0, out)
        self.assertIn("SSH bash -s --", logs["ssh.log"])        # the remote half went through the fake ssh
        self.assertIn("bench up 120s boot_id fake-boot-id", out)
        self.assertIn("PLAN SHA MATCH (e)", out)
        self.assertIn("RESULT: sequence exhausted, all under (runner rc 0)", out)
        self.assertIn("SCP -q", logs["ssh.log"])
        self.assertTrue(os.path.exists(os.path.join(self.tmp, "logs", "bootrun-e.last")))

    def test_plan_checksum_failures(self):
        with open(os.path.join(self.tmp, "plan-remote", "dump.json"), "w") as f:
            f.write("changed")
        rc, out, logs = self.run_bootrun()
        self.assertEqual(rc, 4, out)
        self.assertIn("PLAN SHA MISMATCH: stop", out)
        self.assertNotIn("SSH bash -s --", logs["ssh.log"])
        for side in ("plan-local", "plan-remote"):
            os.remove(os.path.join(self.tmp, side, "dump.json"))
        rc, out, logs = self.run_bootrun()                       # missing on both ends is a failure, not a match
        self.assertEqual(rc, 4, out)
        self.assertIn("PLAN SHA FAILED", out)
        self.assertNotIn("PLAN SHA MATCH", out)

    def test_no_plan_files_no_checksum(self):
        rc, out, logs = self.run_bootrun(card="x")
        self.assertEqual(rc, 0, out)
        self.assertNotIn("PLAN SHA", out)
        self.assertIn("SENTINEL_EXPECT=", logs["remote.args"])
        self.assertNotIn("SENTINEL_EXPECT=0x1", logs["remote.args"])

    def test_not_a_fresh_boot(self):
        rc, out, logs = self.run_bootrun(FAKE_UPTIME=5000)
        self.assertEqual(rc, 6, out)
        self.assertIn("NOT A FRESH BOOT", out)

    def test_runner_settings_reach_the_remote_half(self):
        rc, out, logs = self.run_bootrun(BAD0_FOLLOWUP=1, PARK_EACH=1)
        self.assertEqual(rc, 0, out)
        for frag in ("SENTINEL_EXPECT=0x1", "BAD0_FOLLOWUP=1", "CLASSIFY_WRITE=0", "SENTINEL=0x820410"):
            self.assertIn(frag, logs["remote.args"])
        self.assertIn("TEMP GATE FAILED", logs["remote.sh"])
        self.assertIn("exit 16", logs["remote.sh"])
        self.assertIn("runner_rc=$rc", logs["remote.sh"])

    def test_runner_status_is_propagated(self):
        rc, out, logs = self.run_bootrun(FAKE_REMOTE_OUT="TIPPED at step 0 (v=350): lost", FAKE_REMOTE_RC=10)
        self.assertEqual(rc, 10, out)
        self.assertIn("RESULT: TIPPED at step 0 (v=350): lost (runner rc 10)", out)
        rc, out, logs = self.run_bootrun(FAKE_REMOTE_OUT="MISTIMED at step 0 shot 0: release_at=9 us", FAKE_REMOTE_RC=14)
        self.assertEqual(rc, 14, out)
        self.assertIn("RESULT: MISTIMED at step 0", out)
        rc, out, logs = self.run_bootrun(FAKE_REMOTE_OUT="TEMP GATE FAILED: temp_raw 300 did not reach TMIN", FAKE_REMOTE_RC=16)
        self.assertEqual(rc, 16, out)

    def test_failed_runner_copy_stops(self):
        rc, out, logs = self.run_bootrun(FAKE_SCP_RC=1)
        self.assertEqual(rc, 8, out)
        self.assertIn("RUNNER TRANSFER FAILED", out)
        self.assertNotIn("SSH bash -s --", logs["ssh.log"])

    def test_hold_runs_the_hook_and_returns_its_status(self):
        log = os.path.join(self.tmp, "custom", "run.log")
        rc, out, logs = self.run_bootrun(FAKE_REMOTE_OUT=">>> GOLDILOCKS HELD (card e) v=350 td=-60",
                                         ON_HOLD='echo "HOOK $CARD $RUN_LOG"; exit 3', BOOTRUN_LOG=log)
        self.assertEqual(rc, 3, out)
        self.assertIn("HOLD SEEN", out)
        self.assertIn("HOOK e %s" % log, out)
        self.assertIn("ON_HOLD rc=3", out)
        self.assertTrue(os.path.exists(log))

    def test_hold_without_a_hook_stops_and_reports(self):
        rc, out, logs = self.run_bootrun(FAKE_REMOTE_OUT=">>> GOLDILOCKS HELD (card e) v=350 td=-60")
        self.assertEqual(rc, 0, out)
        self.assertIn("No ON_HOLD hook set: stopping here", out)

    def test_hold_line_with_a_failed_runner_does_not_run_the_hook(self):
        rc, out, logs = self.run_bootrun(FAKE_REMOTE_OUT=">>> GOLDILOCKS HELD (card e) v=350 td=-60", FAKE_REMOTE_RC=11,
                                         ON_HOLD='echo HOOK-RAN')
        self.assertEqual(rc, 11, out)
        self.assertIn("HOLD LINE BUT RUNNER RC 11", out)
        self.assertNotIn("HOOK-RAN", out)

    def test_power_cycle_path(self):
        rc, out, logs = self.run_bootrun(NOCYCLE="0")
        self.assertEqual(rc, 0, out)
        self.assertIn("power on (Wake-on-LAN)", out)
        self.assertIn("WOL 02:00:00:00:00:01 255.255.255.255", logs["ssh.log"])
        self.assertIn("POWEROFF", logs["ssh.log"])


if __name__ == "__main__":
    unittest.main()
