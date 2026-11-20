"""Shared state of the fake bench: one outcome code per shot from FAKE_SHOTS, events appended to FAKE_LOG."""
import os

LOG = os.environ["FAKE_LOG"]
SHOTS = [c for c in os.environ.get("FAKE_SHOTS", "").split(",") if c]
HEALTHY = {0x820000: 0xE0040000, 0x118234: 0x3FF, 0x820084: 1, 0x820070: 0x55, 0x8200FC: 0xFFFFFFFF,
           0x820410: 0x00000001}   # a healthy alias sentinel (0x1 on one card here)
# Per code: register overrides for each successive passive look (the last one repeats); None = link lost.
OUTCOMES = {
    "u": [{}],                                                   # under
    "l": None,                                                   # lost: config space never returns
    "x": [{0x820000: 0xBADF1701}],                               # poisoned at FUSECTRL
    "d": [{0x820084: 0xBADF1701}],                               # poisoned DSOV behind a healthy FUSECTRL
    "q": [{0x820084: 0, 0x820070: 0xBAD00100}],                  # 0xBAD0 error read at CRC
    "g": [{0x820084: 0}],                                        # goldilocks
    "f": [{0x820084: 0xFFFFFFFF}],                               # bad DSOV, CRC and PLM healthy
    "t": [{0x820084: 2}],                                        # DSOV neither 0 nor 1
    "p": [{0x820084: 0, 0x8200FC: 0xFFFFFEFE}],                  # partial
    "o": [{0x820084: 0, 0x820070: 0, 0x8200FC: 0}],              # over-glitched
    "i": [{0x820000: 0xE0040001}],                               # FUSECTRL not idle
    "h": [{0x820410: 0xBADF1020}],                               # hidden poison in the alias sentinel
    "a": [{0x820410: 0xBAD00100}],
    "z": [{0x820410: 0x00000000}],                               # sentinel 0
    "w": [{0x820410: 0xFFFFFFFF}],                               # sentinel all-ones
    "m": [{0x820410: 0x00000002}],                               # sentinel differs from the boot baseline
    "b": [{0x118234: 0x301}],                                    # GFW never finishes booting
    "B": [{0x118234: 0x301}, {}],                                # GFW booting on the first look only
    "c": [{}],                                                   # controller latched off (STATUS_WORD / READ_VOUT)
    "y": [{0x820000: 0xBAD00100}, {}],                           # 0xBAD0 at FUSECTRL, idle again on the next read
    "Y": [{0x820000: 0xBAD00100}],                               # 0xBAD0 at FUSECTRL that stays
    # release-timing codes (MISTIMED): registers as under unless noted
    "L": [{}], "F": [{}], "N": [{}], "W": [{}],
    "G": [{0x820084: 0}], "K": None, "X": [{0x820084: 0xBADF1701}], "P": [{0x820084: 0, 0x8200FC: 0xFFFFFEFE}],
    # serial-level codes: the shot reply itself
    "n": [{}], "A": [{}], "E": [{}], "D": [{}],
}
RELEASE = {"L": "release_at=9 scheduled=0 via=alarm", "F": "release_at=0 scheduled=0 via=fallback", "N": None,
           "K": "release_at=10 scheduled=0 via=alarm", "G": "release_at=9 scheduled=0 via=alarm",
           "X": "release_at=10 scheduled=0 via=alarm", "P": "release_at=9 scheduled=0 via=alarm"}
cur = [os.environ.get("FAKE_BASE", "u")]   # the pre-shot baseline look sees this code
shot = [0]
look = [0]
open_cards = [0]


def log(line):
    with open(LOG, "a") as f:
        f.write(line + "\n")


def next_shot():
    cur[0] = SHOTS[shot[0]] if shot[0] < len(SHOTS) else "u"
    shot[0] += 1
    look[0] = 0
    return cur[0]


def bar0_value(offset):
    looks = OUTCOMES[cur[0]]
    regs = dict(HEALTHY)
    regs.update(looks[min(look[0] - 1, len(looks) - 1)])
    return regs[offset]
