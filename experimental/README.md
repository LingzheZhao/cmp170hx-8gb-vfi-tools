# Experimental: not yet run on hardware

> **Nothing in this folder has run on a card yet.** The firmware builds with no warnings and the runner passes its
> offline tests against a fake bench; that is all. Expect changes. For work on a card use `../firmware` (0001-0005)
> and `../tools/runner`, which are what the lab ran. Like the rest of this repository, nothing here burns fuses.

## Firmware: `firmware/` (marker `fw=lab-psr1-push1`)

Three `git am` patches that apply on top of `../firmware/0001-0005` (made with one line of context, so apply them on
exactly that base):

```sh
cd pico-ga100vfi && git checkout e1ebd97
git am /path/to/cmp170hx-8gb-vfi-tools/firmware/*.patch
git am /path/to/cmp170hx-8gb-vfi-tools/experimental/firmware/*.patch
```

| Patch | What |
|---|---|
| 0001 | `PEX_RST` is released by a hardware alarm at exactly t = 0 (highest IRQ priority on core 0) instead of a timeline step, so a PMBus write in flight no longer delays it. A `glitch WARN` line names every write that overlaps t = 0, and `release_at=<us> scheduled=0 via=<alarm\|fallback>` reports when the release ran. The assert step is skipped if the release has already fired, and the pin is released unconditionally after the timeline. |
| 0002 | `u` always writes reg `0x13` = `0x00` and config ID `0x46` = `0x1229` and reads both back (`unlock MATCH wp=00 cfg=1229`). The stock command returned early when the controller already reported MATCH, which can happen after a soft-off because the Pico and the standby rail stay powered. Here a separate `unlock_config()` runs after the stock unlock; the lab's own build made the same change inside the unlock function. |
| 0003 | Lab knobs, all off by default: `fast` 2..7 (one subset of the fast profile each), `t_restore` <= -1000 (a longer `PEX_RST` lead so a long dip is not overwritten by the setup write), `revert=2` (one-VID dither inside the train), `hold_ms` with the SCL hold starting at the release, and `rail_code=... status ...` (READ_VOUT and seven status bytes) after any shot that leaves READ_VOUT below 500. |

Why 0001 exists: on one bench, with the timing build (`../firmware/0004`), a single 36 µs pulse at `t_dip` +81 µs
released `PEX_RST` at +59 µs and at +82 µs released it at +13 µs, because the release waited for a voltage write
already in flight. See `../docs/MEASUREMENTS.md`.

## Runner: `runner/`

`runner/bisect_shots.py` is `../tools/runner/bisect_shots.py` with:

- Steps `[v, td, pulses, pw, v_restore, fast, reps, hold_ms, t_restore, revert]`. Omitted fields send the same
  command as the main runner. Every step is checked before the first shot (v_restore 575-825, v >= FLOOR, fast 0-7,
  hold 0-20, revert 0 or 2, t_restore -20 or -20000..-1000). `revert` 1 is refused because the no-dip check needs
  the rail under PMBus control after the shot.
- The knobs of patch 0003 stop the run unless `v` reports `fw=lab-psr1-push` firmware.
- Once per boot: the `v` line, an unconditional `u` with its readback (a WARNING unless `wp=00 cfg=1229`), and a
  read of controller registers `0x13 0x46 0x7E 0x79 0x88 0x8B 0x8D` before the first shot.
- Every shot logs the firmware's echo (resolved `pw` and `gap`), WARN, timing, `release_at` and `rail_code` lines.
- Instead of `classify()`, a copy of `passive_snapshot.observe` (FUSECTRL, GFW, DSOV, CRC, privilege mask; stop at
  the first `0xBADF` value, I/O error, all-ones read, GFW booting or DSOV not 0 or 1; no BAR0 writes), then the
  sentinel alias. GFW still booting is polled for up to 5 s.

Use it through the launcher: `RUNNER=experimental/runner/bisect_shots.py tools/runner/bootrun.sh <card> '<SEQ>'`.

Tests (offline; the observer copy check needs the patched host package):

```sh
GA100VFI_HOST=/path/to/pico-ga100vfi/host python3 -I experimental/runner/test_bisect_shots.py
```

`runner/fake/` is a stand-in `ga100vfi` package: serial replies like the push1 firmware, BAR0 values per shot code,
every access logged, and any BAR0 write fails the test.
