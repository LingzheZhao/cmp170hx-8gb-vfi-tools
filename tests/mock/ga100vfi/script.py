"""Shot script for the fake bench: MOCK=u,l,... gives one outcome code per shot (then "u").
u under, l lost, p partial (DSOV 0, mask 0xFFFFFEFE), x poisoned FUSECTRL, d poisoned DSOV behind a healthy
FUSECTRL, g goldilocks, h hidden poison in the sentinel alias, b GFW never finishes booting, n no dip (the controller
refused the shot), f DSOV 0xFFFFFFFF, t DSOV 2, o over (DSOV 0, CRC 0, mask 0), c controller latched off."""
import os
OUT = [c for c in os.environ.get("MOCK", "").split(",") if c]
pos = [0]
def nxt():
    k = OUT[pos[0]] if pos[0] < len(OUT) else "u"; pos[0] += 1; return k
cur = ["u"]
