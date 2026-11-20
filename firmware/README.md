# Firmware and host patches for pico-ga100vfi

Five changes to [dalzyu/pico-ga100vfi](https://github.com/dalzyu/pico-ga100vfi), made against commit `e1ebd97`.
Apply them with `git am`:

```sh
cd pico-ga100vfi && git checkout e1ebd97
git am /path/to/cmp170hx-8gb-vfi-tools/firmware/*.patch
```

`series` lists them in order. Built with pico-sdk 2.3.1 and the Arm GNU toolchain 14.2 for `pico2_w` (RP2350A and
RP2350B) and the RP2350A debug build, with no warnings. Patches 0001-0003 give the firmware the lab ran on its
cards up to 2026-11-19 (the debug build is byte-identical; the normal build differs only in its embedded build
date). With 0004 applied the firmware is the timing build the lab ran from 2026-11-20 (`fw=lab-psr1-timing1`).

Three more firmware changes, built but not yet run on a card, are in `../experimental/firmware`.

## 0001: restore the card's own VRM profile after each shot

For the fast part of each shot the firmware writes a "fast" transient profile into six MP2888B registers (`0xBA`,
`0xB9`, `0x30`, `0x31`, `0x32`, `0x45`). Afterwards upstream writes back a hard-coded "stock" table:
`0x0019 0x0032 0xCE64 0x0072 0x7F81 0x7018`. That table comes from one card. Two of the lab's cards read
`0xBA = 0x000E`, `0xB9 = 0x0039` and `0x31 = 0x1872`, so every shot left their controller with another card's
settings.

The patch reads the six registers right before the fast profile and writes those values back after the shot, in
the glitch command and in the `k`/`K` commands. If any register already holds its fast value (an earlier shot was
interrupted), it refuses to capture: the glitch aborts with `glitch ABORT: VRM profile capture failed or tainted`
and asks for a power cycle. With nothing captured, nothing is written back.

It also adds a marker to the boot banner and the `v` status line, `fw=lab-psr1`, so host tools can check what is
flashed before they glitch. The tools in `../tools` refuse fast-profile shots on firmware without it.

On one card the controller refused the fast value for `0x31` (it stayed `0x1872`, with STATUS_CML `0x28`); the
glitch still worked there. Whatever firmware you run, read the six registers before the first shot
(`tools/vfi_preflight.py` does) and compare.

## 0002: stop classify at a poisoned fuse controller

A shot that disturbs the fuse read can leave the fuse controller poisoned: FUSECTRL (`0x820000`) reads
`0xBADFxxxx` and the controller stops answering normally. Upstream `classify()` then goes on to read DSOV, an FPF
row, the privilege mask, the CRC status and the shadow registers, and writes DSOV_SET. More BAR0 reads on such a
card are a host-hang risk.

The patch reads FUSECTRL first and, on `0xBADF`, reads nothing else: the verdict has DSOV, write-back, privilege
mask and CRC at 0 and the FPF row unreadable, with the FUSECTRL value in its shadow dict, and `campaign` counts the
shot as over. `campaign` also uses FUSECTRL instead of PMC_BOOT_0 as its liveness read after a lost shot.

A `PEX_RST` does not clear the state. On the lab's cards a poisoned controller stayed poisoned until a power cycle,
and every later shot of that boot read poisoned. (This README and the patch's docstring said otherwise until
2026-11-20; patch 0005 corrects the docstring.) End the boot at the first poisoned shot.

When you count outcomes: a poisoned shot shows DSOV 0, CRC 0 and privilege mask 0 because nothing was read. A real
DSOV flip has a nonzero privilege mask.

## 0003: firmware marker in the debug build

The debug build's `v` line gets `fw=lab-psr1-debug`, so the same check works on a debug board.

## 0004: per-step timing telemetry, passive snapshot, full-line serial replies

Firmware marker `fw=lab-psr1-timing1` (`fw=lab-psr1-timing1-debug`). Every step of the glitch timeline records
when its call started and ended and whether it succeeded; after the shot, before `glitch done`, the firmware prints
one `timing i=... scheduled=... act=... arg=... start=... end=... ok=...` line per step. Nothing is printed inside
the step loop. This is how the lab measured that one PMBus write takes about 47 µs and that the `PEX_RST` release
waits for a voltage write still running at t = 0 (table in `docs/TIMING_DIAGNOSTIC.md` of the patched tree and in
`../docs/MEASUREMENTS.md`).

Host side:

- `host/ga100vfi/glitch.py`: `Card.cmd(until=...)` waits for the newline that ends the completion line. Before, a
  USB packet that split `glitch done` from its fields left the reply unparsed and the shot was logged as a NACK.
- `host/passive_snapshot.py --bdf <bdf>`: a read-only BAR0 observer (FUSECTRL, GFW, DSOV, CRC, privilege mask) that
  stops at the first `0xBADFxxxx` value, I/O error, all-ones read, GFW still booting or DSOV other than 0 or 1, and
  never writes.
- Tests: `host/test_passive_snapshot.py`, `host/test_serial_response.py`.

## 0005: classify stops at a 0xBADF DSOV; the EN_SW_OVERRIDE write is opt-in

- DSOV is checked right after FUSECTRL. If it reads `0xBADFxxxx` (seen on one card behind a healthy FUSECTRL),
  nothing else is read or written.
- Check 5 of the method wrote 1 to `0x00820040` after every shot. That register is EN_SW_OVERRIDE, and on the lab's
  bench it kept its value across `PEX_RST`: after the first shot of a boot, every later shot ran with the software
  override on. The write now happens only with `classify(..., write=True)` or `CLASSIFY_WRITE=1`. Without it the
  register is only read, `Verdict.dsov_written` is false and check 5 is left out of `Verdict.checks`, so
  `Verdict.passed` (and `campaign`'s goldilocks) rests on DSOV 0, a readable FPF row, privilege mask `0xFFFFFFFF`
  and CRC `0x55`.
- An A/B run on one card (four boots alternating with and without the write, same ladder) showed no edge change.
- Test: `host/test_classify.py`.

Run the host tests from the patched tree: `cd host && python3 -m unittest test_classify test_passive_snapshot
test_serial_response` (needs pyserial).

## Not a firmware change: park and rescue

The third problem the lab hit is not in the firmware: after a shot that crashes the GPU, handing NVVDD back to the
GPU's own control (PWM-VID) lets the crashed GPU ask for the ~0.3 V floor (READ_VOUT 301 mV against 713 mV on a
healthy idle card). Every PEX_RST then reboots it into a brown-out and the link never returns; a half-booted card
can also answer config reads with CRS, the likely cause of three silent freezes of an Intel host. The fix is a host-side sequence, in
`tools/vfi_park.py` and `tools/vfi_rescue.py`: hold the rail at stock (787.5 mV) under PMBus, pulse PEX_RST, wait,
read config space, then hand the rail back. Upstream `campaign` ends with a bare revert (`w`); running park
instead is suggested in `docs/UPSTREAM_PR.md`.
