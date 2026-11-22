"""Offline tests of tools/cimp/cimp.py: the configurator (file mode, masking, refused secrets, env precedence, the
generated bench.conf and ssh config), `watch --once` on both log layouts with synthetic logs, the hunter pass-through
and the overlap phase, and `classify`. No serial port, no BAR0, no bench, no network (watch pings a TEST-NET address).

Run:  python3 -I tests/test_cimp.py"""
import json
import os
import stat
import subprocess
import sys
import tempfile
import time
import unittest

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CIMP = os.path.join(REPO, "tools", "cimp", "cimp.py")
WIN = [381, -60, 1, 137, 625, 1, 1, 0, -20, 0, 117, 150, 171]

BOOT = """pico v: clk=400 MHz  nvvdd=0.6923 V  sample_ns=240.0  last reset: power-on / external reset  fw=lab-psr1-push3-debug
PREFLIGHT OK
IDENTITY OK: card X
   gate temp_raw=391 want [395, 406] 12:00:00
   gate temp_raw=397 want [395, 406] 12:00:05
START temp_raw=397 12:00:05
run env: REPEAT=1 SHOTCAP=450 LOSTRUN=8
   glitch vd=381 td=-60 vr=625 tr=-20 tv=171 fast=1 hold=0 pw=130 gap=150 delay=117 revert=0 reset_at=-1000 dither=0 hold_at=none
   release_at=0 scheduled=0 via=alarm
   temp_raw=397
cell 0 v=381 td=-60 np=1 pw=130 vr=625: {'under': 1, 'lost': 0, 'partial': 0, 'other': 0}
   glitch vd=381 td=-60 vr=625 tr=-20 tv=171 fast=1 hold=0 pw=167 gap=150 delay=117 revert=0 reset_at=-1000 dither=0 hold_at=none
   temp_raw=398
   stall read (GFW still booting after 1 s; reads only, no g/r/j): FUSECTRL=0xe0040000 GFW=0x00000000 DSOV=0x00000001 CRC=0x00000055 PLM=0xffffffff SENTINEL=0x00000000
   observed: FUSECTRL=0xe0040000 GFW=0x00000000 DSOV=- CRC=- PLM=- state=booting
step 1 shot 1 v=381 td=-60 pw=167 vr=625 f=1: booting GFW=0x00000000 (no further BAR0 reads)
cell 1 v=381 td=-60 np=1 pw=167 vr=625: {'under': 0, 'lost': 0, 'partial': 0, 'other': 0, 'booting': 1}
   glitch vd=381 td=-60 vr=625 tr=-20 tv=171 fast=1 hold=0 pw=177 gap=150 delay=117 revert=0 reset_at=-1000 dither=0 hold_at=none
   temp_raw=398
   observed: FUSECTRL=0xe0040000 GFW=0x00000000 DSOV=0x00000000 CRC=0x00000000 PLM=0xfffffcee SENTINEL=0x00000000 state=partial_or_corrupt
step 2 shot 2 v=381 td=-60 pw=177 vr=625 f=1: dsov0 crc=0x00 priv=0xfffffcee
cell 2 v=381 td=-60 np=1 pw=177 vr=625: {'under': 0, 'lost': 0, 'partial': 1, 'other': 0}
"""
POIS = """   glitch vd=388 td=-60 vr=625 tr=-20 tv=171 fast=1 hold=0 pw=157 gap=150 delay=117 revert=0 reset_at=-1000 dither=0 hold_at=none
   release_at=0 scheduled=0 via=alarm
   temp_raw=398
step 3 shot 3 v=388 td=-60 pw=157 vr=625 f=1: poisoned FUSECTRL=0xbadf510e (no further BAR0 reads)
cell 3 v=388 td=-60 np=1 pw=157 vr=625: {'under': 0, 'lost': 0, 'partial': 0, 'other': 1}
   poison reset (100 ms PEX_RST): healthy_locked
"""
HOLD = """   glitch vd=381 td=-60 vr=625 tr=-20 tv=171 fast=1 hold=0 pw=137 gap=150 delay=117 revert=0 reset_at=-1000 dither=0 hold_at=none
   glitch NOTE: push3: PMBus write act=1 scheduled at -40 us ran from -40 us across the PEX_RST release at 0 (allowed in push3; release_at is the check)
   release_at=0 scheduled=0 via=alarm
   temp_raw=397
step 3 shot 3 v=381 td=-60 pw=137 vr=625 f=1: GOLDILOCKS passive candidate
>>> GOLDILOCKS HELD (card X) v=381 td=-60
end temp_raw=397 runner_rc=0
"""


def run(args, home, env=None, stdin=subprocess.DEVNULL):
    e = {k: v for k, v in os.environ.items() if not k.startswith("CIMP_")}
    e["HOME"] = home
    e.pop("XDG_CONFIG_HOME", None)
    if args and args[0] == "watch":
        e["CIMP_BENCH_ADDR"] = "192.0.2.1"      # TEST-NET: watch never pings a real host here
    e.update(env or {})
    p = subprocess.run([sys.executable, "-I", CIMP] + args, env=e, stdin=stdin, capture_output=True, text=True,
                       timeout=120)
    return p.returncode, p.stdout + p.stderr


class Config(unittest.TestCase):
    def setUp(self):
        self.home = tempfile.mkdtemp()
        self.cfg = os.path.join(self.home, ".config", "cimp", "config.json")

    def test_set_yes_writes_private_file(self):
        rc, out = run(["config", "--yes", "--set", "bench_host=bench.example.lan", "--set", "wol_mac=AA:BB:CC:DD:EE:FF",
                       "--set", "card.x.tag=card X", "--set", "card.x.gpc=0x00000023",
                       "--set", "card.x.smc=SM now=70 ceiling=110"], self.home)
        self.assertEqual(rc, 0, out)
        self.assertEqual(stat.S_IMODE(os.stat(self.cfg).st_mode), 0o600)
        self.assertEqual(stat.S_IMODE(os.stat(os.path.dirname(self.cfg)).st_mode), 0o700)
        prof = json.load(open(self.cfg))["profiles"]["default"]
        self.assertEqual(prof["bench_host"], "bench.example.lan")
        self.assertEqual(prof["wol_mac"], "aa:bb:cc:dd:ee:ff")
        self.assertEqual(prof["cards"]["x"]["gpc"], "0x00000023")

    def fake_bin(self, ping_rc=0):
        """Fake ssh (records its arguments, answers the probe), sshpass and ping first on PATH: no network."""
        d = os.path.join(self.home, "fakebin")
        os.makedirs(d, exist_ok=True)
        for name, body in (("ssh", 'echo "$@" >> "%s/ssh.args"; echo up pico py dir nonvidia' % self.home),
                           ("sshpass", '[ "$1" = -e ] && shift; exec "$@"'), ("ping", "exit %d" % ping_rc)):
            with open(os.path.join(d, name), "w") as f:
                f.write("#!/bin/sh\n%s\n" % body)
            os.chmod(os.path.join(d, name), 0o755)
        return {"PATH": d + os.pathsep + os.environ["PATH"]}

    def ssh_args(self):
        with open(os.path.join(self.home, "ssh.args")) as f:
            return f.read()

    def test_password_auth_check_does_not_force_batch_mode(self):
        # BatchMode=yes turns password authentication off: sshpass could never log in
        env = dict(self.fake_bin(), CIMP_BENCH_HOST="bench.example.lan", CIMP_SSH_AUTH="password",
                   CIMP_SSH_PASSWORD="x")
        rc, out = run(["config", "--test"], self.home, env)
        self.assertIn("PASS  ssh to the bench", out)
        self.assertNotIn("BatchMode=yes", self.ssh_args())
        os.remove(os.path.join(self.home, "ssh.args"))
        rc, out = run(["config", "--test"], self.home, dict(env, CIMP_SSH_AUTH="key"))
        self.assertIn("BatchMode=yes", self.ssh_args())

    def test_refuses_secrets_and_bad_values(self):
        for kv, msg in (("ssh_password=hunter2", "never stores"), ("bench_port=70000", "1..65535"),
                        ("wol_mac=nope", "MAC"), ("power=maybe", "one of"), ("nosuchkey=1", "unknown setting")):
            rc, out = run(["config", "--yes", "--set", kv], self.home)
            self.assertNotEqual(rc, 0, kv)
            self.assertIn(msg, out, kv)
        self.assertFalse(os.path.exists(self.cfg) and "hunter2" in open(self.cfg).read())

    def test_env_precedence_and_masking(self):
        run(["config", "--yes", "--set", "bench_host=bench.example.lan", "--set", "log_dir=/from/profile",
             "--set", "ssh_auth=password"], self.home)
        rc, out = run(["config", "--json"], self.home, {"CIMP_LOG_DIR": "/from/env", "CIMP_SSH_PASSWORD": "s3cr3t-x"})
        self.assertEqual(rc, 0, out)
        d = json.loads(out)
        self.assertEqual(d["settings"]["log_dir"], {"value": "/from/env", "source": "env CIMP_LOG_DIR"})
        self.assertEqual(d["settings"]["bench_host"]["source"], "profile")
        self.assertIn("masked", d["ssh_password"])
        self.assertNotIn("s3cr3t-x", out)
        rc, out = run(["config", "--show"], self.home, {"CIMP_SSH_PASSWORD": "s3cr3t-x"})
        self.assertNotIn("s3cr3t-x", out)

    def test_generated_bench_conf_and_ssh_config(self):
        run(["config", "--yes", "--set", "bench_host=bench.example.lan", "--set", "bench_port=2222",
             "--set", "jump_host=gw@jump.example.lan", "--set", "power=manual", "--set", "card.x.tag=card X",
             "--set", "card.x.gpc=0x00000023", "--set", "card.x.smc=SM now=70 ceiling=110",
             "--set", "card.x.die_dump=dump.json"], self.home)
        bc = os.path.join(self.home, "bench.conf")
        rc, out = run(["config", "--write-bench-conf", bc], self.home)
        self.assertEqual(rc, 0, out)
        sh = subprocess.run(["bash", "-c", ". %s; card_x; echo \"$BENCH_SSH|$BENCH_ADDR|$POWER|$TAG|$GPC|$SMC\"" % bc],
                            capture_output=True, text=True)
        self.assertEqual(sh.stdout.strip(), "cimp-bench|bench.example.lan|manual|card X|0x00000023|SM now=70 ceiling=110")
        sh = subprocess.run(["bash", "-c", ". %s; card_x; echo \"$DIE_DUMP\"" % bc], capture_output=True, text=True)
        self.assertEqual(sh.stdout.strip(), "dump.json")
        pd = os.path.join(self.home, ".config", "cimp", "profiles", "default")
        sc = open(os.path.join(pd, "ssh_config")).read()
        for want in ("HostName bench.example.lan", "Port 2222", "ProxyJump gw@jump.example.lan", "StrictHostKeyChecking yes"):
            self.assertIn(want, sc)
        for f in ("ssh_config", "bench.conf"):
            self.assertEqual(stat.S_IMODE(os.stat(os.path.join(pd, f)).st_mode), 0o600)
        self.assertEqual(stat.S_IMODE(os.stat(os.path.join(pd, "bin", "ssh")).st_mode), 0o700)

    def test_profiles(self):
        run(["config", "--yes", "--set", "bench_host=a.example.lan"], self.home)
        run(["--profile", "two", "config", "--yes", "--set", "bench_host=b.example.lan"], self.home)
        rc, out = run(["config", "--list"], self.home)
        self.assertIn("default  (default)", out)
        self.assertIn("two", out)
        rc, out = run(["--profile", "two", "config", "--json"], self.home)
        self.assertEqual(json.loads(out)["settings"]["bench_host"]["value"], "b.example.lan")

    def test_first_use_without_terminal_does_not_prompt(self):
        d = tempfile.mkdtemp()
        rc, out = run(["watch", "--once", "--no-color", "--log-dir", d], self.home)
        self.assertEqual(rc, 0, out)
        self.assertIn("no runner logs", out)
        self.assertFalse(os.path.exists(self.cfg))


class Watch(unittest.TestCase):
    def setUp(self):
        self.home = tempfile.mkdtemp()

    def test_hunter_layout_hold(self):
        d = tempfile.mkdtemp()
        r = os.path.join(d, "run-1")
        os.makedirs(r)
        open(os.path.join(r, "run.log"), "w").write(
            "######## RUN overlap: release-edge overlap scan 381 mV, 4 steps, card x 2026-01-01T12:00:00\n"
            "######## boot 1: plan steps 0..3 of 4 12:00:00\n")
        open(os.path.join(r, "boot-01.log"), "w").write(BOOT + HOLD)
        rc, out = run(["watch", "--once", "--no-color", "--log-dir", d], self.home)
        self.assertEqual(rc, 0, out)
        self.assertIn("PHASE  HOLD", out)
        self.assertIn("goldilocks 1", out)
        self.assertIn("band 39.5 C - 40.6 C", out)
        self.assertIn("4 shots fired of 4", out)
        self.assertIn("light (clean blocks, DSOV 1) [stall]", out)     # the dash frame does not hide the stall read
        self.assertIn("over (DSOV 0, blocks wiped", out)

    def test_queue_layout_shooting(self):
        d = tempfile.mkdtemp()
        open(os.path.join(d, "q1.log"), "w").write("######## q1 boot 1 [label x] cont 0 n=20 12:00:00\n")
        open(os.path.join(d, "q1-b01.raw"), "w").write(BOOT)
        rc, out = run(["watch", "--once", "--no-color", "--log-dir", d], self.home)
        self.assertIn("PHASE  SHOOTING", out)
        self.assertIn("3 shots fired of 20", out)
        self.assertIn("FAN    OK, in band", out)

    def test_poison_reset_is_shown_and_counted(self):
        d = tempfile.mkdtemp()
        open(os.path.join(d, "q1.log"), "w").write("######## q1 boot 1 [label x] cont 0 n=20 12:00:00\n")
        open(os.path.join(d, "q1-b01.raw"), "w").write(BOOT + POIS)
        rc, out = run(["watch", "--once", "--no-color", "--log-dir", d], self.home)
        self.assertIn("POISON, cleared by a 100 ms reset", out)
        self.assertIn("poison resets 1", out)
        self.assertIn("poison 1", out)

    def test_log_mode(self):
        d = tempfile.mkdtemp()
        open(os.path.join(d, "q1.log"), "w").write("######## q1 boot 1 [label x] cont 0 n=20 12:00:00\n")
        open(os.path.join(d, "q1-b01.raw"), "w").write(BOOT)
        e = {k: v for k, v in os.environ.items() if not k.startswith("CIMP_")}
        e.update(HOME=self.home, CIMP_BENCH_ADDR="192.0.2.1", CIMP_LOG_DIR=d)
        p = subprocess.Popen([sys.executable, "-I", CIMP, "watch", "--log"], env=e, stdout=subprocess.PIPE,
                             stdin=subprocess.DEVNULL, text=True)
        time.sleep(2.5)
        p.terminate()
        out = p.communicate(timeout=10)[0]
        self.assertIn("SHOOTING", out)
        self.assertIn("fan: OK, in band", out)


class Hunt(unittest.TestCase):
    def setUp(self):
        self.home = tempfile.mkdtemp()

    def test_check_push2_and_push3(self):
        rc, out = run(["hunt", "check", json.dumps([WIN])], self.home)
        self.assertEqual(rc, 1, out)
        self.assertIn("in flight across the PEX_RST release", out)
        rc, out = run(["hunt", "check", "--allow-overlap", json.dumps([WIN])], self.home)
        self.assertEqual(rc, 0, out)
        rc, out = run(["hunt", "check", "--allow-overlap", json.dumps([[381, -2000, 1, 100, 625, 1, 1, 0, -20, 0, 0, 150]])],
                      self.home)
        self.assertEqual(rc, 1, out)
        self.assertIn("precedes the setup write", out)

    def test_overlap_phase_builds_the_card_e_shot(self):
        f = os.path.join(self.home, "ov.json")
        rc, out = run(["hunt", "overlap", "--v", "381", "--s1", "-177", "--rises", "7", "--out", f], self.home)
        self.assertEqual(rc, 0, out)
        plan = json.load(open(f))
        steps = [s for c in plan["columns"] for s in c["steps"]]
        self.assertIn(WIN, steps)
        self.assertTrue(plan["allow_overlap"])
        rc, out = run(["hunt", "check", f], self.home)
        self.assertEqual(rc, 0, out)

    def test_run_dry_uses_the_profile(self):
        run(["config", "--yes", "--set", "bench_host=bench.example.lan", "--set", "power=manual", "--set", "card=x",
             "--set", "card.x.tag=card X", "--set", "card.x.gpc=0x00000023", "--set", "card.x.smc=SM now=70 ceiling=110",
             "--set", "log_dir=" + os.path.join(self.home, "logs")], self.home)
        f = os.path.join(self.home, "ov.json")
        run(["hunt", "overlap", "--v", "381", "--s1", "-177", "--rises", "7", "--out", f], self.home)
        rc, out = run(["hunt", "run", f], self.home)
        self.assertEqual(rc, 0, out)
        self.assertIn("DRY RUN", out)
        self.assertIn("card x", out)
        self.assertIn("gate 395..406 raw", out)
        for selection in (["--card", "y"], ["--card=y"]):
            with self.subTest(selection=selection):
                rc, out = run(["hunt", "run", f] + selection, self.home)
                self.assertEqual(rc, 0, out)
                self.assertIn("card y, bootrun", out)


class Classify(unittest.TestCase):
    def test_classify_stdin(self):
        e = {k: v for k, v in os.environ.items() if not k.startswith("CIMP_")}
        p = subprocess.run([sys.executable, "-I", CIMP, "classify"], input=BOOT + HOLD, env=e, capture_output=True,
                           text=True)
        lines = p.stdout.strip().splitlines()
        self.assertEqual([x.split("-> ")[1].split(" (")[0] for x in lines],
                         ["under", "light", "over", "GOLDILOCKS"])

    def classify_text(self, text):
        e = {k: v for k, v in os.environ.items() if not k.startswith("CIMP_")}
        p = subprocess.run([sys.executable, "-I", CIMP, "classify"], input=text, env=e, capture_output=True, text=True)
        return [x.split("-> ")[1] for x in p.stdout.strip().splitlines()]

    def test_a_cell_loss_does_not_relabel_the_last_shot(self):
        g = "   glitch vd=450 td=-60 vr=750 tr=-20 tv=0 fast=1 hold=0 pulses=3 pw=198 gap=150 delay=117 revert=0 dither=0\n"
        text = (g + "SHOT n=0 step=0 v=450 kind=lost FUSECTRL=0xffffffff | lost\n"
                "step 0 shot 0 v=450 td=-60 pw=198 vr=750 f=1: lost\n" + g + "SHOT n=1 step=0 v=450 kind=under | under\n"
                "cell 0 v=450 td=-60 np=3 pw=198 vr=750: {'under': 1, 'lost': 1, 'partial': 0, 'other': 0}\n")
        self.assertEqual(self.classify_text(text), ["lost link", "under (no effect)"])

if __name__ == "__main__":
    unittest.main(verbosity=1)
