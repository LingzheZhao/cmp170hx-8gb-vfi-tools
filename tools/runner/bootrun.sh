#!/bin/bash
# bootrun.sh <card> '<SEQ json>': one run of bisect_shots.py on a freshly booted bench host, one data point per boot.
# Runs on a controller machine that reaches the bench host over ssh (key auth). Settings: bench.conf next to this
# file (copy bench.conf.example), or BENCH_CONF=<file>. <card> selects the function card_<card> in that file.
#
# power cycle (Wake-on-LAN or IPMI) -> fresh-boot guard -> plan checksum (when the card names plan files) -> copy the
# runner to the bench -> host guard -> I2C preflight -> card identity -> temperature gate -> bisect_shots.py (stops at
# the first shot that is not "under") -> on a ">>> GOLDILOCKS HELD" line with runner status 0: run ON_HOLD if set
# (and NO_ON_HOLD is not 1), else stop and report the held state. Nothing here burns fuses.
#
# env: NOCYCLE=1 (use the current boot), FLOOR (mV), TMIN (raw READ_TEMP1), TSTART (raw, optional upper bound: the gate
#      opens only inside [TMIN, TSTART]), TCAP (s), OFFWAIT (s), POWER (wol|ipmi|manual),
#      RUNTO (s, run time limit), RUNNER (runner file to copy; default bisect_shots.py here), BOOTRUN_LOG (the run
#      log; default $LOG_DIR/bootrun-<card>.last), NO_ON_HOLD=1 (never run ON_HOLD, whatever bench.conf says; the
#      hunter sets it), and for the runner: TRACE, PARK_EACH, REPEAT, SHOTCAP, LOSTRUN, RESTORE_WAIT, CELLLOST,
#      CLASSIFY_WRITE, SENTINEL, BAD0_FOLLOWUP, GFW_WAIT, LOST_NOPARK, PARTIAL_CONTINUE, SHOOT_THROUGH_STALLS, STALLRUN,
#      SENT0_CONTINUE, POISON_RESET
#      (SENTINEL_EXPECT comes from the card function)
# Exit codes: 1 bench never came up, 2 usage or config (or a run log it cannot write), 3 host guard or preflight
# failed, 4 plan checksum failed or mismatched, 5 identity or die ID mismatch, 6 not a fresh boot (up longer than
# FRESH_MAX, or the same boot id after the power cycle), 7 bench did not power off, 8 runner transfer failed,
# 16 temperature gate (TMIN not reached within TCAP: no shot); otherwise the runner's own exit code (0 sequence done
# or goldilocks held, 7 park failed, 9 controller off, 10 tipped, 13 partial held, 14 mistimed, 15 DSOV 0 held during a GFW stall,
# ...; see bisect_shots.py), 124 when
# RUNTO cut the run, or ON_HOLD's exit code after a hold. The last line is "RESULT: ..." or the hold report.
set -u
HERE=$(cd "$(dirname "$0")" && pwd); TOOLS=$(dirname "$HERE")
CONF=${BENCH_CONF:-$HERE/bench.conf}
[ -f "$CONF" ] || { echo "no $CONF: copy bench.conf.example to bench.conf and fill it in"; exit 2; }
# shellcheck source=/dev/null
. "$CONF"
. "$HERE/power.sh"
: "${BENCH_SSH:?set BENCH_SSH in $CONF}" "${BENCH_DIR:?set BENCH_DIR in $CONF}" "${BENCH_PY:?set BENCH_PY in $CONF}"
[ $# -eq 2 ] || { echo "usage: $0 <card> '<SEQ json>'"; exit 2; }
CARD=$1; SEQ=$2
TAG=""; GPC=""; SMC=""; CARD_BDF=""; PLAN_LOCAL=""; PLAN_REMOTE=""; PLAN_FILES=""; SENTINEL_EXPECT=""; DIE_DUMP=""
declare -F "card_$CARD" >/dev/null || { echo "unknown card '$CARD': define card_$CARD in $CONF"; exit 2; }
"card_$CARD"
[ -n "$TAG" ] && [ -n "$GPC" ] && [ -n "$SMC" ] || { echo "card_$CARD must set TAG, GPC and SMC"; exit 2; }
RUNNER=${RUNNER:-$HERE/bisect_shots.py}
TRACE=${TRACE:-0}; PARK_EACH=${PARK_EACH:-0}; FLOOR=${FLOOR:-338}; TMIN=${TMIN:-375}; TCAP=${TCAP:-300}
PORT=${PICO_PORT:-/dev/ttyACM0}; H=$BENCH_SSH

bid0=""
if [ "${NOCYCLE:-0}" != 1 ]; then
  # boot id before the cycle (empty when the bench is off): power.sh's wol/manual paths take the first missed ping as
  # "off", so a host that never went down (ping filtered, ssh through a jump host) is caught here instead
  # shellcheck disable=SC2046
  bid0=$(ssh -o ConnectTimeout=3 $(ssh_batch) "$H" cat /proc/sys/kernel/random/boot_id 2>/dev/null)
  power_cycle || exit $?
fi
# The boot id goes into the log, so two runs can be told apart as two boots.
# shellcheck disable=SC2046
u=$(bench_uptime); bid=$(ssh -o ConnectTimeout=3 $(ssh_batch) "$H" cat /proc/sys/kernel/random/boot_id 2>/dev/null)
echo "bench up ${u:-?}s boot_id ${bid:-?}"; [ "${u:-99999}" -lt "${FRESH_MAX:-900}" ] || { echo "NOT A FRESH BOOT: stop"; exit 6; }
[ -z "$bid0" ] || { [ -n "$bid" ] && [ "$bid" != "$bid0" ]; } \
  || { echo "NOT A FRESH BOOT: boot_id ${bid:-unreadable} after the power cycle, $bid0 before: stop"; exit 6; }
DIE=""
if [ -n "$DIE_DUMP" ]; then   # die ID: fuse rows 8-15 of the card must match this checksummed plan dump
  case " $PLAN_FILES " in *" $DIE_DUMP "*) DIE="$PLAN_REMOTE/$DIE_DUMP" ;;
    *) echo "card_$CARD: DIE_DUMP ($DIE_DUMP) must be one of PLAN_FILES, so it is checksummed: stop"; exit 2 ;; esac
elif [ -n "$PLAN_FILES" ]; then
  echo "WARNING: card_$CARD names plan files but no DIE_DUMP: the die ID is not checked (two cards with the same GPC/SM/ceiling look identical)"
fi
if [ -n "$PLAN_FILES" ]; then
  # Hash lines include the file names; both sides must hash every plan file, and match line for line.
  nf=$(echo "$PLAN_FILES" | wc -w)
  # shellcheck disable=SC2086
  a=$(cd "$PLAN_LOCAL" && sha256sum $PLAN_FILES) || { echo "PLAN SHA FAILED (local hash): stop"; exit 4; }
  b=$(ssh "$H" "cd $(printf %q "$PLAN_REMOTE") && sha256sum $PLAN_FILES") || { echo "PLAN SHA FAILED (bench hash): stop"; exit 4; }
  [ "$(echo "$a" | grep -cE '^[0-9a-f]{64}  ')" = "$nf" ] && [ "$(echo "$b" | grep -cE '^[0-9a-f]{64}  ')" = "$nf" ] \
    || { echo "PLAN SHA FAILED (need $nf hashes on each side): stop"; exit 4; }
  [ "$a" = "$b" ] && echo "PLAN SHA MATCH ($CARD)" || { echo "PLAN SHA MISMATCH: stop"; exit 4; }
fi
ssh "$H" "mkdir -p $(printf %q "$BENCH_DIR")" && scp -q "$RUNNER" "$H:$BENCH_DIR/bisect_shots.py" \
  && scp -q "$HERE/park.py" "$HERE/preflight.py" "$TOOLS/vfi_snap.py" "$TOOLS/vfi_common.py" "$H:$BENCH_DIR/" \
  || { echo "RUNNER TRANSFER FAILED: stop"; exit 8; }
L=${BOOTRUN_LOG:-${LOG_DIR:-.}/bootrun-$CARD.last}; mkdir -p "$(dirname "$L")"
# the hold check below greps this file: empty it first, so a log tee cannot write never shows an old run's hold
: 2>/dev/null > "$L" || { echo "RUN LOG $L is not writable: stop"; exit 2; }
XENV="REPEAT=${REPEAT:-1} SHOTCAP=${SHOTCAP:-450} LOSTRUN=${LOSTRUN:-8} RESTORE_WAIT=${RESTORE_WAIT:-8.0} CELLLOST=${CELLLOST:-0} CLASSIFY_WRITE=${CLASSIFY_WRITE:-0} SENTINEL=${SENTINEL:-0x820410} BAD0_FOLLOWUP=${BAD0_FOLLOWUP:-0} GFW_WAIT=${GFW_WAIT:-5} LOST_NOPARK=${LOST_NOPARK:-0} PARTIAL_CONTINUE=${PARTIAL_CONTINUE:-0} SHOOT_THROUGH_STALLS=${SHOOT_THROUGH_STALLS:-0} STALLRUN=${STALLRUN:-8} SENT0_CONTINUE=${SENT0_CONTINUE:-0} POISON_RESET=${POISON_RESET:-0} SENTINEL_EXPECT=$SENTINEL_EXPECT"
ssh "$H" "bash -s -- $(printf '%q ' "$TAG" "$SEQ" "$GPC" "$SMC" "$FLOOR" "$TMIN" "$TCAP" "$TRACE" "$PARK_EACH" "$XENV" "${RUNTO:-900}" "$BENCH_DIR" "$BENCH_PY" "${BENCH_TASKSET:-}" "$PORT" "$CARD_BDF" "${GLITCH_CMDLINE:-}" "${MASK_PCIEHP:-0}" "${TSTART:-99999}" "$DIE")" <<'R' | tee "$L"
TAG=$1; SEQ=$2; GPC=$3; SMC=$4; FLOOR=$5; TMIN=$6; TCAP=$7; TRACE=$8; PARK_EACH=$9; XENV=${10}; RUNTO=${11}
D=${12}; PY=${13}; TS=${14}; PORT=${15}; BDF=${16}; CMDW=${17}; MASK=${18}; TSTART=${19:-99999}; DIE=${20:-}
PYV="$TS $PY"
stop(){ echo "$1: stop"; exit "$2"; }
cd /tmp || exit 3
# host guard: the glitch boot, no NVIDIA driver, no lockdown, one card on pci-stub (or none), the Pico, decode on, GFW done
[ -z "$CMDW" ] || grep -qw -- "$CMDW" /proc/cmdline || stop "HOST GUARD: '$CMDW' is not on the kernel command line (not the glitch boot)" 3
lsmod | grep -q '^nvidia ' && stop "HOST GUARD: the nvidia module is loaded" 3
lsmod | grep -q '^nouveau ' && stop "HOST GUARD: the nouveau module is loaded" 3
[ ! -e /sys/kernel/security/lockdown ] || grep -q '\[none\]' /sys/kernel/security/lockdown || stop "HOST GUARD: kernel lockdown is on (Secure Boot?)" 3
if [ -z "$BDF" ]; then
  mapfile -t B < <(lspci -Dn -d 10de:20c2 | awk '{print $1}')
  [ ${#B[@]} -eq 1 ] || stop "HOST GUARD: expected exactly one 10de:20c2 card, found ${#B[@]}" 3
  BDF=${B[0]}
fi
[ -e "/sys/bus/pci/devices/$BDF" ] || stop "HOST GUARD: no PCI device $BDF" 3
# vendor and device from sysfs (cached by the kernel, no config access) before any config or BAR0 access: a stale bdf
# must not reach another device
ID="$(cat "/sys/bus/pci/devices/$BDF/vendor" 2>/dev/null):$(cat "/sys/bus/pci/devices/$BDF/device" 2>/dev/null)"
[ "$ID" = 0x10de:0x20c2 ] || stop "HOST GUARD: $BDF is $ID, not a 10de:20c2 card" 3
DRV=$(basename "$(readlink "/sys/bus/pci/devices/$BDF/driver" 2>/dev/null)" 2>/dev/null)
[ -z "$DRV" ] || [ "$DRV" = pci-stub ] || stop "HOST GUARD: driver $DRV is bound to $BDF (only pci-stub is allowed)" 3
[ -e "$PORT" ] || stop "HOST GUARD: no Pico serial port at $PORT" 3
[ $(( 0x$(setpci -s "$BDF" COMMAND) & 2 )) -eq 0 ] && { setpci -s "$BDF" COMMAND=0x0002:0x0002; echo "memory decode was off on $BDF: turned on"; }
GFW=$(python3 -I - "$BDF" <<'P'
import mmap, os, sys
p = f"/sys/bus/pci/devices/{sys.argv[1]}"
cfg = open(p + "/config", "rb").read(8)
if cfg[:4] == b"\xff\xff\xff\xff": print("LINKDOWN"); sys.exit()
if not (cfg[4] & 2): print("NODECODE"); sys.exit()
fd = os.open(p + "/resource0", os.O_RDONLY | os.O_SYNC); m = mmap.mmap(fd, 0x200000, mmap.MAP_SHARED, mmap.PROT_READ); os.close(fd)
print(hex(memoryview(m).cast("I")[0x118234 // 4] & 0xff))
P
)
case "$GFW" in 0x1) stop "HOST GUARD: GFW boot progress reads 0x01 (still booting)" 3;; LINKDOWN|NODECODE|"") stop "HOST GUARD: config space says $GFW" 3;; esac
echo "card $BDF gfw=$GFW pico=$PORT"
CRP=$(basename "$(dirname "$(readlink -f "/sys/bus/pci/devices/$BDF")")")
if [ "$MASK" = 1 ] && lspci -s "$CRP" -vv 2>/dev/null | grep -q "HotPlug+"; then setpci -s "$CRP" CAP_EXP+18.w=0000:1028; echo "pciehp interrupts masked on $CRP (SltCtl=$(setpci -s "$CRP" CAP_EXP+18.w))"; fi
temp(){ $PYV -I -c "import sys
from ga100vfi.glitch import Card
c = Card(sys.argv[1]); d = c.read(0x25, 0x8D, 2); print(d[0] | d[1] << 8); c.close()" "$PORT" 2>/dev/null; }
timeout 60 $PYV -I "$D/preflight.py" "$PORT" > /tmp/vfi-pf.out 2>&1; grep -E "PREFLIGHT|pico v" /tmp/vfi-pf.out
grep -q "PREFLIGHT OK" /tmp/vfi-pf.out || { echo "PREFLIGHT FAILED: stop"; tail -4 /tmp/vfi-pf.out; exit 3; }
timeout 60 $PYV -E -s "$D/vfi_snap.py" --bdf "$BDF" --port "$PORT" --label preflight --outdir "$D/snaps" ${DIE:+--die-id "$DIE"} > /tmp/vfi-snap.out 2>&1
grep -E "verdict|GPC dis" /tmp/vfi-snap.out
grep -q "GPC dis/def/st=$GPC/" /tmp/vfi-snap.out && grep -q "$SMC" /tmp/vfi-snap.out && echo "IDENTITY OK: $TAG" || { echo "IDENTITY MISMATCH: stop"; exit 5; }
if [ -n "$DIE" ]; then
  grep -q "die ID (fuse rows 8-15): match" /tmp/vfi-snap.out && echo "DIE ID OK: $TAG" \
    || { grep -E "die ID|DIE ID" /tmp/vfi-snap.out; echo "DIE ID MISMATCH or unreadable: stop"; exit 5; }
fi
# one line per poll, so a watcher (tools/cimp/cimp.py watch) can show the card warming or cooling into the band
t0=$(date +%s); while [ $(( $(date +%s) - t0 )) -lt "$TCAP" ]; do v=$(temp)
  echo "   gate temp_raw=${v:-unreadable} want [$TMIN, $TSTART] $(date +%H:%M:%S)"
  [ -n "$v" ] && [ "$v" -ge "$TMIN" ] && [ "$v" -le "$TSTART" ] && break; sleep 5; done
# the check uses the reading that passed (a second read could bounce one ADC step out of the band)
if ! { [ "$v" -ge "$TMIN" ] && [ "$v" -le "$TSTART" ]; } 2>/dev/null; then
  echo "TEMP GATE FAILED: temp_raw ${v:-unreadable} did not reach TMIN $TMIN (and stay at or below TSTART $TSTART) within TCAP ${TCAP}s: no shot"; exit 16; fi
echo "START temp_raw=$v $(date +%H:%M:%S)"
echo "run env: $XENV timeout ${RUNTO}s"
# shellcheck disable=SC2086
env $XENV VFI_BDF="$BDF" VFI_PORT="$PORT" VFI_TRACE_DIR="$D" FLOOR="$FLOOR" TRACE="$TRACE" PARK_EACH="$PARK_EACH" timeout "$RUNTO" $PYV -I "$D/bisect_shots.py" "$TAG" "$SEQ"
rc=$?
echo "end temp_raw=$(temp) runner_rc=$rc"
exit $rc
R
rc=${PIPESTATUS[0]}
if grep -q "^>>> GOLDILOCKS HELD" "$L"; then
  [ "$rc" = 0 ] || { echo "HOLD LINE BUT RUNNER RC $rc: no ON_HOLD, stop for review"; exit "$rc"; }
  echo "HOLD SEEN $(date +%H:%M:%S): $TAG is held in the goldilocks state (rail under PMBus control, not parked)"
  if [ -n "${ON_HOLD:-}" ] && [ "${NO_ON_HOLD:-0}" != 1 ]; then
    CARD=$CARD TAG=$TAG BENCH_SSH=$BENCH_SSH BENCH_DIR=$BENCH_DIR RUN_LOG=$L bash -c "$ON_HOLD"; hrc=$?
    echo "ON_HOLD rc=$hrc"; exit "$hrc"
  fi
  if [ -n "${ON_HOLD:-}" ]; then echo -n "NO_ON_HOLD=1, the ON_HOLD hook was not run: stopping here."
  else echo -n "No ON_HOLD hook set: stopping here."; fi
  echo " Read the held state on the bench (vfi_snap.py in $BENCH_DIR), then park (park.py) or power-cycle."
  exit 0
fi
r=$(grep -E "TIPPED|MISTIMED at|PARTIAL HELD|STALL DSOV0 HELD|sequence exhausted|CONTROLLER OFF|NO DIP|STOP|FAILED|MISMATCH|HOST GUARD" "$L" | head -1)
echo "RESULT: ${r:-see log} (runner rc $rc)"
exit "$rc"
