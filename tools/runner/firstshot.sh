#!/bin/bash
# firstshot.sh CARD 'JSON list of steps': one power cycle per step, each step fired as the FIRST shot of a fresh boot.
# Short steps are padded with the runner defaults (pulses 3, pw 198, v_restore 750, fast 1) and reps is forced to 1.
# Every boot is a fresh power cycle: an inherited NOCYCLE=1 is ignored. Stops on goldilocks (bootrun's ON_HOLD hook),
# a held partial (exit 13), and on any stop line or a bootrun exit other than 0 (exhausted) or 10 (tipped): exit 2.
# A tipped boot (lost, poison) goes on to the next step, also when the park after a lost shot failed (the next
# step starts from a fresh power cycle). Full output of every boot: $LOGDIR (default logs/ next to this script).
cd "$(dirname "$0")" || exit 2
CARD=$1; STEPS=$2; n=$(python3 -c "import json,sys;print(len(json.loads(sys.argv[1])))" "$STEPS") || { echo "FIRSTSHOT: bad step list: stop"; exit 2; }
LOGDIR=${LOGDIR:-$PWD/logs/firstshot-$CARD-$(date +%Y%m%dT%H%M%S)}; mkdir -p "$LOGDIR" || exit 2; echo "full logs: $LOGDIR"
# Summary lines shown per boot (the full output of every boot is kept in $LOGDIR), and the lines that stop the run.
SHOW="START|cell |step |TIPPED|MISTIMED|exhausted|NO DIP|GOLDILOCKS|HOLD|PARTIAL|ON_HOLD|CONTROLLER OFF|STOP|FAILED|MISMATCH|park|prime_at|release_at|WARN|FAIL|ABORT|hidden|poison|glitch vd=|boot pmbus|observed|rail_code=|hold_at|unlock:|sentinel|root port|Traceback|Error|RESULT"
STOPRE="NO DIP|IDENTITY MISMATCH|PREFLIGHT FAILED|PLAN SHA MISMATCH|PLAN SHA FAILED|HOST GUARD|HOLD LINE BUT RUNNER RC|never came up|did not go down|NOT A FRESH BOOT|POWER STILL ON|CONTROLLER OFF|PARK FAILED|MISTIMED|AssertionError|Traceback|STOP:|TEMP GATE FAILED|RUNNER TRANSFER FAILED"
for i in $(seq 0 $((n-1))); do
  ST=$(python3 -c "import json,sys;s=list(json.loads(sys.argv[1])[int(sys.argv[2])]);d={2:3,3:198,4:750,5:1}
for k in range(2,6):
    if len(s)<=k: s.append(d[k])
if len(s)==6: s.append(1)
else: s[6]=1
print(json.dumps([s]))" "$STEPS" "$i") || { echo "FIRSTSHOT: bad step $i: stop"; exit 2; }
  echo "######## first-shot boot $((i+1))/$n: $ST $(date +%H:%M:%S)"
  out=$(NOCYCLE=0 bash bootrun.sh "$CARD" "$ST" 2>&1); rc=$?
  echo "$out" > "$LOGDIR/boot-$((i+1)).log"
  echo "$out" | grep -E "$SHOW" | grep -v "^   timing"
  echo "$out" | grep -qE "HOLD SEEN|GOLDILOCKS HELD" && { echo "FIRSTSHOT: goldilocks held, see the hold report (bootrun rc $rc)"; exit "$rc"; }
  echo "$out" | grep -q "PARTIAL HELD" && { echo "FIRSTSHOT: partial held for read-only inspection"; exit 13; }
  echo "$out" | grep -qE "$STOPRE" && { echo "FIRSTSHOT: stop for review (bootrun rc $rc)"; exit 2; }
  [ "$rc" = 0 ] || [ "$rc" = 10 ] || { echo "FIRSTSHOT: bootrun exit $rc: stop for review"; exit 2; }
done
echo "FIRSTSHOT: done"
