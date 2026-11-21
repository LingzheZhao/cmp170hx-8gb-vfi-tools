# push3: a PMBus write may run across the reset release

`firmware/0011` turns push2 into push3 (`fw=lab-psr1-push3`, `-debug` in the debug build). It is one small change, and
it is what finished card E: see [`OVERLAP_EDGE.md`](OVERLAP_EDGE.md) for the map and the shot.

## What changed

- push2 refused any shot with a PMBus write in flight at the `PEX_RST` release (t = 0): `glitch ABORT: PMBus write ...
  runs ... across the PEX_RST release`. push3 runs that shot. The first such write is named after the shot:
  `glitch NOTE: push3: PMBus write act=1 scheduled at -40 us ran from -40 us across the PEX_RST release at 0 (allowed in push3; release_at is the check)`.
- Nothing else in the timeline changes. Every shot push2 accepts runs identically on push3 (same writes, same times,
  same assert, same release, same output apart from the marker).
- The other refusal stays: a VOUT step before the 600 mV setup write still aborts.
- The marker is `fw=lab-psr1-push3`. `tools/runner/bisect_shots.py` accepts push1, push2 and push3.

## Why it is safe

The refusal is older than the alarm. Before `0006` the step loop released `PEX_RST` itself, and the loop blocks for
every PMBus write (about 47 µs), so a write due at or just before t = 0 pushed the release late. Since `0006`/`0009`
the release is a hardware alarm whose handler runs at the highest IRQ priority from SRAM, and every PMBus write runs
with interrupts enabled. A write in flight cannot hold the release back any more: the alarm interrupts it, sets the
pin to input, and the I2C peripheral finishes the write on its own.

The runner checks this on every shot anyway. It scores a shot only when exactly one `release_at=` line says the
release came 0 to 4 µs after t = 0 by the alarm; anything else is MISTIMED and never counted as under or goldilocks.
The push3 NOTE line deliberately does not contain the runner's `overlaps the PEX_RST release` marker, so overlap
shots are judged by their measured release time alone.

## How it was checked

- **Host simulation** (`tools/hostsim/`): the firmware's glitch code, extracted and compiled on the host against
  stubs with a 1 µs clock that runs the alarm handler from inside any time advance when interrupts are on (so a write
  in flight really is interrupted). `check3.py` compares push2 and push3 on 27 commands: 16 that push2 accepts give
  byte-identical output, write timelines and reset-pin timing; 11 overlap shots that push2 refuses run on push3 with
  the dip and restore writes at the intended times and the release exactly at t = 0 by the alarm; a 3-pulse dither
  across the release and a dip written across the release run too; the setup-overwrite refusal is kept. 90 of 90
  checks pass, on the public series plus `0011`.
- **Builds:** `pico2_w` RP2350A (normal and debug), `pico2` RP2350A debug and `pico2_w` RP2350B debug, zero warnings.
  A `-Wall -Wextra` pass shows the same four format warnings as push2, all in code `0011` does not touch.
- **Bench (2026-11-21, card E):** 13 shots in the first run, 10 of them overlap shots (restore writes starting at -40 to 0 µs) and 3 push2-legal anchors, every
  one with `release_at=0 via=alarm`, none MISTIMED. The NVVDD trace showed the rail below 450 mV for pw minus about
  3 µs on every shot, so the rise landed where the schedule put it. The 13th shot was the goldilocks.

The lab's bench build was made from its own push2 tree; the public series arranges the unlock readback of `0007`
differently (see `firmware/README.md`), so checksums differ from the lab's image. Behavior is the same.

## Flashing it, and one trap

```sh
cd pico-ga100vfi && git checkout e1ebd97
git am /path/to/cmp170hx-8gb-vfi-tools/firmware/*.patch      # 0001 to 0011
cmake -S firmware -B build -G Ninja -DPICO_BOARD=pico2_w -DBOARD_RP2350A=ON -DDEBUG=ON && cmake --build build
picotool load -v -x build/pwmvid.uf2                         # the board must be in BOOTSEL
```

On the lab's bench `picotool load -f` (force a running board into BOOTSEL over USB) did **not** work with push2
running: the board never re-enumerated and its serial console stopped answering until it was replugged. What worked:

1. Hold BOOTSEL, unplug and replug the Pico's USB, release BOOTSEL after a second.
2. `picotool load -v -x pwmvid.uf2` (load, verify, run).
3. If it comes straight back as the bootloader (`2e8a:000f`, the button was still held when it rebooted), run
   `picotool reboot`.

Then check the marker with the `v` command: `... fw=lab-psr1-push3-debug`. Keep the push2 image next to it: rolling
back is the same three steps. Disconnect probes that touch the card while the Pico is unpowered or in BOOTSEL if you
are worried about back-feeding; the lab did.

## Using it

Overlap shots are refused by the planners unless you opt in, because push2 boards abort them:

- `tools/hunter/goldilocks_hunt.py overlap ...` writes plans that carry `"allow_overlap": true`.
- `goldilocks_hunt.py check --allow-overlap '<steps>'` checks steps under push3's rule.
- In `tools/planner`, a card file with `"allow_overlap": true` turns the release rule off for that card.

On a push2 board an overlap shot is refused by the firmware before anything is written (the runner stops with
exit 2), so a wrong plan cannot fire a different shot than it says.
