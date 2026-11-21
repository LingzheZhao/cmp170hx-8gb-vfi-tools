"""Runner step format (tools/runner/bisect_shots.py).

Step list: [v_mV, td_us, pulses, pw_us, v_restore_mV, fast, reps, hold_ms, t_restore_us, revert, delay_us, gap_us]
plus an optional 13th field t_revert_us (tv): when the firmware writes the stock VOUT after the train (default 0).
Omitted tail fields take the runner defaults: pulses 3, pw 198, v_restore 750, fast 1, reps REPEAT (env, default 1),
hold 0, t_restore -20, revert 0, delay 117, gap 150 (delay and gap became step fields on 2026-11-20; before that the
runner always sent 117 and 150).

In pwmvid.c td and delay only appear as td - delay: pulse k's dip command is at td - delay + k * (pw + gap) and its
restore pw later. Two steps with the same td - delay are the same waveform; t0() is that first dip command time.
"""
FIELDS = ("v", "td", "pulses", "pw", "vr", "fast", "reps", "hold", "t_restore", "revert", "delay", "gap")
DEFAULTS = {"pulses": 3, "pw": 198, "vr": 750, "fast": 1, "reps": None, "hold": 0, "t_restore": -20, "revert": 0,
            "delay": 117, "gap": 150}
RUNNER_GAP_US = 150
RUNNER_DELAY_US = 117
RUNNER_DELAY_RANGE = (0, 900)
RUNNER_GAP_RANGE = (36, 2000)
# Fields that define a waveform (reps is only how often the runner repeats it).
GEOMETRY = ("td", "pulses", "pw", "vr", "fast", "hold", "t_restore", "revert", "delay", "gap", "tv")


def from_list(s, repeat_default=1):
    """Runner list -> dict with every field filled in the way bisect_shots.step_fields() fills it."""
    if not isinstance(s, (list, tuple)) or len(s) < 2:
        raise ValueError("a step needs at least [v, td]: %r" % (s,))
    d = {"v": int(s[0]), "td": int(s[1])}
    for idx, name in enumerate(FIELDS[2:], start=2):
        if len(s) > idx and s[idx] is not None:
            d[name] = int(s[idx])
        else:
            d[name] = DEFAULTS[name] if name != "reps" else repeat_default
    d["tv"] = int(s[12]) if len(s) > 12 and s[12] is not None else 0
    return d


def to_list(d, reps=None):
    """Dict -> full 10-field runner list (firstshot.sh needs at least 7 fields; it crashes on shorter lists)."""
    out = []
    for name in FIELDS:
        if name == "reps":
            out.append(int(reps if reps is not None else (d.get("reps") or 1)))
        else:
            val = d.get(name)
            if val is None:
                val = DEFAULTS[name]
            out.append(int(val))
    if d.get("tv"):
        out.append(int(d["tv"]))
    return out


def t0(d):
    """First dip command time relative to the PEX_RST release (td - delay)."""
    return int(d["td"]) - int(d.get("delay", RUNNER_DELAY_US))


def normalize(d):
    """Fill fields missing from older records (delay 117, gap 150)."""
    out = dict(d)
    for k in ("delay", "gap", "hold", "t_restore", "revert"):
        if out.get(k) is None:
            out[k] = DEFAULTS[k]
    if out.get("tv") is None:
        out["tv"] = 0
    return out


def energy(d):
    """Latch-energy proxy used by the never-rules: pulses x pw (us)."""
    return int(d["pulses"]) * int(d["pw"])


def short(d):
    """Compact human label, e.g. '344(55) 3x80 td-60 vr750 f1'."""
    from .vid import mv_to_code
    extra = []
    if d.get("hold"):
        extra.append("hold%d" % d["hold"])
    if d.get("t_restore", -20) != -20:
        extra.append("lead%d" % d["t_restore"])
    if d.get("revert"):
        extra.append("rev%d" % d["revert"])
    if d.get("delay", RUNNER_DELAY_US) != RUNNER_DELAY_US:
        extra.append("delay%d" % d["delay"])
    if d.get("gap", RUNNER_GAP_US) != RUNNER_GAP_US:
        extra.append("gap%d" % d["gap"])
    return "%d(%d) %dx%d td%+d vr%d f%d%s" % (
        d["v"], mv_to_code(d["v"]), d["pulses"], d["pw"], d["td"], d["vr"], d["fast"],
        (" " + " ".join(extra)) if extra else "")
