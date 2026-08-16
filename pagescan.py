#!/usr/bin/env python3
"""pagescan.py — what a served page actually asks for, read from its bytes.

Issue #12's finding was invisible to every repo-side check: the HTML asked
for `state/feed/reddit.json` while the file lived at `docs/feed/reddit.json`,
and both halves were individually fine. Seeing that requires the visitor's
vantage: extract the requests the page states it will make, then ask the
served host whether each one comes back.

STATED LIMIT, not hidden: this is REGEX extraction over served bytes. It
sees URL *literals* — fetch("..."), xhr.open("GET","..."), src=/href=
attributes. It cannot see a URL computed at runtime (template literals,
concatenation, variables); those are COUNTED as blind spots, never guessed,
because a scanner that guesses is a scanner you stop believing. A CDP-driven
variant can replace this vantage later by writing state/pagescan.json with
observed_by: "cdp" — the receipt schema is the seam.

Every function that touches the network takes an injected fetch callable
with the signature `fetch(url, method="GET", headers=None) -> (status, text)`
so the prove harness needs no network. `http_fetch` is the real one.
"""

import re
import urllib.error
import urllib.request
from datetime import datetime, timezone
from urllib.parse import urljoin, urlparse

TIMEOUT = 25

# How many first-party <script src> targets scan_page will fetch and scan.
# Bounded on purpose: the real dangling pollers live in .js files, but a page
# with fifty scripts must not turn one check into fifty fetches.
SCRIPT_FETCH_LIMIT = 5

# Schemes that never reach the network: not requests, not blind spots.
_INERT = ("data:", "mailto:", "javascript:", "about:", "blob:", "tel:")

_FETCH_LIT = re.compile(r"""\bfetch\(\s*(['"])(?P<u>[^'"]+?)\1""")
_FETCH_BLIND = re.compile(r"""\bfetch\(\s*(?!['"])""")
_XHR_LIT = re.compile(
    r"""\.open\(\s*(['"])(?:GET|POST|HEAD|PUT|DELETE|PATCH)\1\s*,"""
    r"""\s*(['"])(?P<u>[^'"]+?)\2""", re.I)
_XHR_BLIND = re.compile(
    r"""\.open\(\s*(['"])(?:GET|POST|HEAD|PUT|DELETE|PATCH)\1\s*,\s*(?!['"])""",
    re.I)
_TAG_SRC = re.compile(
    r"""<(?P<tag>script|img|source)\b[^>]*?\ssrc\s*=\s*(['"])(?P<u>[^'"]+?)\2""",
    re.I)
_LINK_HREF = re.compile(
    r"""<link\b[^>]*?\shref\s*=\s*(['"])(?P<u>[^'"]+?)\1""", re.I)
_JSON_HREF = re.compile(
    r"""<a\b[^>]*?\shref\s*=\s*(['"])(?P<u>[^'"]+?\.json(?:\?[^'"]*)?)\1""",
    re.I)


def http_fetch(url, method="GET", headers=None):
    """The one real network read: (status, text). 0 means transport failure.

    Never raises — a scanner mid-page must record "unreachable" and keep
    scanning, not abort the whole vantage on one dropped connection.
    """
    hdrs = {"User-Agent": "rapp-sentinel"}
    if headers:
        hdrs.update(headers)
    req = urllib.request.Request(url, method=method, headers=hdrs)
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT) as r:
            body = b"" if method == "HEAD" else r.read()
            return r.status, body.decode("utf-8", "replace")
    except urllib.error.HTTPError as e:
        return e.code, ""
    except Exception:
        return 0, ""


def first_party(url, page_netloc):
    """The page's own host, plus the raw-content host serving kody-w repos.

    raw.githubusercontent.com is a shared host; only /kody-w/* paths on it
    are ours to assert about. Everything else is third-party: counted,
    never probed, never judged.
    """
    p = urlparse(url)
    if p.netloc and p.netloc == page_netloc:
        return True
    return (p.netloc == "raw.githubusercontent.com"
            and p.path.startswith("/kody-w/"))


def _interval_spans(text):
    """(start, end, interval_s) for each setInterval(...) call's full span.

    The interval is taken only when it is a bare numeric literal in the last
    argument position — anything computed is polling with an unknown period,
    which is stated as such, never guessed.
    """
    spans = []
    for m in re.finditer(r"\bsetInterval\s*\(", text):
        i, depth = m.end(), 1
        while i < len(text) and depth:
            c = text[i]
            if c == "(":
                depth += 1
            elif c == ")":
                depth -= 1
            i += 1
        span_text = text[m.start():i]
        interval = None
        mi = re.search(r",\s*([0-9][0-9_]*)\s*\)\s*$", span_text)
        if mi:
            try:
                interval = int(mi.group(1).replace("_", "")) / 1000.0
            except ValueError:
                interval = None
        spans.append((m.start(), i, interval))
    return spans


def extract(text, base_url):
    """(targets, skipped): every request literal the text states it makes.

    targets: list of {url, kind, polling, interval_s, first_party}, deduped
    on (url, kind) with polling evidence merged. skipped: the count of
    request sites whose URL is COMPUTED (template literals, expressions) —
    the stated blind spot of this vantage.

    Relative URLs resolve against base_url — the DOCUMENT's URL even for
    text that came from an external script, because that is how a browser
    resolves fetch()/XHR/DOM-injected paths.
    """
    spans = _interval_spans(text)
    page_netloc = urlparse(base_url).netloc
    merged = {}
    skipped = 0

    def polling_at(pos):
        for start, end, interval in spans:
            if start <= pos < end:
                return True, interval
        return False, None

    def add(raw, kind, pos, may_poll):
        nonlocal skipped
        raw = (raw or "").strip()
        if not raw or raw.startswith("#"):
            return                      # bare fragment: not a request
        if "${" in raw or "`" in raw or raw.startswith("$"):
            # Computed URL: stated blind spot. The startswith("$") arm is
            # paid for: the first live run probed a literal src="$2" — a
            # regex BACKREFERENCE inside a .replace() template on the
            # rappterbook page — as https://.../$2 and reported a 404 the
            # browser never requests. A $-token is a placeholder, and a
            # placeholder is a computed URL wearing a literal's clothes.
            skipped += 1
            return
        if raw.lower().startswith(_INERT):
            return                      # never reaches the network
        url = urljoin(base_url, raw)
        if not url.lower().startswith(("http://", "https://")):
            return
        polling, interval = polling_at(pos) if may_poll else (False, None)
        key = (url, kind)
        cur = merged.get(key)
        if cur is None:
            merged[key] = {"url": url, "kind": kind, "polling": polling,
                           "interval_s": interval,
                           "first_party": first_party(url, page_netloc)}
        elif polling:
            cur["polling"] = True
            if interval is not None and (cur["interval_s"] is None
                                         or interval < cur["interval_s"]):
                cur["interval_s"] = interval

    for m in _FETCH_LIT.finditer(text):
        add(m.group("u"), "fetch", m.start(), True)
    for m in _XHR_LIT.finditer(text):
        add(m.group("u"), "xhr", m.start(), True)
    for m in _TAG_SRC.finditer(text):
        kind = "script" if m.group("tag").lower() == "script" else "img"
        add(m.group("u"), kind, m.start(), False)
    for m in _LINK_HREF.finditer(text):
        add(m.group("u"), "link", m.start(), False)
    for m in _JSON_HREF.finditer(text):
        add(m.group("u"), "json", m.start(), False)

    # Request sites we can SEE but whose URL we cannot read: count them.
    skipped += sum(1 for _ in _FETCH_BLIND.finditer(text))
    skipped += sum(1 for _ in _XHR_BLIND.finditer(text))

    return list(merged.values()), skipped


def scan_page(url, fetch, script_limit=SCRIPT_FETCH_LIMIT):
    """Fetch one page, extract its stated requests, then scan up to
    `script_limit` of its first-party scripts — bounded, because the real
    dangling pollers live in .js files, not the HTML.
    """
    status, html = fetch(url)
    if status == 0:
        # One dropped TCP connection is not an unscannable page — the same
        # one-retry rule probe() applies (rb_content_moving's discipline).
        import time
        time.sleep(1)
        status, html = fetch(url)
    page = {"url": url, "status": status, "targets": [], "skipped": 0,
            "scripts_scanned": 0}
    if not (200 <= status < 300) or not html:
        return page
    targets, skipped = extract(html, url)
    page["skipped"] = skipped
    scripts = [t for t in targets
               if t["kind"] == "script" and t["first_party"]]
    for s in scripts[:script_limit]:
        s_status, body = fetch(s["url"])
        if not (200 <= s_status < 300) or not body:
            continue                    # its own probe will say so later
        page["scripts_scanned"] += 1
        sub, sub_skipped = extract(body, url)
        page["skipped"] += sub_skipped
        targets.extend(sub)
    # Re-dedupe across HTML + scripts, merging polling evidence.
    merged = {}
    for t in targets:
        key = (t["url"], t["kind"])
        cur = merged.get(key)
        if cur is None:
            merged[key] = t
        elif t["polling"]:
            cur["polling"] = True
            if t["interval_s"] is not None and (
                    cur["interval_s"] is None
                    or t["interval_s"] < cur["interval_s"]):
                cur["interval_s"] = t["interval_s"]
    page["targets"] = list(merged.values())
    return page


def probe(url, fetch):
    """Status of one target: HEAD, falling back to a ranged GET where HEAD
    is refused (405/501) — a probe must never download a 300KB feed to learn
    it exists. A transport failure (status 0) is retried once before it is
    believed: one dropped TCP connection is not a dangling request
    (rb_content_moving's rule)."""
    status, _ = fetch(url, method="HEAD")
    if status in (405, 501):
        status, _ = fetch(url, method="GET", headers={"Range": "bytes=0-0"})
    # Retry transport failures AND 5xx once. A single Pages 503 used to be
    # believed immediately, while every neighbouring check (outsider_can_join,
    # rb_content_moving) already refuses to judge from one dropped sample —
    # critical=False bounded the blast radius, but a spurious warn still
    # flips the verdict and trains readers to scroll (2026-08-16 review sweep).
    if status == 0 or status >= 500:
        import time
        time.sleep(1)
        status, _ = fetch(url, method="HEAD")
        if status in (405, 501):
            status, _ = fetch(url, method="GET",
                              headers={"Range": "bytes=0-0"})
    return status


# ── declared cadence vs. actual stamp — the grammar, and only the grammar ──

_CADENCE_KEY = re.compile(r"refresh|cadence|interval|schedule|frequency|cron",
                          re.I)
_PROSE = re.compile(
    r"every\s+(\d+(?:\.\d+)?)\s*(sec(?:ond)?|min(?:ute)?|h(?:ou)?r)s?\b",
    re.I)
_CRON = re.compile(r"^\s*\*/(\d+)\s+\*\s+\*\s+\*\s+\*\s*$")

STAMP_KEYS = ("last_updated", "lastUpdate", "materialized_at", "_generated",
              "generated", "updated_at")


def _shallow_items(doc):
    """Top level + _meta. Shallow on purpose: a cadence claim buried three
    levels deep is not a claim the document is making about itself."""
    if not isinstance(doc, dict):
        return []
    items = list(doc.items())
    meta = doc.get("_meta")
    if isinstance(meta, dict):
        items.extend(meta.items())
    return items


def _cadence_from_value(key, val):
    lk = key.lower()
    if isinstance(val, bool):
        return None
    if isinstance(val, (int, float)):
        # Only unit-suffixed numeric keys are a claim. A bare "interval": 30
        # could be seconds, minutes, or a count — never guess.
        if lk.endswith(("_ms", "_millis", "_milliseconds")):
            return val / 1000.0
        if lk.endswith(("_seconds", "_secs", "_sec", "_s")):
            return float(val)
        if lk.endswith(("_minutes", "_mins", "_min", "_m")):
            return val * 60.0
        if lk.endswith(("_hours", "_hrs", "_hr", "_h")):
            return val * 3600.0
        return None
    if isinstance(val, str):
        m = _PROSE.search(val)
        if m:
            n = float(m.group(1))
            unit = m.group(2).lower()
            if unit.startswith("sec"):
                return n
            if unit.startswith("min"):
                return n * 60.0
            return n * 3600.0
        m = _CRON.match(val)
        if m:
            return int(m.group(1)) * 60.0
    return None


def declared_cadence(doc):
    """Seconds the document CLAIMS to refresh at, or None.

    The grammar is deliberately closed: "every N min/minute(s)/hour(s)/sec"
    prose, *_seconds/*_minutes/*_ms numeric keys, and trivial "*/N * * * *"
    cron — on refresh/cadence/interval/schedule/frequency/cron-named keys at
    the top level and in _meta. Anything else returns None: an unparsed
    claim is a claim we do not judge, never one we guess at.

    When several claims coexist, the strictest (smallest) one is the claim,
    because that is the promise a reader would hold the document to.
    """
    found = []
    for key, val in _shallow_items(doc):
        if not isinstance(key, str) or not _CADENCE_KEY.search(key):
            continue
        secs = _cadence_from_value(key, val)
        if secs is not None and secs > 0:
            found.append(secs)
    return min(found) if found else None


def parse_iso(stamp):
    """ISO string -> aware datetime, or None. Never raises."""
    if not isinstance(stamp, str) or not stamp:
        return None
    try:
        dt = datetime.fromisoformat(stamp.replace("Z", "+00:00"))
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def newest_stamp(doc):
    """The newest parseable ISO stamp among the known stamp keys, searched
    shallowly (top level + _meta). Returns the original string, or None —
    a document with no readable stamp cannot testify to its own freshness,
    and that absence is the caller's finding, not this function's guess."""
    best, best_dt = None, None
    for key, val in _shallow_items(doc):
        if key not in STAMP_KEYS:
            continue
        dt = parse_iso(val)
        if dt is None:
            continue
        if best_dt is None or dt > best_dt:
            best, best_dt = val, dt
    return best
