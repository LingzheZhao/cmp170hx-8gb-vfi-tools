# Running the bench autonomously with an LLM agent

The lab runs its glitch campaigns through an LLM agent. The owner gives the agent remote power control of the bench
host and a shell on it; the agent runs the tests itself, one boot per run, and writes up each run in a journal. The
scripts in `tools/runner/` are the ones it drives. This page mirrors the
[section on cmp.wiki](https://cmp.wiki/wiki/Voltage-glitch_unlock:_lab_method_on_a_Pico_2_W#Running_the_bench_autonomously_with_an_LLM_agent)
and maps each step to the code here. `tools/planner/hunt.py` can also run the plan, run, ingest loop by itself
(section "The hunter loop" below), and `tools/hunter/goldilocks_hunt.py` drives the strategy that finished cards G
and H (section "The goldilocks hunter").

## What the agent controls

- **Power**: Wake-on-LAN to switch the bench on and an OS poweroff to switch it off. On a board with a BMC, IPMI does
  both and confirms that the host is really off, with a hard power-off if a clean shutdown stalls (`power.sh`,
  `POWER=wol` or `ipmi`).
- **A shell on the bench host**, with the glitch tools, the Pico's serial console and the card's BAR0.

## One run, one boot (`tools/runner/bootrun.sh`)

1. **Power cycle**: clean shutdown, wait until the host is really off, wait a little longer, power on, wait for the
   host to answer. If the host does not go down, stop: it needs a hand on the power button.
2. **Fresh-boot guard**: refuse to run if the host has been up for more than 15 minutes (`FRESH_MAX`), so nothing
   from an earlier boot carries into the run.
3. **Plan checksum**: files that must not change between review and run (in the lab, the card's plan and the cold
   fuse dump it was built from) must match the reviewed copies by checksum (`PLAN_FILES`).
4. **Preflight and identity**: the I2C preflight must pass, and the card's fuse identity (GPC disable mask, current
   SM count and SM ceiling) must match the card the run was set up for. A mismatch stops the run.
5. **Temperature gate**: wait until the voltage controller's temperature reaches the run's minimum, up to a time cap
   (`TMIN`, `TCAP`); if it does not, no shot is fired.
6. **The per-boot runner** (`bisect_shots.py`) fires the planned settings, many back-to-back shots per setting
   ("cells"), with a cap on shots per boot and a time limit per run. Every shot is logged with its parameters and
   its outcome.
7. **After every shot**: the firmware's reply (`pwd=1`, `e1=0xc2fa`, or the shot did not run), the release timing
   (0 to 4 µs by the alarm, or the shot is not scored), the controller's STATUS_WORD and READ_VOUT, then a passive
   read with FUSECTRL first, CRC included, and the sentinel alias. A lost shot gets a park. A partial state is held
   and never sensed.
8. **Stops**: a poisoned shot, the controller-off latch, a shot that did not run, a mistimed shot, a failed park, a
   run of lost shots, or any outcome the runner does not recognise ends the run. The next run starts with a power
   cycle.
9. **Goldilocks**: the runner holds the state (no park). In this repository `bootrun.sh` then runs the `ON_HOLD`
   command if you set one, and otherwise stops and reports. In the lab the hold went to a separately reviewed burn
   script, and only for a card whose planned rows the owner had already approved; that script checked again before
   it wrote anything. No burn tooling is published here. Only a cold boot and a new read verify a burn.

## The hunter loop (`tools/planner/`)

The lab's agent first ran hand-written queues of launcher calls. `tools/planner/` holds the loop it grew into, with
no agent needed:

1. **Plan**: read the card's ledger (one record per shot, append-only) and propose the next shots for the chosen
   search family, under the never-rules: the card's floor, the runner's limits, no write across the release, and a
   latch rule learned from every controller-off shot.
2. **Launch**: one `firstshot.sh` run (a fresh boot per shot) or one `bootrun.sh` boot with `PARK_EACH=1`.
3. **Ingest**: parse the launcher's full log into the ledger.
4. **Stop rules**, per boot: a held goldilocks, a shot that did not run, identity, checksum, preflight, fresh-boot or
   transfer failures, a Python error, a second controller latch, park failures or mistimed shots in two boots in a
   row, three temperature gate failures, or an exit it does not recognise. Otherwise plan again, until the budget
   (`--max-boots`, `--max-hours`) runs out.

`hunt.py` is a dry run unless given `--execute`, logs every decision to `decisions.jsonl`, and runs nothing but the
launchers. The search methods and the ledger format are in `tools/planner/README.md`; the method behind them is in
`docs/METHOD.md`.

## The goldilocks hunter (`tools/hunter/goldilocks_hunt.py`)

The strategy that finished cards G and H (`docs/SWEET_SPOT.md`) as a tool an agent can drive phase by phase. It
plans, runs through `bootrun.sh`, and reads the logs back; it never senses, arms or burns. `goldilocks_hunt.py
strategy` prints the strategy and these rules.

The loop:

1. **Tally what is there.** `goldilocks_hunt.py tally <log dirs>` prints the map: counts per cell (depth, s1,
   pulses, width, restore, slew) with the first shot of every run left out, the register codes seen (GFW, DSOV, CRC,
   the mask, the sentinel), the edges between cells, the partial CRC codes, and a "Next" block. With no logs yet it
   says: start with the community train.
2. **Plan the phase the "Next" block names**, with the command it prints (`train`, `readmap --v N`, `init`,
   `slow --v N`, `dwell ...`), and `--out plan.json`. Every step is checked against push2's release rule and the
   runner's limits before it is written; `check` repeats that check on any step list.
3. **Dry run**: `goldilocks_hunt.py run plan.json --card <name>` prints the boots it would run. Read them.
4. **Run**: the same with `--execute --log-dir <dir>`. One `bootrun.sh` call per boot, a power cycle each time,
   `NO_ON_HOLD=1` so no hold hook can run, every boot's log kept. A tip (lost, stall) moves the rest of the plan to a
   new boot.
5. **Back to 1** with the new logs.

Stop rules: end the loop and report to the owner, with the log path, on

- **a goldilocks hold** (`>>> GOLDILOCKS HELD`, or a goldilocks signature in a stall). Leave the card as it is: no
  park, no shot, no power cycle. The owner runs the active sense, the window check and any burn, with their own tools.
  Never burn, and never run a sense yourself;
- **a partial or stall hold** (`>>> PARTIAL HELD`, `>>> STALL DSOV0 HELD`): read it, do not sense it;
- **poison** (`run` stops on it by default; `--on-poison continue` only if the owner set the night up that way:
  each poison costs a power cycle);
- **a controller latch** (`CONTROLLER OFF`, STATUS_WORD `0x2042`);
- **an unknown outcome**: a nack, NO DIP, a mistimed shot, a temperature gate or preflight failure, an exit code or
  a line you do not recognise, a Python error. `run` exits 0 when the plan is done, 3 when it stopped for one of
  these reasons (or a budget or `STOP` file), 4 on a hold, 2 when it refused the plan.

Ask the owner first, every time, before `--shoot-through` (`SHOOT_THROUGH_STALLS=1` breaks rule 2 of upstream
pico-ga100vfi: no `g`, `r`/`j` or FLR while GFW is `0x01`; `run` refuses such a plan without
`--owner-ok-shoot-through`), before `--on-poison continue`, before a floor below the plan's default, and before
anything physical.

## What stays with people

- Physical work: swapping cards, placing and checking the probes, the fan.
- Any burn approval, given per card for that card's reviewed plan, and the sense, window check and burn themselves.
- Turning on shoot-through stalls, or going on after a poison without stopping.
- The agent asks before anything that cannot be undone.

## What this buys

Many boots and thousands of shots a night with nobody at the bench, every shot logged the same way, so cards and
settings can be compared on equal terms.

## The guard rails that matter most

- No secondary bus reset and no `setpci` link retrain during a campaign (`RETRAIN` and `SBR` are off by default).
- FUSECTRL first after every shot; stop on `0xBADF` and `0xBAD0`.
- Never run a fuse sense on a state whose privilege mask reads `0xFFFFFEFE`.
- No NVVDD probe lead attached while glitching; verification shots only.
- Check that every shot really ran (`pwd=1`, `e1=0xc2fa`).
- Burns only from a verified goldilocks state, and only with the owner's explicit approval.

## Suggestions for the agent's instructions

Not from the wiki page; what this setup asks of whoever drives it:

- The settings file (`bench.conf`) and the card table, so the agent never types a host, address or card identity by
  hand.
- One command per run (`bootrun.sh <card> '<SEQ>'`) and the meaning of its last line and exit code.
- Which outcomes end the night (a poisoned or latched card is done until a power cycle; a card that keeps crashing
  at the same settings needs a person to look at it).
- Where the journal goes, and that every claim in it cites the run log.
- What it must never do: load a GPU driver near the card, write DEBUGCTRL, change the fuse plan, or burn without a
  per-card approval.
