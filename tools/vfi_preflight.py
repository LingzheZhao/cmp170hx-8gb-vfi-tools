#!/usr/bin/env python3
"""Preflight before glitching: host, card, Pico firmware and the voltage controller. Reads only.

Pico side (serial commands v, z and PMBus reads): firmware build and the profile save/restore marker, who controls
the rail, the six transient-profile registers of this card (fails if one holds its fast value: an interrupted shot,
power the card fully off), VOUT, temperature and status words.

Host side (with --bdf): card identity and driver from sysfs, kernel lockdown, the nvidia module, pci=noaer. Config
space and BAR0 are read only when the rail reads healthy under the GPU's own control: never on a card in brown-out.
Then: config dword, memory decode, FUSECTRL (stops on 0xBADF), GFW boot progress.

Exit 0 = OK to glitch. Run it before every session and after any probe move.
"""
import argparse
import os
import sys
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import vfi_common as vc  # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    vc.add_bench_args(ap, bdf_required=False)
    ap.add_argument("--allow-stock-fw", action="store_true", help="do not fail on firmware without the patch")
    ap.add_argument("--enable-decode", action="store_true",
                    help="turn on memory decode (PCI COMMAND bit 1) if a cold card has it off")
    a = ap.parse_args()
    bad, warn = [], []

    with vc.open_card(a.port) as card:
        st = vc.fw_status(card)
        print("pico v:", st[:300])
        if vc.FW_MARKER not in st:
            (warn if a.allow_stock_fw else bad).append(
                f"firmware lacks {vc.FW_MARKER}: stock firmware writes its author's VRM table onto this card after "
                "every fast-profile shot")
        pwd, e1 = card.lock_status()
        mode = {vc.E1_GPU: "GPU PWM-VID control (normal)", vc.E1_PMBUS: "PMBus control"}.get(e1, "unexpected")
        print(f"pico z: unlocked={pwd} E1={e1:#06x} ({mode})")
        if e1 == vc.E1_PMBUS:
            bad.append("the rail is under PMBus control (a held state or an interrupted run): run vfi_park.py")
        elif e1 != vc.E1_GPU:
            bad.append(f"unexpected E1 {e1:#06x}")
        vals, probs = vc.profile_check(card)
        for reg, p in zip(vc.PROFILE_REGS, probs):
            if p:
                bad.append(p)
                continue
            v = vals[reg]
            same = v == vc.PROFILE_UPSTREAM_TABLE[reg]
            print(f"  MP2888B reg {reg:#04x} = {v:#06x}  "
                  + ("same as the upstream stock table" if same else
                     f"differs from the upstream stock table ({vc.PROFILE_UPSTREAM_TABLE[reg]:#06x}): the patched "
                     "firmware restores this card's own value"))
        if vals:
            print("  card profile:", " ".join(f"{r:02x}={v:04x}" for r, v in vals.items()))
        mv = None
        for name, reg in (("VOUT_MIN", 0x1F), ("VOUT_COMMAND", 0x21), ("READ_VOUT", 0x8B), ("READ_TEMP1", 0x8D),
                          ("STATUS_WORD", 0x79)):
            try:
                v = vc.vrm16(card, reg)
            except IOError as e:
                bad.append(f"{name} read failed ({str(e).strip()[:60]})")
                continue
            extra = ""
            if reg in (0x1F, 0x21):
                extra = f" (~{v * 6.25:.1f} mV at 6.25 mV per code)"
            if reg == 0x8B:
                mv, extra = v, " mV"
            if reg == 0x8D:
                extra = f" (raw; {v * 0.1:.1f} C if 0.1 C per bit, scale unconfirmed: watch the trend)"
            print(f"  {name} {reg:#04x} = {v:#06x}{extra}")

    if a.bdf:
        try:
            ident = vc.host_checks(a.bdf)
            print(f"card {a.bdf}: {ident['vendor']}:{ident['device']} driver={ident['driver']}")
        except vc.BenchStop as e:
            bad.append(str(e))
            ident = None
        if "pci=noaer" not in Path("/proc/cmdline").read_text().split():
            warn.append("pci=noaer is not on the kernel command line (AER recovery may reset the slot mid-campaign)")
        healthy = e1 == vc.E1_GPU and mv is not None and mv >= vc.HEALTHY_MV
        if ident and not healthy:
            bad.append(f"rail not healthy under GPU control (E1 {e1:#06x}, READ_VOUT {mv} mV): config space NOT "
                       "read; run vfi_rescue.py")
        elif ident:
            cfg = vc.cfg_dword(a.bdf)
            cmd = vc.cfg_dword(a.bdf, 4) & 0xFFFF
            print(f"  config dword {cfg:#010x}, COMMAND {cmd:#06x}")
            if cfg != vc.DEVICE_8GB:
                bad.append(f"config dword {cfg:#010x} (expected {vc.DEVICE_8GB:#010x})")
            elif not cmd & 2:
                if a.enable_decode:
                    with open(vc.sysfs(a.bdf) / "config", "r+b") as f:
                        f.seek(4)
                        f.write((cmd | 2).to_bytes(2, "little"))
                    cmd |= 2
                    print("  memory decode was off: turned on")
                else:
                    bad.append("memory decode is off (cold card, no driver): rerun with --enable-decode or "
                               f"setpci -s {a.bdf} COMMAND=0x0002")
            if cfg == vc.DEVICE_8GB and cmd & 2:
                with vc.Bar0(a.bdf) as bar:
                    ctrl = bar.rd32(vc.FUSECTRL)
                    if vc.is_poisoned(ctrl) or ctrl == vc.UNREADABLE:
                        bad.append(f"FUSECTRL {ctrl:#010x}: controller poisoned or BAR0 dead; nothing else read, "
                                   "run vfi_park.py")
                    else:
                        gfw = bar.rd32(0x118234) & 0xFF
                        dsov = bar.rd32(0x820084)
                        print(f"  FUSECTRL {ctrl:#010x}, GFW boot progress {gfw:#04x}, DSOV {dsov}")
                        if gfw == 0x01:
                            bad.append("GFW boot progress 0x01: the card is mid-boot, do not glitch or reset now")
    for w in warn:
        print("WARN:", w)
    for b in bad:
        print("FAIL:", b)
    print("PREFLIGHT", "OK" if not bad else f"FAILED ({len(bad)} problem(s)): do not glitch until reviewed")
    return 0 if not bad else 1


if __name__ == "__main__":
    sys.exit(vc.main_guard(main))
