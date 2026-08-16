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
from pathlib import Path

CODE = Path(__file__).resolve().parent


def _home():
    raw = os.environ.get("SENTINEL_HOME", "").strip()
    if not raw:
        return CODE
    home = Path(raw).expanduser().resolve()
    home.mkdir(parents=True, exist_ok=True)
    return home


HOME = _home()
