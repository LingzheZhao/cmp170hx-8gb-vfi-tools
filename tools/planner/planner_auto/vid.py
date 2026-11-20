"""VID code arithmetic, bit-exact with the firmware.

pwmvid.c: `static int mv_to_code(int mv) { return (int)(mv / VID_LSB_MV + 0.5f); }` with VID_LSB_MV = 6.25f.
The RP2350 evaluates that in single precision, so the division and the +0.5 are emulated in float32 here.
"""
import math
import struct

VID_LSB_MV = 6.25


def _f32(x):
    return struct.unpack("<f", struct.pack("<f", x))[0]


def mv_to_code(mv):
    """Commanded millivolts -> VOUT_COMMAND code, rounded exactly as the firmware rounds it."""
    q = _f32(_f32(float(mv)) / _f32(VID_LSB_MV))
    s = _f32(q + _f32(0.5))
    return int(s)  # C cast truncates toward zero; int() does the same


def code_to_mv(code):
    """Exact rail command of a code in mV (may be fractional, e.g. 55 -> 343.75)."""
    return code * VID_LSB_MV


def canonical_mv(code, prefer=()):
    """Integer mV to put in a runner step so that the firmware resolves `code`.

    `prefer` lists mV values already used in the ledger; the first one that resolves to `code` wins, so the planner
    keeps the lab's spelling (437 rather than 438 for code 70). Otherwise round half up.
    """
    for mv in prefer:
        if mv_to_code(mv) == code:
            return int(mv)
    mv = int(math.floor(code * VID_LSB_MV + 0.5))
    if mv_to_code(mv) != code:  # cannot happen for 6.25 mV steps, kept as a guard
        for d in (-1, 1, -2, 2):
            if mv_to_code(mv + d) == code:
                return mv + d
        raise ValueError("no integer mV resolves to code %d" % code)
    return mv
