#!/usr/bin/env bash
# run-evolve-worker.sh — what launchd calls for the proactive art arm.
#
# Deliberately dumb, exactly like run.sh: login-like PATH, one pass, exit. The
# decisions (lock, cadence, budget, health gates, the deterministic gate, the
# merge) all live in evolve_worker.py.
#
# SENTINEL_HOME is inherited untouched, so `install-launchd.sh --home DIR`
# points this job at the same instance as the tick.
set -uo pipefail

export HOME="${HOME:?HOME must be set}"
export PATH="/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin:$HOME/.local/bin"

cd "$(dirname "$0")" || exit 1

# One pass, with a hard ceiling. macOS ships no timeout(1), so it is enforced
# here. Sized against the worst LEGITIMATE pass so real work is never killed
# mid-flight: three health runs (3 x 600) + the model (1800) + clone, gate,
# push, PR, merge and the re-read afterwards + overhead.
#
# The ceiling exists because launchd serialises a StartInterval job: a wedged
# pass would otherwise stop the art arm entirely, quietly, forever. If the
# ceiling fires the flock dies with the process, so the next pass is free to
# start — nothing to unwedge by hand.
LIMIT="${EVOLVE_WORKER_LIMIT:-5400}"

/usr/bin/python3 evolve_worker.py "$@" &
PASS=$!
( sleep "$LIMIT"; kill -TERM "$PASS" 2>/dev/null ) &
REAPER=$!

wait "$PASS"; RC=$?
kill "$REAPER" 2>/dev/null || true

if [ "$RC" -ne 0 ]; then
    printf '%s evolve worker exited %s\n' "$(date -u +%Y-%m-%dT%H:%M:%SZ)" "$RC" \
        >> logs/run.log 2>/dev/null || true
fi
exit "$RC"
