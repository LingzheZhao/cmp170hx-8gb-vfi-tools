"""Next-shot planner over the ledger.

A family fixes a waveform geometry (base), a state class (first-shot-fresh-boot by default), a primary search knob
(VID code by default; pw or vr also work) and a list of columns: one-knob overrides of the base (vr 625, pw 100,
td -50, ...). The planner maps outcomes per column and primary value and picks the next shots:

  lock-in   any goldilocks in the family: repeat the best recipe as first shots, track the Wilson lower bound and
            report LOCKED when it reaches the target.
  densify   any partial: repeat it, then its primary neighbours and fine steps of td/pw/vr around it.
  edge      bisection per column on the primary knob between the deepest pure 'under' (U) and the shallowest value
            that ever tipped (S). Adjacent U|S needs N informative shots from independent boots at both values before
            the edge is declared. Columns other than the base open only once the base edge is declared (or the base
            ran all-under to the floor / all-tipped to the top); a fresh column starts at the base edge, or at the
            column's own prior-firmware bracket when the family names a prior policy.

Outcome meaning: lost / poisoned / hidden_poison / controller_off = too strong; mistimed / invalid / no_dip /
unknown / excluded = no information (the point stays needed, so it is proposed again); partial = signal;
goldilocks = lock-in. A pending ARMED shot blocks its column until it completes or goes stale.

Batches: the planner fills a batch by lookahead. A shot whose outcome is predictable (a repeat of a value whose
outcomes so far all agree) is assumed to come out that way and planning continues; a shot with an open outcome
(bisection midpoint, mixed value, fresh column) ends planning in its column for this batch.
"""
import json
import os

from . import ledger, schedule, stats, steps as S
from .rules import Rules
from .vid import canonical_mv, mv_to_code

GEO = ("td", "pulses", "pw", "vr", "fast", "hold", "t_restore", "revert", "delay", "gap")
KEY_FIELDS = ("pulses", "pw", "vr", "fast", "hold", "t_restore", "revert", "gap")
FAMILY_DEFAULTS = {
    "state_class": "first-shot-fresh-boot",
    "primary": {"knob": "code", "start_mv": 450, "coarse_step": 4, "column_step": 1},
    "columns": [],
    "repeats": 2,
    "policy": {},
    "prior_policy": None,
    "densify": {"td": 2, "pw": 5, "vr": 25},
    "lockin": {"target": 0.9, "confidence": 0.95},
    "batch": 4,
}
OUTCOME_GROUP = {"under": "under", "crc_closed": "under", "lost": "strong", "poisoned": "strong",
                 "hidden_poison": "strong", "controller_off": "strong", "over": "strong", "partial": "partial",
                 "crc_intermediate": "partial", "goldilocks": "gold"}


def geo_key(st, rpw, knob=None):
    """Waveform identity without the knob being searched. td and delay enter the firmware only as td - delay, so
    the key holds t0 = td - delay (both are dropped when the knob is td or delay: the value carries them)."""
    g = S.normalize(st)
    g["pw"] = rpw
    k = [(f, g[f]) for f in KEY_FIELDS if f != knob]
    if knob not in ("td", "delay"):
        k.append(("t0", S.t0(g)))
    if knob != "code":
        k.append(("code", mv_to_code(g["v"])))
    return tuple(sorted(k))


def load_json(path_or_obj):
    if isinstance(path_or_obj, dict):
        return dict(path_or_obj)
    with open(path_or_obj) as f:
        return json.load(f)


def load_family(path_or_obj):
    fam = load_json(path_or_obj)
    out = json.loads(json.dumps(FAMILY_DEFAULTS))
    for k, v in fam.items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k].update(v)
        else:
            out[k] = v
    base = {"pulses": 3, "pw": 198, "vr": 750, "fast": 1, "hold": 0, "t_restore": -20, "revert": 0, "td": -60,
            "delay": S.RUNNER_DELAY_US, "gap": S.RUNNER_GAP_US}
    base.update(out.get("base") or {})
    out["base"] = base
    return out


def resolved_pw(d):
    chk = schedule.check_step(dict(d, v=d.get("v", 400)))
    return chk["resolved"].get("pw", d["pw"]) if not chk["abort"] else d["pw"]


class Column:
    def __init__(self, idx, overrides, family):
        self.idx = idx
        self.overrides = dict(overrides)
        self.knob = family["primary"]["knob"]
        self.geo = dict(family["base"])
        self.geo.update(self.overrides)
        self.label = "base" if not self.overrides else " ".join("%s %s" % kv for kv in sorted(self.overrides.items()))

    def key(self):
        g = dict(self.geo)
        if "v" not in g:
            g["v"] = 400
        return geo_key(g, resolved_pw(g), self.knob)

    @staticmethod
    def record_key(rec, knob):
        st = rec["step"]
        return geo_key(st, (rec.get("resolved") or {}).get("pw", st["pw"]), knob)

    def value_of(self, rec):
        st = S.normalize(rec["step"])
        if self.knob == "code":
            return mv_to_code(st["v"])
        if self.knob == "delay":   # the delay that gives this waveform at the column's td
            return int(self.geo["td"]) - S.t0(st)
        if self.knob == "td":
            return S.t0(st) + int(self.geo.get("delay", S.RUNNER_DELAY_US))
        return st[self.knob]

    def step_for(self, value, prefer_mv=()):
        d = dict(self.geo)
        if self.knob == "code":
            d["v"] = canonical_mv(value, prefer_mv)
        else:
            d[self.knob] = value
        d["reps"] = 1
        return d


class Cell:
    __slots__ = ("under", "strong", "partial", "gold", "noinfo", "pending", "boots", "recs", "mv")

    def __init__(self):
        self.under = self.strong = self.partial = self.gold = self.noinfo = self.pending = 0
        self.boots, self.recs, self.mv = set(), [], []

    @property
    def info(self):
        return self.under + self.strong + self.partial + self.gold

    def tag(self):
        parts = []
        for name in ("under", "strong", "partial", "gold", "noinfo", "pending"):
            n = getattr(self, name)
            if n:
                parts.append("%s %d" % (name, n))
        return ", ".join(parts) or "-"


def sample_id(r):
    """Independence unit for repeat counts: the boot for first shots, the shot itself for a park-normalized
    (post-park) shot, which the card E families treat as equivalent to a fresh boot's first shot."""
    return r.get("attempt_id") if r.get("state_class") == "post-park" else r.get("boot_id")


def family_states(family):
    """State classes a family pools: its own plus 'equivalent_states' (e.g. post-park after PARK_EACH)."""
    return {family["state_class"]} | set(family.get("equivalent_states") or [])


def _cells(col, recs, policy, hypo=()):
    cells = {}
    for r in list(recs) + list(hypo):
        eff, _ = (r["_eff"], None) if "_eff" in r else ledger.effective_outcome(r, policy)
        c = cells.setdefault(col.value_of(r), Cell())
        c.recs.append(r)
        c.mv.append(r["step"]["v"])
        grp = OUTCOME_GROUP.get(eff)
        if eff == "pending":
            c.pending += 1
        elif grp is None:
            c.noinfo += 1
        else:
            setattr(c, grp, getattr(c, grp) + 1)
            c.boots.add(sample_id(r))
    return cells


def _ladder(family, col, rules, data_values):
    p = family["primary"]
    if col.knob == "code":
        # the ladder reaches up to max_mv so a column that tips at its start can still climb weaker
        top = max(mv_to_code(p.get("start_mv", 450)), mv_to_code(p.get("max_mv", 525)))
        bottom = mv_to_code(rules.floor_mv) if rules.floor_mv else mv_to_code(p.get("min_mv", 300))
        if data_values:
            top, bottom = max(top, max(data_values)), min(bottom, min(data_values))
        return list(range(top, bottom - 1, -1))   # weak -> strong
    lo, hi, st = p.get("lo"), p.get("hi"), p.get("step", 5)
    if lo is None or hi is None:
        raise ValueError("primary knob %s needs lo and hi" % col.knob)
    vals = set(range(lo, hi + 1, st)) | set(data_values)
    stronger_up = p.get("stronger", "up") == "up"
    return sorted(vals) if stronger_up else sorted(vals, reverse=True)


def analyze_column(col, cells, ladder, n_rep, allowed, hint=None, coarse=4, col_step=1, start_value=None):
    """Return dict(status, U, S, moves=[{value, reason, expected, group}], edge)."""
    idx = {v: i for i, v in enumerate(ladder)}
    pure_under = [idx[v] for v, c in cells.items() if v in idx and c.under > 0 and c.strong == 0]
    tipped = [idx[v] for v, c in cells.items() if v in idx and c.strong > 0]
    U = max(pure_under) if pure_under else None
    S_ = min(tipped) if tipped else None
    res = {"U": ladder[U] if U is not None else None, "S": ladder[S_] if S_ is not None else None,
           "moves": [], "edge": None, "status": None}

    def ok(i):
        return 0 <= i < len(ladder) and allowed(ladder[i])

    def need(i):
        c = cells.get(ladder[i])
        return max(0, n_rep - (len(c.boots) if c else 0))

    def expect(i):
        c = cells.get(ladder[i])
        if not c or c.info == 0:
            return None
        if c.strong == 0 and c.under > 0:
            return "under"
        if c.under == 0 and c.strong > 0:
            return "lost"
        # mixed value: it stays 'tipped' whatever the repeat shows, so the edge logic can plan past it
        return "mixed" if i == S_ else None

    if U is None and S_ is None:
        if hint and (hint.get("U") is not None or hint.get("S") is not None):
            for key in ("S", "U"):
                v = hint.get(key)
                if v is not None and v in idx and ok(idx[v]):
                    res["moves"].append({"value": v, "reason": "first probe at the %s side of the %s bracket"
                                         % ("tipping" if key == "S" else "under", hint.get("source", "hint")),
                                         "expected": None, "group": "hint"})
            res["status"] = "probing hint (%s)" % hint.get("source", "hint")
            if not res["moves"]:
                res["status"] = "blocked: hint values not allowed"
        else:
            i = idx.get(start_value, 0)
            while i < len(ladder) and not ok(i):
                i += 1
            if i < len(ladder):
                res["moves"].append({"value": ladder[i], "reason": "first probe at the shallow start", "expected": None})
            res["status"] = "start"
        return res
    if S_ is None:
        step = col_step if hint else coarse
        target = None
        for i in range(min(U + step, len(ladder) - 1), U, -1):
            if ok(i):
                target = i
                break
        if target is None:
            res["status"] = "all under to the floor / never-rules"
            res["edge"] = {"U": ladder[U], "S": None, "kind": "under_to_floor"}
            return res
        res["status"] = "descending (all under so far)"
        res["moves"].append({"value": ladder[target], "reason": "all under so far: %d step(s) stronger" % (target - U),
                             "expected": None})
        return res
    if U is None:
        step = col_step if hint else coarse
        target = None
        for i in range(max(S_ - step, 0), S_):
            if ok(i):
                target = i
                break
        if target is None:
            res["status"] = "tipped at the top of the ladder"
            res["edge"] = {"U": None, "S": ladder[S_], "kind": "strong_at_top"}
            return res
        res["status"] = "ascending (everything tipped so far)"
        res["moves"].append({"value": ladder[target], "reason": "all tipped so far: %d step(s) weaker" % (S_ - target),
                             "expected": None})
        return res
    if U < S_:
        if S_ - U >= 2:
            mid = U + (S_ - U) // 2
            cand = [mid] + [i for d in range(1, S_ - U) for i in (mid - d, mid + d) if U < i < S_]
            for i in cand:
                if ok(i):
                    res["status"] = "bisect %s|%s" % (ladder[U], ladder[S_])
                    res["moves"].append({"value": ladder[i], "reason": "bisect between under %s and tipped %s"
                                         % (ladder[U], ladder[S_]), "expected": None})
                    return res
            res["status"] = "blocked: nothing allowed inside %s|%s" % (ladder[U], ladder[S_])
            return res
        band = [U, S_]
        kind = "edge"
    else:
        band = list(range(S_, U + 1))
        kind = "band"
    for i in band:
        n = need(i)
        if n and ok(i):
            e = expect(i)
            for _ in range(n):
                res["moves"].append({"value": ladder[i], "reason": "repeat at the %s (%d/%d independent boots)"
                                     % ("under side" if i == U else "tipping side" if i == S_ else "band",
                                        n_rep - n, n_rep), "expected": e, "group": "repeat"})
    if res["moves"]:
        res["status"] = "%s %s|%s: repeats needed" % ("adjacent" if kind == "edge" else "inverted band",
                                                      ladder[U], ladder[S_])
        return res
    cS = cells[ladder[S_]]
    res["status"] = "EDGE %s|%s" % (ladder[U], ladder[S_]) if kind == "edge" else "EDGE BAND %s..%s" % (ladder[S_], ladder[U])
    res["edge"] = {"U": ladder[U], "S": ladder[S_], "kind": kind,
                   "p_tip_at_S": round(cS.strong / cS.info, 3) if cS.info else None}
    return res


def _family_records(resolved, family, card):
    out = []
    for r in resolved:
        if str(r.get("card")) != str(card) or not r.get("step"):
            continue
        if r.get("state_class") not in family_states(family):
            continue
        out.append(r)
    return out


def plan(records, family, card_cfg, batch=None, now=None):
    family = load_family(family)
    if family.get("method") == "compass":
        from . import compass
        return compass.plan_compass(records, family, card_cfg, batch=batch, now=now)
    card = str(family.get("card") or card_cfg.get("card"))
    batch = int(batch or family.get("batch", 4))
    resolved = ledger.resolve(records, now=now)
    rules = Rules(card_cfg, resolved)
    policy = dict(ledger.DEFAULT_POLICY)
    policy.update(family.get("policy") or {})
    prior_policy = None
    if family.get("prior_policy"):
        prior_policy = dict(ledger.DEFAULT_POLICY)
        prior_policy.update(family["prior_policy"])
    n_rep = int(family.get("repeats", 2))
    knob = family["primary"]["knob"]
    cols = [Column(0, {}, family)] + [Column(i + 1, ov, family) for i, ov in enumerate(family.get("columns") or [])]
    fam_recs = _family_records(resolved, family, card)
    by_key = {}
    for r in fam_recs:
        by_key.setdefault(Column.record_key(r, knob), []).append(r)
    prefer_mv = sorted({r["step"]["v"] for r in resolved if r.get("step")}, key=lambda v: -sum(
        1 for x in resolved if x.get("step") and x["step"]["v"] == v)) + [family["primary"].get("start_mv", 450)]

    out = {"family": family.get("name"), "card": card, "state_class": family["state_class"], "mode": None,
           "knob": knob,
           "columns": [], "proposals": [], "rules": rules.describe(), "lockin": None, "warnings": [],
           "excluded": {}, "pending": []}

    # split each column's records into own (main policy) and prior (prior policy) views
    own, prior = {}, {}
    for col in cols:
        rs = by_key.get(col.key(), [])
        o, p = [], []
        for r in rs:
            eff, why = ledger.effective_outcome(r, policy)
            rr = dict(r, _eff=eff)
            if eff in ("excluded", "mistimed", "invalid", "unknown") and prior_policy:
                peff, _ = ledger.effective_outcome(r, prior_policy)
                if peff not in ledger.NO_INFO:
                    p.append(dict(r, _eff=peff))
                    continue
            o.append(rr)
            if eff in ("excluded", "mistimed", "invalid", "unknown", "no_dip"):
                k = "%s: %s" % (eff, why)
                out["excluded"][k] = out["excluded"].get(k, 0) + 1
            if eff == "pending":
                out["pending"].append(r["attempt_id"])
        own[col.idx], prior[col.idx] = o, p

    def allowed_fn(col):
        def f(value):
            return rules.allowed(col.step_for(value, prefer_mv))
        return f

    # ---- lock-in
    gold_recipes = {}
    for r in fam_recs:
        eff, _ = ledger.effective_outcome(r, policy)
        if eff == "goldilocks":
            gold_recipes.setdefault((Column.record_key(r, "code"), mv_to_code(r["step"]["v"])), r)
    if gold_recipes:
        best = None
        conf = float(family["lockin"].get("confidence", 0.95))
        target = float(family["lockin"].get("target", 0.9))
        for (gk, code), r0 in gold_recipes.items():
            same = [r for r in fam_recs if Column.record_key(r, "code") == gk and mv_to_code(r["step"]["v"]) == code]
            effs = [ledger.effective_outcome(r, policy)[0] for r in same]
            k = sum(1 for e in effs if e == "goldilocks")
            n = sum(1 for e in effs if e in OUTCOME_GROUP)
            lb = stats.wilson_lower(k, n, conf)
            cand = {"recipe": S.short(r0["step"]), "step": dict(r0["step"], reps=1), "k": k, "n": n,
                    "rate": round(k / n, 3) if n else None, "wilson_lower": round(lb, 4), "target": target,
                    "confidence": conf, "locked": lb >= target,
                    "runs_if_all_hit": stats.runs_needed(target, conf)}
            if best is None or (lb, k) > (best["wilson_lower"], best["k"]):
                best = cand
        out["lockin"] = best
        if best["locked"]:
            out["mode"] = "locked"
        else:
            out["mode"] = "lock-in"
            if rules.allowed(best["step"]):
                for _ in range(batch):
                    out["proposals"].append({"step": dict(best["step"]), "column": None, "label": "lock-in",
                                             "reason": "repeat the goldilocks recipe (%d/%d, Wilson lower %.3f < %.2f)"
                                             % (best["k"], best["n"], best["wilson_lower"], target), "expected": None})
            else:
                out["warnings"].append("goldilocks recipe now violates a never-rule: %s" % rules.check(best["step"]))
        _tables(out, cols, own, prior, knob, rules, family, allowed_fn)
        return _finish(out)

    # ---- densify around partials
    partials = []
    for col in cols:
        for r in own[col.idx]:
            if r["_eff"] in ledger.SIGNAL:
                partials.append((col, r))
    for r in fam_recs:
        if ledger.effective_outcome(r, policy)[0] in ledger.SIGNAL and not any(r is p[1] or r.get("attempt_id") == p[1].get("attempt_id") for p in partials):
            partials.append((None, r))
    if partials:
        out["mode"] = "densify"
        seen = set()
        dens = family.get("densify") or {}
        for col, r in partials:
            st = dict(r["step"], reps=1)
            code = mv_to_code(st["v"])
            same = [x for x in fam_recs if Column.record_key(x, "code") == Column.record_key(r, "code")
                    and mv_to_code(x["step"]["v"]) == code]
            st = S.normalize(st)
            info = len({sample_id(x) for x in same if ledger.effective_outcome(x, policy)[0] in OUTCOME_GROUP})
            cands = [(st, "repeat the partial (%d/%d boots)" % (info, n_rep))] * max(0, n_rep - info)
            for dc, why in ((1, "one code weaker than the partial"), (-1, "one code stronger than the partial")):
                cands.append((dict(st, v=canonical_mv(code + dc, prefer_mv)), why))
            for k, d in dens.items():
                for sgn in (-1, 1):
                    cands.append((dict(st, **{k: st[k] + sgn * d}), "%s %+d around the partial" % (k, sgn * d)))
            for s2, why in cands:
                key = tuple(S.to_list(s2))
                if "repeat" not in why and (key in seen or any(tuple(S.to_list(dict(x["step"], reps=1))) == key for x in fam_recs)):
                    continue
                seen.add(key)
                if rules.allowed(s2):
                    out["proposals"].append({"step": s2, "column": col.idx if col else None,
                                             "label": "densify", "reason": why, "expected": None})
                if len(out["proposals"]) >= batch:
                    break
            if len(out["proposals"]) >= batch:
                break
        _tables(out, cols, own, prior, knob, rules, family, allowed_fn)
        return _finish(out)

    # ---- edge search with lookahead
    out["mode"] = "edge-search"
    hypo = {c.idx: [] for c in cols}
    blocked = set()
    pending_cols = {c.idx for c in cols if any(r["_eff"] == "pending" for r in own[c.idx])}
    blocked |= pending_cols
    proposed = []
    guard = 0
    while len(proposed) < batch and guard < 64:
        guard += 1
        moves = _next_moves(cols, own, prior, hypo, family, rules, allowed_fn, n_rep, blocked)
        if not moves:
            break
        col, group = moves
        for mv in group:
            if len(proposed) >= batch:
                break
            st = col.step_for(mv["value"], prefer_mv)
            proposed.append({"step": st, "column": col.idx, "label": col.label, "reason": mv["reason"],
                             "expected": mv["expected"]})
            if mv["expected"] is not None:
                hypo[col.idx].append(_hypo_record(st, mv["expected"], len(proposed), card, family))
        if any(mv["expected"] is None for mv in group):
            blocked.add(col.idx)
    out["proposals"] = proposed
    if pending_cols:
        out["warnings"].append("columns with a pending shot are not planned until it completes: %s"
                               % sorted(pending_cols))
    _tables(out, cols, own, prior, knob, rules, family, allowed_fn)
    if not proposed and all(c["edge"] for c in out["columns"] if c["status"] and not c["status"].startswith("blocked")):
        out["mode"] = "exhausted"
    return _finish(out)


def _hypo_record(st, expected, n, card, family):
    return {"attempt_id": "hypothetical-%d" % n, "boot_id": "hypothetical-%d" % n, "card": card, "step": dict(st),
            "resolved": {"pw": resolved_pw(st)}, "state_class": family["state_class"],
            "_eff": "lost" if expected == "lost" else "under", "outcome": expected}


def _column_hint(col, cols, own, prior, hypo, family, rules, allowed_fn, n_rep, base_res):
    """Hint for a column without own data: its prior-firmware bracket, else the base column's edge."""
    if prior.get(col.idx):
        pc = _cells(col, prior[col.idx], None)
        lad = _ladder(family, col, rules, list(pc))
        pr = analyze_column(col, pc, lad, 1, allowed_fn(col), None)
        if pr["U"] is not None or pr["S"] is not None:
            return {"U": pr["U"], "S": pr["S"], "source": "prior firmware"}
    if col.idx != 0 and base_res and base_res.get("edge"):
        e = base_res["edge"]
        return {"U": e.get("U"), "S": e.get("S"), "source": "base edge"}
    return None


def _analyze(col, cols, own, prior, hypo, family, rules, allowed_fn, n_rep, base_res):
    cells = _cells(col, own[col.idx], None, hypo.get(col.idx, []))
    has_info = any(c.info for c in cells.values())
    hint = None if has_info else _column_hint(col, cols, own, prior, hypo, family, rules, allowed_fn, n_rep, base_res)
    if has_info and col.idx != 0:
        hint = {"source": "column"}  # use the fine column step once a column has its own data
    if has_info and col.idx == 0 and prior.get(0):
        hint = {"source": "prior firmware"}
    lad = _ladder(family, col, rules, list(cells))
    p = family["primary"]
    start = mv_to_code(p.get("start_mv", 450)) if col.knob == "code" else p.get("start")
    res = analyze_column(col, cells, lad, n_rep, allowed_fn(col), hint, p.get("coarse_step", 4),
                         p.get("column_step", 1), start_value=start)
    res["cells"], res["ladder"], res["hint"] = cells, lad, hint
    return res


def _next_moves(cols, own, prior, hypo, family, rules, allowed_fn, n_rep, blocked):
    base_res = _analyze(cols[0], cols, own, prior, hypo, family, rules, allowed_fn, n_rep, None)
    if 0 not in blocked and base_res["moves"]:
        return cols[0], _group(base_res["moves"])
    early = family.get("expand_before_base_edge", True)
    for col in cols[1:]:
        if col.idx in blocked:
            continue
        if not base_res.get("edge"):
            # before the base edge, only columns that stand on their own data or their own prior bracket
            started = any(r.get("_eff") in OUTCOME_GROUP for r in own[col.idx] + hypo.get(col.idx, []))
            if not (early and (started or prior.get(col.idx))):
                continue
        res = _analyze(col, cols, own, prior, hypo, family, rules, allowed_fn, n_rep, base_res)
        if res["moves"]:
            return col, _group(res["moves"])
    return None


def _group(moves):
    """Moves that share a group are independent of each other and go out together; otherwise one at a time."""
    g = moves[0].get("group")
    return [m for m in moves if g is not None and m.get("group") == g] or moves[:1]


def _tables(out, cols, own, prior, knob, rules, family, allowed_fn):
    base_res = None
    for col in cols:
        res = _analyze(col, cols, own, prior, {}, family, rules, allowed_fn, int(family.get("repeats", 2)), base_res)
        if col.idx == 0:
            base_res = res
        geo_viol = rules.check(col.step_for(res["ladder"][0] if res["ladder"] else 72))
        geo_viol = [w for w in geo_viol if "FLOOR" not in w and "latch" not in w]
        rows = []
        for v in sorted(res["cells"], reverse=(knob == "code")):
            c = res["cells"][v]
            mv = sorted(set(c.mv))
            rows.append({"value": v, "mv": mv, "tag": c.tag()})
        prow = []
        if prior.get(col.idx):
            pc = _cells(col, prior[col.idx], None)
            for v in sorted(pc, reverse=(knob == "code")):
                prow.append({"value": v, "mv": sorted(set(pc[v].mv)), "tag": pc[v].tag()})
        status = res["status"]
        if geo_viol:
            status = "blocked: " + "; ".join(geo_viol)
        elif col.idx != 0 and not (base_res and base_res.get("edge")) and not any(c.info for c in res["cells"].values()) \
                and not (family.get("expand_before_base_edge", True) and prior.get(col.idx)):
            status = "waiting for the base edge"
        out["columns"].append({"idx": col.idx, "label": col.label, "status": status, "edge": res.get("edge"),
                               "rows": rows, "prior_rows": prow, "hint": res.get("hint")})


def _finish(out):
    for p in out["proposals"]:
        p["step_list"] = S.to_list(p["step"], reps=1)
        p["code"] = mv_to_code(p["step"]["v"])
        p["short"] = S.short(p["step"])
    return out
