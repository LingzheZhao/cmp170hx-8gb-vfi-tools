# planner: glitch ledger, next-shot planner and hunter loop

The planner reads the runner's logs, keeps an append-only ledger of every shot and proposes the next shots for a
card. `hunt.py` can run the whole loop (plan, run a launcher, ingest, plan again) unattended, inside a budget and a
set of stop rules. Nothing in this directory talks to the bench, the Pico or a card: the only thing it ever runs is a
launcher from `tools/runner/` (`firstshot.sh` or `bootrun.sh`), and only with `hunt.py --execute`. Nothing here burns
fuses or arms a burn.

Python 3.10 or newer, standard library only.

## Quick start

```
python3 plan_run.py --card e                       # plan with the card's default family (cards/e.json)
python3 plan_run.py --card e --ledger samples/sample-ledger.jsonl   # the same on the synthetic sample
python3 summary.py --card e --ledger samples/sample-ledger.jsonl    # per-family maps: VID code x delay, per pw
python3 hunt.py --card e                           # dry run: plans once and prints the command it would run
cd tests && python3 -m unittest discover          # the test suite (no hardware, no ssh)
```

`plan_run.py` prints the never-rules, the state of every column or search phase, the proposed shots and two
commands: the launcher call and the matching `ingest.py` call. An operator runs the first by hand, then the second,
then plans again. `hunt.py --execute` does the same in a loop:

```
python3 hunt.py --card e --execute --max-boots 12 --max-hours 2
python3 hunt.py --card e --family families/e-vr625-push2.json --execute --once
python3 ledger_tools.py dedupe --card e [--apply]   # shots ingested twice -> VOID records
```

Other entry points:

```
python3 plan_run.py --card e --family families/e-std3x80-first-push2.json --batch 6
python3 plan_run.py --card e --json                            # machine-readable plan
python3 ingest.py --card e --firmware lab-psr1-push2-debug RUN/launcher.out
python3 ingest.py --card e logs/RUN                             # a directory of boot-N.log files
python3 ingest.py --card e --fresh bootrun-e.last               # a bare bootrun log (no 'cycle (' line)
python3 ingest.py --card e --dry-run LOG                        # print the records, append nothing
```

## Where things are found

| What | Default | Override |
|---|---|---|
| card config | `cards/<card>.json` | `--card` also takes a path to a JSON file |
| ledger | the card config's `ledger` (`ledger/card-e.jsonl`) | `--ledger FILE` |
| launchers | the card config's `launcher.runner_dir`, else `$VFI_RUNNER_DIR`, else `../runner` (tools/runner) | `--runner-dir DIR` (plan_run.py, hunt.py) |
| run output | `runs/` next to these scripts | `--run-dir` (plan_run.py), `--run-root` (hunt.py) |

The card id is passed to the launchers as their first argument, so `bootrun.sh e ...` needs a `card_e` function in
your `bench.conf` (see `tools/runner/bench.conf.example`). The card config's `launcher.env` and the family's
`launcher.env` become environment variables of the launcher call (`TMIN`, `TCAP`, `OFFWAIT`, `FLOOR`,
`BAD0_FOLLOWUP`, `RUNTO`; for PARK_EACH boots also `PARK_EACH`, `REPEAT`, `LOSTRUN`, `CELLLOST`, `SHOTCAP`,
`BOOTRUN_LOG`; for first-shot runs `LOGDIR`).

## Files

| Path | What |
|---|---|
| `planner_auto/vid.py` | `mv_to_code` bit-exact with the firmware (`(int)(mv / 6.25f + 0.5f)` in float32), canonical mV per code |
| `planner_auto/steps.py` | runner step format `[v, td, pulses, pw, vr, fast, reps, hold, t_restore, revert, delay, gap]` |
| `planner_auto/schedule.py` | port of the firmware's shot timeline (push2): resolved pw/gap, queued 47 us writes, overlap and overwrite rules |
| `planner_auto/ledger.py` | JSONL ledger: fsynced append, ARMED/COMPLETE resolution, family policy (`effective_outcome`) |
| `planner_auto/parse.py` | log parser: bootrun, firstshot, campaign and wrapper output, push1 and push2 runner lines, cell mode |
| `planner_auto/rules.py` | never-rules (floor, runner limits, pw floor, release overlap, overwrite, learned latch energy, never list) |
| `planner_auto/planner.py` | edge method: per-column VID bisection with repeats, columns, prior hints, densify, lock-in, lookahead |
| `planner_auto/compass.py` | compass method (CRC compass): loss island, delay sweep, band edges, pw tune, drift loop |
| `planner_auto/launch.py` | plan -> launcher argv/env (firstshot.sh, or one bootrun.sh boot with PARK_EACH=1 and reps); card/ledger/runner lookup |
| `hunt.py` | autonomous plan/run/ingest loop with stop rules and a decision log (dry run unless `--execute`) |
| `plan_run.py`, `ingest.py`, `summary.py`, `ledger_tools.py` | CLIs |
| `cards/e.json` | example card config: floor, latch margins, never list, launcher env, families |
| `families/*.json` | example search families for that card (see Methods) |
| `ledger/` | ledgers, one JSONL file per card; ships with an empty `card-e.jsonl` (see `ledger/README.md`) |
| `samples/sample-ledger.jsonl` | 14 SYNTHETIC records (see below) |
| `tests/` | unit and CLI tests; `tests/fakebench/` is a fake firstshot.sh / bootrun.sh for the hunt tests |

`cards/e.json` and the `e-*` families are the configuration used for card E in the lab write-up; the numbers in their
descriptions and never lists are that card's. Copy and adapt them for another card.

## Synthetic sample

`samples/sample-ledger.jsonl` is made up. It shows the record format and gives `summary.py` and `plan_run.py`
something to work on: a short descent with the slow-slew family, a loss island at 356|350 mV with repeats, three
delays at the anchor and one controller-off latch on a 3 x 198 us train. None of it is a measurement of any card;
every record has `"source": "synthetic"` and says so in `notes`.

## Ledger

One JSON object per attempted shot, appended and fsynced. Main fields: `attempt_id`, `phase` (ARMED | COMPLETE |
VOID), `card`, `boot_id`, `boot_uptime_s`, `shot_in_boot`, `state_class` (first-shot-fresh-boot |
later-shot-same-boot | post-park), `firmware`, `step` (all 12 fields), `step_list`, `vid_code` (6.25 mV units,
firmware rounding), `resolved` (pw/gap/delay/reset_at/dither/hold_at from the timeline model), `write_overlaps_release`,
`overwritten_vout`, `release_at`, `release_via`, `prime_at`, `warns`, `writes_failed`, `pwd`, `e1`, `runner_outcome`
(raw text), `outcome`, `outcome_detail`, `dsov`, `crc`, `plm`, `crc_source`, `regs`, `observed`, `park`,
`temp_raw_start/end`, `run_env`, `conditions`, `raw`, `source`, `source_file`, `source_sha256`, `source_line`, `notes`.

Outcome classes: `under`, `crc_closed` (CRC 0x77), `crc_intermediate` (0x75, 0x57 or between 0x55 and 0x77),
`partial` (any other non-healthy read with the link alive, incl. DSOV 0 with PLM 0xFFFFFEFE), `goldilocks`, `over`
(CRC 0x00), `lost`, `poisoned`, `hidden_poison`, `controller_off` (also a controller that cannot be read right after
a shot), `no_dip`, `mistimed`, `invalid`. Planner meaning: under and crc_closed are too weak; lost, poisoned,
hidden_poison, controller_off and over are too strong; crc_intermediate and partial are a signal (densify);
goldilocks starts lock-in; mistimed, invalid, no_dip and unknown carry no information (the point is proposed again).

Identity: `boot_id` is the bench clock at START (`e@2026-11-20T09:12:55B`), so the same boot dedupes whichever log
is ingested; wrapper headers give `armed_key` (`...L`, launcher clock). A wrapper boot with a step but no outcome (in
flight, or a killed launcher) becomes ARMED: `pending` for 20 min (its point is not planned again), then `unknown`.
It is never read as under.

Duplicates: one boot can reach the ledger twice (a wrapper's filtered output and the launcher's full per-boot log).
Records carry `launch_time` and `host_boot_id` (the bench kernel's boot id, logged by bootrun); `resolve()` keeps one
record per shot and `append_new()` skips such copies. Older copies are withdrawn with append-only `VOID` records
(`ledger_tools.py dedupe --apply`); nothing is rewritten.

State classes: `first-shot-fresh-boot`, `later-shot-same-boot`, `post-park` (PARK_EACH=1: a park before every later
shot). On card E a park normalized the shot index (350 mV pw 80 twice with a park between: under/under; without the
park the second shot lost the link), so the `e-*` families list `"equivalent_states": ["post-park"]`: post-park shots
count as first shots and each one is an independent sample. Remove it from a family file to turn this off. The
runner does not print PARK_EACH; `ingest.py` takes `--park-each`.

The ledger keeps facts; the family policy decides what counts: accepted firmware, release window (push2 runner:
0..4 us; push1 accepted up to 15), a write overlapping the release = mistimed, excluded conditions (NVVDD probe on).
A goldilocks or CRC signal is never dropped on a timing technicality; it is flagged instead.

## Never-rules (every proposal passes all of them)

1. v >= the card floor (`floor_mv`; also passed to the runner as FLOOR).
2. Runner limits: vr 575..825, fast 0..7, hold 0..20, revert 0 or 2, t_restore -20 or -20000..-1000, delay 0..900,
   gap 36..2000, pulses 1..16.
3. pw >= 47 us (one measured PMBus write; below 36 the firmware clamps silently).
4. No PMBus write in flight across the PEX_RST release: each write takes 47 us and queues behind the previous one; a
   write whose queued start t satisfies `t <= 0 < t + 47` is refused (push2 aborts such a shot). For a plain train:
   dip command at `td - delay + k*(pw+gap)`, restore `pw` later, overlap if `t_cmd <= 0 < t_cmd + 47`.
5. No VOUT step before the 600 mV setup write at reset_at (it never reaches the rail).
6. Latch energy, learned from every controller_off in the ledger: a latch at code c with E = pulses x pw forbids
   code <= c + depth_margin and E >= E_latch x (1 - energy_margin). Card E uses margins 0 codes / 5 % (3 x 198 at
   388 mV was under more than 40 times): 381 mV 3 x 198 -> code <= 61 and E >= 564; 388 mV 3 x 220 -> code <= 62
   and E >= 627.
7. The card's `never` list (`{"match": {field: value or [lo, hi]}}`, fields = step fields + code + energy); card E
   repeats its latch rules and excludes long flat dips (pw > 1000 us).

## Methods

### edge (`e-std3x80-first-*`)

Per column (the base geometry plus one-knob overrides: vr, pw, td, delay, revert ...), the primary knob (VID code by
default) is bisected between the deepest all-under value U and the shallowest value that ever tipped S. Descent is in
`coarse_step` codes from `start_mv` (1 code once a column has a hint). Adjacent U|S needs `repeats` (default 2)
informative shots from independent boots at both values before the edge is declared; a mixed S is a stochastic edge
with its tip rate. Columns open after the base edge and start at the base edge's S then U. Partials and CRC signals
switch to densify (repeat, code +-1, td/pw/vr fine steps); goldilocks switches to lock-in. A batch is filled by
lookahead: a repeat whose outcome is predictable is assumed and planning continues.

### compass (`e-slow-push2`, the card default; also `e-vr625-push2`, `e-compass-push2`, `e-compass-pw120-push2`)

A community member's method (CRC_STATUS as a compass: 0x77 closed, 0x75 / 0x57 next to the window, 0x00 over),
run as first shots or PARK_EACH-normalized shots:
0 lock-in; 1 drift tracking (below); 2 loss island: VID bisection on the base geometry with repeats, anchor = the
shallow side (one 6.25 mV code above the loss); 3 delay sweep at the anchor, 120..460 us by 30 plus the base delay, in
spread order, skipping (and listing) delays whose writes would cross the release; 4 band edges: bisect delay to 2 us
between neighbouring swept delays that flip weak <-> strong; 5 pw tune at each edge's weak delay, +10 us steps then
2 us, capped by the latch rule; 6 widen one code deeper (all weak) or shallower (all strong), up to `codes_extra`;
7 drift loop: repeat the signals and edge weak sides, least recently shot first.

Drift tracking: any CRC signal (crc_intermediate, partial) or sporadic fuse touch (poisoned, hidden poison, over)
inside the family's fixed geometry switches the family to drift rounds around the best signal: `drift.offsets`
(default delay -4..+4 in 2 us, illegal delays skipped) x `drift.shots` per round, one round every
`drift.round_interval_s` (plan field `not_before`; hunt.py waits for it). The set is re-centred every round.

Delay note: in the firmware td and delay only appear as `td - delay` (the first dip command time), so
`[.., td -60, delay 330]` and `[.., td -273, delay 117]` are the same waveform; the ledger pools them. Some delays are
unreachable without a write across the release: with 3 x 198 / gap 150 / td -60, delays 138..184 and 288..334; with
3 x 80, the holes in 120..460 are 170..216, 250..296 and 400..446.

### lock-in

When a family has a goldilocks, the best recipe (highest one-sided Wilson lower bound) is repeated as the first shot of
fresh boots until the bound reaches `lockin.target` at `lockin.confidence` (default 0.9 at 95 %: 25 hits in a row).
`bootrun.sh` stops on a held goldilocks (and runs its optional ON_HOLD hook), so with the stock launcher lock-in ends
at the first verified hit.

## hunt.py

`hunt.py` repeats: plan (the family's method) -> build the launcher call (`planner_auto/launch.py`: firstshot.sh with
one fresh boot per proposal, expected losses spread apart; or one bootrun.sh boot with PARK_EACH=1, LOSTRUN=1,
CELLLOST=1, identical proposals merged into reps, known-under first and expected losses last, at most
`launcher.max_shots_per_boot`) -> run it (`--execute` only; output in `<run-root>/hunt-<ts>-c<card>/itNNN/`) ->
ingest -> stop rules -> plan again. The default is a dry run that plans once and prints the command. Budgets:
`--max-boots`, `--max-hours` (checked before each launch; a running launcher is never cut for budget);
`--boot-timeout-min` kills a hung launcher by its own process group and stops.

Stop rules, per boot: GOLDILOCKS HELD / HOLD SEEN, STALL DSOV0 HELD, NO DIP, identity / die ID / host guard / preflight
/ plan checksum / fresh boot / power / runner transfer failure, Traceback, a runner `STOP:` line not covered below,
CONTROLLER OFF the second time, a park failure in two consecutive boots, MISTIMED / ABORT in two consecutive boots,
TEMP GATE FAILED three times in a row, an unexplained launcher exit. A park that fails right after a lost shot (the link is
dead; the next fresh boot recovers it) does not count by default; `--park-fail-mode strict` counts it. A held partial is
logged and the next plan densifies; `--stop-on-partial` stops instead. Every decision (plan, command, launch, result,
ingest, event, wait, stop) is a line in `decisions.jsonl` and on stdout. Exit 0 for budget / planner done / goldilocks /
dry run, 3 for a stop that needs review.

Never run a hunt next to another script that drives the same bench.

## Safety rules (unchanged by this tool)

- No PCIe secondary bus reset and no setpci retrain during a campaign.
- FUSECTRL first; nothing after a 0xBADF or 0xBAD0 read.
- Never sense a 0xFFFFFEFE (partial) state.
- No NVVDD probe while glitching (records with `conditions.nvvdd_probe` are excluded from every family).
- This tool never burns, never arms a burn and runs nothing but the launchers.

## Limitations

- Filtered wrapper logs lose information: they hide the firmware tag (pass `--firmware`) and the cycle line (wrapper
  boots are assumed fresh unless `--nocycle`); cell mode prints no under shots, so shots are rebuilt from cell
  summaries, `step i shot k`, TIPPED and release_at lines. The push2 launchers keep full per-boot logs, which the
  parser prefers.
- Boot ids use the date from `--date` (default: the log's mtime); a run across midnight needs care.
- The latch rule is a pulses x pw energy proxy; deeper codes may latch at lower energy. A latch is recoverable (power
  cycle) and every new one tightens the rule automatically.
- The compass phases assume locally monotone responses along delay and pw; noisy flips converge to an edge and then
  feed the drift loop.
- hunt.py trusts the launchers' own gates (fresh boot, identity, temperature, plan checksum) and reads their text
  output; a launcher whose wording changes needs the patterns in `hunt.py` (RULES) updated.
- `crc_closed` is treated as weak. On card E the healthy read is CRC 0x55 with DSOV 1, so a 0x77 after a shot is a
  change from baseline; the planner records it and keeps searching stronger.
