#!/bin/bash
# Host simulation of glitch_cmd() in firmware/pwmvid.c: build it for two versions of a pico-ga100vfi tree and compare
# them with check3.py (push2-legal shots identical, overlap shots run with the release at t = 0, setup refusal kept).
# usage: tools/hostsim/run.sh /path/to/pico-ga100vfi [BASE_REF]
#   the "new" side is the checked-out firmware/pwmvid.c; BASE_REF (default HEAD~1) is the push2 side, e.g. the commit
#   before firmware/0011 was applied. Needs gcc and python3.
set -eu
H=$(cd "$(dirname "$0")" && pwd); P=$(cd "$1" && pwd); BASE=${2:-HEAD~1}
O=$(mktemp -d); trap 'rm -rf "$O"' EXIT
mkdir -p "$O/base" "$O/new"; cp "$H/harness.c" "$H/stubs.h" "$H/check3.py" "$O/"
git -C "$P" show "$BASE:firmware/pwmvid.c" > "$O/base/pwmvid.c"
python3 "$H/extract.py" "$O/base/pwmvid.c" "$O/base/glitch_section.c"
python3 "$H/extract.py" "$P/firmware/pwmvid.c" "$O/new/glitch_section.c"
for s in base new; do
  gcc -O1 -w -DBOARD_RP2350A -DHAVE_ALARM2 -I"$O" -I"$O/$s" -I"$P/firmware" -o "$O/$s/sim" "$O/harness.c" -lm
done
python3 "$O/check3.py"
