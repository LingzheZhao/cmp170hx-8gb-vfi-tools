# What the bench measured (November 2026)

Facts behind the November 20 changes, from the lab's bench: one glitch host, a Raspberry Pi Pico 2 W, 8 GB CMP 170HX
cards (`10de:20c2`). Each item says what it rests on. Your bench and cards will differ; treat single-card results
as reports, not rules.

## Shot timing

**One PMBus write takes about 47 µs.** Measured with the timing build (`firmware/0004`), which logs the start and end
of every timeline step. The firmware's scheduler assumes 36 µs (`PMBUS_W_US`).

**The `PEX_RST` release slips when a voltage write overlaps it.** The firmware plays a shot as a list of timed steps
around the release (t = 0) in a blocking loop: each step runs to its end before the next starts. When a
`VOUT_COMMAND` write is still running at t = 0, the release waits for it. Release times measured with harmless
700 mV dips (the GPU idles near 695 mV), `delay` 117 µs, on one card (`tools/diag/timing_controls.py`):

| Shot | `t_dip` | `PEX_RST` released at |
|---|---|---|
| single 36 µs pulse | +81 µs | +59 µs |
| single 36 µs pulse | +82 µs | +13 µs |
| single 36 µs pulse | +100 µs | +30 µs |
| standard 3 x 198 µs train | -85 µs | +43 µs |
| slow single 36 µs dip (`fast` 0), recovery 825 mV | +30 µs | +9 µs |
| standard 3 x 198 µs train | -60 µs (the default) | on time |

A timing map whose shots put a write across t = 0 moves the release along with `t_dip`, so it does not measure what
it seems to. On one card the lab had mapped a sharp edge between `t_dip` +81 µs (poisoned) and +82 µs (under) with
single short dips; it sits where the table shows the release jumping from +59 µs to +13 µs. Check where each write of a shot falls before reading
meaning into a timing edge.

**With the alarm release.** `firmware/0006` (push1) releases `PEX_RST` from a hardware alarm: on one card the releases
then landed 1 to 4 µs after t = 0 for the shots in the table, but the first release of every boot came 9 to 10 µs
late. `firmware/0009` (push2) runs the release handler from SRAM and primes the alarm before every shot: the first
shot of three fresh boots released at 0, and push2 refuses any shot with a write in flight across t = 0 before it
writes anything. The runner refuses to score a shot released outside 0 to 4 µs (MISTIMED).

**Pulse widths below 36 µs are 36 µs.** The firmware raises any shorter width to one write time
(`if (pw < PMBUS_W_US) pw = PMBUS_W_US` in `pwmvid.c`). Widths of 25, 27, 30 and 35 µs all send the same pulse, so
comparing them compares one setting with itself. Log the values the firmware echoes back for each shot, not the
requested ones. `tools/vfi_*.py` refuse widths below 36 µs; the runner sends what the step asks for, so do not ask
for less.

## EN_SW_OVERRIDE (`0x820040`)

**The stock classifier wrote it after every shot.** Check 5 of the method ("write DSOV = 1, read back") writes 1 to
`0x820040`, which is EN_SW_OVERRIDE.

**It survives `PEX_RST`.** Test on one card, fresh boot, 700 mV shots (`tools/diag/enswtest.py`): it read 0 after a
shot; after a write of 1 it still read 1 after two more `PEX_RST` shots. So with the stock classifier every shot
after the first in a boot ran with the software override on.

**An A/B run on one card showed no edge change.** Four boots alternating without and with the write, same ladder
(two 450 mV shots, then 381 mV down to 344 mV in 6.25 mV steps, 3 x 80 µs pulses, five shots per step). Three boots,
both modes, stayed under down to 356 mV and lost the link at 350 mV. The fourth boot, without the write, lost the
link on its second shot at 450 mV.

`firmware/0005` makes the write opt-in (`CLASSIFY_WRITE=1`). The runner and the tools here run without it.

## Check that the shot ran

**A locked MP2888B acknowledges dip writes and discards them.** Reported by the
[esp32-ga100vfi](https://github.com/sayhellotojungle/esp32-ga100vfi) project. Such a shot changes nothing and reads
"under", so a whole campaign can run without a single dip. After every shot that keeps the rail under PMBus control
(`revert=0`), the firmware's `glitch done` line must show `pwd=1` and `e1=0xc2fa`. The runner stops with exit 12
("NO DIP") and the tools stop with a safety error when it does not.

**The unlock can skip the controller's config writes.** The firmware's `u` returns early when the MP2888B already
reports MATCH (unlocked), and then skips register `0x13` = `0x00` (write protect) and the config ID `0x46` =
`0x1229`. The Pico stays powered over USB and the standby rail stays up through a soft-off, so a boot can start with
MATCH already set. On the boot the lab checked, both registers were set. Read them back each boot
(`firmware/0007` does it in `u`; the runner stops unless it reads `unlock MATCH wp=00 cfg=1229`).

**Split serial replies.** The host client stopped reading at the first `glitch done`, before the rest of that line
had arrived. When a USB packet split the reply, the shot was logged as a NACK and went unclassified, without a park.
Fixed in `firmware/0004`.

## Poison is sticky

Once a shot poisoned the fuse controller (FUSECTRL or DSOV reading `0xBADFxxxx`, or the stock classifier reporting
CRC 0 and privilege mask 0 because it read nothing), every later shot of that boot read poisoned on the lab's cards,
including shots at 700 and 450 mV that cannot glitch anything. A `PEX_RST` did not clear it; a power cycle did. End
the boot at the first poisoned shot; the runner does.

## The controller-off latch, and shorter pulses

The MP2888B can switch its output off: STATUS_WORD (`0x79`) reads `0x2042` and READ_VOUT (`0x8B`) about 41 mV. Only
a power cycle cleared it; the cause is not known. Three cards latched this way on deep shots: one at 381-388 mV with
3 x 198 µs trains, two others at about 300 mV. On these cards STATUS_WORD idles at `0x2c00` or `0x2c02`, so bit 13 is
a standing bit, not a fault; the latch adds OFF (bit 6) and CML (bit 1). The runner stops with exit 9 ("CONTROLLER
OFF") when bit 6 is set or READ_VOUT is below 500.

On the card that latched at 381 mV with 198 µs pulses (two boots), shorter pulses did not latch: 3-pulse trains of
120 µs at 388-375 mV, 100 µs at 375-369 mV and 80 µs at 362-356 mV (70 shots, `t_dip` -60 µs) all stayed under with
the link alive and no latch. A lower restore also helped: with `v_restore` 588 mV instead of 750 mV the 3 x 198 µs
train at 381 mV did not latch (2 of 2), and the standard train then ran from 375 down to 331 mV (32 shots) with no
latch; one first shot at 331 mV with delay 200 µs still latched (`METHOD.md`, section 4). One card: a report, not a
rule. Start a new card with shallow dips, step down, and watch STATUS_WORD and READ_VOUT after every shot.

## Other bench notes

- **No NVVDD probe lead while glitching.** With the sense lead attached, one card crashed 30 to 70 mV earlier than
  without it, and its earlier margins came back when the lead was removed. Use the lead for verification shots only.
- **A fuse alias register once read `0xBADF1020` with everything else normal.** After a run that scored every shot
  "under", a later read found fuse alias registers (`0x820324`, `0x820410`, per-GPC TPC and PES registers) at
  `0xBADF1020` / `0xBAD00100` while FUSECTRL, DSOV, CRC and the privilege mask looked normal. A repeat of that run
  with a check after every shot never tripped; the state appeared after the runs, not on a shot, and its cause is not
  known. The runner reads one alias (`SENTINEL`, default `0x820410`) after every clean read and stops unless it
  equals the boot's baseline, as a cheap guard.
- **Rare fuse-unit errors.** On one card FUSECTRL read `0xBAD00100` on about 1 to 2 % of shots at large delays (300 to
  400 µs), once still 3 s later; exact repeats were under. The runner stops at a `0xBAD0` read like at `0xBADF`
  (`firmware/0010` gives `passive_snapshot.py` the same rule).
