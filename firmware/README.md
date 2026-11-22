# Firmware and host patches for pico-ga100vfi

Twelve changes to [dalzyu/pico-ga100vfi](https://github.com/dalzyu/pico-ga100vfi), made against commit `e1ebd97`.
Apply them with `git am`:

```sh
cd pico-ga100vfi && git checkout e1ebd97
git am /path/to/cmp170hx-8gb-vfi-tools/firmware/*.patch
```

`series` lists them in order. Some are made with one line of context, so apply the whole series on exactly that base.
Built with pico-sdk 2.3.1 and the Arm GNU toolchain 14.2 for `pico2_w` (RP2350A and RP2350B) and `pico2`, normal and
debug builds, with no warnings.

| After patch | Firmware marker | What the lab ran |
|---|---|---|
| 0003 | `fw=lab-psr1` | the firmware on its cards up to 2026-11-19 (debug build byte-identical) |
| 0004 | `fw=lab-psr1-timing1` | the timing build, from 2026-11-20 (debug build byte-identical) |
| 0008 | `fw=lab-psr1-push1` | push1, on one card on 2026-11-20 |
| 0009 | `fw=lab-psr1-push2` | push2, on one card on 2026-11-20 (about 500 shots) |
| 0011 | `fw=lab-psr1-push3` | push3, from 2026-11-21: card E's goldilocks; the current firmware |

`-debug` is appended in the debug build. The lab's push1 and push2 builds arrange the unlock readback of patch 0007
inside the stock unlock function; here it is a separate function after it, so the patched upstream file never
carries the controller's unlock code in a diff. Behavior is the same; checksums differ from the lab's builds. The
runner in `../tools/runner` needs push1, push2 or push3 and is meant for push2 or push3.

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

## 0006: release `PEX_RST` from a hardware alarm at exactly t = 0 (push1)

The step loop blocks for every PMBus write (about 47 µs each), so a write due at or just before t = 0 used to push
the release late by up to one write (on one bench a single 36 µs pulse at `t_dip` +81 µs released at +59 µs). The
release is now a hardware alarm armed at t0, at the highest IRQ priority on core 0; the release step is gone from the
timeline. The assert step is skipped if the release has already fired, the pin is released unconditionally after the
timeline, and every shot prints `release_at=<us> scheduled=0 via=<alarm|fallback>`. Before the shot a
`glitch WARN` line names every scheduled write that overlaps t = 0. On the bench the releases then landed 1 to 4 µs
after t = 0, except the first release of every boot, which came 9 to 10 µs late (fixed in 0009).

## 0007: `u` always writes and reads back reg `0x13` and config ID `0x46`

The stock unlock returns early when the controller already reports MATCH, which happens after a soft-off because the
Pico and the standby rail stay powered; then reg `0x13` = `0x00` and config ID `0x46` = `0x1229` (which stop the
GPU's devinit from locking the controller again) were never written. A separate `unlock_config()` now runs after the
stock unlock on every `u`, writes both, reads them back and prints `unlock MATCH wp=00 cfg=1229` (`LOCKED` or `ERR`
otherwise). The runner stops unless it reads exactly that line.

## 0008: lab knobs (push1)

All off by default; with `fast` 0 or 1, `t_restore` above -1000, `revert` 0 or 1 and `hold_ms` 0 the timeline is
unchanged. `fast` 2..7 applies one subset of the fast profile (2 = `0x30` + `0x32`, 3 = `0x30`, 4 = `0x32`,
5 = `0xBA`, 6 = `0xB9`, 7 = `0x45`); `t_restore` <= -1000 moves the reset and the 600 mV setup write earlier so a long
dip is not overwritten; `revert=2` is a one-VID dither inside the train; `hold_ms` with a train that ends before t = 0
holds SCL from the release; after a shot that leaves READ_VOUT below 500 the firmware prints
`rail_code=... status ...` (READ_VOUT and seven status bytes). On one card the fast subsets 2..7 were run at the
card's brown-out floor and changed nothing there. The dither and SCL hold runs on push1 had a write overlapping the
release (a clean test needs push2), and a hold that starts at the release has not been run.

## 0009: push2, the current firmware

Release timing:

- The release handler is installed directly on the alarm IRQ, at the highest priority, and runs from SRAM with no
  flash code on its path (push1 used the SDK's dispatcher in flash; its first release of every boot came 9 to 10 µs
  late, which fits a cold XIP cache). Before every shot a throwaway alarm exercises the IRQ path and prints
  `prime_at=<us>`; a priming alarm that never runs aborts the shot. On one card the first shot of three fresh boots
  then released at 0.
- Core 1 feeds the watchdog with a busy wait instead of `sleep_us`, whose alarm-pool IRQ runs on core 0 with
  interrupts masked and could delay the release.
- Nothing is printed between the command and the end of the shot; the echo, WARN and FAIL lines follow it.

Fail closed:

- The release alarm is armed by the assert step after `PEX_RST` is low, so it can never release a card that was not
  put in reset; an assert more than one write late fails the shot.
- Refused before anything is written (`glitch ABORT: ...`): a VOUT step that the 600 mV setup write would
  overwrite, and any PMBus write in flight across t = 0, counting 47 µs per write and the queueing of back-to-back
  writes.
- The timeline stops at its first failed action; the stock rail is written while the card is still in reset when
  possible, then `PEX_RST` is released (`via=abort`); a FAIL line names the step.
- VRM profile: a tainted capture or reuse of an older capture is refused, `u` forgets the capture, the fast profile
  write is checked (except the full profile's `0x31` write, which this controller refuses), and the restore writes
  back only the registers the fast value touched and reads them back.
- SCL: after a hold the firmware waits up to 200 µs for SCL to read high and 200 µs more before the next PMBus write;
  the stock VOUT write after a hold from the release retries for up to 500 µs. Dither pulses are at least 47 µs.

## 0010: passive_snapshot stops at 0xBAD0 reads, a busy FUSECTRL or GFW

`host/passive_snapshot.py` gets the stop rules of the push2 runner, which carries a copy of `observe()`: a
`0xBAD0xxxx` value stops the read as poisoned (seen at FUSECTRL on one card with the link up, about 1 to 2 % of
shots at large delays, never on an exact repeat), FUSECTRL must read exactly the idle `0xE0040000`, and GFW boot
status must end in `0xFF`. Test: `host/test_passive_snapshot.py`.

## 0011: push3, a PMBus write may run across the `PEX_RST` release

push2 refused any shot with a PMBus write in flight at t = 0. The refusal is older than the alarm of `0006`/`0009`:
when the step loop released `PEX_RST` itself, a write due at or just before t = 0 pushed the release late. Now the
release is the alarm IRQ at the highest priority from SRAM, and PMBus writes run with interrupts on, so a write in
flight cannot move it. push3 drops only that refusal: the first such write is named after the shot in a
`glitch NOTE: push3:` line, `release_at` stays the proof of the release time (the runner scores only releases 0 to
4 µs after t = 0 by the alarm), and the refusal of a VOUT step before the setup write is kept. Every shot push2
accepts runs identically. This is what lets a long dip keep the rail low at the release and bring it back a few µs
after it, the shot that finished card E. Details, the host simulation (`../tools/hostsim`, 90 of 90), the bench
proof and a flashing trap: `../docs/PUSH3.md`.

## 0012: `Bar0.wr32` writes one aligned 32-bit store

`struct.pack_into` zeroes its destination before it packs, so upstream's `wr32` stored `0x00000000` to the register
and then the value: on the BAR0 mmap every `wr32` briefly cleared the whole register. With a reader on a shared mmap
the zero showed up in about 15 % of reads on Python 3.12. The patch writes through a 4-byte `memoryview` cast: one
aligned store, nothing in between. `rd32` already does a single load. In this repo `wr32` is used only by the opt-in
`DSOV_SET` write in classify (off by default since `0005`) and by `../tools/diag/enswtest.py`, so the lab's shots
never went through it; any tool that writes a live register with it (a PCIe PHY register, for example) did. Host
only, the firmware is unchanged.

## Not a firmware change: park and rescue

The third problem the lab hit is not in the firmware: after a shot that crashes the GPU, handing NVVDD back to the
GPU's own control (PWM-VID) lets the crashed GPU ask for the ~0.3 V floor (READ_VOUT 301 mV against 713 mV on a
healthy idle card). Every PEX_RST then reboots it into a brown-out and the link never returns; a half-booted card
can also answer config reads with CRS, the likely cause of three silent freezes of an Intel host. The fix is a host-side sequence, in
`tools/vfi_park.py` and `tools/vfi_rescue.py`: hold the rail at stock (787.5 mV) under PMBus, pulse PEX_RST, wait,
read config space, then hand the rail back. Upstream `campaign` ends with a bare revert (`w`); running park
instead is suggested in `docs/UPSTREAM_PR.md`.
