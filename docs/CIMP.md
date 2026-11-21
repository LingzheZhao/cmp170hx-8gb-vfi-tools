# CiMP: one command for the glitch bench

`tools/cimp/cimp.py` (or `./cimp` at the repository root) puts the bench's pieces behind one command: a
configurator that works across machines and benches, a live read-only status panel, the goldilocks hunter wired to
that configuration, and the outcome ladder. Standard library only. Nothing in it senses, arms or burns fuses.

```sh
./cimp config                 # set up this machine's bench profile (asked on first use if there is no config)
./cimp config --test          # read-only checks: settings, log dir, ping, ssh, Pico port present, bench python, no nvidia
./cimp watch                  # live panel; --log for one line per change, --once to print it once
./cimp hunt overlap --out plan-overlap.json          # any goldilocks_hunt.py command, with this profile's settings
./cimp hunt run plan-overlap.json                    # dry run; add --execute to power-cycle and shoot
./cimp ladder                 # what each frame means
./cimp classify boot-01.log   # name the outcome of every shot in a runner log
```

## watch

A panel redrawn every second (the clock in the header ticks, so you can see it is alive):

- **PHASE** in plain words: POWER CYCLE (the bench is off on purpose between boots, usually 60 to 90 s), BENCH
  BOOTING, PREFLIGHT, TEMP GATE, SHOOTING (shot n of this boot), TRIPPED (with the reason in words), BOOT ENDED,
  FINISHED, IDLE, and a HOLD banner with a terminal bell when a goldilocks is held. How long it has been in that phase.
- **TEMP**: the last reading (READ_TEMP1 / 10, in °C), how old it is, rising or falling per minute, the gate band and
  the cap.
- **FAN**: a direct cue for whoever runs the fan by hand: fan DOWN a notch, fan UP, leave it, or "no live reading
  while the bench is off".
- **The last eight shots** as words (light, light-PLM, CRC44, over, CRC fault, under, lost, POISON, GOLDILOCKS) with
  depth, restore, delay and temperature, and a tally for the whole run. A poison the runner cleared with
  `POISON_RESET=1` shows as "POISON, cleared by a 100 ms reset", and the RUN line counts poison resets.

It reads only log files and pings the bench address while there is no log for the current boot (that is how it tells
a power cycle from a booting bench). It never opens the Pico's serial port. Between boots no file on disk is live, so
this inference is the only way to show the phase there.

Log layouts it reads: the hunter's (`<log_dir>/<run>/run.log` and `boot-NN.log`, written by `cimp hunt run` and
`goldilocks_hunt.py run`) and a queue layout (`<log_dir>/<name>.log` with `######## <name> boot N [...]` headers and
`<name>-bNN.raw` boot logs). It follows the newest run. If the logs live on another machine, set `runner_host`: `cimp
watch` then streams itself over ssh and runs there (it needs only `python3` on that machine).

Gate lines: `tools/runner/bootrun.sh` prints one `gate temp_raw=... want [TMIN, TSTART]` line per poll, so the panel
shows the card warming or cooling into the band.

## config

`cimp config` asks for every value the runner, the launchers and `watch` use, shows the default or current value in
brackets, validates each answer (port range, MAC format, choices) and asks again on a bad one. Enter keeps the value,
`-` clears it. Then it offers to add cards (the `card_<label>` functions `bootrun.sh` reads). It is offered
automatically the first time you run `watch` or `hunt` on a terminal with no config and no `CIMP_*` variables set.

| Command | What it does |
|---|---|
| `cimp config --show` | the effective settings and where each came from (default, profile, env, flag); secrets masked |
| `cimp config --json` | the same as JSON, for agents |
| `cimp config --test` | read-only checks (below); exit 1 if any fails |
| `cimp config --set KEY=VALUE` | set a value without questions; repeatable; `card.<label>.<key>=...` for cards |
| `cimp config --unset KEY` | back to the default |
| `cimp config --yes` | no questions: save what `--set` gave, defaults for the rest (agents, CI) |
| `cimp --profile NAME config ...` | work on another profile; `cimp config --list` lists them |
| `cimp config --write-bench-conf [PATH]` | generate `bench.conf`, `ssh_config` and the `ssh`/`scp` shims |
| `cimp config --store-password` | keep the ssh password in the system keyring (optional `keyring` module) |

Settings are read in this order, highest first: command-line flags (`watch --log-dir`), `CIMP_<KEY>` environment
variables (`CIMP_LOG_DIR`, `CIMP_BENCH_HOST`, `CIMP_TMIN_C`, ...), the profile, the defaults. `CIMP_PROFILE` picks the
profile, `CIMP_CONFIG` the config file.

The config is `~/.config/cimp/config.json` (`$XDG_CONFIG_HOME` is honoured), written with mode 0600 in a 0700
directory. The generated files go to `~/.config/cimp/profiles/<name>/` with the same modes. None of it belongs in a
repository; `tools/cimp/config.example.json` shows the format with placeholders.

### Settings

| Key | Default | What |
|---|---|---|
| `bench_host` | `` | bench host name or IP (the machine with the card, the Pico and the glitch boot) |
| `bench_user` | `root` | ssh user on the bench host (bootrun needs root there) |
| `bench_port` | `22` | ssh port of the bench host |
| `ssh_auth` | `key` | ssh login: key (a key file), agent (ssh-agent), password |
| `ssh_key` | `~/.ssh/id_ed25519` | ssh private key file (ssh_auth = key) |
| `jump_host` | `` | optional jump host for ssh, user@host[:port] (ProxyJump) |
| `ssh_host_keys` | `strict` | host key check: strict (known_hosts must have the bench) or accept-new (trust on first use) |
| `bench_addr` | `` | address to ping the bench at (default: bench_host) |
| `power` | `wol` | power control: wol, ipmi or manual (someone presses the button) |
| `wol_mac` | `` | bench NIC MAC address (power = wol) |
| `wol_broadcast` | `255.255.255.255` | Wake-on-LAN broadcast address(es), space separated |
| `ipmi_host` | `` | BMC address (power = ipmi) |
| `ipmi_user` | `` | BMC user (power = ipmi) |
| `ipmi_passfile` | `` | a 0600 file ipmitool -f reads the BMC password from (power = ipmi) |
| `ipmi_opts` | `-I lanplus` | extra ipmitool options |
| `offwait` | `15` | seconds the bench stays off before power on |
| `manual_wait` | `600` | power = manual: seconds to wait for the power button |
| `bench_dir` | `/opt/vfi` | directory on the bench host bootrun copies the runner into |
| `bench_py` | `/opt/vfi/venv/bin/python` | python on the bench host with the ga100vfi host package |
| `bench_taskset` | `` | optional CPU pinning prefix on the bench, e.g. taskset -c 2-7 |
| `pico_port` | `/dev/ttyACM0` | Pico serial device on the bench host (a /dev/serial/by-id link is better) |
| `glitch_cmdline` | `` | optional word that must be on the bench's /proc/cmdline (the glitch boot entry) |
| `mask_pciehp` | `0` | 1: mask pciehp interrupts on a hotplug root port before the run |
| `fresh_max` | `900` | refuse a run when the bench has been up longer than this, seconds |
| `runner_host` | `local` | where the hunter runs and its logs live: local, or an ssh destination |
| `repo_dir` | `` | path of this repository on the runner host (default: this checkout when local) |
| `log_dir` | `hunt-logs` | runner log directory (relative paths are relative to repo_dir) |
| `run_screens` | `` | screen/tmux session names that mean a run is active (empty: judge by log activity) |
| `on_hold` | `` | command run on the controller when a goldilocks is held (empty: stop and report) |
| `card` | `` | default card label (the bench.conf function card_<label>) |
| `tmin_c` | `39.5` | temperature gate, lowest start temperature, C (READ_TEMP1 / 10) |
| `tstart_c` | `40.6` | temperature gate, highest start temperature, C (0 = no upper bound) |
| `tmax_c` | `41.8` | watch: above this the fan cue says fan up now, C |
| `tcap_s` | `900` | seconds the gate waits for the band before giving up |
| `refresh_s` | `1.0` | watch refresh period, seconds |

Cards (one set per card label; `tag`, `gpc` and `smc` are required, the rest optional):

| Key | What |
|---|---|
| `card.<label>.tag` | label in the runner's log, e.g. card E |
| `card.<label>.gpc` | GPC_DISABLE as vfi_snap.py prints it on a cold boot, e.g. 0x00000023 |
| `card.<label>.smc` | the SM line vfi_snap.py prints, e.g. SM now=70 ceiling=110 |
| `card.<label>.bdf` | optional PCI address on the bench (empty: the only 10de:20c2 card) |
| `card.<label>.sentinel_expect` | optional healthy value of the 0x820410 fuse alias on a fresh boot, e.g. 0x1 |
| `card.<label>.plan_local` | optional dir of reviewed burn-plan files on the controller |
| `card.<label>.plan_remote` | optional bench dir whose copies must match them by sha256 |
| `card.<label>.plan_files` | optional plan file names, space separated |

### ssh, keys and passwords

CiMP writes an ssh config for the bench (host alias `cimp-bench`: host, user, port, key, jump host, a ControlMaster
so the many short ssh calls of a boot reuse one connection) and two shims, `ssh` and `scp`, that call the real tools
with that config. `cimp hunt run` puts the shims first on `PATH` and points `BENCH_CONF` at the generated
`bench.conf`, so the launchers need no change.

- **Key (recommended)** or **agent**: password logins are switched off for the bench alias.
- **Password**: never written anywhere by CiMP, and `cimp config --set ssh_password=...` is refused. At run time
  CiMP takes it from `CIMP_SSH_PASSWORD` or, if the optional `keyring` module is installed and you ran
  `cimp config --store-password`, from the system keyring, and hands it to `sshpass` through `SSHPASS` in the
  launcher's environment only. Without either, ssh asks on the terminal; since the bench reboots every boot, expect
  one prompt per boot. Use a key for unattended runs.
- **Host keys**: `ssh_host_keys` is `strict` by default (the bench must be in `known_hosts`; run
  `ssh -F ~/.config/cimp/profiles/<name>/ssh_config cimp-bench true` once). `accept-new` trusts on first use.
- The IPMI password stays in its own 0600 file (`ipmi_passfile`), as in `bench.conf`.

### config --test

Read-only: the settings are complete, the generated files and the config file are private, the hunter and runner are
present, the log directory is readable (here or on the runner host), the bench answers ping (fine to fail while it is
off), ssh to the bench works with this profile, and on the bench: the Pico port exists (`test -e`, the port is not
opened), the bench python exists, the bench directory exists, and no NVIDIA driver is loaded (a glitched card must
never meet the driver).

## hunt

`cimp hunt <anything>` is `tools/hunter/goldilocks_hunt.py <anything>`. For `run` it also generates the profile's
files, puts the ssh shims first on `PATH`, sets `BENCH_CONF`, the gate (`TMIN`/`TSTART` from `tmin_c`/`tstart_c`, raw
= °C x 10, unless already set), `TCAP`, the card (`--card` from `card` unless given) and a time-stamped log directory
under `log_dir`, which is what `cimp watch` follows.

## For agents

```sh
export CIMP_BENCH_HOST=bench.example.lan CIMP_WOL_MAC=aa:bb:cc:dd:ee:ff CIMP_LOG_DIR=/srv/hunt-logs
./cimp config --yes --set card=x --set card.x.tag="card X" --set card.x.gpc=0x00000023 \
      --set "card.x.smc=SM now=70 ceiling=110"
./cimp config --json             # masked; read it back
./cimp config --test             # exit 1 on any failed check
./cimp watch --log               # one line per change, easy to read in a loop
```

The stop rules in `docs/AUTONOMOUS_AGENT.md` apply unchanged: stop and report on any hold, latch or outcome you do not
know, and leave every burn decision to people.
