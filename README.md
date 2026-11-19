# CMP 170HX 8 GB: voltage-glitch bench tools

Bench tools and firmware fixes for the Pico voltage-glitch ("goldilocks") method on NVIDIA CMP 170HX 8 GB cards
(`10de:20c2`), built on [dalzyu/pico-ga100vfi](https://github.com/dalzyu/pico-ga100vfi).

With this glitch setup, and a separate burn step that is not part of this repository, the lab finished two
8 GB cards in November 2026: compute from 70 to 122 and 120 SMs (126 and 124
with the RECONFIG override), the 80 GB memory layout, and PCIe fuses that advertise 2.5 to 16 GT/s, all read back
after a cold boot. The method, wiring, parts and results are on cmp.wiki:

- [Voltage-glitch unlock: lab method on a Pico 2 W](https://cmp.wiki/wiki/Voltage-glitch_unlock:_lab_method_on_a_Pico_2_W)
- [Voltage-glitch unlock: lab results and fuse findings](https://cmp.wiki/wiki/Voltage-glitch_unlock:_lab_results_and_fuse_findings)
- [Voltage-glitch unlock (GA100VFI)](https://cmp.wiki/wiki/Voltage-glitch_unlock_(GA100VFI)), the community page

> **These tools only glitch and read.** None of them programs fuses. Burning FF records is permanent, a wrong
> record can brick a card, and no burn tool, burn planner or per-card plan is included here. A glitch by itself
> changes nothing permanent: a power cycle returns an unburned card to stock. You probe a powered card's board with
> needles and pull its reset line; a slipped probe can damage the card or the host. Use at your own risk.

## What is here

| Path | What |
|---|---|
| `firmware/` | Three patches against pico-ga100vfi (`git am`-ready), with a README: each card's own VRM profile restored after every shot, a classifier that stops at a poisoned fuse controller, a firmware marker in the debug build |
| `tools/vfi_preflight.py` | Read-only checks before a session: firmware marker, who controls the rail, the card's six VRM profile registers (stops on a leftover fast value), host lockdown, driver, config space, FUSECTRL, GFW boot progress |
| `tools/vfi_park.py` | Hold NVVDD at stock under PMBus, pulse PEX_RST, wait, read config space, hand the rail back |
| `tools/vfi_rescue.py` | The same sequence step by step with diagnostics, for a card whose link is gone after a crash |
| `tools/vfi_snap.py` | Register snapshot (fuse status, gates, SM count, FB partitions, the PCIe option words) to a 0600 JSON file; `--rows` adds two passes over the 512 fuse rows with the controller's READ command |
| `tools/vfi_trace.py` | NVVDD trace capture per shot with its outcome (debug firmware), back-to-back or first-shot mode |
| `tools/vfi_hunt.py` | Stop-and-hold hunter: shots at one setting until a clean goldilocks, which it leaves held |
| `tools/vfi_edge.py` | Edge tracker: walks the train timing along a card's lethal edge in short cells, with a crash budget |
| `tools/vfi_common.py` | Shared code: park, poison-safe classify, bounds, per-boot shot ledger |
| `tests/` | Offline tests against a simulated Pico, voltage controller and card |
| `docs/UPSTREAM_PR.md` | Pull request text for the firmware patches |

## Setup

Hardware, wiring and probe placement: see the
[method page](https://cmp.wiki/wiki/Voltage-glitch_unlock:_lab_method_on_a_Pico_2_W#Parts_and_host). In short: a
Raspberry Pi Pico 2 W class board (RP2350), four needles on the back of the card (SDA, SCL, PEX_RST_BUF*, ground),
an optional fifth on NVVDD for the debug build's ADC. No soldering.

Host, before the first shot:

1. Secure Boot off. `/sys/kernel/security/lockdown` must show `[none]`, or every BAR0 and config write is refused.
2. No NVIDIA driver near the card: bind `10de:20c2` to `pci-stub` and keep `nvidia` blacklisted on the glitch boot.
   A driver build that applies fuse overrides at probe time can write to a card whose DSOV reads 0.
3. Boot with `pci=noaer`.
4. Memory decode on for a cold card with no driver (`vfi_preflight.py --enable-decode`, or
   `setpci -s <bdf> COMMAND=0x0002`).

Firmware and host package:

```sh
git clone https://github.com/dalzyu/pico-ga100vfi && cd pico-ga100vfi
git checkout e1ebd97                       # the base these patches were made against
git am /path/to/cmp170hx-8gb-vfi-tools/firmware/*.patch
# build per docs/BUILD.md, e.g. for an official Pico 2 W (RP2350A):
#   cmake -S firmware -B build -G Ninja -DPICO_BOARD=pico2_w -DBOARD_RP2350A=ON [-DDEBUG=ON]
python3 -m venv ~/vfi-venv && ~/vfi-venv/bin/pip install ./host
```

Flash the Pico before any lead touches PEX_RST. The debug build (`-DDEBUG=ON`) is needed only for `vfi_trace.py`;
it samples NVVDD on GP26 on RP2350A boards (GP42 on RP2350B).

## Use

Every tool takes `--bdf` (the card's PCI address) and `--port` (the Pico's serial port), or reads `VFI_BDF` and
`VFI_PORT`. Run as root, with the venv's Python:

```sh
export VFI_BDF=0000:05:00.0                                # yours: lspci -Dn -d 10de:20c2
export VFI_PORT=/dev/serial/by-id/usb-Raspberry_Pi_...     # yours
PY=~/vfi-venv/bin/python

sudo -E $PY tools/vfi_preflight.py --bdf $VFI_BDF --enable-decode
sudo -E $PY tools/vfi_snap.py --rows --label cold-baseline       # keep this file private
sudo -E $PY tools/vfi_hunt.py --v-dip 356 --budget 200            # stops and holds on a clean goldilocks
sudo -E $PY tools/vfi_snap.py --label held                       # read the held state
sudo -E $PY tools/vfi_park.py                                     # always end here
```

More examples:

```sh
# first-shot mode: park before every shot
sudo -E $PY tools/vfi_hunt.py --v-dip 331 --t-dip 0 --budget 100 --first-shot
# edge tracker: 10-shot cells, start on the safe side, rotate two depths
sudo -E $PY tools/vfi_edge.py --v-dips 356,356,362 --t-dip-start -140 --crash-budget 60
# NVVDD traces (debug build), two cells, five shots each
sudo -E $PY tools/vfi_trace.py --out traces.json --shots 5 338:-60 331:0
# link gone after a crash
sudo -E $PY tools/vfi_rescue.py
```

Exit codes: 0 done (or goldilocks held), 1 nothing found or a check failed, 3 stopped by a safety check (the
message says which), 4 park or rescue failed, 5 serial or sysfs error.

If one CPU core of your host is suspect, the tools run fine under `taskset`.

## Shot outcomes

| Outcome | What the reads show | Meaning |
|---|---|---|
| under | DSOV still 1 | dip too shallow, or at the wrong time |
| over | DSOV 0, CRC not `0x55` | too deep |
| hit | DSOV 0 and CRC `0x55`, another check fails (usually the privilege mask) | close, not burnable |
| goldilocks | all five checks pass: DSOV 0, CRC `0x55`, privilege mask `0xFFFFFFFF`, FPF row readable, DSOV write-back works | the target state |
| lost | no link after the shot | the GPU crashed; the next park or shot resets it |
| nack | the voltage controller did not answer on I2C | seen only at crash-prone settings: a crashing GPU disturbs the shared I2C bus |
| poisoned | FUSECTRL `0xBADFxxxx` | the dip disturbed the fuse read itself; nothing else is read, the next reset clears it |

A poisoned shot read with the stock classifier shows DSOV 0, CRC 0 and privilege mask 0 because nothing was read.
Count a real DSOV flip only when the privilege mask is nonzero. One card here gave about 600 "hit" shots with the
privilege mask stuck at `0xFFFFFEFE`; a sense on one of them poisoned the controller. Wait for the full mask.

## What moves the window

- **First shot versus back-to-back.** The campaign leaves the rail under PMBus control between shots (`revert=0`).
  The first shot after a park starts from the GPU's own PWM-VID control instead, and its trough measured about
  40 mV shallower (270 against 230 mV on the Pico ADC). The two behave like two different settings. One card's
  second goldilocks was the first shot of a cell, right after a park; on another card the lethal timing edge moved
  about 45 µs later in first-shot mode. `--first-shot` parks before every shot.
- **Timing is a continuous knob.** `t_dip` sets when the pulse train starts relative to the PEX_RST release (tool
  default -60 µs) and moves in 1 µs steps; depth moves in 6.25 mV controller codes (354 and 356 mV are the same
  code). One card's DSOV flip had a 4 µs wall (-66 µs never flipped, -62 µs did). Another hit after the standard
  3 x 198 µs train was slid 60 µs later. On a third, the line between surviving and crashing was 1 µs wide and did
  not drift. `vfi_edge.py` walks that line; pulse width is the other fine knob.
- **Temperature.** Let the card settle at its normal idle temperature and hold it there with a fan. Cooling moved
  one card's edge up one code, 43.7 C moved it down, and at 47-49 C every shot was lost. The two hits here came at
  about 40 C and 36 C, read from the controller's own sensor (READ_TEMP1, taken as 0.1 C per bit; the scale is not
  confirmed, so watch the trend, not the number).
- **Shots per boot.** The lab kept to about 200-450 shots between host reboots, in line with community reports
  that nothing hits after about 500 shots on one boot. Every tool that glitches counts shots and lost shots per
  host boot (`--max-shots-per-boot`, default 450; `--max-lost-per-boot`, default 150) in `--state-dir`, and stops at
  the cap. `vfi_edge.py` adds a per-run `--crash-budget` (default 60).
- **Cards differ.** Commanded depth at the hit ranged from 300 to 356 mV across sessions and cards. One card never
  reached goldilocks in about 4,000 shots: it went from under straight to a crash at every depth and timing tried,
  with the fuse read and the lethal event inside the same few microseconds. Details on the results page.

## Safety behavior

- **Park before config reads.** A crashed GA100 under its own PWM-VID control asks for the ~0.3 V floor, and every
  PEX_RST then reboots it into a brown-out; a half-booted card can also answer config reads with CRS, which an
  Intel root complex retries forever (the lab's reading of three silent host freezes, each on a config read right
  after a handback). Park holds the rail at stock
  787.5 mV under PMBus and checks READ_VOUT before it pulses PEX_RST and reads config space. If the link does not
  come back while held, the rail stays held and config space is not read again; if the GPU asks for the floor
  after the handback, the rail is taken back. Every tool that glitches parks at the start, between cells and on
  exit (also on Ctrl-C or a safety stop), except when it holds a goldilocks.
- **Poison-safe reads.** FUSECTRL is read first after every shot and in the snapshot; on `0xBADFxxxx` nothing else
  is read. This works with the stock classifier too; patch 0002 adds the same check upstream.
- **Firmware and profile checks.** With the fast VRM profile, the tools refuse firmware without the profile
  save/restore patch (stock firmware writes its author's VRM table onto your card after every shot) and refuse to
  start when one of the six profile registers holds its fast value (an interrupted shot: power the card fully off).
- **Bounds.** The firmware has no voltage limits of its own. The tools accept dips of 280-400 mV, recovery of
  550-800 mV, 1-5 pulses of 36-400 µs and `t_dip` within ±1000 µs, and stop otherwise.
- **No driver, no lockdown.** The tools stop if the card is bound to anything but `pci-stub` (or nothing), if the
  `nvidia` module is loaded while the card is not held by `pci-stub`, or if kernel lockdown is on.
- **Reads only.** The snapshot is BAR0 reads only by default. `--rows` writes nothing but FUSEADDR and the READ
  command to FUSECTRL, behind an allowlist, never DEBUGCTRL, and restores FUSEADDR. Fuse dumps and the upstream
  campaign's goldilocks log identify the card: keep them private.
- **The firmware's `u` command** writes the voltage controller's unlock and config-id, as upstream documents. The
  tools use it to take the rail under PMBus control for park and rescue; nothing else is written to the controller
  except through the firmware's own glitch command.

## Status

The firmware patches rebuild the exact firmware the lab ran on its cards (the debug build is byte-identical; the
normal build differs only in its embedded build date). The host tools are cleaned-up, generic versions of the
bench scripts used for those cards; they pass the offline tests in `tests/`, and they had not yet run on a card in
this exact form when published (2026-11-19). Reports welcome.

Run the tests with the ga100vfi host package on the path (needs pyserial):

```sh
GA100VFI_HOST=/path/to/pico-ga100vfi/host python3 -I tests/test_tools.py
```

## Upstream

The patches are meant for upstream; `docs/UPSTREAM_PR.md` has the pull request text. They are made against
pico-ga100vfi `e1ebd97`. Upstream has rewritten its history before; if `git am` fails on a newer checkout, the
changes are small enough to apply by hand.

## Credits

[kate-land](https://github.com/kate-land) for the GA100 voltage-glitch method and the original
[ga100vfi](https://github.com/kate-land/ga100vfi) tooling; [dalzyu](https://github.com/dalzyu) for
pico-ga100vfi, which these tools drive; [mellojello_](https://github.com/JosiahWhite) for proving the I2C and
PEX_RST path; the members of the cmpunlocker Discord whose reports are credited on cmp.wiki.

## License

MIT, as upstream. See `LICENSE`.
