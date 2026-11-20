"""Append-only JSONL ledger, one record per attempted shot.

Record phases (plus VOID: {"phase": "VOID", "attempt_id": X, "reason": ...} withdraws record X, e.g. a duplicate;
the ledger itself is never rewritten):
  ARMED     written (and fsynced) before a shot is sent; the future runner owns this. The log parser also writes
            ARMED for a boot whose header shows a step but whose outcome is not in the log yet (in flight, or the
            launcher was killed).
  COMPLETE  the outcome. A COMPLETE with the same attempt_id supersedes the ARMED record.
An ARMED record without a COMPLETE is never read as 'under': it is 'pending' while young (the planner will not
propose that point again) and 'unknown' (no information) after PENDING_TTL_S.

The ledger keeps facts (what the runner printed and what the firmware schedule implies). Interpretation lives in
effective_outcome(), driven by a family policy, so a stricter timing rule can be applied later without rewriting
history.
"""
import datetime as _dt
import json
import os

SCHEMA = 1
OUTCOMES = ("under", "lost", "poisoned", "hidden_poison", "partial", "goldilocks", "controller_off", "no_dip",
            "mistimed", "invalid", "crc_intermediate", "crc_closed", "over")
TOO_STRONG = frozenset(("lost", "poisoned", "hidden_poison", "controller_off", "over"))
SIGNAL = frozenset(("partial", "crc_intermediate"))
WEAK = frozenset(("under", "crc_closed"))
NO_INFO = frozenset(("mistimed", "invalid", "no_dip", "pending", "unknown", "excluded"))
STATE_CLASSES = ("first-shot-fresh-boot", "later-shot-same-boot", "post-park")
PENDING_TTL_S = 20 * 60


def now_iso():
    return _dt.datetime.now().astimezone().isoformat(timespec="seconds")


def read(path, warn=None):
    """All records in file order. A torn last line (crash mid-write) is skipped with a warning, never fatal."""
    out = []
    if not os.path.exists(path):
        return out
    with open(path, "r", encoding="utf-8") as f:
        lines = f.readlines()
    for n, line in enumerate(lines, 1):
        line = line.strip()
        if not line:
            continue
        try:
            out.append(json.loads(line))
        except ValueError:
            msg = "ledger %s line %d unreadable, skipped" % (path, n)
            if warn is not None:
                warn.append(msg)
    return out


def append(path, records):
    """Append records, flush and fsync before returning (the record must survive a crash of the caller)."""
    if not records:
        return 0
    d = os.path.dirname(os.path.abspath(path))
    os.makedirs(d, exist_ok=True)
    with open(path, "a", encoding="utf-8") as f:
        for r in records:
            f.write(json.dumps(r, sort_keys=True, separators=(",", ":")) + "\n")
        f.flush()
        os.fsync(f.fileno())
    return len(records)


DUP_WINDOW_S = 5


def _launch_ts(r):
    t = r.get("launch_time")
    if not t:
        return None
    try:
        return _dt.datetime.fromisoformat(t).timestamp()
    except ValueError:
        return None


def same_shot(a, b):
    """Two COMPLETE records for one physical shot logged twice (filtered wrapper log + full per-boot log): same card,
    shot index and step, and the same kernel boot id or launcher clock times within DUP_WINDOW_S."""
    if a.get("attempt_id") == b.get("attempt_id"):
        return True
    if str(a.get("card")) != str(b.get("card")) or a.get("shot_in_boot") != b.get("shot_in_boot"):
        return False
    if (a.get("step_list") or [])[:6] != (b.get("step_list") or [])[:6] or \
            (a.get("step_list") or [])[7:] != (b.get("step_list") or [])[7:]:
        return False
    if a.get("host_boot_id") and b.get("host_boot_id"):
        return a["host_boot_id"] == b["host_boot_id"]
    ia, ib = str(a.get("attempt_id", "")).split("#")[0], str(b.get("attempt_id", "")).split("#")[0]
    if ia.endswith("B") and ib.endswith("B"):
        return False   # two different bench START times: two boots
    ta, tb = _launch_ts(a), _launch_ts(b)
    return ta is not None and tb is not None and abs(ta - tb) <= DUP_WINDOW_S


def _richness(r):
    return (str(r.get("attempt_id", "")).split("#")[0].endswith("B"), r.get("release_at") is not None,
            bool(r.get("host_boot_id")), len(r.get("raw") or []))


def append_new(path, records):
    """Append only records that are not already in the ledger: same (attempt_id, phase), or a COMPLETE for a shot the
    ledger already holds from another log of the same boot (same_shot). Returns (added, skipped)."""
    old = read(path)
    have = {(r.get("attempt_id"), r.get("phase")) for r in old}
    done = [r for r in old if r.get("phase", "COMPLETE") == "COMPLETE"]
    new, seen = [], set()
    for r in records:
        key = (r.get("attempt_id"), r.get("phase"))
        if key in have or key in seen:
            continue
        if r.get("phase", "COMPLETE") == "COMPLETE" and any(same_shot(r, x) for x in done):
            continue
        seen.add(key)
        new.append(r)
        if r.get("phase", "COMPLETE") == "COMPLETE":
            done.append(r)
    append(path, new)
    return len(new), len(records) - len(new)


def void(path, attempt_id, reason):
    """Withdraw a record without rewriting the ledger."""
    append(path, [{"schema": SCHEMA, "phase": "VOID", "attempt_id": attempt_id, "reason": reason,
                   "ingested_at": now_iso()}])


def arm(path, record):
    """Future runner API: persist the intent to fire before sending 'g'."""
    r = dict(record)
    r["phase"] = "ARMED"
    r.setdefault("armed_at", now_iso())
    r.setdefault("schema", SCHEMA)
    append(path, [r])
    return r


def complete(path, attempt_id, outcome, **fields):
    """Future runner API: persist the outcome before the next attempt."""
    if outcome not in OUTCOMES:
        raise ValueError("unknown outcome %r" % outcome)
    armed = [r for r in read(path) if r.get("attempt_id") == attempt_id and r.get("phase") == "ARMED"]
    base = dict(armed[-1]) if armed else {"attempt_id": attempt_id, "schema": SCHEMA}
    base.update(fields)
    base.update(phase="COMPLETE", outcome=outcome, completed_at=now_iso())
    append(path, [base])
    return base


def _parse_time(s):
    if not s:
        return None
    try:
        t = _dt.datetime.fromisoformat(s)
    except ValueError:
        return None
    if t.tzinfo is None:
        t = t.astimezone()
    return t


def resolve(records, now=None, ttl_s=PENDING_TTL_S):
    """One record per attempt_id: the last COMPLETE, else the ARMED marked pending/unknown by age."""
    now = now or _dt.datetime.now().astimezone()
    voided = {r.get("attempt_id") for r in records if r.get("phase") == "VOID"}
    records = [r for r in records if r.get("phase") != "VOID" and r.get("attempt_id") not in voided]
    # one physical shot logged twice (filtered wrapper log + its full per-boot log): keep the richer record
    done = [r for r in records if r.get("phase", "COMPLETE") == "COMPLETE" and r.get("launch_time")]
    drop = set()
    by_shot = {}
    for r in done:
        sl = list(r.get("step_list") or [])
        by_shot.setdefault((str(r.get("card")), r.get("shot_in_boot"), tuple(sl[:6] + sl[7:])), []).append(r)
    for group in by_shot.values():
        group = sorted(group, key=_richness, reverse=True)
        for i, a in enumerate(group):
            if a.get("attempt_id") in drop:
                continue
            for b in group[i + 1:]:
                if b.get("attempt_id") != a.get("attempt_id") and same_shot(a, b):
                    drop.add(b.get("attempt_id"))
    if drop:
        records = [r for r in records if r.get("attempt_id") not in drop]
    # a COMPLETE may carry the armed_key of the ARMED record it settles (wrapper header id vs bench START id)
    settled = set()
    for r in records:
        if r.get("phase", "COMPLETE") == "COMPLETE":
            settled.add(r.get("attempt_id"))
            if r.get("armed_key"):
                settled.add(r["armed_key"])
    records = [r for r in records if r.get("phase", "COMPLETE") == "COMPLETE"
               or not ({r.get("attempt_id"), r.get("armed_key")} & (settled - {None}))
               or any(x.get("attempt_id") == r.get("attempt_id") and x.get("phase", "COMPLETE") == "COMPLETE"
                      for x in records)]
    by_id, order = {}, []
    for r in records:
        aid = r.get("attempt_id")
        if aid not in by_id:
            by_id[aid] = []
            order.append(aid)
        by_id[aid].append(r)
    out = []
    for aid in order:
        rs = by_id[aid]
        done = [r for r in rs if r.get("phase", "COMPLETE") == "COMPLETE"]
        if done:
            out.append(done[-1])
            continue
        a = dict(rs[-1])
        t = _parse_time(a.get("armed_at")) or _parse_time(a.get("ingested_at"))
        age = (now - t).total_seconds() if t else None
        a["outcome"] = "pending" if (age is not None and 0 <= age < ttl_s) else "unknown"
        a["armed_age_s"] = age
        out.append(a)
    return out


DEFAULT_POLICY = {
    "firmware": None,                 # list of accepted firmware tags (None = any, including unknown)
    "release_window_us": [-15, 15],   # push1 runner gate; push2 families use [0, 4]
    "require_release": False,         # True: a record without release_at is no information
    "overlap_is_mistimed": True,      # a write overlapping t = 0 makes the shot a different event
    "exclude_conditions": {"nvvdd_probe": True},
}


def effective_outcome(rec, policy=None):
    """(class, reason) after the family policy. Classes: OUTCOMES plus pending/unknown/excluded."""
    p = dict(DEFAULT_POLICY)
    p.update(policy or {})
    out = rec.get("outcome")
    for k, v in (p.get("exclude_conditions") or {}).items():
        if (rec.get("conditions") or {}).get(k) == v:
            return "excluded", "condition %s=%s" % (k, v)
    fw = p.get("firmware")
    if fw and rec.get("firmware") not in fw:
        return "excluded", "firmware %s not in %s" % (rec.get("firmware"), fw)
    if out in ("pending", "unknown"):
        return out, "ARMED without COMPLETE"
    if out not in OUTCOMES:
        return "invalid", "unrecognised outcome %r" % (out,)
    if out in ("goldilocks", "partial", "crc_intermediate"):
        # never discard a hit on a timing technicality; it is flagged for review instead
        return out, None
    if rec.get("overwritten_vout"):
        return "invalid", "VOUT step before the setup write (overwritten)"
    if p.get("overlap_is_mistimed") and rec.get("write_overlaps_release"):
        return "mistimed", "PMBus write overlaps the release at %s us" % rec["write_overlaps_release"]
    ra = rec.get("release_at")
    win = p.get("release_window_us")
    if ra is None:
        if p.get("require_release") and out not in NO_INFO:
            return "mistimed", "no release_at recorded"
    elif win and not (win[0] <= ra <= win[1]):
        return "mistimed", "release_at %d outside %s" % (ra, win)
    if rec.get("release_via") not in (None, "alarm"):
        return "mistimed", "release via %s" % rec.get("release_via")
    return out, None
