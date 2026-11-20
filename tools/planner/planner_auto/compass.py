"""CRC-compass search: a community member's method, run as first shots on this firmware (6.25 mV VID steps, push2's no-write-across-the-release rule, card latch-energy rules).

Phases, each decided from the ledger alone (family "method": "compass"):
  0 lock-in      any goldilocks: the planner's lock-in (repeat the recipe, Wilson lower bound against the target).
  1 densify      any crc_intermediate (CRC 0x75 / 0x57 / between 0x55 and 0x77) or other partial read: repeat it on
                 N independent boots, then delay +-2 / +-4, pw +-5 / +-10 and one VID code either side around it.
  2 loss island  base geometry: VID-code bisection with N repeats at the edge (the planner's edge search). The anchor
                 is the shallow side of that edge: one VID code (6.25 mV) above the loss, the community's "1 mV to the
                 shallow side of the lost island" at our resolution.
  3 delay sweep  at the anchor: delay lo..hi (default 120..460) every 'coarse' us plus the base delay, one shot each,
                 in spread order so a partial sweep still covers the range. Delays that put a PMBus write across the
                 release are skipped and listed (push2 refuses them).
  4 band edges   neighbouring swept delays whose outcome flips between weak (under, crc 0x77) and strong (lost,
                 poison, over, latch): bisect the delay between them down to 'fine' us.
  5 pw tune      at the weak side of each converged delay edge: pw up from the base in 'pw.step' us, then 'pw.fine' us,
                 watching CRC (each ~10 us opened 1-2 CRC bits on the community's card C). Latch rules cap pw.
  6 widen        a complete sweep with no flip: sweep again one code deeper (all weak) or shallower (all strong), up
                 to 'codes_extra' codes.
  7 drift loop   nothing left to refine: repeat a small set (signals first, then the weak sides of the converged
                 edges), least recently shot first; the hit drifts with temperature.

Note on delay: pwmvid.c uses td and delay only as td - delay (pulse k's dip command at td - delay + k * (pw + gap)), so
a delay sweep at fixed td is a sweep of the first dip time; the ledger pools equivalent (td, delay) pairs.
"""
from . import ledger, steps as S
from .rules import Rules
from .vid import canonical_mv, mv_to_code

COMPASS_DEFAULTS = {
    "delay": {"lo": 120, "hi": 460, "coarse": 30, "fine": 2},
    "pw": {"lo": 50, "hi": 240, "step": 10, "fine": 2},
    "codes_extra": 2,
    "sweep_repeats": 1,
    "densify": {"delay": [2, 4], "pw": [5, 10], "code": [1, -1]},
    "drift": {"set": 5, "offsets": [-4, -2, 0, 2, 4], "shots": 1, "round_interval_s": 180,
              "pw_offsets": [], "code_offsets": []},
}
# outcomes that show the fuse read was reached: CRC signals and the sporadic fuse-touch reads (BAD0 / BADF / over)
SIGNAL_OUT = ("crc_intermediate", "partial", "poisoned", "hidden_poison", "over")
SIG_RANK = {"crc_intermediate": 0, "partial": 1, "poisoned": 2, "hidden_poison": 3, "over": 4}
FIXED = ("vr", "fast", "pulses", "gap", "hold", "t_restore", "revert")


def spread_order(vals):
    """lo, hi, middle, quarters, ...: a cut-short sweep still covers the whole range."""
    vals = sorted(vals)
    if len(vals) <= 2:
        return vals
    out, seen = [vals[0], vals[-1]], {0, len(vals) - 1}
    segs = [(0, len(vals) - 1)]
    while segs:
        nxt = []
        for a, b in segs:
            if b - a < 2:
                continue
            m = (a + b) // 2
            if m not in seen:
                seen.add(m)
                out.append(vals[m])
            nxt += [(a, m), (m, b)]
        segs = nxt
    out += [v for i, v in enumerate(vals) if i not in seen]
    return out


def _samples(c):
    return len(c.boots) if c is not None else 0


def _cls(c):
    if c is None or c.info == 0:
        return None
    if c.gold:
        return "G"
    if c.partial:
        return "I"
    if c.strong and c.under:
        return "M"
    return "S" if c.strong else "W"


def plan_compass(records, family, card_cfg, batch=None, now=None):
    from . import planner as P
    fam = P.load_family(family)
    cfg = {k: (dict(v) if isinstance(v, dict) else v) for k, v in COMPASS_DEFAULTS.items()}
    for k, v in (fam.get("compass") or {}).items():
        if isinstance(v, dict) and isinstance(cfg.get(k), dict):
            cfg[k].update(v)
        else:
            cfg[k] = v
    card = str(fam.get("card") or card_cfg.get("card"))
    batch = int(batch or fam.get("batch", 4))
    n_rep = int(fam.get("repeats", 2))
    resolved = ledger.resolve(records, now=now)
    rules = Rules(card_cfg, resolved)
    policy = dict(ledger.DEFAULT_POLICY)
    policy.update(fam.get("policy") or {})
    base = S.normalize(dict(fam["base"]))
    prefer_mv = sorted({r["step"]["v"] for r in resolved if r.get("step")})

    def rpw(st):
        return P.resolved_pw(st)

    def pkey(st, pw=None):
        st = S.normalize(st)
        return P.geo_key(st, pw if pw is not None else rpw(st), None)

    cells, pending, excluded = {}, set(), {}
    states = P.family_states(fam)
    sig_recs = []
    for idx, r in enumerate(resolved):
        if str(r.get("card")) != card or not r.get("step") or r.get("state_class") not in states:
            continue
        eff, why = ledger.effective_outcome(r, policy)
        k = pkey(r["step"], (r.get("resolved") or {}).get("pw", r["step"]["pw"]))
        c = cells.setdefault(k, P.Cell())
        c.recs.append(r)
        c.mv.append(r["step"]["v"])
        grp = P.OUTCOME_GROUP.get(eff)
        if eff == "pending":
            c.pending += 1
            pending.add(k)
        elif grp is None:
            c.noinfo += 1
            if eff in ("excluded", "mistimed", "invalid", "unknown", "no_dip"):
                excluded["%s: %s" % (eff, why)] = excluded.get("%s: %s" % (eff, why), 0) + 1
        else:
            setattr(c, grp, getattr(c, grp) + 1)
            c.boots.add(P.sample_id(r))
            if eff in SIGNAL_OUT and all(S.normalize(r["step"]).get(f) == base.get(f) for f in FIXED):
                sig_recs.append((k, r, eff))
    last_seen = {}
    for idx, r in enumerate(resolved):
        if r.get("step") and str(r.get("card")) == card:
            last_seen[pkey(r["step"], (r.get("resolved") or {}).get("pw", r["step"]["pw"]))] = idx

    out = {"family": fam.get("name"), "card": card, "state_class": fam["state_class"], "knob": "code",
           "method": "compass", "mode": None, "phase": None, "columns": [], "proposals": [],
           "rules": rules.describe(), "lockin": None, "warnings": [], "excluded": excluded,
           "pending": sorted({r["attempt_id"] for k in pending for r in cells[k].recs if r.get("outcome") == "pending"}),
           "report": []}
    taken = set()

    def point(code, **ov):
        st = dict(base, **ov)
        st["v"] = canonical_mv(code, prefer_mv)
        st["reps"] = 1
        return st

    def cell(st):
        return cells.get(pkey(st))

    def propose(st, why, phase, need_info=1):
        k = pkey(st)
        if len(out["proposals"]) >= batch or k in pending or not rules.allowed(st):
            return False
        c = cells.get(k)
        have = len(c.boots) if c else 0
        already = sum(1 for p in out["proposals"] if pkey(p["step"]) == k)
        if have + already >= need_info:
            return False
        out["proposals"].append({"step": st, "column": None, "label": phase, "reason": why, "expected": None})
        taken.add(k)
        return True

    # 0 lock-in
    if any(c.gold for c in cells.values()):
        res = P.plan(records, dict(fam, method="edge", columns=[]), card_cfg, batch=batch, now=now)
        res.update(method="compass", phase="lock-in", report=["goldilocks in the ledger: lock-in"])
        return res

    # 1 drift tracking around the best signal: CRC intermediate / partial, or a sporadic fuse touch (poison, over)
    if sig_recs:
        return _drift_track(out, sig_recs, cells, fam, cfg, base, rules, point, pkey, pending, P, now)

    # 2 loss island: the base column edge (VID code bisection with repeats)
    bres = P.plan(records, dict(fam, method="edge", columns=[]), card_cfg, batch=batch, now=now)
    col0 = bres["columns"][0] if bres["columns"] else {}
    out["columns"] = bres["columns"][:1]
    edge = col0.get("edge")
    dc = cfg["delay"]
    grid_all = sorted(set(range(dc["lo"], dc["hi"] + 1, dc["coarse"])) | {dc["hi"], base["delay"]})
    if not edge:
        out.update(phase="loss island", mode="loss island", lockin=bres.get("lockin"))
        out["warnings"].extend(bres.get("warnings") or [])
        out["report"].append("phase 2: find the loss edge on the base geometry (%s)" % S.short(dict(base, v=450)))
        for p in bres["proposals"]:
            out["proposals"].append(dict(p, label="loss island"))
        hint = dict(col0.get("hint") or {})
        own_under = [code for code in range(mv_to_code(450), mv_to_code(max(rules.floor_mv, 250)) - 1, -1)
                     if _cls(cell(point(code))) == "W"]
        if own_under:   # the column's own deepest pure-under code beats a prior-firmware hint
            hint = {"U": min(own_under), "source": "current"}
        if hint.get("U") is not None and len(out["proposals"]) < batch:
            a = hint["U"]
            grid = [d for d in grid_all if rules.allowed(point(a, delay=d))]
            out["report"].append("speculative delay sweep at code %d (%d mV), the under side of the %s bracket"
                                 % (a, canonical_mv(a, prefer_mv), hint.get("source", "hint")))
            need = int(cfg["sweep_repeats"])
            for d in spread_order(grid):
                if d != base["delay"] and _samples(cell(point(a, delay=d))) < need:
                    while propose(point(a, delay=d), "delay sweep, speculative anchor (loss island not confirmed "
                                  "yet), %d per delay" % need, "delay sweep", need_info=need):
                        pass
        return P._finish(out)
    if edge["kind"] == "band":
        anchor = max(edge["U"], edge["S"]) + 1
    elif edge.get("U") is not None:
        anchor = edge["U"]
    else:
        anchor = edge["S"] + 1
    out["report"].append("loss island edge %s (%s): anchor code %d (%d mV, shallow side)" % (
        "%s|%s" % (edge.get("U"), edge.get("S")), edge["kind"], anchor, canonical_mv(anchor, prefer_mv)))

    anchors = [anchor]
    direction = 0
    converged_weak = []   # weak-side points of converged edges (drift set)
    for step_i in range(2 * cfg["codes_extra"] + 1):
        a = anchors[-1]
        grid = [d for d in grid_all if rules.allowed(point(a, delay=d))]
        skipped = [d for d in grid_all if d not in grid]
        cls = {d: _cls(cell(point(a, delay=d))) for d in grid}
        out["report"].append("delay sweep at code %d (%d mV): %s" % (
            a, canonical_mv(a, prefer_mv), " ".join("%d:%s" % (d, cls[d] or ".") for d in grid)))
        if skipped:
            out["report"].append("  skipped (write across the release or another never-rule): %s" %
                                 ",".join(str(d) for d in skipped))
        need = int(cfg["sweep_repeats"])
        missing = [d for d in spread_order(grid) if _samples(cell(point(a, delay=d))) < need]
        for d in missing:
            while propose(point(a, delay=d), "delay sweep at the anchor (coarse %d us, %d per delay)"
                          % (dc["coarse"], need), "delay sweep", need_info=need):
                pass
        flips = []
        for d1, d2 in zip(grid, grid[1:]):
            c1, c2 = cls[d1], cls[d2]
            if c1 and c2 and ({c1, c2} in ({"W", "S"}, {"W", "M"})):
                flips.append((d1, d2) if c1 == "W" else (d2, d1))
        for dw, ds in flips:
            res = _fine_edge(a, dw, ds, dc["fine"], point, cell, rules)
            if res[0] == "probe":
                propose(point(a, delay=res[1]), "band edge %d|%d: bisect delay to %d us" % (dw, ds, dc["fine"]),
                        "band edge")
                continue
            fw, fs = res[1], res[2]
            out["report"].append("  fine delay edge at code %d: weak %d | strong %d" % (a, fw, fs))
            converged_weak.append(pkey(point(a, delay=fw)))
            pres = _pw_tune(a, fw, base, cfg["pw"], point, cell, rules)
            if pres[0] == "probe":
                propose(point(a, delay=fw, pw=pres[1]), "pw tune at delay %d (%s)" % (fw, pres[2]), "pw tune")
            else:
                out["report"].append("  pw at delay %d: %s" % (fw, pres[1]))
                if pres[0] == "edge":
                    converged_weak.append(pkey(point(a, delay=fw, pw=pres[2])))
        if missing or flips:
            break
        allc = {c for c in cls.values() if c}
        if allc <= {"W"} and direction <= 0 and len([x for x in anchors if x < anchor]) < cfg["codes_extra"]:
            direction = -1
            nxt = a - 1
        elif allc <= {"S", "M"} and allc and direction >= 0 and len([x for x in anchors if x > anchor]) < cfg["codes_extra"]:
            direction = 1
            nxt = a + 1
        else:
            break
        if not rules.allowed(point(nxt, delay=base["delay"])) and not any(rules.allowed(point(nxt, delay=d)) for d in grid_all):
            out["report"].append("  code %d is outside the never-rules: stop widening" % nxt)
            break
        anchors.append(nxt)
        out["report"].append("  no flip at code %d: widen to code %d" % (a, nxt))
    if out["proposals"]:
        phases = sorted({p["label"] for p in out["proposals"]})
        out["phase"] = out["mode"] = "+".join(phases)
        return P._finish(out)
    out["report"].append("sweeps and edges converged with no CRC signal: drift loop")
    return _drift(out, converged_weak, cells, last_seen, cfg, batch, rules, P)


def _fine_edge(code, dw, ds, fine, point, cell, rules):
    """Bisect delay between a weak delay dw and a strong delay ds at 'fine' resolution."""
    step = fine if ds > dw else -fine
    ladder = [d for d in range(dw, ds + step, step) if rules.allowed(point(code, delay=d))]
    if dw not in ladder:
        ladder.insert(0, dw)
    if ds not in ladder:
        ladder.append(ds)
    cls = [_cls(cell(point(code, delay=d))) for d in ladder]
    weak = [i for i, c in enumerate(cls) if c == "W"]
    strong = [i for i, c in enumerate(cls) if c in ("S", "M")]
    u = max(weak) if weak else 0
    s = min([i for i in strong if i > u] or [len(ladder) - 1])
    if s - u >= 2:
        return ("probe", ladder[u + (s - u) // 2])
    return ("edge", ladder[u], ladder[s])


def _pw_tune(code, delay, base, pcfg, point, cell, rules):
    """pw up from the base at a converged weak delay; then fine steps between the last weak and first strong pw."""
    ladder = [p for p in range(base["pw"], pcfg["hi"] + 1, pcfg["step"]) if rules.allowed(point(code, delay=delay, pw=p))]
    if not ladder:
        return ("capped", "no pw above the base passes the never-rules")
    cls = {p: _cls(cell(point(code, delay=delay, pw=p))) for p in ladder}
    weak = [p for p in ladder if cls[p] == "W"]
    strong = [p for p in ladder if cls[p] in ("S", "M")]
    u = max(weak) if weak else None
    s = min([p for p in strong if u is None or p > u] or [None]) if strong else None
    if u is None:
        return ("probe", ladder[0], "base pw first")
    if s is None:
        nxt = [p for p in ladder if p > u]
        if not nxt:
            return ("capped", "weak up to pw %d (never-rules cap)" % u)
        return ("probe", nxt[0], "weak so far, %d us wider" % (nxt[0] - u))
    fine = [p for p in range(u, s + 1, pcfg["fine"]) if rules.allowed(point(code, delay=delay, pw=p))]
    fcls = {p: _cls(cell(point(code, delay=delay, pw=p))) for p in fine}
    fu = max([p for p in fine if fcls[p] == "W"] or [u])
    fs = min([p for p in fine if fcls[p] in ("S", "M") and p > fu] or [s])
    inner = [p for p in fine if fu < p < fs]
    if inner:
        return ("probe", inner[len(inner) // 2], "bisect pw %d|%d at %d us" % (fu, fs, pcfg["fine"]))
    return ("edge", "pw edge weak %d | strong %d" % (fu, fs), fu)


def _drift(out, keys, cells, last_seen, cfg, batch, rules, P):
    """Repeat a small set of points, least recently shot first (the hit drifts with temperature)."""
    out["phase"] = out["mode"] = "drift loop"
    keys = list(dict.fromkeys(keys))[: cfg["drift"]["set"]]
    pts = []
    for k in keys:
        c = cells.get(k)
        if not c or not c.recs:
            continue
        pts.append((last_seen.get(k, -1), dict(S.normalize(c.recs[0]["step"]), reps=1)))
    pts.sort(key=lambda x: x[0])
    for _, st in pts:
        if len(out["proposals"]) >= batch:
            break
        if rules.allowed(st):
            out["proposals"].append({"step": st, "column": None, "label": "drift loop",
                                     "reason": "drift tracking: repeat the set, least recently shot first",
                                     "expected": None})
    if not out["proposals"]:
        out["mode"] = "exhausted"
        out["report"].append("nothing left to propose in this family")
    return P._finish(out)


def _ts(r):
    import datetime as _dt
    for k in ("ingested_at", "launch_time"):
        v = r.get(k)
        if v:
            try:
                t = _dt.datetime.fromisoformat(v)
                return (t if t.tzinfo else t.astimezone()).timestamp()
            except ValueError:
                pass
    return 0.0


def _drift_track(out, sig_recs, cells, fam, cfg, base, rules, point, pkey, pending, P, now=None):
    """Community drift loop: a small set of delays around the best signal (default signal delay -4..+4 in 2 us),
    N shots each per round, one round every round_interval_s (the hit drifts with temperature). The set is
    re-centred on the best signal every round, so a stronger signal (CRC 0x75 over a sporadic BAD0) takes over."""
    import datetime as _dt
    dc = cfg["drift"]
    by_point = {}
    for k, r, eff in sig_recs:
        by_point.setdefault(k, []).append((r, eff))
    base_pw = base["pw"]

    def rank(item):
        k, lst = item
        c = cells[k]
        best = min(SIG_RANK[e] for _, e in lst)
        crc_bits = min([bin(r["crc"] ^ 0x55).count("1") for r, e in lst if r.get("crc") is not None
                        and e in ("crc_intermediate", "partial")] or [8])
        rate = len(lst) / max(1, c.info)
        st = lst[0][0]["step"]
        return (best, crc_bits, -rate, abs(st["pw"] - base_pw), -mv_to_code(st["v"]), -max(_ts(r) for r, _ in lst))
    ranked = sorted(by_point.items(), key=rank)
    k0, lst0 = ranked[0]
    r0 = max((r for r, _ in lst0), key=_ts)
    st0 = dict(S.normalize(r0["step"]), reps=1)
    out["phase"] = out["mode"] = "drift loop"
    for k, lst in ranked[:5]:
        c = cells[k]
        r = lst[0][0]
        out["report"].append("signal %s: %s (%d of %d informative)%s" % (
            S.short(r["step"]), ", ".join(sorted({e for _, e in lst})), len(lst), c.info,
            ("  CRC 0x%02x" % r["crc"]) if r.get("crc") is not None and lst[0][1] != "poisoned" else ""))
    pts = []
    for o in dc.get("offsets", [0]):
        pts.append(dict(st0, delay=st0["delay"] + o))
    for o in dc.get("pw_offsets", []):
        pts.append(dict(st0, pw=st0["pw"] + o))
    for o in dc.get("code_offsets", []):
        pts.append(dict(st0, v=canonical_mv(mv_to_code(st0["v"]) + o)))
    allowed = [p for p in pts if rules.allowed(p) and pkey(p) not in pending][: int(dc.get("set", 5))]
    skipped = [p for p in pts if not rules.allowed(p)]
    if skipped:
        out["report"].append("drift set: skipped (never-rules): %s" % ", ".join(
            "delay %d pw %d" % (p["delay"], p["pw"]) for p in skipped))
    last = 0.0
    rounds = []
    for p in allowed:
        c = cells.get(pkey(p))
        if c is not None:
            last = max([last] + [_ts(r) for r in c.recs])
            rounds.append(c.info)
        else:
            rounds.append(0)
    done_rounds = min(rounds) // max(1, int(dc.get("shots", 1))) if rounds else 0
    nb = last + float(dc.get("round_interval_s", 180)) if last else 0.0
    now_ts = (now or _dt.datetime.now().astimezone()).timestamp()
    if nb > now_ts:
        out["not_before"] = _dt.datetime.fromtimestamp(nb).astimezone().isoformat(timespec="seconds")
    out["report"].append("drift round %d around %s: delays %s, %d shot(s) each, every %ds%s" % (
        done_rounds + 1, S.short(st0), ",".join(str(p["delay"]) for p in allowed), int(dc.get("shots", 1)),
        int(dc.get("round_interval_s", 180)), (", not before " + out["not_before"]) if out.get("not_before") else ""))
    for p in allowed:
        for _ in range(int(dc.get("shots", 1))):
            out["proposals"].append({"step": dict(p), "column": None, "label": "drift loop",
                                     "reason": "drift round %d: delay %+d around the signal" % (
                                         done_rounds + 1, p["delay"] - st0["delay"]), "expected": None})
    if not out["proposals"]:
        out["mode"] = "exhausted"
        out["report"].append("no legal point in the drift set")
    return P._finish(out)
