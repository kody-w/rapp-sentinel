#!/usr/bin/env python3
"""paths.py — the one place HOME and CODE are derived (#1, ask 1).

Every runtime module used to compute `HOME = Path(__file__).resolve().parent`
independently, which welded "where the code lives" to "where this instance's
state lives". An instance was therefore necessarily a COPY of the code: two
neighborhoods on one machine meant every file vendored twice and every
upstream fix applied twice by hand.

The two ideas are now two names:

  CODE  where the code lives. Always this directory. Subprocess targets
        (health.py, standup.py) and code-owned manifests
        (required_checks.json) key off it — the required-check set is a
        property of checks.py, not of an instance.

  HOME  where THIS instance's state lives: config.json, direction.json,
        state/, logs/, neighborhood/, public/, STOP, dashboard/. Set
        SENTINEL_HOME to run a second instance off the same checkout.
        Unset — the live install's situation — HOME is CODE and every path
        is byte-identical to what the previous code computed, which is the
        molt constraint: the running organism's ledger key, chains, and
        heartbeat must not move when it pulls this change.

One derivation, imported everywhere, because ten independent derivations
honoring an env var independently is ten chances for one of them to be
missed — and a module that misses it writes a second, silent instance into
the code tree.
"""

import os
import sys
from pathlib import Path

CODE = Path(__file__).resolve().parent


def app_support(*parts):
    """The per-user directory for state that must OUTLIVE any one checkout.

    Two things live here: the anchor ledger (an instance's chain anchors, which must survive the
    code being re-cloned) and the baseline clones. The location was hardcoded to macOS's
    ~/Library/Application Support, so a Windows sentinel built a nonexistent macOS-shaped path
    inside its profile and then measured against it.

    macOS keeps EXACTLY the path it always had — a running organism's ledger key must not move
    when it pulls this change; a moved ledger reads as 'this instance's streams vanished'."""
    if sys.platform == "darwin":
        base = Path.home() / "Library" / "Application Support" / "rapp-sentinel"
    elif os.name == "nt":
        base = Path(os.environ.get("LOCALAPPDATA") or (Path.home() / "AppData" / "Local")) / "rapp-sentinel"
    else:
        base = Path(os.environ.get("XDG_DATA_HOME") or (Path.home() / ".local" / "share")) / "rapp-sentinel"
    return base.joinpath(*parts) if parts else base


def _home():
    raw = os.environ.get("SENTINEL_HOME", "").strip()
    if not raw:
        return CODE
    home = Path(raw).expanduser().resolve()
    home.mkdir(parents=True, exist_ok=True)
    return home


HOME = _home()
