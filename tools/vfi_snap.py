#!/usr/bin/env python3
"""Snapshot a CMP 170HX 8 GB card's fuse, gate and PCIe option registers to a private JSON file.

Default mode: BAR0 READS ONLY, no register or config-space writes. FUSECTRL is read first; on 0xBAD0xxxx..0xBADFxxxx
or 0xFFFFFFFF the tool stops immediately with ZERO subsequent BAR0 reads or writes.

--rows also reads the 512 main fuse rows twice through the fuse controller's READ command. That writes only
FUSEADDR (0x820004) and FUSECTRL = 1 (CMD_READ), then restores FUSEADDR ONLY on clean completion (never in finally
after an error). It never writes DEBUGCTRL (0x820060), never selects RIR rows and never programs anything.

--die-id DUMP reads fuse rows 8-15 (the die ID) once with the same CMD_READ and compares them with DUMP, the 512-row
dump a burn plan was made from (its ff_rows, or the rows of a --rows snapshot whose two passes matched and whose
gate/status state did not change). Exit 5 when they differ. Only row numbers are printed, never the values.

The output file is mode 0600. Fuse rows identify the card: keep dumps private.
"""
import argparse
import datetime
import json
import mmap
import os
import struct
import sys
import time
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import vfi_common as vc  # noqa: E402

CTRL, ADDR, DATA = 0x820000, 0x820004, 0x820008
REGS = {
    "PMC_BOOT_0": 0x000000, "GFW_BOOT_118234": 0x118234,
    "FUSECTRL": 0x820000, "FUSEADDR": 0x820004, "EN_SW_OVERRIDE": 0x820040, "IFF_RECORD": 0x820050,
    "DEBUGCTRL": 0x820060, "CRC_STATUS": 0x820070, "EFP": 0x820078, "R_82007c": 0x82007C, "DSOV": 0x820084,
    "R_820088": 0x820088, "PLM_e0": 0x8200E0, "PLM_f4": 0x8200F4, "PLM_f8": 0x8200F8, "PLM_fc": 0x8200FC,
    "WDATA_WR_SECURE": 0x82048C, "DEVIDA": 0x8204D8, "SBFE": 0x821108, "FPF_824314": 0x824314,
    "GPC_DISABLE": 0x820350, "GPC_DEFECTIVE": 0x8205C4, "GPC_STATUS": 0x820C1C,
    "PES_DISABLE": 0x820690, "PES_DEFECTIVE": 0x820698,
    "FBP_DISABLE": 0x820364, "FBP_R_820368": 0x820368, "FBP_DEFECTIVE": 0x8205CC, "FBPA_DEFECTIVE": 0x8205D0,
    "FBP_STATUS": 0x820D38, "FBIO_DISABLE": 0x82036C, "FBIO_DEFECTIVE": 0x8205D4,
    "ROP_L2_DISABLE": 0x8202C4, "ROP_L2_DEFECTIVE": 0x8205E8, "FB_CFG1_9a0204": 0x9A0204, "FB_LMR_100ce0": 0x100CE0,
    "OPT_DISABLE_GEN3_SPEED": 0x820250, "OPT_PCIE_MAGIC_BITS_D": 0x820520, "OPT_PCIE_BOOT_GEN3_DIS": 0x820580,
}
ALLOW_ALL_ONES_REGS = {"PLM_e0", "PLM_f4", "PLM_f8", "PLM_fc", "FPF_824314"}
ALIASES = [0x820148, 0x820E20, 0x8207F4, 0x820158, 0x820324, 0x820434, 0x821108, 0x821058, 0x82074C,
           0x8206FC, 0x820644, 0x820640, 0x820410, 0x82063C, 0x820B98, 0x8205A8, 0x821064]
TPC_DEFECTIVE = [0x8205C8, 0x820508, 0x82050C, 0x820514, 0x820564, 0x8205AC, 0x820B84, 0x820B80]
TPC_RECONFIG = [0x820788, 0x82078C, 0x820790, 0x820794, 0x820798, 0x82079C, 0x820B94, 0x820B90]
DIE_ID_ROWS = tuple(range(8, 16))   # fuse rows that carry the die ID


def _ctrl_is_idle(c: int) -> bool:
    return (
        c != vc.UNREADABLE
        and not vc.is_poisoned(c)
        and ((c >> 16) & 31) == 4
        and (c & 0xF) == 0
    )


def _poll_ctrl_idle(rd, row, timeout=0.2):
    """Single FUSECTRL read per iteration; stops immediately on poison, 0xFFFFFFFF, or timeout."""
    deadline = time.monotonic() + timeout
    first = True
    while True:
        if not first and time.monotonic() > deadline:
            raise RuntimeError(f"CMD_READ timeout at row {row}")
        first = False
        c = rd(CTRL)
        if c == vc.UNREADABLE or vc.is_poisoned(c):
            raise RuntimeError(f"controller poisoned/dead ({c:#010x}) at row {row}")
        if _ctrl_is_idle(c):
            return c
        if time.monotonic() > deadline:
            raise RuntimeError(f"CMD_READ timeout at row {row}")


def read_fuse_rows(rd, wr, idle, rows, timeout=0.2):
    """One CMD_READ per row; FUSEADDR is restored ONLY on clean completion (never in finally on error)."""
    orig = rd(ADDR)
    if orig == vc.UNREADABLE or vc.is_poisoned(orig):
        raise RuntimeError(f"FUSEADDR reads {orig:#010x} before row read")
    got = {}
    for row in rows:
        wr(ADDR, row)
        latched = rd(ADDR)
        if latched == vc.UNREADABLE or vc.is_poisoned(latched):
            raise RuntimeError(f"FUSEADDR poisoned/dead ({latched:#010x}) latching row {row}")
        if latched != row:
            raise RuntimeError(f"FUSEADDR did not latch row {row}")
        wr(CTRL, 1)
        _poll_ctrl_idle(rd, row, timeout=timeout)
        got[row] = rd(DATA)
    # Only reached when every single row succeeded without any exception!
    if _ctrl_is_idle(rd(CTRL)):
        wr(ADDR, orig)
    return got


def load_die_id(path):
    """The die ID rows (8-15) of a 512-row dump, as ints: a dump's ff_rows, or the rows list this tool's --rows writes
    (only when its two passes matched AND state_unchanged_by_row_read is not False and no rows_error)."""
    with open(path) as f:
        d = json.load(f)
    if d.get("rows_error") or d.get("read_errors"):
        raise vc.BenchStop(f"{path}: dump recorded row read errors; refuse as die-ID reference")
    rows = d.get("ff_rows")
    if rows is None and isinstance(d.get("all_rows"), dict):
        rows = d["all_rows"]
    if rows is None and isinstance(d.get("rows"), list):
        if d.get("rows_match") is not True:
            raise vc.BenchStop(f"{path}: the two passes of its row read differed; use a dump whose passes matched")
        if d.get("state_unchanged_by_row_read") is False:
            raise vc.BenchStop(f"{path}: gate/status state changed during its row read; refuse as die-ID reference")
        rows = {str(i): v for i, v in enumerate(d["rows"])}
    rows = rows or {}
    want = {}
    for r in DIE_ID_ROWS:
        v = rows.get(str(r))
        if v is None:
            raise vc.BenchStop(f"{path} does not hold fuse row {r}: use the full 512-row dump the plan was made from")
        want[r] = int(v, 0) if isinstance(v, str) else int(v)
    return want


def snapshot_rc(out: dict) -> int:
    """5 when --die-id found a different card. 1 when a --rows or --die-id read cannot be trusted: it failed, the two
    passes differ, or the gate/status registers changed during the read. A plan must never be built from such a
    snapshot."""
    if out.get("die_id_match") is False:
        return 5
    bad = (out.get("rows_error") or out.get("die_id_error") or out.get("rows_match") is False
           or out.get("state_unchanged_by_row_read") is False)
    return 1 if bad else 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--bdf", type=vc.bdf_type, default=os.environ.get("VFI_BDF"), required=not os.environ.get("VFI_BDF"),
                    help="PCI address of the card (or set VFI_BDF)")
    ap.add_argument("--port", default=os.environ.get("VFI_PORT"),
                    help="Pico serial port: check the rail over I2C before any config read (or set VFI_PORT)")
    ap.add_argument("--rows", action="store_true", help="also read the 512 main fuse rows twice (CMD_READ only)")
    ap.add_argument("--die-id", metavar="DUMP", help="also read fuse rows 8-15 (the die ID, CMD_READ only) and compare "
                    "them with DUMP's (ff_rows, or a --rows snapshot); exit 5 when they differ (a different card)")
    ap.add_argument("--label", default="snap")
    ap.add_argument("--outdir", default="vfi-snaps")
    ap.add_argument("--allow-10gb", action="store_true", help="allow a 10 GB card (10de:2082), read-only use")
    ap.add_argument("--resource", help=argparse.SUPPRESS)   # offline tests: a file standing in for BAR0
    a = ap.parse_args()
    if a.resource:
        return run(a, {"vendor": "0x10de", "device": "0x20c2", "driver": None}, 0x0006, a.resource)

    devices = ("0x20c2", "0x2082") if a.allow_10gb else ("0x20c2",)
    ident = vc.check_card(a.bdf, devices)
    if a.port:
        with vc.open_card(a.port) as card:
            e1, mv = vc.rail(card)
        if mv < vc.HEALTHY_MV:   # whoever owns the rail (GPU, PMBus, unknown): a low rail is a brown-out
            who = "GPU asks for" if e1 == vc.E1_GPU else f"rail at (E1={e1:#06x})"
            raise vc.BenchStop(f"{who} {mv} mV (brown-out): config NOT read; run vfi_rescue.py")
        print(f"rail: E1={e1:#06x} READ_VOUT {mv} mV")
    p = vc.sysfs(a.bdf)
    cfg = (p / "config").read_bytes()[:8]
    if struct.unpack_from("<I", cfg, 0)[0] == vc.UNREADABLE:
        raise vc.BenchStop("config space reads 0xffffffff: the link is down (vfi_rescue.py)")
    cfg_cmd = struct.unpack_from("<H", cfg, 4)[0]
    if not cfg_cmd & 0x2:
        raise vc.BenchStop(f"memory decode is off (COMMAND bit 1); for a cold card: setpci -s {a.bdf} COMMAND=0x0002")
    return run(a, ident, cfg_cmd, p / "resource0")


def run(a, ident, cfg_cmd, resource) -> int:
    rw = a.rows or a.die_id
    fd = os.open(resource, (os.O_RDWR if rw else os.O_RDONLY) | os.O_SYNC)
    mm = mmap.mmap(fd, 0x1000000, mmap.MAP_SHARED, mmap.PROT_READ | (mmap.PROT_WRITE if rw else 0))
    os.close(fd)
    mv32 = memoryview(mm).cast("I")
    try:
        return snapshot(a, ident, cfg_cmd, mv32)
    finally:
        mv32.release()
        mm.close()


def snapshot(a, ident, cfg_cmd, mv32) -> int:
    def rd(off):
        return mv32[off // 4]

    def wr(off, val):
        if not (a.rows or a.die_id) or off not in (ADDR, CTRL) or (off == CTRL and val != 1):
            raise RuntimeError(f"write outside the CMD_READ allowlist: {off:#x}={val:#x}")
        mv32[off // 4] = val

    def rd_checked(off, label, allow_all_ones=False):
        val = rd(off)
        if vc.is_poisoned(val):
            raise vc.BenchStop(f"{label} reads {val:#010x}: poisoned controller; stopped immediately.")
        if not allow_all_ones and val == vc.UNREADABLE:
            raise vc.BenchStop(f"{label} reads {val:#010x}: dead BAR0; stopped immediately.")
        return val

    ctrl = rd(CTRL)                      # first BAR0 read, always
    if ctrl == vc.UNREADABLE or vc.is_poisoned(ctrl):
        raise vc.BenchStop(f"FUSECTRL reads {ctrl:#010x}: poisoned controller or dead BAR0; nothing else read. "
                           "Park the card (vfi_park.py) before reading it.")
    out = {"bdf": a.bdf, "identity": ident, "pci_command": hex(cfg_cmd), "label": a.label,
           "utc": datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds")}
    r = {}
    for k, v in REGS.items():
        if k == "FUSECTRL":
            r[k] = ctrl
            continue
        r[k] = rd_checked(v, k, allow_all_ones=(k in ALLOW_ALL_ONES_REGS))
    out["regs"] = {k: f"{v:#010x}" for k, v in r.items()}

    # Fail-fast on EVERY alias read (zero further reads after first poison/dead BAR0).
    alias_vals = {}
    for x in ALIASES:
        alias_vals[x] = rd_checked(x, f"alias {x:#08x}")
    out["aliases"] = {f"{x:#08x}": f"{val:#010x}" for x, val in alias_vals.items()}
    out["aliases_zero"] = sum(1 for val in alias_vals.values() if val == 0)

    # Fail-fast on EVERY per_gpc read (zero further reads after first poison/dead BAR0).
    g = {
        "tpc_disable": [rd_checked(0x820760 + 4 * i, f"tpc_disable[{i}]") for i in range(8)],
        "tpc_defective": [rd_checked(x, f"tpc_defective[{i}]") for i, x in enumerate(TPC_DEFECTIVE)],
        "tpc_reconfig": [rd_checked(x, f"tpc_reconfig[{i}]") for i, x in enumerate(TPC_RECONFIG)],
        "reconfig_override": [rd_checked(0x820A40 + 4 * i, f"reconfig_override[{i}]") for i in range(8)],
        "tpc_status": [rd_checked(0x820C38 + 4 * i, f"tpc_status[{i}]") for i in range(8)],
        "pes_status": [rd_checked(0x820DBC + 4 * i, f"pes_status[{i}]") for i in range(8)],
    }
    out["per_gpc"] = {k: [f"{v:#x}" for v in vs] for k, vs in g.items()}
    gdef, gst = r["GPC_DEFECTIVE"], r["GPC_STATUS"]
    out["sm_ceiling_nondefective"] = sum(2 * (8 - bin(g["tpc_defective"][i] & 0xFF).count("1"))
                                         for i in range(8) if not (gdef >> i) & 1)
    out["sm_status_now"] = sum(2 * (8 - bin(g["tpc_status"][i] & 0xFF).count("1"))
                               for i in range(8) if not (gst >> i) & 1)
    gfw = r["GFW_BOOT_118234"] & 0xFF
    if r["DSOV"] == 1:
        verdict = "under/stock (DSOV=1)"
    elif r["DSOV"] == 0 and r["CRC_STATUS"] == 0x55 and r["PLM_fc"] == 0xFFFFFFFF:
        verdict = "GOLDILOCKS candidate (DSOV=0, CRC=0x55, PLM_fc=0xffffffff)"
    elif r["DSOV"] == 0 and r["CRC_STATUS"] == 0x55:
        verdict = f"hit, not burnable (DSOV=0, CRC=0x55, PLM_fc={r['PLM_fc']:#x})"
    elif r["DSOV"] == 0:
        verdict = f"over (DSOV=0, CRC={r['CRC_STATUS']:#x}, PLM_fc={r['PLM_fc']:#x})"
    else:
        verdict = f"unknown (DSOV={r['DSOV']:#x})"
    out["verdict"] = verdict
    warn = []
    if r["DEBUGCTRL"] != 0:
        warn.append("DEBUGCTRL nonzero: stop, never write it")
    if not _ctrl_is_idle(ctrl):
        warn.append("fuse controller not idle")
    if r["DEVIDA"] & 0xFFFF not in (0x20C2, 0x2082):
        warn.append(f"DEVIDA={r['DEVIDA']:#x} (expected 0x20c2)")
    if gfw == 0x01:
        warn.append("GFW boot progress 0x01: do NOT glitch now, the card is mid-boot")
    out["warnings"] = warn

    rows = []
    if a.rows:
        if any(w.startswith(("DEBUGCTRL", "fuse controller")) for w in warn):
            raise vc.BenchStop("refusing the row read: " + "; ".join(warn))
        orig_addr = rd_checked(ADDR, "FUSEADDR")
        keys = ("EN_SW_OVERRIDE", "DSOV", "EFP", "SBFE", "GPC_STATUS", "FBP_STATUS", "DEBUGCTRL")
        before = {k: rd_checked(REGS[k], k) for k in keys}
        try:
            for _ in range(2):
                cur = []
                for row in range(512):
                    wr(ADDR, row)
                    latched = rd(ADDR)
                    if latched == vc.UNREADABLE or vc.is_poisoned(latched):
                        raise RuntimeError(f"FUSEADDR poisoned/dead ({latched:#010x}) at row {row}")
                    if latched != row:
                        raise RuntimeError(f"FUSEADDR did not latch row {row}")
                    wr(CTRL, 1)
                    _poll_ctrl_idle(rd, row, timeout=0.2)
                    cur.append(rd(DATA))
                rows.append(cur)
            # Clean-path-only FUSEADDR restore (NEVER inside finally on error!).
            c_end = rd(CTRL)
            if _ctrl_is_idle(c_end):
                wr(ADDR, orig_addr)
            else:
                warn.append("controller busy after the row read; FUSEADDR not restored")
        except Exception as e:  # noqa: BLE001 — catch RuntimeError, OSError, etc.
            out["rows_error"] = str(e)
            warn.append("ROW READ FAILED: " + str(e))
            warn.append("row read errored; FUSEADDR not restored and no further BAR0 reads performed")

        if "rows_error" not in out:
            after = {k: rd_checked(REGS[k], k) for k in keys}
            out["state_unchanged_by_row_read"] = before == after
            if before != after:
                warn.append("gate/status registers changed during the row read")
            if len(rows) == 2:
                out["rows_match"] = rows[0] == rows[1]
                out["rows"] = [f"{v:#010x}" for v in rows[0]]
                out["rows_nonzero"] = sum(1 for v in rows[0] if v)
                if not out["rows_match"]:
                    warn.append("ROW READS DIFFER between passes")
        else:
            out["state_unchanged_by_row_read"] = None

    # Do NOT begin --die-id BAR0 accesses if --rows already errored!
    if a.die_id and "rows_error" not in out:
        want = load_die_id(a.die_id)
        if any(w.startswith(("DEBUGCTRL", "fuse controller")) for w in warn):
            raise vc.BenchStop("refusing the die ID read: " + "; ".join(warn))
        try:
            got = read_fuse_rows(rd, wr, lambda: _ctrl_is_idle(rd(CTRL)), DIE_ID_ROWS)
        except Exception as e:  # noqa: BLE001
            out["die_id_error"] = str(e)
            warn.append("DIE ID READ FAILED: " + str(e))
        else:
            bad = [row for row in DIE_ID_ROWS if got[row] != want[row]]
            out["die_id_match"] = not bad
            out["die_id_rows_differing"] = bad
            if bad:
                warn.append(f"DIE ID MISMATCH: fuse rows {bad} differ from {a.die_id}: not the card that dump came from")

    stem = f"{datetime.datetime.now().strftime('%Y%m%dT%H%M%S')}-{a.label}"
    os.makedirs(a.outdir, mode=0o700, exist_ok=True)
    for seq in range(100):
        fn = os.path.join(a.outdir, f"{stem}.json" if seq == 0 else f"{stem}-{seq}.json")
        try:
            fd_out = os.open(fn, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            break
        except FileExistsError:
            continue
    else:
        raise RuntimeError(f"could not allocate unique snapshot file for {stem}")
    with os.fdopen(fd_out, "w") as f:
        json.dump(out, f, indent=1)
    s = out["regs"]
    print(f"{a.bdf} driver={ident['driver']} verdict: {verdict}")
    print(f"  DSOV={s['DSOV']} CRC={s['CRC_STATUS']} EFP={s['EFP']} SBFE={s['SBFE']} EN_SW={s['EN_SW_OVERRIDE']} "
          f"PLM_f8={s['PLM_f8']} PLM_fc={s['PLM_fc']} FUSECTRL={s['FUSECTRL']} GFW={gfw:#04x}")
    print(f"  GPC dis/def/st={s['GPC_DISABLE']}/{s['GPC_DEFECTIVE']}/{s['GPC_STATUS']} "
          f"FBP dis/def/st={s['FBP_DISABLE']}/{s['FBP_DEFECTIVE']}/{s['FBP_STATUS']} "
          f"SM now={out['sm_status_now']} ceiling={out['sm_ceiling_nondefective']} aliases_zero={out['aliases_zero']}/17")
    print(f"  PCIe: MAGIC_BITS_D={s['OPT_PCIE_MAGIC_BITS_D']} BOOT_GEN3_DIS={s['OPT_PCIE_BOOT_GEN3_DIS']} "
          f"DISABLE_GEN3_SPEED={s['OPT_DISABLE_GEN3_SPEED']}")
    if "rows_match" in out:
        print(f"  rows: 512 x2 match={out['rows_match']} nonzero={out['rows_nonzero']} "
              f"state_unchanged={out['state_unchanged_by_row_read']}")
    if "die_id_match" in out:
        print("  die ID (fuse rows 8-15): " + ("match" if out["die_id_match"]
                                                 else f"MISMATCH rows {out['die_id_rows_differing']}"))
    for w in warn:
        print("  WARN: " + w)
    print("  saved " + fn + " (0600; keep it private)")
    return snapshot_rc(out)


if __name__ == "__main__":
    sys.exit(vc.main_guard(main))
