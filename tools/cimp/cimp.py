#!/usr/bin/env python3
"""CiMP: one command for the CMP 170HX 8 GB glitch bench. Standard library only.

  cimp config            set up (or change) this machine's bench profile, interactively; --yes for agents
  cimp config --test     read-only checks: config, log dir, ping, ssh, Pico port present, bench venv
  cimp watch             live, read-only status panel of the run (phase, temperature, fan cue, last shots)
  cimp hunt <args>       the goldilocks hunter (tools/hunter/goldilocks_hunt.py) with this profile's bench settings
  cimp ladder            the outcome ladder: what each frame means
  cimp classify          name the outcome of frames read from runner logs (stdin or files)

Nothing here senses, arms or burns fuses. `watch` and `config --test` never open the Pico's serial port: they read
log files and ask the bench host over ssh whether files exist. Only `cimp hunt run --execute` powers the bench and
shoots, through tools/runner/bootrun.sh, exactly like goldilocks_hunt.py run --execute.

Settings come from, highest first: command-line flags, CIMP_<KEY> environment variables (CIMP_LOG_DIR, CIMP_BENCH_HOST,
...), the profile in ~/.config/cimp/config.json (or $CIMP_CONFIG), then the defaults. --profile NAME or CIMP_PROFILE
picks the profile. Passwords are never written to disk: use an ssh key or agent; for password logins CiMP reads
CIMP_SSH_PASSWORD or an optional keyring entry at run time and hands it to sshpass.
"""
import argparse
import glob
import json
import os
import re
import shutil
import subprocess
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
TOOLS = os.path.dirname(HERE)
REPO = os.path.dirname(TOOLS)
VERSION = 1

# ------------------------------------------------------------------ settings
# (key, default, kind, help). kind: str, int, float, choice:a|b, path, mac, host, list. Taken from what
# tools/runner/bootrun.sh, power.sh, bench.conf and the hunter need, plus what `watch` needs.
SETTINGS = [
    ("bench_host", "", "host", "bench host name or IP (the machine with the card, the Pico and the glitch boot)"),
    ("bench_user", "root", "str", "ssh user on the bench host (bootrun needs root there)"),
    ("bench_port", 22, "int", "ssh port of the bench host"),
    ("ssh_auth", "key", "choice:key|agent|password", "ssh login: key (a key file), agent (ssh-agent), password"),
    ("ssh_key", "~/.ssh/id_ed25519", "path", "ssh private key file (ssh_auth = key)"),
    ("jump_host", "", "str", "optional jump host for ssh, user@host[:port] (ProxyJump)"),
    ("ssh_host_keys", "strict", "choice:strict|accept-new", "host key check: strict (known_hosts must have the "
     "bench) or accept-new (trust on first use)"),
    ("bench_addr", "", "str", "address to ping the bench at (default: bench_host)"),
    ("power", "wol", "choice:wol|ipmi|manual", "power control: wol, ipmi or manual (someone presses the button)"),
    ("wol_mac", "", "mac", "bench NIC MAC address (power = wol)"),
    ("wol_broadcast", "255.255.255.255", "str", "Wake-on-LAN broadcast address(es), space separated"),
    ("ipmi_host", "", "str", "BMC address (power = ipmi)"),
    ("ipmi_user", "", "str", "BMC user (power = ipmi)"),
    ("ipmi_passfile", "", "path", "a 0600 file ipmitool -f reads the BMC password from (power = ipmi)"),
    ("ipmi_opts", "-I lanplus", "str", "extra ipmitool options"),
    ("offwait", 15, "int", "seconds the bench stays off before power on"),
    ("manual_wait", 600, "int", "power = manual: seconds to wait for the power button"),
    ("bench_dir", "/opt/vfi", "str", "directory on the bench host bootrun copies the runner into"),
    ("bench_py", "/opt/vfi/venv/bin/python", "str", "python on the bench host with the ga100vfi host package"),
    ("bench_taskset", "", "str", "optional CPU pinning prefix on the bench, e.g. taskset -c 2-7"),
    ("pico_port", "/dev/ttyACM0", "str", "Pico serial device on the bench host (a /dev/serial/by-id link is better)"),
    ("glitch_cmdline", "", "str", "optional word that must be on the bench's /proc/cmdline (the glitch boot entry)"),
    ("mask_pciehp", 0, "int", "1: mask pciehp interrupts on a hotplug root port before the run"),
    ("fresh_max", 900, "int", "refuse a run when the bench has been up longer than this, seconds"),
    ("runner_host", "local", "str", "where the hunter runs and its logs live: local, or an ssh destination"),
    ("repo_dir", "", "str", "path of this repository on the runner host (default: this checkout when local)"),
    ("log_dir", "hunt-logs", "str", "runner log directory (relative paths are relative to repo_dir)"),
    ("run_screens", "", "list", "screen/tmux session names that mean a run is active (empty: judge by log activity)"),
    ("on_hold", "", "str", "command run on the controller when a goldilocks is held (empty: stop and report)"),
    ("card", "", "str", "default card label (the bench.conf function card_<label>)"),
    ("tmin_c", 39.5, "float", "temperature gate, lowest start temperature, C (READ_TEMP1 / 10)"),
    ("tstart_c", 40.6, "float", "temperature gate, highest start temperature, C (0 = no upper bound)"),
    ("tmax_c", 41.8, "float", "watch: above this the fan cue says fan up now, C"),
    ("tcap_s", 900, "int", "seconds the gate waits for the band before giving up"),
    ("refresh_s", 1.0, "float", "watch refresh period, seconds"),
]
CARD_KEYS = (("tag", "label in the runner's log, e.g. card E"),
             ("gpc", "GPC_DISABLE as vfi_snap.py prints it on a cold boot, e.g. 0x00000023"),
             ("smc", "the SM line vfi_snap.py prints, e.g. SM now=70 ceiling=110"),
             ("bdf", "optional PCI address on the bench (empty: the only 10de:20c2 card)"),
             ("sentinel_expect", "optional healthy value of the 0x820410 fuse alias on a fresh boot, e.g. 0x1"),
             ("plan_local", "optional dir of reviewed burn-plan files on the controller"),
             ("plan_remote", "optional bench dir whose copies must match them by sha256"),
             ("plan_files", "optional plan file names, space separated"),
             ("die_dump", "the 512-row dump among plan_files the plan was made from: its fuse rows 8-15 (die ID) must "
              "match the card before any shot"))
DEFAULTS = {k: d for k, d, _, _ in SETTINGS}
KIND = {k: t for k, _, t, _ in SETTINGS}
HELP = {k: h for k, _, _, h in SETTINGS}
SECRET_RE = re.compile(r"pass(word)?$|token|secret", re.I)
NEVER_STORE = re.compile(r"(^|_)(password|passwd|pass|token|secret)$", re.I)


def config_path():
    if os.environ.get("CIMP_CONFIG"):
        return os.path.expanduser(os.environ["CIMP_CONFIG"])
    base = os.environ.get("XDG_CONFIG_HOME") or os.path.join(os.path.expanduser("~"), ".config")
    return os.path.join(base, "cimp", "config.json")


def profile_dir(profile):
    return os.path.join(os.path.dirname(config_path()), "profiles", profile)


def load_file():
    p = config_path()
    if p == os.devnull or not os.path.exists(p) or os.path.getsize(p) == 0:
        return None
    try:
        with open(p) as f:
            data = json.load(f)
    except ValueError as e:
        raise SystemExit("cimp: %s is not valid JSON (%s); fix it or move it away and run cimp config" % (p, e))
    if not isinstance(data, dict) or "profiles" not in data:
        raise SystemExit("cimp: %s is not a CiMP config (no 'profiles')" % p)
    return data


def save_file(data):
    p = config_path()
    old = os.umask(0o077)
    try:
        os.makedirs(os.path.dirname(p), mode=0o700, exist_ok=True)
        tmp = p + ".tmp"
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w") as f:
            json.dump(data, f, indent=1, sort_keys=True)
            f.write("\n")
        os.replace(tmp, p)
        os.chmod(p, 0o600)
    finally:
        os.umask(old)
    return p


def convert(key, val):
    """Validate and convert one setting (raises ValueError with a readable reason)."""
    kind = KIND.get(key, "str")
    if val is None:
        return DEFAULTS.get(key, "")
    s = str(val).strip()
    if kind == "int":
        v = int(s)
        if key == "bench_port" and not 1 <= v <= 65535:
            raise ValueError("port must be 1..65535")
        if v < 0:
            raise ValueError("must be >= 0")
        return v
    if kind == "float":
        v = float(s)
        if v < 0:
            raise ValueError("must be >= 0")
        return v
    if kind.startswith("choice:"):
        opts = kind.split(":", 1)[1].split("|")
        if s not in opts:
            raise ValueError("one of %s" % ", ".join(opts))
        return s
    if kind == "mac":
        if s and not re.fullmatch(r"([0-9A-Fa-f]{2}[:-]?){5}[0-9A-Fa-f]{2}", s):
            raise ValueError("a MAC address like aa:bb:cc:dd:ee:ff")
        return s.lower()
    if kind == "host":
        if s and not re.fullmatch(r"[A-Za-z0-9._:\-\[\]]+", s):
            raise ValueError("a host name or IP address")
        return s
    if kind == "list":
        return ",".join(x.strip() for x in s.split(",") if x.strip())
    return s


def env_overrides():
    out = {}
    for k in DEFAULTS:
        e = os.environ.get("CIMP_" + k.upper())
        if e is not None:
            out[k] = convert(k, e)
    return out


def resolve(profile, cli=None):
    """Effective settings: defaults < profile file < CIMP_* env < cli. Returns (settings, sources, cards)."""
    data = load_file() or {"profiles": {}}
    prof = dict(data["profiles"].get(profile, {}))
    cards = prof.pop("cards", {}) or {}
    eff, src = {}, {}
    for k in DEFAULTS:
        eff[k], src[k] = DEFAULTS[k], "default"
        if k in prof:
            eff[k], src[k] = convert(k, prof[k]), "profile"
    for k, v in env_overrides().items():
        eff[k], src[k] = v, "env CIMP_" + k.upper()
    for k, v in (cli or {}).items():
        if v is not None:
            eff[k], src[k] = convert(k, v), "flag"
    return eff, src, cards


def mask(key, val):
    if SECRET_RE.search(key) and val:
        return "********"
    return val


def password_status(profile):
    if os.environ.get("CIMP_SSH_PASSWORD"):
        return "from CIMP_SSH_PASSWORD (masked)"
    kr = _keyring()
    if kr is not None:
        try:
            if kr.get_password("cimp", profile):
                return "in the system keyring (masked)"
        except Exception:
            pass
    return "not stored (ssh asks at login)"


def _keyring():
    try:
        import keyring  # optional, never required
        return keyring
    except Exception:
        return None


def ssh_password(profile):
    if os.environ.get("CIMP_SSH_PASSWORD"):
        return os.environ["CIMP_SSH_PASSWORD"]
    kr = _keyring()
    if kr is not None:
        try:
            return kr.get_password("cimp", profile)
        except Exception:
            return None
    return None


# ------------------------------------------------------------------ generated files: ssh config, shims, bench.conf
def write_private(path, text, mode=0o600):
    old = os.umask(0o077)
    try:
        os.makedirs(os.path.dirname(path), mode=0o700, exist_ok=True)
        fd = os.open(path + ".tmp", os.O_WRONLY | os.O_CREAT | os.O_TRUNC, mode)
        with os.fdopen(fd, "w") as f:
            f.write(text)
        os.replace(path + ".tmp", path)
        os.chmod(path, mode)
    finally:
        os.umask(old)


def real_tool(name, skip_dir):
    for d in os.environ.get("PATH", "").split(os.pathsep):
        if os.path.abspath(d) == os.path.abspath(skip_dir):
            continue
        c = os.path.join(d, name)
        if os.path.isfile(c) and os.access(c, os.X_OK):
            return c
    return "/usr/bin/" + name


def shell_q(s):
    return "'" + str(s).replace("'", "'\"'\"'") + "'"


def generate(profile, eff, cards):
    """Write <profile dir>/ssh_config, bin/ssh, bin/scp and bench.conf. Returns their paths."""
    pd = profile_dir(profile)
    bindir = os.path.join(pd, "bin")
    cfg = os.path.join(pd, "ssh_config")
    lines = ["# written by cimp for profile %s; edit with `cimp config`, not by hand" % profile,
             "Host cimp-bench",
             "  HostName %s" % (eff["bench_host"] or "bench.invalid"),
             "  User %s" % eff["bench_user"],
             "  Port %d" % eff["bench_port"],
             "  ConnectTimeout 10",
             "  ServerAliveInterval 15",
             "  ControlMaster auto",
             "  ControlPath %s" % os.path.join(pd, "cm-%C"),
             "  ControlPersist 20m",
             "  StrictHostKeyChecking %s" % ("yes" if eff["ssh_host_keys"] == "strict" else "accept-new")]
    if eff["ssh_auth"] == "key":
        lines += ["  IdentityFile %s" % os.path.expanduser(eff["ssh_key"]), "  IdentitiesOnly yes",
                  "  PasswordAuthentication no"]
    elif eff["ssh_auth"] == "agent":
        lines += ["  IdentitiesOnly no", "  PasswordAuthentication no"]
    else:
        lines += ["  PreferredAuthentications password,keyboard-interactive", "  PubkeyAuthentication no"]
    if eff["jump_host"]:
        lines.append("  ProxyJump %s" % eff["jump_host"])
    write_private(cfg, "\n".join(lines) + "\n")
    for tool in ("ssh", "scp"):
        real = real_tool(tool, bindir)
        shim = ("#!/bin/sh\n# written by cimp: %s with this profile's ssh_config; a password, if any, comes from "
                "SSHPASS at run time\n"
                "if [ -n \"${SSHPASS:-}\" ] && command -v sshpass >/dev/null 2>&1; then exec sshpass -e %s -F %s \"$@\"; fi\n"
                "exec %s -F %s \"$@\"\n") % (tool, shell_q(real), shell_q(cfg), shell_q(real), shell_q(cfg))
        write_private(os.path.join(bindir, tool), shim, 0o700)
    bc = os.path.join(pd, "bench.conf")
    q = shell_q
    t = ["# shellcheck shell=bash disable=SC2034",
         "# written by cimp for profile %s (tools/runner/bench.conf.example explains every setting)" % profile,
         "BENCH_SSH=cimp-bench",
         "BENCH_ADDR=%s" % q(eff["bench_addr"] or eff["bench_host"]),
         "POWER=${POWER:-%s}" % eff["power"],
         "WOL_MAC=%s" % q(eff["wol_mac"]),
         "WOL_BROADCAST=%s" % q(eff["wol_broadcast"]),
         "IPMI_HOST=%s" % q(eff["ipmi_host"]),
         "IPMI_USER=%s" % q(eff["ipmi_user"]),
         "IPMI_PASSFILE=%s" % q(os.path.expanduser(eff["ipmi_passfile"]) if eff["ipmi_passfile"] else ""),
         "IPMI_OPTS=%s" % q(eff["ipmi_opts"]),
         "OFFWAIT=${OFFWAIT:-%d}" % eff["offwait"],
         "MANUAL_WAIT=${MANUAL_WAIT:-%d}" % eff["manual_wait"],
         "BENCH_DIR=%s" % q(eff["bench_dir"]),
         "BENCH_PY=%s" % q(eff["bench_py"]),
         "BENCH_TASKSET=%s" % q(eff["bench_taskset"]),
         "PICO_PORT=%s" % q(eff["pico_port"]),
         "GLITCH_CMDLINE=%s" % q(eff["glitch_cmdline"]),
         "MASK_PCIEHP=%d" % eff["mask_pciehp"],
         "FRESH_MAX=%d" % eff["fresh_max"],
         "LOG_DIR=%s" % q(abs_log_dir(eff)),
         "ON_HOLD=%s" % q(eff["on_hold"]), ""]
    for label, c in sorted(cards.items()):
        if not re.fullmatch(r"[A-Za-z0-9_]+", label):
            raise SystemExit("card label %r: letters, digits and _ only" % label)
        t.append("card_%s() {" % label)
        for k, _h in CARD_KEYS:
            t.append("  %s=%s" % (k.upper() if k not in ("bdf",) else "CARD_BDF", q(c.get(k, ""))))
        t.append("}")
    write_private(bc, "\n".join(t) + "\n")
    return {"ssh_config": cfg, "bin": bindir, "bench_conf": bc}


def abs_log_dir(eff):
    d = os.path.expanduser(eff["log_dir"])
    if os.path.isabs(d) or eff["runner_host"] != "local":
        return d
    return os.path.join(eff["repo_dir"] or os.getcwd(), d)


# ------------------------------------------------------------------ cimp config
def show(profile, eff, src, cards, as_json):
    out = {"config_file": config_path(), "profile": profile, "settings": {}, "cards": cards,
           "ssh_password": password_status(profile) if eff["ssh_auth"] == "password" else "not used"}
    for k in DEFAULTS:
        out["settings"][k] = {"value": mask(k, eff[k]), "source": src[k]}
    if as_json:
        print(json.dumps(out, indent=1, sort_keys=True))
        return 0
    print("CiMP profile %s  (%s)" % (profile, config_path()))
    for k in DEFAULTS:
        print("  %-15s %-34s %s" % (k, str(mask(k, eff[k]))[:34], src[k]))
    print("  ssh password:   %s" % out["ssh_password"])
    for label, c in sorted(cards.items()):
        print("  card %s: %s" % (label, ", ".join("%s=%s" % (k, c.get(k)) for k, _ in CARD_KEYS if c.get(k))))
    return 0


def ask(key, cur):
    kind = KIND[key]
    opts = kind.split(":", 1)[1].replace("|", "/") if kind.startswith("choice:") else ""
    while True:
        try:
            ans = input("%s%s [%s]: " % (HELP[key], (" (%s)" % opts) if opts else "", cur))
        except EOFError:
            ans = ""
        ans = ans.strip()
        if ans == "":
            return convert(key, cur)
        if ans == "-":
            return convert(key, DEFAULTS[key]) if DEFAULTS[key] != "" else ""
        try:
            return convert(key, ans)
        except ValueError as e:
            print("  not valid: %s" % e)


def interactive(profile, prof):
    print("CiMP configuration, profile %s. Enter keeps the value in brackets, '-' clears it." % profile)
    print("Passwords are never asked for or stored here.")
    out = dict(prof)
    skip = set()
    for k in DEFAULTS:
        if k in skip:
            continue
        cur = out.get(k, DEFAULTS[k])
        out[k] = ask(k, cur)
        if k == "ssh_auth" and out[k] != "key":
            skip.add("ssh_key")
        if k == "power":
            skip |= {"wol_mac", "wol_broadcast"} if out[k] != "wol" else set()
            skip |= {"ipmi_host", "ipmi_user", "ipmi_passfile", "ipmi_opts"} if out[k] != "ipmi" else set()
            skip |= {"manual_wait"} if out[k] != "manual" else set()
    cards = dict(out.pop("cards", {}) or {})
    while True:
        try:
            label = input("add or change a card (its bench.conf label, e.g. e; Enter to finish): ").strip()
        except EOFError:
            label = ""
        if not label:
            break
        if not re.fullmatch(r"[A-Za-z0-9_]+", label):
            print("  letters, digits and _ only")
            continue
        c = dict(cards.get(label, {}))
        for k, h in CARD_KEYS:
            try:
                v = input("  %s [%s]: " % (h, c.get(k, ""))).strip()
            except EOFError:
                v = ""
            if v == "-":
                c.pop(k, None)
            elif v:
                c[k] = v
        cards[label] = c
    out["cards"] = cards
    return out


def check_required(eff, cards):
    probs = []
    if not eff["bench_host"]:
        probs.append("bench_host is empty")
    if eff["power"] == "wol" and not eff["wol_mac"]:
        probs.append("power = wol needs wol_mac")
    if eff["power"] == "ipmi" and not (eff["ipmi_host"] and eff["ipmi_user"] and eff["ipmi_passfile"]):
        probs.append("power = ipmi needs ipmi_host, ipmi_user and ipmi_passfile")
    if eff["ssh_auth"] == "key" and not eff["ssh_key"]:
        probs.append("ssh_auth = key needs ssh_key")
    if eff["tstart_c"] and eff["tstart_c"] < eff["tmin_c"]:
        probs.append("tstart_c (%.1f) is below tmin_c (%.1f)" % (eff["tstart_c"], eff["tmin_c"]))
    for label, c in cards.items():
        for k in ("tag", "gpc", "smc"):
            if not c.get(k):
                probs.append("card %s needs %s" % (label, k))
    return probs


def run_checks(profile, eff, cards):
    """Read-only environment checks. Returns the number of failures."""
    fails = 0

    def res(name, ok, detail=""):
        nonlocal fails
        tag = {True: "PASS", False: "FAIL", None: "SKIP"}[ok]
        fails += ok is False
        print("%s  %-34s %s" % (tag, name, detail))
    probs = check_required(eff, cards)
    res("settings complete", not probs, "; ".join(probs))
    gen = generate(profile, eff, cards)
    res("generated ssh_config, shims, bench.conf", True, os.path.dirname(gen["bench_conf"]))
    perms = oct(os.stat(config_path()).st_mode & 0o777) if os.path.exists(config_path()) else "no file (env only)"
    res("config file private (0600)", perms in ("0o600", "no file (env only)"), perms)
    res("hunter and runner present", os.path.exists(os.path.join(TOOLS, "hunter", "goldilocks_hunt.py"))
        and os.path.exists(os.path.join(TOOLS, "runner", "bootrun.sh")), TOOLS)
    if eff["runner_host"] == "local":
        d = abs_log_dir(eff)
        res("log dir readable", os.path.isdir(d) and os.access(d, os.R_OK), d if os.path.isdir(d) else d + " (missing: created on the first run)")
    else:
        rc = subprocess.run(["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=8", eff["runner_host"],
                             "test -r %s" % shell_q(eff["log_dir"])], capture_output=True).returncode
        res("runner host log dir readable", rc == 0, "%s:%s" % (eff["runner_host"], eff["log_dir"]))
    addr = eff["bench_addr"] or eff["bench_host"]
    if addr and shutil.which("ping"):
        rc = subprocess.run(["ping", "-c1", "-W2", addr], capture_output=True).returncode
        res("bench answers ping", rc == 0, addr + ("" if rc == 0 else " (fine if the bench is off right now)"))
    else:
        res("bench answers ping", None, "no address or no ping")
    if not eff["bench_host"]:
        res("ssh to the bench", None, "no bench_host")
        return fails
    env = dict(os.environ, PATH=gen["bin"] + os.pathsep + os.environ.get("PATH", ""))
    pw = ssh_password(profile) if eff["ssh_auth"] == "password" else None
    if pw:
        env["SSHPASS"] = pw
    batch = [] if eff["ssh_auth"] == "password" else ["-o", "BatchMode=yes"]   # BatchMode turns password auth off
    probe = ("echo up; test -e %s && echo pico || echo nopico; test -x %s && echo py || echo nopy; "
             "test -d %s && echo dir || echo nodir; lsmod | grep -q '^nvidia ' && echo nvidia || echo nonvidia"
             % (shell_q(eff["pico_port"]), shell_q(eff["bench_py"]), shell_q(eff["bench_dir"])))
    try:
        p = subprocess.run(["ssh"] + batch + ["cimp-bench", probe], env=env, capture_output=True, text=True, timeout=40)
        out = p.stdout.split()
    except subprocess.TimeoutExpired:
        out, p = [], None
    err = ((p.stderr.strip().splitlines() or [""])[-1][:80] if p else "timeout") if "up" not in out else ""
    if "Host key verification failed" in err:
        err += " (run `ssh -F %s cimp-bench true` once to accept the key, or ssh_host_keys=accept-new)" % gen["ssh_config"]
    res("ssh to the bench", "up" in out, "%s@%s:%d (%s)" % (eff["bench_user"], eff["bench_host"], eff["bench_port"],
                                                          eff["ssh_auth"]) + ("  " + err if err else ""))
    if "up" in out:
        res("Pico port present on the bench", "pico" in out, eff["pico_port"] + " (existence only; the port is not opened)")
        res("bench python present", "py" in out, eff["bench_py"])
        res("bench dir present", ("dir" in out) or None, eff["bench_dir"] + ("" if "dir" in out else " (created on the first run)"))
        res("no nvidia driver loaded on the bench", "nonvidia" in out, "a glitched card must never meet the driver")
    return fails


def cmd_config(a):
    prof_name = a.profile
    data = load_file()
    if a.list:
        if not data:
            print("no config file at %s" % config_path())
            return 0
        for name in sorted(data["profiles"]):
            print("%s%s" % (name, "  (default)" if name == data.get("default_profile") else ""))
        return 0
    data = data or {"version": VERSION, "default_profile": prof_name, "profiles": {}}
    prof = dict(data["profiles"].get(prof_name, {}))
    changed = False
    for kv in a.set or []:
        if "=" not in kv:
            raise SystemExit("--set wants key=value, got %r" % kv)
        k, v = kv.split("=", 1)
        k = k.strip()
        if NEVER_STORE.search(k):
            raise SystemExit("cimp never stores passwords or tokens: use an ssh key or agent, CIMP_SSH_PASSWORD, or "
                             "`cimp config --store-password` (system keyring)")
        if k.startswith("card."):
            _, label, ck = (k.split(".", 2) + [""])[:3]
            if ck not in dict(CARD_KEYS) or not re.fullmatch(r"[A-Za-z0-9_]+", label or ""):
                raise SystemExit("card settings are card.<label>.<%s>" % "|".join(k for k, _ in CARD_KEYS))
            prof.setdefault("cards", {}).setdefault(label, {})[ck] = v.strip()
        else:
            if k not in DEFAULTS:
                raise SystemExit("unknown setting %r (cimp config --show lists them)" % k)
            try:
                prof[k] = convert(k, v)
            except ValueError as e:
                raise SystemExit("%s: %s" % (k, e))
        changed = True
    for k in a.unset or []:
        if k.startswith("card."):
            parts = k.split(".", 2)
            if len(parts) == 2:
                prof.get("cards", {}).pop(parts[1], None)
            else:
                prof.get("cards", {}).get(parts[1], {}).pop(parts[2], None)
        else:
            prof.pop(k, None)
        changed = True
    interactive_run = not (a.yes or a.show or a.json or a.test or a.set or a.unset or a.write_bench_conf
                           or a.store_password)
    if interactive_run:
        if not sys.stdin.isatty():
            raise SystemExit("cimp config: no terminal; use --yes with --set key=value (or CIMP_* variables)")
        prof = interactive(prof_name, prof)
        changed = True
    if changed or (a.yes and prof_name not in data["profiles"]):
        data["profiles"][prof_name] = prof
        data.setdefault("default_profile", prof_name)
        data["version"] = VERSION
        print("saved profile %s to %s (mode 0600)" % (prof_name, save_file(data)))
    eff, src, cards = resolve(prof_name)
    if a.store_password:
        kr = _keyring()
        if kr is None:
            raise SystemExit("no keyring module here: use CIMP_SSH_PASSWORD at run time, or ssh's own prompt")
        import getpass
        kr.set_password("cimp", prof_name, getpass.getpass("ssh password for %s@%s (kept in the system keyring): "
                                                           % (eff["bench_user"], eff["bench_host"])))
        print("stored in the system keyring (service cimp, profile %s)" % prof_name)
    if a.write_bench_conf:
        gen = generate(prof_name, eff, cards)
        if a.write_bench_conf != "-":
            shutil.copyfile(gen["bench_conf"], a.write_bench_conf)
            os.chmod(a.write_bench_conf, 0o600)
            print("wrote %s (BENCH_SSH=cimp-bench needs the ssh shims in %s first on PATH, or `cimp hunt run`)"
                  % (a.write_bench_conf, gen["bin"]))
        else:
            print("generated %s" % ", ".join("%s=%s" % kv for kv in gen.items()))
    if a.show or a.json:
        return show(prof_name, eff, src, cards, a.json)
    if a.test:
        f = run_checks(prof_name, eff, cards)
        print("%d check(s) failed" % f if f else "all checks passed")
        return 1 if f else 0
    probs = check_required(eff, cards)
    for p in probs:
        print("note: %s" % p)
    return 0


# ------------------------------------------------------------------ watch (read-only panel)
R_GLITCH = re.compile(r"^\s+glitch vd=(\d+) .*?vr=(\d+) .*?pw=(\d+) gap=(\d+) delay=(\d+) .*?dither=(\d)")
R_TEMP = re.compile(r"^\s+temp_raw=(\d+)")
R_GATE = re.compile(r"gate temp_raw=(\d+|unreadable) want \[(\d+), (\d+)\]")
R_FRAME = re.compile(r"DSOV=(0x[0-9a-f]+|-) CRC=(0x[0-9a-f]+|-) PLM=(0x[0-9a-f]+|-)")
R_STEP = re.compile(r"^step (\d+)(?: shot \d+)? v=\d+ .*?f=\d+: (.*)$")
R_PRESET = re.compile(r"^\s+poison reset \(100 ms PEX_RST\): (.*)$")
R_CELL = re.compile(r"^cell (\d+)(?: \([^)]*\))? v=(\d+) td=-?\d+ np=(\d+) pw=(\d+) vr=(\d+): \{(.*)\}")
R_SHOT = re.compile(r"^SHOT (.*?) \| (.*)$")
R_TMAX = re.compile(r"\bTMAX=(\d+)")
R_QHDR = re.compile(r"^######## (\S+) boot (\d+) \[(.*)\] cont \d+ n=(\d+) (\d\d:\d\d:\d\d)")
R_QDONE = re.compile(r"^######## (\S+) done (\d\d:\d\d:\d\d)")
R_HRUN = re.compile(r"^######## RUN (\S+): (.*), (\d+) steps, card (\S+)")
R_HBOOT = re.compile(r"^######## boot (\d+): plan steps \d+\.\.\d+(?: of \d+)? (\d\d:\d\d:\d\d)")
R_HEND = re.compile(r"^######## RUN (done|stopped: .*) after (\d+) boots")

LADDER = [
    ("GOLDILOCKS", "DSOV 0, CRC 0x55, PLM 0xffffffff: the write window. The only frame anyone should burn on, after "
                   "an active sense shows it again."),
    ("light", "DSOV 1, CRC 0x55, PLM clean: the dip disturbed the boot (often a GFW stall, sentinel 0) but not DSOV."),
    ("light-PLM", "DSOV 1, CRC 0x55, PLM disturbed: a little heavier."),
    ("CRC fault", "DSOV 1 with CRC 0x57 / 0x75 / 0x77: the dip hit fuse block 0 and/or 2 during the read."),
    ("CRC44", "DSOV 0 but CRC 0x44 (blocks 0 and 2 never checked). Looks close; an active sense keeps CRC at 0x44 and "
              "the write-window check fails. Not burnable."),
    ("over", "DSOV 0, CRC 0x00, PLM 0xfffffcee-like: blocks wiped. Too much."),
    ("poison", "FUSECTRL 0xBADF/0xBAD0 or the sentinel alias wrong: the fuse controller is wedged; power cycle "
               "(with POISON_RESET=1 the runner clears a FUSECTRL poison with a 100 ms reset once GFW is ready)."),
    ("lost", "the GPU fell off the bus; the runner recovers or the boot ends."),
    ("under", "nothing happened; a clean boot."),
]


def classify(shot):
    st = shot.get("step", "") or ""
    kind = shot.get("kind")
    if kind == "mistimed" or st.startswith("MISTIMED"):   # never scored, whatever frame it showed (the runner stops)
        return "mistimed", "MISTIMED (not scored)"
    if "GOLDILOCKS" in (shot.get("hold") or "") or st.startswith("GOLDILOCKS") or kind == "goldilocks":
        return "GOLDILOCKS", "GOLDILOCKS (held for the active sense)"
    if shot.get("hold"):
        return "HOLD", "HOLD: " + shot["hold"]
    if "hidden-poison" in st or "poisoned" in st or shot.get("poison") or kind == "poison":
        r_ = shot.get("reset")
        if r_ == "healthy_locked":
            return "poison", "POISON, cleared by a 100 ms reset"
        if r_:
            return "poison", "POISON, the reset did not clear it (%s)" % r_[:40]
        return "poison", "POISON (controller wedged)"
    # the cell counts ride on the cell's last shot: use them only for a shot that carries nothing of its own
    d = {} if (kind or shot.get("frame") or st) else shot.get("cell", {})
    if d.get("lost") or st.strip().startswith("lost") or kind == "lost":
        return "lost", "lost link"
    fr = shot.get("frame")
    tag = " [stall]" if shot.get("stall") else ""
    if fr and fr[0] != "-" and fr[1] != "-" and fr[2] != "-":
        dsov, crc, plm = int(fr[0], 16), int(fr[1], 16), int(fr[2], 16)
        if dsov == 0 and crc == 0x55 and plm == 0xFFFFFFFF:
            return "GOLDILOCKS", "GOLDILOCKS  DSOV 0 / CRC 55 / PLM ff"
        if dsov == 0 and crc == 0x44:
            return "CRC44", "CRC44 (DSOV 0, blocks 0/2 skipped)" + tag
        if dsov == 0 and crc == 0x00:
            return "over", "over (DSOV 0, blocks wiped, PLM %08x)" % plm + tag
        if dsov == 0:
            return "dsov0", "DSOV 0 with CRC %02x" % crc + tag
        if crc == 0x55 and plm == 0xFFFFFFFF:
            return "light", "light (clean blocks, DSOV 1)" + tag
        if crc == 0x55:
            return "lightPLM", "light, PLM disturbed %08x" % plm + tag
        return "crcfault", "CRC fault %02x (DSOV 1)" % crc + tag
    if kind == "poison":
        return "poison", "POISON (controller wedged)"
    if d.get("booting") or "booting" in st or kind == "stall":
        return "booting", "GPU still booting (no read)"
    if d.get("under") or kind == "under":
        return "under", "under (no effect)"
    if d.get("partial") or kind == "partial":
        return "partial", "partial"
    return "other", (st[:40] or kind or "other")


_cache = {}


def lines_of(path):
    try:
        st = os.stat(path)
    except OSError:
        return []
    key = (st.st_mtime_ns, st.st_size)
    hit = _cache.get(path)
    if hit and hit[0] == key:
        return hit[1]
    try:
        with open(path, errors="replace") as fh:
            ls = fh.read().splitlines()
    except OSError:
        return []
    _cache[path] = (key, ls)
    return ls


def mtime(path):
    try:
        return os.stat(path).st_mtime
    except OSError:
        return None


def parse_boot(lines):
    b = {"phase": None, "gate": None, "temps": [], "shots": [], "tmax": None, "trip": None, "result": None,
         "end": False, "hold": None}
    cur = None
    for i, ln in enumerate(lines):
        m = R_GATE.search(ln)
        if m:
            if m.group(1) != "unreadable":
                b["temps"].append((i, int(m.group(1))))
            hi = int(m.group(3))
            b["gate"] = (int(m.group(2)), hi if hi < 99999 else None)
            b["phase"] = "gate"
            continue
        if ln.startswith("run env:"):
            m = R_TMAX.search(ln)
            if m:
                b["tmax"] = int(m.group(1))
        if ln.startswith(("pico v:", "PLAN SHA", "PREFLIGHT OK", "bench up")):
            b["phase"] = b["phase"] or "preflight"
        if ln.startswith("IDENTITY OK"):
            b["phase"] = "identity"
        if ln.startswith(("IDENTITY MISMATCH", "PREFLIGHT FAILED", "HOST GUARD", "NOT A FRESH BOOT")):
            b["phase"] = "stopped"
            b["result"] = ln.strip()[:90]
        if ln.startswith("START temp_raw"):
            b["phase"] = "shooting"
            m = re.search(r"temp_raw=(\d+)", ln)
            if m:
                b["temps"].append((i, int(m.group(1))))
        m = R_GLITCH.match(ln)
        if m:
            cur = {"v": int(m.group(1)), "vr": int(m.group(2)), "pw": int(m.group(3)), "delay": int(m.group(5)),
                   "dither": m.group(6) == "1", "line": i, "temp": None}
            b["shots"].append(cur)
            b["phase"] = "shooting"
            continue
        m = R_TEMP.match(ln)
        if m:
            t = int(m.group(1))
            b["temps"].append((i, t))
            if cur is not None:
                cur["temp"] = t
            continue
        if cur is not None and ("observed" in ln or "stall read" in ln or "SENT0 frame" in ln):
            m = R_FRAME.search(ln)
            if m and "-" not in m.groups():      # a later 'DSOV=- CRC=-' line never hides a real frame
                cur["frame"] = m.groups()
            if "stall read" in ln:
                cur["stall"] = True
            if "hidden_poison" in ln:
                cur["poison"] = True
        if ln.startswith(">>> ") or "HOLD SEEN" in ln:
            b["hold"] = ln.strip()[:90]
            if cur is not None:
                cur["hold"] = ln.strip()[4:70]
        m = R_SHOT.match(ln)
        if m and cur is not None:
            f = dict(x.split("=", 1) for x in m.group(1).split() if "=" in x)
            cur["kind"] = f.get("kind")
        m = R_STEP.match(ln)
        if m and cur is not None:
            cur["step"] = m.group(2)
        m = R_PRESET.match(ln)
        if m:
            res = m.group(1).strip()
            b["resets"] = b.get("resets", 0) + (1 if res == "healthy_locked" else 0)
            tgt = cur if cur is not None else (b["shots"][-1] if b["shots"] else None)
            if tgt is not None:
                tgt["reset"] = res
                if "key" in tgt:
                    tgt["key"], tgt["words"] = classify(tgt)
            continue
        m = R_CELL.match(ln)
        if m:
            d = {k: int(v) for k, v in re.findall(r"'(\w+)': (\d+)", m.group(6))}
            if cur is not None:
                cur["cell"] = d
                cur["key"], cur["words"] = classify(cur)
            cur = None
        if ln.startswith("TIPPED"):
            b["trip"] = ln
            b["phase"] = "tripped"
        if "TEMP CAP" in ln:
            b["phase"] = "toohot"
            b["result"] = ln.strip()[:90]
        if "TEMP GATE FAILED" in ln:
            b["phase"] = "gatefail"
            b["result"] = ln.strip()[:90]
        if ln.startswith("end temp_raw"):
            b["end"] = True
            m = re.search(r"temp_raw=(\d+)", ln)
            if m:
                b["temps"].append((i, int(m.group(1))))
        if ln.startswith("RESULT:"):
            b["result"] = ln[7:].strip()
            b["end"] = True
    # no cell line: single mode (SHOT line only) or the shot the runner stopped on (a hold, a trip)
    for s in b["shots"]:
        if "key" not in s and (s.get("kind") or s.get("hold") or s.get("step") or s.get("frame")):
            s["key"], s["words"] = classify(s)
    return b


def trip_words(t):
    if not t:
        return ""
    if "hidden-poison" in t:
        why = "fuse controller wedged (hidden poison)"
    elif "poison" in t:
        why = "fuse controller poisoned"
    elif "park failed" in t:
        why = "link lost and the park failed"
    elif "lost" in t:
        why = "too many lost shots"
    else:
        why = t[:60]
    m = re.search(r"\(v=(\d+) .*?vr=(\d+)", t)
    return why + (" at %s/vr%s" % m.groups() if m else "")


class Watch:
    def __init__(self, opts):
        self.o = opts
        self.seen_temp, self.temp_hist, self.shot_seen = {}, [], {}
        self.phase_key, self.phase_t = None, time.time()
        self.started = time.time()
        self._screens, self._ping = (0.0, ""), (0.0, None)

    # -- environment probes (read-only)
    def screens_active(self):
        names = [n for n in self.o["run_screens"].split(",") if n]
        if not names:
            return None
        if time.time() - self._screens[0] > 2:
            out = ""
            for cmd in (["screen", "-ls"], ["tmux", "ls"]):
                if shutil.which(cmd[0]):
                    try:
                        out += subprocess.run(cmd, capture_output=True, text=True, timeout=3).stdout
                    except Exception:
                        pass
            self._screens = (time.time(), out)
        out = self._screens[1]
        return any(re.search(r"(\.|^)%s(\s|:)" % re.escape(n), out, re.M) for n in names)

    def bench_up(self):
        addr = self.o["bench_addr"]
        if not addr or not shutil.which("ping"):
            return None
        if time.time() - self._ping[0] > 3:
            try:
                ok = subprocess.run(["ping", "-c1", "-W1", addr], capture_output=True, timeout=3).returncode == 0
            except Exception:
                ok = None
            self._ping = (time.time(), ok)
        return self._ping[1]

    # -- run discovery
    def find_run(self):
        d = self.o["log_dir"]
        cands = []
        for p in glob.glob(os.path.join(d, "*.log")) + glob.glob(os.path.join(d, "*-b[0-9][0-9].raw")):
            cands.append(("queue", p))
        for p in glob.glob(os.path.join(d, "*", "run.log")) + glob.glob(os.path.join(d, "*", "boot-[0-9][0-9].log")):
            cands.append(("hunter", os.path.dirname(p) + "/"))
        for p in glob.glob(os.path.join(d, "run.log")) + glob.glob(os.path.join(d, "boot-[0-9][0-9].log")):
            cands.append(("hunter", d.rstrip("/") + "/"))
        best, bt = None, -1
        for kind, p in cands:
            if kind == "hunter":
                files = glob.glob(os.path.join(p, "run.log")) + glob.glob(os.path.join(p, "boot-[0-9][0-9].log"))
                t = max([mtime(f) or 0 for f in files] or [0])
            else:
                t = mtime(p) or 0
            if t > bt:
                best, bt = (kind, p), t
        return best

    def snapshot(self):
        now = time.time()
        S = {"now": now}
        found = self.find_run()
        if not found:
            S["phase"] = ("IDLE", "no runner logs in %s" % self.o["log_dir"])
            S.update(run=None, shots=[], tally={}, boots=[], trips=[])
            return S
        kind, p = found
        if kind == "queue":
            run = os.path.basename(p)
            run = run[:-4] if run.endswith(".log") else re.sub(r"-b\d\d\.raw$", "", run)
            logp = os.path.join(self.o["log_dir"], run + ".log")
            log = lines_of(logp)
            hdrs = [h for h in (R_QHDR.match(x) for x in log) if h]
            done = any(R_QDONE.match(x) for x in log)
            raws = sorted(glob.glob(os.path.join(self.o["log_dir"], run + "-b[0-9][0-9].raw")))
            boot_no = int(hdrs[-1].group(2)) if hdrs else (int(raws[-1][-6:-4]) if raws else 1)
            S["label"] = hdrs[0].group(3) if hdrs else ""
            S["total"] = int(hdrs[0].group(4)) if hdrs else None
            cur_path = os.path.join(self.o["log_dir"], "%s-b%02d.raw" % (run, boot_no))
            stopfile = any("STOP file" in x for x in log[-3:])
            files = [logp] + raws
        else:
            run = os.path.basename(p.rstrip("/"))
            logp = os.path.join(p, "run.log")
            log = lines_of(logp)
            hr = next((R_HRUN.match(x) for x in log if R_HRUN.match(x)), None)
            hb = [h for h in (R_HBOOT.match(x) for x in log) if h]
            done = any(R_HEND.match(x) for x in log)
            raws = sorted(glob.glob(os.path.join(p, "boot-[0-9][0-9].log")))
            boot_no = int(hb[-1].group(1)) if hb else (int(raws[-1][-6:-4]) if raws else 1)
            S["label"] = ("%s: %s (card %s)" % (hr.group(1), hr.group(2), hr.group(4))) if hr else ""
            S["total"] = int(hr.group(3)) if hr else None
            cur_path = os.path.join(p, "boot-%02d.log" % boot_no)
            stopfile = os.path.exists(os.path.join(p, "STOP"))
            files = [logp] + raws
        S["run"], S["boot"] = run, boot_no
        boots = []
        for f in raws:
            b = parse_boot(lines_of(f))
            b["n"], b["path"] = int(f[-6:-4]), f
            boots.append(b)
        S["boots"] = boots
        cur = next((b for b in boots if b["path"] == cur_path), None)
        gate = next((b["gate"] for b in reversed(boots) if b["gate"]), None)
        if gate is None:
            gate = (int(round(self.o["tmin_c"] * 10)), int(round(self.o["tstart_c"] * 10)) if self.o["tstart_c"] else None)
        tmax = next((b["tmax"] for b in reversed(boots) if b["tmax"]), None) or int(round(self.o["tmax_c"] * 10))
        S["gate"], S["tmax"] = gate, tmax
        for b in boots:
            for idx, t in b["temps"]:
                k = (run, b["n"], idx)
                if k not in self.seen_temp:
                    first = now if now - self.started > 1.5 else None
                    self.seen_temp[k] = first
                    if first:
                        self.temp_hist.append((now, t))
        del self.temp_hist[:-300]
        last = None
        for b in boots:
            if b["temps"]:
                last = (b, b["temps"][-1])
        S["temp"] = last[1][1] if last else None
        if last:
            k = (run, last[0]["n"], last[1][0])
            S["temp_age"] = (now - self.seen_temp[k]) if self.seen_temp.get(k) else (now - (mtime(last[0]["path"]) or now))
        else:
            S["temp_age"] = None
        pts = [(t0, v) for t0, v in self.temp_hist if now - t0 <= 120]
        S["trend"] = ((pts[-1][1] - pts[0][1]) / 10.0) / ((pts[-1][0] - pts[0][0]) / 60.0) \
            if len(pts) >= 2 and pts[-1][0] - pts[0][0] >= 15 else None
        shots = []
        for b in boots:
            for s in b["shots"]:
                if "key" in s:
                    k = (run, b["n"], s["line"])
                    if k not in self.shot_seen:
                        self.shot_seen[k] = now if now - self.started > 1.5 else None
                    s["seen"], s["boot"] = self.shot_seen[k], b["n"]
                    shots.append(s)
        S["shots"] = shots
        tally = {}
        for s in shots:
            tally[s["key"]] = tally.get(s["key"], 0) + 1
        S["tally"] = tally
        S["trips"] = [b["trip"] for b in boots if b["trip"]]
        S["resets"] = sum(b.get("resets", 0) for b in boots)
        hold = next((b["hold"] for b in boots if b["hold"]), None)
        lm = max([mtime(f) or 0 for f in files] or [0])
        S["log_age"] = now - lm if lm else None
        act = self.screens_active()
        if act is None:
            act = not done and (S["log_age"] or 1e9) < 150
        S["active"] = act
        if hold:
            ph = ("HOLD", "GOLDILOCKS / HOLD. Do not touch the bench or the fan; read the held state before anything.")
        elif done and not act:
            ph = ("FINISHED", "run finished. %s" % ((boots[-1]["result"] or "") if boots else ""))
        elif stopfile and not act:
            ph = ("STOPPED", "run stopped by a STOP file")
        elif not act:
            ph = ("IDLE", "no run active (last log activity %s ago)" % ago(S["log_age"]))
        elif cur is None:
            up = self.bench_up()
            if up is False:
                ph = ("POWER CYCLE", "bench is OFF on purpose: clean power cycle before boot %d (normally 60-90 s)" % boot_no)
            elif up:
                ph = ("BENCH BOOTING", "bench is on the network; the runner is connecting for boot %d" % boot_no)
            else:
                ph = ("POWER CYCLE", "between boots: power cycle or bench booting (boot %d)" % boot_no)
        else:
            p_ = cur["phase"]
            if cur["end"] or p_ in ("tripped", "toohot", "gatefail", "stopped"):
                if p_ == "tripped":
                    ph = ("TRIPPED", "safety trip: %s. Power cycle next." % trip_words(cur["trip"]))
                elif p_ == "toohot":
                    ph = ("TOO HOT", "the runner stopped at the temperature cap. Fan UP.")
                elif p_ == "gatefail":
                    ph = ("GATE TIMEOUT", "the card never reached the gate band in time")
                elif p_ == "stopped":
                    ph = ("STOPPED", cur["result"] or "stopped")
                else:
                    ph = ("BOOT ENDED", "boot %d finished: %s" % (boot_no, (cur["result"] or "")[:70]))
            elif p_ == "shooting":
                ph = ("SHOOTING", "firing shot %d of this boot" % (len([s for s in cur["shots"] if "key" in s]) + 1))
            elif p_ == "gate":
                ph = ("TEMP GATE", "waiting for the card to reach the band before shooting")
            elif p_ in ("preflight", "identity"):
                ph = ("PREFLIGHT", "checking the Pico, the plan and the card identity")
            else:
                ph = ("BENCH BOOTING", "runner starting")
        S["phase"] = ph
        if self.phase_key != ph[0]:
            self.phase_key, self.phase_t = ph[0], now
        S["phase_age"] = now - self.phase_t if now - self.started > 1.5 else None
        return S


def ago(sec):
    if sec is None:
        return "?"
    sec = int(max(0, sec))
    return "%ds" % sec if sec < 90 else "%dm%02ds" % (sec // 60, sec % 60)


def degc(raw):
    return "--" if raw is None else "%.1f C" % (raw / 10.0)


def fan_cue(S):
    t, g, tmax, ph = S.get("temp"), S.get("gate"), S.get("tmax"), S["phase"][0]
    if t is None or not g:
        return ("2", "no reading yet")
    lo, hi = g
    hi = hi if hi else 10**6
    tr = S.get("trend")
    if ph in ("POWER CYCLE", "BENCH BOOTING", "IDLE", "FINISHED", "STOPPED"):
        return ("2", "no live reading while the bench is off or booting (last %s)" % degc(t))
    if ph == "HOLD":
        return ("35;1", "leave the fan exactly as it is")
    if ph == "TOO HOT" or (tmax and t >= tmax - 10):
        return ("31", "near or over the cap (%s): fan UP now" % degc(tmax))
    if ph in ("TEMP GATE", "PREFLIGHT"):
        if t < lo:
            if tr is not None and tr > 0.3:
                return ("33", "warming toward the band (need %s+), leave it" % degc(lo))
            return ("33", "too COOL to start (need %s+): fan DOWN a notch" % degc(lo))
        if t > hi:
            if tr is not None and tr < -0.3:
                return ("33", "cooling toward the band (need <= %s), leave it" % degc(hi))
            return ("33", "too WARM to start (need <= %s): fan UP a notch" % degc(hi))
        return ("32", "in the band, shooting should start")
    if t > hi + 8:
        return ("33", "running warm (band tops at %s): fan UP a notch" % degc(hi))
    if t < lo - 8:
        return ("33", "running cool (band starts at %s): fan DOWN a notch" % degc(lo))
    return ("32", "OK, in band")


PH_COLOR = {"SHOOTING": "32", "TEMP GATE": "33", "PREFLIGHT": "36", "BENCH BOOTING": "36", "POWER CYCLE": "36",
            "TRIPPED": "31", "TOO HOT": "31", "GATE TIMEOUT": "31", "BOOT ENDED": "33", "HOLD": "35;1",
            "FINISHED": "1", "IDLE": "2", "STOPPED": "2"}
KEY_COLOR = {"GOLDILOCKS": "35;1", "HOLD": "35;1", "light": "32", "lightPLM": "33", "CRC44": "36", "over": "33",
             "dsov0": "36", "crcfault": "33", "lost": "31", "poison": "31", "under": "2", "booting": "2",
             "mistimed": "33"}
ORDER = ["light", "lightPLM", "CRC44", "dsov0", "over", "crcfault", "under", "lost", "poison", "booting",
         "partial", "mistimed", "other"]
NAMES = {"lightPLM": "light-PLM", "dsov0": "DSOV0-other", "crcfault": "CRC-fault"}


def panel(S, tick, color):
    def c(code, s):
        return "\033[%sm%s\033[0m" % (code, s) if color else s
    W = max(60, min(110, shutil.get_terminal_size((100, 30)).columns))
    L = [c("1", "CiMP watch  %s   boot %s   %s %s" % (S.get("run") or "-", S.get("boot", "-"),
                                                  time.strftime("%H:%M:%S"), "|/-\\"[tick % 4]))]
    if S.get("label"):
        L.append(c("2", "  " + S["label"][:W - 4]))
    L.append(c("2", "-" * min(W, 78)))
    name, words = S["phase"]
    pa = (" (%s)" % ago(S["phase_age"])) if S.get("phase_age") is not None else ""
    L.append("PHASE  " + c(PH_COLOR.get(name, "1"), name) + pa)
    L.append("       " + words[:W - 8])
    t, tr = S.get("temp"), S.get("trend")
    trs = "" if tr is None else ("  rising %+.1f C/min" % tr if tr > 0.05 else "  falling %+.1f C/min" % tr if tr < -0.05 else "  steady")
    g = S.get("gate")
    band = ("   band %s - %s" % (degc(g[0]), degc(g[1]) if g[1] else "open")) if g else ""
    cap = ("   cap %s" % degc(S["tmax"])) if S.get("tmax") else ""
    L.append("TEMP   " + c("1", degc(t)) + (c("2", "  (%s old)" % ago(S.get("temp_age"))) if t is not None else "")
             + trs + band + cap)
    col, cue = fan_cue(S)
    L.append("FAN    " + c(col, cue))
    shots, tot = S.get("shots", []), S.get("total")
    L.append("RUN    %d shots fired%s   boots %d   trips %d%s" % (len(shots), (" of %d" % tot) if tot else "",
                                                                 len(S.get("boots", [])), len(S.get("trips", [])),
                                                                 ("   poison resets %d" % S["resets"]) if S.get("resets") else ""))
    L.append(c("2", "-" * min(W, 78)))
    L.append(c("2", "last shots (newest last)            v / restore / delay   temp   outcome"))
    for s in shots[-8:]:
        when = time.strftime("%H:%M:%S", time.localtime(s["seen"])) if s.get("seen") else "  (earlier)"
        knob = "%d/vr%d/d%d%s" % (s["v"], s["vr"], s["delay"], " dither" if s.get("dither") else "")
        L.append("  %-10s b%-2d %-24s %6s  %s" % (when, s.get("boot", 0), knob,
                 ("%.1f" % (s["temp"] / 10.0)) if s.get("temp") else "--", c(KEY_COLOR.get(s["key"], "0"), s["words"])))
    if not shots:
        L.append(c("2", "  (no shots yet this run)"))
    ta = S.get("tally", {})
    parts = ["%s %d" % (NAMES.get(k, k), ta[k]) for k in ORDER if ta.get(k)]
    L.append(c("2", "-" * min(W, 78)))
    gl = ta.get("GOLDILOCKS", 0)
    L.append("TALLY  " + ("  ".join(parts) if parts else "-") + "   " + c("35;1" if gl else "2", "goldilocks %d" % gl))
    if S.get("trips"):
        L.append(c("2", "trips: ") + "; ".join(trip_words(x) for x in S["trips"][-3:])[:W - 8])
    L.append(c("2", "log activity %s ago   |   read-only: log files + ping; never opens the Pico" % ago(S.get("log_age"))))
    return "\n".join(x[:W + 30] for x in L)


def log_line(S):
    name, words = S["phase"]
    _, cue = fan_cue(S)
    last = S["shots"][-1] if S.get("shots") else None
    ls = (" | last %d/vr%d: %s" % (last["v"], last["vr"], last["words"])) if last else ""
    return "%s  %-13s %-7s %s | fan: %s%s" % (time.strftime("%H:%M:%S"), name, degc(S.get("temp")), words[:60], cue, ls)


def watch_loop(opts, mode, color):
    w = Watch(opts)
    tick, last = 0, {"txt": None, "t": 0.0}
    belled = False
    if mode == "panel":
        sys.stdout.write("\033[?25l")
    try:
        while True:
            try:
                S = w.snapshot()
                out = panel(S, tick, color) if mode != "log" else None
            except Exception as e:      # keep watching whatever a log line looks like
                S, out = None, "cimp watch: parse error (%s); still watching" % e
            if S and (S["phase"][0] == "HOLD" or S.get("tally", {}).get("GOLDILOCKS")) and not belled:
                belled = True
                sys.stdout.write("\a")
            if mode == "once":
                print(out if out else log_line(S))
                return 0
            if mode == "log":
                if S:
                    ln = log_line(S)
                    key = ln.split(None, 1)[1]
                    if key != last["txt"] or time.time() - last["t"] > 20:
                        print(ln, flush=True)
                        last.update(txt=key, t=time.time())
                else:
                    print(out, flush=True)
            else:
                sys.stdout.write("\033[H" + "\n".join(x + "\033[K" for x in out.split("\n")) + "\033[J")
                sys.stdout.flush()
            tick += 1
            time.sleep(opts["refresh_s"])
    except KeyboardInterrupt:
        return 0
    finally:
        if mode == "panel":
            sys.stdout.write("\033[?25h\n")


def cmd_watch(a):
    eff, src, cards = resolve(a.profile, {"log_dir": a.log_dir})
    mode = "once" if a.once else ("log" if (a.log or not sys.stdout.isatty()) else "panel")
    color = sys.stdout.isatty() and not a.no_color
    if eff["runner_host"] != "local" and not a.here:
        # run this file on the runner host, where the logs are; it needs python3 there and nothing else
        args = ["watch", "--here", "--log-dir", eff["log_dir"]]
        args += ["--once"] if a.once else (["--log"] if mode == "log" else [])
        args += ["--no-color"] if not color else []
        env = " ".join("CIMP_%s=%s" % (k.upper(), shell_q(eff[k])) for k in
                       ("bench_addr", "run_screens", "tmin_c", "tstart_c", "tmax_c", "refresh_s") if eff[k] != "")
        if not eff["bench_addr"] and eff["bench_host"]:
            env += " CIMP_BENCH_ADDR=%s" % shell_q(eff["bench_host"])
        remote = "%s CIMP_CONFIG=/dev/null python3 - %s" % (env, " ".join(shell_q(x) for x in args))
        with open(os.path.abspath(__file__), "rb") as f:
            return subprocess.run(["ssh"] + (["-tt"] if mode == "panel" else []) + [eff["runner_host"], remote],
                                  stdin=f).returncode
    opts = {"log_dir": abs_log_dir(eff), "run_screens": eff["run_screens"],
            "bench_addr": eff["bench_addr"] or eff["bench_host"], "tmin_c": eff["tmin_c"], "tstart_c": eff["tstart_c"],
            "tmax_c": eff["tmax_c"], "refresh_s": max(0.2, eff["refresh_s"])}
    return watch_loop(opts, mode, color)


# ------------------------------------------------------------------ hunt (the hunter, wired to this profile)
def cmd_hunt(a, rest):
    sys.path.insert(0, os.path.join(TOOLS, "hunter"))
    import goldilocks_hunt as gh  # noqa: E402
    args = list(rest)
    if args and args[0] == "run":
        eff, src, cards = resolve(a.profile)
        probs = check_required(eff, cards)
        if probs and "--execute" in args:
            raise SystemExit("cimp hunt run --execute: fix the profile first (%s); cimp config" % "; ".join(probs))
        gen = generate(a.profile, eff, cards)
        os.environ["BENCH_CONF"] = gen["bench_conf"]
        os.environ["PATH"] = gen["bin"] + os.pathsep + os.environ.get("PATH", "")
        os.environ.setdefault("TMIN", str(int(round(eff["tmin_c"] * 10))))
        if eff["tstart_c"]:
            os.environ.setdefault("TSTART", str(int(round(eff["tstart_c"] * 10))))
        os.environ.setdefault("TCAP", str(eff["tcap_s"]))
        if eff["ssh_auth"] == "password":
            pw = ssh_password(a.profile)
            if pw:
                os.environ["SSHPASS"] = pw
        if eff["card"]:
            # argparse uses the last occurrence, so an explicit card takes precedence.
            args[1:1] = ["--card", eff["card"]]
        if "--log-dir" not in args and eff["runner_host"] == "local":
            stamp = time.strftime("%Y%m%dT%H%M%S")
            args += ["--log-dir", os.path.join(abs_log_dir(eff), "run-%s" % stamp)]
        print("cimp: bench.conf %s, ssh via %s, gate %s..%s raw" % (gen["bench_conf"], gen["bin"], os.environ["TMIN"],
                                                                     os.environ.get("TSTART", "open")))
    return gh.main(args)


# ------------------------------------------------------------------ ladder / classify
def cmd_ladder(a):
    print("The outcome ladder, light to heavy (docs/OVERLAP_EDGE.md, docs/SWEET_SPOT.md):")
    for k, v in LADDER:
        print("  %-11s %s" % (k, v))
    print("Hold rule: the runner holds a goldilocks (DSOV 0 + CRC 0x55 + PLM 0xffffffff) and, by default, every other\n"
          "readable DSOV 0 frame (a partial, exit 13; CRC 0x44 included) and a DSOV 0 read in a GFW stall (exit 15). Leave\n"
          "any hold as it is and report it; do not power-cycle it. Only a goldilocks is a write window: CRC 0x44 is not.")
    return 0


def cmd_classify(a):
    lines = []
    srcs = a.files or ["-"]
    for f in srcs:
        lines += (sys.stdin.read() if f == "-" else open(f, errors="replace").read()).splitlines()
    b = parse_boot(lines)
    shots = [s for s in b["shots"] if "key" in s]
    for s in shots:
        print("%d mV restore %d pw %d delay %d -> %s" % (s["v"], s["vr"], s["pw"], s["delay"], s["words"]))
    if not shots:
        for ln in lines:
            m = R_FRAME.search(ln)
            if m:
                print("%s -> %s" % (m.group(0), classify({"frame": m.groups()})[1]))
    return 0


# ------------------------------------------------------------------ CLI
def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    p = argparse.ArgumentParser(prog="cimp", description=__doc__.split("\n\n")[0],
                                formatter_class=argparse.RawDescriptionHelpFormatter, epilog=__doc__.split("\n\n", 1)[1])
    p.add_argument("--profile", default=None, help="profile name (default: CIMP_PROFILE, the config's default, 'default')")
    sub = p.add_subparsers(dest="cmd", required=True)
    sp = sub.add_parser("config", help="set up or show this machine's bench profile")
    sp.add_argument("--show", action="store_true", help="print the effective settings (secrets masked)")
    sp.add_argument("--json", action="store_true", help="the same as JSON (secrets masked)")
    sp.add_argument("--test", action="store_true", help="read-only environment checks")
    sp.add_argument("--set", action="append", metavar="KEY=VALUE", help="set a value (card.<label>.<key>=... for cards)")
    sp.add_argument("--unset", action="append", metavar="KEY", help="remove a value (back to the default)")
    sp.add_argument("--yes", action="store_true", help="no questions: save what --set gave, defaults for the rest")
    sp.add_argument("--list", action="store_true", help="list profiles")
    sp.add_argument("--write-bench-conf", nargs="?", const="-", metavar="PATH",
                    help="generate bench.conf, ssh_config and ssh/scp shims (optionally copy bench.conf to PATH)")
    sp.add_argument("--store-password", action="store_true", help="ask for the ssh password and keep it in the "
                    "system keyring (needs the optional keyring module; never written to the config)")
    sp = sub.add_parser("watch", help="live read-only status panel")
    sp.add_argument("--once", action="store_true", help="print the panel once and exit")
    sp.add_argument("--log", action="store_true", help="one line per change instead of a panel")
    sp.add_argument("--log-dir", help="runner log directory (overrides the profile)")
    sp.add_argument("--no-color", action="store_true")
    sp.add_argument("--here", action="store_true", help=argparse.SUPPRESS)
    sub.add_parser("hunt", help="the goldilocks hunter with this profile (cimp hunt --help)", add_help=False)
    sub.add_parser("ladder", help="the outcome ladder")
    sp = sub.add_parser("classify", help="name the outcomes in runner log text (files or stdin)")
    sp.add_argument("files", nargs="*")
    sub.add_parser("test", help="the same as config --test")
    if "hunt" in argv:
        i = argv.index("hunt")
        a = p.parse_args(argv[:i + 1])
        a.profile = pick_profile(a.profile)
        first_use(a)
        return cmd_hunt(a, argv[i + 1:])
    a = p.parse_args(argv)
    a.profile = pick_profile(a.profile)
    if a.cmd == "config":
        return cmd_config(a)
    if a.cmd == "test":
        eff, src, cards = resolve(a.profile)
        f = run_checks(a.profile, eff, cards)
        print("%d check(s) failed" % f if f else "all checks passed")
        return 1 if f else 0
    if a.cmd == "watch":
        if not a.here:
            first_use(a)
        return cmd_watch(a)
    if a.cmd == "ladder":
        return cmd_ladder(a)
    return cmd_classify(a)


def pick_profile(name):
    if name:
        return name
    if os.environ.get("CIMP_PROFILE"):
        return os.environ["CIMP_PROFILE"]
    try:
        data = load_file()
    except SystemExit:
        data = None
    return (data or {}).get("default_profile") or "default"


def first_use(a):
    """No config file and no CIMP_* settings: on a terminal, offer the configurator once."""
    if os.path.exists(config_path()) or any(k.startswith("CIMP_") and k not in ("CIMP_PROFILE", "CIMP_CONFIG")
                                            for k in os.environ) or config_path() == os.devnull:
        return
    if sys.stdin.isatty() and sys.stdout.isatty():
        print("No CiMP config yet (%s). Let's set one up; Ctrl-C to skip." % config_path())
        try:
            ns = argparse.Namespace(profile=a.profile, list=False, set=None, unset=None, yes=False, show=False,
                                    json=False, test=False, write_bench_conf=None, store_password=False)
            cmd_config(ns)
        except KeyboardInterrupt:
            print("\nskipped; defaults and CIMP_* variables apply")


if __name__ == "__main__":
    sys.exit(main())
