#!/usr/bin/env bash
# install-launchd.sh — run the sentinel every 15 minutes, surviving reboot.
#
#   ./install-launchd.sh                        # instance state lives beside the code
#   ./install-launchd.sh --home DIR             # instance state lives in DIR
#   ./install-launchd.sh --with-evolve-worker   # also load the art arm's own job
#
# --home writes SENTINEL_HOME into every job's EnvironmentVariables dict, so
# one checkout can serve several instances (see paths.py). Without it nothing
# changes: no SENTINEL_HOME in the plists, paths byte-identical to before.
#
# --with-evolve-worker loads com.rapp.evolve-worker, the separate job that runs
# proactive art (evolve_worker.py) OUTSIDE the 15-minute health tick. It is
# opt-in, and it is also loaded automatically when the instance's config.json
# already says {"evolve_worker": {"enabled": true}} — the installer follows the
# config rather than inventing a second place to turn the same thing on. An
# existing install that says neither keeps exactly the jobs it has today.
set -euo pipefail
DIR="$(cd "$(dirname "$0")" && pwd)"

SENTINEL_HOME_DIR=""
WITH_EVOLVE_WORKER=0
while [ $# -gt 0 ]; do
  case "$1" in
    --home)   SENTINEL_HOME_DIR="${2:?--home needs a directory}"; shift 2 ;;
    --home=*) SENTINEL_HOME_DIR="${1#--home=}"; shift ;;
    --with-evolve-worker) WITH_EVOLVE_WORKER=1; shift ;;
    *) echo "usage: $0 [--home DIR] [--with-evolve-worker]" >&2; exit 1 ;;
  esac
done

stamp_home() {  # write SENTINEL_HOME into a rendered plist, idempotently
  [ -n "$SENTINEL_HOME_DIR" ] || return 0
  /usr/libexec/PlistBuddy -c "Delete :EnvironmentVariables:SENTINEL_HOME" "$1" 2>/dev/null || true
  /usr/libexec/PlistBuddy -c "Add :EnvironmentVariables:SENTINEL_HOME string $SENTINEL_HOME_DIR" "$1"
}

# The instance the jobs will serve — the same derivation paths.py makes.
INSTANCE_HOME="${SENTINEL_HOME_DIR:-$DIR}"

config_enables_evolve_worker() {  # {"evolve_worker": {"enabled": true}}
  /usr/bin/python3 - "$INSTANCE_HOME/config.json" <<'PY' 2>/dev/null
import json, sys
try:
    cfg = json.load(open(sys.argv[1], encoding="utf-8"))
except Exception:
    sys.exit(1)
block = cfg.get("evolve_worker")
sys.exit(0 if isinstance(block, dict) and block.get("enabled") else 1)
PY
}

config_allows_nightwatch() {
  /usr/bin/python3 - "$INSTANCE_HOME/config.json" <<'PY' 2>/dev/null
import json, sys
try:
    cfg = json.load(open(sys.argv[1], encoding="utf-8"))
except Exception:
    sys.exit(1)
mode = str(cfg.get("notification_mode") or "all").strip().lower()
destination = cfg.get("report_number") or cfg.get("notify_handle")
sys.exit(0 if mode == "all" and destination else 1)
PY
}

LABEL="com.rapp.neighborhood-watch"
PLIST="$HOME/Library/LaunchAgents/$LABEL.plist"

sed "s|__DIR__|$DIR|g; s|__HOME__|$HOME|g" \
  "$DIR/$LABEL.plist.template" > "$PLIST"
stamp_home "$PLIST"

launchctl unload "$PLIST" 2>/dev/null || true
launchctl load "$PLIST"
echo "loaded $LABEL — every 15 min"
echo "  logs:      $DIR/logs/"
echo "  stop:      touch $DIR/STOP"
echo "  uninstall: launchctl unload $PLIST && rm $PLIST"

# the dashboard server — always on, so the page is just a bookmark
DLABEL="com.rapp.watch-dashboard"
DPLIST="$HOME/Library/LaunchAgents/$DLABEL.plist"
sed "s|__DIR__|$DIR|g; s|__HOME__|$HOME|g" "$DIR/$DLABEL.plist.template" > "$DPLIST"
stamp_home "$DPLIST"
launchctl unload "$DPLIST" 2>/dev/null || true
launchctl load "$DPLIST"
echo "loaded $DLABEL — http://localhost:9797"

# the overnight play-by-play texter (optional — needs report_number in config)
NLABEL="com.rapp.nightwatch"
NPLIST="$HOME/Library/LaunchAgents/$NLABEL.plist"
sed "s|__DIR__|$DIR|g; s|__HOME__|$HOME|g" "$DIR/$NLABEL.plist.template" > "$NPLIST"
stamp_home "$NPLIST"
if config_allows_nightwatch; then
  launchctl enable "gui/$(id -u)/$NLABEL" 2>/dev/null || true
  launchctl unload "$NPLIST" 2>/dev/null || true
  launchctl load "$NPLIST"
  echo "loaded $NLABEL — texts a play-by-play every 90 min"
else
  launchctl unload "$NPLIST" 2>/dev/null || true
  launchctl disable "gui/$(id -u)/$NLABEL" 2>/dev/null || true
  echo "disabled $NLABEL — notification_mode is not all"
fi

# Messages automation is safe from the logged-in Aqua session, but nightwatch
# itself stays queue-only so a hidden TCC prompt can never wedge report
# generation. This short, serialized job owns delivery of everything queued.
OLABEL="com.rapp.outbox-drain"
OPLIST="$HOME/Library/LaunchAgents/$OLABEL.plist"
sed "s|__DIR__|$DIR|g; s|__HOME__|$HOME|g" \
  "$DIR/$OLABEL.plist.template" > "$OPLIST"
stamp_home "$OPLIST"
launchctl enable "gui/$(id -u)/$OLABEL" 2>/dev/null || true
launchctl unload "$OPLIST" 2>/dev/null || true
launchctl load "$OPLIST"
echo "loaded $OLABEL — drains queued iMessage reports every 5 min"

# The proactive art arm, on its own clock (opt-in).
#
# It is a SEPARATE job because launchd serialises a StartInterval job: a
# 15-30 minute model run inside the 15-minute health tick is 15-30 minutes
# with nobody measuring the estate. Health keeps ticking; this one thinks.
if [ "$WITH_EVOLVE_WORKER" -eq 1 ] || config_enables_evolve_worker; then
  ELABEL="com.rapp.evolve-worker"
  EPLIST="$HOME/Library/LaunchAgents/$ELABEL.plist"
  sed "s|__DIR__|$DIR|g; s|__HOME__|$HOME|g" \
    "$DIR/$ELABEL.plist.template" > "$EPLIST"
  stamp_home "$EPLIST"
  launchctl enable "gui/$(id -u)/$ELABEL" 2>/dev/null || true
  launchctl unload "$EPLIST" 2>/dev/null || true
  launchctl load "$EPLIST"
  echo "loaded $ELABEL — proactive art every 30 min, gated by its own cadence"
  echo "  dry run:   SENTINEL_HOME=$INSTANCE_HOME /usr/bin/python3 $DIR/evolve_worker.py --dry-run"
  if ! config_enables_evolve_worker; then
    echo "  NOTE: the job is loaded but $INSTANCE_HOME/config.json does not set"
    echo "        evolve_worker.enabled=true, so every pass will skip. See"
    echo "        config.example.json."
  fi
else
  # Not enabled — so make sure it is not still RUNNING from a previous
  # install. A job left loaded after its config was turned off is a model
  # spending money on behalf of a decision that was reversed.
  ELABEL="com.rapp.evolve-worker"
  EPLIST="$HOME/Library/LaunchAgents/$ELABEL.plist"
  if [ -f "$EPLIST" ]; then
    launchctl unload "$EPLIST" 2>/dev/null || true
    rm -f "$EPLIST"
    echo "unloaded and removed $ELABEL — art is disabled for this instance"
  else
    echo "skipped com.rapp.evolve-worker — proactive art still runs inside the tick"
  fi
  echo "  enable: set evolve_worker.enabled=true in $INSTANCE_HOME/config.json,"
  echo "          then rerun with --with-evolve-worker"
fi
