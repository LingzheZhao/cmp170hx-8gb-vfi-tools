"""Turn bootrun / firstshot / campaign / abtest logs into ledger records (one per fired shot).

Formats handled (all read-only; tools/runner output as of push1 and push2):
  * bootrun.sh output: 'cycle (wol): ...', 'bench up Ns', 'pico v: ... fw=TAG', 'START temp_raw=N HH:MM:SS',
    'run env: ...', then bisect_shots.py output ('pico:', 'unlock:', 'boot pmbus', per shot the firmware lines
    '   glitch vd=..', '   glitch WARN: ..', '   timing i=..', '   rail_code=..', '   release_at=N scheduled=0 via=..',
    '   glitch done pwd=..', then 'step N v=.. td=.. pw=.. vr=.. f=..: <outcome>'), 'end temp_raw', 'RESULT: ..'.
  * firstshot.sh: '######## first-shot boot i/n: [[step]] HH:MM:SS' + a grep of the bootrun output.
  * campaign.sh: '######## campaign boot b, steps a..z HH:MM:SS' + grep; the step list comes from --seq.
  * abtest.sh: '######## BOOT CLASSIFY_WRITE=x HH:MM:SS' + grep; step list from --seq.
Cell mode (REPEAT > 1 or a step with reps > 1) prints only non-under shots plus 'cell i ...: {counts}' summaries, so
the shots are rebuilt from the step list, the summaries and the 'step i shot k' / 'TIPPED at step i shot k' lines.
"""
import ast
import datetime as _dt
import hashlib
import json
import os
import re

from . import ledger, schedule, steps as S
from .vid import mv_to_code

RE_HDR_FS = re.compile(r"^######## first-shot boot (\d+)/(\d+): (\[.*\]) (\d\d:\d\d:\d\d)\s*$")
RE_HDR_CAMP = re.compile(r"^######## campaign boot (\d+), steps (\d+)\.\.(-?\d+) (\d\d:\d\d:\d\d)\s*$")
RE_HDR_AB = re.compile(r"^######## BOOT (\S+) (\d\d:\d\d:\d\d)\s*$")
RE_HDR_ANY = re.compile(r"^######## (.*?)(?: (\d\d:\d\d:\d\d))?\s*$")
RE_CYCLE = re.compile(r"^cycle \((\w+)\): clean shutdown(?: (\d\d:\d\d:\d\d))?")
RE_UP = re.compile(r"^bench up (\d+)s(?: boot_id (\S+))?")
RE_PLAN = re.compile(r"^PLAN SHA (MATCH|MISMATCH)")
RE_FW = re.compile(r"\bfw=(\S+)")
RE_START = re.compile(r"^START temp_raw=(\S*) (\d\d:\d\d:\d\d)")
RE_END = re.compile(r"^end temp_raw=(\S*)(?: runner_rc=(\d+))?")
RE_ENV = re.compile(r"^run env: (.*?)(?: timeout (\d+)s)?\s*$")
RE_IDENT = re.compile(r"^IDENTITY OK: card (\S+)")
RE_ECHO = re.compile(r"^\s*glitch vd=(-?\d+) td=(-?\d+) vr=(-?\d+) tr=(-?\d+) tv=(-?\d+) fast=(\d+) hold=(\d+) "
                     r"pw=(\d+) gap=(\d+) delay=(\d+) revert=(\d+) reset_at=(-?\d+) dither=(\d+) hold_at=(\w+)")
RE_WARN = re.compile(r"^\s*glitch WARN: (.*)$")
RE_ABORT = re.compile(r"^\s*glitch ABORT: (.*)$")
RE_TIMING = re.compile(r"^\s*timing i=\d+ ")
RE_TIMING_F = re.compile(r"^\s*timing i=\d+ scheduled=(-?\d+) act=(\d+) arg=(-?\d+)")
RE_RAIL = re.compile(r"^\s*rail_code=(\S+) status")
RE_PRIME = re.compile(r"^\s*prime_at=(-?\d+)")
RE_RELEASE = re.compile(r"^\s*release_at=(-?\d+) scheduled=(-?\d+) via=(\w+)")
RE_DONE = re.compile(r"^\s*glitch done pwd=(\d) e1=0x([0-9a-fA-F]{4})(?: nvvdd=(-?[\d.]+))? writes_failed=(\d+)")
RE_STEP = re.compile(r"^step (\d+)(?: shot (\d+))? v=(-?\d+) td=(-?\d+) pw=(-?\d+) vr=(-?\d+) f=(\d+): (.*)$")
RE_CELL = re.compile(r"^cell (\d+)(?: \(([^)]*)\))? v=(-?\d+) td=(-?\d+) np=(\d+) pw=(-?\d+) vr=(-?\d+): (\{.*\})\s*$")
RE_TIPPED = re.compile(r"^TIPPED at step (\d+)(?: shot (\d+))? \(v=(-?\d+) td=(-?\d+) pw=(-?\d+) vr=(-?\d+) f=(\d+)\): (.*)$")
RE_NSHOTS = re.compile(r"\((\d+) shots this boot\)")
RE_PARK = re.compile(r"^\s*park after lost shot(?: (FAILED) \(rc (\d+)\))?: (.*)$")
RE_PARK_END = re.compile(r"^\s*park: rc (\d+) (.*)$")
RE_MIST_AT = re.compile(r"^MISTIMED at step (\d+) shot (\d+) \(v=(-?\d+) td=(-?\d+) pw=(-?\d+) vr=(-?\d+) f=(\d+)\): (.*?); outcome (.*?)\. Not scored")
RE_FULL_LOGS = re.compile(r"^full logs: (\S+)")
RE_SENT_BASE = re.compile(r"^sentinel baseline (\S+)=(0x[0-9a-fA-F]+)")
RE_OBS_NS = re.compile(r"^\s*observed, not scored: (.*)$")
RE_PARK_RAIL = re.compile(r"GPU rail (\d+) mV")
RE_PARK_CFG = re.compile(r"after handback config (0x[0-9a-fA-F]+)")
RE_OBS = re.compile(r"^\s*observed: (.*)$")
RE_HALF = re.compile(r"^\s*half-boot .*re-read after 2 s: (.*)$")
RE_SLOW = re.compile(r"^\s*link back after ([\d.]+) s")
RE_STOP = re.compile(r"^STOP: (.*)$")
RE_STOP_WRITES = re.compile(r"^STOP: PMBus/setup writes failed or were not reported on step (\d+)")
RE_STOP_RELEASE = re.compile(r"^STOP: reset release was absent, late or a fallback")
RE_EXH = re.compile(r"^sequence exhausted(?:, no hold \((\d+) shots)?")
RE_HELD = re.compile(r"^>>> (GOLDILOCKS|PARTIAL) HELD")
RE_RESULT = re.compile(r"^RESULT: (.*)$")
RE_FOOTER = re.compile(r"^(FIRSTSHOT|CAMPAIGN): ")
RE_BOOTPM = re.compile(r"^boot pmbus [^:]*: (.*)$")
RE_UNLOCK = re.compile(r"^unlock: (.*)$")
RE_HOLD_SEEN = re.compile(r"^HOLD SEEN")
RE_BOOT_FAIL = re.compile(r"firmware refused step|TEMP GATE FAILED|RUNNER TRANSFER FAILED|PREFLIGHT FAILED|IDENTITY MISMATCH|PLAN SHA MISMATCH|NOT A FRESH BOOT|never came up|"
                          r"did not go down|POWER STILL ON|unsafe pre-shot fuse baseline|unlock/config readback failed|"
                          r"not in a healthy voltage band|unreadable controller before first shot|"
                          r"push1 runner requires")


CRC_INTERMEDIATE = (0x57, 0x75)


def crc_class(dsov, crc, plm):
    """The CRC_STATUS compass (a community member's method): 0x77 window closed, 0x75 / 0x57 (or any value between
    0x55 and 0x77) intermediate = next to the sweet spot, 0x00 over-glitch, goal DSOV 0 + CRC 0x55 + PLM all ones
    in the same read. A healthy locked read is DSOV 1 + CRC 0x55 + PLM all ones (runner 'under')."""
    if crc is None:
        return None
    if crc == 0x00:
        return "over"
    if crc == 0x55 and plm == 0xFFFFFFFF:
        if dsov == 0:
            return "goldilocks"
        if dsov == 1:
            return "under"
    if crc == 0x77:
        return "crc_closed"
    if crc in CRC_INTERMEDIATE or 0x55 < crc < 0x77:
        return "crc_intermediate"
    return "partial"


RE_REG = re.compile(r"\b(FUSECTRL|GFW|DSOV|CRC|PLM|SENTINEL)=(0x[0-9a-fA-F]+|-)")


def regs_from_observed(text):
    out = {}
    for name, val in RE_REG.findall(text or ""):
        out[name] = None if val == "-" else int(val, 16)
    return out


def classify(text):
    """Runner outcome text -> (outcome class, detail dict)."""
    t = (text or "").strip()
    first = t.split(" | re-read ")[0].strip()
    low = first.lower()
    if first.startswith("MISTIMED"):
        m = re.match(r"MISTIMED \((.*); observed (.*)\)$", first)
        return "mistimed", ({"reason": m.group(1), "observed": m.group(2)} if m else {"reason": first})
    if first.startswith("GOLDILOCKS"):
        return "goldilocks", {"kind": "passive_candidate"}
    if "NO DIP" in first:
        return "no_dip", {}
    if first.startswith("CONTROLLER OFF"):
        m = re.search(r"\((0x[0-9a-fA-F]+), (\d+) mV\)", first)
        return "controller_off", ({"status_word": m.group(1), "vout_mv": int(m.group(2))} if m else {})
    if first.startswith("controller unreadable"):
        # read failure right after a shot: scored as a controller latch so the never-rules stay conservative
        return "controller_off", {"unreadable": True}
    if first == "nack" or first.startswith("nack"):
        return "invalid", {"kind": "nack"}
    if first.startswith("hidden-poison"):
        m = re.search(r"alias (0x[0-9a-fA-F]+)=(0x[0-9a-fA-F]+)", first)
        return "hidden_poison", ({"alias": m.group(1), "value": m.group(2)} if m else {})
    if first.startswith("poisoned"):
        m = re.match(r"poisoned (\w+)=(0x[0-9a-fA-F]+)", first)
        if m:
            return "poisoned", {"register": m.group(1), "value": m.group(2)}
        if first.startswith("poisoned/over0"):
            return "over", {"kind": "over0", "crc": "0x00", "plm": "0x00000000"}
        return "poisoned", {"kind": first.split()[0]}
    if re.search(r"\blost\b", low):
        return "lost", {"park_failed": "park failed" in low}
    if first.startswith("under"):
        if "re-read" in t and ("poison" in t.lower() or "0xbad" in t.lower()):
            return "poisoned", {"kind": "re-read"}
        return "under", {}
    m = re.match(r"dsov(\d) crc=0x([0-9a-fA-F]+) priv=0x([0-9a-fA-F]+)", first)
    if m:
        dsov, crc, plm = int(m.group(1)), int(m.group(2), 16), int(m.group(3), 16)
        kind = "dsov0" if dsov == 0 else "dsov%d_corrupt" % dsov
        cls = crc_class(dsov, crc, plm) or "partial"
        if cls == "under":
            cls = "partial"   # the runner only prints this form for a non-healthy read
        return cls, {"kind": kind, "dsov": dsov, "crc": "0x%02x" % crc, "plm": "0x%08x" % plm}
    m = re.match(r"dsov=0x([0-9a-fA-F]{8})", first)
    if m:
        v = int(m.group(1), 16)
        if v >> 20 == 0xBAD:
            return "poisoned", {"register": "DSOV", "value": "0x%08x" % v}
        return "invalid", {"kind": "dsov_unexpected", "value": "0x%08x" % v}
    if first.startswith("booting"):
        return "invalid", {"kind": "gfw_booting"}
    if first.startswith("unread"):
        if "0xbad" in low:
            return "poisoned", {"kind": "unread_bad"}
        return "invalid", {"kind": "unread"}
    if first.startswith("passive candidate after an unstable read"):
        return "invalid", {"kind": "unstable_candidate"}
    return "invalid", {"kind": "unrecognised", "text": first[:120]}


def parse_mistimed_observed(text):
    """'MISTIMED (release_at=9 us; observed under)' -> 'under'."""
    return classify(text)[1].get("observed", text)


class _Block:
    """One shot's firmware output (glitch echo .. glitch done)."""

    def __init__(self, idx):
        self.first_line = idx
        self.lines = []
        self.echo = None
        self.warns = []
        self.release = None
        self.via = None
        self.done = None
        self.rail = None
        self.prime = None
        self.timing = []
        self.closed = False


class _Event:
    def __init__(self, kind, idx, text, step_i=None, shot_k=None, vals=None):
        self.kind, self.idx, self.text, self.step_i, self.shot_k, self.vals = kind, idx, text, step_i, shot_k, vals
        self.extras = []


class Section:
    def __init__(self, header=None, header_idx=None):
        self.header = header
        self.header_idx = header_idx
        self.lines = []          # (idx, text)
        self.complete = False


def split_sections(lines):
    """Split a log into boots: one per wrapper header ('########'); in bare bootrun output a new boot starts at a
    'cycle (' line, or at 'bench up' / 'PLAN SHA' once the current boot already printed START or RESULT."""
    secs = []
    cur = None
    for idx, line in enumerate(lines):
        if line.startswith("########"):
            if cur is not None:
                cur.complete = True  # the next boot started, so the previous boot's output ended
            cur = Section(line.strip(), idx)
            secs.append(cur)
            continue
        if RE_FOOTER.match(line):
            if cur is not None:
                cur.complete = True
            continue
        started = cur is not None and any(RE_START.match(l) or RE_RESULT.match(l) for _, l in cur.lines)
        new_bare = cur is None or (started and (RE_CYCLE.match(line) or (cur.header is None and (RE_UP.match(line) or RE_PLAN.match(line)))))
        if new_bare:
            if cur is not None:
                cur.complete = True
            cur = Section(None, None)
            secs.append(cur)
        cur.lines.append((idx, line.rstrip("\n")))
        if RE_RESULT.match(line):
            cur.complete = True

    def useful(s):
        return s.header is not None or any(RE_START.match(l) or RE_STEP.match(l) or RE_CELL.match(l) or RE_RESULT.match(l)
                                           or RE_RELEASE.match(l) for _, l in s.lines)
    return [s for s in secs if useful(s)]


RE_HDR_STEPS = re.compile(r"(\[\s*\[.*\]\s*\]|\[\s*-?\d+(?:\s*,\s*-?\d+){1,11}\s*\])")


def _header_steps(header):
    """A step list written into a queue script's header ('######## Q6 [331,-60,...] 04:28:49')."""
    m = RE_HDR_STEPS.search(header or "")
    if not m:
        return None
    try:
        v = json.loads(m.group(1))
    except ValueError:
        return None
    if v and all(isinstance(x, int) for x in v):
        v = [v]
    if v and all(isinstance(x, list) and len(x) >= 2 and all(isinstance(y, int) for y in x) for x in v):
        return v
    return None


def _load_seq(seq):
    if seq is None:
        return None
    if isinstance(seq, list):
        return seq
    if os.path.exists(seq):
        with open(seq) as f:
            return json.load(f)
    return json.loads(seq)


def _sha256(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 16), b""):
            h.update(chunk)
    return h.hexdigest()


def _int_or_none(s):
    try:
        return int(s)
    except (TypeError, ValueError):
        return None


def parse_file(path, card, seq=None, firmware=None, date=None, nocycle=False, park_each=False, conditions=None,
               repeat_default=None, header_seq=None):
    """Parse one log file. Returns (records, report) where report lists boots, warnings and ARMED records."""
    with open(path, "r", encoding="utf-8", errors="replace") as f:
        text = f.read()
    if date is None:
        date = _dt.datetime.fromtimestamp(os.path.getmtime(path)).date().isoformat()
    return parse_text(text, card=card, source_file=os.path.abspath(path), source_sha256=_sha256(path), seq=seq,
                      firmware=firmware, date=date, nocycle=nocycle, park_each=park_each, conditions=conditions,
                      repeat_default=repeat_default, header_seq=header_seq)


def parse_text(text, card, source_file="<text>", source_sha256=None, seq=None, firmware=None, date="1970-01-01",
               nocycle=False, park_each=False, conditions=None, repeat_default=None, header_seq=None):
    """header_seq: {regex: step list} for wrapper sections whose header does not carry the steps (queue scripts)."""
    lines = text.splitlines()
    seq_full = _load_seq(seq)
    report = {"boots": [], "warnings": [], "armed": 0, "full_logs": []}
    records = []
    secs = split_sections(lines)
    # push2 wrappers print 'full logs: DIR' and keep every boot's complete bootrun output as DIR/boot-N.log; use it
    # in place of the grep in the wrapper output (the header still supplies the step list and the boot number)
    full_dir = None
    for ln in lines:
        m = RE_FULL_LOGS.match(ln)
        if m:
            full_dir = m.group(1)
            report["full_logs"].append(full_dir)
        m_fs = RE_HDR_FS.match(ln) or RE_HDR_CAMP.match(ln)
        if m_fs and full_dir:
            n = int(m_fs.group(1))
            path = os.path.join(full_dir, "boot-%d.log" % n)
            for sec in secs:
                if sec.header == ln.strip() and getattr(sec, "full_path", None) is None and os.path.exists(path):
                    with open(path, "r", encoding="utf-8", errors="replace") as f:
                        flines = f.read().splitlines()
                    sec.lines = list(enumerate(flines))
                    sec.full_path = path
                    sec.complete = sec.complete or any(RE_RESULT.match(l) for l in flines)
                    break
    for bi, sec in enumerate(secs):
        fp = getattr(sec, "full_path", None)
        recs, info = _parse_section(sec, bi, len(secs), lines, card, seq_full, firmware, date, nocycle, park_each,
                                    conditions or {}, repeat_default, fp or source_file,
                                    _sha256(fp) if fp else source_sha256, header_seq)
        records.extend(recs)
        report["boots"].append(info)
        report["warnings"].extend("boot %s: %s" % (info["boot_id"], w) for w in info["warnings"])
        report["armed"] += sum(1 for r in recs if r["phase"] == "ARMED")
    return records, report


def _parse_section(sec, bi, nsecs, all_lines, card, seq_full, firmware, date, nocycle, park_each, conditions,
                   repeat_default, source_file, source_sha256, header_seq=None):
    warnings = []
    launcher, hdr_time, seq, seq_offset = "bootrun", None, None, 0
    if sec.header:
        m = RE_HDR_FS.match(sec.header)
        if m:
            launcher, hdr_time = "firstshot", m.group(4)
            try:
                seq = json.loads(m.group(3))
            except ValueError:
                seq = ast.literal_eval(m.group(3))
        else:
            m = RE_HDR_CAMP.match(sec.header)
            if m:
                launcher, hdr_time, seq_offset = "campaign", m.group(4), int(m.group(2))
                if seq_full is not None:
                    seq = seq_full[seq_offset:]
                else:
                    warnings.append("campaign header without --seq: geometry inferred from step/cell lines")
            else:
                m = RE_HDR_AB.match(sec.header)
                if m:
                    launcher, hdr_time = "abtest", m.group(2)
                    conditions = dict(conditions)
                    kv = m.group(1).split("=", 1)
                    if len(kv) == 2:
                        conditions[kv[0].lower()] = _int_or_none(kv[1]) if _int_or_none(kv[1]) is not None else kv[1]
                    seq = seq_full
                else:
                    m = RE_HDR_ANY.match(sec.header)
                    launcher, hdr_time = "wrapper", (m.group(2) if m else None)
                    seq = _header_steps(sec.header) or seq_full
    else:
        seq = seq_full
    for rx, hs in (header_seq or {}).items():
        if sec.header and re.search(rx, sec.header):
            seq = _load_seq(hs)
            break

    info = {"fw": None, "uptime": None, "cycle": None, "start": None, "temp0": None, "temp1": None, "env": None,
            "ident": None, "unlock": None, "bootpm": None, "result": None, "nshots_reported": None,
            "exhausted": False, "held": None, "boot_fail": None, "runner_rc": None, "sentinel_base": None,
            "end_park": None, "cycle_time": None, "host_boot_id": None}
    blocks, events, extras_pending, parks = [], [], [], []
    cur = None
    for idx, line in sec.lines:
        mf = RE_BOOT_FAIL.search(line)
        if mf:
            info["boot_fail"] = mf.group(0)
        m = RE_FW.search(line)
        if m and (line.startswith("pico v:") or line.startswith("pico:")):
            info["fw"] = m.group(1)
        m = RE_CYCLE.match(line)
        if m:
            info["cycle"], info["cycle_time"] = m.group(1), m.group(2)
            continue
        m = RE_UP.match(line)
        if m:
            info["uptime"] = int(m.group(1))
            info["host_boot_id"] = m.group(2)
            continue
        m = RE_START.match(line)
        if m:
            info["temp0"], info["start"] = _int_or_none(m.group(1)), m.group(2)
            continue
        m = RE_END.match(line)
        if m:
            info["temp1"] = _int_or_none(m.group(1))
            info["runner_rc"] = _int_or_none(m.group(2))
            continue
        m = RE_SENT_BASE.match(line)
        if m:
            info["sentinel_base"] = m.group(2)
            continue
        m = RE_ENV.match(line)
        if m:
            info["env"] = m.group(1).strip()
            continue
        m = RE_IDENT.match(line)
        if m:
            info["ident"] = m.group(1)
            continue
        m = RE_UNLOCK.match(line)
        if m:
            info["unlock"] = m.group(1)
            continue
        m = RE_BOOTPM.match(line)
        if m:
            info["bootpm"] = m.group(1)
            continue
        # firmware block lines
        m = RE_ECHO.match(line)
        if m:
            if cur is not None and not cur.closed:
                cur.closed = True
            cur = _Block(idx)
            blocks.append(cur)
            cur.echo = [int(x) if x.lstrip("-").isdigit() else x for x in m.groups()]
            cur.lines.append(line.strip())
            continue
        if RE_WARN.match(line) or RE_TIMING.match(line) or RE_RAIL.match(line) or RE_RELEASE.match(line) \
                or RE_DONE.match(line) or RE_ABORT.match(line) or RE_PRIME.match(line):
            # within one shot the firmware prints [echo] [WARN..] [rail_code] [timing..] release_at [done]; once a
            # block has its release line, anything but 'glitch done' belongs to the next shot
            if cur is None or cur.closed or (cur.release is not None and not RE_DONE.match(line)):
                cur = _Block(idx)
                blocks.append(cur)
            cur.lines.append(line.strip())
            mw = RE_WARN.match(line)
            if mw:
                cur.warns.append(mw.group(1).strip())
            mr = RE_RELEASE.match(line)
            if mr:
                cur.release, cur.via = int(mr.group(1)), mr.group(3)
            mt = RE_TIMING_F.match(line)
            if mt:
                cur.timing.append((int(mt.group(1)), int(mt.group(2)), int(mt.group(3))))
            mp = RE_PRIME.match(line)
            if mp:
                cur.prime = int(mp.group(1))
            mrc = RE_RAIL.match(line)
            if mrc:
                cur.rail = line.strip()
            md = RE_DONE.match(line)
            if md:
                cur.done = {"pwd": int(md.group(1)), "e1": "0x" + md.group(2).lower(),
                            "writes_failed": int(md.group(4)),
                            "nvvdd": float(md.group(3)) if md.group(3) else None}
                cur.closed = True
            if RE_ABORT.match(line):
                cur.closed = True
            continue
        # anything else closes an open block that already has its release line
        if cur is not None and not cur.closed and (cur.release is not None or cur.done is not None):
            cur.closed = True
        m = RE_PARK_END.match(line)
        if m:
            p = {"ok": m.group(1) == "0", "rc": int(m.group(1)), "text": m.group(2).strip(), "line": idx}
            mr = RE_PARK_RAIL.search(p["text"])
            p["gpu_rail_mv"] = int(mr.group(1)) if mr else None
            mc = RE_PARK_CFG.search(p["text"])
            p["config_after"] = mc.group(1).lower() if mc else None
            info["end_park"] = p
            continue
        m = RE_PARK.match(line)
        if m:
            p = {"ok": m.group(1) is None, "rc": _int_or_none(m.group(2)), "text": m.group(3).strip(), "line": idx}
            mr = RE_PARK_RAIL.search(p["text"])
            p["gpu_rail_mv"] = int(mr.group(1)) if mr else None
            mc = RE_PARK_CFG.search(p["text"])
            p["config_after"] = mc.group(1).lower() if mc else None
            parks.append(p)
            extras_pending.append(("park", p))
            continue
        for rex, key in ((RE_OBS, "observed"), (RE_OBS_NS, "observed"), (RE_HALF, "reread"), (RE_SLOW, "slow_link")):
            m = rex.match(line)
            if m:
                extras_pending.append((key, m.group(1)))
                break
        else:
            m = RE_STEP.match(line)
            if m:
                ev = _Event("step", idx, m.group(8), int(m.group(1)), _int_or_none(m.group(2)),
                            {"v": int(m.group(3)), "td": int(m.group(4)), "pw": int(m.group(5)),
                             "vr": int(m.group(6)), "fast": int(m.group(7))})
                ev.extras, extras_pending = extras_pending, []
                events.append(ev)
                continue
            m = RE_CELL.match(line)
            if m:
                try:
                    counts = ast.literal_eval(m.group(8))
                except (ValueError, SyntaxError):
                    counts = {}
                ev = _Event("cell", idx, m.group(2) or "", int(m.group(1)), None,
                            {"v": int(m.group(3)), "td": int(m.group(4)), "pulses": int(m.group(5)),
                             "pw": int(m.group(6)), "vr": int(m.group(7)), "counts": counts})
                events.append(ev)
                continue
            m = RE_MIST_AT.match(line)
            if m:
                ev = _Event("mistimed_at", idx, m.group(8), int(m.group(1)), int(m.group(2)),
                            {"v": int(m.group(3)), "td": int(m.group(4)), "pw": int(m.group(5)),
                             "vr": int(m.group(6)), "fast": int(m.group(7)), "observed": m.group(9)})
                mn = RE_NSHOTS.search(line)
                if mn:
                    info["nshots_reported"] = int(mn.group(1))
                events.append(ev)
                continue
            m = RE_TIPPED.match(line)
            if m:
                ev = _Event("tipped", idx, m.group(8), int(m.group(1)), _int_or_none(m.group(2)),
                            {"v": int(m.group(3)), "td": int(m.group(4)), "pw": int(m.group(5)),
                             "vr": int(m.group(6)), "fast": int(m.group(7))})
                ev.extras, extras_pending = extras_pending, []
                mn = RE_NSHOTS.search(line)
                if mn:
                    info["nshots_reported"] = int(mn.group(1))
                events.append(ev)
                continue
            if RE_STOP_RELEASE.match(line):
                ev = _Event("stop_release", idx, line.strip())
                ev.extras, extras_pending = extras_pending, []
                events.append(ev)
                continue
            m = RE_STOP_WRITES.match(line)
            if m:
                ev = _Event("stop_writes", idx, line.strip(), int(m.group(1)))
                ev.extras, extras_pending = extras_pending, []
                events.append(ev)
                continue
            m = RE_EXH.match(line)
            if m:
                info["exhausted"] = True
                info["exhausted_n"] = _int_or_none(m.group(1))
                continue
            m = RE_HELD.match(line)
            if m:
                info["held"] = m.group(1)
                continue
            m = RE_RESULT.match(line)
            if m:
                info["result"] = m.group(1).strip()
                continue
    for b in blocks:
        b.closed = True

    # Boot identity: the bench clock at START (same id whichever log of that boot is ingested), else the wrapper
    # header time (local clock), else file + line. armed_key ties a wrapper's ARMED record to the later COMPLETE.
    armed_base = ("%s@%sT%sL" % (card, date, hdr_time)) if (sec.header and hdr_time) else None
    if info["start"]:
        boot_id = "%s@%sT%sB" % (card, date, info["start"])
    elif armed_base:
        boot_id = armed_base
    else:
        boot_id = "%s@%s#%s:%d" % (card, date, os.path.basename(source_file), sec.header_idx or (sec.lines[0][0] if sec.lines else 0))
    # every bootrun power-cycles unless NOCYCLE=1 (firstshot.sh forces a cycle); wrapper output greps the cycle line
    # away, so a wrapper boot counts as fresh unless --nocycle; bare bootrun output must show its 'cycle (' line
    fresh = (not nocycle) if sec.header else (info["cycle"] is not None and not nocycle)
    # queue scripts name PARK_EACH in their headers; the runner itself never prints it
    if sec.header and re.search(r"\bPARK_EACH\b", sec.header) and not re.search(r"PARK_EACH=0", sec.header):
        park_each = True
    # launcher clock time of the boot: the wrapper header, else bootrun's own 'cycle (..): clean shutdown HH:MM:SS'
    # line (firstshot prints the header right before bootrun prints that line); used to spot the same boot ingested
    # from a filtered wrapper log and from its full per-boot log
    launch_time = ("%sT%s" % (date, hdr_time)) if hdr_time else (("%sT%s" % (date, info["cycle_time"])) if info["cycle_time"] else None)
    fw = firmware or info["fw"]
    env = info["env"] or ""
    repeat = repeat_default
    m = re.search(r"\bREPEAT=(\d+)", env)
    if m:
        repeat = int(m.group(1))
    if repeat is None:
        repeat = 1
    # Cell mode (runner CONT): REPEAT > 1 or a step with reps > 1. 'step i shot k' lines are cell-mode only, except
    # push2's MISTIMED step line, which always carries 'shot k'.
    cont = any(e.kind == "cell" for e in events) \
        or any(e.shot_k is not None for e in events if e.kind == "tipped") \
        or any(e.shot_k is not None and not e.text.strip().startswith("MISTIMED") for e in events if e.kind == "step") \
        or repeat > 1 or (seq is not None and launcher != "firstshot" and any(len(s) > 6 and s[6] > 1 for s in seq))

    fired_blocks = [b for b in blocks if b.release is not None or b.done is not None or b.echo is not None or b.warns]
    nblocks = len([b for b in fired_blocks if b.release is not None or b.done is not None]) or None
    shots = _rebuild_cont(events, seq, repeat, nblocks, warnings) if cont else _rebuild_single(events, warnings)
    if not shots and info["exhausted"] and seq:
        # cell mode with the cell lines grepped away: an exhausted sequence fired every SEQX shot, all under
        for i, s_ in enumerate(seq):
            for _ in range(s_[6] if len(s_) > 6 else repeat):
                shots.append({"step_i": i, "k": len(shots), "outcome_text": "under", "line": None, "extras": [],
                              "vals": None, "raw_line": None, "inferred_under": True})
        n_rep_ = info.get("exhausted_n") or nblocks
        if n_rep_ is not None and n_rep_ != len(shots):
            warnings.append("exhausted sequence: runner says %s shots, step list gives %d" % (n_rep_, len(shots)))
            shots = shots[:n_rep_]
    # push2: 'MISTIMED at step i shot k (...): why; outcome X. Not scored' -> that shot is mistimed, X kept
    for ev in events:
        if ev.kind != "mistimed_at":
            continue
        obs = (ev.vals.get("observed") or "").strip()
        if obs.startswith("MISTIMED"):
            obs = parse_mistimed_observed(obs)
        detail = {"reason": ev.text.strip(), "observed": obs}
        hit = [s for s in shots if s.get("step_i") == ev.step_i and (s.get("k") in (None, ev.shot_k) or not cont)]
        if hit:
            hit[-1]["forced"] = ("mistimed", detail)
        else:
            shots.append({"step_i": ev.step_i, "k": ev.shot_k, "outcome_text": obs, "line": ev.idx,
                          "extras": [], "vals": ev.vals, "raw_line": ev.text, "forced": ("mistimed", detail)})

    # firmware blocks -> shots (fire order)
    shots_with_fw = [s for s in shots if s["outcome_text"] != "nack"]
    if fired_blocks:
        if len(fired_blocks) == len(shots_with_fw):
            for s, b in zip(shots_with_fw, fired_blocks):
                s["block"] = b
        elif len(fired_blocks) == len(shots_with_fw) + 1 and not sec.complete:
            for s, b in zip(shots_with_fw, fired_blocks):
                s["block"] = b
            shots.append({"step_i": None, "k": None, "outcome_text": None, "line": fired_blocks[-1].first_line,
                          "block": fired_blocks[-1], "extras": [], "vals": None, "in_flight": True})
        else:
            warnings.append("%d firmware blocks for %d shots: release_at/WARN not attached" % (len(fired_blocks), len(shots_with_fw)))
    if info["nshots_reported"] is not None and info["nshots_reported"] != len([s for s in shots if not s.get("in_flight")]):
        warnings.append("runner reported %d shots, rebuilt %d" % (info["nshots_reported"], len(shots)))

    # A wrapper boot with a step but no outcome and no RESULT (in flight, or the launcher was killed): ARMED for its
    # first step. resolve() turns it into 'pending' while young and 'unknown' (no information) later.
    if not shots and info["result"] is None and not info["boot_fail"] and seq and \
            (launcher in ("firstshot", "campaign") or (launcher == "wrapper" and not sec.complete)):
        shots.append({"step_i": 0, "k": 0, "outcome_text": None, "line": sec.header_idx, "block": None, "extras": [],
                      "vals": None, "in_flight": True})

    recs = []
    parked_before = False
    for j, s in enumerate(shots):
        step_d, geo_src = _geometry(s, seq, repeat, warnings)
        if step_d is None:
            warnings.append("shot %d: no geometry, skipped" % j)
            continue
        if j == 0 and fresh:
            state = "first-shot-fresh-boot"
        elif park_each and j > 0:
            state = "post-park"
        elif parked_before:
            state = "post-park"
        else:
            state = "later-shot-same-boot"
        chk = schedule.check_step(step_d)
        b = s.get("block")
        rec = {
            "schema": ledger.SCHEMA, "phase": "ARMED" if s.get("in_flight") else "COMPLETE",
            "attempt_id": "%s#s%03d" % (boot_id, j), "card": str(card),
            "armed_key": ("%s#s%03d" % (armed_base, j)) if armed_base else None,
            "source": "log", "source_file": source_file, "source_sha256": source_sha256,
            "source_line": (s["line"] + 1) if s.get("line") is not None else None,
            "ingested_at": ledger.now_iso(),
            "launcher": launcher, "boot_id": boot_id, "boot_header": sec.header, "boot_index_in_log": bi,
            "boot_uptime_s": info["uptime"], "host_boot_id": info["host_boot_id"], "launch_time": launch_time,
            "park_each": bool(park_each), "cycle": info["cycle"] or ("assumed" if fresh else None),
            "fresh_boot": fresh, "shot_in_boot": j, "seqx_index": s.get("k"),
            "step_index": (s["step_i"] + seq_offset) if s.get("step_i") is not None else None,
            "state_class": state, "firmware": fw,
            "temp_raw_start": info["temp0"], "temp_raw_end": info["temp1"], "run_env": info["env"],
            "step": step_d, "step_list": S.to_list(step_d), "geometry_source": geo_src,
            "vid_code": mv_to_code(step_d["v"]), "vr_code": mv_to_code(step_d["vr"]),
            "energy_us": S.energy(dict(step_d, pw=chk["resolved"].get("pw", step_d["pw"]))),
            "resolved": chk["resolved"], "write_overlaps_release": chk["overlaps"],
            "overwritten_vout": chk["overwritten"],
            "release_at": b.release if b else None, "release_via": b.via if b else None,
            "warns": list(b.warns) if b else [], "rail_code": b.rail if b else None,
            "writes_failed": (b.done or {}).get("writes_failed") if b else None,
            "pwd": (b.done or {}).get("pwd") if b else None, "e1": (b.done or {}).get("e1") if b else None,
            "conditions": dict(conditions), "notes": [],
            "raw": [],
        }
        if b:
            rec["raw"].extend(b.lines)
        if b and b.echo:
            e = b.echo
            echo_res = {"pw": e[7], "gap": e[8], "delay": e[9], "reset_at": e[11], "dither": e[12], "hold_at": e[13]}
            for k_, idx_ in (("delay", 9), ("gap", 8)):
                if step_d.get(k_) != e[idx_] and not (k_ == "gap" and e[idx_] == max(36, step_d.get(k_, 150))):
                    rec["notes"].append("echo %s=%s but the step list says %s" % (k_, e[idx_], step_d.get(k_)))
            for k_, v_ in echo_res.items():
                if chk["resolved"].get(k_) != v_:
                    rec["notes"].append("echo %s=%s differs from the schedule model %s" % (k_, v_, chk["resolved"].get(k_)))
            rec["resolved_echo"] = echo_res
        if s.get("in_flight"):
            rec["outcome"] = None
            rec["armed_at"] = _armed_at(date, hdr_time)
            rec["notes"].append("no outcome in the log (in flight or launcher killed)")
        else:
            cls, detail = classify(s["outcome_text"])
            if s.get("forced"):
                cls, detail = s["forced"]
            rec["runner_outcome"] = s["outcome_text"]
            rec["outcome"], rec["outcome_detail"] = cls, detail
            if s.get("raw_line") is not None:
                rec["raw"].append(s["raw_line"])
        _set_crc(rec)
        park = None
        for kind, val in s.get("extras", []):
            if kind == "park":
                park = val
            elif kind == "observed":
                rec["observed"] = val
                rec["raw"].append("observed: " + val)
                _set_crc(rec, regs_from_observed(val))
            elif kind == "reread":
                rec.setdefault("notes", []).append("half-boot re-read: " + val)
            elif kind == "slow_link":
                rec["slow_link_s"] = float(val)
        if park:
            rec["park"] = {k_: park[k_] for k_ in ("ok", "rc", "gpu_rail_mv", "config_after", "text")}
            rec["raw"].append("park: " + park["text"])
        parked_before = bool(park and park["ok"])
        if b is not None and getattr(b, "prime", None) is not None:
            rec["prime_at"] = b.prime
        recs.append(rec)
    if recs and info["end_park"]:
        p = info["end_park"]
        recs[-1]["park_end"] = {k_: p[k_] for k_ in ("ok", "rc", "gpu_rail_mv", "config_after", "text")}
    for r in recs:
        r["runner_rc"] = info["runner_rc"]
        if info["sentinel_base"]:
            r["sentinel_baseline"] = info["sentinel_base"]

    binfo = {"boot_id": boot_id, "launcher": launcher, "header": sec.header, "firmware": fw, "fresh": fresh,
             "shots": len([r for r in recs if r["phase"] == "COMPLETE"]),
             "armed": len([r for r in recs if r["phase"] == "ARMED"]), "result": info["result"],
             "complete": sec.complete, "warnings": warnings, "cont": cont}
    if not sec.complete and not recs and sec.header and seq:
        warnings.append("boot section has no outcome yet")
    return recs, binfo


def _set_crc(rec, regs=None):
    """DSOV / CRC / PLM of the shot's first passive read: the observed line when printed (every non-under shot on
    push2), else the verdict text, else implied by 'under' (DSOV 1, CRC 0x55, PLM ffffffff). Lost shots have none."""
    if regs:
        rec["regs"] = {k: ("0x%08x" % v if v is not None else None) for k, v in regs.items()}
        for k in ("DSOV", "CRC", "PLM"):
            if k in regs:
                rec[k.lower()] = regs[k]
        rec["crc_source"] = "observed"
        cls = crc_class(regs.get("DSOV"), regs.get("CRC"), regs.get("PLM")) if regs.get("CRC") is not None else None
        if cls and rec.get("outcome") in ("partial", "invalid") and cls not in ("under",):
            rec["outcome"] = cls
        return
    if rec.get("crc_source") == "observed":
        return
    det = rec.get("outcome_detail") or {}
    if "crc" in det and det.get("crc") is not None:
        rec["crc"] = int(det["crc"], 16)
        rec["plm"] = int(det["plm"], 16) if det.get("plm") else None
        rec["dsov"] = det.get("dsov")
        rec["crc_source"] = "verdict"
    elif rec.get("outcome") == "under":
        rec["dsov"], rec["crc"], rec["plm"] = 1, 0x55, 0xFFFFFFFF
        rec["crc_source"] = "implied_under"
    else:
        rec.setdefault("crc", None)
        rec.setdefault("plm", None)
        rec.setdefault("dsov", None)
        rec.setdefault("crc_source", None)


def _armed_at(date, hms):
    if not hms:
        return None
    try:
        t = _dt.datetime.fromisoformat("%sT%s" % (date, hms))
    except ValueError:
        return None
    return t.astimezone().isoformat(timespec="seconds")


def _rebuild_single(events, warnings):
    """One shot per step line (REPEAT 1, reps 1). STOP lines that end a shot without a step line add that shot."""
    shots = []
    last_i = -1
    for e in events:
        if e.kind == "step":
            shots.append({"step_i": e.step_i, "k": e.step_i, "outcome_text": e.text.strip(), "line": e.idx,
                          "extras": e.extras, "vals": e.vals, "raw_line": "step %d: %s" % (e.step_i, e.text.strip())})
            last_i = e.step_i
        elif e.kind == "stop_release":
            i = last_i + 1
            shots.append({"step_i": i, "k": i, "outcome_text": e.text, "line": e.idx, "extras": e.extras,
                          "vals": None, "forced": ("mistimed", {"kind": "release_gate"}), "raw_line": e.text})
            last_i = i
        elif e.kind == "stop_writes":
            if shots and shots[-1]["step_i"] == e.step_i:
                shots[-1]["forced"] = ("invalid", {"kind": "writes_failed"})
            else:
                shots.append({"step_i": e.step_i, "k": e.step_i, "outcome_text": e.text, "line": e.idx,
                              "extras": e.extras, "vals": None, "forced": ("invalid", {"kind": "writes_failed"}),
                              "raw_line": e.text})
            last_i = e.step_i
        elif e.kind == "tipped":
            if not shots or shots[-1]["step_i"] != e.step_i:
                warnings.append("TIPPED at step %d without its step line" % e.step_i)
                shots.append({"step_i": e.step_i, "k": e.step_i, "outcome_text": e.text, "line": e.idx,
                              "extras": e.extras, "vals": e.vals, "raw_line": e.text})
    return shots


def _rebuild_cont(events, seq, repeat, nblocks, warnings):
    """Cell mode: shots come from cell summaries + 'step i shot k' lines + the terminal line, laid out on the
    runner's SEQX (step i repeated reps_i times; k is the SEQX index, which keeps counting over skipped shots).

    Terminal lines end the boot on the shot they describe: TIPPED (has k), or a step line without 'shot k'
    (CONTROLLER OFF, NO DIP, nack), or a release / write STOP. When the terminal cell printed no summary, its shot
    count comes from k, else from the firmware blocks (one release_at per fired shot), else it is a lower bound.
    """
    reps = [(s[6] if len(s) > 6 else repeat) for s in seq] if seq is not None else None
    cells, listed, term = {}, {}, None
    for e in events:
        if e.kind == "cell":
            cells[e.step_i] = e
        elif e.kind == "step" and e.shot_k is not None:
            listed.setdefault(e.step_i, {})[e.shot_k] = e
        elif e.kind in ("step", "tipped", "stop_release", "stop_writes"):
            if e.kind == "tipped" and term is not None and term.kind == "step" and term.step_i == e.step_i:
                continue
            term = e
    if term is not None and term.step_i is None:
        term.step_i = (max(cells) + 1) if cells else 0
    idxs = set(cells) | set(listed) | ({term.step_i} if term is not None else set())
    if not idxs:
        return []
    max_i = max(idxs)
    if reps is None:
        reps = []
        for i in range(max_i + 1):
            c = cells.get(i)
            reps.append(sum(c.vals["counts"].values()) if (c is not None and not c.text) else None)
    start_k, acc = [], 0
    for r in reps:
        start_k.append(acc)
        acc = acc + r if (acc is not None and r is not None) else None
    while len(start_k) <= max_i:
        start_k.append(None)

    counts_n = {}
    for i in range(max_i + 1):
        if i in cells:
            counts_n[i] = sum(cells[i].vals["counts"].values())
    shots = []
    fired_before = 0
    for i in range(max_i + 1):
        c = cells.get(i)
        k0 = start_k[i]
        mine = listed.get(i, {})
        is_term = term is not None and term.step_i == i
        if c is not None:
            n = counts_n[i]
        elif is_term:
            if term.shot_k is not None and k0 is not None:
                n = term.shot_k - k0 + 1
            elif nblocks is not None:
                n = nblocks - fired_before + (1 if (term.kind == "step" and term.text.strip() == "nack") else 0)
            else:
                n = len(mine) + 1
                warnings.append("cell %d: no summary and no release_at lines; earlier under shots in it unknown" % i)
        elif mine:
            n = len(mine)
            warnings.append("cell %d has no summary: only its non-under shots are known" % i)
        else:
            continue
        n = max(n, 1)
        for j in range(n):
            k = (k0 + j) if k0 is not None else None
            ev = mine.get(k) if k is not None else None
            last = j == n - 1
            if ev is None and k is None and mine and j >= n - len(mine):
                ev = list(mine.values())[j - (n - len(mine))]
            if ev is not None:
                shots.append({"step_i": i, "k": k, "outcome_text": ev.text.strip(), "line": ev.idx,
                              "extras": ev.extras, "vals": ev.vals,
                              "raw_line": "step %d shot %s: %s" % (i, k, ev.text.strip())})
            elif is_term and ((term.shot_k is not None and term.shot_k == k) or (term.shot_k is None and last)):
                forced = None
                if term.kind == "stop_release":
                    forced = ("mistimed", {"kind": "release_gate"})
                elif term.kind == "stop_writes":
                    forced = ("invalid", {"kind": "writes_failed"})
                shots.append({"step_i": i, "k": k, "outcome_text": term.text.strip(), "line": term.idx,
                              "extras": term.extras, "vals": term.vals, "forced": forced,
                              "raw_line": term.text.strip()})
            else:
                shots.append({"step_i": i, "k": k, "outcome_text": "under", "line": c.idx if c is not None else None,
                              "extras": [], "vals": c.vals if c is not None else None, "raw_line": None,
                              "inferred_under": True})
        fired_before += n
        if c is not None:
            nonunder_n = n - c.vals["counts"].get("under", 0)
            seen = sum(1 for s in shots if s["step_i"] == i and not s.get("inferred_under")
                       and classify(s["outcome_text"])[0] != "under")
            if seen != nonunder_n:
                warnings.append("cell %d: summary says %d non-under, rebuilt %d" % (i, nonunder_n, seen))
    return shots


def _from_echo(block):
    """Geometry from the firmware echo; pulses from the timing lines (dip writes at the dip code, or code + 1 on the
    odd pulses of a dither). The echo shows the resolved pw (after the 36 us clamp)."""
    e = block.echo
    vd, td, vr, tr, tv, fast, hold, pw, gap, delay, revert, reset_at, dither, hold_at = e
    code = mv_to_code(vd)
    dips = [t for t, act, arg in block.timing
            if act == schedule.GA_VOUT and t > reset_at and (arg == code or (dither and arg == code + 1))]
    if not dips:
        return None
    return {"v": vd, "td": td, "pulses": len(dips), "pw": pw, "vr": vr, "fast": fast, "reps": 1, "hold": hold,
            "t_restore": reset_at if reset_at != -1000 else tr, "revert": 2 if dither else revert,
            "delay": delay, "gap": gap}


def _geometry(shot, seq, repeat, warnings):
    """Step dict for a shot: the step list when known, else the firmware echo + timing, else the step/cell/TIPPED
    line values (pulses then default to the runner's 3; flagged as log_line)."""
    i = shot.get("step_i")
    vals = shot.get("vals") or {}
    b = shot.get("block")
    if seq is not None and i is not None and 0 <= i < len(seq):
        d = S.from_list(seq[i], repeat_default=repeat)
        for k_ in ("v", "td", "pw", "vr", "fast", "pulses"):
            if k_ in vals and vals[k_] != d[k_]:
                warnings.append("step %d: log says %s=%s, step list says %s" % (i, k_, vals[k_], d[k_]))
        return d, "step_list"
    if b is not None and b.echo and b.timing:
        d = _from_echo(b)
        if d is not None:
            return d, "echo_timing"
    if vals:
        d = S.from_list([vals["v"], vals["td"], vals.get("pulses"), vals["pw"], vals["vr"], vals.get("fast")],
                        repeat_default=repeat)
        return d, "log_line"
    return None, None
