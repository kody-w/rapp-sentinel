#!/usr/bin/env python3
"""identity.py — a sentinel instance knows WHO it is and WHAT code it runs.

The lesson that produced this file (measured 2026-08-25, from a real inventory of the
estate): six sentinel installs existed across four devices at five different code
versions, and nothing could tell them apart. The alerts carried a human-typed display
name ("Storykeeper One") that lived only in a config file — so when three days of noise
arrived, nobody could answer which instance sent it, what code that instance was running,
or whether the fix they just wrote had ever reached it. Two of the noisiest were running
code that was six days and *twenty days* stale, and that stayed invisible because a
display name is not an identity.

An identity has to be minted once and carried forever; a version has to travel with every
claim the instance makes. That is exactly what rapp/1 gives us, so the sentinel adopts it:

  rappid.json   the instance's permanent address, minted ONCE per spec §6.2
                (uuid-entropy tail, never a hash of the name — two instances called
                "Storykeeper One" on different machines must not collide)
  stamp()       identity + running code version + host, attached to every alert and
                every ledger frame, so a message can always be traced back to the exact
                instance and commit that produced it

Why compliance rather than a homegrown id: every other chain in this estate already
verifies under one envelope, so a sentinel frame can be pooled, traded, and gate-checked
alongside world data, brains, and films with no special case. An identity that only the
sentinel understands would need its own tooling forever; a rapp/1 identity is understood
by everything already.
"""
from __future__ import annotations

import datetime
import json
import platform
import subprocess
from pathlib import Path

import rapp as R
from paths import CODE, HOME

RAPPID = HOME / "rappid.json"


def _utc() -> str:
    return datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def code_version() -> str:
    """The commit this instance is actually running — not what a README claims."""
    try:
        r = subprocess.run(["git", "-C", str(CODE), "log", "-1", "--format=%h"],
                           capture_output=True, text=True, timeout=10)
        return r.stdout.strip() or "unknown"
    except Exception:
        return "unknown"


def ensure(display_name: str = "RAPP Sentinel", owner: str = "kody-w") -> dict:
    """Mint this instance's identity once; return it forever after.

    Mint-once matters: re-minting on every start would make the identity meaningless,
    and deriving it from the display name would collide across machines. §6.2 says the
    tail is uuid entropy, never a name-hash — this is that rule, honored."""
    if RAPPID.exists():
        try:
            return json.loads(RAPPID.read_text())
        except Exception:
            pass
    slug = "".join(c if c.isalnum() or c in "-_" else "-"
                   for c in display_name.lower()).strip("-") or "sentinel"
    doc = {
        "schema": "rapp/1",
        "rappid": R.mint_rappid(owner, f"sentinel-{slug}"),
        "kind": "sentinel",
        "name": display_name,
        "host": platform.node(),
        "code_path": str(CODE),
        "home": str(HOME),
        "minted_at": _utc(),
        "notes": ("A sentinel instance. Minted once per RAPP spec §6.2 — uuid-entropy "
                  "tail, never a name-hash, so two instances sharing a display name on "
                  "different machines never collide. Every alert and ledger frame this "
                  "instance emits carries this address plus its running code version."),
    }
    RAPPID.parent.mkdir(parents=True, exist_ok=True)
    RAPPID.write_text(json.dumps(doc, indent=2) + "\n")
    return doc


def stamp(display_name: str = "RAPP Sentinel") -> dict:
    """What every alert and frame should carry: who said it, from where, running what."""
    ident = ensure(display_name)
    return {
        "rappid": ident["rappid"],
        "name": ident.get("name", display_name),
        "host": platform.node(),
        "code_version": code_version(),
    }


if __name__ == "__main__":
    import sys
    print(json.dumps(stamp(sys.argv[1] if len(sys.argv) > 1 else "RAPP Sentinel"), indent=2))
