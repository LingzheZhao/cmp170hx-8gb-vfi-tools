"""Fake serial client: answers like lab-psr1-push1 firmware, records every command."""
from __future__ import annotations

import os
from dataclasses import dataclass

from . import _bench as b


@dataclass
class Attempt:   # same fields and command() as host/ga100vfi/glitch.py
    v_dip_mv: int = 325
    t_dip_us: int = -100
    v_restore_mv: int = 650
    t_restore_us: int = -20
    t_revert_us: int = 0
    fast: int = 1
    hold_ms: int = 0
    pulses: int = 1
    pw: int = 0
    gap: int = 0
    delay: int = 75
    revert: int = 1

    def command(self) -> str:
        return (f"g {self.v_dip_mv} {self.t_dip_us} {self.v_restore_mv} "
                f"{self.t_restore_us} {self.t_revert_us} {self.fast} "
                f"{self.hold_ms} {self.pulses} {self.pw} {self.gap} {self.delay} "
                f"{self.revert}")


@dataclass
class Report:
    pwd: bool
    e1: int
    nvvdd: float | None
    writes_failed: int
    raw: str


class Card:
    def __init__(self, port="/dev/ttyACM0", baud=115200):
        b.log("OPEN %s" % port)

    def close(self):
        b.log("CLOSE")

    def cmd(self, line, wait=0.3, total=3.0, until=None):
        b.log("SER " + line)
        if line == "u":
            return os.environ.get("FAKE_UNLOCK", "unlock already MATCH (CML 0x08)\r\nunlock MATCH wp=00 cfg=1229")
        if line == "v":
            return os.environ.get("FAKE_V", "clk=400 MHz  nvvdd=0.0000 V  sample_ns=240.0  "
                                  "last reset: power-on / external reset  fw=lab-psr1-push1-debug")
        raise AssertionError("unexpected serial command %r" % line)

    def status(self):
        return self.cmd("v")

    def lock_status(self):
        b.log("SER z")
        return True, 0xC2FA

    def read(self, addr, reg, n):
        b.log("PMBUS r %02x %02x %02x" % (addr, reg, n))
        off = b.cur[0] == "c"
        value = {0x79: 0x2042 if off else 0x2C00, 0x8B: 41 if off else 713, 0x13: 0x00, 0x46: 0x1229,
                 0x7E: 0x08, 0x88: 0x2F00, 0x8D: 0x0170}[reg]
        return value.to_bytes(n, "little")

    def attempt(self, a):
        code = b.next_shot()
        b.log("SHOT %s %s" % (code, a.command()))
        pw, gap = max(a.pw, 36), max(a.gap, 36)
        raw = (f"glitch vd={a.v_dip_mv} td={a.t_dip_us} vr={a.v_restore_mv} tr={a.t_restore_us} tv={a.t_revert_us} "
               f"fast={a.fast} hold={a.hold_ms} pw={pw} gap={gap} delay={a.delay} revert={a.revert} "
               f"reset_at=-1000 dither=0 hold_at=none\r\n"
               "timing i=0 scheduled=-1300 act=8 arg=32 start=-1300 end=-1253 ok=1\r\n"
               "release_at=0 scheduled=0 via=alarm\r\n")
        if code == "c":
            raw += "rail_code=41 status 7a=10 7b=00 7c=00 7d=00 7f=00 80=ERR 78=42\r\n"
        e1 = int(os.environ.get("FAKE_E1", "0xc2fa"), 16)
        raw += "glitch done pwd=1 e1=0x%04x nvvdd=0.7000 writes_failed=0" % e1
        return Report(True, e1, 0.7, 0, raw)

    def trace(self):
        return []
