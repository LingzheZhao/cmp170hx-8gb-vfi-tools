"""Turn a plan into a launcher invocation (argv + env) for tools/runner, without running it.

Two modes, chosen by the family's "launcher": {"mode": ...}:
  firstshot   firstshot.sh CARD '[steps]': one fresh power cycle per step, full per-boot logs in LOGDIR.
  park_each   bootrun.sh CARD '[steps]' with PARK_EACH=1: one fresh boot, the first shot fresh and every
              later shot after a park (post-park, equivalent to a first shot for families that say so); identical
              proposals are grouped into one step with reps. Shots expected to lose the link go last (a lost shot
              ends the boot: LOSTRUN=1).
"""
import json
import os
import shlex

from . import steps as S

PLANNER_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

PARK_EACH_ENV = {"PARK_EACH": 1, "REPEAT": 1, "LOSTRUN": 1, "CELLLOST": 1, "SHOTCAP": 450, "RUNTO": 900,
                 "BAD0_FOLLOWUP": 1}
ORDER = {"under": 0, None: 1, "mixed": 2, "lost": 3}


def default_runner_dir():
    """The launcher directory: $VFI_RUNNER_DIR, else tools/runner next to this planner (tools/planner)."""
    return os.environ.get("VFI_RUNNER_DIR") or os.path.join(os.path.dirname(PLANNER_DIR), "runner")


def runner_dir(card_cfg, override=None):
    """--runner-dir, else the card config's launcher.runner_dir, else default_runner_dir()."""
    return override or ((card_cfg or {}).get("launcher") or {}).get("runner_dir") or default_runner_dir()


def card_path(card):
    """cards/<card>.json under the planner, or a path to a card config file."""
    if card.endswith(".json") or os.sep in card:
        return card
    return os.path.join(PLANNER_DIR, "cards", "%s.json" % card)


def load_card(card):
    with open(card_path(card)) as f:
        cfg = json.load(f)
    cfg.setdefault("card", card)
    return cfg


def ledger_path(card_cfg, card, override=None):
    """--ledger, else the card config's ledger (relative to the planner), else ledger/card-<card>.jsonl."""
    if override:
        return override
    return os.path.join(PLANNER_DIR, (card_cfg or {}).get("ledger") or "ledger/card-%s.jsonl" % card)


def mode_of(family):
    return ((family or {}).get("launcher") or {}).get("mode", "firstshot")


def group_park_each(proposals, max_shots):
    """[[step_list with reps], ...] for one PARK_EACH boot: expected-under first, expected-lost last, identical
    steps merged into reps, at most max_shots shots."""
    # a lost shot ends a PARK_EACH boot: known-under first, then shallow to deep, expected losses last
    props = sorted(proposals, key=lambda p: (p.get("expected") == "lost", ORDER.get(p.get("expected"), 1) > 0,
                                             -int(p.get("code", 0) or 0)))[:max_shots]
    out = []
    for p in props:
        sl = S.to_list(p["step"], reps=1)
        if out and out[-1][:6] + out[-1][7:] == sl[:6] + sl[7:]:
            out[-1][6] += 1
        else:
            out.append(sl)
    return out


def order_firstshot(proposals):
    """One boot per proposal; keep expected-lost shots apart so two lost boots (two failed parks) are not adjacent
    when the batch allows it."""
    props = sorted(proposals, key=lambda p: ORDER.get(p.get("expected"), 1))
    strong = [p for p in props if p.get("expected") == "lost"]
    rest = [p for p in props if p.get("expected") != "lost"]
    out = []
    while rest or strong:
        if rest:
            out.append(rest.pop(0))
        if strong:
            out.append(strong.pop(0))
    return out


def build(card_cfg, family, plan, run_dir, runner=None, max_boots=None):
    """dict(mode, argv, env, display, steps, boots, shots, out_path, ingest)."""
    lau = card_cfg.get("launcher") or {}
    rb = runner_dir(card_cfg, runner)
    fam_l = (family or {}).get("launcher") or {}
    mode = fam_l.get("mode", "firstshot") if plan.get("state_class") == "first-shot-fresh-boot" else "bootrun"
    env = {k: str(v) for k, v in (lau.get("env") or {}).items()}
    env["FLOOR"] = str(card_cfg.get("floor_mv", 338))
    env.update({k: str(v) for k, v in (fam_l.get("env") or {}).items()})
    out_path = os.path.join(run_dir, "launcher.out")
    if mode == "firstshot":
        props = order_firstshot(plan["proposals"])
        if max_boots is not None:
            props = props[:max(0, max_boots)]
        steps = [S.to_list(p["step"], reps=1) for p in props]
        env["LOGDIR"] = run_dir
        script, boots, shots = "firstshot.sh", len(steps), len(steps)
    elif mode == "park_each":
        steps = group_park_each(plan["proposals"], int(fam_l.get("max_shots_per_boot", 8)))
        for k, v in PARK_EACH_ENV.items():
            env.setdefault(k, str(v))
        env["BOOTRUN_LOG"] = os.path.join(run_dir, "bootrun.last")
        script, boots, shots = "bootrun.sh", 1, sum(s[6] for s in steps)
    else:   # later-shot families: one boot, the steps as one sequence
        steps = [S.to_list(p["step"], reps=1) for p in plan["proposals"]]
        env["BOOTRUN_LOG"] = os.path.join(run_dir, "bootrun.last")
        if plan.get("state_class") == "post-park":
            env["PARK_EACH"] = "1"
        script, boots, shots = "bootrun.sh", 1, len(steps)
    js = json.dumps(steps, separators=(",", ":"))
    argv = ["bash", os.path.join(rb, script), str(plan["card"]), js]
    envs = " ".join("%s=%s" % (k, shlex.quote(v)) for k, v in env.items())
    display = "mkdir -p %s && cd %s && %s bash %s %s %s 2>&1 | tee -a %s" % (
        shlex.quote(run_dir), shlex.quote(rb), envs, script, plan["card"], shlex.quote(js), shlex.quote(out_path))
    ingest = {"path": out_path, "park_each": mode == "park_each" or env.get("PARK_EACH") == "1",
              "firmware": card_cfg.get("firmware_expected")}
    return {"mode": mode, "argv": argv, "env": env, "cwd": rb, "display": display, "steps": steps, "boots": boots,
            "shots": shots, "out_path": out_path, "ingest": ingest}
