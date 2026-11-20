# Pull request text for dalzyu/pico-ga100vfi

Not sent yet. To send it: fork dalzyu/pico-ga100vfi, apply `firmware/*.patch` with `git am` on a branch from
`main` (they were made against `e1ebd97`), push the branch and open a pull request with the title and body below.
The body covers patches 0001-0003; text for 0004 and 0005 (added 2026-11-20) is at the end, for the same pull
request or a second one.

---

**Title:** Restore each card's own VRM profile; stop classify at a poisoned fuse controller

**Body:**

Three small changes from running the tool on several 8 GB CMP 170HX cards.

**1. firmware: restore the card's own VRM profile after the fast profile**

After each shot the firmware writes the hard-coded "stock" values from `PROFILE` back into the six MP2888B
transient registers (`0xBA 0xB9 0x30 0x31 0x32 0x45`). Those values come from one card. Two of our cards read
`0xBA=0x000e`, `0xB9=0x0039`, `0x31=0x1872`, so every shot left their controller with another card's settings.

This change reads the six registers right before the fast profile and writes those values back afterwards (glitch
command and `k`/`K`). If any register already holds its fast value, which means an earlier shot was interrupted,
the glitch aborts and asks for a power cycle instead of saving the fast values as "stock". With nothing captured,
nothing is written back. The banner and the `v` line carry a marker (`fw=lab-psr1`) so host tools can check what is
flashed; rename it if you prefer another string.

Tested: `k`/`K` round trip read back exact, and several thousand shots across three cards with each card's values
restored after every shot. On one card the controller refused the fast value for `0x31` (stayed `0x1872`, CML
`0x28`); the glitch still worked there.

**2. host: stop classify at a poisoned fuse controller**

A shot that disturbs the fuse read can leave FUSECTRL (`0x820000`) at `0xBADFxxxx`. `classify()` then kept reading
DSOV, the FPF row, the privilege mask, CRC and the shadows, and wrote DSOV_SET, on a controller in that state.
We treat further BAR0 reads there as a host-hang risk. Now FUSECTRL is read first; on `0xBADF` nothing else is read and the shot is counted as over.
`campaign` also uses FUSECTRL instead of PMC_BOOT_0 for its liveness read after a lost shot.

A poisoned shot reports dsov=0, crc=0, priv=0 because nothing was read. That is worth knowing when counting DSOV
flips in logs: a real flip has a nonzero privilege mask.

**3. firmware: show the firmware marker in the debug build status line**

Same marker in the debug build's `v` line (`fw=lab-psr1-debug`).

**Related observations, not in this PR:**

- After a shot that crashes the GPU, the end-of-campaign revert (`w`) hands NVVDD back to the crashed GPU, which
  asks for the ~0.3 V floor (READ_VOUT ~301 mV against ~713 mV on a healthy idle card). Every PEX_RST then reboots
  it into a brown-out and the link does not return; a half-booted card can also answer config reads with CRS, and
  an Intel root complex retries those forever (our reading of three silent host freezes). What fixed both for us: take the rail under
  PMBus control at stock (`u`, VOUT_COMMAND `0x007E`, `0xE1` = `0xC2FA`), check READ_VOUT, pulse `r`, wait about
  1.5 s, read config space, then `w`. We run that at the end of every campaign instead of a bare revert. Our host
  side version is in https://github.com/darkmaniac7/cmp170hx-8gb-vfi-tools (`tools/vfi_park.py`).
- `ga100vfi attempt` fails with an AttributeError: `cmd_attempt` reads `args.fast` and `args.hold_ms`, but the
  `attempt` subparser defines neither option.
- `bar0.DEFAULT_BDF` (`0000:1a:00.0`) is used when `--bdf` is left out; on most hosts that address is another
  device or nothing. Requiring `--bdf`, or checking that the address is a `10de:20c2`/`10de:2082` function before
  any access, would avoid reads and writes to the wrong device.

Thanks for the tool.

---

**Patches 0004 and 0005 (2026-11-20)**

**4. firmware: per-step timing telemetry; host: passive snapshot, full-line replies**

Every step of the glitch timeline records the start and end of its call; after the shot the firmware prints one
`timing ...` line per step (format in `docs/TIMING_DIAGNOSTIC.md`; nothing is printed inside the loop). It showed
us that one PMBus write takes about 47 µs, not 36, and that the `PEX_RST` release waits for a `VOUT_COMMAND` write
still running at t = 0: a single 36 µs pulse at td +81 released at +59 µs, at td +82 at +13 µs; the standard train
at td -60 released on time. `Card.cmd(until=...)` now waits for the end of the `glitch done` line (a split USB
packet made shots unparseable), and `host/passive_snapshot.py` is a read-only observer that stops at the first
`0xBADF` read. The marker becomes `fw=lab-psr1-timing1`; drop that hunk if you prefer.

**5. host: classify stops at a 0xBADF DSOV; the EN_SW_OVERRIDE write is opt-in**

DSOV can read `0xBADFxxxx` behind a healthy FUSECTRL; classify now stops there too. Check 5 wrote 1 to
`0x00820040` (EN_SW_OVERRIDE) after every shot, and on our bench that register kept its value across `PEX_RST`,
so every later shot of a boot ran with the software override on. The write is now opt-in (`write=True` or
`CLASSIFY_WRITE=1`); without it check 5 is left out of `Verdict.checks` and `campaign` still reports goldilocks
on DSOV 0, FPF readable, PLM `0xffffffff` and CRC `0x55`. An A/B run on one card showed no edge change from the
write. Also: a poisoned controller stayed poisoned until a power cycle on our cards, so the docstring no longer
says the next `PEX_RST` clears it.
