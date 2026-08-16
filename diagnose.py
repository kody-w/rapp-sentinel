#!/usr/bin/env python3
"""diagnose.py — the two-minute diagnostic, made permanent. Read-only.

Issue #4's incident: discussion writes returned FORBIDDEN and the cause was
diagnosed by reasoning about it, three times, wrong every time — ending with
a false alarm texted to the owner asking him to mint a PAT he did not need.
The diagnostic that settled it printed the identity, scope and reachability
of every credential on the call path, and it was shorter than any one of the
wrong fixes.

So that diagnostic is now a command: one row per dependency —
SUBJECT | IDENTITY | SCOPE | REACHABILITY — with the literal word
"unverified" wherever a probe cannot answer, because a gap admitted is worth
more than a guess (the same rule w_openrappter learned in #23).

Secrets are handled as a POSITIVE guarantee, not a habit: every credential
value this module reads is registered in _SECRETS, and the single emit()
printer refuses to print any line containing one. A leak becomes a loud
crash, never a quiet line in a transcript. Values are described only as
prefix-class + length.

No model, no writes, no budget machinery. Run it by hand, or via
`python3 sentinel.py diagnose`, whenever an inference about a credential or
an endpoint is about to be made for the second time.
"""

import json
import os
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

HOME = Path(__file__).resolve().parent

TOKEN_ENVS = ("GITHUB_TOKEN", "GH_TOKEN", "GH_PAT", "COPILOT_GITHUB_TOKEN")

_SECRETS = []


def emit(line):
    """The only printer. Raises on any registered secret substring — the
    redaction is a guarantee that can fail loudly, not a convention."""
    for value in _SECRETS:
        if value and value in line:
            raise RuntimeError(
                "diagnose refused to print a line containing a secret value")
    print(line, flush=True)


def describe_token(value):
    """Shape of a token, never its value: prefix class + length."""
    if not value:
        return "unset"
    for prefix in ("github_pat_", "ghp_", "gho_", "ghu_", "ghs_", "ghr_"):
        if value.startswith(prefix):
            return f"{prefix}* ({len(value)} chars)"
    return f"opaque ({len(value)} chars)"


def _http(url, headers=None, method="GET", data=None, timeout=15):
    """(status, headers, body_bytes, latency_s) or (None, {}, b"", None)."""
    req = urllib.request.Request(url, method=method, data=data,
                                 headers={"User-Agent": "rapp-sentinel-diagnose",
                                          **(headers or {})})
    start = time.time()
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, dict(r.headers), r.read(), time.time() - start
    except urllib.error.HTTPError as e:
        return e.code, dict(e.headers or {}), b"", time.time() - start
    except Exception:
        return None, {}, b"", None


def _run(cmd, timeout=20):
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        return r.returncode, (r.stdout or "") + (r.stderr or "")
    except Exception:
        return None, ""


def row(subject, identity, scope, reach):
    emit(f"  {subject:<24} {identity:<28} {scope:<32} {reach}")


def section(title):
    emit("")
    emit(f"── {title} " + "─" * max(1, 66 - len(title)))


def probe_tokens():
    section("github tokens (env)")
    for env in TOKEN_ENVS:
        value = os.environ.get(env, "")
        if value:
            _SECRETS.append(value)
        if not value:
            row(env, "unset", "-", "-")
            continue
        status, headers, body, lat = _http(
            "https://api.github.com/user",
            headers={"Authorization": "token " + value})
        if status is None:
            row(env, describe_token(value), "unverified", "unverified (no answer)")
            continue
        login = "?"
        try:
            login = json.loads(body.decode()).get("login", "?")
        except Exception:
            pass
        scopes = headers.get("X-OAuth-Scopes") or headers.get("x-oauth-scopes")
        scope_txt = scopes if scopes else "none/fine-grained"
        row(env, f"{describe_token(value)} user={login}",
            f"scopes=[{scope_txt}]", f"http={status} {lat:.1f}s")


def probe_gh_cli():
    section("gh CLI (what the checks actually run as)")
    rc, out = _run(["gh", "api", "/user", "--jq", ".login"])
    if rc is None:
        row("gh api /user", "unverified", "unverified", "gh not runnable")
        return
    login = out.strip() if rc == 0 else "?"
    rc2, out2 = _run(["gh", "auth", "status"])
    scopes = "unverified"
    for line in (out2 or "").splitlines():
        if "Token scopes" in line:
            scopes = line.split(":", 1)[-1].strip()
    row("gh api /user", f"login={login}" if rc == 0 else "FAILED",
        scopes, "ok" if rc == 0 else f"exit {rc}")


def probe_watchers_arm():
    section("watchers")
    import health as H
    r = H._brainstem_answers_turns()
    row("brainstem :7071/chat", "answers turns" if r["ok"] else "not answering",
        "-", r["detail"][:44])
    pid = H._listening_pid(18790)
    if not pid:
        row("openrappter :18790", "nothing LISTENING", "-", "down")
    else:
        owner, verdict = H._launchd_owner(pid)
        row("openrappter :18790", f"pid {pid}", owner or verdict, "serving")


def probe_endpoints():
    section("public surfaces the checks depend on")
    import checks as C
    urls = [
        ("rv site", "https://kody-w.github.io/rappterverse/"),
        ("rb site", "https://kody-w.github.io/rappterbook/"),
        ("rb state", f"https://raw.githubusercontent.com/{C.RB}/main/state/agents.json"),
        ("rv state", f"https://raw.githubusercontent.com/{C.RV}/main/state/actions.json"),
        ("channel", C.CHANNEL + "/rappvision/channel.json"),
        ("github status", "https://www.githubstatus.com/api/v2/components.json"),
    ]
    for name, url in urls:
        status, _, _, lat = _http(url)
        reach = "unverified (no answer)" if status is None else \
            f"http={status} {lat:.1f}s"
        row(name, url[8:46], "-", reach)


def probe_delivery():
    section("delivery + escalation arms")
    try:
        import sentinel
        cfg = sentinel.config()
        handle = cfg.get("notify_handle") or ""
        row("notify_handle", "set" if handle else "unset (notify disabled)",
            "-", f"notify={cfg.get('notify')}")
    except Exception as e:
        row("notify_handle", "unverified", "-", f"config unreadable: {type(e).__name__}")
    chat_db = Path.home() / "Library" / "Messages" / "chat.db"
    try:
        readable = chat_db.exists() and os.access(chat_db, os.R_OK)
        row("chat.db", "readable" if readable else "NOT readable (TCC?)",
            "-", str(chat_db.exists()))
    except Exception:
        row("chat.db", "unverified", "-", "-")
    try:
        import outbox
        st = outbox.status()
        row("outbox", f"{st.get('pending', 0)} pending",
            "-", f"oldest {st.get('oldest_minutes')}m")
    except Exception as e:
        row("outbox", "unverified", "-", f"{type(e).__name__}")
    rc, out = _run(["copilot", "--version"])
    row("copilot CLI", (out.strip().splitlines() or ["?"])[0][:26] if rc == 0
        else "not runnable", "-", "ok" if rc == 0 else "unverified")


def main():
    emit("sentinel diagnose — identity / scope / reachability, values never printed")
    emit("read-only; 'unverified' means the probe could not answer, not that the")
    emit("subject is broken. When two inferences about one cause have failed, this")
    emit("page is the next move — not a third inference (#4).")
    probe_tokens()
    probe_gh_cli()
    probe_watchers_arm()
    probe_endpoints()
    probe_delivery()
    emit("")
    return 0


if __name__ == "__main__":
    sys.exit(main())
