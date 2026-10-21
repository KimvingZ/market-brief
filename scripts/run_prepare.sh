#!/usr/bin/env bash
# Usage: scripts/run_prepare.sh RUN DATA_DIR LEDGER_FILE WORK_DIR
#
# Pulls the latest `data` branch and runs `python -m brief.run prepare` until
# the snapshot is fresh enough for this run (up to 15 minutes, checking once a
# minute). If it is still stale after that, prepares anyway with
# --allow-stale, and the report carries a "data delayed" banner.
set -u
RUN=$1
DATA=$2
LEDGER=$3
WORK=$4
deadline=$(( $(date +%s) + 900 ))
while :; do
  git -C "$DATA" fetch -q --depth 1 origin data && git -C "$DATA" reset -q --hard origin/data
  python -m brief.run prepare --run "$RUN" --data "$DATA" --ledger "$LEDGER" --work "$WORK"
  code=$?
  [ "$code" -eq 0 ] && exit 0
  [ "$code" -ne 3 ] && exit "$code"
  [ "$(date +%s)" -ge "$deadline" ] && break
  sleep 60
done
echo "data still stale after 15 minutes: preparing anyway"
python -m brief.run prepare --run "$RUN" --data "$DATA" --ledger "$LEDGER" --work "$WORK" --allow-stale
