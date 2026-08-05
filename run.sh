#!/usr/bin/env bash
# run.sh — what launchd and openrappter both call.
#
# Deliberately dumb: set up a login-like PATH, run one tick, exit. All the
# decisions live in sentinel.py. Keeping the wrapper trivial means a bug here
# cannot silently stop the watch — the worst it can do is fail loudly.
set -uo pipefail

export HOME="${HOME:?HOME must be set}"
export PATH="/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin:$HOME/.local/bin"

cd "$(dirname "$0")" || exit 1

# One tick, with a hard ceiling.
#
# The comment that used to live here said launchd would "just start the next
# one on schedule" if a tick hung. That is false, and it was load-bearing:
# launchd SERIALISES a StartInterval job. Measured with a 45s job on a 10s
# interval -- every start followed the previous END, never overlapping. So a
# hung tick does not get overtaken, it stops the watch entirely, and the
# heartbeat freezes at whatever it last wrote.
#
# macOS ships no timeout(1), so the ceiling is enforced here. It is set above
# evolve_timeout_s (1800) so a legitimate long tick is never killed mid-repair;
# anything past that is hung, not working.
LIMIT="${SENTINEL_TICK_LIMIT:-2100}"

/usr/bin/python3 sentinel.py &
TICK=$!
( sleep "$LIMIT"; kill -TERM "$TICK" 2>/dev/null ) &
REAPER=$!

wait "$TICK"; RC=$?
kill "$REAPER" 2>/dev/null || true

if [ "$RC" -ne 0 ]; then
    printf '%s tick exited %s\n' "$(date -u +%Y-%m-%dT%H:%M:%SZ)" "$RC" \
        >> logs/run.log 2>/dev/null || true
fi
exit "$RC"
