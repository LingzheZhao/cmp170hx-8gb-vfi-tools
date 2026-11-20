#!/bin/bash
# cycle.sh: power-cycle the bench host (clean poweroff, wait, Wake-on-LAN or IPMI power on) and print its uptime when
# it answers again. Settings from bench.conf (or BENCH_CONF=<file>). OFFWAIT defaults to 45 s here.
# Exit codes: 0 back up, 1 never came back, 2 config, 7 did not go down.
set -u
HERE=$(cd "$(dirname "$0")" && pwd)
CONF=${BENCH_CONF:-$HERE/bench.conf}
[ -f "$CONF" ] || { echo "no $CONF: copy bench.conf.example to bench.conf and fill it in"; exit 2; }
OFFWAIT=${OFFWAIT:-45}
# shellcheck source=/dev/null
. "$CONF"
. "$HERE/power.sh"
: "${BENCH_SSH:?set BENCH_SSH in $CONF}"
power_cycle
