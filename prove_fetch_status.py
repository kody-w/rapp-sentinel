"""Reproduction for #1 (ask 5) — a fetch failure must carry its HTTP status.

Every fetch_peer failure used to arrive as the bare word "HTTPError". But 404
(misconfigured — fix your URL) and 503 (transient — wait) demand opposite
responses, and the detail string is the entire diagnosis anyone gets. So the
statuses are served by a REAL stdlib HTTP server and read through the REAL
fetch_peer — a stub that raises HTTPError by hand cannot prove urlopen's
error path is the one being caught.

Asserted: the detail names the status, the additive `http_status` field
carries it as an int, and `reachable` stays False — an HTTP error is still a
head that could not be read, so the consumer's `gone` classification keeps
its meaning. The control leg proves a valid rapp-sentinel-head/1.0 doc (one
that genuinely passes head_violations) still parses reachable+valid.

Then the same courtesy for gh_status: its except carried only the type name,
so a 503 from githubstatus.com read identically to a DNS failure.
"""
import http.server
import json
import sys
import threading
import urllib.error
import urllib.request

import checks
import neighborhood as NB
import rapp

# ── a head doc the real validator accepts (pattern: prove_first_sight) ──────
SLUGS = ("openrappter", "brainstem", "copilot")
RIDS = {s: rapp.mint_rappid("prover", f"watcher-{s}") for s in SLUGS}


def head_doc():
    utc = NB.utc_now()
    return {
        "schema": "rapp-sentinel-head/1.0",
        "neighborhood": "peer-under-test",
        "name": "Peer Under Test",
        "utc": utc,
        "heads": {s: {"rappid": RIDS[s], "seq": 7,
                      "frame_hash": rapp.H("rapp/1:particle",
                                           {"prove": "fetch-status", "slug": s}),
                      "utc": utc}
                  for s in SLUGS},
    }


VALID_DOC = head_doc()


class Scripted(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path == "/head.json":
            body = json.dumps(VALID_DOC).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        elif self.path == "/gone":
            self.send_error(404, "Not Found")
        elif self.path == "/busy":
            self.send_error(503, "Service Unavailable")
        else:
            self.send_error(500)

    def log_message(self, *a):
        pass


server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Scripted)
port = server.server_address[1]
threading.Thread(target=server.serve_forever, daemon=True).start()


def url(path):
    return f"http://127.0.0.1:{port}{path}"


CASES = []


def case(name, got, want):
    CASES.append((got == want, name, want, got))


try:
    # the fixture must be a live instrument before anything is measured
    case("fixture: head doc passes the real head_violations()",
         NB.head_violations(VALID_DOC), [])

    gone = NB.fetch_peer("peer-a", url("/gone"), timeout=10)
    case("404: detail names the status", "HTTP 404" in gone["detail"], True)
    case("404: additive http_status field is the int",
         gone.get("http_status"), 404)
    case("404: reachable stays False — gone semantics preserved",
         gone["reachable"], False)

    busy = NB.fetch_peer("peer-a", url("/busy"), timeout=10)
    case("503: detail names the status", "HTTP 503" in busy["detail"], True)
    case("503: additive http_status field is the int",
         busy.get("http_status"), 503)
    case("503: reachable stays False", busy["reachable"], False)

    good = NB.fetch_peer("peer-a", url("/head.json"), timeout=10)
    case("CONTROL valid doc: reachable", good.get("reachable"), True)
    case("CONTROL valid doc: valid", good.get("valid"), True)
    case("CONTROL valid doc: no http_status on success",
         "http_status" in good, False)
finally:
    server.shutdown()
    server.server_close()

# ── gh_status carries the reason, not just the type name ────────────────────
REAL = urllib.request.urlopen


def raise_503(req, timeout=None):
    raise urllib.error.HTTPError(
        "https://www.githubstatus.com/api/v2/components.json",
        503, "Service Unavailable", None, None)


urllib.request.urlopen = raise_503
try:
    got = checks.github_status_operational()
finally:
    urllib.request.urlopen = REAL

case("gh_status on 503: still fails closed", got["ok"], False)
case("gh_status on 503: detail carries more than the bare type name",
     "503" in got["detail"], True)

bad = 0
for good_, name, want, got_ in CASES:
    bad += not good_
    print(f"  [{'ok' if good_ else 'XX'}] {name}\n"
          f"        expected {want!r}  got {got_!r}")
print(f"\n{len(CASES)-bad}/{len(CASES)} scenarios behaved as specified")
sys.exit(1 if bad else 0)
