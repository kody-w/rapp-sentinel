#!/usr/bin/env python3
"""hub.py — grow the organism with sentinels from the RAPP Sentinel Hub.

WHY
checks.py is the checks Kody wrote for two GitHub-native platforms. Anyone
else's checks used to mean forking the file. The hub
(github.com/kody-w/rapp-sentinel-hub) is where single-file sentinels are
posted the way RAR posts agent.pys; this module is the socket they plug
into: drop `<slug>_sentinel.py` into HOME/hub/ (sentinel_sdk.py install does
exactly that) and the next tick runs it - no restart, no edit, no fork.

WHAT A HUB SENTINEL GETS
  * a ctx of the SAME helpers checks.py uses (ok, fail, moving,
    require_success, hours_since, gh) plus http_get and a state_read/write
    scoped to HOME/state/hub/ - so a hub sentinel inherits R1/R2/R3 the same
    way a native check does, and gh() still counts _GH_CALLS so an outsider
    claim is enforced, not asserted (issue #5)
  * its manifest defaults merged with config.json -> hub.config.<slug>
  * its declared check ids ADDED to the required set for this tick, and its
    kinds map added to the R2 pairing map: a hub sentinel that stops
    emitting an id fails w_checks_complete exactly like a native check whose
    @check line went missing (#15). Installing a sentinel is a promise the
    verdict enforces.

WHAT IT DOES NOT GET
  Trust by default. A stranger's file can say "critical" and wake the repair
  arm, which spends money and pushes commits. So a hub result's severity is
  capped at warn unless its slug is listed in config.json -> hub.critical_allowed
  (the dial, not the switch - same shape as `level`). The demotion is written
  into the detail so it is a declaration, never a silent downgrade.

GROWTH PATH
  No HOME/hub/ directory, or no `hub` block in config.json -> this module
  contributes nothing and every existing verdict is byte-identical. A file in
  hub/ that fails to load, has a bad manifest, or raises is ONE warn result
  under `hub_<slug>_load` (blind is not broken; the tick continues). Files
  ending in .removed / starting with _ are ignored (sentinel_sdk uninstall
  keeps the bytes).
"""

import ast
import hashlib
import importlib.util
import json
import re
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

import checks as C
from paths import HOME

SCHEMA = "rapp-sentinel/1.0"
HUB_DIR = HOME / "hub"
STATE_DIR = HOME / "state" / "hub"
NAME_RE = re.compile(r"^@([A-Za-z0-9][A-Za-z0-9-]{0,38})/([a-z][a-z0-9_]{1,60}_sentinel)$")
LEDGER = HOME / "state" / "hub-integrity.json"   # what this organism has agreed to execute
ID_RE = re.compile(r"^[a-z][a-z0-9_]{2,40}$")
KINDS = ("reachability", "output-freshness", "run-status", "consistency", "watcher")


def _config():
    """config.json -> `hub` block, tolerant (missing/unparseable/misshaped = {})."""
    try:
        doc = json.loads((HOME / "config.json").read_text(encoding="utf-8"))
        hub = doc.get("hub")
        return hub if isinstance(hub, dict) else {}
    except Exception:
        return {}


def _http_get(url, timeout=C.TIMEOUT):
    req = urllib.request.Request(url, headers={"User-Agent": "rapp-sentinel"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, r.read()
    except urllib.error.HTTPError as e:
        return e.code, b""


def _state_read(name):
    try:
        return json.loads((STATE_DIR / Path(name).name).read_text(encoding="utf-8"))
    except Exception:
        return None


def _state_write(name, doc):
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    p = STATE_DIR / Path(name).name
    tmp = p.with_suffix(p.suffix + ".tmp")
    tmp.write_text(json.dumps(doc, indent=2, sort_keys=True), encoding="utf-8")
    tmp.replace(p)


def host_ctx():
    """The helpers a hub sentinel runs with. Same functions the native checks use."""
    return {
        "ok": C.ok, "fail": C.fail, "moving": C.moving,
        "require_success": C.require_success, "hours_since": C.hours_since,
        "gh": lambda args, timeout=None: C.gh(list(args)),      # counts _GH_CALLS
        "http_get": _http_get,
        "now_iso": lambda: datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z"),
        "state_read": _state_read, "state_write": _state_write,
    }


def _read_manifest(path):
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    for node in tree.body:
        if isinstance(node, ast.Assign) and any(
                isinstance(t, ast.Name) and t.id == "__manifest__" for t in node.targets):
            return ast.literal_eval(node.value)
    raise ValueError("no top-level __manifest__ literal")


def _manifest_errors(m):
    if not isinstance(m, dict) or m.get("schema") != SCHEMA:
        return f"schema must be {SCHEMA}"
    if not NAME_RE.match(str(m.get("name", ""))):
        return "name must be @publisher/slug_sentinel"
    checks = m.get("checks")
    if not isinstance(checks, dict) or not checks:
        return "checks must be a non-empty dict"
    for cid, meta in checks.items():
        if not ID_RE.match(str(cid)):
            return f"bad check id {cid!r}"
        if not isinstance(meta, dict) or not meta.get("domain") or meta.get("kind") not in KINDS:
            return f"check {cid!r} needs domain and a known kind"
    return None


def installed(hub_dir=None):
    """[(path, manifest|None, error|None)] for every candidate file in HOME/hub/."""
    hub_dir = Path(hub_dir) if hub_dir else HUB_DIR
    out = []
    if not hub_dir.is_dir():
        return out
    for p in sorted(hub_dir.glob("*.py")):
        if p.name.startswith("_"):
            continue
        try:
            m = _read_manifest(p)
            err = _manifest_errors(m)
            out.append((p, None if err else m, err))
        except Exception as e:
            out.append((p, None, f"{type(e).__name__}: {str(e)[:80]}"))
    return out


def declared_ids(hub_dir=None, cfg=None):
    """Ids the installed, well-formed, enabled hub sentinels owe this tick."""
    cfg = _config() if cfg is None else cfg
    disabled = set(cfg.get("disabled") or [])
    ids = set()
    for p, m, err in installed(hub_dir):
        if m and p.stem not in disabled:
            ids |= set(m["checks"])
    return ids


def declared_kinds(hub_dir=None, cfg=None):
    cfg = _config() if cfg is None else cfg
    disabled = set(cfg.get("disabled") or [])
    kinds = {}
    for p, m, err in installed(hub_dir):
        if m and p.stem not in disabled:
            for cid, meta in m["checks"].items():
                kinds[cid] = {"domain": meta["domain"], "kind": meta["kind"]}
    return kinds


def _load(path):
    spec = importlib.util.spec_from_file_location("hub_" + path.stem, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def run_all(hub_dir=None, cfg=None, ctx=None):
    """Run every installed hub sentinel; return a flat list of results.

    Never raises. Every result is tagged produced_by="hub:<name>" so the
    duplicate-id guard in check_completeness has a real producer to compare.
    """
    cfg = _config() if cfg is None else cfg
    ctx = host_ctx() if ctx is None else ctx
    disabled = set(cfg.get("disabled") or [])
    overrides = cfg.get("config") if isinstance(cfg.get("config"), dict) else {}
    critical_allowed = set(cfg.get("critical_allowed") or [])
    results = []
    for p, m, err in installed(hub_dir):
        slug = p.stem
        if slug in disabled:
            continue
        load_id = f"hub_{slug}_load"[:60]
        if err:
            r = C.fail(load_id, f"hub sentinel {p.name} unloadable: {err}", critical=False)
            r["produced_by"] = f"hub:{p.name}"
            results.append(r)
            continue
        name = m["name"]
        declared = set(m["checks"])
        try:
            mod = _load(p)
            if not callable(getattr(mod, "run", None)):
                raise TypeError("no run() callable")
            user_cfg = overrides.get(slug) if isinstance(overrides.get(slug), dict) else {}
            merged = dict(m.get("config") or {}, **user_cfg)
            out = mod.run(merged, ctx)
            if not isinstance(out, list):
                raise TypeError("run() did not return a list")
        except Exception as e:
            r = C.fail(load_id, f"hub sentinel {name} raised {type(e).__name__}: {str(e)[:80]}",
                       critical=False)
            r["produced_by"] = f"hub:{name}"
            results.append(r)
            # the ids it owed still owe a line: name them as blind, not absent
            for cid in sorted(declared):
                rr = C.fail(cid, f"{name} did not report (see {load_id})", critical=False)
                rr["produced_by"] = f"hub:{name}"
                results.append(rr)
            continue
        emitted = set()
        for r in out:
            if not isinstance(r, dict) or r.get("id") not in declared:
                continue                      # undeclared/malformed lines are dropped, never trusted
            r = dict(r)
            r.setdefault("ok", False)
            r.setdefault("severity", C.WARN)
            r.setdefault("detail", "")
            if (not r["ok"] and r["severity"] == C.CRITICAL and slug not in critical_allowed):
                r["severity"] = C.WARN
                r["detail"] = (str(r["detail"]) + " [hub: critical demoted to warn - add "
                               f"'{slug}' to config.json hub.critical_allowed to honour it]")
            r["produced_by"] = f"hub:{name}"
            r["hub_version"] = m.get("version")
            results.append(r)
            emitted.add(r["id"])
        for cid in sorted(declared - emitted):
            rr = C.fail(cid, f"{name} did not emit {cid} this tick", critical=False)
            rr["produced_by"] = f"hub:{name}"
            results.append(rr)
    return results


# ── integrity: the code this organism executes is the code it agreed to execute ──
#
# run_all() imports every file in HOME/hub/ and executes it, every tick, forever. Until now the
# only gate was a well-formed __manifest__ literal — a shape check, not an identity check. Nothing
# recorded WHICH BYTES were accepted, so a hub sentinel could be edited in place (by an update, a
# stray script, or anyone who can write that directory) and the next tick would execute the new
# code silently, with the same name and the same manifest. A watchdog that cannot say what it is
# running cannot be a witness to anything else.
#
# So: an explicit, human-scale acceptance ledger. `hub.py accept <slug>` records the sha256 of the
# bytes on disk; the w_hub_integrity check compares every installed file against that record and
# says, out loud, when they differ. Acceptance is never automatic — an organism that accepts
# whatever it finds has recorded a habit, not a decision.


def file_sha256(path):
    """The digest of the bytes as they are, with no normalization — this is an identity, not a diff."""
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for block in iter(lambda: fh.read(65536), b""):
            h.update(block)
    return h.hexdigest()


def read_ledger():
    try:
        doc = json.loads(LEDGER.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {"schema": "rapp-sentinel-hub-integrity/1.0", "accepted": {}}
    if not isinstance(doc, dict) or not isinstance(doc.get("accepted"), dict):
        return {"schema": "rapp-sentinel-hub-integrity/1.0", "accepted": {}}
    return doc


def _write_ledger(doc):
    LEDGER.parent.mkdir(parents=True, exist_ok=True)
    tmp = LEDGER.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(doc, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    tmp.replace(LEDGER)


def accept(slug, hub_dir=None):
    """Record the bytes currently on disk for `slug` as accepted. Returns (ok, message)."""
    hub_dir = Path(hub_dir) if hub_dir else HUB_DIR
    path = hub_dir / (slug if slug.endswith(".py") else slug + ".py")
    if not path.is_file():
        return False, "no such hub sentinel: %s" % path
    try:
        m = _read_manifest(path)
        err = _manifest_errors(m)
    except Exception as e:
        m, err = None, "%s: %s" % (type(e).__name__, str(e)[:80])
    if err:
        return False, "refusing to accept a malformed sentinel (%s)" % err
    digest = file_sha256(path)
    doc = read_ledger()
    prev = doc["accepted"].get(path.stem)
    doc["accepted"][path.stem] = {
        "name": m["name"], "sha256": digest,
        "accepted_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "previous_sha256": (prev or {}).get("sha256"),
        "checks": sorted(m["checks"]),
    }
    _write_ledger(doc)
    return True, "accepted %s (%s) sha256 %s" % (path.stem, m["name"], digest[:12])


def forget(slug):
    """Drop an acceptance — for a sentinel that has been uninstalled."""
    doc = read_ledger()
    if doc["accepted"].pop(slug, None) is None:
        return False, "%s was not in the ledger" % slug
    _write_ledger(doc)
    return True, "forgot %s" % slug


def integrity(hub_dir=None, cfg=None):
    """Compare what is installed against what was accepted. Pure: reads, never writes.

    Returns {"unaccepted": [...], "changed": [...], "missing": [...], "matched": [...], "malformed": [...]}
    - unaccepted: installed and EXECUTING, never accepted by anyone
    - changed:    accepted once, but the bytes on disk are different now
    - missing:    accepted, no longer installed (informational — an uninstall leaves a record)
    - malformed:  present, never accepted, and will not load

    The digest is taken BEFORE the manifest is parsed, and that order is the point. An earlier
    version asked "is this a valid sentinel?" first, so a file that was rewritten into something
    unparseable fell into `malformed` and the fact that ACCEPTED BYTES HAD MOVED was lost — the
    loudest possible signal, silently downgraded to a load warning. Identity does not depend on
    the file still being valid: a changed file is changed whether or not it still parses.
    """
    doc = read_ledger()
    accepted = doc["accepted"]
    # A disabled sentinel is NOT executing (run_all skips it), so it must never be described as if it
    # were. Its bytes changing is still worth saying — just not with the word "executing".
    disabled = set((_config() if cfg is None else cfg).get("disabled") or [])
    out = {"unaccepted": [], "changed": [], "missing": [], "matched": [], "malformed": [], "disabled": []}
    seen = set()
    for path, m, err in installed(hub_dir):
        seen.add(path.stem)
        try:
            digest = file_sha256(path)
        except OSError as e:
            out["malformed"].append({"slug": path.stem, "error": "unreadable: %s" % e})
            continue
        rec = accepted.get(path.stem)
        name = (m or {}).get("name") or (rec or {}).get("name") or path.stem
        if path.stem in disabled:
            row = {"slug": path.stem, "name": name}
            if rec and rec.get("sha256") != digest:
                row["changed"] = True
            out["disabled"].append(row)
            continue
        if rec:                                     # identity first — validity is a separate question
            if rec.get("sha256") != digest:
                row = {"slug": path.stem, "name": name,
                       "accepted": rec.get("sha256"), "found": digest}
                if err or not m:
                    row["also"] = "and no longer loads (%s)" % err
                out["changed"].append(row)
            else:
                out["matched"].append({"slug": path.stem, "name": name})
            continue
        if err or not m:
            out["malformed"].append({"slug": path.stem, "error": err})
        else:
            out["unaccepted"].append({"slug": path.stem, "name": name, "sha256": digest})
    for slug in sorted(set(accepted) - seen):
        out["missing"].append({"slug": slug, "name": (accepted[slug] or {}).get("name")})
    return out


if __name__ == "__main__":
    import sys
    a = sys.argv[1:]
    if a and a[0] == "accept":
        if len(a) < 2:
            print("usage: hub.py accept <slug>|--all"); sys.exit(2)
        targets = [p.stem for p, m, e in installed() if m] if a[1] == "--all" else a[1:]
        bad = 0
        for t in targets:
            good, msg = accept(t)
            print(("  " if good else "  REFUSED: ") + msg)
            bad += 0 if good else 1
        sys.exit(1 if bad else 0)
    elif a and a[0] == "forget" and len(a) > 1:
        good, msg = forget(a[1]); print(msg); sys.exit(0 if good else 1)
    elif a and a[0] == "integrity":
        print(json.dumps(integrity(), indent=2)); sys.exit(0)
    else:
        print(__doc__)
        print("commands: accept <slug>|--all | forget <slug> | integrity")
