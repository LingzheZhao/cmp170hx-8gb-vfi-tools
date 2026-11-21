"""Python port of the shot timeline built by glitch_cmd() in pwmvid.c (lab-psr1-push2; push1 is the same timeline
without the dither pw floor and the SCL settle).

Only the step list is modelled (times relative to the PEX_RST release at t = 0), not execution. It gives the resolved
pw/gap and push2's refusal rules: a VOUT step before the 600 mV setup write, or a PMBus write in flight across the
release, counting 47 us per write and queueing each write behind the previous one (push2 aborts such a shot; push1
only printed a WARN, using the scheduled time; push3 runs it and names the write in a "glitch NOTE" line, see
firmware/0011 and docs/PUSH3.md).
"""
from .steps import RUNNER_DELAY_US, RUNNER_GAP_US
from .vid import mv_to_code

GA_VOUT, GA_MODE, GA_RST_LOW, GA_SCL_LOW, GA_SCL_REL, GA_MODE_ON, GA_VMIN, GA_VOUT_RETRY = 1, 2, 3, 5, 6, 7, 8, 9
ACT_NAMES = {1: "VOUT", 2: "MODE", 3: "RST_LOW", 5: "SCL_LOW", 6: "SCL_REL", 7: "MODE_ON", 8: "VMIN", 9: "VOUT_RETRY"}
WRITE_ACTS = (GA_VOUT, GA_MODE, GA_MODE_ON, GA_VMIN, GA_VOUT_RETRY)
SCL_SETTLE_US = 200
PMBUS_W_US = 36          # firmware floor for pw and gap
PMBUS_W_MEAS_US = 47     # measured duration of one PMBus write (bench, 2026-11-20)
PWMVID_SWITCH_DELAY_US = 500000


def _cdiv(a, b):
    """C integer division (truncates toward zero)."""
    q = abs(a) // abs(b)
    return q if (a >= 0) == (b >= 0) else -q


def build(vd, td, vr, tr=-20, tv=0, fast=1, hold_ms=0, pulses=1, pw=0, gap=0, delay=75, revert=1):
    """Return {'steps': [(t_us, act, arg)], 'resolved': {...}, 'abort': str|None} like glitch_cmd()."""
    if fast < 0 or fast > 7:
        return {"steps": [], "resolved": {}, "abort": "fast=%d, use 0..7" % fast}
    dither = revert == 2
    if dither:
        revert = 0
    reset_at = -1000
    if tr <= -1000:
        if tr < -20000:
            return {"steps": [], "resolved": {}, "abort": "reset lead %d us, use -20000..-1000" % tr}
        reset_at = tr
        tr = -20
    pulses = max(1, min(16, pulses))
    if pw <= 0:
        pw = (tr - td) if pulses == 1 else _cdiv(tr - td, 2 * pulses - 1)
    if gap <= 0:
        gap = pw
    if pw < PMBUS_W_US:
        pw = PMBUS_W_US
    if gap < PMBUS_W_US:
        gap = PMBUS_W_US
    if dither and pw < PMBUS_W_MEAS_US:   # push2: a dither below one write would queue up
        pw = PMBUS_W_MEAS_US

    st = [(reset_at - 300, GA_VMIN, 0x20), (reset_at - 200, GA_MODE_ON, 0),
          (reset_at, GA_RST_LOW, 0), (reset_at, GA_VOUT, mv_to_code(600))]
    train_end = 0
    t = td
    for p in range(pulses):
        if len(st) >= 40:
            break
        st.append((t - delay, GA_VOUT, mv_to_code(vd) + (1 if (dither and (p & 1)) else 0)))
        if dither and p + 1 < pulses:
            t += pw
            continue
        train_end = t + pw - delay
        st.append((train_end, GA_VOUT, mv_to_code(vr)))
        t += pw + gap
    post = tv
    if train_end > post:
        post = train_end + gap
    hold_at_release = hold_ms > 0 and train_end < 0
    if hold_at_release:
        st.append((0, GA_SCL_LOW, 0))
        st.append((hold_ms * 1000, GA_SCL_REL, 0))
        if hold_ms * 1000 + SCL_SETTLE_US > post:
            post = hold_ms * 1000 + SCL_SETTLE_US
    st.append((post + 10, GA_VOUT_RETRY if hold_at_release else GA_VOUT, 0x7E))
    st.append((post + 60, GA_VMIN, 0x30))
    if revert:
        st.append((post + PWMVID_SWITCH_DELAY_US, GA_MODE, 0))
    if hold_ms > 0 and not hold_at_release:
        st.append((post + 100, GA_SCL_LOW, 0))
        st.append((post + 100 + hold_ms * 1000, GA_SCL_REL, 0))
    st.sort(key=lambda s: s[0])  # the firmware's insertion sort is stable too
    hold_at = "none" if hold_ms <= 0 else ("release" if hold_at_release else "post")
    resolved = {"pw": pw, "gap": gap, "delay": delay, "reset_at": reset_at, "dither": int(dither),
                "hold_at": hold_at, "train_end": train_end, "post": post, "revert": revert}
    return {"steps": st, "resolved": resolved, "abort": None}


def build_for_step(d):
    """Timeline for a runner step dict (gap and delay from the step, default 150 / 117; tv from the step, default 0)."""
    return build(vd=d["v"], td=d["td"], vr=d["vr"], tr=d["t_restore"], tv=int(d.get("tv") or 0), fast=d["fast"], hold_ms=d["hold"],
                 pulses=d["pulses"], pw=d["pw"], gap=d.get("gap", RUNNER_GAP_US),
                 delay=d.get("delay", RUNNER_DELAY_US), revert=d["revert"])


def queued(steps, write_us=PMBUS_W_MEAS_US):
    """[(scheduled, start, act)] for every PMBus write: each write starts when it is due or when the previous one
    finished, whichever is later (push2's model)."""
    out, bus_free = [], None
    for t, act, _arg in steps:
        if act not in WRITE_ACTS:
            continue
        start = t if bus_free is None or t > bus_free else bus_free
        out.append((t, start, act))
        bus_free = start + write_us
    return out


def release_overlaps(steps, write_us=PMBUS_W_MEAS_US):
    """PMBus writes in flight at the PEX_RST release, by queued start: start <= 0 < start + write_us. Returns the
    step tuples (scheduled time first) like the firmware's ABORT line names them."""
    hits = {(t, act) for t, start, act in queued(steps, write_us) if start <= 0 < start + write_us}
    return [s for s in steps if (s[0], s[1]) in hits]


def late_writes(steps, write_us=PMBUS_W_MEAS_US):
    """Writes that run later than scheduled because the previous one is still on the bus (push2 WARN)."""
    return [(t, start) for t, start, _ in queued(steps, write_us) if start > t]


def overwritten(steps, reset_at):
    """VOUT steps scheduled before the 600 mV setup write at reset_at (they never reach the rail)."""
    return [s for s in steps if s[1] == GA_VOUT and s[0] < reset_at]


def simple_overlap_times(td, pw, pulses, gap=RUNNER_GAP_US, delay=RUNNER_DELAY_US, write_us=PMBUS_W_MEAS_US):
    # valid while every write is >= write_us after the previous one (pw, gap >= 47, train clear of the setup steps)
    """The written rule for a plain train (no dither, pw/gap >= 36): dip command at td - delay, restore
    at td - delay + pw, pulse k offset k * (pw + gap); a write overlaps if t_cmd <= 0 < t_cmd + write_us."""
    out = []
    for k in range(pulses):
        dip = td - delay + k * (pw + gap)
        for t in (dip, dip + pw):
            if t <= 0 < t + write_us:
                out.append(t)
    return out


def check_step(d):
    """Summary used by the parser, the rules and the tests."""
    b = build_for_step(d)
    if b["abort"]:
        return {"abort": b["abort"], "resolved": {}, "overlaps": [], "overwritten": [], "steps": []}
    r = b["resolved"]
    return {"abort": None, "resolved": r, "steps": b["steps"],
            "overlaps": [s[0] for s in release_overlaps(b["steps"])],
            "overwritten": [s[0] for s in overwritten(b["steps"], r["reset_at"])],
            "late": late_writes(b["steps"])}
