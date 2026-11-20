import datetime as _dt
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from planner_auto import ledger, schedule, steps as S  # noqa: E402
from planner_auto.vid import mv_to_code  # noqa: E402

PUSH2 = "lab-psr1-push2-debug"
CARD = {"card": "e", "floor_mv": 305, "pw_min_us": 47, "latch": {"depth_margin_codes": 1, "energy_margin": 0.10},
        "never": []}
FAMILY = {"name": "t", "card": "e", "state_class": "first-shot-fresh-boot",
          "base": {"td": -60, "pulses": 3, "pw": 80, "vr": 750, "fast": 1, "hold": 0, "t_restore": -20, "revert": 0},
          "primary": {"knob": "code", "start_mv": 450, "coarse_step": 4, "column_step": 1},
          "columns": [], "repeats": 2,
          "policy": {"firmware": [PUSH2], "release_window_us": [0, 4]}, "batch": 1}
_n = [0]


def rec(v, outcome="under", td=-60, pulses=3, pw=80, vr=750, fast=1, hold=0, t_restore=-20, revert=0, delay=117,
        gap=150, state="first-shot-fresh-boot", firmware=PUSH2, release_at=1, boot=None, shot=0, phase="COMPLETE",
        **extra):
    _n[0] += 1
    st = {"v": v, "td": td, "pulses": pulses, "pw": pw, "vr": vr, "fast": fast, "reps": 1, "hold": hold,
          "t_restore": t_restore, "revert": revert, "delay": delay, "gap": gap}
    chk = schedule.check_step(st)
    boot = boot or "e@test-%d" % _n[0]
    r = {"schema": 1, "phase": phase, "attempt_id": "%s#s%03d" % (boot, shot), "card": "e", "boot_id": boot,
         "shot_in_boot": shot, "state_class": state, "firmware": firmware, "step": st, "step_list": S.to_list(st),
         "vid_code": mv_to_code(v), "resolved": chk["resolved"], "write_overlaps_release": chk["overlaps"],
         "overwritten_vout": chk["overwritten"], "release_at": release_at, "release_via": "alarm", "warns": [],
         "outcome": outcome if phase == "COMPLETE" else None, "conditions": {}}
    r.update(extra)
    return r


def ago(seconds):
    return (_dt.datetime.now().astimezone() - _dt.timedelta(seconds=seconds)).isoformat(timespec="seconds")
