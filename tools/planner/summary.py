#!/usr/bin/env python3
"""summary.py: per-family outcome maps from the ledger (VID code x delay, one table per pw).

  python3 summary.py --card e                       # every family listed in cards/e.json
  python3 summary.py --card e --family families/e-slow-push2.json
  python3 summary.py --card e --geometries          # also: every push2 geometry, whatever family

A family's map holds the records its planner would count: the family's state classes (incl. equivalents), its
policy (firmware, release window, overlap), and its fixed geometry (pulses, vr, fast, gap, hold, t_restore, revert).
Delay is shown as the equivalent delay at the family's td (td and delay only enter the firmware as td - delay).
Cell letters: u under, c crc 0x77, i crc intermediate, p partial, G goldilocks, o over (crc 0), P poisoned,
h hidden poison, L lost, X controller off, n no information (mistimed / invalid / no dip / unknown / pending).
"""
import argparse
import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
from planner_auto import launch, ledger, planner, steps as S  # noqa: E402
from planner_auto.vid import mv_to_code  # noqa: E402

LETTER = {"under": "u", "crc_closed": "c", "crc_intermediate": "i", "partial": "p", "goldilocks": "G", "over": "o",
          "poisoned": "P", "hidden_poison": "h", "lost": "L", "controller_off": "X"}
ORDER = "GiporhPLXcun"
FIXED = ("pulses", "vr", "fast", "gap", "hold", "t_restore", "revert")


def cell_text(counts):
    return " ".join("%d%s" % (counts[k], k) for k in sorted(counts, key=lambda x: ORDER.index(x))) or "."


def family_map(records, fam, card):
    states = planner.family_states(fam)
    pol = dict(ledger.DEFAULT_POLICY)
    pol.update(fam.get("policy") or {})
    base = S.normalize(fam["base"])
    maps, n = {}, 0
    for r in records:
        if str(r.get("card")) != str(card) or not r.get("step") or r.get("state_class") not in states:
            continue
        st = S.normalize(r["step"])
        pw = (r.get("resolved") or {}).get("pw", st["pw"])
        if any((pw if f == "pw" else st.get(f)) != base.get(f) for f in FIXED):
            continue
        eff, _ = ledger.effective_outcome(r, pol)
        if eff == "excluded":
            continue
        d = int(base["td"]) - S.t0(st)
        code = mv_to_code(st["v"])
        cell = maps.setdefault(pw, {}).setdefault(code, {}).setdefault(d, {})
        k = LETTER.get(eff, "n")
        cell[k] = cell.get(k, 0) + 1
        n += 1
    return maps, n


def render_map(maps, mv_for):
    L = []
    for pw in sorted(maps):
        rows = maps[pw]
        delays = sorted({d for code in rows for d in rows[code]})
        L.append("  pw %d us:" % pw)
        w = max([7] + [len(cell_text(rows[c].get(d, {}))) for c in rows for d in delays])
        L.append("    %-11s|%s" % ("code (mV)", "|".join(("d%d" % d).center(w) for d in delays)))
        for code in sorted(rows, reverse=True):
            L.append("    %3d (%3d)  |%s" % (code, mv_for.get(code, round(code * 6.25)),
                                            "|".join(cell_text(rows[code].get(d, {})).center(w) for d in delays)))
    return L


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--card", required=True, help="card id (cards/<id>.json) or a card config file")
    ap.add_argument("--family", action="append", default=[])
    ap.add_argument("--ledger")
    ap.add_argument("--geometries", action="store_true", help="also map every push2 geometry")
    a = ap.parse_args(argv)
    cfg = launch.load_card(a.card)
    card = str(cfg["card"])
    path = launch.ledger_path(cfg, card, a.ledger)
    recs = ledger.resolve(ledger.read(path))
    mv_for = {}
    for r in recs:
        if r.get("step"):
            mv_for.setdefault(mv_to_code(r["step"]["v"]), r["step"]["v"])
    fams = a.family or [os.path.join(HERE, f) for f in cfg.get("families", [cfg["default_family"]])]
    print("card %s: %d resolved records in %s" % (card, len(recs), path))
    for fp in fams:
        fam = planner.load_family(fp)
        maps, n = family_map(recs, fam, card)
        b = S.normalize(fam["base"])
        print("\n== %s: %d shot(s); %s, td %d, %d pulses, vr %d, fast %d, gap %d; states %s; fw %s, release %s" % (
            fam.get("name"), n, fam.get("method", "edge"), b["td"], b["pulses"], b["vr"], b["fast"], b["gap"],
            "+".join(sorted(planner.family_states(fam))), ",".join((fam.get("policy") or {}).get("firmware") or ["any"]),
            (fam.get("policy") or {}).get("release_window_us")))
        for line in render_map(maps, mv_for):
            print(line)
    if a.geometries:
        print("\n== every push2 geometry (first shots and post-park; release 0..4)")
        geo = {}
        for r in recs:
            if "push2" not in str(r.get("firmware")) or r.get("state_class") not in ("first-shot-fresh-boot", "post-park"):
                continue
            st = S.normalize(r["step"])
            geo.setdefault(tuple((f, st[f]) for f in FIXED + ("td",)), []).append(r)
        for key, rs in sorted(geo.items(), key=lambda kv: -len(kv[1])):
            fam = {"state_class": "first-shot-fresh-boot", "equivalent_states": ["post-park"],
                   "base": dict(key, v=400, pw=80), "policy": {"firmware": None, "release_window_us": [0, 4]}}
            maps, n = family_map(rs, fam, card)
            print("\n-- %s: %d shot(s)" % (", ".join("%s %s" % kv for kv in key), n))
            for line in render_map(maps, mv_for):
                print(line)
    return 0


if __name__ == "__main__":
    sys.exit(main())
