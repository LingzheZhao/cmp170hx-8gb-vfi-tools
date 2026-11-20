# shellcheck shell=bash
# power.sh: power-cycle helpers for bootrun.sh and cycle.sh (sourced, after bench.conf).
# POWER=wol : OS poweroff over ssh, wait until the host stops answering ping, wait, Wake-on-LAN.
# POWER=ipmi: OS poweroff over ssh, the BMC confirms the host is off (hard power-off if a clean shutdown stalls),
#             wait, power on through the BMC.

bench_addr() { echo "${BENCH_ADDR:-${BENCH_SSH#*@}}"; }

bench_uptime() { ssh -o ConnectTimeout=3 -o BatchMode=yes "$BENCH_SSH" "cut -d. -f1 /proc/uptime" 2>/dev/null; }

ipmi() {
  [ -n "${IPMI_HOST:-}" ] && [ -n "${IPMI_USER:-}" ] && [ -n "${IPMI_PASSFILE:-}" ] || { echo "POWER=ipmi needs IPMI_HOST, IPMI_USER and IPMI_PASSFILE"; return 2; }
  # shellcheck disable=SC2086
  timeout 30 ipmitool ${IPMI_OPTS:--I lanplus} -H "$IPMI_HOST" -U "$IPMI_USER" -f "$IPMI_PASSFILE" "$@"
}

wol() {
  [ -n "${WOL_MAC:-}" ] || { echo "POWER=wol needs WOL_MAC"; return 2; }
  python3 - "$WOL_MAC" ${WOL_BROADCAST:-255.255.255.255} <<'PY'
import socket, sys
mac = sys.argv[1].replace(":", "").replace("-", "")
p = b"\xff" * 6 + bytes.fromhex(mac) * 16
s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM); s.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
for a in sys.argv[2:]:
    for port in (9, 7): s.sendto(p, (a, port))
PY
}

# power_cycle: returns 0 when the bench answers ssh again, 7 when it did not go down, 1 when it never came back.
power_cycle() {
  local power=${POWER:-wol} offwait=${OFFWAIT:-15} off i u
  echo "cycle ($power): clean shutdown $(date +%H:%M:%S)"
  ssh -o ConnectTimeout=5 "$BENCH_SSH" 'sync; systemctl poweroff' >/dev/null 2>&1
  if [ "$power" = ipmi ]; then
    off=0; for i in $(seq 1 30); do ipmi chassis power status 2>/dev/null | grep -q "is off" && { off=1; break; }; sleep 4; done
    if [ $off != 1 ]; then echo "no clean off after 120 s: hard power off"; ipmi chassis power off >/dev/null; sleep 8
      ipmi chassis power status 2>/dev/null | grep -q "is off" || { echo "POWER STILL ON: stop"; return 7; }; fi
    echo "power off confirmed by BMC; waiting ${offwait}s"; sleep "$offwait"
    ipmi chassis power on >/dev/null && echo "power on $(date +%H:%M:%S)"
  else
    off=0; for i in $(seq 1 40); do ping -c1 -W1 "$(bench_addr)" >/dev/null 2>&1 || { off=1; break; }; sleep 3; done
    [ $off = 1 ] || { echo "bench did not go down after a clean shutdown (no BMC here, needs a hand on the power button): stop"; return 7; }
    echo "bench off the network; waiting $((offwait + 15))s for soft-off"; sleep $((offwait + 15))
    wol; echo "power on (Wake-on-LAN) $(date +%H:%M:%S)"
  fi
  u=""; for i in $(seq 1 60); do u=$(bench_uptime); [ -n "$u" ] && break
    [ "$power" != ipmi ] && [ "$i" = 30 ] && { wol; echo "no wake after 150 s: resent Wake-on-LAN"; }; sleep 5; done
  [ -n "$u" ] || { echo "bench never came up: stop"; return 1; }
  echo "bench up ${u}s"
}
