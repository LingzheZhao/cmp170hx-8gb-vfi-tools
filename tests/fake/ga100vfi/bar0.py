"""Fake BAR0: values from the current shot code; every access is logged and a write fails the run."""
from . import _bench as b


def restore(bdf="0000:05:00.0", wait_s=0.25, settle_s=0.15):
    b.log("RESTORE")
    return b.OUTCOMES[b.cur[0]] is not None


class Bar0:
    def __init__(self, bdf="0000:05:00.0", size=16 << 20):
        pass

    def rd32(self, off):
        if off == 0x820000:
            b.look[0] += 1
        b.log("RD %06x" % off)
        return b.bar0_value(off)

    def wr32(self, off, value):
        b.log("WR %06x" % off)
        raise AssertionError("BAR0 write at 0x%06x" % off)

    def close(self):
        pass

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        pass
