from . import script
class Attempt:
    def __init__(self, **k): self.k = k
class Rep:
    writes_failed = 0
    def __init__(self): self.pwd = script.cur[0] != 'n'; self.e1 = 0xC2FA
class Card:
    def __init__(self, *a): pass
    def attempt(self, a): script.cur[0] = script.nxt(); return Rep()
    def read(self, addr, reg, n):
        if script.cur[0] == "c": return [0x42, 0x20] if reg == 0x79 else [41, 0x00]
        return [0x00, 0x00] if reg == 0x79 else [0xC9, 0x02]
    def close(self): pass
    def trace(self): return []
