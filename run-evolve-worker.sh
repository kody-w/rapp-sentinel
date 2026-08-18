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

# One pass, with a hard ceiling — enforced on the whole PROCESS TREE.
#
# `set -m` puts the pass in its own process group, so `kill -TERM -$PID`
# reaches the worker, the maker, every sub-sentinel, and anything they
# spawned. Signalling only the worker used to leave a 30-minute model running
# with nobody waiting on it, holding a workspace we had already given up on.
#
# The worker installs its own SIGTERM handler: it kills its tracked children,
# deletes the workspace and only then releases the lock. The KILL sweep below
# is the backstop for a worker too wedged to run its own handler.
#
# The ceiling is the sum of the worst LEGITIMATE pass, not a round number:
#   3 health runs      3 x 600 = 1800   (start, pre-write, pre-merge)
#   fan-out            total_timeout_s  = 1200
#   maker              evolve_timeout_s = 1800
#   clone/gate/push/PR/merge/verify      ~600
#   view URL probe (bounded backoff)     ~100
#   overhead                             ~600
#                                       ------
#                                        6100  -> 6600 with headroom
LIMIT="${EVOLVE_WORKER_LIMIT:-6600}"
GRACE="${EVOLVE_WORKER_GRACE:-45}"

set -m
/usr/bin/python3 evolve_worker.py "$@" &
PASS=$!

(
  sleep "$LIMIT"
  kill -TERM -"$PASS" 2>/dev/null || kill -TERM "$PASS" 2>/dev/null
  sleep "$GRACE"
  kill -KILL -"$PASS" 2>/dev/null || kill -KILL "$PASS" 2>/dev/null
) &
REAPER=$!

wait "$PASS"; RC=$?
kill "$REAPER" 2>/dev/null || true

# Whatever happened above, nothing this job started may still be running when
# it exits: launchd's next pass must not race a model from the last one.
kill -TERM -"$PASS" 2>/dev/null || true

if [ "$RC" -ne 0 ]; then
    printf '%s evolve worker exited %s\n' "$(date -u +%Y-%m-%dT%H:%M:%SZ)" "$RC" \
        >> logs/run.log 2>/dev/null || true
fi
exit "$RC"
