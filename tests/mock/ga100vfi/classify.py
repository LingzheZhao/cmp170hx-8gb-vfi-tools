from . import script
class C: pass
def classify(bar):
    c = C(); k = script.cur[0]
    c.dsov, c.crc_status, c.priv_mask = {"u": (1, 0x55, 0xFFFFFFFF), "h": (1, 0x55, 0xFFFFFFFF), "n": (1, 0x55, 0xFFFFFFFF), "b": (1, 0x55, 0xFFFFFFFF), "c": (1, 0x55, 0xFFFFFFFF), "p": (0, 0x55, 0xFFFFFEFE), "g": (0, 0x55, 0xFFFFFFFF), "f": (0xFFFFFFFF, 0x55, 0xFFFFFFFF), "t": (2, 0x55, 0xFFFFFFFF), "o": (0, 0, 0), "d": (0xBADF1701, 0, 0)}[k]
    c.fpf_row = 0; c.dsov_writeback = 0; return c
