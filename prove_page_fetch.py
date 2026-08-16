#!/usr/bin/env python3
"""prove_page_fetch.py — the visitor's vantage extracts what a page states
it will request, counts what it cannot see, and fires on requests that do
not come back — pollers loudest.

The fired leg reproduces #12's exact live drift: a page requesting
state/feed/reddit.json (404) while docs/feed/reddit.json serves 200 — two
halves individually fine, broken only from the visitor's chair. A control
proves all-2xx passes with positive counts, and a page with no extractable
requests reads as blind, never clean.

Run: python3 prove_page_fetch.py   (exit 0 only on all-behaved)
"""

import json
import sys
import tempfile
from pathlib import Path

import checks as C
import pagescan as P

FAILURES = []


def scenario(name, cond, observed):
    status = "PASS" if cond else "FAIL"
    print(f"  [{status}] {name}\n        {observed}")
    if not cond:
        FAILURES.append(name)


# ── pure leg: extract() over synthetic HTML ─────────────────────────────────

BASE = "https://kody-w.github.io/rappterbook/reddit.html"
HTML = """<!doctype html>
<script src="assets/app.js"></script>
<script src="https://cdn.example.com/lib.js"></script>
<link rel="preload" href="state/agents.json">
<img src="img/logo.png">
<a href="#top">top</a>
<script>
fetch('state/feed/reddit.json').then(r => r.json());
setInterval(() => { fetch('state/steward.json'); }, 30000);
const xhr = new XMLHttpRequest();
xhr.open('GET', '../shared/data.json');
fetch(`state/${name}.json`);
fetch('data:text/plain,hi');
document.body.innerHTML = html.replace(/x/, '<img src="$2">');
</script>
"""

targets, skipped = P.extract(HTML, BASE)
by_url = {t["url"]: t for t in targets}

steward = by_url.get("https://kody-w.github.io/rappterbook/state/steward.json")
scenario("a fetch literal inside setInterval(...,30000) is a poller at 30s",
         steward is not None and steward["polling"]
         and steward["interval_s"] == 30.0,
         f"target={steward}")

scenario("the template-literal fetch is a counted blind spot, not a guess",
         skipped >= 1 and not any("${" in u for u in by_url),
         f"skipped={skipped}, urls={sorted(by_url)}")

scenario("a $-token placeholder (src=\"$2\" in a .replace template) is a "
         "blind spot, never probed — the first live run paid for this",
         not any(u.endswith("/$2") for u in by_url), f"urls={sorted(by_url)}")

cdn = by_url.get("https://cdn.example.com/lib.js")
scenario("a third-party CDN script is extracted with first_party=False",
         cdn is not None and cdn["first_party"] is False, f"target={cdn}")

resolved = by_url.get("https://kody-w.github.io/shared/data.json")
scenario("an XHR relative URL resolves against the page URL",
         resolved is not None and resolved["kind"] == "xhr"
         and resolved["first_party"], f"target={resolved}")

link = by_url.get("https://kody-w.github.io/rappterbook/state/agents.json")
scenario("<link href> is extracted; data:/fragment URLs are ignored entirely",
         link is not None and link["kind"] == "link"
         and not any(u.startswith("data:") for u in by_url),
         f"link={link}")

plain = by_url.get(
    "https://kody-w.github.io/rappterbook/state/feed/reddit.json")
scenario("a plain fetch literal outside setInterval is NOT a poller",
         plain is not None and plain["polling"] is False
         and plain["interval_s"] is None, f"target={plain}")

# ── fired legs: the check with injected fetch ───────────────────────────────

SITE = "https://kody-w.github.io/rappterbook"
PAGE = SITE + "/reddit.html"

DRIFT_HTML = """<script>
setInterval(function() { fetch('state/steward.json'); }, 30000);
fetch('state/feed/reddit.json');
</script>
<img src="img/logo.png">
"""

ALL_GREEN_HTML = """<script>
fetch('state/feed/reddit.json');
setInterval(() => { fetch('state/steward.json'); }, 60000);
</script>
<script src="https://cdn.example.com/lib.js"></script>
<img src="img/logo.png">
"""

NO_URLS_HTML = "<p>a page that states no requests at all</p>"


def serving(routes, page_html):
    """fetch fake: page URL -> HTML; targets by route table; else 404.
    #12's exact shape: state/feed/reddit.json 404 while docs/feed 200."""
    def fetch(url, method="GET", headers=None):
        if url == PAGE:
            return 200, page_html
        if url in routes:
            return routes[url], ""
        return 404, ""
    return fetch


REAL_HOME, REAL_PAGES, REAL_FETCH = C.HOME, C.PAGE_FETCH_PAGES, P.http_fetch
try:
    C.HOME = Path(tempfile.mkdtemp(prefix="prove-pagefetch-"))
    C.PAGE_FETCH_PAGES = (PAGE,)

    # (1) The #12 drift: state/feed 404s while docs/feed serves 200.
    P.http_fetch = serving({
        SITE + "/docs/feed/reddit.json": 200,      # the file exists — HERE
        SITE + "/img/logo.png": 200,
    }, DRIFT_HTML)
    r = C.served_pages_fetch_what_they_ask_for()
    d = r["detail"]
    scenario("(1) the #12 drift fires at WARN — page and file individually fine",
             (not r["ok"]) and r["severity"] == C.WARN,
             f"severity={r['severity']} {d}")
    scenario("(1b) the POLLING failure is named FIRST, with its interval",
             "steward.json" in d and "reddit.json" in d
             and d.index("steward.json") < d.index("reddit.json")
             and "every 30s" in d, d)

    receipt = json.loads(
        (C.HOME / "state" / "pagescan.json").read_text(encoding="utf-8"))
    scenario("(1c) the receipt states its vantage and its failures",
             receipt["observed_by"] == "regex"
             and SITE + "/state/steward.json" in receipt["failures"],
             f"observed_by={receipt['observed_by']} "
             f"failures={receipt['failures']}")

    # (2) All-2xx control passes with positive counts, third-party skipped.
    P.http_fetch = serving({
        SITE + "/state/feed/reddit.json": 200,
        SITE + "/state/steward.json": 200,
        SITE + "/img/logo.png": 200,
    }, ALL_GREEN_HTML)
    r = C.served_pages_fetch_what_they_ask_for()
    scenario("(2) all-2xx -> ok with positive counts, CDN counted not probed",
             r["ok"] and "3 first-party targets all 2xx" in r["detail"]
             and "1 pollers" in r["detail"]
             and "1 third-party skipped" in r["detail"], r["detail"])

    # (3) Zero extraction is blindness, never cleanliness (R3/#45).
    P.http_fetch = serving({}, NO_URLS_HTML)
    r = C.served_pages_fetch_what_they_ask_for()
    scenario("(3) zero extracted targets -> WARN 'blind, not clean'",
             (not r["ok"]) and r["severity"] == C.WARN
             and "blind, not clean" in r["detail"],
             f"severity={r['severity']} {r['detail']}")

    # (4) probe() falls back to a ranged GET when HEAD is refused.
    calls = []

    def head_hater(url, method="GET", headers=None):
        calls.append((method, headers))
        if method == "HEAD":
            return 405, ""
        return 206, ""
    status = P.probe("https://kody-w.github.io/x.json", head_hater)
    scenario("(4) HEAD 405 -> ranged GET, and the 206 is believed",
             status == 206 and calls[0][0] == "HEAD"
             and calls[1][0] == "GET"
             and (calls[1][1] or {}).get("Range") == "bytes=0-0",
             f"status={status} calls={calls}")
finally:
    C.HOME, C.PAGE_FETCH_PAGES, P.http_fetch = REAL_HOME, REAL_PAGES, REAL_FETCH

print(f"\n{len(FAILURES)} failing scenario(s)" if FAILURES
      else "\nall scenarios behaved as specified")
sys.exit(1 if FAILURES else 0)
