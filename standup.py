#!/usr/bin/env python3
"""standup.py — the morning shift report.

You wake up and want the same thing you'd want from an offshore team: what did
you work on, what shipped, what's blocked, and can I check any of it myself.

Nothing here is a new log. The append-only source of truth already exists — the
three rapp/1 frame chains — and this only reads them. That matters: a dashboard
with its own private log is a second copy that can disagree with reality. This
one renders the record and re-verifies it from genesis while doing so, so an
unverified chain shows up as a red banner rather than a nice chart.

Every line that claims something happened carries a link to the evidence:
a commit, a pull request, or the full local transcript of the run.

  python3 standup.py            # regenerate dashboard/index.html
  python3 standup.py --open     # ...and open it
"""

import html
import ipaddress
import json
import re
import secrets
import shutil
import socket
import subprocess
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import neighborhood as NB
from paths import HOME

OUT = HOME / "dashboard"
OUT.mkdir(exist_ok=True)
LOGS = HOME / "logs"
REPORTS = HOME / "state" / "reports"
SHARED_REPORTS = HOME / "state" / "shared-reports"

def _cfg():
    p = HOME / "config.json"
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return {}


def instance_name():
    return str(_cfg().get("instance_name") or
               "Rappterbook & Rappterverse Neighborhood Watch").strip()


# Which repos to surface commits from, and (optionally) a commons to show
# contributions from. Both live in config.json so this file stays generic.
REPOS = _cfg().get("watch_repos", [])
COLLECTIVE = _cfg().get("commons_repo", "")
BOT_AUTHORS = ("rappterbook-bot", "Kody Wildfeuer", "github-actions")


def esc(s):
    return html.escape(str(s), quote=True)


def sh(args, timeout=45):
    try:
        r = subprocess.run(args, capture_output=True, text=True, timeout=timeout)
        return r.stdout if r.returncode == 0 else ""
    except Exception:
        return ""


def since_iso(hours):
    return (datetime.now(timezone.utc) - timedelta(hours=hours)).isoformat()


def parse_utc(s):
    for fmt in ("%Y-%m-%dT%H:%M:%S.%fZ", "%Y-%m-%dT%H:%M:%SZ", "%Y-%m-%dT%H:%M:%S%z"):
        try:
            d = datetime.strptime(s, fmt)
            return d if d.tzinfo else d.replace(tzinfo=timezone.utc)
        except ValueError:
            continue
    return None


# ── evidence gathering ──────────────────────────────────────────────────────

def recent_commits(repo, hours):
    """Commits the loop could plausibly be responsible for, with links."""
    out = sh(["gh", "api", f"repos/{repo}/commits?per_page=40", "--jq",
              '[.[] | {sha: .sha, msg: (.commit.message | split("\\n")[0]), '
              'author: .commit.author.name, date: .commit.author.date}]'])
    if not out:
        return []
    try:
        items = json.loads(out)
    except Exception:
        return []
    cutoff = datetime.now(timezone.utc) - timedelta(hours=hours)
    rows = []
    for c in items:
        d = parse_utc(c["date"])
        if not d or d < cutoff:
            continue
        rows.append({
            "repo": repo, "sha": c["sha"][:8], "msg": c["msg"],
            "author": c["author"], "when": d,
            "url": f"https://github.com/{repo}/commit/{c['sha']}",
            # a fix reads differently from a heartbeat; surface the difference
            "notable": bool(re.match(r"^(fix|feat|perf|refactor|art|retract)", c["msg"], re.I)),
        })
    return rows


def art_submissions():
    if not COLLECTIVE:
        return []
    out = sh(["gh", "api", f"repos/{COLLECTIVE}/contents/submissions/index.json",
              "--jq", ".content"])
    if not out:
        return []
    try:
        import base64
        data = json.loads(base64.b64decode(out.strip().strip('"')).decode())
    except Exception:
        return []
    rows = []
    for s in data.get("submissions", []):
        slug = s["slug"]
        rows.append({
            "slug": slug, "title": s.get("title", slug),
            "url": f"https://github.com/{COLLECTIVE}/tree/main/submissions/{slug}",
            "raw": f"https://raw.githubusercontent.com/{COLLECTIVE}/main/"
                   f"submissions/{slug}/piece.svg",
            "when": parse_utc(s.get("submitted_at", "")) or datetime.now(timezone.utc),
        })
    return rows


def transcript_for(kind, slug, when):
    """Find the local transcript nearest a recorded action, so a claim on the
    dashboard can be opened and read in full rather than taken on trust."""
    pats = {"evolve": f"evolve-{slug}-*.log", "escalation": "escalation-*.log"}
    best, bestdiff = None, timedelta(hours=2)
    for p in LOGS.glob(pats.get(kind, "*.log")):
        m = re.search(r"(\d{8})-(\d{6})", p.name)
        if not m:
            continue
        try:
            t = datetime.strptime(m.group(1) + m.group(2), "%Y%m%d%H%M%S").replace(
                tzinfo=timezone.utc)
        except ValueError:
            continue
        diff = abs(t - when)
        if diff < bestdiff:
            best, bestdiff = p, diff
    return best


def action_outcome(payload):
    """Normalize evolve and repair records into one reportable outcome."""
    outcome = str(payload.get("outcome") or "").strip()
    if outcome:
        return outcome, "evolve"
    result = str(payload.get("result") or "").strip()
    act = str(payload.get("act") or "acted").upper()
    if result:
        return f"{act} — {result}", "escalation"
    return json.dumps(payload, sort_keys=True), (
        "escalation" if payload.get("act") else "evolve")


# ── the shift ───────────────────────────────────────────────────────────────

def shift(hours):
    cutoff = datetime.now(timezone.utc) - timedelta(hours=hours)
    events = []
    for slug in NB.NEIGHBORS:
        for f in NB.read_chain(slug):
            t = parse_utc(f["utc"])
            if not t or t < cutoff:
                continue
            events.append({"slug": slug, "kind": f["kind"], "when": t,
                           "payload": f["payload"], "seq": f["seq"],
                           "hash": f["frame_hash"]})
    events.sort(key=lambda e: e["when"])
    return events


def summarize(events, commits, art, hours):
    ticks = [e for e in events if e["kind"] == "sentinel.tick"]
    acts = [e for e in events if e["kind"] == "neighbor.acted"]
    crit = [e for e in ticks if e["payload"].get("status") == "critical"]
    # A repair is now something the sentinel RECORDS, not something the report
    # guesses. This used to read "critical tick followed by a healthy one",
    # which for an intermittent check is a sample of a flapping signal rather
    # than evidence of an act: copilot showed one of last night's two credited
    # recoveries was recorded 85 seconds BEFORE the repair finished, by a
    # different process. The re-probe was the real evidence and it was being
    # thrown away, while the inference was published.
    verifs = [e for e in events if e["kind"] == "repair.verified"]
    recovered = sum(1 for e in verifs if e["payload"].get("landed"))
    attempted = len([e for e in acts if e["payload"].get("act") == "repair"])
    # Repairs that predate the chain fix are real and must not vanish from the
    # report just because the record cannot vouch for them. They are shown with
    # their provenance stated: a mutable JSON file, not a sealed frame.
    unsealed = []
    try:
        hist = json.loads((HOME / "state" / "escalations.json").read_text())
        cut = datetime.now(timezone.utc) - timedelta(hours=hours)
        for h in hist:
            if h.get("mode") != "repair":
                continue
            try:
                at = datetime.fromisoformat(h["at"])
                at = at if at.tzinfo else at.replace(tzinfo=timezone.utc)
            except Exception:
                continue
            if at >= cut and not any(
                    e["payload"].get("issue") == h.get("key") for e in verifs):
                unsealed.append({"at": at, "key": h.get("key"),
                                 "result": h.get("result", "")})
    except Exception:
        pass
    return {
        "hours": hours,
        "checks": len(ticks),
        "critical": len(crit),
        "recovered": recovered,
        "repairs_attempted": attempted,
        "unsealed_repairs": unsealed,
        "decisions": len(acts),
        "contributed": sum(1 for a in acts if "CONTRIBUTED" in str(a["payload"].get("outcome", ""))),
        "declined": sum(1 for a in acts if "DECLINED" in str(a["payload"].get("outcome", ""))),
        "notable_commits": sum(1 for c in commits if c["notable"]),
        "art": len(art),
    }


CSS = """
:root{--ink:#141413;--cream:#faf9f5;--tile:#efe9de;--coral:#cc785c;--muted:#73726f;
--good:#3f7d4e;--bad:#b4442e}
*{box-sizing:border-box}
body{margin:0;background:var(--cream);color:var(--ink);
font:16px/1.6 "EB Garamond",Georgia,serif;-webkit-font-smoothing:antialiased}
.wrap{max-width:1080px;margin:0 auto;padding:34px 22px 80px}
.mono,code{font-family:ui-monospace,SFMono-Regular,Menlo,monospace}
.mono{font-size:11px;letter-spacing:.11em;text-transform:uppercase;color:var(--muted)}
h1{font-size:40px;margin:.25em 0 .1em;font-weight:600}
h2{font-size:22px;margin:38px 0 10px;font-weight:600}
.sub{color:var(--muted);margin:0 0 22px}
.banner{padding:12px 16px;border-radius:8px;margin:16px 0;font-size:15px}
.ok{background:rgba(63,125,78,.09);border:1px solid rgba(63,125,78,.35)}
.warn{background:rgba(204,120,92,.09);border:1px solid rgba(204,120,92,.4)}
.bad{background:rgba(180,68,46,.10);border:1px solid rgba(180,68,46,.45)}
.cards{display:grid;grid-template-columns:repeat(auto-fit,minmax(150px,1fr));gap:12px;margin:18px 0}
.card{background:#fff;border:1px solid rgba(20,20,19,.12);border-radius:10px;padding:14px 16px}
.card .n{font-size:30px;font-weight:600;line-height:1.1}
.card .l{font-size:12px;color:var(--muted)}
table{width:100%;border-collapse:collapse;margin:10px 0 4px;font-size:15px}
th{text-align:left;font-family:ui-monospace,Menlo,monospace;font-size:10px;
letter-spacing:.11em;text-transform:uppercase;color:var(--muted);
border-bottom:1px solid rgba(20,20,19,.16);padding:7px 8px}
td{padding:8px;border-bottom:1px solid rgba(20,20,19,.07);vertical-align:top}
tr:hover td{background:rgba(20,20,19,.02)}
a{color:var(--coral)}
.pill{display:inline-block;padding:1px 8px;border-radius:999px;font-size:11px;
font-family:ui-monospace,Menlo,monospace}
.p-ok{background:rgba(63,125,78,.14);color:var(--good)}
.p-bad{background:rgba(180,68,46,.14);color:var(--bad)}
.p-mut{background:rgba(20,20,19,.07);color:var(--muted)}
.ev{font-family:ui-monospace,Menlo,monospace;font-size:11px}
.empty{color:var(--muted);font-style:italic;padding:14px 8px}
footer{margin-top:44px;padding-top:16px;border-top:1px solid rgba(20,20,19,.14)}
"""


def render(hours=14):
    events = shift(hours)
    commits = [c for r in REPOS for c in recent_commits(r, hours)]
    commits.sort(key=lambda c: c["when"], reverse=True)
    art = art_submissions()
    roll = NB.roll_call()
    s = summarize(events, commits, art, hours)

    verified = all(v["chain_ok"] for v in roll.values())
    anchors = NB.check_anchors()
    verdict = json.loads((HOME / "state" / "last_verdict.json").read_text()) \
        if (HOME / "state" / "last_verdict.json").exists() else {"status": "unknown", "summary": ""}

    now = datetime.now().astimezone()
    name = instance_name()
    p = [f'<!doctype html><html lang="en"><head><meta charset="utf-8">',
         '<meta name="viewport" content="width=device-width,initial-scale=1">',
         '<meta http-equiv="refresh" content="300">',
         f'<title>Overnight Shift Report — {esc(name)}</title>',
         f'<style>{CSS}</style></head><body><div class="wrap">']

    p.append(f'<div class="mono">{esc(name)}</div>')
    p.append('<h1>Overnight shift report</h1>')
    p.append(f'<p class="sub">Last {hours} hours &middot; generated '
             f'{now:%a %d %b %Y, %H:%M %Z}. Every claim below links to something you can check.</p>')

    # Undelivered alerts first. If texts are not getting out, every other
    # reassurance on this page is worth less — you would be reading "healthy"
    # while the thing that is supposed to interrupt you cannot.
    try:
        import outbox
        ob = outbox.status()
    except Exception:
        ob = {"pending": 0}
    if ob.get("pending"):
        p.append(f'<div class="banner bad"><strong>{ob["pending"]} text(s) undelivered'
                 f'{" — oldest " + str(int(ob["oldest_minutes"])) + "m" if ob.get("oldest_minutes") else ""}.'
                 f'</strong> The persistent Aqua-session outbox drainer has not cleared '
                 f'the queue. Run <code>python3 outbox.py drain</code> from a terminal '
                 f'and inspect <code>logs/outbox-drain.err.log</code>. Until then alerts '
                 f'are queued, not sent.</div>')

    if s.get("unsealed_repairs"):
        rows = "".join(
            f'<tr><td>{esc(u["at"].strftime("%H:%M"))}Z</td><td><code>{esc(u["key"])}</code></td>'
            f'<td>{esc(u["result"][:180])}</td></tr>' for u in s["unsealed_repairs"])
        p.append('<div class="banner warn"><strong>'
                 f'{len(s["unsealed_repairs"])} repair(s) recorded OUTSIDE the chain.</strong> '
                 'These happened and were verified at the time, but they predate the fix that '
                 'seals repairs into frames — so they live in a mutable JSON file that any '
                 'process could rewrite, and no neighbor can verify them. '
                 'Shown because omitting real work is its own dishonesty.'
                 f'<table class="mini">{rows}</table></div>')

    # integrity first: if the record can't be trusted, nothing under it matters
    if verified:
        rev = [k for k, v in anchors.items() if v.get("revised")]
        if rev:
            p.append(f'<div class="banner bad"><strong>RECORD REVISED — {esc(rev)}.</strong> '
                     'A chain verifies clean but no longer reproduces a digest this '
                     'neighborhood witnessed earlier. An interior frame was rewritten '
                     'after the fact.</div>')
        else:
            p.append('<div class="banner ok"><strong>Record verified.</strong> All three chains '
                     're-verified from genesis against <code>rapp/1</code> §7.5, and every '
                     'frame — not just the head — still reproduces the digests the external '
                     'anchor witnessed. '
                     '<span class="dim">§7.5 alone would not license that: <code>prev</code> '
                     'binds the predecessor\'s <code>payload_hash</code>, so a rewritten '
                     'interior payload perturbs two frames and never reaches the head. '
                     'openrappter proved it by forging its own DECLINE into a CONTRIBUTE '
                     'with every guard green. The per-frame digest is what closed it.'
                     '</span></div>')
    else:
        broken = [k for k, v in roll.items() if not v["chain_ok"]]
        p.append(f'<div class="banner bad"><strong>CHAIN INTEGRITY FAILURE — {esc(broken)}.</strong> '
                 'A watcher\'s own record no longer verifies. Treat everything below as suspect.</div>')

    st = verdict.get("status", "unknown")
    cls = {"healthy": "ok", "degraded": "warn", "critical": "bad"}.get(st, "warn")
    p.append(f'<div class="banner {cls}"><strong>Right now: {esc(st)}.</strong> '
             f'{esc(verdict.get("summary", ""))}</div>')

    p.append('<div class="cards">')
    for n, l in ((s["checks"], "checks run"), (s["critical"], "found critical"),
                 (s["recovered"], "recovered"), (s["decisions"], "decisions made"),
                 (s["notable_commits"], "code changes"), (s["art"], "pieces in commons")):
        p.append(f'<div class="card"><div class="n">{n}</div><div class="l">{l}</div></div>')
    p.append('</div>')

    # ── what it decided ──
    p.append('<h2>Decisions it made on its own</h2>')
    acts = [e for e in events if e["kind"] == "neighbor.acted"]
    if acts:
        p.append('<table><tr><th>when</th><th>neighbor</th><th>outcome</th><th>evidence</th></tr>')
        for a in reversed(acts):
            out, transcript_kind = action_outcome(a["payload"])
            upper = out.upper()
            tag = ("p-ok" if any(word in upper for word in ("CONTRIBUTED", "FIXED"))
                   else "p-mut" if any(word in upper for word in ("DECLINED", "NO_ACTION"))
                   else "p-bad")
            word = out.split(",")[0].split("—")[0].strip()[:12] or "?"
            t = transcript_for(transcript_kind, a["slug"], a["when"])
            # relative, not file:// — a file:// link from an http page is
            # blocked by the browser, and this report is served over http
            ev = (f'<a href="/logs/{t.name}">full transcript</a>' if t else
                  f'<span class="ev">frame {a["hash"][:12]}…</span>')
            p.append(f'<tr><td class="ev">{a["when"].astimezone():%H:%M}</td>'
                     f'<td>{esc(a["slug"])}</td>'
                     f'<td><span class="pill {tag}">{esc(word)}</span> {esc(out[:150])}</td>'
                     f'<td>{ev}</td></tr>')
        p.append('</table>')
    else:
        p.append('<div class="empty">No autonomous decisions in this window.</div>')

    # ── what shipped ──
    p.append('<h2>Code it changed</h2>')
    notable = [c for c in commits if c["notable"]]
    if notable:
        p.append('<table><tr><th>when</th><th>repo</th><th>change</th><th>verify</th></tr>')
        for c in notable[:25]:
            p.append(f'<tr><td class="ev">{c["when"].astimezone():%H:%M}</td>'
                     f'<td class="ev">{esc(c["repo"].split("/")[1])}</td>'
                     f'<td>{esc(c["msg"][:110])}</td>'
                     f'<td><a class="ev" href="{c["url"]}">{esc(c["sha"])}</a></td></tr>')
        p.append('</table>')
        routine = len(commits) - len(notable)
        if routine:
            p.append(f'<p class="sub">Plus {routine} routine state/heartbeat commits, hidden.</p>')
    else:
        p.append('<div class="empty">No code changes in this window.</div>')

    # ── what it made ──
    p.append('<h2>What it contributed to the commons</h2>')
    if art:
        p.append('<table><tr><th>piece</th><th>look</th><th>verify</th></tr>')
        for a in sorted(art, key=lambda x: x["when"], reverse=True):
            p.append(f'<tr><td><strong>{esc(a["title"])}</strong><br>'
                     f'<span class="ev">{esc(a["slug"])}</span></td>'
                     f'<td><a href="{a["raw"]}">view the piece</a></td>'
                     f'<td><a class="ev" href="{a["url"]}">source + statement</a></td></tr>')
        p.append('</table>')
    else:
        p.append('<div class="empty">Nothing in the commons yet.</div>')

    # ── the crew ──
    p.append('<h2>The crew</h2>')
    p.append('<table><tr><th>neighbor</th><th>role</th><th>frames</th><th>last seen</th>'
             '<th>record</th></tr>')
    for slug, v in roll.items():
        alive = ('<span class="pill p-ok">alive</span>' if v["alive"]
                 else '<span class="pill p-bad">stale</span>')
        chain = ('<span class="pill p-ok">verified</span>' if v["chain_ok"]
                 else '<span class="pill p-bad">BROKEN</span>')
        age = "—" if v["age_minutes"] is None else f'{v["age_minutes"]:.0f}m ago'
        p.append(f'<tr><td><strong>{esc(slug)}</strong></td><td>{esc(v["role"])}</td>'
                 f'<td class="ev">{v["frames"]}</td><td class="ev">{alive} {age}</td>'
                 f'<td>{chain}<br><span class="ev">{esc(v["chain_detail"][:44])}</span></td></tr>')
    p.append('</table>')

    # ── the raw shift log ──
    p.append('<h2>Shift log</h2>')
    p.append('<p class="sub">The append-only record itself, newest first. '
             'This is the source everything above is derived from.</p>')
    if events:
        p.append('<table><tr><th>when</th><th>neighbor</th><th>kind</th><th>what</th>'
                 '<th>frame</th></tr>')
        for e in reversed(events[-120:]):
            pay = e["payload"]
            if e["kind"] == "sentinel.tick":
                what = f'status {pay.get("status")}' + (
                    f' — {", ".join(pay.get("failed", []))}' if pay.get("failed") else "")
            elif e["kind"] == "watcher.attested":
                what = f'{"alive" if pay.get("alive") else "NOT ALIVE"} — {pay.get("detail", "")}'
            elif e["kind"] == "neighbor.acted":
                what = action_outcome(pay)[0][:120]
            else:
                what = json.dumps(pay)[:110]
            p.append(f'<tr><td class="ev">{e["when"].astimezone():%H:%M:%S}</td>'
                     f'<td class="ev">{esc(e["slug"])}</td>'
                     f'<td class="ev">{esc(e["kind"])}</td>'
                     f'<td>{esc(what)}</td>'
                     f'<td class="ev">#{e["seq"]} {esc(e["hash"][:10])}…</td></tr>')
        p.append('</table>')
    else:
        p.append('<div class="empty">No frames in this window — the loop may not be running.</div>')

    p.append('<footer class="mono">append-only · rapp/1 frames · re-verified at render · '
             'auto-refresh 5 min · nothing here is stored twice</footer>')
    p.append('</div></body></html>')

    (OUT / "index.html").write_text("".join(p), encoding="utf-8")
    return s, verified


def portable_snapshot(hours=14, rebuild=True):
    """Write an immutable report that remains readable away from localhost."""
    if rebuild or not (OUT / "index.html").is_file():
        render(hours)
    page = (OUT / "index.html").read_text(encoding="utf-8")
    page = re.sub(r'<meta http-equiv="refresh"[^>]*>', "", page)

    transcripts = []

    def inline_transcript(match):
        name = Path(match.group(1)).name
        target = LOGS / name
        anchor = "transcript-" + re.sub(r"[^a-zA-Z0-9_-]", "-", name)
        if target.is_file():
            transcripts.append((anchor, name, target.read_text(
                encoding="utf-8", errors="replace")))
            return f'href="#{anchor}"'
        return 'href="#missing-local-transcript"'

    page = re.sub(r'href="/logs/([^"]+)"', inline_transcript, page)
    mobile = """
<style>
@media(max-width:640px){
  .wrap{padding:20px 14px 48px}h1{font-size:31px;line-height:1.1}
  table{display:block;overflow-x:auto;font-size:13px}.cards{grid-template-columns:1fr 1fr}
}
details{margin:10px 0;border:1px solid rgba(20,20,19,.14);border-radius:8px;padding:10px}
summary{cursor:pointer;font-family:ui-monospace,SFMono-Regular,Menlo,monospace;font-size:12px}
pre{white-space:pre-wrap;overflow-wrap:anywhere;font-size:11px;line-height:1.45}
</style>"""
    page = page.replace("</head>", mobile + "</head>")
    page = page.replace(
        "auto-refresh 5 min · nothing here is stored twice",
        "portable static snapshot · nothing here is stored twice",
    )
    if transcripts:
        detail = ["<h2>Decision transcripts</h2>",
                  '<p class="sub">Embedded so this file works without the local dashboard.</p>']
        for anchor, name, text in transcripts:
            detail.append(
                f'<details id="{esc(anchor)}"><summary>{esc(name)}</summary>'
                f'<pre>{esc(text)}</pre></details>')
        page = page.replace("<footer", "".join(detail) + "<footer", 1)

    REPORTS.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().astimezone().strftime("%Y%m%d-%H%M%S")
    target = REPORTS / f"neighborhood-watch-{stamp}.html"
    suffix = 1
    while target.exists():
        target = REPORTS / f"neighborhood-watch-{stamp}-{suffix}.html"
        suffix += 1
    target.write_text(page, encoding="utf-8")
    return target


def private_report_addresses():
    """Private addresses that can reach the Mac from the user's phone."""
    candidates = []
    try:
        result = subprocess.run(
            ["tailscale", "ip", "-4"], capture_output=True, text=True, timeout=10)
        if result.returncode == 0:
            candidates.extend(result.stdout.splitlines())
    except Exception:
        pass
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as probe:
            probe.connect(("8.8.8.8", 80))
            candidates.append(probe.getsockname()[0])
    except Exception:
        pass
    addresses = []
    for candidate in candidates:
        try:
            address = ipaddress.ip_address(candidate.strip())
        except ValueError:
            continue
        if address.version == 4 and not address.is_loopback:
            value = str(address)
            if value not in addresses:
                addresses.append(value)
    return addresses


def publish_snapshot(snapshot, port=9797, max_age_hours=48):
    """Publish one tokenized static report on private Mac network addresses."""
    SHARED_REPORTS.mkdir(parents=True, exist_ok=True)
    cutoff = time.time() - max_age_hours * 3600
    for old in SHARED_REPORTS.glob("*.html"):
        try:
            if old.stat().st_mtime < cutoff:
                old.unlink()
        except OSError:
            pass
    token = secrets.token_urlsafe(24)
    target = SHARED_REPORTS / f"{token}.html"
    shutil.copy2(snapshot, target)
    return [
        f"http://{address}:{port}/share/{target.name}"
        for address in private_report_addresses()
    ]


if __name__ == "__main__":
    hours = 14
    for a in sys.argv[1:]:
        if a.startswith("--hours="):
            hours = int(a.split("=")[1])
    s, ok = render(hours)
    print(f"dashboard/index.html — last {s['hours']}h: {s['checks']} checks, "
          f"{s['recovered']} recovered, {s['decisions']} decisions, "
          f"{s['notable_commits']} code changes, chains {'verified' if ok else 'BROKEN'}")
    if "--open" in sys.argv:
        subprocess.run(["open", str(OUT / "index.html")])
