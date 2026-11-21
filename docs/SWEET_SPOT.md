# Finding the sweet spot: how the lab finds the goldilocks frame

On 2026-11-20 the lab finished two more 8 GB cards, card G and card H on the
[cmp.wiki results page](https://cmp.wiki/wiki/Voltage-glitch_unlock:_lab_results_and_fuse_findings). Card G hit with
the community recipe. On card H the same recipe did not transfer, and the hit came from a map of where the glitch
lands in the GPU's boot. This page turns both into one strategy, in order, with the commands of
`tools/hunter/goldilocks_hunt.py`, which plans each phase, runs it through `tools/runner/bootrun.sh` if you ask it to,
and reads the logs back into a map.

The tool never senses, arms or burns fuses. When the runner holds a goldilocks frame the tool stops and tells you so;
the active sense, the write-window check and any burn are yours, with your own reviewed tools.

## Words used here

| Word | Meaning |
|---|---|
| goldilocks | DSOV (`0x820084`) 0, CRC_STATUS (`0x820070`) `0x55`, privilege mask (`0x8200FC`) `0xFFFFFFFF`, FUSECTRL idle, read after a shot: the target state |
| release, t = 0 | the moment `PEX_RST` is released and the GPU leaves reset |
| s1 | when the first dip write starts, relative to the release: `td - delay` in the runner's step. A PMBus write takes about 47 µs, so s1 = -47 is a write that ends exactly at the release |
| fast 1, fast 0 | the fast slew profile, or the card's stock slew (a slower fall of the rail) |
| code | a VID code of the voltage controller, 6.25 mV apart (375 mV is code 60, 381 mV code 61) |
| cell | one setting (depth, s1, pulses, width, restore, slew), shot several times |
| under | DSOV 1, CRC `0x55`, full mask: nothing happened that the reads can see |
| stall | GFW boot progress (`0x118234`) has not reached `0xFF` a few seconds after the shot; the runner then reads FUSECTRL, DSOV, CRC, the mask and the sentinel once, read-only |
| compass | a finished boot with DSOV 1, a full mask and a CRC other than `0x55` |
| partial | DSOV 0 that is not goldilocks |
| over | DSOV 0 with CRC `0x00` or a mask that is not all ones (`0xFFFFFEFE`): too deep, never sense it |
| poison | FUSECTRL or DSOV reads `0xBADFxxxx` (or `0xBAD0xxxx`): the fuse unit is gone until a power cycle |
| latch | the controller switched its output off (STATUS_WORD `0x2042`): power cycle |
| sentinel | the fuse alias `0x820410` (`0x1` on a healthy boot of the lab's cards) |

CRC_STATUS is four 2-bit fields, one per fuse block, and `01` means the block passed. `0x57` is block 0 failed
(field 0 = `11`), `0x75` is block 2 failed, `0x77` is both. Codes with a field of `00` or `10` (`0x74`, `0x76`,
`0x67`, `0x65`) are partial codes; on card H they showed up near a threshold. Card H only ever moved fields 0 and 2.

## The strategy, in order

1. **Baseline.** Identity, VRM profile, a cold snapshot, and every plan written before the first glitch (the method
   page on cmp.wiki covers this; `tools/vfi_preflight.py`, `tools/vfi_snap.py`).
2. **The community train first.** 3 x 198 µs, gap 150, `td` -60, delay 117 (s1 = -177: pulse 1 spans the release),
   restore 750 mV, fast 1, from shallow to deep, no parks between shots.
   `goldilocks_hunt.py train`
   If a DSOV-0 stall shows (card G: CRC `0x77`, sentinel 0, full mask), dwell there: card G hit two shots later.
3. **A non-monotonic map means: map the fuse read.** If a deeper code reads under between a poison code and a lost
   code, stop dwelling. One fast dip, s1 from +40 to +160 in 5 µs steps, at the poison-edge code, 3 or 4 shots each.
   Expect block codes. `goldilocks_hunt.py readmap --v 375`
4. **The init phase.** One dip whose write ends at the release (s1 = -47, the latest legal start) and earlier, down
   to about -100, shallow to deep. Look for stalls with sentinel 0 and CRC `0x55` (the init was disturbed), then the
   poison edge, 1 to 3 µs wide. `goldilocks_hunt.py init`
5. **The slow slew.** At the code where s1 near -47 reads under and a few µs earlier is poison (card H: 375 mV),
   switch to the stock slew (fast 0) and step s1 from -47 to -100: the rail is still falling at the release, which
   lands between the codes. `goldilocks_hunt.py slow --v 375`
6. **Hold.** Hold any goldilocks frame for the active sense and the window check. Only a cold boot verifies a burn.

After every phase, `goldilocks_hunt.py tally <logs>` prints the map (counts per cell, the register codes, the edges
and the partial codes) and a "Next" block with the command for the next phase. The first shot of every boot is left
out of the counts (below).

**Shot budget.** Each poison or latch costs a power cycle (about 1.5 minutes on the lab's bench). Edges are 1 µs
sharp, so use 1 or 2 µs steps near an edge, 3 or 4 shots per cell, and never count the first shot of a boot.

## Worked example 1: card G, the community recipe

| | |
|---|---|
| Hit | 369 mV, 3 x 198 µs pulses, gap 150, `td` -60, delay 117 (pulse 1 from -177 to +21 µs), restore 750 mV, fast 1 |
| How it was shot | no parks between shots, shooting through stalls, continuing through partials, 10 warm-up shots; about 100 shots at that setting over the day |
| The hit boot | shot 1 at 369 mV: a GFW stall (GFW `0x00`) with DSOV 0, CRC `0x77`, mask `0xFFFFFFFF`, sentinel 0. Shot 2: goldilocks (DSOV 0, CRC `0x55`, mask `0xFFFFFFFF`) |
| Earlier that day | a single slow dip (350 mV, `td` 30, delay 124, 36 µs, restore 825, fast 0: dip write -94 to -47, restore write -47 to 0) gave DSOV 0, CRC `0x00`, mask `0xFFFFFEFE`, sentinel 0: over, not a lead-up |
| Cold-boot verify | GPC_DISABLE `0x13` to `0x00`, SM 70 to 122 (126 with the RECONFIG override, its ceiling; one defective TPC stays off), FBP `0x852` to `0x840`, FBIO/ROP_L2/FBPA `0xC03000` (80 GB layout), MAGIC_D `0x14680000`, BOOT_GEN3_DIS 0, DISABLE_GEN3_SPEED 0, LnkCap2 2.5 to 16 GT/s; 15 records |
| In a server | with a driver that takes each card's own geometry: 81920 MiB, 126 SMs in CUDA, PCIe Gen2 x16, no Xid, mining at 100 % |

**The sentinel trap.** Earlier the same day a real goldilocks frame on card G was thrown away by the lab's runner,
because it treated the sentinel alias `0x820410` reading 0 as hidden poison. In a real goldilocks the option shadows
have not been sensed, so that alias reads 0. The rule now, in `tools/runner/bisect_shots.py`: sentinel 0 counts as
poison only when DSOV is 1, and never in a stall read.

The hunter's version of card G's run: `goldilocks_hunt.py dwell --v 369 --s1 -177 --np 3 --reps 10 --boots 10
--continue-partials --shoot-through` (the last option needs the owner's OK, see below).

## Worked example 2: card H, where the recipe did not transfer

Same bench, same firmware (push2), no parks between shots, a 100 ms `PEX_RST` pulse after a lost shot.

**1. The train map was non-monotonic.** The community train, shallow to deep:

| Depth | Outcome |
|---|---|
| 388 mV | lost |
| 381 mV | GFW stalls, CRC `0x75` |
| 375 mV | poison, FUSECTRL `0xBADF510E` |
| 369 mV | under |
| 362 mV | lost |

Pulse widths of 192 to 228 µs and delays of 105 to 137 µs did not move it. With one pulse (only the one that spans
the release) 381 and 375 mV poisoned; the later pulses turned the poison into stalls.

**2. The first shot of every boot is benign.** Before it the controller is still in its stock mode (PWM-VID, about
694 mV); after it the rail stays under PMBus control. Count a cell from the second shot, or give each boot one
warm-up shot. The hunter does the first by default (the first step of every boot gets one extra shot, and the tally
leaves out the first shot of every run) and the second with `--warmup '<step>'`.

**3. Map the fuse read first.** One fast dip, 198 µs, 375 mV, its write starting s1 after the release:

| s1 | Outcome |
|---|---|
| +51 µs and earlier | under |
| +54 to +109 µs | GFW `0x01` stall, CRC `0x57` (block 0 failed) |
| +110 to +137 µs | stall, CRC `0x75` (block 2 failed); some boots completed with `0x65` |
| +138 µs and later | under |

The boundaries are 1 µs sharp (4 of 4 on each side). Only the falling edge matters: a short 50 to 110 µs dip at
s1 +60 still gives only `0x57`.

**4. Depth works as a fine timing knob.** At s1 +100:

| Depth | Outcome |
|---|---|
| 450, 425, 400 mV | under |
| 388 mV | block 2 (`0x75`) |
| 381 mV | mostly block 2, some both (`0x77`) |
| 375 mV | block 0 (`0x57`) |

Near the threshold the partial codes appear: `0x76`, `0x77`, `0x74` (block 0 reads `00`), `0x67`. No post-release
shot ever moved DSOV: more than 75 partial frames, DSOV 1 every time.

**5. DSOV is decided in the init phase**, about the first 50 µs after the release, not by the block reads. One fast
dip with its write ending at the release (s1 -47) and earlier:

| Depth | s1 | Outcome |
|---|---|---|
| 450, 425, 400 mV | every s1 | under |
| 388 mV | | block 0 stalls |
| 381 mV | -47 to -59 µs | GFW `0x00` stalls with sentinel 0, CRC `0x55`, mask `0xFFFFFFFF`, DSOV 1 (option shadows unsensed, CRC clean) |
| 381 mV | -60 µs and earlier | poison (a 1 µs edge) |
| 375 mV | -47 to -57 µs | under (clean) |
| 375 mV | -58 µs and earlier | poison |

**6. The lab's reading of the map.** A dip deep enough while the GPU leaves reset makes it restart its init cleanly
(likely its own brown-out reset), so the shot looks under. Just shallower than that, the init is corrupted: sentinel
0, then poison. The goldilocks is the deepest disturbance that does not trigger the clean restart, right next to the
poison, and that edge moves with timing and slew, so timing and slew are finer knobs than the 6.25 mV VID step.

**7. The hit.** One dip, 375 mV, the stock (slow) slew, fast 0, 198 µs, restore 750 mV, `td` 62 with delay 117
(s1 -55: the dip write runs from -55 to -8 µs and the rail is still falling at the release). It was the first counted
shot of a fresh boot. The lead-up, at the slow slew and 381 mV: sentinel-0 stalls for s1 -55 to -90, poison at -60
and -80 in some boots.

**8. Verified.** 16 records (compute, memory, PCIe). Cold-boot verify: GPC_DISABLE `0xA8` to `0x00`, PES 0, TPC
disables are the defect masks only, SM 70 to 118 (122 with the RECONFIG override, its non-defective ceiling), FBP
`0x2D` to `0x24`, FBIO/ROP_L2/FBPA `0xC30` (80 GB layout, 10 of 12 FBPs), MAGIC_D `0x14680000`, BOOT_GEN3_DIS 0,
DISABLE_GEN3_SPEED 0, LnkCap2 2.5 to 16 GT/s.

The hunter's commands for the same path: `train` (step 1), `readmap --v 375` (3), `init` (5), `slow --v 375` (7).
Fed card H's logs phase by phase, `tally` names exactly these next steps.

## Runner settings behind both cards

`tools/runner/bisect_shots.py` has the lab's runner behaviour of 2026-11-20. The hunter's plans set the safe ones and
leave the others to you:

| Setting | Hunter default | What it does |
|---|---|---|
| no parks between shots (`PARK_EACH` 0) | on | parking's hand-back left BAR0 dead on the lab's bench |
| `LOST_NOPARK=1` | on | after a lost shot: one 100 ms `PEX_RST` pulse (`r`), then the card must read healthy and locked (FUSECTRL idle, DSOV 1, CRC `0x55`, full mask, sentinel), or the boot ends |
| stall read | always | a GFW stall is polled for `GFW_WAIT` seconds, then FUSECTRL is read first (poison ends the boot), then DSOV, CRC, the mask and the sentinel, read-only, and logged; a stall with DSOV 0 is held (exit 15), a goldilocks signature in a stall always |
| sentinel rule | always | sentinel 0 is poison only with DSOV 1 outside a stall |
| compass frames | always | logged as compass, not as lost, and the cell goes on |
| `PARTIAL_CONTINUE=1` | off (`--continue-partials`) | keep shooting through readable DSOV-0 frames; card G's recipe |
| `SHOOT_THROUGH_STALLS=1` | off (`--shoot-through`) | fire the next shot while GFW is stalled, `STALLRUN` stalls in a row at most |
| poison, latch | always | end the boot: power cycle |

**Shoot-through breaks a rule.** Upstream pico-ga100vfi's AGENTS.md, rule 2: never `g`, `r`/`j` or FLR while GFW
boot status is `0x01`; it can wedge the GFW boot sequencer. Both cards' maps were taken with shoot-through on, and a
clean host power cycle recovered the card every time on the lab's bench. Without it every stall ends its boot, which
costs a power cycle per stall. Get the card owner's OK before you turn it on; `run` refuses such a plan without
`--owner-ok-shoot-through`.

## After a hold

- Leave the card as it is: no park, no further shot, no power cycle. `bootrun.sh` does not park a held card, and
  under the hunter (`NO_ON_HOLD=1`) it never runs an `ON_HOLD` hook.
- Run your own active sense and write-window check on the held state, then decide on a burn with your own reviewed
  tool and plan. Nothing here senses, arms or burns.
- One note for whoever burns: on card G the burn arm step (SBFE shadow `0x821108` to 0, WDATA_WR_SECURE `0x82048C` to
  0) only stuck after EN_SW_OVERRIDE (`0x820040`) = 1: write it, read it back as 1, then sense and arm. The runner's
  classifier no longer writes that register after every shot (firmware patch 0005).
- Only a cold boot and a new read verify a burn.

## Two more notes from card H

- **80 GB drivers.** Card H's FBP mask is `0x24`, not `0x840`. A driver patch that hard-codes the W1 FBP_DISABLE
  shadow `0x840`, CTRL_FBPA `0x0C0C` and CTRL_FBP `0x12` (as one public 80 GB patch does) would switch on defective FBPs
  on card H. The geometry has to come from each card's own masks: FBP n owns FBPA, FBIO and ROP_L2 bits 2n and 2n+1,
  and the card is 80 GiB only when exactly 2 of 12 FBPs are off and NUM_ACTIVE is 10.
- **The Pico's ADC trace means nothing without the optional NVVDD lead** (the input floats): do not tune from it.

## Using the hunter

```sh
H=tools/hunter/goldilocks_hunt.py
python3 $H strategy                               # the steps above and the stop rules, for a person or an agent
python3 $H train --out plan-train.json            # plan phase 2
python3 $H run plan-train.json --card g           # dry run: prints the boots it would run
python3 $H run plan-train.json --card g --execute --log-dir logs/g-train
python3 $H tally logs/g-train                     # the map and the next phase
python3 $H check '[[375,62,1,198,750,0,2,0,-20,0,117,150]]' -v     # any step against push2's release rule
```

Every step the hunter emits passes the push2 legality model (`tools/planner/planner_auto/schedule.py`, a port of the
firmware's timeline: 47 µs per PMBus write, writes queued, no write that starts at or before the release and is
still running at t = 0) and the runner's limits; illegal cells of a scan are left out and listed. `run` stops on
poison by default (`--on-poison continue` goes on in a fresh boot, as the plan says), on a latch, on any hold and on
anything it does not recognise, and it keeps every boot's log in `--log-dir`. `docs/AUTONOMOUS_AGENT.md` has the loop
for an LLM agent.
