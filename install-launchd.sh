#!/usr/bin/env bash
# install-launchd.sh — run the sentinel every 15 minutes, surviving reboot.
set -euo pipefail
DIR="$(cd "$(dirname "$0")" && pwd)"
LABEL="com.rapp.neighborhood-watch"
PLIST="$HOME/Library/LaunchAgents/$LABEL.plist"

sed "s|__DIR__|$DIR|g; s|__HOME__|$HOME|g" \
  "$DIR/$LABEL.plist.template" > "$PLIST"

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
launchctl unload "$DPLIST" 2>/dev/null || true
launchctl load "$DPLIST"
echo "loaded $DLABEL — http://localhost:9797"

# the overnight play-by-play texter (optional — needs report_number in config)
NLABEL="com.rapp.nightwatch"
NPLIST="$HOME/Library/LaunchAgents/$NLABEL.plist"
sed "s|__DIR__|$DIR|g; s|__HOME__|$HOME|g" "$DIR/$NLABEL.plist.template" > "$NPLIST"
launchctl unload "$NPLIST" 2>/dev/null || true
launchctl load "$NPLIST"
echo "loaded $NLABEL — texts a play-by-play every 90 min"

# Messages automation is safe from the logged-in Aqua session, but nightwatch
# itself stays queue-only so a hidden TCC prompt can never wedge report
# generation. This short, serialized job owns delivery of everything queued.
OLABEL="com.rapp.outbox-drain"
OPLIST="$HOME/Library/LaunchAgents/$OLABEL.plist"
sed "s|__DIR__|$DIR|g; s|__HOME__|$HOME|g" \
  "$DIR/$OLABEL.plist.template" > "$OPLIST"
launchctl unload "$OPLIST" 2>/dev/null || true
launchctl load "$OPLIST"
echo "loaded $OLABEL — drains queued iMessage reports every 5 min"
