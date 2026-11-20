from . import script
def restore(bdf, wait_s=0): return script.cur[0] != "l"
class Bar0:
    def __init__(self, b): pass
    def __enter__(self): return self
    def __exit__(self, *a): pass
    def rd32(self, off):
        if script.cur[0] == "h" and off == 0x820410: return 0xBADF1020
        if off == 0x118234: return 0x01 if script.cur[0] == "b" else 0x3FF
        return 0xBADF1701 if script.cur[0] == "x" else 0xE0040000
