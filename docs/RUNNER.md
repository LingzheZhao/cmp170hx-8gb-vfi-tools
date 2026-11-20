# The per-boot runner and the boot launcher

`tools/runner/` holds the scripts the lab used to run its glitch campaigns one boot at a time, cleaned of
bench-specific names: the card's address, the serial port, the hosts and the power control are settings now. The
logic is unchanged, except that the launcher's last step (handing a held goldilocks to a burn script) is replaced
by an optional hook. Nothing here burns fuses.

| File | Runs on | What |
|---|---|---|
| `bisect_shots.py` | bench | The per-boot runner: fires a sequence of steps, classifies every shot, stops at the first shot that is not "under" (or, in cell mode, counts outcomes per step) and holds a goldilocks |
| `park.py` | bench | Hold NVVDD at stock under PMBus, pulse `PEX_RST`, then read config space and hand the rail back. The runner calls it after a lost shot and at the end |
| `preflight.py` | bench | Read-only Pico and MP2888B check: firmware marker, rail under GPU control, the six VRM profile registers |
| `bootrun.sh` | controller | One run per boot: power cycle, fresh-boot guard, plan checksum, host guard, preflight, card identity, temperature gate, runner, hold hook |
| `cycle.sh` | controller | Power-cycle the bench host only |
| `power.sh` | controller | Power helpers for both (Wake-on-LAN or IPMI) |
| `bench.conf.example` | controller | Settings and the per-card table; copy to `bench.conf` |

`tools/diag/` has two gentle checks at 700 mV: `timing_controls.py` (where the `PEX_RST` release lands with each kind
of shot; needs the timing build) and `enswtest.py` (whether EN_SW_OVERRIDE survives `PEX_RST`; writes that register).

## On the bench

Prepare the host as in the main README (Secure Boot off, card on `pci-stub`, no NVIDIA driver, `pci=noaer`, memory
decode on) and install the patched host package in a venv:

```sh
git clone https://github.com/dalzyu/pico-ga100vfi && cd pico-ga100vfi && git checkout e1ebd97
git am /path/to/cmp170hx-8gb-vfi-tools/firmware/*.patch
python3 -m venv /opt/vfi/venv && /opt/vfi/venv/bin/pip install ./host
```

The runner can be used by hand:

```sh
export VFI_BDF=0000:05:00.0 VFI_PORT=/dev/ttyACM0
sudo -E /opt/vfi/venv/bin/python -I tools/runner/bisect_shots.py 'card X' \
  '[[450,-60,3,198,750,1,2],[381,-60,3,80,750,1,5],[375,-60,3,80,750,1,5]]'
```

A step is `[v_mV, td_us, pulses, pw_us, v_restore_mV, fast, reps]`. Omitted fields default to 3 pulses, 198 µs,
750 mV, `fast` 1 and `REPEAT` shots. Every shot uses gap 150 µs, delay 117 µs and `revert` 0, so the rail stays
under PMBus control between shots. Order the steps from the weakest effect to the strongest.

- **Single mode** (every step 1 shot): the run stops at the first shot that is not "under".
- **Cell mode** (`REPEAT` > 1 or a `reps` field > 1): each step is a cell of back-to-back shots. A lost shot is
  parked and the run goes on; partial states (DSOV 0 that is neither goldilocks nor CRC 0 with mask 0, such as
  `dsov0 crc=0x55 priv=0xfffffefe`) are counted and the run goes on; anything else stops it. `LOSTRUN` lost shots in a row stop the run, `CELLLOST` lost shots in a
  row skip the rest of a cell, and `SHOTCAP` caps the shots per run. Every cell ends with a count line:
  `cell 3 v=369 td=-60 np=3 pw=80 vr=750: {'under': 5, 'lost': 0, 'partial': 0, 'other': 0}`.

After every shot the runner checks, in order:

1. The firmware's reply: `pwd=1` and `e1=0xc2fa`, or it stops with **NO DIP** (exit 12): a locked controller drops
   the dip writes, so the shot did not run.
2. The controller: STATUS_WORD bit 6 (OFF) or READ_VOUT below 500 mV stops with **CONTROLLER OFF** (exit 9).
3. The link (`RESTORE_WAIT`, default 8 s). No link is **lost**.
4. FUSECTRL first. `0xBADFxxxx` is **poisoned**, and nothing else is read.
5. The classifier (patched, passive). A `0xBADF` DSOV, CRC or privilege mask is poisoned (no re-read); CRC 0 and
   mask 0 is **poisoned/over0**; DSOV 1 is **under**; DSOV exactly 0, CRC `0x55` and mask `0xFFFFFFFF` is
   **GOLDILOCKS**; anything else is printed as read (`dsov0 crc=.. priv=..`, `dsov=0x..`).
6. After an "under": the `SENTINEL` alias register (a `0xBAD.....` value is **hidden-poison**), then GFW boot
   progress (low byte `0xFF` within 3 s, or **gfw-not-done**).
7. A CRC or mask read of `0xBAD0....` (a half-booted card) is read again after 2 s.

On GOLDILOCKS the runner prints `>>> GOLDILOCKS HELD (<tag>) v=.. td=..` and exits 0 without parking: the rail stays
under PMBus control and the state stays readable. Everything else ends with a park. Exit codes: 0 sequence done or
goldilocks held, 2 unlock failed, 7 park failed before a step, 9 controller off, 10 tipped (a stop above, or a
failed park after a lost shot), 12 no dip.

Environment: `VFI_BDF` (required), `VFI_PORT`, `VFI_PARK`, `VFI_TRACE_DIR`, `FLOOR` (lowest dip, default 381 mV),
`REPEAT`, `SHOTCAP`, `LOSTRUN`, `CELLLOST`, `RESTORE_WAIT`, `SENTINEL` (0 turns it off), `PARK_EACH=1` (park before
every shot: first-shot mode), `TRACE=1` (NVVDD traces, debug firmware, NVVDD lead on), `CLASSIFY_WRITE=1` (let the
classifier write EN_SW_OVERRIDE; off by default, see `MEASUREMENTS.md`). `RETRAIN=1` and `SBR=1` (link retrain and
secondary bus reset after a lost shot) exist for diagnosis only; do not use them during a campaign.

## On the controller: `bootrun.sh`

`bootrun.sh` gives every run a fresh boot, so nothing (a sticky poison, a latched controller, a stale override)
carries from one run into the next. Copy `bench.conf.example` to `bench.conf`, fill it in and add one `card_<name>`
function per card, then:

```sh
FLOOR=340 TMIN=360 TCAP=120 REPEAT=1 SHOTCAP=450 LOSTRUN=1 CELLLOST=1 RUNTO=900 \
  tools/runner/bootrun.sh example '[[450,-60,3,198,750,1,2],[381,-60,3,80,750,1,5],[375,-60,3,80,750,1,5]]'
```

1. **Power cycle** (`POWER=wol` or `ipmi`): clean shutdown, wait until the host is really off (ping, or the BMC's
   power status, with a hard power-off if a clean shutdown stalls), wait `OFFWAIT`, power on, wait for ssh. A host
   that does not go down stops the run: it needs a hand on the power button. `NOCYCLE=1` skips this.
2. **Fresh-boot guard**: the host must have been up for less than `FRESH_MAX` seconds (default 900).
3. **Plan checksum** (only when the card names `PLAN_FILES`): the files on the bench must match the reviewed copies
   on the controller by sha256.
4. **Copy** the runner, `park.py`, `preflight.py`, `vfi_snap.py` and `vfi_common.py` to `BENCH_DIR`. `RUNNER=<file>`
   copies another runner, for example `experimental/runner/bisect_shots.py`.
5. **Host guard**: optional kernel command line word (`GLITCH_CMDLINE`), no `nvidia` module, no kernel lockdown,
   exactly one `10de:20c2` card (or `CARD_BDF`) bound to `pci-stub` or nothing, the Pico's port present, memory
   decode on, GFW boot progress not `0x01`. `MASK_PCIEHP=1` masks pciehp interrupts on a hotplug-capable root port.
6. **Preflight** (`preflight.py`) must print `PREFLIGHT OK`.
7. **Identity**: `vfi_snap.py` must print the card's `GPC dis/def/st=<GPC>/` and `SMC` line from `bench.conf`. A
   different card stops the run.
8. **Temperature gate**: wait until READ_TEMP1 (raw) reaches `TMIN`, for up to `TCAP` seconds.
9. **Run** `bisect_shots.py` with a time limit (`RUNTO`) and the runner settings from the environment.
10. **Hold**: when the log shows `>>> GOLDILOCKS HELD`, `bootrun.sh` runs `ON_HOLD` if it is set (with `CARD`, `TAG`,
    `BENCH_SSH`, `BENCH_DIR` and `RUN_LOG` in its environment) and otherwise stops and reports the held state. Read it
    with `vfi_snap.py`, then park or power-cycle.

The run log goes to `LOG_DIR/bootrun-<card>.last`; the last line is `RESULT: ...` (or the hold report). Exit codes
are listed at the top of the script.

## Tests

```sh
python3 -I tests/test_runner.py
```

runs the runner against a fake `ga100vfi` package (`tests/mock/`) through every stop (under, lost with and without a
working park, partial, poisoned FUSECTRL and DSOV, over0, goldilocks, bad DSOV values, sentinel, GFW gate, no dip,
controller off, floor, shot cap) and checks the shell scripts' syntax and config handling.
