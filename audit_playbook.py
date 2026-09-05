#!/usr/bin/env python3
"""audit_playbook.py — generate a tailored fan-out audit playbook for any repo.

The pattern this codifies: sentinel/checks.py answers "is it alive?" cheaply,
without a model, forever. This answers a different, more expensive question —
"is the *logic* actually correct?" — which genuinely needs a model, so it is
not something you'd run every 15 minutes. It's the escalation-worthy sibling:
run it periodically (daily/weekly) or on demand, not on sentinel's heartbeat.

Given a path to any git repo, this script inspects its actual shape —
language mix, build/test commands, source layout — and emits a Markdown
playbook: a set of instructions for fanning out read-only audit subagents
over that repo's real files, verifying every finding against source, fixing
only what's proven, and testing with that repo's own real build/test
commands (not assumed ones). The playbook this produces is meant to be
handed to an agent (or a sentinel-style scheduled loop) exactly the way
kody-w/rappterverse's CLAUDE.md "Frontend Quality Loop" section was written
by hand — except this one is generated fresh for a repo nobody has
hand-tailored instructions for yet.

Usage:
    python3 audit_playbook.py /path/to/some/repo > PLAYBOOK.md
    python3 audit_playbook.py /path/to/some/repo --write   # writes PLAYBOOK.md into that repo

No model calls here — this is pure static inspection, same "cheap first"
principle as checks.py. The model only gets invoked once an agent actually
executes the generated playbook.
"""

import json
import subprocess
import sys
from collections import Counter
from pathlib import Path

IGNORE_DIRS = {
    ".git", "node_modules", "__pycache__", ".venv", "venv", "vendor",
    "dist", "build", "target", ".next", ".cache", "coverage",
}

# extension -> (human language name, typical test command guess, typical build command guess)
LANG_HINTS = {
    ".py":  ("Python", "python3 -m pytest {tests}", None),
    ".js":  ("JavaScript", "npm test", "npm run build"),
    ".mjs": ("JavaScript (ESM)", "node {test_entry}", None),
    ".ts":  ("TypeScript", "npm test", "npm run build"),
    ".go":  ("Go", "go test ./...", "go build ./..."),
    ".rs":  ("Rust", "cargo test", "cargo build"),
    ".rb":  ("Ruby", "bundle exec rspec", None),
    ".java": ("Java", "mvn test", "mvn package"),
    ".sh":  ("Shell", None, None),
}

# manifest filename -> language it signals. In a large polyglot repo, a real
# source language's files can be outnumbered by config/data/asset files, so
# relying only on "top 5 extensions by count" (below) missed Rust/Go entirely
# on a 608-file repo whose Cargo.toml/go.mod were found just fine by
# _scan()'s manifest detection — this closes that gap by trusting manifest
# files as a language signal in their own right, not just extension counts.
MANIFEST_LANG_HINTS = {
    "Cargo.toml": "Rust",
    "go.mod": "Go",
    "package.json": "JavaScript/TypeScript",
    "pyproject.toml": "Python",
    "requirements.txt": "Python",
    "setup.py": "Python",
    "Gemfile": "Ruby",
    "pom.xml": "Java",
}

BUG_CATEGORIES = [
    "dead code — fields/functions defined but never read or called anywhere",
    "wrong or nonexistent API usage — code reading/writing a field or "
    "function name that doesn't match what the rest of the codebase actually "
    "defines (grep for the real name before trusting an assumption)",
    "state never reset between init()/cleanup() (or equivalent lifecycle) "
    "pairs — check whether cleanup actually gets *called*, not just whether "
    "it *exists*",
    "reward/event/side-effect paths that are inconsistent with a parallel "
    "path elsewhere in the codebase for the same kind of event",
    "off-by-one errors and boundary conditions",
    "a module's own test coverage encoding the same wrong assumption as a "
    "bug it should be catching (fix the test to assert real behavior, not "
    "a workaround, if you find this)",
]


def _walk_files(root: Path):
    for p in root.rglob("*"):
        if p.is_dir():
            continue
        if any(part in IGNORE_DIRS for part in p.parts):
            continue
        yield p


def _scan(root: Path):
    ext_counts = Counter()
    top_dirs = Counter()
    manifest_files = []
    for p in _walk_files(root):
        rel = p.relative_to(root)
        if rel.suffix:
            ext_counts[rel.suffix] += 1
        if len(rel.parts) > 1:
            top_dirs[rel.parts[0]] += 1
        if rel.name in (
            "package.json", "pyproject.toml", "requirements.txt", "setup.py",
            "Cargo.toml", "go.mod", "Gemfile", "pom.xml", "Makefile",
            "bundle.sh",
        ):
            manifest_files.append(str(rel))
    return ext_counts, top_dirs, manifest_files


def _detect_test_command(root: Path, manifest_files):
    for m in manifest_files:
        if m == "package.json":
            try:
                pkg = json.loads((root / m).read_text())
                scripts = pkg.get("scripts", {})
                if "test" in scripts:
                    return f"npm test   # runs: {scripts['test']}"
            except Exception:
                pass
    for candidate in ("tests/run-regressions.mjs", "scripts/test-cases.js",
                       "scripts/test_state_integrity.py", "run_tests.sh"):
        if (root / candidate).exists():
            return f"(found) {candidate}"
    return None


def _git_remote(root: Path):
    try:
        out = subprocess.run(
            ["git", "-C", str(root), "remote", "get-url", "origin"],
            capture_output=True, text=True, timeout=5,
        )
        return out.stdout.strip() or None
    except Exception:
        return None


def generate(root: Path) -> str:
    ext_counts, top_dirs, manifest_files = _scan(root)
    total_files = sum(ext_counts.values())
    # Extension-based detection alone missed real languages in a large,
    # messy polyglot repo (608 files; Rust/Go were <5 files each, so they
    # never made the "top 5 extensions by raw count" cut) even though their
    # manifest files were sitting right there. Union both signals.
    primary_exts = [e for e in ext_counts if e in LANG_HINTS]
    languages = {LANG_HINTS[e][0] for e in primary_exts}
    for m in manifest_files:
        base = Path(m).name
        if base in MANIFEST_LANG_HINTS:
            languages.add(MANIFEST_LANG_HINTS[base])
    languages = sorted(languages)
    test_cmd = _detect_test_command(root, manifest_files)
    remote = _git_remote(root)

    top_dir_lines = "\n".join(
        f"- `{d}/` — {n} files" for d, n in top_dirs.most_common(12)
    )
    lang_line = ", ".join(languages) if languages else "(mixed / undetected)"
    manifest_line = ", ".join(f"`{m}`" for m in manifest_files) or "(none found)"
    bug_cats = "\n".join(f"- {b}" for b in BUG_CATEGORIES)

    return f"""# Audit Playbook — {root.name}

> Auto-generated by `audit_playbook.py` (rapp-sentinel) — a tailored,
> ready-to-run fan-out audit playbook for this specific repo's real shape.
> This is the escalation-worthy sibling of `checks.py`: run periodically or
> on demand, not on the health heartbeat, because finding logic bugs
> genuinely needs a model.

**Repo:** {remote or root}
**Scanned:** {total_files} files across {len(top_dirs)} top-level directories
**Primary language(s):** {lang_line}
**Manifest/build files found:** {manifest_line}
**Detected test command:** {test_cmd or '(none detected — confirm manually before trusting a green run)'}

## Top-level directories (fan-out scope candidates)

{top_dir_lines}

## The loop

1. **Fan out 2 read-only audit subagents in parallel**, each scoped to 1-3
   related, currently-unaudited files or directories from the list above.
   Tell each agent the exact bug categories below, and explicitly: do not
   fix anything, and report honestly if you find fewer than 2 solid bugs
   rather than padding the list with style nits.

{bug_cats}

2. **Verify every finding yourself** against the actual source before
   touching anything — grep for every reference to a suspect field/function
   across the whole repo before concluding it's dead/unreachable/wrong.
3. **Fix surgically**, with a comment explaining *why*, not just what.
4. **Test before committing, every time:**
   ```
   {test_cmd or '# no test command detected for this repo — find or write one before trusting a "no regressions" claim'}
   ```
   Compare the pass count before and after your change. A regression means
   you broke something; an improvement is a strong signal you fixed
   something the suite was already trying to check.
5. **Commit with the reasoning, not just the change.** Open a PR if the
   repo is branch-protected (check before assuming you can push directly).
   Wait for CI, then merge.
6. **Repeat**, moving to the next unaudited directory, until a round comes
   back with fewer than 2 solid findings — that's the honest signal to stop
   for now, not a reason to invent nitpicks.

## Known gaps in this playbook (fill in as you learn the repo)

- [ ] Confirm the detected test command actually reflects intended CI
      behavior (this was guessed from manifest files, not verified by a run).
- [ ] Identify which directories are generated/vendored and should be
      excluded from audit scope (this scan already excludes common ones —
      node_modules, __pycache__, vendor, dist, build, target — but repo-
      specific generated output may need adding).
- [ ] Note any repo-specific constitution/guardrail docs (CONSTITUTION.md,
      AGENTS.md, CLAUDE.md, CONTRIBUTING.md) and read them before the first
      real audit round — this generator does not read prose, only structure.
"""


def main():
    if len(sys.argv) < 2:
        print("usage: audit_playbook.py /path/to/repo [--write]", file=sys.stderr)
        sys.exit(1)
    root = Path(sys.argv[1]).resolve()
    if not root.is_dir():
        print(f"error: {root} is not a directory", file=sys.stderr)
        sys.exit(1)
    playbook = generate(root)
    if "--write" in sys.argv:
        out_path = root / "AUDIT_PLAYBOOK.md"
        out_path.write_text(playbook)
        print(f"wrote {out_path}")
    else:
        print(playbook)


if __name__ == "__main__":
    main()
