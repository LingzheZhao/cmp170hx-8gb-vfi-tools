# Method notes from the push2 campaign (November 2026)

> **This page is the record of the push2 nights, not the current method.** Card E was finished on 2026-11-21 with
> the push3 overlap shot (`docs/OVERLAP_EDGE.md`). Section 7 ("Card E is unsolved", remove the PWM-VID link) and
> the CRC table in section 6 (`0x77` as "closed"; the text below it corrects that: `0x77` is blocks 0 and 2 failed)
> are kept as history. Start from `AGENTS.md` and `docs/SWEET_SPOT.md`; this repository does not use the PWM-VID
> cold power-on path.

What changed in how the lab searches for the glitch window, and why. Most of it comes from one night on one 8 GB card
(card E on the [cmp.wiki results page](https://cmp.wiki/wiki/Voltage-glitch_unlock:_lab_results_and_fuse_findings)),
about 500 shots with the push2 firmware (`firmware/0009`) and the push2 runner (`tools/runner/`). One card is a
report, not a rule: each item says what it rests on.

The strategy that finished cards G and H on 2026-11-20 builds on this and adds two phases this page does not have
(a map of the fuse read and a scan of the GPU's init right at the release): see `docs/SWEET_SPOT.md` and
`tools/hunter/goldilocks_hunt.py`.

## 1. Check the release timing first

Every shot is timed against the `PEX_RST` release (t = 0). The old step loop released late whenever a voltage write
was still running at t = 0 (up to about 60 µs, `MEASUREMENTS.md`). push1 (`firmware/0006`) moved the release to a
hardware alarm, but its first release of every boot still came 9 to 10 µs late, so "first shot of a boot" and "later
shot" were two different timings. push2 (`firmware/0009`) runs the release handler from SRAM and primes the alarm
before every shot.

Before a campaign, the lab now runs an on-time gate: three fresh boots, one harmless 450 mV standard shot each, and
every one must report `release_at=0 scheduled=0 via=alarm`. With push2 all three did. During the campaign the
runner refuses to score any shot whose release was not 0 to 4 µs after t = 0 by the alarm, or that had a write across
the release (**MISTIMED**, exit 14): poison and loss still show, "under" and goldilocks do not.

## 2. Park-normalized shots (`PARK_EACH=1`)

Two shots at the same setting on one boot are not two samples of one setting. On card E, with push2 releasing both on
time, 350 mV (3 x 80 µs, `t_dip` -60) as the first shot of a boot was under and the second shot lost the link. With
push1, 350 mV was under 4 of 4 times as a first shot and lost as a second shot. NVVDD traces had shown why: the
first shot of a boot starts from the GPU's own PWM-VID control, later ones from a rail held under PMBus, and the
later shots dipped about 30 to 40 mV deeper on the Pico's ADC.

`PARK_EACH=1` parks before every shot after the first: the rail goes back to stock under PMBus, `PEX_RST` is pulsed
and the rail is handed back to the GPU, so the next shot starts like the first shot of a boot. With a park in between,
the same two 350 mV shots were both under. At 331 mV, 8 park-normalized shots per boot in 4 cells gave 32 of 32 under
with no loss that depended on the shot's position. One fresh boot can now carry many comparable shots instead of one.
The planner counts post-park shots as first shots (`equivalent_states` in its family files; remove it to turn that
off).

## 3. Slow slew and a lower restore against PCIe link loss

Below some depth the GPU drops off the bus. On card E the park after such a shot found config space reading
`0xffffffff` while the root port still showed the link up (2.5 GT/s x8, correctable error flagged): the GPU's side of
link training failed, not the host. Which depth that happens at depends on how the rail recovers after the dip.
Standard short train (3 x 80 µs, `t_dip` -60, delay 117), one shot per fresh boot:

| Recovery | Last depth under | First depth lost |
|---|---|---|
| fast profile (`fast` 1), restore 750 mV | 338 mV | 331 mV |
| stock slew (`fast` 0), restore 750 mV | 331 mV (2 of 2) | 325 mV |
| fast profile, restore 625 mV | 331 mV | 325 mV |
| fast profile, restore 825 mV | | 338 mV (one shot) |

A gentler recovery (stock slew, or a smaller step back up) bought one VID step. Below about 325 mV nothing helped
(section 7). The lab's reading: a fast, large recovery step disturbs the GPU's PCIe training. The planner's default
family for this card uses the stock slew (`e-slow-push2`); `e-vr625-push2` uses restore 625 mV.

The direction is card-specific. On another card, with the earlier firmware, 306 mV kept the link with a restore of 800
to 825 mV and lost it at 775 mV. Try the restore in both directions at a card's edge.

## 4. Restore 588 mV against the controller latch

The MP2888B can switch its output off (STATUS_WORD `0x2042`, READ_VOUT about 41 mV) until a power cycle. On card E
the standard 3 x 198 µs train at 381 mV latched it every time with restore 750 mV, with the fast profile and with the
stock slew; 3 x 220 µs at 388 mV latched too. Shorter pulses (80 to 120 µs) did not.

With restore 588 mV, below the 600 mV level the rail holds while the card is in reset, the same 3 x 198 µs train at
381 mV came back under twice with no latch, and the standard train then ran at every VID step from 375 down to
331 mV (4 park-normalized shots each, 32 of 32 under, no latch, no loss). It is not a cure: one first shot of a boot
at 331 mV with restore 588 mV and delay 200 µs still latched. The lab's reading: the latch follows the energy of the
dip and the size of the step back up, and a restore below the reset level makes that step smaller.

## 5. Delay is a knob, and only some delays are legal

The lab had run every shot at the firmware's default `delay` of 117 µs. In the firmware `t_dip` and `delay` only ever
appear as `t_dip - delay`, the time of the first dip command: delay 330 µs at `t_dip` -60 is the same waveform as
delay 117 µs at `t_dip` -273. Sweep one of them, and pool shots by the difference (the planner's ledger does).

**The 47 µs rule.** One PMBus write takes about 47 µs and writes queue behind each other. A write that is still in
flight at t = 0 delays the release (old firmware) or makes push2 refuse the shot (`glitch ABORT: PMBus write ...
across the PEX_RST release`). For a plain train the dip commands go out at `t_dip - delay + k (pw + gap)` and each
restore `pw` later; a write starting at t is illegal when `t <= 0 < t + 47`, counting the queueing of writes closer
than 47 µs. So only some delays are legal for a given train. At `t_dip` -60 with gap 150 µs (from the planner's
model of the push2 timeline):

| Train | Illegal delays (within 117 to 460 µs) |
|---|---|
| 3 x 80 µs | 170 to 216, 250 to 296, 400 to 446 |
| 3 x 120 µs | 210 to 256, 330 to 376 |
| 3 x 198 µs | 138 to 184, 288 to 334 |

A longer gap moves the holes: the same check decides.

The planner computes this per shot (`tools/planner/planner_auto/schedule.py`) and skips illegal delays. On card E,
delays from 117 to 460 µs at 331 to 338 mV, with short trains and with the standard train at restore 588 mV, all came
back under, apart from rare fuse-unit errors (section 7).

## 6. CRC as a compass (a community member's method)

A community member shared a per-card search that reads CRC_STATUS (`0x820070`) after every shot as a compass. On a
healthy read DSOV is 1 and CRC is `0x55`. After a shot:

| CRC_STATUS | Meaning |
|---|---|
| `0x77` | closed: the shot did not reach the fuse read |
| `0x75`, `0x57` | adjacent: the shot is next to the window |
| `0x00` | over: the whole block sensed as zero |

The search, per card: find the loss island (the depth where the link dies, with repeats), go one VID code shallower,
sweep delay there, densify wherever CRC moves, bisect delay to 2 µs where outcomes flip, then tune the pulse width,
and track drift (the window moved a few µs over a session, so repeat the best settings in rounds a few minutes apart).
Their reported hits sat at about 297 to 341 mV commanded, delays of 330 to 360 µs and widths of 198 to 220 µs; thousands
of shots at delays of 90 to 160 µs never opened one card. They arm a burn only when DSOV 0, CRC `0x55`, the privilege
mask all ones, row 0 intact and the target row blank show in the same read.

Card H later showed what the codes are: CRC_STATUS is four 2-bit fields, one per fuse block, `01` = pass. `0x57` is
block 0 failed, `0x75` block 2 failed, `0x77` both, and codes with a `00` or `10` field (`0x74`, `0x76`, `0x67`)
appeared near a threshold. On card H no shot after the release ever moved DSOV, whatever the code
(`docs/SWEET_SPOT.md`).

What it changed here: before 2026-11-20 the lab's classifier scored any DSOV 1 as under without looking at CRC, so
earlier campaigns could not see a CRC intermediate at all. The push2 runner calls a shot "under" only with DSOV 1,
CRC `0x55` and a full privilege mask, and reports anything else as read; the planner's ledger classes `0x77` as
closed and `0x75`, `0x57` and values between `0x55` and `0x77` as intermediate, and its default method
(`compass`) is this search.

## 7. What card E showed

- **A brown-out floor at about 325 mV commanded.** The GPU dropped off the bus at 325 mV and below in every form
  tried: short and standard trains, stock slew and every fast-profile subset, restores of 588 to 750 mV, delays up to
  460 µs, and a single 80 µs dip placed entirely before the release, while the GPU was still held in reset.
- **At 331 mV and above, everything tested came back under** (with the gentler recoveries of section 3): delays 117
  to 460 µs, widths 50 to 198 µs, the standard 3 x 198 µs train at restore 588 mV, early and late windows (`t_dip`
  -60 to +730 µs), park-normalized repeats. No CRC intermediate, no DSOV flip, no partial state.
- **Rare fuse-unit errors.** FUSECTRL read `0xBAD00100` on a few shots at large delays (300 µs at 338 mV, 360 µs at
  388 mV, 400 µs at 331 mV, the last one still there 3 s later), about 1 to 2 % of such shots; exact repeats
  (8 of 8 and 36 of 36) were under. `BAD0_FOLLOWUP=1` logs a second read 3 s later without changing the outcome.

The lab's reading: on this card the fuse read cannot be disturbed by a hot-reset PMBus glitch before the GPU itself
browns out, so the fuse threshold sits below the floor. The open path for such a card is the original cold power-on
method: the controller's PWM-VID input is driven from the glitch board (the link from the GPU's PWM-VID output is
removed) and the glitch is timed off the NVVDD power-up ramp, instead of over PMBus across a `PEX_RST` of a running
card. Card E is unsolved.

## 8. The search order that worked best for us

1. Verify on-time release first (section 1). Do not compare timings from firmware that slips.
2. One scored shot per fresh boot, or park-normalized shots (`PARK_EACH=1`); never compare shot 0 with shot 1.
3. Find the loss island with a depth ladder at the default delay, with repeats at the edge.
4. At the edge, try the gentler recoveries: stock slew (`fast` 0), restore 625 mV, and restore 588 mV for the
   standard train.
5. At the shallowest depth that stays under, sweep delay over its legal values (about 120 to 460 µs).
6. Then pulse width, within the latch limits the card has shown.
7. Wherever CRC moves, densify and run a drift loop with repeats.
8. Read FUSECTRL, DSOV, CRC and the privilege mask on every shot, and keep a ledger of all of it.

## 9. The hunter loop

`tools/planner/` turns this into a loop. `plan_run.py` reads the ledger and proposes the next shots for a card with
the never-rules applied (floor, runner limits, the 47 µs rule, learned latch energy); an operator runs the printed
launcher command and `ingest.py` adds the log to the ledger. `hunt.py --execute` repeats plan, launch, ingest within
a budget of boots and hours and stops on a held goldilocks, a shot that did not run, identity, checksum or preflight
failures, a repeated controller latch, repeated park failures, repeated mistimed shots, three temperature gate
failures, or anything it does not recognise. It is a dry run unless asked otherwise, and it runs nothing but the
launchers in `tools/runner/`. See `tools/planner/README.md`.
