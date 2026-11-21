"""Never-rules: every proposal is checked against all of them; a single violation removes it.

1. FLOOR: commanded mV below the card's floor (the runner refuses it too).
2. Runner limits: v_restore 575..825, fast 0..7, hold 0..20, revert 0 or 2, t_restore -20 or -20000..-1000,
   pulses 1..16 (bisect_shots.py asserts and firmware clamps).
3. pw below pw_min_us (default 47 us, the measured PMBus write time): the restore write would start while the dip
   write is still on the bus, and below 36 us the firmware silently clamps pw to 36.
4. Release overlap: a PMBus write in flight at the PEX_RST release (t_cmd <= 0 < t_cmd + 47), computed on the
   firmware's own timeline (dip at td - 117, restore at td - 117 + pw, pulse k at k * (pw + 150)). push2 refuses
   these shots; with {"allow_overlap": true} (push3 firmware only, docs/PUSH3.md) this rule is off.
5. Overwritten: a VOUT step before the 600 mV setup write at reset_at never reaches the rail.
6. Latch energy, learned from controller_off outcomes: a latch at code c with energy E = pulses x pw forbids every
   shot with code <= c + depth_margin_codes and energy >= E x (1 - energy_margin).
7. The card's explicit never list: {"match": {field: value or [lo, hi]}, "note": ...}; fields are step fields plus
   'code' (VID code of v) and 'energy' (pulses x resolved pw).
"""
from . import schedule, steps as S
from .vid import mv_to_code

RUNNER_LIMITS = {"vr": (575, 825), "fast": (0, 7), "hold": (0, 20), "pulses": (1, 16), "delay": (0, 900),
                 "gap": (36, 2000)}


class Rules:
    def __init__(self, card_cfg, records=()):
        self.cfg = card_cfg or {}
        self.floor_mv = int(self.cfg.get("floor_mv", 0))
        self.pw_min = int(self.cfg.get("pw_min_us", schedule.PMBUS_W_MEAS_US))
        lat = self.cfg.get("latch") or {}
        self.depth_margin = int(lat.get("depth_margin_codes", 1))
        self.energy_margin = float(lat.get("energy_margin", 0.10))
        self.never = list(self.cfg.get("never") or [])
        self.allow_overlap = bool(self.cfg.get("allow_overlap"))
        self.latches = self.learn_latches(records)

    def learn_latches(self, records):
        out = []
        seen = set()
        for r in records:
            if r.get("outcome") != "controller_off" or r.get("phase", "COMPLETE") != "COMPLETE":
                continue
            if str(r.get("card")) != str(self.cfg.get("card", r.get("card"))):
                continue
            st = r.get("step") or {}
            pw = (r.get("resolved") or {}).get("pw", st.get("pw"))
            if pw is None or st.get("pulses") is None:
                continue
            code = r.get("vid_code", mv_to_code(st["v"]))
            e = int(st["pulses"]) * int(pw)
            key = (code, e)
            if key in seen:
                continue
            seen.add(key)
            out.append({"code": code, "energy": e, "v": st["v"], "attempt_id": r.get("attempt_id"),
                        "label": S.short(st)})
        return sorted(out, key=lambda x: (x["code"], x["energy"]))

    @staticmethod
    def _match(entry, d, code, energy):
        vals = dict(d, code=code, energy=energy)
        for k, want in (entry.get("match") or {}).items():
            have = vals.get(k)
            if have is None:
                return False
            if isinstance(want, (list, tuple)):
                if not (want[0] <= have <= want[1]):
                    return False
            elif have != want:
                return False
        return True

    def check(self, d):
        """List of reasons this step must never be proposed (empty = allowed)."""
        d = S.normalize(d)
        why = []
        if d["v"] < self.floor_mv:
            why.append("below FLOOR %d mV" % self.floor_mv)
        for k, (lo, hi) in RUNNER_LIMITS.items():
            if not lo <= d[k] <= hi:
                why.append("%s=%s outside runner limits %d..%d" % (k, d[k], lo, hi))
        if d["revert"] not in (0, 2):
            why.append("revert=%s (runner allows 0 or 2)" % d["revert"])
        if not (d["t_restore"] == -20 or -20000 <= d["t_restore"] <= -1000):
            why.append("t_restore=%s (runner allows -20 or -20000..-1000)" % d["t_restore"])
        if d["pw"] < self.pw_min:
            why.append("pw %d us below the %d us write floor" % (d["pw"], self.pw_min))
        chk = schedule.check_step(d)
        if chk["abort"]:
            why.append("firmware would abort: %s" % chk["abort"])
            return why
        for t in ([] if self.allow_overlap else chk["overlaps"]):
            why.append("PMBus write at %d us is in flight across the PEX_RST release (47 us per write, queued)" % t)
        for t in chk["overwritten"]:
            why.append("VOUT step at %d us precedes the setup write at %d us" % (t, chk["resolved"]["reset_at"]))
        code = mv_to_code(d["v"])
        energy = int(d["pulses"]) * int(chk["resolved"]["pw"])
        for lt in self.latches:
            if code <= lt["code"] + self.depth_margin and energy >= lt["energy"] * (1 - self.energy_margin):
                why.append("latch energy: code %d <= %d+%d and pulses x pw %d >= %.0f (controller latched at %s)"
                           % (code, lt["code"], self.depth_margin, energy, lt["energy"] * (1 - self.energy_margin),
                              lt["label"]))
        for e in self.never:
            if self._match(e, d, code, energy):
                why.append("never list: %s" % e.get("note", e.get("match")))
        return why

    def allowed(self, d):
        return not self.check(d)

    def describe(self):
        lines = ["FLOOR %d mV (code >= %d), pw >= %d us, %s, no overwritten dip"
                 % (self.floor_mv, mv_to_code(self.floor_mv), self.pw_min,
                    "PMBus writes across the release allowed (push3)" if self.allow_overlap
                    else "no PMBus write across the release")]
        for lt in self.latches:
            lines.append("latch: code <= %d and pulses x pw >= %.0f  (learned from %s, %s)"
                         % (lt["code"] + self.depth_margin, lt["energy"] * (1 - self.energy_margin), lt["label"],
                            lt["attempt_id"]))
        for e in self.never:
            lines.append("never: %s  (%s)" % (e.get("match"), e.get("note", "")))
        return lines
