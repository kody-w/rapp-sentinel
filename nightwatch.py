#!/usr/bin/env python3
"""nightwatch.py — text the play-by-play.

The sentinel already texts on state change. That is an alarm, not a report:
if nothing breaks you hear nothing, and silence is indistinguishable from the
loop being dead. This sends a periodic account of the shift either way, which
is the difference between an alert and a colleague checking in.

It reports what CHANGED since the last text, not a running total, so two
reports in a row never say the same thing. A quiet stretch gets one line — a
quiet night should be cheap to read, not padded to look like work.

  python3 nightwatch.py            # report since the last one
  python3 nightwatch.py --since=6  # force a window, in hours
  python3 nightwatch.py --dry      # print, don't send
"""

import json
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import neighborhood as NB

HOME = Path(__file__).resolve().parent
STATE = HOME / "state"
MARK = STATE / "last_report.json"
DASH = "http://localhost:9797"


def cfg():
    try:
        return json.loads((HOME / "config.json").read_text(encoding="utf-8"))
    except Exception:
        return {}


def parse(u):
    for f in ("%Y-%m-%dT%H:%M:%S.%fZ", "%Y-%m-%dT%H:%M:%SZ"):
        try:
            return datetime.strptime(u, f).replace(tzinfo=timezone.utc)
        except ValueError:
            pass
    return None


def send(text, to):
    """Queue first, then attempt delivery.

    Under launchd, osascript blocks forever on a TCC prompt nobody can see, so
    a direct send loses the report AND looks like a quiet night. Queueing first
    means the worst case is a delayed message, never a missing one.
    """
    import outbox
    outbox.enqueue(text, to)
    if "--queue-only" in sys.argv:
        # launchd invokes it this way on purpose. Attempting a send from a
        # background context does not merely fail — it wedges Messages for
        # everyone, including the contexts that CAN send. Learned by wedging it.
        return True, "queued (send deferred to a permitted context)"
    sent, kept, why = outbox.drain()
    return sent > 0, (why if kept else "")


def build(since):
    """Everything that happened after `since`, as a short shift update."""
    events = []
    for slug in NB.NEIGHBORS:
        for f in NB.read_chain(slug):
            t = parse(f["utc"])
            if t and t > since:
                events.append({"slug": slug, "kind": f["kind"], "t": t,
                               "p": f["payload"]})
    events.sort(key=lambda e: e["t"])

    ticks = [e for e in events if e["kind"] == "sentinel.tick"]
    acts = [e for e in events if e["kind"] == "neighbor.acted"]

    # status transitions are the story; a run of identical ticks is not
    transitions = []
    prev = None
    for e in ticks:
        st = e["p"].get("status")
        if st != prev:
            transitions.append((e["t"], st, e["p"].get("failed") or []))
            prev = st

    roll = NB.roll_call()
    anchors = {}
    try:
        anchors = NB.check_anchors()
    except Exception:
        pass

    now = datetime.now().astimezone()
    lines = [f"Neighborhood Watch — {now:%-I:%M %p}"]

    if not events:
        lines.append("")
        lines.append("No frames since the last report. The loop may have stopped — "
                     "worth a look.")
        return "\n".join(lines), True

    span = (datetime.now(timezone.utc) - since).total_seconds() / 3600
    lines.append(f"Last {span:.1f}h: {len(ticks)} checks.")

    if len(transitions) <= 1 and (not transitions or transitions[0][1] == "healthy"):
        lines.append("Held healthy the whole time. Nothing to do.")
    else:
        lines.append("")
        for t, st, failed in transitions:
            mark = {"healthy": "recovered", "critical": "WENT CRITICAL",
                    "degraded": "degraded"}.get(st, st)
            detail = f" ({', '.join(failed)})" if failed else ""
            lines.append(f"{t.astimezone():%-I:%M %p} — {mark}{detail}")

    for a in acts:
        out = str(a["p"].get("outcome", "")).strip()
        # a run that produced no SENTINEL_RESULT line has nothing to report;
        # printing an empty bullet just makes the text look broken
        if not out:
            continue
        verb = ("contributed" if "CONTRIBUTED" in out else
                "declined" if "DECLINED" in out else "acted")
        lines.append("")
        lines.append(f"{a['t'].astimezone():%-I:%M %p} — {a['slug']} {verb}:")
        lines.append(out.split("—", 1)[-1].strip()[:220] if "—" in out else out[:220])

    # integrity is the one thing worth waking up for
    broken = [k for k, v in roll.items() if not v["chain_ok"]]
    cut = [k for k, v in anchors.items() if v.get("truncated")]
    stale = [k for k, v in roll.items() if not v["alive"] and v["frames"]]
    if broken or cut:
        lines.append("")
        lines.append(f"⚠️ INTEGRITY: chains broken {broken or 'none'}, "
                     f"truncated {cut or 'none'}. This is the one that matters.")
    elif stale:
        lines.append("")
        lines.append(f"Stale watcher: {', '.join(stale)} — not reporting.")

    lines.append("")
    lines.append(DASH)
    return "\n".join(lines), bool(broken or cut or not events)


def main():
    c = cfg()
    to = None
    for a in sys.argv[1:]:
        if a.startswith("--to="):
            to = a.split("=", 1)[1]
    to = to or c.get("report_number") or c.get("notify_handle")
    if not to:
        print("no destination: set report_number in config.json")
        return 1

    since = None
    for a in sys.argv[1:]:
        if a.startswith("--since="):
            since = datetime.now(timezone.utc) - timedelta(hours=float(a.split("=")[1]))
    if since is None:
        try:
            since = datetime.fromisoformat(
                json.loads(MARK.read_text())["at"].replace("Z", "+00:00"))
        except Exception:
            since = datetime.now(timezone.utc) - timedelta(hours=2)

    text, _ = build(since)

    if "--dry" in sys.argv:
        print(text)
        return 0

    ok, err = send(text, to)
    print(f"sent={ok} to={to}" + (f" err={err}" if err else ""))
    if ok:
        STATE.mkdir(exist_ok=True)
        MARK.write_text(json.dumps(
            {"at": datetime.now(timezone.utc).isoformat(timespec="seconds")}) + "\n")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
