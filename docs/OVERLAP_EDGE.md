# What finished card E: the overlap edge

Card E (census ceiling 110, the same voltage-controller profile as cards A and C) ran more than 3,000 shots over three
days without a goldilocks frame. On 2026-11-21 it gave one on the 13th shot of the first run that push3 made possible.
This page is the map, the shot and how to look for the same edge on another card.

## The wall card E kept hitting

Every shape push2 could make moved card E along one ladder, light to heavy:

| Frame | DSOV | CRC | PLM | Meaning |
|---|---|---|---|---|
| light | 1 | `0x55` | `0xffffffff` | the boot was disturbed (often a GFW stall, sentinel 0), DSOV was not |
| CRC44 | 0 | `0x44` | `0xffffffff` | DSOV flipped, but fuse blocks 0 and 2 were never checked |
| over | 0 | `0x00` | `0xfffffcee` | DSOV flipped and the blocks were wiped |
| lost | | | | the GPU fell off the bus |

DSOV 0 only ever came with CRC `0x44` or `0x00`. A CRC44 frame looks close, but an active sense keeps CRC at `0x44`
(EFP goes to 1, DSOV stays 0) and the write-window check fails on CRC: it is not a write window, and the lab did not
burn on it. Voltage, timing, temperature (a sub-step knob, 0.1 °C per raw unit of READ_TEMP1), restore level, slew,
second dips, one-step dither and the community 3-pulse train all moved card E along the same ladder.

On card E, DSOV flipped only when the rail was still low **at the reset release**. A single long dip that stays low
across the release does that, and its rise time decides the rest: back up 48 µs after the release gave CRC44, 68 µs
gave light. Back up before the release gave under. The rise times between 0 and +47 µs were never tried, because
putting the rise there needs the restore write in flight at t = 0, and push2 refuses that shot.

## The map push3 opened

One long dip at 381 mV, fast slew, restore 625 mV, the stock 787.5 mV write pinned at the same time on every shot
(`tv` 151 or 171) so that only the rise moves. The restore write starts at s1 + pw; the rail comes back up about 47 µs
later. First pass, one shot per cell, 39.3 to 40.6 °C:

Dip write at -130 µs (rail falls near -83):

| pw | restore write | rail back up | frame |
|---|---|---|---|
| 131 | +1 | +48 | under (CRC44 on earlier days) |
| 126 | -4 | +43 | light, GFW stall |
| 120 | -10 | +37 | over |
| 110 | -20 | +27 | over |
| 100 | -30 | +17 | over |
| 90 | -40 | +7 | over |
| 83 | -47 | 0 | under |

Dip write at -177 µs (rail falls near -130):

| pw | restore write | rail back up | frame |
|---|---|---|---|
| 178 | +1 | +48 | light |
| 177 | 0 | +47 | over |
| 167 | -10 | +37 | light |
| 157 | -20 | +27 | light |
| 147 | -30 | +17 | under |
| **137** | **-40** | **+7** | **GOLDILOCKS** |

The window push3 opened is not empty. With the dip written at -130 µs it is the heaviest part of the map (over from +7
to +37 µs), with a sharp edge to under between a rise at 0 and at +7 µs. With the dip written at -177 µs the same
window read lighter (light at +37 and +27 µs, under at +17 µs), and the goldilocks came at +7 µs. One shot per cell:
the edges are real, their exact positions are not yet repeated.

**The shot:** `[381, -60, 1, 137, 625, 1, 1, 0, -20, 0, 117, 150, 171]`: dip write -177 to -130 µs, restore write -40
to +7 µs (in flight across the release), stock write at +181 µs, `release_at=0 via=alarm`, NVVDD trough 334 mV and
134.6 µs below 450 mV, 39.7 °C, the 13th shot of the boot.

**After it:** the active sense with EN_SW_OVERRIDE = 1 showed CRC `0x55`, EFP 1, DSOV 0 (a real write window). After
the burn and a cold boot: GPC disable `0x23` to `0x20` (only the defective GPC off), FBP disable `0x2d` to `0x09`
(only the two defective FBPs off: the 80 GB layout by its masks, not yet checked under a driver), SM 70 to 106 (two
RECONFIG TPCs still off; ceiling 110), and a primary LnkCap of 16 GT/s from the BOOT_GEN23 record.

## Looking for the same edge on another card

Try this when a card goes from under straight to link loss, CRC44 or over, with nothing in between, on the shapes in
[`SWEET_SPOT.md`](SWEET_SPOT.md). You need push3 ([`PUSH3.md`](PUSH3.md)).

1. **Plan:** `goldilocks_hunt.py overlap --out plan-overlap.json` (or `cimp hunt overlap ...`). Defaults: depths
   388, 381, 375, 369 (shallow to deep), dip writes at -177 and -130 µs, the rail back up 0, 3, 7, 11 and 17 µs after
   the release, plus the +48 µs anchor (a push2-legal shot, card E's CRC44 point), restore 625 mV, `tv` 171.
2. **Ladder shallow to deep.** Fragile cards lose the link at the deep end; the ladder finds the depth where the card
   starts to answer before it gets there. No conditioning or warm-up shot at a deep code on a fragile card.
3. **Keep `tv` pinned.** With `tv` 0, a shot whose restore write starts before the release moves the stock 787.5 mV
   write to +10 µs, right on top of the edge; the shots then differ in two things at once.
4. **Check the first shots on the bench:** every shot needs `release_at=0 via=alarm`; with `--trace` (debug firmware
   and an NVVDD lead), the time below 450 mV should grow with pw (pw minus about 3 µs on card E).
5. **Then go fine.** Once a depth shows the over-to-under edge, scan the rise 1 µs at a time across it, at both falls,
   and dwell. The temperature band matters as much as on any other edge: card E's hits came at 39 to 41 °C.
6. **Hold rule unchanged:** only DSOV 0 + CRC `0x55` + PLM `0xffffffff`, then your own active sense and window check.

`cimp watch` shows the run live: phase, temperature with a fan cue, and every shot as light, CRC44, over, lost,
under, poison or GOLDILOCKS ([`CIMP.md`](CIMP.md)).
