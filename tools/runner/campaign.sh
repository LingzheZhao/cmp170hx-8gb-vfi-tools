#!/bin/bash
# campaign.sh CARD SEQFILE [MAXBOOTS]: run bootrun boot after boot; when a boot tips (lost / poison) at step N,
# the next boot continues from step N+1 (a monotone ladder only: when shot 0 and shot 1 of a boot can disagree, use
# firstshot.sh or PARK_EACH=1). Stops on GOLDILOCKS (bootrun's ON_HOLD hook), a held partial (exit 13), any stop line (CONTROLLER OFF,
# park failure, MISTIMED, Traceback, STOP:, NO DIP, preflight/identity/plan/temperature/transfer failures) or a bootrun
# exit other than 0/10 (exit 2). A sequence that runs out because its last step tipped ends with exit 2 "ended on a tip";
# "sequence done" (exit 0) only when a boot exhausted it. Extra env (FLOOR, LOSTRUN, ...) passes through to bootrun.
# Full output of every boot: $LOGDIR (default logs/ next to this script).
CARD=$1; SEQFILE=$2; MAX=${3:-8}; start=0
case "$SEQFILE" in /*) ;; *) SEQFILE=$PWD/$SEQFILE ;; esac   # a relative path is the caller's, read before the cd
cd "$(dirname "$0")" || exit 2
n=$(python3 -c "import json,sys;print(len(json.load(open(sys.argv[1]))))" "$SEQFILE") || { echo "CAMPAIGN: bad sequence file: stop"; exit 2; }
LOGDIR=${LOGDIR:-$PWD/logs/campaign-$CARD-$(date +%Y%m%dT%H%M%S)}; mkdir -p "$LOGDIR" || exit 2; echo "full logs: $LOGDIR"
# Summary lines shown per boot (the full output of every boot is kept in $LOGDIR), and the lines that stop the run.
SHOW="START|cell |step |TIPPED|MISTIMED|exhausted|NO DIP|GOLDILOCKS|HOLD|PARTIAL|ON_HOLD|CONTROLLER OFF|STOP|FAILED|MISMATCH|park|prime_at|release_at|WARN|FAIL|ABORT|hidden|poison|glitch vd=|boot pmbus|observed|rail_code=|hold_at|unlock:|sentinel|root port|Traceback|Error|RESULT"
STOPRE="NO DIP|IDENTITY MISMATCH|PREFLIGHT FAILED|PLAN SHA MISMATCH|PLAN SHA FAILED|HOST GUARD|HOLD LINE BUT RUNNER RC|never came up|did not go down|NOT A FRESH BOOT|POWER STILL ON|CONTROLLER OFF|park after lost shot FAILED|PARK FAILED|MISTIMED|AssertionError|Traceback|STOP:|TEMP GATE FAILED|RUNNER TRANSFER FAILED"
for b in $(seq 1 $MAX); do
  [ $start -ge $n ] && { echo "CAMPAIGN: ended on a tip past the last step"; exit 2; }
  SEQ=$(python3 -c "import json,sys;print(json.dumps(json.load(open(sys.argv[1]))[int(sys.argv[2]):]))" "$SEQFILE" "$start")
  echo "######## campaign boot $b, steps $start..$((n-1)) $(date +%H:%M:%S)"
  out=$(bash bootrun.sh "$CARD" "$SEQ" 2>&1); rc=$?
  echo "$out" > "$LOGDIR/boot-$b.log"
  echo "$out" | grep -E "$SHOW" | grep -v "^   timing"
  echo "$out" | grep -qE "HOLD SEEN|GOLDILOCKS HELD" && { echo "CAMPAIGN: goldilocks held, see the hold report (bootrun rc $rc)"; exit "$rc"; }
  echo "$out" | grep -qE "PARTIAL HELD|STALL DSOV0 HELD" && { echo "CAMPAIGN: partial held for read-only inspection"; exit 13; }
  echo "$out" | grep -qE "$STOPRE" && { echo "CAMPAIGN: stop for review (bootrun rc $rc)"; exit 2; }
  [ "$rc" = 0 ] && echo "$out" | grep -q "sequence exhausted" && { echo "CAMPAIGN: sequence done"; exit 0; }
  [ "$rc" = 10 ] || { echo "CAMPAIGN: bootrun exit $rc: stop for review"; exit 2; }
  s=$(echo "$out" | grep -oE "(TIPPED at step|^step) [0-9]+" | grep -oE "[0-9]+" | tail -1)
  [ -n "$s" ] || { echo "CAMPAIGN: could not find the tipping step: stop"; exit 3; }
  start=$((start + s + 1))
done
echo "CAMPAIGN: max boots reached at step $start"; exit 2
