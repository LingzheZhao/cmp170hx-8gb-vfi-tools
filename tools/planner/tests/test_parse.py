"""Parser tests on log snippets written in the runner's documented output formats (bisect_shots.py push1/push2,
bootrun.sh, firstshot.sh, campaign.sh, abtest.sh)."""
import json
import os
import tempfile
import unittest

from helpers import ledger  # noqa: F401
from planner_auto import parse

R = "Rest of this boot is unreliable (sticky poison): power cycle, next boot starts just before this step"

FIRSTSHOT_PUSH1 = """######## first-shot boot 1/3: [[344, -60, 3, 80, 750, 1, 1]] 01:25:25
START temp_raw=358 07:29:19
   release_at=9 scheduled=0 via=alarm
step 0 v=344 td=-60 pw=80 vr=750 f=1: under
sequence exhausted, all under
RESULT: sequence exhausted, all under
######## first-shot boot 2/3: [[338, -60, 3, 80, 750, 1, 1]] 01:29:29
START temp_raw=358 07:33:23
   release_at=10 scheduled=0 via=alarm
   observed: FUSECTRL=0xbad00100 GFW=0xbad00100 DSOV=0xbadf1701 CRC=- PLM=- state=poisoned reason=DSOV: BADF sentinel
step 0 v=338 td=-60 pw=80 vr=750 f=1: poisoned DSOV=0xbadf1701 (no further BAR0 reads)
TIPPED at step 0 (v=338 td=-60 pw=80 vr=750 f=1): poisoned DSOV=0xbadf1701 (no further BAR0 reads). %s
RESULT: TIPPED at step 0 (v=338 td=-60 pw=80 vr=750 f=1): poisoned DSOV=0xbadf1701 (no further BAR0 reads). %s
######## first-shot boot 3/3: [[331, -60, 3, 80, 750, 1, 1]] 01:33:32
""" % (R, R)

CAMPAIGN = """7 steps
######## campaign boot 1, steps 0..2 01:00:31
START temp_raw=362 07:03:23
   release_at=9 scheduled=0 via=alarm
   release_at=2 scheduled=0 via=alarm
cell 0 v=450 td=-60 np=3 pw=198 vr=750: {'under': 2, 'lost': 0, 'partial': 0, 'other': 0}
   glitch WARN: PMBus write act=1 at -17 us overlaps the PEX_RST release at 0 (one write ~47 us)
   release_at=0 scheduled=0 via=alarm
   glitch WARN: PMBus write act=1 at -17 us overlaps the PEX_RST release at 0 (one write ~47 us)
   release_at=5 scheduled=0 via=alarm
   park after lost shot FAILED (rc 4): park: rail held 760 mV, PEX_RST, config 0xffffffff; after handback config 0xffffffff, GPU rail 695 mV
TIPPED at step 1 shot 3 (v=350 td=-60 pw=80 vr=750 f=1): lost and the park failed. Power cycle (4 shots this boot)
RESULT:    park after lost shot FAILED (rc 4): park: rail held 760 mV, PEX_RST, config 0xffffffff; after handback config 0xffffffff, GPU rail 695 mV
CAMPAIGN: sequence done
"""
CAMPAIGN_SEQ = [[450, -60, 3, 198, 750, 1, 2], [350, -60, 3, 80, 750, 1, 3, 0, -20, 2], [344, -60, 3, 80, 750, 1, 3]]

ABTEST = """######## BOOT CLASSIFY_WRITE=0 00:40:05
START temp_raw=362 06:43:51
run env: REPEAT=1 SHOTCAP=450 LOSTRUN=2 RESTORE_WAIT=8.0 CELLLOST=1 CLASSIFY_WRITE=0 timeout 900s
   park after lost shot: park: rail held 760 mV, PEX_RST, config 0x20c210de; after handback config 0x20c210de, GPU rail 693 mV
step 0 shot 1 v=450 td=-60 pw=198 vr=750 f=1: lost
cell 0 v=450 td=-60 np=3 pw=198 vr=750: {'under': 1, 'lost': 1, 'partial': 0, 'other': 0}
cell 1 v=381 td=-60 np=3 pw=80 vr=750: {'under': 3, 'lost': 0, 'partial': 0, 'other': 0}
   observed: FUSECTRL=0xe0040000 GFW=0x000000ff DSOV=0x00000001 CRC=0x00000000 PLM=0x00000000 state=partial_or_corrupt reason=x
step 2 shot 6 v=375 td=-60 pw=80 vr=750 f=1: poisoned/over0
TIPPED at step 2 shot 6 (v=375 td=-60 pw=80 vr=750 f=1): poisoned/over0. Power cycle (7 shots this boot)
RESULT: TIPPED at step 2 shot 6 (v=375 td=-60 pw=80 vr=750 f=1): poisoned/over0. Power cycle (7 shots this boot)
"""
ABTEST_SEQ = [[450, -60, 3, 198, 750, 1, 2], [381, -60, 3, 80, 750, 1, 3], [375, -60, 3, 80, 750, 1, 3]]

PUSH2_BOOT = """cycle (wol): clean shutdown 02:44:43
bench off the network; waiting 60s for soft-off
power on (Wake-on-LAN) 02:45:47
bench up 16s
PLAN SHA MATCH (card e)
pico v: clk=400 MHz  nvvdd=0.9589 V  sample_ns=240.0  last reset: power-on / external reset  fw=lab-psr1-push2-debug
PREFLIGHT OK
IDENTITY OK: card e
START temp_raw=362 08:46:49
run env: REPEAT=1 SHOTCAP=450 LOSTRUN=1 RESTORE_WAIT=8.0 CELLLOST=1 CLASSIFY_WRITE=0 SENTINEL_EXPECT=0x1 timeout 600s
pico: clk=400 MHz  nvvdd=0.9825 V  sample_ns=240.0  last reset: power-on / external reset  fw=lab-psr1-push2-debug
unlock: unlock CML 0x08 MATCH | unlock MATCH wp=00 cfg=1229
boot pmbus (@r 25 <reg> <n>; words shown high byte first): 13=00 46=1229 7e=08 79=2c00 88=ed83 8b=02b5 8d=016a
sentinel baseline 0x820410=0x00000001 (expected 0x1)
   glitch vd=350 td=170 vr=750 tr=-20 tv=0 fast=1 hold=0 pw=80 gap=150 delay=117 revert=0 reset_at=-1000 dither=0 hold_at=none
   timing i=0 scheduled=-1300 act=8 arg=32 start=-1296 end=-1240 ok=1
   timing i=1 scheduled=-1200 act=7 arg=0 start=-1200 end=-1151 ok=1
   timing i=2 scheduled=-1000 act=3 arg=0 start=-1000 end=-994 ok=1
   timing i=3 scheduled=-1000 act=1 arg=96 start=-994 end=-946 ok=1
   timing i=4 scheduled=53 act=1 arg=56 start=53 end=100 ok=1
   timing i=5 scheduled=133 act=1 arg=120 start=133 end=180 ok=1
   timing i=6 scheduled=293 act=1 arg=126 start=293 end=340 ok=1
   timing i=7 scheduled=343 act=8 arg=48 start=343 end=390 ok=1
   prime_at=0
   release_at=2 scheduled=0 via=alarm
   glitch done pwd=1 e1=0xc2fa nvvdd=1.0265 writes_failed=0
step 0 v=350 td=170 pw=80 vr=750 f=1: under
   park: rc 0 park: rail held 760 mV, PEX_RST, config 0x20c210de; after handback config 0x20c210de, GPU rail 695 mV
sequence exhausted, all under
end temp_raw=362 runner_rc=0
RESULT: sequence exhausted, all under (runner rc 0)
"""

PUSH2_MISTIMED = """######## first-shot boot 1/1: [[344, -60, 3, 80, 750, 1, 1]] 03:10:00
START temp_raw=362 09:10:00
   release_at=9 scheduled=0 via=alarm
   observed, not scored: FUSECTRL=0xe0040000 GFW=0x000000ff DSOV=0x00000001 CRC=0x00000055 PLM=0xffffffff state=healthy_locked reason=None
step 0 shot 0 v=344 td=-60 pw=80 vr=750 f=1: MISTIMED (release_at=9 us; observed under)
MISTIMED at step 0 shot 0 (v=344 td=-60 pw=80 vr=750 f=1): release_at=9 us; outcome MISTIMED (release_at=9 us; observed under). Not scored: power cycle (1 shots this boot)
   park: rc 0 park: rail held 760 mV, PEX_RST, config 0x20c210de; after handback config 0x20c210de, GPU rail 695 mV
RESULT: MISTIMED at step 0 shot 0 (v=344 td=-60 pw=80 vr=750 f=1): release_at=9 us (runner rc 14)
FIRSTSHOT: stop for review (bootrun rc 14)
"""


def parse_s(text, seq=None, **kw):
    return parse.parse_text(text, card="e", seq=seq, date="2026-11-20", **kw)


class TestClassify(unittest.TestCase):
    def test_outcome_classes(self):
        cases = {
            "under": "under", "lost": "lost", "1 lost in a row": "lost", "lost and the park failed": "lost",
            "poisoned DSOV=0xbadf1701 (no further BAR0 reads)": "poisoned",
            "hidden-poison alias 0x820410=0xbadf1020": "hidden_poison",
            "GOLDILOCKS passive candidate": "goldilocks",
            "dsov0 crc=0x55 priv=0xfffffefe": "partial", "dsov1 crc=0x00 priv=0xffffffff": "over",
            "dsov1 crc=0x75 priv=0xffffffff": "crc_intermediate", "dsov0 crc=0x57 priv=0xffffffff": "crc_intermediate",
            "dsov1 crc=0x77 priv=0xffffffff": "crc_closed", "dsov0 crc=0x00 priv=0x00000000": "over",
            "dsov1 crc=0x05 priv=0xffffffff": "partial",
            "CONTROLLER OFF (0x2042, 41 mV)": "controller_off",
            "controller unreadable after the shot (I2C ERR)": "controller_off",
            "NO DIP (controller pwd=0 e1=0xc2ea): the shot did not run; stop": "no_dip",
            "nack": "invalid", "booting GFW=0x00000001 (no further BAR0 reads)": "invalid",
            "poisoned/over0": "over",
            "dsov=0xbad00100 (no further BAR0 reads)": "poisoned", "dsov=0x00000002 (no further BAR0 reads)": "invalid",
            "unread (FUSECTRL: unexpected non-idle value 0xbad00100)": "poisoned",
            "unread (FUSECTRL: [Errno 5] Input/output error)": "invalid",
            "MISTIMED (release_at=9 us; observed under)": "mistimed",
            "under | re-read poisoned DSOV=0xbadf1020 (no further BAR0 reads)": "poisoned",
        }
        for text, want in cases.items():
            self.assertEqual(parse.classify(text)[0], want, text)
        self.assertEqual(parse.classify("dsov0 crc=0x55 priv=0xfffffefe")[1]["plm"], "0xfffffefe")
        self.assertEqual(parse.classify("MISTIMED (release_at=9 us; observed under)")[1]["observed"], "under")


class TestCrcCompass(unittest.TestCase):
    def test_crc_class(self):
        self.assertEqual(parse.crc_class(1, 0x55, 0xFFFFFFFF), "under")
        self.assertEqual(parse.crc_class(0, 0x55, 0xFFFFFFFF), "goldilocks")
        self.assertEqual(parse.crc_class(0, 0x55, 0xFFFFFEFE), "partial")
        self.assertEqual(parse.crc_class(1, 0x77, 0xFFFFFFFF), "crc_closed")
        for crc in (0x75, 0x57, 0x60):
            self.assertEqual(parse.crc_class(1, crc, 0xFFFFFFFF), "crc_intermediate")
        self.assertEqual(parse.crc_class(0, 0x00, 0), "over")
        self.assertIsNone(parse.crc_class(1, None, None))

    def test_observed_line_sets_crc_on_every_non_under_shot(self):
        text = ("######## first-shot boot 1/1: [[331, -60, 3, 80, 750, 1, 1, 0, -20, 0, 330, 150]] 03:00:00\n"
                "START temp_raw=362 09:00:00\n   release_at=1 scheduled=0 via=alarm\n"
                "   observed: FUSECTRL=0xe0040000 GFW=0x000000ff DSOV=0x00000001 CRC=0x00000075 PLM=0xffffffff "
                "SENTINEL=0x00000001 state=partial_or_corrupt reason=Diagnostic signature is not healthy\n"
                "step 0 v=331 td=-60 pw=80 vr=750 f=1: dsov1 crc=0x75 priv=0xffffffff\n"
                "TIPPED at step 0 (v=331 td=-60 pw=80 vr=750 f=1): dsov1 crc=0x75 priv=0xffffffff. %s\n" % R)
        r = parse_s(text)[0][0]
        self.assertEqual(r["outcome"], "crc_intermediate")
        self.assertEqual((r["crc"], r["dsov"], r["plm"]), (0x75, 1, 0xFFFFFFFF))
        self.assertEqual(r["regs"]["SENTINEL"], "0x00000001")
        self.assertEqual((r["step"]["delay"], r["step"]["gap"]), (330, 150))
        self.assertEqual(r["step_list"][10:], [330, 150])


class TestFirstShot(unittest.TestCase):
    def test_firstshot_records_and_armed_tail(self):
        recs, rep = parse_s(FIRSTSHOT_PUSH1, firmware="lab-psr1-push1-debug")
        self.assertEqual([r["phase"] for r in recs], ["COMPLETE", "COMPLETE", "ARMED"])
        a, b, c = recs
        self.assertEqual(a["outcome"], "under")
        self.assertEqual(a["release_at"], 9)
        self.assertEqual(a["state_class"], "first-shot-fresh-boot")
        self.assertEqual(a["step_list"], [344, -60, 3, 80, 750, 1, 1, 0, -20, 0, 117, 150])
        self.assertEqual(a["vid_code"], 55)
        self.assertEqual(a["attempt_id"], "e@2026-11-20T07:29:19B#s000")
        self.assertEqual(a["armed_key"], "e@2026-11-20T01:25:25L#s000")
        self.assertEqual(b["outcome"], "poisoned")
        self.assertEqual(b["outcome_detail"], {"register": "DSOV", "value": "0xbadf1701"})
        self.assertIn("FUSECTRL=0xbad00100", b["observed"])
        self.assertEqual(c["step"]["v"], 331)
        self.assertIsNone(c["outcome"])
        self.assertEqual(c["attempt_id"], c["armed_key"])

    def test_armed_settled_by_later_complete(self):
        recs, _ = parse_s(FIRSTSHOT_PUSH1)
        done = FIRSTSHOT_PUSH1 + "START temp_raw=358 07:37:18\n   release_at=10 scheduled=0 via=alarm\n" \
            "step 0 v=331 td=-60 pw=80 vr=750 f=1: lost\nTIPPED at step 0 (v=331 td=-60 pw=80 vr=750 f=1): lost. %s\n" \
            "RESULT: TIPPED at step 0 (v=331 td=-60 pw=80 vr=750 f=1): lost. %s\nFIRSTSHOT: done\n" % (R, R)
        recs2, _ = parse_s(done)
        res = ledger.resolve(recs + recs2)
        self.assertEqual(len(res), 3)
        self.assertEqual(sorted(r["outcome"] for r in res), ["lost", "poisoned", "under"])

    def test_push2_mistimed_line(self):
        recs, _ = parse_s(PUSH2_MISTIMED, firmware="lab-psr1-push2-debug")
        self.assertEqual(len(recs), 1)
        r = recs[0]
        self.assertEqual(r["outcome"], "mistimed")
        self.assertEqual(r["outcome_detail"]["observed"], "under")
        self.assertEqual(r["outcome_detail"]["reason"], "release_at=9 us")
        self.assertEqual(r["release_at"], 9)
        self.assertEqual(r["state_class"], "first-shot-fresh-boot")


class TestCellMode(unittest.TestCase):
    def test_campaign_rebuild_with_warn_and_park(self):
        recs, rep = parse_s(CAMPAIGN, seq=CAMPAIGN_SEQ, firmware="lab-psr1-push1-debug")
        self.assertEqual(rep["warnings"], [])
        self.assertEqual([r["outcome"] for r in recs], ["under", "under", "under", "lost"])
        self.assertEqual([r["release_at"] for r in recs], [9, 2, 0, 5])
        self.assertEqual(recs[0]["state_class"], "first-shot-fresh-boot")
        self.assertEqual(recs[1]["state_class"], "later-shot-same-boot")
        self.assertEqual(recs[3]["seqx_index"], 3)
        self.assertEqual(recs[3]["step"]["revert"], 2)
        self.assertEqual(recs[3]["write_overlaps_release"], [-17])
        self.assertTrue(recs[3]["warns"])
        self.assertEqual(recs[3]["park"]["gpu_rail_mv"], 695)
        self.assertFalse(recs[3]["park"]["ok"])

    def test_abtest_cells_post_park_and_tipped(self):
        recs, rep = parse_s(ABTEST, seq=ABTEST_SEQ)
        self.assertEqual(rep["warnings"], [])
        self.assertEqual(len(recs), 7)
        self.assertEqual([r["outcome"] for r in recs], ["under", "lost", "under", "under", "under", "under", "over"])
        self.assertEqual((recs[6]["dsov"], recs[6]["crc"], recs[6]["plm"], recs[6]["crc_source"]), (1, 0, 0, "observed"))
        self.assertEqual((recs[0]["crc"], recs[0]["crc_source"]), (0x55, "implied_under"))
        self.assertIsNone(recs[1]["crc"])
        self.assertEqual(recs[1]["park"]["config_after"], "0x20c210de")
        self.assertEqual(recs[2]["state_class"], "post-park")
        self.assertEqual(recs[3]["state_class"], "later-shot-same-boot")
        self.assertEqual(recs[0]["conditions"], {"classify_write": 0})
        self.assertIn("CRC=0x00000000", recs[6]["observed"])

    def test_cell_mode_controller_off_counts_from_release_lines(self):
        text = ("cycle (wol): clean shutdown 01:00:00\nbench up 16s\nSTART temp_raw=370 07:00:00\n"
                "run env: REPEAT=1 SHOTCAP=450 LOSTRUN=20 timeout 900s\n"
                + "   release_at=1 scheduled=0 via=alarm\n" * 2
                + "cell 0 v=388 td=-60 np=3 pw=198 vr=750: {'under': 2, 'lost': 0, 'partial': 0, 'other': 0}\n"
                + "   release_at=2 scheduled=0 via=alarm\n" * 3
                + "step 1 v=381 td=-60 pw=198 vr=750 f=1: CONTROLLER OFF (0x2042, 41 mV)\nSTOP: power cycle needed\n")
        seq = [[388, -60, 3, 198, 750, 1, 2], [381, -60, 3, 198, 750, 1, 20]]
        recs, rep = parse_s(text, seq=seq)
        self.assertEqual([r["outcome"] for r in recs], ["under", "under", "under", "under", "controller_off"])
        self.assertEqual(recs[-1]["outcome_detail"], {"status_word": "0x2042", "vout_mv": 41})
        self.assertEqual(recs[0]["state_class"], "first-shot-fresh-boot")


class TestFullLogs(unittest.TestCase):
    def test_push2_boot_geometry_from_echo_and_timing(self):
        recs, rep = parse_s(PUSH2_BOOT)
        self.assertEqual(len(recs), 1)
        r = recs[0]
        self.assertEqual(r["geometry_source"], "echo_timing")
        self.assertEqual(r["step_list"], [350, 170, 1, 80, 750, 1, 1, 0, -20, 0, 117, 150])
        self.assertEqual(r["firmware"], "lab-psr1-push2-debug")
        self.assertEqual((r["release_at"], r["prime_at"], r["pwd"], r["e1"], r["writes_failed"]), (2, 0, 1, "0xc2fa", 0))
        self.assertEqual(r["boot_uptime_s"], 16)
        self.assertEqual(r["state_class"], "first-shot-fresh-boot")
        self.assertEqual(r["park_end"]["rc"], 0)
        self.assertEqual(r["runner_rc"], 0)
        self.assertEqual(r["sentinel_baseline"], "0x00000001")
        self.assertEqual(r["resolved_echo"]["pw"], 80)
        self.assertFalse(r["notes"])

    def test_bare_log_without_cycle_is_not_fresh(self):
        text = PUSH2_BOOT.split("PLAN SHA MATCH (card e)\n", 1)[1]
        recs, _ = parse_s(text)
        self.assertEqual(recs[0]["state_class"], "later-shot-same-boot")

    def test_wrapper_full_logs_dir(self):
        d = tempfile.mkdtemp()
        with open(os.path.join(d, "boot-1.log"), "w") as f:
            f.write(PUSH2_BOOT)
        wrapper = ("full logs: %s\n######## first-shot boot 1/1: [[350, 170, 1, 80, 750, 1, 1]] 02:44:42\n"
                   "START temp_raw=362 08:46:49\n   release_at=2 scheduled=0 via=alarm\n"
                   "step 0 v=350 td=170 pw=80 vr=750 f=1: under\nFIRSTSHOT: done\n" % d)
        recs, rep = parse_s(wrapper)
        self.assertEqual(len(recs), 1)
        self.assertEqual(recs[0]["source_file"], os.path.join(d, "boot-1.log"))
        self.assertEqual(recs[0]["geometry_source"], "step_list")
        self.assertEqual(recs[0]["firmware"], "lab-psr1-push2-debug")
        self.assertEqual(recs[0]["prime_at"], 0)
        # same boot ingested from the directory dedupes on the bench START id
        recs2, _ = parse.parse_file(os.path.join(d, "boot-1.log"), "e", date="2026-11-20")
        self.assertEqual(recs[0]["attempt_id"], recs2[0]["attempt_id"])

    def test_single_mode_stop_lines(self):
        text = ("######## first-shot boot 1/2: [[350, -60, 3, 80, 750, 1, 1]] 01:00:00\nSTART temp_raw=360 07:00:00\n"
                "step 0 v=350 td=-60 pw=80 vr=750 f=1: NO DIP (controller pwd=0 e1=0xc2ea): the shot did not run; stop\n"
                "######## first-shot boot 2/2: [[350, -60, 3, 80, 750, 1, 1]] 01:05:00\nSTART temp_raw=360 07:05:00\n"
                "step 0 v=350 td=-60 pw=80 vr=750 f=1: nack\n   reply: x\nSTOP: shot execution is unknown; power cycle before another shot\n"
                "FIRSTSHOT: stop for review\n")
        recs, _ = parse_s(text)
        self.assertEqual([r["outcome"] for r in recs], ["no_dip", "invalid"])
        self.assertEqual(recs[1]["outcome_detail"], {"kind": "nack"})

    def test_partial_goldilocks_hidden(self):
        mk = lambda out, extra="": ("######## first-shot boot 1/1: [[344, -60, 3, 80, 750, 1, 1]] 01:00:00\n"
                                     "START temp_raw=360 07:00:00\n   release_at=1 scheduled=0 via=alarm\n"
                                     "step 0 v=344 td=-60 pw=80 vr=750 f=1: %s\n%s" % (out, extra))
        r = parse_s(mk("dsov0 crc=0x55 priv=0xfffffefe", ">>> PARTIAL HELD (card e); no sense or next shot\n"))[0][0]
        self.assertEqual((r["outcome"], r["outcome_detail"]["kind"]), ("partial", "dsov0"))
        r = parse_s(mk("GOLDILOCKS passive candidate", ">>> GOLDILOCKS HELD (card e) v=344 td=-60 (passive)\n"))[0][0]
        self.assertEqual(r["outcome"], "goldilocks")
        r = parse_s(mk("hidden-poison alias 0x820410=0xbadf1020"))[0][0]
        self.assertEqual(r["outcome"], "hidden_poison")

    def test_boot_failure_makes_no_record(self):
        text = ("######## first-shot boot 1/1: [[344, -60, 3, 80, 750, 1, 1]] 01:00:00\n"
                "IDENTITY MISMATCH: stop\nRESULT: IDENTITY MISMATCH: stop\nFIRSTSHOT: stop for review\n")
        recs, rep = parse_s(text)
        self.assertEqual(recs, [])
        text2 = "######## first-shot boot 1/1: [[344, -60, 3, 80, 750, 1, 1]] 01:00:00\nPREFLIGHT FAILED: stop\n"
        self.assertEqual(parse_s(text2)[0], [])


class TestQueueHeaders(unittest.TestCase):
    def test_step_list_in_header_and_park_each_cells(self):
        text = ("######## Q6 [331,-60,3,80,750,0,8,0,-20,0,330,150] 04:31:59\n"
                "bench up 16s boot_id f00c31f8-f533-4982-b79a-ee31142aeffd\n"
                "cell 0 v=331 td=-60 np=3 pw=80 vr=750: {'under': 8, 'lost': 0, 'partial': 0, 'other': 0}\n"
                "sequence exhausted, no hold (8 shots)\n"
                "RESULT: sequence exhausted, no hold (8 shots) (runner rc 0)\n")
        recs, rep = parse_s(text, park_each=True)
        self.assertEqual(len(recs), 8)
        self.assertEqual(recs[0]["state_class"], "first-shot-fresh-boot")
        self.assertEqual({r["state_class"] for r in recs[1:]}, {"post-park"})
        self.assertEqual(recs[3]["step"]["delay"], 330)
        self.assertEqual(recs[3]["host_boot_id"], "f00c31f8-f533-4982-b79a-ee31142aeffd")
        self.assertEqual(recs[3]["launch_time"], "2026-11-20T04:31:59")

    def test_park_each_named_in_header_and_exhausted_cells(self):
        text = ("######## Q4-2 PARK_EACH 350 x2 04:09:08\n"
                "   park: rc 0 park: rail held 760 mV, PEX_RST, config 0x20c210de; after handback config 0x20c210de, GPU rail 693 mV\n"
                "sequence exhausted, no hold (2 shots)\n")
        recs, _ = parse_s(text, header_seq={"^######## Q4-2": [[350, -60, 3, 80, 750, 1, 2]]})
        self.assertEqual([(r["state_class"], r["outcome"]) for r in recs],
                         [("first-shot-fresh-boot", "under"), ("post-park", "under")])

    def test_in_flight_queue_boot_is_armed(self):
        text = "######## Q7 [[331,-60,3,80,750,0,8,0,-20,0,150,150],[331,-60,3,80,750,0,8,0,-20,0,220,150]] 04:41:28\n"
        recs, _ = parse_s(text)
        self.assertEqual([(r["phase"], r["step"]["delay"]) for r in recs], [("ARMED", 150)])


class TestIngestDedupe(unittest.TestCase):
    def test_append_new_is_idempotent(self):
        d = tempfile.mkdtemp()
        led = os.path.join(d, "l.jsonl")
        recs, _ = parse_s(FIRSTSHOT_PUSH1)
        self.assertEqual(ledger.append_new(led, recs), (3, 0))
        recs, _ = parse_s(FIRSTSHOT_PUSH1)
        self.assertEqual(ledger.append_new(led, recs), (0, 3))
        with open(led) as f:
            self.assertEqual(len([json.loads(x) for x in f]), 3)


if __name__ == "__main__":
    unittest.main()
