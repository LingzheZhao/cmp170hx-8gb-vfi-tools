# Running the bench autonomously with an LLM agent

The lab runs its glitch campaigns through an LLM agent. The owner gives the agent remote power control of the bench
host and a shell on it; the agent runs the tests itself, one boot per run, and writes up each run in a journal. The
scripts in `tools/runner/` are the ones it drives. This page mirrors the
[section on cmp.wiki](https://cmp.wiki/wiki/Voltage-glitch_unlock:_lab_method_on_a_Pico_2_W#Running_the_bench_autonomously_with_an_LLM_agent)
and maps each step to the code here.

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
   (`TMIN`, `TCAP`).
6. **The per-boot runner** (`bisect_shots.py`) fires the planned settings, many back-to-back shots per setting
   ("cells"), with a cap on shots per boot and a time limit per run. Every shot is logged with its parameters and
   its outcome.
7. **After every shot**: the firmware's reply (`pwd=1`, `e1=0xc2fa`, or the shot did not run), the controller's
   STATUS_WORD and READ_VOUT, then FUSECTRL first and the classifier. A lost shot gets a park. Partial states are
   counted and never sensed.
8. **Stops**: a poisoned shot, the controller-off latch, a shot that did not run, a failed park, a run of lost
   shots, or any outcome the runner does not recognise ends the run. The next run starts with a power cycle.
9. **Goldilocks**: the runner holds the state (no park). In this repository `bootrun.sh` then runs the `ON_HOLD`
   command if you set one, and otherwise stops and reports. In the lab the hold went to a separately reviewed burn
   script, and only for a card whose planned rows the owner had already approved; that script checked again before
   it wrote anything. No burn tooling is published here. Only a cold boot and a new read verify a burn.

## What stays with people

- Physical work: swapping cards, placing and checking the probes, the fan.
- Any burn approval, given per card for that card's reviewed plan.
- The agent asks before anything that cannot be undone.

## What this buys

Many boots and thousands of shots a night with nobody at the bench, every shot logged the same way, so cards and
settings can be compared on equal terms.

## The guard rails that matter most

- No secondary bus reset and no `setpci` link retrain during a campaign (`RETRAIN` and `SBR` are off by default).
- FUSECTRL first after every shot; stop on `0xBADF`.
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
