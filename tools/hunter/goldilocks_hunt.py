#!/usr/bin/env python3
"""goldilocks_hunt.py: plan, tally and (optionally) run the search for a goldilocks frame on a CMP 170HX 8 GB card,
following docs/SWEET_SPOT.md. Written for a person at the bench or an LLM agent driving it. Standard library only.

It never senses, arms or burns fuses and never writes a fuse register. It writes plan files, reads runner logs and,
only with `run --execute`, calls tools/runner/bootrun.sh, which power-cycles the bench host and runs the per-boot
runner (tools/runner/bisect_shots.py). The runner holds a goldilocks frame; this tool then stops and tells you so.
The active sense, the write-window check and any burn are yours to run with your own tools.

Subcommands (docs/SWEET_SPOT.md explains the order and the reasons):
  train     phase 2: the community train (3 x 198 us, gap 150, delay 117, td -60 so s1 = -177, restore 750, fast
            slew) as a depth ladder, shallow to deep
  readmap   phase 3: map the fuse read: one fast dip, its write starting s1 = +40 .. +160 us after the release
  init      phase 4: the init phase: one fast dip from s1 = -47 (the write ends at the release) down to about -100
  slow      phase 5: the same scan at one code with the card's stock (slow) slew, fast 0
  dwell     one cell, many shots, optionally over several fresh boots
  overlap   phase 6 (push3 firmware only): one long dip with its restore write in flight across the release, so the
            rail is still low at the release and comes back up 0 .. +17 us after it (docs/OVERLAP_EDGE.md)
  check     run the legality model over a step list or a plan file (exit 1 if a step is illegal); push2's release
            rule unless the plan carries "allow_overlap" or you pass --allow-overlap (push3)
  tally     per-cell counts and register codes from runner logs, edges, partial CRC codes and the next phase
  run       drive tools/runner/bootrun.sh through a plan file, one boot at a time (a dry run unless --execute);
            exit 0 plan done, 2 refused, 3 stopped (poison, latch, unknown outcome, budget, STOP file),
            4 a goldilocks or DSOV-0 frame is held
  strategy  print the strategy, the agent loop and the stop rules

A runner step is [v_mV, td_us, pulses, pw_us, v_restore_mV, fast, reps, hold_ms, t_restore_us, revert, delay_us,
gap_us] plus an optional 13th field tv (t_revert_us, when the stock VOUT is written after the train). The dip write
of pulse k is queued at td - delay + k * (pw + gap) and its restore write pw later. s1 is
td - delay, the start of the first dip write relative to the PEX_RST release at t = 0. One PMBus write takes about
47 us and push2 refuses a shot with a write queued at start <= 0 that is still running at 0, so s1 in (-47, 0] is
illegal and s1 = -47 (the write ends exactly at the release) is legal. Every step this tool emits passes that model
(tools/planner/planner_auto/schedule.py, a port of the firmware's timeline) and the runner's limits. The `overlap`
phase is the one exception: it needs push3 (firmware/0011), which runs a write across the release and still releases
on time; its plans carry "allow_overlap": true and are refused on push2 by the firmware itself (glitch ABORT).

Examples:
  goldilocks_hunt.py train --out plan-train.json
  goldilocks_hunt.py run plan-train.json --card g                  # dry run: prints the boots it would run
  goldilocks_hunt.py run plan-train.json --card g --execute --log-dir logs/g-train
  goldilocks_hunt.py tally logs/g-train
  goldilocks_hunt.py readmap --v 375 --out plan-readmap.json
  goldilocks_hunt.py init --depths 450,425,400,388,381,375 --out plan-init.json
  goldilocks_hunt.py slow --v 375 --out plan-slow.json
  goldilocks_hunt.py dwell --v 375 --s1 -55 --fast 0 --reps 2 --boots 4 --out plan-dwell.json
  goldilocks_hunt.py check '[[375,62,1,198,750,0,2,0,-20,0,117,150]]'
  goldilocks_hunt.py overlap --v 388,381,375,369 --out plan-overlap.json      # push3 only
  goldilocks_hunt.py check --allow-overlap '[[381,-60,1,137,625,1,1,0,-20,0,117,150,171]]'
"""
import argparse
import collections
import datetime as _dt
import hashlib
import json
import os
import re
import shlex
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
TOOLS = os.path.dirname(HERE)
sys.path.insert(0, os.path.join(TOOLS, "planner"))
from planner_auto import schedule, steps as S  # noqa: E402
from planner_auto.rules import Rules  # noqa: E402
from planner_auto.vid import canonical_mv, mv_to_code  # noqa: E402

W = schedule.PMBUS_W_MEAS_US            # 47 us per PMBus write
S1_LATEST_BEFORE_RELEASE = -W           # the latest legal start of a write that must end by the release
LAB_MV = (450, 438, 431, 425, 419, 412, 406, 400, 394, 388, 381, 375, 369, 362, 356, 350, 344, 338, 331, 325)
DEFAULT_DELAY, DEFAULT_GAP, DEFAULT_PW, DEFAULT_VR = 117, 150, 198, 750
TRAIN_TD = -60                          # the community train: td -60 at delay 117, s1 = -177
INIT_S1_MIN = -120                      # init-phase scans: s1 from -47 down to about -100
MAX_EDGE_US = 20                        # s1 neighbours further apart than this belong to separate scans
MIN_PHASE_CELLS = 3                     # cells a phase needs before the tally treats it as done
KINDS = ("under", "compass", "stall", "partial", "goldilocks", "poison", "lost", "latch", "other")
SEVERITY = ("goldilocks", "partial", "latch", "poison", "lost", "stall", "compass", "other", "under")
REG_NAMES = ("FUSECTRL", "GFW", "DSOV", "CRC", "PLM", "SENTINEL")

STRATEGY = """\
The strategy (docs/SWEET_SPOT.md has the reasons and the two worked examples, cards G and H):

1. Baseline: identity, VRM profile, cold snapshot, every plan written before the first glitch.
2. The community train first (3 x 198 us, gap 150, td -60, delay 117, restore 750, fast 1), shallow to deep, no
   parks between shots:  goldilocks_hunt.py train
   A DSOV-0 frame (card G: a GFW stall with DSOV 0, CRC 0x77, sentinel 0): dwell there. Card G hit two shots later.
3. A non-monotonic map (a deeper code reads under between poison and lost codes): stop dwelling and map the read
   at the poison-edge code: one fast dip, s1 +40 .. +160 in 5 us steps, 3 or 4 shots each:
   goldilocks_hunt.py readmap --v <code>         Expect block codes: CRC 0x57 (block 0 failed), 0x75 (block 2).
4. The init phase: one fast dip from s1 -47 (the write ends at the release) down to about -100, shallow to deep:
   goldilocks_hunt.py init
   Look for sentinel 0 with CRC 0x55 (the init was disturbed), then the poison edge, 1 to 3 us wide.
5. At the code where s1 near -47 reads under and a few us earlier is poison (card H: 375 mV), switch to the slow
   slew (fast 0) and step s1 from -47 to -100: the rail is mid-fall at the release:  goldilocks_hunt.py slow --v <code>
6. Under then straight to link loss, CRC44 frames (DSOV 0 with fuse blocks 0 and 2 skipped) or 'over' frames, and
   nothing in between, on every shape above: flash push3 (firmware/0011, docs/PUSH3.md) and scan the release edge
   itself, one long dip whose restore write is in flight across the release:  goldilocks_hunt.py overlap
   Card E (ceiling 110) went from 2,200 shots without a goldilocks to one on the 13th overlap shot: 381 mV, dip write
   at -177 us, restore write -40 .. +7 us (rail back up about 7 us after the release), restore 625, tv 171.
7. Hold any goldilocks frame for the active sense and the window check. Only a cold boot verifies a burn.

Shot budget: each poison or latch costs a power cycle; edges are 1 us sharp, so use 1 or 2 us steps near an edge and
3 or 4 shots per cell. The first shot of every boot starts from the GPU's own rail control and does not count.

The agent loop: plan one phase -> `run` it (dry run first, then --execute) -> `tally` the logs -> read the "Next"
block -> plan again. Stop and report to the owner on: a goldilocks hold (never sense or burn it yourself), a partial
or stall hold, a controller latch, an unknown outcome (nack, no dip, mistimed, an exit code or line you do not know,
a Python error), and by default on poison. Ask the owner before SHOOT_THROUGH_STALLS (--shoot-through), before
--on-poison continue, and before anything physical."""


# ---------------------------------------------------------------- steps and legality

def make_step(v, s1, np_=1, pw=DEFAULT_PW, vr=DEFAULT_VR, fast=1, reps=3, delay=DEFAULT_DELAY, gap=DEFAULT_GAP):
    """A full 12-field runner step whose first dip write starts at s1 (td = s1 + delay)."""
    return [int(v), int(s1) + int(delay), int(np_), int(pw), int(vr), int(fast), int(reps), 0, -20, 0, int(delay),
            int(gap)]


def s1_of(step):
    d = S.from_list(step)
    return d["td"] - d["delay"]


def writes(step):
    """[(name, scheduled, start, end)] for the shot's PMBus writes on push2's queue model."""
    d = S.from_list(step)
    b = schedule.build_for_step(d)
    names = dict(schedule.ACT_NAMES)
    out = []
    for t, start, act in schedule.queued(b["steps"]):
        out.append((names.get(act, str(act)), t, start, start + W))
    return out


def problems(step, floor, pw_min=W, allow_overlap=False):
    """Why the runner, push2 (push3 with allow_overlap) or the never-rules would refuse this step (empty = legal)."""
    if not isinstance(step, (list, tuple)) or len(step) < 2:
        return ["not a step list: %r" % (step,)]
    why = Rules({"floor_mv": floor, "pw_min_us": pw_min, "allow_overlap": allow_overlap}).check(S.from_list(step))
    d = S.from_list(step)
    if d["pulses"] < 1 or d["pulses"] > 16:
        why.append("pulses %d outside 1..16" % d["pulses"])
    if d["reps"] is not None and d["reps"] < 1:
        why.append("reps %d below 1" % d["reps"])
    return why


def describe_step(step):
    d = S.from_list(step)
    w = writes(step)
    vout = [x for x in w if x[0] == "VOUT" and x[1] > -1000]
    dips = ", ".join("%+d..%+d" % (x[2], x[3]) for x in vout[:2 * d["pulses"]])
    return "%d mV (code %d) %dx%d us gap %d restore %d fast %d s1 %+d (td %d delay %d) reps %s; train writes %s" % (
        d["v"], mv_to_code(d["v"]), d["pulses"], d["pw"], d["gap"], d["vr"], d["fast"], d["td"] - d["delay"], d["td"],
        d["delay"], d["reps"], dips)


def lab_mv(code):
    return canonical_mv(code, prefer=LAB_MV)


def ladder(frm, to):
    """VID codes from frm mV to to mV (both included), one 6.25 mV step apart, as the lab spells them."""
    a, b = mv_to_code(frm), mv_to_code(to)
    step = -1 if b < a else 1
    return [lab_mv(c) for c in range(a, b + step, step)]


def s1_range(frm, to, step):
    step = abs(int(step)) or 1
    if to < frm:
        return list(range(int(frm), int(to) - 1, -step))
    return list(range(int(frm), int(to) + 1, step))


def parse_list(text, cast=int):
    return [cast(x) for x in str(text).replace(" ", "").split(",") if x != ""]


# ---------------------------------------------------------------- plans

def base_env(a):
    env = {"REPEAT": 2, "FLOOR": a.floor, "LOST_NOPARK": 1, "PARK_EACH": 0, "GFW_WAIT": a.gfw_wait,
           "SHOOT_THROUGH_STALLS": 1 if a.shoot_through else 0, "STALLRUN": a.stallrun,
           "PARTIAL_CONTINUE": 1 if a.continue_partials else 0, "BAD0_FOLLOWUP": 1, "LOSTRUN": a.lostrun,
           "SHOTCAP": 450}
    if getattr(a, "continue_sent0", False):
        env["SENT0_CONTINUE"] = 1
    if getattr(a, "trace", False):
        env["TRACE"] = 1
    return env


def new_plan(a, phase, label, columns, on_poison, notes):
    warm = None
    extra = False
    if a.warmup == "extra":
        extra = True
    elif a.warmup != "none":
        try:
            warm = S.to_list(S.from_list(json.loads(a.warmup)))   # all 12 fields; one shot unless field 7 says more
        except (ValueError, TypeError):
            raise SystemExit("--warmup takes extra, none or a step as JSON, e.g. '[369,-60,3,198,750,1,1]'")
    plan = {"tool": "goldilocks_hunt", "version": 1, "phase": phase, "label": label,
            "created": _dt.datetime.now().strftime("%Y-%m-%dT%H:%M:%S"),
            "env": base_env(a), "warmup": warm, "extra_first_rep": extra, "on_poison": on_poison,
            "max_shots_per_boot": a.max_shots_per_boot, "columns": columns, "notes": list(notes)}
    if getattr(a, "allow_overlap", False):
        plan["allow_overlap"] = True
        plan["firmware"] = "push3"
        plan["notes"].append("allow_overlap: some writes run across the PEX_RST release. Needs push3 firmware "
                             "(fw=lab-psr1-push3, firmware/0011); push2 refuses these shots with a glitch ABORT line "
                             "and the runner stops (exit 2) without firing.")
    if a.shoot_through:
        plan["notes"].append("SHOOT_THROUGH_STALLS=1: shots fire while GFW is stalled. This breaks rule 2 of upstream "
                             "pico-ga100vfi (never g, r/j or FLR while GFW boot status is 0x01) and can wedge the GFW "
                             "boot sequencer; a host power cycle recovered it every time on the lab's bench. Get the "
                             "card owner's OK first.")
    else:
        plan["notes"].append("Shoot-through is off: every GFW stall ends its boot (a power cycle each). Card H's maps "
                             "were taken with SHOOT_THROUGH_STALLS=1 (--shoot-through, ask the owner first).")
    return plan


def check_plan(plan, floor=None, pw_min=W):
    """[(where, step, reasons)] for every illegal step of a plan (warm-up included)."""
    fl = plan.get("env", {}).get("FLOOR", 0) if floor is None else floor
    ov = bool(plan.get("allow_overlap"))
    bad = []
    if plan.get("warmup"):
        why = problems(plan["warmup"], fl, pw_min, ov)
        if why:
            bad.append(("warm-up", plan["warmup"], why))
    for ci, col in enumerate(plan.get("columns", [])):
        for si, st in enumerate(col.get("steps", [])):
            why = problems(st, fl, pw_min, ov)
            if why:
                bad.append(("column %d step %d" % (ci, si), st, why))
    return bad


def build_columns(a, cells, group_by, new_boot=False):
    """cells: [(group_key, label, step)]; keeps only legal steps, grouped into columns. Returns (columns, skipped)."""
    cols, skipped, order = collections.OrderedDict(), [], []
    for key, label, st in cells:
        why = problems(st, a.floor, a.pw_min, getattr(a, "allow_overlap", False))
        if why:
            skipped.append((st, why))
            continue
        if key not in cols:
            cols[key] = {"label": label, "steps": []}
            if new_boot:
                cols[key]["new_boot"] = True
            order.append(key)
        cols[key]["steps"].append(st)
    return [cols[k] for k in order], skipped


def emit(a, plan, skipped):
    bad = check_plan(plan, pw_min=a.pw_min)
    if bad:   # cannot happen: build_columns filtered them; kept as a guard
        for where, st, why in bad:
            print("ILLEGAL %s %s: %s" % (where, st, "; ".join(why)), file=sys.stderr)
        raise SystemExit(2)
    if not any(c["steps"] for c in plan["columns"]):
        for st, why in skipped:
            print("skipped %s: %s" % (st, "; ".join(why)), file=sys.stderr)
        raise SystemExit("no legal step left in this plan")
    text = json.dumps(plan, indent=1)
    if a.out:
        with open(a.out, "w") as f:
            f.write(text + "\n")
    if a.json:
        print(text)
        return
    n_steps = sum(len(c["steps"]) for c in plan["columns"])
    shots = sum(st[6] for c in plan["columns"] for st in c["steps"])
    print("PLAN %s: %s" % (plan["phase"], plan["label"]))
    print("  %d steps in %d columns, %d shots before warm-ups; poison policy for `run --on-poison continue`: %s"
          % (n_steps, len(plan["columns"]), shots, plan["on_poison"]))
    print("  runner env: %s" % " ".join("%s=%s" % kv for kv in plan["env"].items()))
    if plan["warmup"]:
        print("  warm-up step at the start of every boot: %s" % json.dumps(plan["warmup"]))
    elif plan["extra_first_rep"]:
        print("  the first step of every boot gets one extra shot (the first shot of a boot does not count)")
    for c in plan["columns"]:
        print("  [%s]%s" % (c["label"], " (fresh boot)" if c.get("new_boot") else ""))
        for st in c["steps"]:
            print("    %s" % json.dumps(st, separators=(",", ":")))
    for st, why in skipped:
        print("  skipped (illegal) %s: %s" % (json.dumps(st, separators=(",", ":")), "; ".join(why)))
    for n in plan["notes"]:
        print("  note: %s" % n)
    if a.out:
        print("  written to %s" % a.out)
        print("  next: %s run %s --card <card>        (dry run; add --execute to run it)"
              % (os.path.relpath(__file__), a.out))
    else:
        print("  (add --out FILE to write the plan for `run`; --json prints the plan itself)")


def cmd_train(a):
    depths = parse_list(a.depths) if a.depths else ladder(a.from_mv, a.to_mv)
    td = a.td if a.td is not None else TRAIN_TD
    cells = []
    for v in depths:
        st = [int(v), td, a.np, a.pw, a.vr, a.fast, a.reps, 0, -20, 0, a.delay, a.gap]
        cells.append((v, "%d mV (code %d), %dx%d s1 %+d" % (v, mv_to_code(v), a.np, a.pw, td - a.delay), st))
    cols, skipped = build_columns(a, cells, "v")
    notes = ["Shallow to deep. A DSOV-0 frame at any depth: dwell there (card G hit two shots after one).",
             "A non-monotonic map (an under code between poison and lost codes) means stop dwelling and map the read "
             "(readmap) at the poison-edge code, as on card H."]
    if a.continue_partials:
        notes.append("PARTIAL_CONTINUE=1: readable DSOV-0 frames are logged and shooting goes on (card G's recipe).")
    emit(a, new_plan(a, "train", "community train ladder %s mV" % ",".join(map(str, depths)), cols, "next_step",
                     notes), skipped)


def cmd_readmap(a):
    vs = parse_list(a.v)
    s1s = s1_range(a.s1_from, a.s1_to, a.s1_step)
    cells = [(v, "%d mV (code %d) one fast dip, s1 %+d..%+d" % (v, mv_to_code(v), s1s[0], s1s[-1]),
              make_step(v, s1, 1, a.pw, a.vr, 1, a.reps, a.delay, a.gap)) for v in vs for s1 in s1s]
    cols, skipped = build_columns(a, cells, "v")
    notes = ["Expect GFW stalls with block codes where the dip lands in the fuse read: CRC 0x57 = block 0 failed, "
             "0x75 = block 2 failed. Only the falling edge matters. No post-release shot moved DSOV on card H: this "
             "map tells you where the read is, the next phase is init."]
    emit(a, new_plan(a, "readmap", "fuse-read window map at %s mV" % ",".join(map(str, vs)), cols, "next_step", notes),
         skipped)


def cmd_init(a):
    depths = parse_list(a.depths)
    s1s = s1_range(a.s1_from, a.s1_to, a.s1_step)
    cells = [(v, "%d mV (code %d) one fast dip, s1 %+d..%+d" % (v, mv_to_code(v), s1s[0], s1s[-1]),
              make_step(v, s1, 1, a.pw, a.vr, 1, a.reps, a.delay, a.gap)) for v in depths for s1 in s1s]
    cols, skipped = build_columns(a, cells, "v")
    notes = ["Each depth is a column, s1 from the release edge (-47) to earlier. A poison at one s1 ends that depth's "
             "column under `run --on-poison continue`: the earlier s1 values poison too.",
             "Look for stalls with sentinel 0 and CRC 0x55 (the init was disturbed), then the poison edge. The code "
             "where s1 near -47 reads under and a few us earlier is poison is the one for the slow phase."]
    emit(a, new_plan(a, "init", "init-phase release-edge scan %s mV" % ",".join(map(str, depths)), cols, "next_column",
                     notes), skipped)


def cmd_slow(a):
    vs = parse_list(a.v)
    s1s = s1_range(a.s1_from, a.s1_to, a.s1_step)
    cells = [(v, "%d mV (code %d) one SLOW dip (fast 0), s1 %+d..%+d" % (v, mv_to_code(v), s1s[0], s1s[-1]),
              make_step(v, s1, 1, a.pw, a.vr, 0, a.reps, a.delay, a.gap)) for v in vs for s1 in s1s]
    cols, skipped = build_columns(a, cells, "v")
    notes = ["The stock slew leaves the rail still falling at the release, between the codes. Card H's goldilocks: "
             "375 mV, fast 0, pw 198, restore 750, s1 -55 (td 62, delay 117), the first counted shot of a fresh boot.",
             "Poison here came and went between boots on card H (381 mV: poison at -60 and -80 in some boots only), "
             "so a poison does not end the column."]
    emit(a, new_plan(a, "slow", "slow-slew edge scan %s mV" % ",".join(map(str, vs)), cols, "next_step", notes),
         skipped)


def cmd_dwell(a):
    if a.s1 is None and a.td is None:
        raise SystemExit("dwell needs --s1 (or --td with --delay)")
    s1 = a.s1 if a.s1 is not None else a.td - a.delay
    st = make_step(a.v, s1, a.np, a.pw, a.vr, a.fast, a.reps, a.delay, a.gap)
    if getattr(a, "tv", 0):
        st = list(st) + [int(a.tv)]
    why = problems(st, a.floor, a.pw_min, getattr(a, "allow_overlap", False))
    if why:
        raise SystemExit("illegal dwell cell %s: %s" % (json.dumps(st), "; ".join(why)))
    label = "dwell %d mV (code %d) %dx%d s1 %+d fast %d restore %d" % (a.v, mv_to_code(a.v), a.np, a.pw, s1, a.fast, a.vr)
    cols = [{"label": "%s, boot %d" % (label, b + 1), "steps": [list(st)], "new_boot": True} for b in range(a.boots)]
    notes = ["One column per fresh boot. Card G hit at its dwell setting after about 100 shots over a day; card H hit "
             "on the first counted shot of a fresh boot."]
    emit(a, new_plan(a, "dwell", label, cols, "next_column", notes), [])


def overlap_cells(a):
    """[(v, label, step)] for the overlap phase: per depth, per dip-write start s1 (one long dip, fall about 47 us
    later), restore write rw = rise - 47 so the rail comes back up at `rise` us after the release; tv pinned so the
    stock 787.5 mV write lands at the same time on every shot and only the restore edge moves."""
    cells, out = [], []
    rises = parse_list(a.rises)
    if a.anchor:
        rises = rises + [W + 1]          # restore write at +1 us: legal on push2, rail back at +48 (card E: CRC44)
    for v in parse_list(a.v):
        for s1 in parse_list(a.s1):
            for rise in rises:
                rw = rise - W
                pw = rw - s1
                st = make_step(v, s1, 1, pw, a.vr, a.fast, a.reps, a.delay, a.gap) + [int(a.tv)]
                b = schedule.build_for_step(S.from_list(st))
                if b["abort"] or b["resolved"]["post"] != int(a.tv):
                    raise SystemExit("tv %d does not pin the stock write for %s (post %s): raise --tv"
                                     % (a.tv, json.dumps(st), b["resolved"].get("post")))
                cells.append((v, "%d mV (code %d) one long dip, write s1 %+d, rail back up %+d us after the release"
                              % (v, mv_to_code(v), s1, rise), st))
    return cells


def cmd_overlap(a):
    a.allow_overlap = True
    cols, skipped = build_columns(a, overlap_cells(a), "v")
    notes = ["push3 only: the restore write is in flight across the release (rail still low at t = 0, back up a few us "
             "after it). push2 aborts these shots (glitch ABORT) before firing.",
             "Card E: rail back up 0 us = under, +7 .. +37 us = over (DSOV 0, blocks wiped), +43 us = light stall, +48 us "
             "= under or CRC44; the goldilocks was at +7 us with the dip write at s1 -177 (381 mV, restore 625, tv 171).",
             "Check every shot's release_at=0 via=alarm line (the runner scores nothing else) and, with --trace, that the "
             "NVVDD time below 450 mV grows with pw (about pw - 3 us on card E).",
             "Fragile cards: no conditioning or warm-up shot at a deep code (use --warmup none or extra)."]
    emit(a, new_plan(a, "overlap", "release-edge overlap scan %s mV, s1 %s, rise %s us" % (a.v, a.s1, a.rises),
                     cols, "next_step", notes), skipped)


def cmd_check(a):
    src = a.steps
    if os.path.exists(src):
        with open(src) as f:
            src = f.read()
    obj = json.loads(src)
    if isinstance(obj, dict):
        items = []
        if obj.get("warmup"):
            items.append(("warm-up", obj["warmup"]))
        for ci, col in enumerate(obj.get("columns", [])):
            items += [("column %d step %d" % (ci, si), st) for si, st in enumerate(col.get("steps", []))]
        floor = a.floor if a.floor is not None else obj.get("env", {}).get("FLOOR", 0)
        a.allow_overlap = a.allow_overlap or bool(obj.get("allow_overlap"))
    else:
        if obj and not isinstance(obj[0], list):
            obj = [obj]
        items = [("step %d" % i, st) for i, st in enumerate(obj)]
        floor = a.floor if a.floor is not None else 0
    nbad = 0
    for where, st in items:
        why = problems(st, floor, a.pw_min, a.allow_overlap)
        nbad += bool(why)
        print("%-18s %-8s %s" % (where, "ILLEGAL" if why else "ok", json.dumps(st, separators=(",", ":"))))
        if a.verbose or why:
            try:
                print("    %s" % describe_step(st))
            except Exception:
                pass
        for w in why:
            print("    - %s" % w)
    print("%d of %d steps legal (floor %d mV, 47 us per PMBus write, %s)" % (
        len(items) - nbad, len(items), floor,
        "push3: writes across the release allowed" if a.allow_overlap else "push2 release rule"))
    return 1 if nbad else 0


# ---------------------------------------------------------------- log parsing (tally)

RE_RUN = re.compile(r"^pico: ")
RE_SHOT = re.compile(r"^SHOT (.*?) \| (.*)$")
RE_ECHO = re.compile(r"^\s*glitch vd=(-?\d+) td=(-?\d+) vr=(-?\d+) tr=(-?\d+) tv=(-?\d+) fast=(\d+) hold=(\d+) "
                     r"pw=(\d+) gap=(\d+) delay=(\d+)")
RE_STEP = re.compile(r"^step (\d+)(?: shot (\d+))? v=(-?\d+) td=(-?\d+) pw=(-?\d+) vr=(-?\d+) f=(\d+): (.*)$")
RE_CELL = re.compile(r"^cell (\d+)(?: \([^)]*\))? v=(-?\d+) td=(-?\d+) np=(\d+) pw=(-?\d+) vr=(-?\d+): ")
RE_STALL = re.compile(r"^\s*stall read \([^)]*\): (.*)$")
RE_OBS = re.compile(r"^\s*observed(?:, not scored)?: (.*)$")
RE_REG = re.compile(r"\b(FUSECTRL|GFW|DSOV|CRC|PLM|SENTINEL)=(0x[0-9a-fA-F]+|-)")
RE_TIMING = re.compile(r"^\s*timing i=\d+ scheduled=(-?\d+) act=(\d+) arg=(-?\d+)")
RE_PARKED = re.compile(r"^\s*park after lost shot: ")
RE_HOLD = re.compile(r"^>>> (GOLDILOCKS HELD|STALL DSOV0 HELD|PARTIAL HELD)(.*)$")
RE_FP = re.compile(r"^(SHOT |step |cell |TIPPED|\s*glitch |\s*stall read|\s*observed|sentinel baseline|boot pmbus|"
                   r"\s*temp_raw|>>> )")


def regs_of(text):
    return {k: (None if v == "-" else int(v, 16)) for k, v in RE_REG.findall(text or "")}


def kind_of(out, regs=None):
    """Outcome text (the runner's, old or new) -> the tally's class."""
    t = (out or "").strip()
    if t == "under":
        return "under"
    if t.startswith("GOLDILOCKS"):
        return "goldilocks"
    if t.startswith("STALL DSOV0"):
        r = regs or regs_of(t)
        return "goldilocks" if (r.get("CRC") == 0x55 and r.get("PLM") == 0xFFFFFFFF) else "partial"
    if t.startswith("STALL PARTIAL"):
        return "partial"
    if t == "lost" or t.startswith("lost"):
        return "lost"
    if t.startswith(("poisoned", "hidden-poison")):
        return "poison"
    if t.startswith("MISTIMED"):
        return "other"
    if t.startswith("dsov0"):
        return "partial"
    if t.startswith("dsov1 crc=0x") and t.endswith(" priv=0xffffffff"):
        return "compass"
    if t.startswith("booting"):
        return "stall"
    if t.startswith("CONTROLLER OFF"):
        return "latch"
    return "other"


def _np_from_timing(block):
    vd, reset_at = block["v"], -1000
    code = mv_to_code(vd)
    dips = [t for t, act, arg in block["timing"] if act == 1 and t > reset_at and arg == code]
    return len(dips) or None


def parse_run_legacy(lines):
    """Shots of one runner run without SHOT lines (the runner before 2026-11-21, or the lab's): one block per firmware
    echo; cell-mode under shots print nothing, so a block with no outcome line is an under shot."""
    blocks, cells = [], []
    for ln in lines:
        m = RE_ECHO.match(ln)
        if m:
            vd, td, vr, tr, tv, fast, hold, pw, gap, delay = map(int, m.groups())
            blocks.append({"v": vd, "td": td, "vr": vr, "fast": fast, "pw": pw, "gap": gap, "delay": delay,
                           "lines": [], "timing": []})
            continue
        if not blocks:
            continue
        b = blocks[-1]
        b["lines"].append(ln)
        mt = RE_TIMING.match(ln)
        if mt:
            b["timing"].append(tuple(map(int, mt.groups())))
        mc = RE_CELL.match(ln)
        if mc:
            i, v, td, np_, pw, vr = map(int, mc.groups())
            cells.append((len(blocks) - 1, v, td, np_, max(pw, 36), vr))
    shots = []
    parked = False
    for j, b in enumerate(blocks):
        out, regs, stall, special = None, {}, False, None
        for ln in b["lines"]:
            mo = RE_OBS.match(ln)
            if mo:
                for k, v in regs_of(mo.group(1)).items():
                    if v is not None or k not in regs:
                        regs[k] = v
            ms = RE_STALL.match(ln)
            if ms and "(GFW finished)" not in ms.group(1):
                stall = True
                for k, v in regs_of(ms.group(1)).items():
                    if v is not None:
                        regs[k] = v
            m = RE_STEP.match(ln)
            if m:
                text = m.group(8).strip()
                if text.startswith(("STALL PARTIAL", "STALL DSOV0")):
                    special = text
                elif "GOLDILOCKS passive candidate" in text:
                    out = "GOLDILOCKS"
                elif "CONTROLLER OFF" in text:
                    out = text
                elif "NO DIP" in text:
                    out = "NO DIP"
                else:
                    out = text
        if out is None and special:
            out = special
        if out is None:
            out = "under"
        kind = kind_of(out, regs)
        if special and kind not in ("poison", "lost", "goldilocks"):
            kind = kind_of(special, regs)
        if out == "NO DIP" or out.endswith("nack") or out.startswith("controller unreadable"):
            kind = "other"
        np_ = None
        for pos, v, td, n, pw, vr in cells:
            if pos >= j and (v, td, pw, vr) == (b["v"], b["td"], b["pw"], b["vr"]):
                np_ = n
                break
            if pos >= j:
                break
        if np_ is None:
            np_ = _np_from_timing(b)
        if np_ is None:
            same = {n for pos, v, td, n, pw, vr in cells if (v, td, pw, vr) == (b["v"], b["td"], b["pw"], b["vr"])}
            np_ = same.pop() if len(same) == 1 else None
        shots.append({"n": j, "first": j == 0, "after_park": parked, "stall": stall, "kind": kind,
                      "v": b["v"], "td": b["td"], "np": np_, "pw": b["pw"], "vr": b["vr"], "fast": b["fast"],
                      "delay": b["delay"], "gap": b["gap"], "s1": b["td"] - b["delay"], "regs": regs, "out": out})
        parked = any(RE_PARKED.match(ln) for ln in b["lines"])
    known = collections.defaultdict(set)   # a block with no count line and no timing takes its geometry's known count
    for s in shots:
        if s["np"] is not None:
            known[(s["v"], s["td"], s["pw"], s["vr"], s["fast"], s["delay"], s["gap"])].add(s["np"])
    for s in shots:
        k = known.get((s["v"], s["td"], s["pw"], s["vr"], s["fast"], s["delay"], s["gap"]))
        if s["np"] is None and k and len(k) == 1:
            s["np"] = next(iter(k))
    return shots


def parse_run_shotlines(lines):
    shots = []
    for ln in lines:
        m = RE_SHOT.match(ln)
        if not m:
            continue
        f = dict(x.split("=", 1) for x in m.group(1).split() if "=" in x)
        regs = {k: (None if f.get(k, "-") == "-" else int(f[k], 16)) for k in REG_NAMES}
        kind = f.get("kind", "other")
        if kind not in KINDS:
            kind = "other"
        shots.append({"n": int(f["n"]), "first": f.get("first") == "1", "after_park": f.get("after_park") == "1",
                      "stall": f.get("stall") == "1", "kind": kind, "v": int(f["v"]), "td": int(f["td"]),
                      "np": int(f["np"]), "pw": int(f["pw"]), "vr": int(f["vr"]), "fast": int(f["fast"]),
                      "delay": int(f["delay"]), "gap": int(f["gap"]), "s1": int(f["s1"]), "tv": int(f.get("tv", "0")),
                      "regs": regs,
                      "out": m.group(2)})
    return shots


def split_runs(text):
    runs, cur = [], None
    for ln in text.splitlines():
        if RE_RUN.match(ln):
            cur = []
            runs.append(cur)
        if cur is not None:
            cur.append(ln)
    return runs


def log_files(paths):
    out = []
    for p in paths:
        if os.path.isdir(p):
            for dp, dn, fn in os.walk(p):
                dn.sort()
                for f in sorted(fn):
                    out.append(os.path.join(dp, f))
        elif os.path.exists(p):
            out.append(p)
        else:
            raise SystemExit("no such file or directory: %s" % p)
    return out


def read_logs(paths):
    """All shots and holds from runner logs (files or directories). The same run found twice in files of the same
    name stem (a runner log and the launcher output that contains it: boot-03.log and boot-03.out, x.raw and
    x.raw.launcher) is read once."""
    shots, holds, seen = [], [], set()
    stats = {"files": 0, "runs": 0, "dup_runs": 0}
    for path in log_files(paths):
        try:
            with open(path, "r", encoding="utf-8", errors="replace") as f:
                text = f.read()
        except OSError:
            continue
        runs = split_runs(text)
        if not runs:
            continue
        stats["files"] += 1
        for ri, lines in enumerate(runs):
            stem = os.path.join(os.path.dirname(os.path.abspath(path)), os.path.basename(path).split(".")[0])
            fp = (stem, hashlib.sha1("\n".join(ln.strip() for ln in lines if RE_FP.match(ln)).encode()).hexdigest())
            if fp in seen:
                stats["dup_runs"] += 1
                continue
            seen.add(fp)
            stats["runs"] += 1
            rs = parse_run_shotlines(lines) if any(RE_SHOT.match(ln) for ln in lines) else parse_run_legacy(lines)
            for s in rs:
                s["src"] = "%s#%d" % (path, ri + 1)
            shots.extend(rs)
            for ln in lines:
                mh = RE_HOLD.match(ln)
                if mh:
                    holds.append({"what": mh.group(1), "text": ln.strip(), "src": "%s#%d" % (path, ri + 1),
                                  "shot": rs[-1] if rs else None})
    return shots, holds, stats


# ---------------------------------------------------------------- tally

def crc_label(crc):
    """CRC_STATUS = four 2-bit fields, 01 = pass. Card H only ever moved fields 0 and 2 (blocks 0 and 2)."""
    if crc is None:
        return "?"
    if crc == 0x55:
        return "all blocks pass"
    if crc == 0:
        return "all fields 00 (over)"
    parts = []
    for k in range(4):
        f = (crc >> (2 * k)) & 3
        if f == 3:
            parts.append("block %d failed" % k)
        elif f == 0:
            parts.append("block %d reads 00" % k)
        elif f == 2:
            parts.append("block %d reads 10" % k)
    return ", ".join(parts)


def crc_partial(crc):
    """A code with a field that is neither pass (01) nor failed (11): seen near a threshold on card H."""
    return crc is not None and crc not in (0, 0x55) and any(((crc >> (2 * k)) & 3) in (0, 2) for k in range(4))


def cell_key(s):
    return (s["np"], s["pw"], s["gap"], s["vr"], s["fast"], s["v"], s["s1"])


def geometry_of(key):
    np_, pw, gap, vr, fast = key[:5]
    return "%sx%d us%s, restore %d, %s slew" % ("?" if np_ is None else np_, pw,
                                               (" gap %d" % gap) if (np_ or 1) > 1 or gap != DEFAULT_GAP else "",
                                               vr, "fast" if fast else "stock (slow)")


def phase_of(key):
    np_, pw, gap, vr, fast, v, s1 = key
    if np_ == 1 and -W < s1 + pw <= 0:
        return "overlap"     # restore write in flight across the release (push3)
    if np_ is not None and np_ >= 2:
        return "train"
    if s1 > 0:
        return "readmap"
    if INIT_S1_MIN <= s1 <= S1_LATEST_BEFORE_RELEASE:
        return "init" if fast else "slow"
    return "other"


def tally_cells(shots, skip_after_park=False):
    cells = collections.OrderedDict()
    def order(x):
        return tuple(-1 if f is None else f for f in cell_key(x)[:5]) + (-x["v"], x["s1"])
    for s in sorted(shots, key=order):
        k = cell_key(s)
        c = cells.setdefault(k, {"key": k, "n": 0, "counts": collections.Counter(), "first": collections.Counter(),
                                 "regs": {r: collections.Counter() for r in REG_NAMES}, "flags": set(),
                                 "init_disturbed": 0, "dsov0": 0, "dsov0_full": 0, "dsov0_over": 0,
                                 "stall_codes": collections.Counter()})
        if s["first"] or (skip_after_park and s["after_park"]):
            c["first"][s["kind"]] += 1
            continue
        c["n"] += 1
        c["counts"][s["kind"]] += 1
        r = s["regs"]
        for name in REG_NAMES:
            if r.get(name) is not None:
                c["regs"][name][r[name]] += 1
        if r.get("DSOV") == 0 and s["kind"] != "goldilocks":
            c["dsov0"] += 1
            if is_over(r):
                c["dsov0_over"] += 1
            else:
                c["dsov0_full"] += 1
        if r.get("DSOV") == 1 and r.get("CRC") == 0x55 and r.get("SENTINEL") == 0:
            c["init_disturbed"] += 1
        if s["kind"] in ("stall", "compass", "partial") and r.get("CRC") is not None:
            c["stall_codes"][r["CRC"]] += 1
    for c in cells.values():
        c["dominant"] = dominant(c["counts"])
        c["sig"] = signature(c)
        crcs = [x for x in c["regs"]["CRC"] if x != 0x55]
        if c["dsov0_full"]:
            c["flags"].add("DSOV 0 with a full mask")
        if c["dsov0_over"]:
            c["flags"].add("DSOV 0 over (CRC 0x00 or mask not all ones)")
        if c["init_disturbed"]:
            c["flags"].add("sentinel 0 with CRC 0x55 and DSOV 1: init disturbed")
        if any(crc_partial(x) for x in crcs):
            c["flags"].add("partial CRC code")
        if len(crcs) > 1 or (crcs and c["counts"]["under"]) or len([k for k, v in c["counts"].items() if v]) > 1:
            c["flags"].add("mixed: near an edge")
        if c["counts"]["latch"]:
            c["flags"].add("controller latch")
    return list(cells.values())


def is_over(r):
    """DSOV 0 read as over: CRC 0x00 or a privilege mask that is not all ones (too deep; never sense such a frame)."""
    return r.get("CRC") == 0 or (r.get("PLM") is not None and r.get("PLM") != 0xFFFFFFFF)


def dominant(counts):
    if not sum(counts.values()):
        return None
    top = max(counts.values())
    for k in SEVERITY:
        if counts.get(k) == top:
            return k
    return None


def signature(c):
    d = c["dominant"]
    if d in ("stall", "compass", "partial") and c["stall_codes"]:
        code = c["stall_codes"].most_common(1)[0][0]
        return "%s/0x%02x" % (d, code & 0xFF)
    return d or "-"


def edges(cells):
    """Neighbouring cells with different signatures, along s1 (fixed depth) and along depth (fixed s1)."""
    out = []
    by_line = collections.defaultdict(list)
    by_depth = collections.defaultdict(list)
    for c in cells:
        if not c["n"]:
            continue
        k = c["key"]
        by_line[k[:6]].append(c)
        by_depth[k[:5] + (k[6],)].append(c)
    for line, cs in by_line.items():
        cs.sort(key=lambda c: -c["key"][6])
        for a, b in zip(cs, cs[1:]):
            sa, sb = a["key"][6], b["key"][6]
            if (sa > 0) != (sb > 0) or abs(sa - sb) > MAX_EDGE_US:
                continue   # cells on both sides of the release, or two separate scans
            if a["sig"] != b["sig"]:
                out.append({"axis": "s1", "geometry": geometry_of(a["key"]), "v": a["key"][5],
                            "a": a["key"][6], "b": b["key"][6], "sig_a": a["sig"], "sig_b": b["sig"],
                            "width": abs(a["key"][6] - b["key"][6])})
    for line, cs in by_depth.items():
        cs.sort(key=lambda c: -c["key"][5])
        for a, b in zip(cs, cs[1:]):
            if a["sig"] != b["sig"]:
                out.append({"axis": "v", "geometry": geometry_of(a["key"]), "s1": a["key"][6],
                            "a": a["key"][5], "b": b["key"][5], "sig_a": a["sig"], "sig_b": b["sig"],
                            "width": abs(mv_to_code(a["key"][5]) - mv_to_code(b["key"][5]))})
    return out


def nonmonotonic(cells):
    """Depth lines (fixed geometry and s1) where an under cell sits between non-under cells on both sides."""
    res = []
    by_depth = collections.defaultdict(list)
    for c in cells:
        if c["n"]:
            by_depth[c["key"][:5] + (c["key"][6],)].append(c)
    for line, cs in by_depth.items():
        cs.sort(key=lambda c: -c["key"][5])
        doms = [c["dominant"] for c in cs]
        for i, d in enumerate(doms):
            if d == "under" and any(x not in ("under", None) for x in doms[:i]) and \
                    any(x in ("poison", "lost", "latch", "stall") for x in doms[i + 1:]):
                res.append({"geometry": geometry_of(cs[i]["key"]), "s1": line[5],
                            "map": [(c["key"][5], c["dominant"]) for c in cs], "under_at": cs[i]["key"][5]})
                break
    return res


def fmt_codes(c):
    parts = []
    for name, lab, fmt in (("CRC", "CRC", "%02x"), ("DSOV", "DSOV", "%x"), ("GFW", "GFW", "%x"), ("PLM", "PLM", "%08x"),
                           ("SENTINEL", "SENT", "%x")):
        cnt = c["regs"][name]
        if not cnt:
            continue
        parts.append("%s %s" % (lab, " ".join(("%s:%d" % (fmt % (k & 0xFFFFFFFF), n)) for k, n in sorted(cnt.items()))))
    return "; ".join(parts)


def cmd_args(**kw):
    return " ".join("--%s %s" % (k.replace("_", "-"), v) for k, v in kw.items() if v is not None)


def next_step(cells, shots, holds, edge_list, nonmono):
    """The next phase per docs/SWEET_SPOT.md: (headline, [detail lines], [commands])."""
    me = "goldilocks_hunt.py"
    gold = [s for s in shots if s["kind"] == "goldilocks"] + [h["shot"] for h in holds if h["what"] == "GOLDILOCKS HELD" and h["shot"]]
    gold_holds = [h for h in holds if h["what"] == "GOLDILOCKS HELD" or
                  (h["what"] == "STALL DSOV0 HELD" and "goldilocks signature" in h["text"])]
    if gold or gold_holds:
        s = (gold or [h["shot"] for h in gold_holds if h["shot"]] or [None])[0]
        where = ("%d mV %sx%d s1 %+d fast %d restore %d" % (s["v"], s["np"], s["pw"], s["s1"], s["fast"], s["vr"])) if s else "see the log"
        src = s["src"] if s and "src" in s else (gold_holds[0]["src"] if gold_holds else "")
        in_stall = any(h["what"] == "STALL DSOV0 HELD" for h in gold_holds) or bool(s and s.get("stall"))
        return ("STOP: a goldilocks frame was held (%s, %s)." % (where, src),
                ["Leave the card as it is: no park, no further shot, no power cycle.",
                 "Run YOUR OWN active sense and write-window check on the held state, then decide on a burn with your own "
                 "reviewed tool and plan. Nothing in this repository senses, arms or burns fuses."] +
                (["This frame was held during a GFW stall (STALL DSOV0 HELD): it is not a finished boot; read it before "
                  "anything else."] if in_stall else []) +
                ["Only a cold boot and a new read verify a burn."], [])
    other_holds = [h for h in holds if h not in gold_holds]
    if other_holds:
        h = other_holds[-1]
        s = h["shot"]
        cmd = []
        if s and s.get("np"):
            cmd.append("%s dwell %s --continue-partials" % (me, cmd_args(v=s["v"], s1=s["s1"], np=s["np"], pw=s["pw"],
                                                                         fast=s["fast"], vr=s["vr"], reps=10, boots=2,
                                                                         pw_min=s["pw"] if s["pw"] < W else None)))
        over_txt = []
        if s and is_over(s["regs"]):
            over_txt = ["This frame is over (CRC 0x00 or a mask that is not all ones): too deep, not a lead-up sign."]
        return ("STOP: a DSOV-0 frame is held (%s, %s)." % (h["what"], h["src"]),
                over_txt + ["Read the held state (vfi_snap.py) and decide; never sense or burn a partial.",
                 "Card G's goldilocks came two shots after a DSOV-0 stall frame at the same setting: to dwell through such "
                 "frames, use PARTIAL_CONTINUE=1 (--continue-partials), and for stall frames also SHOOT_THROUGH_STALLS=1 "
                 "(--shoot-through, the owner's OK first)."], cmd)
    lines = []
    if any(c["counts"]["latch"] for c in cells):
        lat = [c for c in cells if c["counts"]["latch"]]
        lines.append("Controller latch at %s: keep away from that energy (pulses x width at that depth)."
                     % ", ".join("%d mV %sx%d s1 %+d" % (c["key"][5], c["key"][0], c["key"][1], c["key"][6]) for c in lat))
    d0 = [c for c in cells if c["dsov0_full"]]
    over = [c for c in cells if c["dsov0_over"] and not c["dsov0_full"]]
    if not d0 and over:
        c = max(over, key=lambda c: (c["dsov0_over"], c["key"][5]))
        np_, pw, gap, vr, fast, v, s1 = c["key"]
        up = lab_mv(mv_to_code(v) + 1)
        return ("DSOV 0 but over at %d mV %sx%d s1 %+d fast %d (CRC 0x00 or mask not all ones): too deep." % (v, np_, pw, s1, fast),
                lines + ["Never sense such a frame. Card G showed these from a single slow dip at 350 mV and then hit with "
                         "the community train at 369 mV, after a DSOV-0 frame with CRC 0x77 and a full mask."],
                ["%s dwell %s --continue-partials" % (me, cmd_args(v=up, s1=s1, np=np_, pw=pw, fast=fast, vr=vr, reps=10, boots=2,
                                                                   pw_min=pw if pw < W else None)),
                 "%s train" % me])
    if d0:
        c = max(d0, key=lambda c: (c["dsov0_full"], -c["key"][5]))
        np_, pw, gap, vr, fast, v, s1 = c["key"]
        return ("Dwell: DSOV-0 frames with a full mask at %d mV %sx%d s1 %+d fast %d (%d of %d counted shots)." % (v, np_, pw, s1, fast, c["dsov0_full"], c["n"]),
                lines + ["Card G hit two shots after a DSOV-0 stall frame at its dwell setting.",
                         "Use --continue-partials so the runner does not hold every partial; stall frames need --shoot-through "
                         "(owner's OK first)."],
                ["%s dwell %s --continue-partials" % (me, cmd_args(v=v, s1=s1, np=np_, pw=pw, fast=fast, vr=vr, gap=gap if (np_ or 1) > 1 else None,
                                                                   reps=20, boots=3, pw_min=pw if pw < W else None))])
    phases = collections.defaultdict(list)
    for c in cells:
        if c["n"]:
            phases[phase_of(c["key"])].append(c)
    for ph, fn in (("slow", _next_slow), ("init", _next_init), ("readmap", _next_readmap)):
        sl = scan_lines(phases.get(ph, []))
        if sl:
            return fn(sl, lines, me)
    if phases.get("train"):
        return _next_train(phases["train"], nonmono, lines, me)
    return ("Start with the community train (phase 2).", lines, ["%s train --out plan-train.json" % me])


def _by_v(cs):
    d = collections.defaultdict(list)
    for c in cs:
        d[c["key"][5]].append(c)
    for v in d:
        d[v].sort(key=lambda c: -c["key"][6])
    return d


def scan_lines(cs):
    """{v: cells sorted from the latest s1 to the earliest} for the geometry with the most cells, keeping only depths
    scanned over at least MIN_PHASE_CELLS s1 values: a scan, not a stray cell."""
    by_geo = collections.defaultdict(list)
    for c in cs:
        by_geo[c["key"][:5]].append(c)
    for geo, gcells in sorted(by_geo.items(), key=lambda kv: -len(kv[1])):
        lines = {v: l for v, l in _by_v(gcells).items() if len(l) >= MIN_PHASE_CELLS}
        if lines:
            return lines
    return {}


def _poison_edge(line):
    """(first poison s1 going earlier from the release, the latest s1 after it that is not poison, edge width)."""
    pois = [c for c in line if c["counts"]["poison"]]
    if not pois:
        return None, None, None
    p = pois[0]["key"][6]
    later = [c for c in line if c["key"][6] > p]
    last_ok = later[-1]["key"][6] if later else None
    return p, last_ok, (last_ok - p) if later else None


def _next_slow(byv, lines, me):
    best, deeper, shallower = None, False, False
    for v, line in sorted(byv.items(), key=lambda kv: -kv[0]):
        p, last_ok, width = _poison_edge(line)
        if p is None:
            if all(c["dominant"] == "under" for c in line):
                deeper = True
                lines.append("%d mV slow: all under from s1 %+d to %+d." % (v, line[0]["key"][6], line[-1]["key"][6]))
            else:
                lines.append("%d mV slow: no poison from s1 %+d to %+d (%s)." % (v, line[0]["key"][6], line[-1]["key"][6],
                                                                            ", ".join(sorted({c["sig"] for c in line}))))
            continue
        if last_ok is None:
            shallower = True
            lines.append("%d mV slow: poison already at s1 %+d, the latest scanned." % (v, p))
            continue
        later = [c for c in line if c["key"][6] > p]
        dist = [c for c in later if c["init_disturbed"]]
        c = (dist or later)[-1]
        lines.append("%d mV slow: first poison at s1 %+d (edge %d us); the cell to dwell on is s1 %+d%s." % (
            v, p, width, c["key"][6], " (sentinel 0 with CRC 0x55 there)" if c["init_disturbed"] else ""))
        best = best or c
    if best is not None:
        np_, pw, gap, vr, fast, v, s1 = best["key"]
        return ("Dwell on the slow-slew cell next to the poison edge, in fresh boots (phase 6).", lines +
                ["Card H's goldilocks was the first counted shot of a fresh boot at 375 mV, fast 0, s1 -55.",
                 "Poison came and went between boots at the slow slew on card H: repeat cells across boots before you "
                 "trust an edge."],
                ["%s dwell %s" % (me, cmd_args(v=v, s1=s1, np=1, pw=pw, fast=0, vr=vr, reps=2, boots=4)),
                 "%s slow %s" % (me, cmd_args(v=v, s1_from=s1 + 2, s1_to=s1 - 4, s1_step=1, reps=3))])
    vs = sorted(byv)
    if deeper and not shallower:
        return ("No poison edge in the slow scan yet: one code deeper.", lines,
                ["%s slow --v %d" % (me, lab_mv(mv_to_code(vs[0]) - 1))])
    if shallower and not deeper:
        return ("Poison right from the release edge at the slow slew: one code shallower.", lines,
                ["%s slow --v %d" % (me, lab_mv(mv_to_code(vs[-1]) + 1))])
    return ("The slow scan shows no clear edge: extend it to earlier s1 or repeat it across boots.", lines,
            ["%s slow --v %s --s1-to -120" % (me, ",".join(str(v) for v in sorted(byv, reverse=True)))])


def _next_init(byv, lines, me):
    cand, wide, poisoned_now, clean = None, [], [], []
    for v, line in sorted(byv.items(), key=lambda kv: -kv[0]):
        latest = line[0]
        p, last_ok, width = _poison_edge(line)
        if p is None:
            clean.append(v)
            lines.append("%d mV init: no poison from s1 %+d to %+d (%s)." % (
                v, latest["key"][6], line[-1]["key"][6], ", ".join(sorted({c["sig"] for c in line}))))
            continue
        dist = sum(c["init_disturbed"] for c in line)
        lines.append("%d mV init: s1 %+d reads %s; poison from s1 %+d%s%s." % (
            v, latest["key"][6], latest["sig"], p, (" (edge %d us wide)" % width) if width else "",
            "; %d frames with sentinel 0 and CRC 0x55 (init disturbed)" % dist if dist else ""))
        if last_ok is None:
            poisoned_now.append(v)
            continue
        if width > 2:
            wide.append((v, last_ok, p))
        if latest["dominant"] == "under" and cand is None:
            cand = (v, p, last_ok, width)
    if cand:
        v, p, last_ok, width = cand
        cmds = ["%s slow --v %d" % (me, v)]
        if width and width > 2:
            cmds.insert(0, "%s init %s" % (me, cmd_args(depths=v, s1_from=last_ok, s1_to=p, s1_step=1, reps=3)))
        return ("Slow slew at %d mV (phase 5): s1 near -47 reads under there and a few us earlier is poison." % v,
                lines + (["The edge is wider than 2 us: pin it with 1 us steps first (first command)."] if width and width > 2 else []),
                cmds)
    cmds = ["%s init %s" % (me, cmd_args(depths=v, s1_from=a, s1_to=b, s1_step=1, reps=3)) for v, a, b in wide]
    if poisoned_now and not clean:
        return ("Poison right from the release edge at every depth scanned: go shallower.", lines,
                cmds + ["%s init --depths %s" % (me, ",".join(str(lab_mv(mv_to_code(max(byv)) + k)) for k in (3, 2, 1)))])
    deepest = min(byv)
    return ("No depth yet that reads under at s1 -47 with poison a few us earlier: pin the edges you have and go one "
            "code deeper (card H: 381 mV stalled at -47 with poison from -60; 375 mV read under at -47 with poison "
            "from -58).", lines,
            cmds + ["%s init --depths %s" % (me, ",".join(str(lab_mv(mv_to_code(deepest) - k)) for k in (1, 2)))])


def _next_readmap(byv, lines, me):
    for v, line in sorted(byv.items(), key=lambda kv: -kv[0]):
        line = sorted(line, key=lambda c: c["key"][6])
        runs, cur = [], None
        for c in line:
            if cur and cur[0] == c["sig"]:
                cur[2] = c["key"][6]
            else:
                cur = [c["sig"], c["key"][6], c["key"][6]]
                runs.append(cur)
        lines.append("%d mV read map: %s" % (v, "; ".join("s1 %+d..%+d %s" % (a, b, sig) if a != b else "s1 %+d %s" % (a, sig)
                                                          for sig, a, b in runs)))
    codes = sorted({k & 0xFF for line in byv.values() for c in line for k in c["stall_codes"]})
    if codes:
        lines.append("Block codes: %s." % "; ".join("0x%02x %s" % (k, crc_label(k)) for k in codes))
    v = max(byv)
    depths = sorted(set([450, 425, 400] + [lab_mv(mv_to_code(v) + k) for k in (2, 1, 0)]), reverse=True)
    return ("Move to the init phase (phase 4): DSOV is decided in the first ~50 us after the release, not by the block "
            "reads.", lines, ["%s init --depths %s" % (me, ",".join(map(str, depths)))])


def _next_train(cs, nonmono, lines, me):
    shots_by = collections.Counter()
    for c in cs:
        shots_by[c["key"][:5] + (c["key"][6],)] += c["n"]
    main_line = shots_by.most_common(1)[0][0]          # the train geometry and s1 with the most counted shots
    line = sorted([c for c in cs if c["key"][:5] + (c["key"][6],) == main_line], key=lambda c: -c["key"][5])
    nonmono = [m for m in nonmono if m["s1"] == main_line[5] and m["geometry"] == geometry_of(line[0]["key"])]
    lines.append("Train map, shallow to deep: %s." % ", ".join("%d %s" % (c["key"][5], c["sig"]) for c in line))
    if nonmono:
        lethal = [c for c in line if c["dominant"] == "poison"] or [c for c in line if c["dominant"] in ("stall", "compass")] or \
                 [c for c in line if c["dominant"] in ("lost", "latch")]
        v = lethal[0]["key"][5]
        return ("Non-monotonic map: stop dwelling and map the read at the poison-edge code (phase 3).", lines,
                ["%s readmap --v %d" % (me, v)])
    if all(c["dominant"] == "under" for c in line):
        deepest = line[-1]["key"][5]
        return ("All under: go deeper.", lines, ["%s train --from %d --to %d" % (me, lab_mv(mv_to_code(deepest) - 1), lab_mv(mv_to_code(deepest) - 4))])
    edge = next((i for i, c in enumerate(line) if c["dominant"] != "under"), None)
    last_ok = line[edge - 1] if edge else None
    cmds = []
    if last_ok is not None:
        np_, pw, gap, vr, fast, v, s1 = last_ok["key"]
        cmds.append("%s dwell %s" % (me, cmd_args(v=v, s1=s1, np=np_, pw=pw, gap=gap, fast=fast, vr=vr, reps=20, boots=3)))
    cmds.append("%s readmap --v %d" % (me, line[edge]["key"][5]))
    return ("Monotonic map with an edge at %d mV: dwell just before it; if nothing moves, map the read at the edge code."
            % line[edge]["key"][5], lines + ["Card G needed about 100 shots at its setting over the day."], cmds)


def cmd_tally(a):
    shots, holds, stats = read_logs(a.logs)
    cells = tally_cells(shots, a.skip_after_park)
    el = edges(cells)
    nm = nonmonotonic(cells)
    head, details, cmds = next_step(cells, shots, holds, el, nm)
    if a.json:
        def jc(c):
            return {"v": c["key"][5], "s1": c["key"][6], "np": c["key"][0], "pw": c["key"][1], "gap": c["key"][2],
                    "vr": c["key"][3], "fast": c["key"][4], "phase": phase_of(c["key"]), "counted": c["n"],
                    "counts": dict(c["counts"]), "first_shots": dict(c["first"]), "signature": c["sig"],
                    "codes": {r: {"0x%x" % k: n for k, n in c["regs"][r].items()} for r in REG_NAMES},
                    "flags": sorted(c["flags"])}
        print(json.dumps({"stats": stats, "shots": len(shots), "first_shots": sum(1 for s in shots if s["first"]),
                          "holds": [{k: h[k] for k in ("what", "text", "src")} for h in holds],
                          "cells": [jc(c) for c in cells], "edges": el, "nonmonotonic": nm,
                          "next": {"headline": head, "details": details, "commands": cmds}}, indent=1))
        return 0
    nfirst = sum(1 for s in shots if s["first"])
    print("Read %d shots in %d runs from %d files (%d duplicate runs skipped). Not counted: %d first shots of a run%s."
          % (len(shots), stats["runs"], stats["files"], stats["dup_runs"], nfirst,
             (", %d shots right after a park" % sum(1 for s in shots if s["after_park"] and not s["first"])) if a.skip_after_park else ""))
    for h in holds:
        print("HOLD %s: %s" % (h["src"], h["text"]))
    geo = None
    hdr = "      v code    s1 |   n und cmp stl par gld psn lst lch oth | first | codes"
    for c in cells:
        g = geometry_of(c["key"])
        if g != geo:
            geo = g
            print("\n%s (%s)\n%s" % (g, phase_of(c["key"]), hdr))
        cnt = c["counts"]
        cols = " ".join("%3s" % (cnt[k] or ".") for k in ("under", "compass", "stall", "partial", "goldilocks", "poison", "lost", "latch", "other"))
        fs = ",".join("%s" % k for k in c["first"]) or "."
        print("  %5d %4d %+5d | %3d %s | %-5s | %s%s" % (c["key"][5], mv_to_code(c["key"][5]), c["key"][6], c["n"], cols, fs,
                                                       fmt_codes(c), ("  << " + "; ".join(sorted(c["flags"]))) if c["flags"] else ""))
    codes = sorted({k & 0xFF for c in cells for k in c["regs"]["CRC"] if k != 0x55})
    if codes:
        print("\nCRC codes seen (four 2-bit fields, 01 = pass): " + "; ".join(
            "0x%02x %s%s" % (k, crc_label(k), " (partial)" if crc_partial(k) else "") for k in codes))
    if el:
        print("\nEdges (neighbouring cells that differ):")
        for e in el:
            if e["axis"] == "s1":
                print("  %s, %d mV: s1 %+d %s | s1 %+d %s  (%d us%s)" % (e["geometry"], e["v"], e["a"], e["sig_a"], e["b"],
                                                                     e["sig_b"], e["width"], ": densify with 1-2 us steps" if 2 < e["width"] <= MAX_EDGE_US else ""))
            else:
                print("  %s, s1 %+d: %d mV %s | %d mV %s  (%d code%s)" % (e["geometry"], e["s1"], e["a"], e["sig_a"], e["b"],
                                                                     e["sig_b"], e["width"], "s" if e["width"] != 1 else ""))
    for m in nm:
        print("\nNon-monotonic in depth (%s, s1 %+d): %s" % (m["geometry"], m["s1"], ", ".join("%d %s" % x for x in m["map"])))
    print("\nNext: %s" % head)
    for d in details:
        print("  %s" % d)
    for c in cmds:
        print("  $ %s" % c)
    return 0


# ---------------------------------------------------------------- run (drive bootrun.sh)

def flatten(plan):
    """[(column index, step)] in plan order."""
    return [(ci, list(st)) for ci, col in enumerate(plan["columns"]) for st in col["steps"]]


def boot_steps(plan, flat, pos, max_shots):
    """The steps of the next boot from flat[pos:]: within max_shots, never across a new_boot column."""
    take, shots = [], 0
    cols = plan["columns"]
    for j in range(pos, len(flat)):
        ci, st = flat[j]
        if take and cols[ci].get("new_boot") and flat[j - 1][0] != ci:
            break
        if take and shots + st[6] > max_shots:
            break
        take.append(st)
        shots += st[6]
    prefix = [list(plan["warmup"])] if plan.get("warmup") else []
    seq = [list(s) for s in take]
    if plan.get("extra_first_rep") and seq:
        seq[0][6] += 1
    return prefix + seq, len(prefix), len(take)


RE_TIP = re.compile(r"^TIPPED at step (\d+)(?: shot \d+)? \([^)]*\): (.*)$", re.M)


def classify_boot(out, rc):
    """(what, step index or None, text) for one bootrun.sh call."""
    if re.search(r"^>>> GOLDILOCKS HELD|^HOLD SEEN", out, re.M):
        return "goldilocks", None, ""
    m = re.search(r"^>>> STALL DSOV0 HELD.*$", out, re.M)
    if m:
        return ("goldilocks" if "goldilocks signature" in m.group(0) else "partial"), None, m.group(0)
    if re.search(r"^>>> PARTIAL HELD", out, re.M):
        return "partial", None, ""
    if re.search(r"MISTIMED at", out):
        return "mistimed", None, ""
    if re.search(r"CONTROLLER OFF|controller unreadable", out):
        return "latch", None, ""
    if re.search(r"NO DIP", out):
        return "nodip", None, ""
    if rc == 0 and re.search(r"^sequence exhausted", out, re.M) and "SHOT CAP" not in out:
        return "done", None, ""
    m = RE_TIP.search(out)
    if m and rc == 10:
        text = m.group(2)
        low = text.lower()
        if low.startswith(("poisoned", "hidden-poison")):
            return "poison", int(m.group(1)), text
        if "lost" in low:
            return "lost", int(m.group(1)), text
        if low.startswith("booting") or "gfw stalls in a row" in low:
            return "stall", int(m.group(1)), text
        return "unknown", int(m.group(1)), text
    return "unknown", None, "bootrun exit %s" % rc


def cmd_run(a):
    with open(a.plan) as f:
        plan = json.load(f)
    bad = check_plan(plan, pw_min=a.pw_min)
    if bad:
        for where, st, why in bad:
            print("ILLEGAL %s %s: %s" % (where, st, "; ".join(why)))
        print("RUN: the plan has illegal steps; nothing run")
        return 2
    bootrun = a.bootrun or os.path.join(TOOLS, "runner", "bootrun.sh")
    flat = flatten(plan)
    max_shots = int(plan.get("max_shots_per_boot", 120))
    logdir = a.log_dir or os.path.join("hunt-logs", "%s-%s-%s" % (plan.get("phase", "plan"), a.card,
                                                                  _dt.datetime.now().strftime("%Y%m%dT%H%M%S")))
    stop_file = a.stop_file or os.path.join(logdir, "STOP")
    env = dict(os.environ)
    env.update({k: str(v) for k, v in plan.get("env", {}).items()})
    env["NO_ON_HOLD"] = "1"
    env.pop("NOCYCLE", None)
    if str(plan.get("env", {}).get("SHOOT_THROUGH_STALLS", 0)) == "1" and not a.owner_ok_shoot_through:
        print("RUN: this plan sets SHOOT_THROUGH_STALLS=1, which breaks upstream's rule 2 (no g, r/j or FLR while GFW "
              "is 0x01) and can wedge the GFW sequencer. Get the card owner's OK, then add --owner-ok-shoot-through.")
        return 2
    print("RUN %s: %s, %d steps, card %s, bootrun %s, logs %s%s" % (plan.get("phase"), plan.get("label"), len(flat), a.card,
                                                                  bootrun, logdir, "" if a.execute else " (DRY RUN)"))
    print("  runner env: %s NO_ON_HOLD=1" % " ".join("%s=%s" % kv for kv in plan.get("env", {}).items()))
    pos, boots, conts = 0, 0, 0
    if not a.execute:
        while pos < len(flat):
            seq, off, n = boot_steps(plan, flat, pos, max_shots)
            boots += 1
            print("  boot %d (if nothing tips): %s %s %s" % (boots, shlex.quote(bootrun), a.card, shlex.quote(json.dumps(seq, separators=(",", ":")))))
            pos += n
        print("DRY RUN: nothing was run. Add --execute to power-cycle the bench and shoot, one boot per line above "
              "(a tip moves the rest to new boots).")
        return 0
    os.makedirs(logdir, exist_ok=True)
    runlog = open(os.path.join(logdir, "run.log"), "a")

    def rl(line):
        runlog.write(line + "\n")
        runlog.flush()
    rl("######## RUN %s: %s, %d steps, card %s %s" % (plan.get("phase"), plan.get("label"), len(flat), a.card,
                                                     _dt.datetime.now().strftime("%Y-%m-%dT%H:%M:%S")))
    stop = None
    while pos < len(flat):
        if os.path.exists(stop_file):
            stop = "STOP file %s present (left in place)" % stop_file
            break
        if boots >= a.max_boots:
            stop = "--max-boots %d reached" % a.max_boots
            break
        seq, off, n = boot_steps(plan, flat, pos, max_shots)
        boots += 1
        log = os.path.join(logdir, "boot-%02d.log" % boots)
        env["BOOTRUN_LOG"] = log
        print("######## boot %d: plan steps %d..%d %s" % (boots, pos, pos + n - 1, _dt.datetime.now().strftime("%H:%M:%S")), flush=True)
        rl("######## boot %d: plan steps %d..%d of %d %s" % (boots, pos, pos + n - 1, len(flat),
                                                          _dt.datetime.now().strftime("%H:%M:%S")))
        p = subprocess.run(["bash", bootrun, a.card, json.dumps(seq, separators=(",", ":"))], env=env,
                           stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
        out = p.stdout
        with open(os.path.join(logdir, "boot-%02d.out" % boots), "w") as f:
            f.write(out)
        for ln in out.splitlines():
            if re.match(r"^(SHOT .*kind=(?!under)|TIPPED|RESULT|>>> |HOLD|STOP|cell |.*FAILED)", ln):
                print("  " + ln[:220])
            if re.match(r"^(TIPPED|RESULT|>>> |HOLD|STOP|.*FAILED)", ln):
                rl(ln[:300])
        what, idx, text = classify_boot(out, p.returncode)
        if what == "done":
            pos += n
            continue
        if what in ("poison", "lost", "stall") and idx is not None:
            if idx < off:
                conts += 1
                print("RUN: tipped in the warm-up step (%s); the same steps go to a new boot" % what)
                if conts > a.max_cont:
                    stop = "%d tips in the warm-up step" % conts
                    break
                continue
            j = pos + (idx - off)
            if what == "poison" and a.on_poison == "stop":
                stop = "poison at plan step %d (%s): the boot ended; tally the logs before going on" % (j, text)
                break
            if what == "poison" and plan.get("on_poison") == "next_column":
                ci = flat[j][0]
                k = j + 1
                while k < len(flat) and flat[k][0] == ci:
                    k += 1
                print("RUN: poison at plan step %d: the rest of column %d (%s) is skipped" % (j, ci, plan["columns"][ci]["label"]))
                pos = k
            else:
                print("RUN: %s at plan step %d: the next boot starts at step %d (the rest of a tipped cell is not retried)" % (what, j, j + 1))
                pos = j + 1
            continue
        stop = {"goldilocks": "GOLDILOCKS HELD", "partial": "a DSOV-0 frame is held", "mistimed": "a mistimed shot",
                "latch": "a controller latch (power cycle needed)", "nodip": "a shot that did not run (NO DIP)"}.get(
            what, "unknown outcome: %s" % (text or what))
        break
    print("RUN: %s after %d boots. Logs: %s" % ("stopped: " + stop if stop else "plan done", boots, logdir))
    rl("######## RUN %s after %d boots %s" % ("stopped: " + stop if stop else "done", boots,
                                             _dt.datetime.now().strftime("%H:%M:%S")))
    runlog.close()
    if stop and stop.startswith("GOLDILOCKS"):
        print("RUN: the card is held in the goldilocks state. Leave it as it is. Run your own active sense and "
              "write-window check, then your own reviewed burn tool, if you choose to. This tool never burns.")
    print("next: %s tally %s" % (os.path.relpath(__file__), logdir))
    if stop is None:
        return 0
    return 4 if (stop.startswith("GOLDILOCKS") or stop.startswith("a DSOV-0")) else 3


def cmd_strategy(a):
    print(STRATEGY)
    return 0


# ---------------------------------------------------------------- CLI

def main(argv=None):
    p = argparse.ArgumentParser(prog="goldilocks_hunt.py", description=__doc__.split("\n\n")[0],
                                formatter_class=argparse.RawDescriptionHelpFormatter,
                                epilog="Run `goldilocks_hunt.py strategy` for the strategy and the stop rules.")
    sub = p.add_subparsers(dest="cmd", required=True)

    def plan_opts(sp, reps, pw=DEFAULT_PW):
        sp.add_argument("--reps", type=int, default=reps, help="counted shots per cell (default %d)" % reps)
        sp.add_argument("--pw", type=int, default=pw, help="pulse width, us (default %d)" % pw)
        sp.add_argument("--vr", type=int, default=DEFAULT_VR, help="restore level, mV (default 750)")
        sp.add_argument("--delay", type=int, default=DEFAULT_DELAY, help="firmware delay, us (default 117); s1 = td - delay")
        sp.add_argument("--gap", type=int, default=DEFAULT_GAP, help="gap between pulses, us (default 150)")
        sp.add_argument("--floor", type=int, default=338, help="lowest dip any step may command, mV; the plan's FLOOR (default 338)")
        sp.add_argument("--pw-min", type=int, default=W, help="shortest pulse allowed, us (default 47, one PMBus write; 36 is the firmware floor)")
        sp.add_argument("--max-shots-per-boot", type=int, default=120, help="split boots above this many shots (default 120)")
        sp.add_argument("--warmup", default="extra", help="extra (default: the first step of every boot gets one more shot), "
                        "none, or a warm-up step as JSON fired at the start of every boot, once unless its reps field says "
                        "more (use a setting that reads under on this card)")
        sp.add_argument("--shoot-through", action="store_true", help="SHOOT_THROUGH_STALLS=1 (breaks upstream rule 2; ask the owner first)")
        sp.add_argument("--continue-partials", action="store_true", help="PARTIAL_CONTINUE=1: keep shooting through readable DSOV-0 frames")
        sp.add_argument("--stallrun", type=int, default=8, help="stalls in a row that end a boot with --shoot-through (default 8)")
        sp.add_argument("--lostrun", type=int, default=8, help="lost shots in a row that end a boot (default 8)")
        sp.add_argument("--gfw-wait", type=float, default=5, help="seconds to poll a booting GFW before the stall read (default 5)")
        sp.add_argument("--continue-sent0", action="store_true", help="SENT0_CONTINUE=1: log a clean DSOV-1 sentinel-0 frame and keep shooting")
        sp.add_argument("--trace", action="store_true", help="TRACE=1: NVVDD trace per shot (debug firmware)")
        sp.add_argument("--out", help="write the plan JSON to this file (for `run`)")
        sp.add_argument("--json", action="store_true", help="print the plan JSON instead of the summary")

    sp = sub.add_parser("train", help="phase 2: community train ladder")
    sp.add_argument("--depths", help="comma-separated mV list, shallow first (default: --from to --to in VID steps)")
    sp.add_argument("--from", dest="from_mv", type=int, default=400, help="shallowest depth, mV (default 400)")
    sp.add_argument("--to", dest="to_mv", type=int, default=350, help="deepest depth, mV (default 350)")
    sp.add_argument("--td", type=int, default=None, help="td, us (default -60: s1 = -177 at delay 117)")
    sp.add_argument("--np", type=int, default=3, help="pulses (default 3)")
    sp.add_argument("--fast", type=int, default=1, help="slew profile (default 1, fast)")
    plan_opts(sp, 4)
    sp.set_defaults(func=cmd_train)

    sp = sub.add_parser("readmap", help="phase 3: fuse-read window map (one fast dip after the release)")
    sp.add_argument("--v", required=True, help="depth(s), mV: the poison-edge code of the train map")
    sp.add_argument("--s1-from", type=int, default=40)
    sp.add_argument("--s1-to", type=int, default=160)
    sp.add_argument("--s1-step", type=int, default=5)
    plan_opts(sp, 3)
    sp.set_defaults(func=cmd_readmap)

    sp = sub.add_parser("init", help="phase 4: init-phase release-edge scan (one fast dip ending at or before the release)")
    sp.add_argument("--depths", default="450,425,400,388,381,375", help="mV list, shallow first (default 450,425,400,388,381,375)")
    sp.add_argument("--s1-from", type=int, default=-47, help="latest s1, us (default -47: the write ends at the release)")
    sp.add_argument("--s1-to", type=int, default=-100)
    sp.add_argument("--s1-step", type=int, default=5)
    plan_opts(sp, 3)
    sp.set_defaults(func=cmd_init)

    sp = sub.add_parser("slow", help="phase 5: slow-slew (fast 0) edge scan at one code")
    sp.add_argument("--v", required=True, help="depth(s), mV: the code where s1 -47 reads under and earlier is poison")
    sp.add_argument("--s1-from", type=int, default=-47)
    sp.add_argument("--s1-to", type=int, default=-100)
    sp.add_argument("--s1-step", type=int, default=5)
    plan_opts(sp, 2)
    sp.set_defaults(func=cmd_slow)

    sp = sub.add_parser("dwell", help="many shots at one cell, optionally over several fresh boots")
    sp.add_argument("--v", type=int, required=True)
    sp.add_argument("--s1", type=int, default=None, help="first dip write start relative to the release, us")
    sp.add_argument("--td", type=int, default=None, help="td instead of --s1 (s1 = td - delay)")
    sp.add_argument("--np", type=int, default=1)
    sp.add_argument("--fast", type=int, default=1)
    sp.add_argument("--boots", type=int, default=1, help="fresh boots, one column each (default 1)")
    sp.add_argument("--tv", type=int, default=0, help="stock VOUT write time, us (13th step field; default 0)")
    sp.add_argument("--allow-overlap", action="store_true", help="push3: allow a write in flight across the release")
    plan_opts(sp, 10)
    sp.set_defaults(func=cmd_dwell)

    sp = sub.add_parser("overlap", help="phase 6, push3 only: one long dip, restore write in flight across the release")
    sp.add_argument("--v", default="388,381,375,369", help="depths, mV, shallow first (default 388,381,375,369)")
    sp.add_argument("--s1", default="-177,-130", help="dip write starts, us (default -177,-130: falls near -130 and -83)")
    sp.add_argument("--rises", default="0,3,7,11,17", help="when the rail is back up, us after the release (default 0,3,7,11,17)")
    sp.add_argument("--no-anchor", dest="anchor", action="store_false", help="leave out the +48 us anchor (restore write at +1)")
    sp.add_argument("--tv", type=int, default=171, help="stock VOUT write time, us, pinned for every shot (default 171)")
    sp.add_argument("--fast", type=int, default=1)
    plan_opts(sp, 1)
    sp.set_defaults(func=cmd_overlap, vr=625)

    sp = sub.add_parser("check", help="legality of a step list (JSON or file) or a plan file")
    sp.add_argument("steps")
    sp.add_argument("--allow-overlap", action="store_true", help="push3: writes in flight across the release are legal")
    sp.add_argument("--floor", type=int, default=None, help="floor, mV (default: the plan's FLOOR, else none)")
    sp.add_argument("--pw-min", type=int, default=W)
    sp.add_argument("-v", "--verbose", action="store_true", help="print each step's write timeline")
    sp.set_defaults(func=cmd_check)

    sp = sub.add_parser("tally", help="per-cell counts and codes from runner logs, edges and the next phase")
    sp.add_argument("logs", nargs="+", help="log files or directories (bootrun logs, runner output)")
    sp.add_argument("--skip-after-park", action="store_true", help="also leave out shots right after a park")
    sp.add_argument("--json", action="store_true")
    sp.set_defaults(func=cmd_tally)

    sp = sub.add_parser("run", help="drive bootrun.sh through a plan (dry run unless --execute)")
    sp.add_argument("plan")
    sp.add_argument("--card", required=True, help="the card name in bench.conf (card_<name>)")
    sp.add_argument("--execute", action="store_true", help="really power-cycle and shoot")
    sp.add_argument("--bootrun", help="launcher (default tools/runner/bootrun.sh)")
    sp.add_argument("--log-dir", help="where every boot's log goes (default hunt-logs/<phase>-<card>-<time>)")
    sp.add_argument("--stop-file", help="stop before the next boot while this file exists (default <log-dir>/STOP)")
    sp.add_argument("--max-boots", type=int, default=20)
    sp.add_argument("--max-cont", type=int, default=3, help="tips in a warm-up step before giving up (default 3)")
    sp.add_argument("--on-poison", choices=("stop", "continue"), default="stop",
                    help="stop (default) or go on in a fresh boot as the plan says (each poison costs a power cycle)")
    sp.add_argument("--owner-ok-shoot-through", action="store_true", help="required to run a plan with SHOOT_THROUGH_STALLS=1")
    sp.add_argument("--pw-min", type=int, default=W)
    sp.set_defaults(func=cmd_run)

    sp = sub.add_parser("strategy", help="print the strategy, the agent loop and the stop rules")
    sp.set_defaults(func=cmd_strategy)

    a = p.parse_args(argv)
    if hasattr(a, "pw_min") and a.pw_min < 36:
        p.error("--pw-min below 36 us: the firmware clamps pulses to 36 us")
    return a.func(a) or 0


if __name__ == "__main__":
    sys.exit(main())
