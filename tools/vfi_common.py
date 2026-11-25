"""Shared helpers for the CMP 170HX 8 GB glitch bench tools.

Needs the ga100vfi host package from dalzyu/pico-ga100vfi (``pip install ./host`` in a clone, ideally with the
patches in ../firmware applied) and root for sysfs PCI access. Every tool here only glitches and reads. None of
them programs fuses.

Safety rules the helpers enforce:
  * never read PCI config space of a card whose rail may be browned out: hold NVVDD at stock under PMBus control,
    pulse PEX_RST, wait, and only then read config space (park);
  * read FUSECTRL first and stop all BAR0 access when it reads 0xBADFxxxx (poisoned controller);
  * glitch parameters stay inside fixed bounds (the firmware has none of its own);
  * shots and lost shots are counted per host boot, with a cap.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import statistics
import struct
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path

try:
    from ga100vfi.bar0 import Bar0, restore
    from ga100vfi.classify import classify
    from ga100vfi.glitch import Attempt, Card
except ImportError as e:  # pragma: no cover
    sys.exit(f"cannot import the ga100vfi host package ({e}); install it from a pico-ga100vfi clone: "
             "pip install ./host")

DEVICE_8GB = 0x20C210DE          # config dword 0: vendor 10de, device 20c2
FUSECTRL = 0x820000
UNREADABLE = 0xFFFFFFFF
VRM = 0x25                       # MP2888B PMBus address on the card's I2CC bus
REG_VOUT_COMMAND, REG_CTRL_MODE, REG_READ_VOUT, REG_READ_TEMP1, REG_STATUS_WORD = 0x21, 0xE1, 0x8B, 0x8D, 0x79
E1_GPU, E1_PMBUS = 0xC2EA, 0xC2FA  # rail under the GPU's PWM-VID control / under PMBus control
STOCK_VOUT_CODE = 0x7E           # 787.5 mV at 6.25 mV per code
HELD_MV = (700, 830)             # READ_VOUT window while held at stock under PMBus control
HEALTHY_MV = 500                 # below this under GPU control the GPU is asking for the brown-out floor
FW_MARKER = "fw=lab-psr1"        # set by firmware/0001 (card profile save/restore)

# The six transient-response registers the firmware switches to a "fast" profile for each shot.
PROFILE_REGS = (0xBA, 0xB9, 0x30, 0x31, 0x32, 0x45)
PROFILE_FAST = {0xBA: 0x0001, 0xB9: 0x0001, 0x30: 0xCE04, 0x31: 0x0002, 0x32: 0x0F81, 0x45: 0x7218}
PROFILE_UPSTREAM_TABLE = {0xBA: 0x0019, 0xB9: 0x0032, 0x30: 0xCE64, 0x31: 0x0072, 0x32: 0x7F81, 0x45: 0x7018}

# Hard bounds on everything sent to the firmware.
BOUNDS = {"v_dip": (280, 400), "v_restore": (550, 800), "t_dip": (-1000, 1000), "t_restore": (-1000, 1000),
          "pulses": (1, 5), "pw": (36, 400), "gap": (36, 400), "delay": (0, 400)}

OUTCOMES = ("goldilocks", "hit", "over", "under", "poisoned", "lost", "nack")
SHORT = {"goldilocks": "G", "hit": "H", "over": "O", "under": "u", "poisoned": "P", "lost": "L", "nack": "n"}


class BenchStop(RuntimeError):
    """Stop the run now (the firmware refused, a limit was reached, a park failed)."""


# ---------------------------------------------------------------- PCI side

BDF_RE = re.compile(r"^[0-9a-f]{4}:[0-9a-f]{2}:[0-9a-f]{2}\.[0-7]$")


def bdf_type(s: str) -> str:
    s = s.strip().lower()
    if re.match(r"^[0-9a-f]{2}:[0-9a-f]{2}\.[0-7]$", s):
        s = "0000:" + s
    if not BDF_RE.match(s):
        raise argparse.ArgumentTypeError(f"not a PCI address: {s!r} (want dddd:bb:dd.f)")
    return s


def sysfs(bdf: str) -> Path:
    return Path("/sys/bus/pci/devices") / bdf


def identity(bdf: str) -> dict:
    """Vendor, device and bound driver from sysfs. These files are cached by the kernel: no config access."""
    p = sysfs(bdf)
    if not p.exists():
        raise BenchStop(f"{bdf}: no such PCI device")
    drv = p / "driver"
    return {"vendor": (p / "vendor").read_text().strip(), "device": (p / "device").read_text().strip(),
            "driver": drv.resolve().name if drv.exists() else None}


def check_card(bdf: str, devices=("0x20c2",)) -> dict:
    ident = identity(bdf)
    if ident["vendor"] != "0x10de" or ident["device"] not in devices:
        raise BenchStop(f"{bdf} is {ident['vendor']}:{ident['device']}, not an allowed card ({', '.join(devices)})")
    if ident["driver"] not in (None, "pci-stub"):
        raise BenchStop(f"{bdf} is bound to {ident['driver']}: unbind it (only no driver or pci-stub is allowed)")
    return ident


def cfg_dword(bdf: str, off: int = 0) -> int:
    """One real config-space read. Only call this on a card that is healthy or parked."""
    with open(sysfs(bdf) / "config", "rb") as f:
        f.seek(off)
        return int.from_bytes(f.read(4), "little")


def lockdown_mode() -> str | None:
    try:
        m = re.search(r"\[(\w+)\]", Path("/sys/kernel/security/lockdown").read_text())
        return m.group(1) if m else None
    except OSError:
        return None


def boot_id() -> str:
    return Path("/proc/sys/kernel/random/boot_id").read_text().strip()


def is_poisoned(v: int) -> bool:
    """0xBADFxxxx (poisoned) or 0xBAD0xxxx (the fuse unit did not answer): read nothing else, as the runner does."""
    return (v >> 20) == 0xBAD


# ---------------------------------------------------------------- Pico / VRM side

def open_card(port: str):
    if not os.path.exists(port):
        raise BenchStop(f"no serial port at {port}")
    return Card(port)


def vrm16(card, reg: int) -> int:
    d = card.read(VRM, reg, 2)
    return d[0] | d[1] << 8


def ensure_unlocked(card) -> bool:
    # Always send 'u' so firmware/0007 re-applies 0x13=0x00 and 0x46=0x1229 even when pwd=1.
    return card.unlock()


def fw_status(card) -> str:
    return card.status().replace("\r", "").replace("\n", " | ")


def require_firmware(card, need_trace: bool = False, allow_stock: bool = False, fast: int = 1) -> str:
    st = fw_status(card)
    if need_trace and "nvvdd=" not in st:
        raise BenchStop("the Pico runs a normal build; NVVDD traces need the debug build (-DDEBUG=ON)")
    if FW_MARKER not in st and fast and not allow_stock:
        raise BenchStop(f"the Pico firmware lacks the profile save/restore patch ({FW_MARKER} not in its status "
                        "line). Stock firmware writes its author's VRM table onto your card after every fast-profile "
                        "shot. Flash a patched build, or use --fast 0, or pass --allow-stock-fw if you checked "
                        "that your card's six profile registers equal that table (vfi_preflight.py).")
    return st


def rail(card) -> tuple[int, int]:
    """(E1 control word, READ_VOUT in mV). I2C only."""
    _, e1 = card.lock_status()
    return e1, vrm16(card, REG_READ_VOUT)


def hold_stock(card) -> int:
    """Take the rail under PMBus control at stock 787.5 mV; returns READ_VOUT."""
    if not ensure_unlocked(card):
        raise BenchStop("VRM unlock failed (no MATCH): stop and check the I2C wiring")
    card.write(VRM, REG_VOUT_COMMAND, bytes((STOCK_VOUT_CODE, 0x00)))
    card.write(VRM, REG_CTRL_MODE, bytes((E1_PMBUS & 0xFF, E1_PMBUS >> 8)))
    time.sleep(0.3)
    return vrm16(card, REG_READ_VOUT)


def temp_raw(card) -> int | None:
    try:
        return vrm16(card, REG_READ_TEMP1)
    except IOError:
        return None


def wait_temp(card, lo: int, hi: int, timeout_s: float, log=print) -> int | None:
    t0 = time.monotonic()
    while True:
        t = temp_raw(card)
        if t is not None and lo <= t <= hi:
            return t
        if time.monotonic() - t0 > timeout_s:
            log(f"temperature raw {t} not inside {lo}-{hi} after {timeout_s:.0f} s: going on")
            return t
        time.sleep(10)


def host_checks(bdf: str, devices=("0x20c2",)) -> dict:
    """Card identity and driver, kernel lockdown, NVIDIA module. No config or BAR0 access."""
    ident = check_card(bdf, devices)
    lm = lockdown_mode()
    if lm not in (None, "none"):
        raise BenchStop(f"kernel lockdown is '{lm}': BAR0 and config writes will be refused (turn Secure Boot off)")
    if Path("/sys/module/nvidia").exists() and ident["driver"] != "pci-stub":
        raise BenchStop("the nvidia module is loaded and the card is not held by pci-stub: a driver must never "
                        "probe a glitched card (bind 10de:20c2 to pci-stub or unload nvidia)")
    return ident


def profile_check(card) -> tuple[dict, list]:
    """Read the six profile registers. Returns ({reg: value}, [problem or '' per register])."""
    vals, problems = {}, []
    for reg in PROFILE_REGS:
        try:
            v = vrm16(card, reg)
        except IOError as e:
            problems.append(f"MP2888B reg {reg:#04x}: read failed ({str(e).strip()[:60]})")
            continue
        vals[reg] = v
        problems.append(f"MP2888B reg {reg:#04x} holds its fast value {v:#06x}: an interrupted shot? "
                        "power the card fully off before glitching" if v == PROFILE_FAST[reg] else "")
    return vals, problems


# ---------------------------------------------------------------- park

@dataclass
class ParkResult:
    ok: bool
    held_mv: int | None = None
    cfg_held: int | None = None
    cfg_after: int | None = None
    gpu_mv: int | None = None
    link_s: float | None = None
    note: str = ""

    def line(self) -> str:
        h = lambda v: "-" if v is None else f"{v:#010x}"
        return (f"park {'OK' if self.ok else 'FAILED'}: held {self.held_mv} mV, config while held {h(self.cfg_held)}"
                f"{'' if self.link_s is None else f' (after {self.link_s:.1f} s)'}, after handback {h(self.cfg_after)}"
                f", GPU rail {self.gpu_mv} mV{('; ' + self.note) if self.note else ''}")


def park(card, bdf: str, expect: int = DEVICE_8GB, settle_s: float = 1.5, extra_s: float = 3.0,
         handback_s: float = 1.0, log=None) -> ParkResult:
    """Rail held at stock under PMBus, PEX_RST, wait for config space, hand the rail back, check again.

    Never reads config space before the rail is held and READ_VOUT confirms it. If the link does not come back
    while held, the rail stays held at stock (no handback, no more config reads): run park again. If the GPU asks
    for the brown-out floor after the handback, the rail is taken back to stock and config space is not read."""
    say = log or (lambda *_: None)
    r = ParkResult(False)
    r.held_mv = hold_stock(card)
    say(f"rail under PMBus control: READ_VOUT {r.held_mv} mV")
    if not HELD_MV[0] <= r.held_mv <= HELD_MV[1]:
        card.revert()
        r.note = f"READ_VOUT {r.held_mv} mV is not stock under PMBus control: handed back, config NOT read"
        return r
    say("PEX_RST: " + card.cmd("r").replace("\n", " | "))
    t0 = time.monotonic()
    time.sleep(settle_s)
    v = cfg_dword(bdf)
    while v == UNREADABLE and time.monotonic() - t0 < settle_s + extra_s:
        time.sleep(0.5)
        v = cfg_dword(bdf)
    r.cfg_held, r.link_s = v, time.monotonic() - t0
    say(f"config while held: {v:#010x} after {r.link_s:.1f} s")
    if v != expect:
        r.note = "link not back while held: rail LEFT under PMBus control at stock; run park again"
        return r
    card.revert()
    time.sleep(handback_s)
    r.gpu_mv = vrm16(card, REG_READ_VOUT)
    if r.gpu_mv < HEALTHY_MV:
        hold_stock(card)
        r.note = (f"after handback the GPU asked for {r.gpu_mv} mV (brown-out): rail taken back to stock, "
                  "config NOT read; run park again")
        return r
    r.cfg_after = cfg_dword(bdf)
    r.ok = r.cfg_after == expect
    if not r.ok:
        r.note = "config changed after handback"
    return r


# ---------------------------------------------------------------- shots

def add_bench_args(p: argparse.ArgumentParser, bdf_required: bool = True) -> None:
    env_bdf = os.environ.get("VFI_BDF")
    p.add_argument("--bdf", type=bdf_type, default=bdf_type(env_bdf) if env_bdf else None,
                   required=bdf_required and not env_bdf, help="PCI address of the card (or set VFI_BDF)")
    env_port = os.environ.get("VFI_PORT")
    p.add_argument("--port", default=env_port, required=not env_port,
                   help="Pico serial port, e.g. /dev/serial/by-id/usb-Raspberry_Pi_... (or set VFI_PORT)")


def add_shot_args(p: argparse.ArgumentParser, with_dip: bool = True) -> None:
    g = p.add_argument_group("shot (bounds in vfi_common.BOUNDS)")
    if with_dip:
        g.add_argument("--v-dip", type=int, default=354, help="dip voltage, mV (6.25 mV steps)")
        g.add_argument("--t-dip", type=int, default=-60, help="train start vs PEX_RST release, us (tool default -60)")
    g.add_argument("--v-restore", type=int, default=750, help="recovery voltage between pulses, mV")
    g.add_argument("--t-restore", type=int, default=-20)
    g.add_argument("--pulses", type=int, default=3)
    g.add_argument("--pw", type=int, default=198, help="pulse width, us")
    g.add_argument("--gap", type=int, default=150, help="gap between pulses, us")
    g.add_argument("--delay", type=int, default=117, help="rail response lead, us")
    g.add_argument("--fast", type=int, default=1, choices=(0, 1), help="fast VRM transient profile during the shot")
    g.add_argument("--allow-stock-fw", action="store_true", help="allow --fast 1 on firmware without the patch")
    g.add_argument("--restore-wait", type=float, default=0.25, help="seconds to wait for the link after a shot")


def add_limit_args(p: argparse.ArgumentParser) -> None:
    g = p.add_argument_group("per-boot limits")
    g.add_argument("--max-shots-per-boot", type=int, default=450)
    g.add_argument("--max-lost-per-boot", type=int, default=150)
    g.add_argument("--state-dir", default=os.environ.get("VFI_STATE_DIR", "/var/tmp/cmp170hx-vfi"),
                   help="where the per-boot shot ledger lives")


def make_attempt(v_dip, t_dip, v_restore, t_restore, pulses, pw, gap, delay, fast):
    vals = {"v_dip": v_dip, "t_dip": t_dip, "v_restore": v_restore, "t_restore": t_restore, "pulses": pulses,
            "pw": pw, "gap": gap, "delay": delay}
    for k, v in vals.items():
        lo, hi = BOUNDS[k]
        if not lo <= v <= hi:
            raise BenchStop(f"{k} {v} outside {lo}..{hi}")
    if v_restore <= v_dip:
        raise BenchStop("v_restore must be above v_dip")
    return Attempt(v_dip_mv=v_dip, t_dip_us=t_dip, v_restore_mv=v_restore, t_restore_us=t_restore,
                   t_revert_us=0, fast=fast, hold_ms=0, pulses=pulses, pw=pw, gap=gap, delay=delay, revert=0)


def attempt_from(args, **over):
    k = {"v_dip": getattr(args, "v_dip", None), "t_dip": getattr(args, "t_dip", None),
         "v_restore": args.v_restore, "t_restore": args.t_restore,
         "pulses": args.pulses, "pw": args.pw, "gap": args.gap, "delay": args.delay, "fast": args.fast}
    k.update(over)
    return make_attempt(**k)


class Ledger:
    """Shots and lost shots per host boot, shared by every tool that glitches."""

    def __init__(self, state_dir: str, max_shots: int, max_lost: int):
        os.makedirs(state_dir, mode=0o700, exist_ok=True)
        self.path = Path(state_dir) / f"ledger-{boot_id()}.json"
        self.max_shots, self.max_lost = max_shots, max_lost
        try:
            self.d = json.loads(self.path.read_text())
        except (OSError, ValueError):
            self.d = {"shots": 0, "lost": 0}

    def check(self) -> None:
        if self.d["shots"] >= self.max_shots:
            raise BenchStop(f"{self.d['shots']} shots on this boot (cap {self.max_shots}): reboot the host first")
        if self.d["lost"] >= self.max_lost:
            raise BenchStop(f"{self.d['lost']} lost shots on this boot (cap {self.max_lost}): reboot the host first")

    def add(self, outcome: str) -> None:
        self.d["shots"] += 1
        self.d["lost"] += outcome == "lost"
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(self.d))
        os.replace(tmp, self.path)

    def __str__(self) -> str:
        return f"boot ledger {self.d['shots']}/{self.max_shots} shots, {self.d['lost']}/{self.max_lost} lost"


@dataclass
class Shot:
    outcome: str
    dsov: int | None = None
    crc: int | None = None
    priv: int | None = None
    fusectrl: int | None = None
    trace_mv: list = field(default_factory=list)
    writes_failed: int | None = None

    def brief(self) -> str:
        if self.dsov is None:
            return self.outcome
        return f"{self.outcome} dsov={self.dsov} crc={self.crc:#04x} priv={self.priv:#010x}"


def read_outcome(bdf: str, restore_wait: float) -> Shot:
    """After a shot (rail held under PMBus by revert=0): wait for the link, then classify, FUSECTRL first."""
    if not restore(bdf, wait_s=restore_wait):
        return Shot("lost")
    with Bar0(bdf) as bar:
        ctrl = bar.rd32(FUSECTRL)
        if ctrl == UNREADABLE:
            return Shot("lost", fusectrl=ctrl)
        if is_poisoned(ctrl):
            return Shot("poisoned", fusectrl=ctrl)
        v = classify(bar)       # firmware/0005: passive by default (no EN_SW_OVERRIDE write)
    if is_poisoned(v.dsov):
        return Shot("poisoned", fusectrl=ctrl)  # DSOV 0xBADFxxxx behind a healthy FUSECTRL; classify read nothing more
    if v.passed:
        out = "goldilocks"
    elif v.dsov == 1:
        out = "under"
    elif v.crc_status == 0x55:
        out = "hit"     # DSOV 0 and CRC 0x55, another check failed (usually the privilege mask): not burnable
    else:
        out = "over"
    return Shot(out, v.dsov, v.crc_status, v.priv_mask, ctrl)


def fire(card, bdf: str, attempt, restore_wait: float, want_trace: bool = False) -> Shot:
    try:
        rep = card.attempt(attempt)
    except IOError as e:
        if "ABORT" in str(e):
            raise BenchStop("firmware refused the shot: " + str(e).strip().splitlines()[-1])
        time.sleep(0.5)
        try:
            ensure_unlocked(card)
        except IOError:
            pass
        return Shot("nack")
    if rep.writes_failed > 0:
        try:
            ensure_unlocked(card)
        except IOError:
            pass
        return Shot("nack", writes_failed=rep.writes_failed)
    if attempt.revert == 0 and (not rep.pwd or rep.e1 != E1_PMBUS):
        # A locked controller acknowledges the dip writes and discards them: the shot did not run.
        raise BenchStop(f"NO DIP: after the shot the controller reports pwd={int(rep.pwd)} e1={rep.e1:#06x} "
                        f"(want pwd=1, e1={E1_PMBUS:#06x}); the shot did not run")
    tr = [round(x * 3300.0 / 4096.0, 1) for x in card.trace()] if want_trace else []
    s = read_outcome(bdf, restore_wait)
    s.trace_mv, s.writes_failed = tr, rep.writes_failed
    return s


def trough_mv(mv: list) -> float | None:
    """Lowest point of a rail trace after a 9-sample running median (drops single-sample ADC speckle)."""
    if len(mv) < 50:
        return None
    return round(min(statistics.median(mv[max(0, i - 4):i + 5]) for i in range(0, len(mv), 3)))


class Bench:
    """One Pico + one card. Use as a context manager: on any exit except a held goldilocks, the card is parked."""

    def __init__(self, args, need_trace: bool = False, log=print):
        self.args, self.log, self.need_trace = args, log, need_trace
        self.bdf, self.port = args.bdf, args.port
        self.ledger = Ledger(args.state_dir, args.max_shots_per_boot, args.max_lost_per_boot)
        self.holding = False
        self.card = None

    def __enter__(self):
        host_checks(self.bdf)
        self.card = open_card(self.port)
        try:
            st = require_firmware(self.card, self.need_trace, self.args.allow_stock_fw, self.args.fast)
            self.log("pico: " + st[:200])
            bad = [p for p in profile_check(self.card)[1] if p]
            if bad:
                raise BenchStop("; ".join(bad))
            if not ensure_unlocked(self.card):
                raise BenchStop("VRM unlock failed: the controller stays locked until its 3.3 V supply cycles")
            self.log(str(self.ledger))
        except BaseException:
            self.card.close()
            raise
        return self

    def park(self) -> ParkResult:
        r = park(self.card, self.bdf)
        self.log(r.line())
        if not r.ok:
            raise BenchStop("park failed: " + r.note)
        return r

    def shot(self, attempt) -> Shot:
        self.ledger.check()
        s = fire(self.card, self.bdf, attempt, self.args.restore_wait, self.need_trace)
        self.ledger.add(s.outcome)
        if s.outcome == "poisoned":   # stays poisoned until a power cycle: no further shot on this boot (parked on exit)
            raise BenchStop(f"the shot poisoned the fuse controller (FUSECTRL {s.fusectrl or 0:#010x}): it stays "
                            "poisoned until a power cycle; the card is parked; power-cycle the host before another "
                            "shot")
        return s

    def hold(self, msg: str) -> None:
        self.holding = True
        self.log(msg)
        self.log("STATE HELD: rail under PMBus control at stock, no reset, no handback. Read it now "
                 "(vfi_snap.py), then run vfi_park.py. Do not reboot from this state.")

    def __exit__(self, et, ev, tb):
        if self.card is None:
            return False
        try:
            if et is not None:
                self.log("exiting on error/interrupt: skipping automatic park; review logs and power cycle")
            elif not self.holding:
                r = park(self.card, self.bdf)
                self.log("final " + r.line())
        except Exception as e:  # noqa: BLE001
            self.log(f"WARN: final park raised {e!r}: run vfi_park.py now")
        finally:
            self.card.close()
        return False


def write_private(path: str, obj) -> None:
    d = os.path.dirname(os.path.abspath(path))
    os.makedirs(d, exist_ok=True)
    with os.fdopen(os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600), "w") as f:
        json.dump(obj, f, indent=1)


def append_jsonl(path: str | None, obj) -> None:
    if not path:
        return
    with os.fdopen(os.open(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600), "a") as f:
        f.write(json.dumps(obj) + "\n")


def stamp() -> str:
    return time.strftime("%H:%M:%S")


def main_guard(fn) -> int:
    """Run a tool's main; a BenchStop becomes exit code 3 with its message."""
    try:
        return fn()
    except BenchStop as e:
        print("STOP: " + str(e), file=sys.stderr)
        return 3
    except KeyboardInterrupt:
        print("interrupted", file=sys.stderr)
        return 130
    except OSError as e:
        print(f"I/O error (serial port, I2C or sysfs): {e}", file=sys.stderr)
        return 5
