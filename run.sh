#!/usr/bin/env bash
# run.sh — what launchd and openrappter both call.
#
# Deliberately dumb: set up a login-like PATH, run one tick, exit. All the
# decisions live in sentinel.py. Keeping the wrapper trivial means a bug here
# cannot silently stop the watch — the worst it can do is fail loudly.
set -uo pipefail

export HOME="${HOME:-/Users/kodywildfeuer}"
export PATH="/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin:$HOME/.local/bin"

cd "$HOME/rapp-sentinel" || exit 1

# One tick. Never let a hung child hold the slot forever — launchd will just
# start the next one on schedule and two overlapping ticks would double-spend.
exec /usr/bin/python3 sentinel.py
