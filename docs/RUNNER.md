# The per-boot runner and the boot launchers

`tools/runner/` holds the scripts the lab ran its glitch campaigns with, one boot at a time, cleaned of bench-specific
names: the card's address, the serial port, the hosts and the power control are settings now. The logic is the
lab's, except that the launcher's last step (handing a held goldilocks to a burn script) is replaced by an optional
hook. Nothing here burns fuses. The runner accepts push1, push2 or push3 firmware (markers `fw=lab-psr1-push1`,
`-push2` and `-push3`). The firmware patch series builds push3 (`firmware/0011`; `0012` changes only the host
package): use that.

| File | Runs on | What |
|---|---|---|
| `bisect_shots.py` | bench | The per-boot runner: fires a sequence of steps, reads every shot passively, stops at the first shot that is not "under" (or, in cell mode, counts outcomes per step) and holds a goldilocks |
| `park.py` | bench | Hold NVVDD at stock under PMBus, pulse `PEX_RST`, read config space, hand the rail back; keeps PMBus control when the card does not come back |
| `preflight.py` | bench | Read-only Pico and MP2888B check: firmware marker, rail under GPU control, the six VRM profile registers |
| `bootrun.sh` | controller | One run per boot: power cycle, fresh-boot guard, plan checksum, runner copy, host guard, preflight, card identity, temperature gate, runner, hold hook |
| `firstshot.sh` | controller | One fresh boot per step: every step is the first shot of its boot |
| `campaign.sh` | controller | Boot after boot along a ladder: after a tip at step N the next boot starts at step N+1 |
| `cycle.sh`, `power.sh` | controller | Power-cycle the bench host only; power helpers (Wake-on-LAN or IPMI) |
| `bench.conf.example` | controller | Settings and the per-card table; copy to `bench.conf` |

`tools/diag/` has two gentle checks at 700 mV: `timing_controls.py` (where the `PEX_RST` release lands with each kind
of shot) and `enswtest.py` (whether EN_SW_OVERRIDE survives `PEX_RST`; writes that register). `tools/planner/`
plans the steps and can drive the launchers in a loop (`tools/planner/README.md`). `tools/hunter/goldilocks_hunt.py`
plans the phases of `docs/SWEET_SPOT.md`, drives `bootrun.sh` through them and tallies the logs.

## On the bench

Prepare the host as in the main README (Secure Boot off, card on `pci-stub`, no NVIDIA driver, `pci=noaer`, memory
decode on), flash the patched firmware (push3) and install the patched host package in a venv:

```sh
git clone https://github.com/dalzyu/pico-ga100vfi && cd pico-ga100vfi && git checkout e1ebd97
git am /path/to/cmp170hx-8gb-vfi-tools/firmware/*.patch
python3 -m venv /opt/vfi/venv && /opt/vfi/venv/bin/pip install ./host
```

The runner can be used by hand:

```sh
export VFI_BDF=0000:05:00.0 VFI_PORT=/dev/ttyACM0
sudo -E /opt/vfi/venv/bin/python -I tools/runner/bisect_shots.py 'card X' \
  '[[450,-60,3,198,750,1,2],[381,-60,3,80,750,0,5],[375,-60,3,80,750,0,5]]'
```

A step is `[v_mV, td_us, pulses, pw_us, v_restore_mV, fast, reps, hold_ms, t_restore_us, revert, delay_us, gap_us,
t_revert_us]`. Omitted tail fields default to 3 pulses, 198 µs, 750 mV, `fast` 1, `REPEAT` shots, hold 0,
`t_restore` -20, `revert` 0, delay 117 µs, gap 150 µs and `t_revert_us` (tv) 0, which is the stock command. tv is
when the firmware writes the stock 787.5 mV after the train (0 to 20000 µs); overlap scans pin it
(`docs/OVERLAP_EDGE.md`). `revert` is 0 or 2 (one-VID dither); 1 is
refused because the no-dip check needs the rail under PMBus control after the shot. `fast` 2..7, `hold_ms` > 0,
`t_restore` <= -1000 and `revert` 2 are the knobs of `firmware/0008`. Every step is checked before the first shot
(`FLOOR` <= v <= 825, v_restore 575 to 825, delay 0 to 900, gap 36 to 2000, tv 0 to 20000, the knob ranges); a bad
step stops the run with a `STOP: step ...` line and exit 2. The firmware uses `td` and
`delay` only as `td - delay`; some delays put a write across the release and push2 refuses them before anything is
written (`docs/METHOD.md`, section 5). Order the steps from the weakest effect to the strongest.

- **Single mode** (every step 1 shot): the run stops at the first shot that is not "under".
- **Cell mode** (`REPEAT` > 1 or a `reps` field > 1): each step is a cell of shots. A lost shot is parked and the run
  goes on; `LOSTRUN` lost shots in a row stop the run, `CELLLOST` lost shots in a row skip the rest of a cell, and
  `SHOTCAP` caps the shots per run. A compass frame (DSOV 1, full mask, CRC other than `0x55`) is logged and the
  cell goes on. A partial (DSOV 0 that is not goldilocks) is held: the run stops with exit 13 and nothing more is
  read or fired, unless `PARTIAL_CONTINUE=1`. Every cell ends with a count line:
  `cell 3 v=369 td=-60 np=3 pw=80 vr=750: {'under': 5, 'lost': 0, 'partial': 0, 'other': 0}` (plus `compass` and
  `stall` when there were any).
- **No parks, reset pulse after a loss** (`LOST_NOPARK=1`, cell mode): no park after a lost shot. The runner sends one
  100 ms `PEX_RST` pulse (the firmware's `r`, which must answer `released`), waits for the link and reads the card
  passively; it has to read healthy and locked (FUSECTRL idle, DSOV 1, CRC `0x55`, full mask, sentinel), or the boot
  ends (exit 10). This is how the lab shot cards G and H: on its bench the park's hand-back left BAR0 dead.
- **Reset pulse after a poison** (`POISON_RESET=1`, cell mode, off by default): after a `0xBADF` or `0xBAD0` read at
  FUSECTRL the runner first polls GFW for up to `GFW_WAIT`, reads only, because rule 2 of upstream pico-ga100vfi's
  AGENTS.md forbids `r` while the GFW boot status is `0x01`. Once GFW is ready it sends one 100 ms `PEX_RST` pulse at
  the stock rail and needs the same healthy locked read as `LOST_NOPARK` (FUSECTRL idle `0xE0040000`, GFW ready,
  DSOV 1, CRC `0x55`, full mask, sentinel). Then the boot goes on and the log says `poison reset (100 ms PEX_RST):
  healthy_locked`. Anything else, or a GFW that never finishes, ends the boot (exit 10) as before; when GFW never
  finishes the boot ends without a park, since the park pulses `PEX_RST` too. Hidden poison and the
  readable `poisoned/over0` frame are never reset. On the lab's card C a `0xBADF510E` poison was sticky (GFW finished
  booting in under a second, FUSECTRL stayed poisoned for 15 s) and one pulse returned a full cold-boot match: 2 of 2
  in standalone tests, then 64 of 64 in one 129-shot boot, about 6 s each instead of a power cycle. A glitch shot's
  own 1 ms reset did not clear it. A goldilocks that comes later in a boot with resets is held as usual, after a
  `note: this boot had N poison reset(s) before this hold` line; the burn tool's own active sense, write-window check
  and readback still decide. The lab chose to burn on such holds; card C's burned shot came in a boot that had no
  reset before it.
- **Through partials** (`PARTIAL_CONTINUE=1`, cell mode): a readable DSOV-0 frame that is not goldilocks is logged
  and shooting goes on. Card G's goldilocks came two shots after such a frame. Nothing is sensed or written.
- **Through stalls** (`SHOOT_THROUGH_STALLS=1`, cell mode, off by default): the next shot fires while GFW is still
  stalled, and `STALLRUN` stalls in a row (default 8) end the boot. This breaks rule 2 of upstream pico-ga100vfi's
  AGENTS.md (never `g`, `r`/`j` or FLR while GFW boot status is `0x01`): it can wedge the GFW boot sequencer. A clean
  host power cycle recovered the card every time on the lab's bench. The runner prints a warning; ask the card's
  owner first. Without it, a stall ends the boot without a park.
- **Park-normalized** (`PARK_EACH=1`): a park before every shot after the first, so each shot starts like the first
  shot of a boot and many comparable shots fit in one boot (`docs/METHOD.md`, section 2).

Once per boot, before the first shot: the firmware marker (push1, push2 or push3, or stop), `u` with its readback (exactly
`unlock MATCH wp=00 cfg=1229`, or stop), a read of controller registers `0x13 0x46 0x7E 0x79 0x88 0x8B 0x8D`, the
controller's health (STATUS_WORD OFF bit clear, READ_VOUT 500 to 850 mV) and a passive BAR0 baseline, which must read
healthy. The `SENTINEL` alias register's baseline value is kept for the boot; `SENTINEL_EXPECT` pins it.

After every shot the runner checks, in order:

1. `glitch ABORT` (push2 refused the shot before writing anything): stop, exit 2. A reply that is cut off: stop,
   exit 11 (the shot's state is unknown).
2. The reply shows `pwd=1` and `e1=0xc2fa`, or **NO DIP** (exit 12): a locked controller drops the dip writes, so the
   shot did not run. `writes_failed` must be 0, or exit 11.
3. The release: exactly one `release_at=` line, 0 to 4 µs after t = 0, `via=alarm`, and no overlap WARN, or the shot
   is **MISTIMED**: it is still read, but never scored as under or goldilocks, and the boot stops with exit 14.
4. The controller: STATUS_WORD bit 6 (OFF) or READ_VOUT below 500 mV is **CONTROLLER OFF** (exit 9).
5. The link (`RESTORE_WAIT`, default 8 s). No link is **lost**.
6. The passive read, in this order, with no BAR0 writes: FUSECTRL (must be exactly the idle `0xE0040000`), GFW (low
   byte `0xFF`), DSOV (0 or 1), CRC, privilege mask. A `0xBADFxxxx` or `0xBAD0xxxx` value is **poisoned** and nothing
   else is read. With `BAD0_FOLLOWUP=1` a `0xBAD0` FUSECTRL gets one more FUSECTRL read 3 s later, logged only.
7. A GFW that is still booting is polled for `GFW_WAIT` seconds (default 5). If it is still booting, that is a
   **stall**, and the runner reads once more, read-only: FUSECTRL first (a `0xBADF` or `0xBAD0` value is poisoned and
   ends the boot; a busy FUSECTRL ends it too), then GFW, DSOV, CRC, the mask and the sentinel, logged as
   `stall read (...)`. A stall with DSOV 0 is held (`>>> STALL DSOV0 HELD`, exit 15), whatever else it shows: a
   goldilocks signature in a stall is not a finished boot. A mistimed shot that shows DSOV 0 in a stall is MISTIMED
   instead (exit 14, no park), never a held goldilocks. The one exception is a DSOV-0 stall that is not a
   goldilocks signature with `PARTIAL_CONTINUE=1` and `SHOOT_THROUGH_STALLS=1`, which is logged as `STALL PARTIAL`.
8. The `SENTINEL` alias register (default `0x820410`). With DSOV 1 it must equal the boot's baseline; `0`, all-ones,
   `0xBAD.....` or any other value is **hidden-poison**. With DSOV 0 the option shadows have not been sensed and the
   alias reads 0 in a real goldilocks (one real goldilocks frame on card G was thrown away before this rule), so then
   only all-ones or a `0xBAD.....` value is poison. A stall read logs the sentinel and never scores it. With
   `SENT0_CONTINUE=1` a DSOV-1 frame whose sentinel reads exactly 0 after a shot is logged as a `SENT0 frame` and the
   boot goes on; that never applies to the pre-shot baseline or to the read after a recovery or poison reset.
9. The verdict: DSOV 1 with CRC `0x55` and a full mask is **under**; DSOV 0 with CRC `0x55`, a full mask and a clean
   sentinel is **GOLDILOCKS**; CRC 0 with mask 0 is **poisoned/over0**; anything else is printed as read
   (`dsov1 crc=0x77 priv=..`, `dsov0 crc=0x55 priv=0xfffffefe`, ...), so a CRC change after a shot shows.

Every fired shot also gets one line for `tools/hunter/goldilocks_hunt.py tally`:

```
SHOT n=1 step=0 v=381 td=70 np=1 pw=198 vr=750 fast=1 delay=117 gap=150 s1=-47 first=0 after_park=0 stall=1 kind=stall FUSECTRL=0xe0040000 GFW=0x00000000 DSOV=0x00000001 CRC=0x00000055 PLM=0xffffffff SENTINEL=0x00000000 | booting GFW=0x00000000 (GFW stall)
```

`n` counts the shots of this run, `s1` is `td - delay`, `kind` is one of under, compass, stall, partial, goldilocks,
lost, poison, latch, mistimed, nodip, unknown or other, and `first=1` marks shot 0 of the run. That shot starts from
the GPU's own rail control (PWM-VID, about 694 mV on card H) where every later shot starts from a rail held under
PMBus, and it behaved differently on the lab's cards; the tally does not count it.

On GOLDILOCKS the runner prints `>>> GOLDILOCKS HELD (<tag>) v=.. td=..` and exits 0 without parking: the rail stays
under PMBus control and the state stays readable. It is a passive candidate: validate it actively before anything else.
A finished sequence and a tipped run end with a park; when the park after the last step fails, the run ends with
`PARK FAILED after the last step` and exit 7, not `sequence exhausted`. A held goldilocks, partial or stall, a GFW that is
still booting (also after the last shot of a sequence), a mistimed goldilocks (left for review) and the stops for
controller off, no dip, a cut-off reply and a refused shot do not park: power-cycle. Poison and a controller latch
always end the boot. After a failed park the root port's link and error lines are logged from a read-only `lspci -vv`.

Exit codes: 0 sequence done or goldilocks held, 2 usage, firmware, unlock readback, a step out of range or a refused
step, 7 park failed (before a step, or after the last one), 9 controller off or unsafe baseline, 10 tipped, 11 nack or
failed writes, 12 no dip, 13 partial held, 14 mistimed, 15 DSOV 0 held during a GFW stall.

Environment: `VFI_BDF` (required), `VFI_PORT`, `VFI_PARK`, `VFI_TRACE_DIR`, `FLOOR` (lowest dip, default 381 mV),
`REPEAT`, `SHOTCAP`, `LOSTRUN`, `CELLLOST`, `RESTORE_WAIT`, `GFW_WAIT` (default 5 s), `SENTINEL` (0 turns it off),
`SENTINEL_EXPECT`, `PARK_EACH=1`, `LOST_NOPARK=1`, `POISON_RESET=1`, `PARTIAL_CONTINUE=1`, `SENT0_CONTINUE=1`,
`SHOOT_THROUGH_STALLS=1` with `STALLRUN`,
`TRACE=1` (NVVDD traces, debug firmware, NVVDD lead on), `BAD0_FOLLOWUP=1`. `RETRAIN` and `SBR`
(link retrain, secondary bus reset) are refused. The runner never writes BAR0, so `CLASSIFY_WRITE` only matters to
tools that call the patched `classify()`. Three different `FLOOR` defaults: the runner alone 381 mV, `bootrun.sh`
338 mV, and the hunter's `--floor` 338 mV (written into every plan). By hand, set `FLOOR` for the recipe.

## On the controller: `bootrun.sh`

`bootrun.sh` gives every run a fresh boot, so nothing (a sticky poison, a latched controller, a stale override)
carries from one run into the next. Copy `bench.conf.example` to `bench.conf`, fill it in and add one `card_<name>`
function per card, then:

```sh
FLOOR=331 TMIN=360 TCAP=300 PARK_EACH=1 REPEAT=1 LOSTRUN=1 CELLLOST=1 BAD0_FOLLOWUP=1 RUNTO=1200 \
  tools/runner/bootrun.sh example '[[450,-60,3,80,750,0,2],[338,-60,3,80,750,0,4,0,-20,0,330]]'
```

1. **Power cycle** (`POWER=wol` or `ipmi`): clean shutdown, wait until the host is really off (ping, or the BMC's
   power status, with a hard power-off if a clean shutdown stalls), wait `OFFWAIT`, power on, wait for ssh. A host
   that does not go down stops the run: it needs a hand on the power button. `NOCYCLE=1` skips this.
2. **Fresh-boot guard**: the host must have been up for less than `FRESH_MAX` seconds (default 900). The bench
   kernel's boot id goes into the log, so two runs can be told apart as two boots.
3. **Plan checksum** (only when the card names `PLAN_FILES`): every file must hash on both sides, and the hash lines
   (with the file names) must match. A file missing on both sides is a failure, not a match (exit 4).
4. **Copy** the runner, `park.py`, `preflight.py`, `vfi_snap.py` and `vfi_common.py` to `BENCH_DIR`
   (`RUNNER TRANSFER FAILED`, exit 8). `RUNNER=<file>` copies another runner.
5. **Host guard**: optional kernel command line word (`GLITCH_CMDLINE`), no `nvidia` module, no kernel lockdown,
   exactly one `10de:20c2` card (or `CARD_BDF`) bound to `pci-stub` or nothing, the Pico's port present, memory
   decode on, GFW boot progress not `0x01`. `MASK_PCIEHP=1` masks pciehp interrupts on a hotplug-capable root port.
6. **Preflight** (`preflight.py`) must print `PREFLIGHT OK`.
7. **Identity**: `vfi_snap.py` must print the card's `GPC dis/def/st=<GPC>/` and `SMC` line from `bench.conf`. A
   different card stops the run.
8. **Temperature gate**: wait until READ_TEMP1 (raw) is inside `[TMIN, TSTART]` (`TSTART` is optional, no upper bound
   when unset), for up to `TCAP` seconds; if it does not, no shot
   is fired (`TEMP GATE FAILED`, exit 16). The check uses the reading that passed: a second read after the loop could
   bounce one ADC step out of the band and fail the gate for nothing (fixed 2026-11-21).
9. **Run** `bisect_shots.py` with a time limit (`RUNTO`) and the runner settings from the environment (`REPEAT`,
   `SHOTCAP`, `LOSTRUN`, `RESTORE_WAIT`, `CELLLOST`, `CLASSIFY_WRITE`, `SENTINEL`, `BAD0_FOLLOWUP`, `GFW_WAIT`,
   `LOST_NOPARK`, `POISON_RESET`, `PARTIAL_CONTINUE`, `SHOOT_THROUGH_STALLS`, `STALLRUN`, `SENT0_CONTINUE`, `FLOOR`,
   `TRACE`, `PARK_EACH`); the card's
   `SENTINEL_EXPECT` comes from `bench.conf`. The remote half exits with the runner's status and the launcher passes
   it on.
10. **Hold**: when a line starts with `>>> GOLDILOCKS HELD` and the runner exited 0, `bootrun.sh` runs `ON_HOLD` if it
    is set (with `CARD`, `TAG`, `BENCH_SSH`, `BENCH_DIR` and `RUN_LOG` in its environment) and exits with its status;
    otherwise it stops and reports the held state. A hold line with any other runner status does not run the hook,
    a stall hold (`>>> STALL DSOV0 HELD`, exit 15) never does, and `NO_ON_HOLD=1` turns the hook off for one run
    whatever `bench.conf` says (`tools/hunter/goldilocks_hunt.py run` always sets it).

The run log goes to `BOOTRUN_LOG` or `LOG_DIR/bootrun-<card>.last`; the last line is
`RESULT: ... (runner rc N)` or the hold report. Exit codes are listed at the top of the script.

## `firstshot.sh` and `campaign.sh`

```sh
tools/runner/firstshot.sh example '[[344,-60,3,80],[338,-60,3,80],[331,-60,3,80,625]]'
tools/runner/campaign.sh example ladder.json 8
```

`firstshot.sh` runs one fresh boot per step, so every step is the first shot of its boot; short steps are padded with
the runner's defaults and `reps` is forced to 1. A tipped boot goes on to the next step (also when the park after a
lost shot failed: the next step starts from a power cycle). `campaign.sh` runs boot after boot along a ladder file;
when a boot tips at step N, the next boot starts at step N+1; use it only for a ladder whose shots do not depend on
their position in the boot, or with `PARK_EACH=1`. Both stop on a held goldilocks or partial, on any stop line
(controller off, no dip, mistimed, preflight, identity, checksum, temperature, transfer, park failure in `campaign.sh`,
a Python error) and on any `bootrun.sh` exit other than 0 or 10. They print a summary per boot and keep every boot's
full output in `LOGDIR` (default `logs/` next to the scripts).

## Tests

```sh
GA100VFI_HOST=/path/to/pico-ga100vfi/host python3 -I tests/test_runner.py
python3 -I tests/test_launchers.py
```

`tests/test_runner.py` runs the runner against a fake `ga100vfi` package (`tests/fake/`, which logs every serial
command, BAR0 read and park, and fails the test on any BAR0 write) through every outcome and stop: under, lost with
and without a working park, poisoned FUSECTRL, DSOV and CRC, a busy FUSECTRL, hidden poison, goldilocks and partial
holds, mistimed shots, no dip, controller off, refused and cut-off shots, the delay and gap fields, `BAD0_FOLLOWUP`,
park hygiene and step validation; and the paths of 2026-11-21: stall reads (with poison found in one), stall holds,
shoot-through with its cap, compass frames, `PARTIAL_CONTINUE`, `LOST_NOPARK` with and without a healthy readback,
the sentinel rule for DSOV 0, and the SHOT lines; and `POISON_RESET` (a poison cleared, one that is not, no pulse
while GFW is still booting, none for hidden poison or `poisoned/over0`, the note before a later hold). `tests/test_hunter.py` covers the hunter. With `GA100VFI_HOST` it also checks that the runner's copy of `observe()` matches
the patched `passive_snapshot.observe` on 6,000 random reads. `tests/test_launchers.py` runs `firstshot.sh` and
`campaign.sh` against a fake `bootrun.sh`, and `bootrun.sh` against fake `ssh`, `scp`, `ping` and power tools with a
`bench.conf` in a temp dir, and runs `bootrun.sh`'s temperature gate on its own with a reading that bounces after
passing.
