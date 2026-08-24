#!/usr/bin/env python3
"""evolve_worker.py — the proactive art arm, moved out of the 15-minute tick.

WHY THIS FILE EXISTS
The health sentinel runs every 15 minutes and launchd SERIALISES a
StartInterval job (see run.sh): a tick that spends 15-30 minutes inside a
model is a tick during which nothing measures the estate, and the next tick
does not start early to make up for it. Level 3 put a 1800s `copilot` call
inside that tick, so proactive art and the heartbeat were competing for the
same wall clock. One of them always lost, and it was never the art.

So the two jobs are two jobs. The tick keeps measuring; this worker — its own
launchd job, its own lock, its own budget — does the slow creative work
alongside it. `evolve_worker.enabled` in config.json is what moves the art arm
out of the tick; absent, nothing changes for an existing install.

WHAT THIS WORKER IS ALLOWED TO BELIEVE
Almost nothing the model says. The model works in a temporary clone this
worker created, and it may not commit, push, open a PR, or merge. It produces
one new `submissions/<slug>/` folder and a private next-state file. Everything
after that — the deterministic gate, the branch, the commit, the PR, the file
scope of that PR as GitHub reports it, the squash merge, and the re-read of
origin/main afterwards — is done HERE, by code, from evidence. A
`SENTINEL_RESULT: CONTRIBUTED` line is a claim, not a receipt (R1), and this
worker never records a contribution it did not re-read from the merged repo.

FAIL-CLOSED, EVERYWHERE
  * a ledger that will not parse aborts the run; it never resets spend
  * any critical health check aborts, at start and again before every remote
    write and before the merge
  * a degraded estate evolves only when EVERY failing id is named in
    `degraded_allowlist`; there is no blanket "evolve while degraded" switch
  * the gate rejects on the first thing it cannot prove
  * the temporary workspace is removed on every path out
"""

import argparse
import filelock
import hashlib
import json
import os
import posixpath
import re
import pwd
import shlex
import shutil
import signal
import stat
import struct
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
import uuid
import xml.etree.ElementTree as ET
import zlib
from datetime import datetime, timedelta
from pathlib import Path, PurePosixPath
from urllib.parse import quote, urlsplit

import neighborhood as NB
import sentinel
import subsentinels as SS
import azure_art
from paths import HOME

STATE = HOME / "state"
LOGS = HOME / "logs"
STOP = HOME / "STOP"

LOCK_PATH = STATE / "evolve-worker.lock"
HISTORY_PATH = STATE / "evolve-worker-history.json"
TURN_PATH = STATE / "evolve-worker-turn.json"
ALERT_PATH = STATE / "evolve-worker-alerts.json"
STATUS_PATH = STATE / "evolve-worker-status.json"
TRANSACTION_PATH = STATE / "evolve-worker-transaction.json"
ART_ARCHIVE = STATE / "art"

# Every model process this pass started, so a timeout, a SIGTERM or a crash
# can take the whole tree down instead of orphaning a 30-minute model run and
# whatever it spawned. The lock is released only after this list is empty.
_LIVE = []
_LIVE_LOCK = threading.Lock()

HISTORY_KEEP = 400

# The worker's own config block. Every key falls back to the level-3 key the
# in-tick arm already used, so an install that only flips `enabled` keeps the
# cadence, budget and timeout it has been running with.
WORKER_DEFAULTS = {
    "enabled": False,
    "repo": "kody-w/public-art-collective",
    "base_branch": "main",
    "roles": [],                     # empty = the whole neighborhood roster
    "degraded_allowlist": [],        # exact check ids; no wildcards on purpose
    "workspace_root": "state/evolve-workspaces",
    "branch_prefix": "art",
    "clone_depth": 50,
    "git_author_name": "RAPP Sentinel evolve worker",
    "git_author_email": "rapp-sentinel@users.noreply.github.com",
    "git_timeout_s": 600,
    "gh_timeout_s": 300,
    "max_piece_bytes": 51200,
    "max_meta_bytes": 262144,
    "max_state_bytes": 262144,
    "publication_profile": "",
    # A newly opened PR can take a short time to receive its protected
    # provenance check. Absence, and a lone cancel-in-progress result while
    # GitHub schedules its replacement, are pending only for this window.
    "provenance_absent_grace_s": 300,
    "allowed_kinds": ["svg", "md", "txt", "json"],
    "notify_declines": False,
    "azure_image": {
        "enabled": False,
        "endpoint": "",
        "deployment": "gpt-image-2",
        "fallback_deployment": "gpt-image",
        "api_version": "2025-04-01-preview",
        "size": "1536x1024",
        "quality": "high",
        "max_attempts": 3,
        "minimum_review_score": 8,
        "review_model": "gpt-5.4",
        "review_timeout_s": 300,
        "review_transcript_bytes": 16 * 1024 * 1024,
        "open_in_browser": False,
    },
    # Optional second deployment. The canonical submission still lands in
    # repo; this adapter mirrors the verified bytes into a RAPP Vision channel
    # and finalization waits until both Pages surfaces answer.
    "rapp_vision": {
        "enabled": False,
        "repo": "kody-w/rapp-vision",
        "base_branch": "main",
        "channel_id": "dada-collective",
        "channel_name": "Dada Collective",
        "channel_path": "dada/channel.json",
        "media_dir": "dada/media",
        "registry_path": "channels.json",
        "branch_prefix": "art/dada-vision",
        "player_url": "https://kody-w.github.io/rapp-vision",
        "collective_viewer_url": "https://kody-w.github.io/public-art-collective/view.html",
        "duration": 60,
        "deployment_retry_limit": 12,
    },
    # Bounded sub-sentinel fan-out (subsentinels.py). Off by default; see
    # FANOUT_DEFAULTS there for the caps every key inherits.
    "fanout": {},
}

# ── the submission protocol, as code ────────────────────────────────────────
# Mirrors specs/SUBMISSION_PROTOCOL.md in public-art-collective. Written out
# here because a gate that reads its rules from the repo it is gating can be
# talked out of them by the same PR it is judging.
SUBMISSION_SCHEMA = "rapp-art-submission/1.0"
REQUIRED_META_KEYS = ("schema", "title", "slug", "contributor", "kind",
                      "submitted_at", "remix_of", "license")
ALLOWED_LICENSES = ("CC0-1.0",)
KIND_EXTENSIONS = {
    "svg": ".svg", "md": ".md", "txt": ".txt", "json": ".json", "png": ".png",
}
SLUG_RE = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")
SLUG_MAX = 48

AZURE_REVIEWED_PNG_PROFILE = "azure-reviewed-png"
PROFILE_SNAPSHOT_SCHEMA = "rapp-publication-profile/1.0"
IMAGE_GENERATION_SCHEMA = "rapp-image-generation/1.0"
IMAGE_REVIEW_SCHEMA = "rapp-image-review/1.0"
FINAL_NOTIFICATION_SCHEMA = "rapp-evolve-art-notification/1.0"
DEPLOYMENT_RECEIPT_SCHEMA = "rapp-evolve-deployment-receipt/1.0"
IMAGE_PROVIDER = "azure-openai"
PROFILE_MIN_PIECE_BYTES = 4 * 1024 * 1024
PROFILE_MAX_PIECE_BYTES = 32 * 1024 * 1024
PNG_MIN_DIMENSION = 512
PNG_MAX_DIMENSION = 4096
PNG_MAX_PIXELS = 16_000_000
PNG_MAX_CHUNK_BYTES = PROFILE_MAX_PIECE_BYTES
PNG_MAX_CHUNKS = 10_000
IMAGE_MAX_IDENTIFIER_CHARS = 100
IMAGE_MAX_STRENGTHS = 8
IMAGE_MAX_STRENGTH_CHARS = 240
IMAGE_IDENTIFIER_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,99}$")
COLLECTIVE_PROVENANCE_WORKFLOW = "Reviewed PNG provenance"
COLLECTIVE_PROVENANCE_CHECK = "Verify controller provenance"
COLLECTIVE_PROVENANCE_FULL_NAME = (
    f"{COLLECTIVE_PROVENANCE_WORKFLOW} / {COLLECTIVE_PROVENANCE_CHECK}")
PROVENANCE_ROLLUP_JSON_FIELDS = (
    "statusCheckRollup,mergeStateStatus,state")
PROVENANCE_PENDING_STATUSES = frozenset({
    "EXPECTED", "PENDING", "QUEUED", "IN_PROGRESS", "REQUESTED", "WAITING",
})
PROVENANCE_FAILURE_CONCLUSIONS = frozenset({
    "ACTION_REQUIRED", "FAILURE", "NEUTRAL", "SKIPPED", "STALE",
    "STARTUP_FAILURE", "TIMED_OUT",
})
REVIEWED_PNG_BRANCH_PREFIX = "art/dada"
REVIEWED_PNG_COMMIT_NAME = "Dada Collective"
REVIEWED_PNG_COMMIT_EMAIL = "kody-w@users.noreply.github.com"
REVIEWED_PNG_CONTRIBUTOR = "kody-w"
REVIEWED_PNG_TITLE_MAX_CHARS = 200
REVIEWED_PNG_COMMIT_BODY_TEMPLATE = (
    "Autonomous submission by the {role} neighbor of Dada Collective.")
REVIEWED_PNG_ROLE_RE = re.compile(
    r"^[A-Za-z0-9][A-Za-z0-9._ -]{0,63}$")
RAW_CREDENTIAL_RE = re.compile(
    r"(?i)(?:"
    r"\bBearer\s+[A-Za-z0-9._~+/=-]{8,}|"
    r"\b(?:gh[pousr]_|github_pat_|sk-)[A-Za-z0-9_-]{8,}|"
    r"\beyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}|"
    r"\b(?:api[ _-]?key|access[ _-]?token|client[ _-]?secret|password)"
    r"\s*[:=]\s*\S+|"
    r"[?&](?:sig|token|key)=[^&\s]+"
    r")"
)
CREDENTIAL_KEY_NAMES = frozenset({
    "apikey", "accesstoken", "refreshtoken", "token", "credential",
    "credentials", "password", "secret", "clientsecret", "authorization",
})
COLLECTIVE_VALIDATOR_PATH = "tools/build_index.py"
COLLECTIVE_VALIDATOR_TIMEOUT_S = 120

# ── the dada cycle invariants ───────────────────────────────────────────────
# The point of the cycle block is that the SEARCH is evidence, not decoration:
# a fixed candidate count per round is the difference between "it explored"
# and "it wrote down that it explored".
SCORE_DIMENSIONS = ("absurdity", "novelty", "craft", "coherence",
                    "provocation", "restraint")
CANDIDATES_PER_ROUND = 10
MIN_ROUNDS = 1
MAX_ROUNDS = 5
SCORE_MIN = 0
SCORE_MAX = 10

OUTCOME_CONTRIBUTED = "contributed"
OUTCOME_DECLINED = "declined"
OUTCOME_TIMEOUT = "timeout"
OUTCOME_FAILED = "failed"
OUTCOME_REJECTED = "rejected"
OUTCOME_ABORTED = "aborted"
OUTCOME_FANOUT = "fanout-failed"

# Only a verified merge is allowed to wear the paintbrush. Every other outcome
# gets a shape a human reads as "something did not happen" (#7).
SUCCESS_PREFIX = "\U0001F3A8"     # 🎨


class LedgerError(RuntimeError):
    """Durable state exists but cannot be trusted. Never overwrite it."""


class GateError(RuntimeError):
    """The model's output failed a deterministic check. Never publish it."""


class AbortError(RuntimeError):
    """Health said stop, mid-flight. Undo what is undoable, record the truth."""


class CommandError(RuntimeError):
    """A git/gh command failed or timed out."""


class DeploymentPending(RuntimeError):
    """A merge is real but both public Pages deployments are not ready yet."""


class MergeAmbiguous(RuntimeError):
    """The canonical merge command was invoked, so failure is not non-merge."""


class MergeNotConfirmed(CommandError):
    """GitHub explicitly reports that the PR is not merged."""

    def __init__(self, pr_number, state):
        self.pr_number = str(pr_number)
        self.state = str(state or "")
        super().__init__(
            f"PR {self.pr_number} is not merged: {self.state!r}")


class ChecksPending(RuntimeError):
    """A protected PR check is not yet a durable permission to merge."""


class ProvenanceCheckFailed(AbortError):
    """The required Collective provenance check or merge gate rejected a PR."""


class NotificationPending(RuntimeError):
    """The contribution is durable but its final outbox enqueue is not."""


def _collective_text(value, label, max_chars=500):
    """Use Collective verify_png_attestation._text's exact grammar."""
    if (
        not isinstance(value, str)
        or not value
        or value != value.strip()
        or len(value) > max_chars
        or any(ord(char) < 0x20 for char in value)
    ):
        raise GateError(
            f"{label} must be a clean non-empty string of at most "
            f"{max_chars} characters"
        )
    return value


def _submission_slug(value):
    if (
        not isinstance(value, str)
        or len(value) > SLUG_MAX
        or not SLUG_RE.fullmatch(value)
    ):
        raise GateError(
            f"meta.slug {value!r} is not lowercase-alphanumeric-hyphen "
            f"within {SLUG_MAX} characters"
        )
    return value


def validate_reviewed_submission_contract(meta, expected_slug=None):
    """Validate metadata fields pinned by Collective's reviewed-PNG gate."""
    if not isinstance(meta, dict):
        raise GateError("reviewed-PNG meta.json must be an object")
    slug = _submission_slug(meta.get("slug"))
    if expected_slug is not None and slug != expected_slug:
        raise GateError("reviewed PNG metadata conflicts with its gated slug")
    title = _collective_text(
        meta.get("title"),
        f"{slug}: title",
        REVIEWED_PNG_TITLE_MAX_CHARS,
    )
    if meta.get("contributor") != REVIEWED_PNG_CONTRIBUTOR:
        raise GateError(
            f"{slug}: reviewed PNG contributor must be "
            f"'{REVIEWED_PNG_CONTRIBUTOR}'"
        )
    return {"slug": slug, "title": title,
            "contributor": REVIEWED_PNG_CONTRIBUTOR}


def reviewed_archive_destination(meta):
    """Resolve a validated slug to one direct child of the archive root."""
    slug = _submission_slug(
        meta.get("slug") if isinstance(meta, dict) else None)
    archive_root = Path(ART_ARCHIVE).resolve()
    archive = (archive_root / f"{slug}.png").resolve()
    if archive.parent != archive_root:
        raise GateError(
            "reviewed PNG archive destination is not a direct child of "
            "the configured archive root"
        )
    return archive


def _bounded_receipt_text(value, subject, label, max_chars, identifier=False):
    """Mirror Public Art Collective's reviewed-PNG text contract exactly."""
    if (
        not isinstance(value, str)
        or not value
        or value != value.strip()
        or len(value) > max_chars
    ):
        raise GateError(
            f"{subject}: {label} must be a non-empty string of at most "
            f"{max_chars} characters"
        )
    if any(ord(char) < 0x20 for char in value):
        raise GateError(f"{subject}: {label} contains control characters")
    if identifier and not IMAGE_IDENTIFIER_RE.fullmatch(value):
        raise GateError(f"{subject}: {label} is not a bounded identifier")
    if RAW_CREDENTIAL_RE.search(value):
        raise GateError(f"{subject}: {label} appears to contain a credential")
    return value


def _reject_credential_material(value, subject, path="meta"):
    """Mirror Collective's recursive credential field and value rejection."""
    if isinstance(value, dict):
        for key, child in value.items():
            normalized = re.sub(r"[^a-z0-9]", "", key.lower())
            child_path = f"{path}.{key}"
            if any(
                normalized == name or normalized.endswith(name)
                for name in CREDENTIAL_KEY_NAMES
            ):
                raise GateError(
                    f"{subject}: {child_path} is a forbidden credential field"
                )
            _reject_credential_material(child, subject, child_path)
    elif isinstance(value, list):
        for index, child in enumerate(value):
            _reject_credential_material(
                child, subject, f"{path}[{index}]"
            )
    elif isinstance(value, str) and RAW_CREDENTIAL_RE.search(value):
        raise GateError(
            f"{subject}: {path} appears to contain a raw credential or token"
        )


# ── the process tree ────────────────────────────────────────────────────────

def track(proc):
    with _LIVE_LOCK:
        _LIVE.append(proc)
    return proc


def untrack(proc):
    with _LIVE_LOCK:
        try:
            _LIVE.remove(proc)
        except ValueError:
            pass


def live_processes():
    with _LIVE_LOCK:
        return [p for p in _LIVE if p.poll() is None]


def kill_tracked(grace=5.0):
    """Terminate every model process this pass started, and their children.

    Each is spawned with start_new_session, so one signal per process GROUP
    reaches the grandchildren a model shelled into. Returns how many were
    still alive when we started, for the caller to report honestly.
    """
    with _LIVE_LOCK:
        procs = list(_LIVE)
    alive = [p for p in procs if p.poll() is None]
    for proc in alive:
        SS._kill_group(proc, grace)
    for proc in procs:
        untrack(proc)
    return len(alive)


_STOPPING = threading.Event()


def _handle_signal(signum, frame):
    """A stopped worker must not leave a model running.

    launchd's ceiling, a reboot, or an operator's kill all arrive here. Kill
    the tree first, then let the normal unwind delete the workspace and
    release the lock — in that order, so nothing that is still running can
    outlive the lock that says a cycle is in flight (#4).
    """
    _STOPPING.set()
    killed = kill_tracked()
    log(f"signal {signum} — terminated {killed} live model process tree(s)")
    raise KeyboardInterrupt(f"signal {signum}")


def install_signal_handlers():
    for sig in (signal.SIGTERM, signal.SIGINT, signal.SIGHUP):
        try:
            signal.signal(sig, _handle_signal)
        except (ValueError, OSError):
            pass          # not the main thread (tests) — nothing to install


# ── logging ─────────────────────────────────────────────────────────────────

def log(msg):
    line = f"[{sentinel.now().isoformat(timespec='seconds')}] evolve-worker: {msg}"
    print(line, flush=True)
    try:
        LOGS.mkdir(parents=True, exist_ok=True)
        with open(LOGS / f"evolve-worker-{sentinel.now():%Y-%m}.log", "a",
                  encoding="utf-8") as fh:
            fh.write(line + "\n")
    except OSError:
        pass


# ── config ──────────────────────────────────────────────────────────────────

def worker_config(cfg):
    """The worker block, merged over defaults and the level-3 fallbacks."""
    block = cfg.get("evolve_worker")
    block = dict(block) if isinstance(block, dict) else {}
    merged = dict(WORKER_DEFAULTS)
    merged["interval_hours"] = float(cfg.get("evolve_interval_hours", 4))
    merged["daily_budget"] = int(cfg.get("daily_evolve_budget", 2))
    merged["timeout_s"] = int(cfg.get("evolve_timeout_s", 1800))
    merged["model"] = cfg.get("copilot_model", "claude-sonnet-4.6")
    merged["creative_state_file"] = str(
        cfg.get("creative_state_file", "state/evolve-creative-state.json"))
    for key, value in block.items():
        merged[key] = value
    return merged


def allowed_kinds(wcfg):
    raw = wcfg.get("allowed_kinds")
    values = (list(WORKER_DEFAULTS["allowed_kinds"])
              if raw is None else list(raw))
    if not values or any(kind not in KIND_EXTENSIONS for kind in values):
        raise GateError("allowed_kinds must be a non-empty subset of "
                        + ", ".join(sorted(KIND_EXTENSIONS)))
    return tuple(dict.fromkeys(values))


def azure_image_config(wcfg):
    defaults = dict(WORKER_DEFAULTS["azure_image"])
    block = wcfg.get("azure_image")
    if isinstance(block, dict):
        defaults.update(block)
    defaults["enabled"] = bool(defaults.get("enabled"))
    if not defaults["enabled"]:
        return defaults
    endpoint = str(defaults.get("endpoint") or "").strip().rstrip("/")
    if not endpoint.startswith("https://"):
        raise GateError("azure_image.endpoint must be an HTTPS endpoint")
    defaults["endpoint"] = endpoint
    defaults["max_attempts"] = max(
        1, min(5, int(defaults.get("max_attempts") or 2)))
    defaults["minimum_review_score"] = max(
        1, min(10, int(defaults.get("minimum_review_score") or 8)))
    defaults["review_timeout_s"] = max(
        30, min(900, int(defaults.get("review_timeout_s") or 300)))
    return defaults


def assert_visual_pipeline_ready(wcfg):
    cfg = azure_image_config(wcfg)
    if not cfg["enabled"]:
        return "Azure visual generation disabled"
    if "png" not in allowed_kinds(wcfg):
        raise AbortError("azure_image is enabled but png is not allowed")
    if not shutil.which("az"):
        raise AbortError("Azure CLI is not installed")
    if not shutil.which("copilot"):
        raise AbortError("Copilot CLI is not installed for visual review")
    auth_var = str(SS.fanout_config(wcfg).get(
        "auth_env_var") or "COPILOT_GITHUB_TOKEN")
    if not os.environ.get(auth_var):
        raise AbortError(
            f"{auth_var} is not available to the multimodal reviewer")
    try:
        token = azure_art._access_token(
            str(cfg.get("az_binary") or "az"),
            int(cfg.get("auth_timeout_s") or 60))
    except azure_art.AzureArtError as exc:
        raise AbortError(str(exc)) from exc
    return f"Azure image auth ready ({len(token)} token chars); visual reviewer ready"


def _visual_review_prompt(brief, minimum):
    return (
        "You are the final visual art director for a public gallery. Inspect "
        "the ATTACHED IMAGE itself, not its filename and not the maker's claim. "
        "Judge composition, visual impact, coherence, craft, originality, "
        "legibility, obvious generation artifacts, accidental text/gibberish, "
        "and whether this looks like finished art worth sending to friends. "
        f"The intended brief was: {brief}\n\n"
        "Return exactly one JSON object and no markdown: "
        f'{{"schema":"{IMAGE_REVIEW_SCHEMA}","score":0,'
        '"publish":false,"failures":["specific visible defect"],'
        '"strengths":["specific visible strength"]}. '
        f"publish may be true only when score is at least {minimum}, there are "
        "no obvious rendering defects, and the image feels complete."
    )


def normalize_visual_review(review):
    """Strict, bounded reviewer output, including injected test reviewers."""
    score = review.get("score") if isinstance(review, dict) else None
    if (not isinstance(review, dict)
            or set(review) != {
                "schema", "score", "publish", "failures", "strengths"}
            or review.get("schema") != IMAGE_REVIEW_SCHEMA
            or not isinstance(score, (int, float))
            or isinstance(score, bool)
            or score != score
            or score in (float("inf"), float("-inf"))
            or not float(score).is_integer()
            or not 0 <= score <= 10
            or not isinstance(review.get("publish"), bool)
            or not isinstance(review.get("failures"), list)
            or not isinstance(review.get("strengths"), list)):
        raise GateError("visual review has the wrong schema")
    bounded = {}
    for field in ("failures", "strengths"):
        values = review[field]
        if len(values) > IMAGE_MAX_STRENGTHS:
            raise GateError(
                f"visual review {field} must contain at most "
                f"{IMAGE_MAX_STRENGTHS} strings")
        bounded[field] = [
            _bounded_receipt_text(
                item,
                "visual review",
                f"{field}[{index}]",
                IMAGE_MAX_STRENGTH_CHARS,
            )
            for index, item in enumerate(values)
        ]
    return {
        "schema": IMAGE_REVIEW_SCHEMA,
        "score": int(score),
        "publish": review["publish"],
        "failures": bounded["failures"],
        "strengths": bounded["strengths"],
    }


def review_generated_image(image_path, brief, wcfg):
    cfg = azure_image_config(wcfg)
    fcfg = SS.fanout_config(wcfg)
    runtime = Path(image_path).parent / "review-runtime"
    runtime.mkdir(parents=True, exist_ok=True)
    env = SS.confined_env(fcfg, runtime, 0)
    argv = SS.confined_argv(
        _visual_review_prompt(brief, cfg["minimum_review_score"]),
        str(cfg.get("review_model") or "gpt-5.4"),
        runtime,
        tools=SS.CHILD_TOOLS,
        secret_vars=SS.secret_vars_for(fcfg),
        log_dir=runtime / "copilot-logs",
        json_output=True,
    )
    argv += ["--attachment", str(Path(image_path).resolve())]
    proc = track(subprocess.Popen(
        argv, cwd=str(runtime), env=env, text=True,
        stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        start_new_session=True))
    try:
        stdout, stderr = proc.communicate(timeout=cfg["review_timeout_s"])
    except subprocess.TimeoutExpired:
        SS._kill_group(proc, 5)
        raise GateError("multimodal visual review timed out")
    finally:
        untrack(proc)
    if proc.returncode != 0:
        raise GateError(
            f"multimodal visual review exited {proc.returncode}: "
            f"{(stderr or stdout or '').strip()[:240]}")
    content = SS.extract_assistant_message(
        stdout,
        max_bytes=int(cfg.get("review_transcript_bytes")
                      or 16 * 1024 * 1024))
    try:
        review = SS.extract_report(content, 65536)
    except Exception as exc:
        raise GateError(f"visual review returned unreadable JSON: {exc}") from exc
    return normalize_visual_review(review)


def _atomic_write_bytes(path, payload):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    try:
        with temp.open("wb") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp, path)
    finally:
        temp.unlink(missing_ok=True)


def materialize_azure_image(staging, wcfg, generator=None, reviewer=None):
    """Replace a maker-written piece.prompt with a reviewed Azure PNG."""
    out = Path(staging) / "out" / SUBMISSION_DIR
    meta_path = out / META_NAME
    if not meta_path.is_file():
        return None
    try:
        meta = json.loads(meta_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return None
    if not isinstance(meta, dict):
        raise GateError("reviewed-PNG meta.json must be an object")
    if meta.get("kind") != "png":
        return None
    archive = reviewed_archive_destination(meta)
    validate_reviewed_submission_contract(meta)
    profile_snapshot = publication_profile_snapshot(wcfg)
    if not profile_snapshot:
        raise GateError(
            "PNG generation requires the azure-reviewed-png publication profile")
    cfg = azure_image_config(wcfg)
    if not cfg["enabled"]:
        raise GateError("meta.kind is png but azure_image is disabled")
    if profile_snapshot and "_image_generation" in meta:
        raise GateError(
            "the maker may not supply _image_generation; only the controller "
            "can issue a reviewed-PNG receipt")
    prompt_path = out / "piece.prompt"
    if not prompt_path.is_file():
        raise GateError("Azure PNG output requires piece.prompt")
    prompt = prompt_path.read_text(encoding="utf-8").strip()
    declared = str(meta.get("_image_prompt") or "").strip()
    if not prompt or len(prompt) > 4000:
        raise GateError("piece.prompt must contain 1-4000 characters")
    if declared != prompt:
        raise GateError("meta._image_prompt must exactly match piece.prompt")
    _reject_credential_material(
        meta, str(meta.get("slug") or "reviewed-PNG submission")
    )
    generator = generator or azure_art.generate
    reviewer = reviewer or review_generated_image
    runtime = Path(staging).parent / "runtime" / "azure-art"
    runtime.mkdir(parents=True, exist_ok=True)
    failures = []
    accepted = None
    deployment = ""
    review = None
    image_info = None
    current_prompt = prompt
    for attempt in range(1, cfg["max_attempts"] + 1):
        try:
            image, deployment = generator(current_prompt, cfg)
        except azure_art.AzureArtError as exc:
            raise GateError(str(exc)) from exc
        if not isinstance(image, bytes):
            raise GateError("Azure image generation returned a non-bytes payload")
        max_bytes = int(wcfg.get("max_piece_bytes", 51200))
        if len(image) > max_bytes:
            raise GateError(
                f"generated PNG is {len(image)} bytes, over the "
                f"{max_bytes} byte cap")
        image_info = _check_png(image)
        candidate = runtime / f"candidate-{attempt}.png"
        _atomic_write_bytes(candidate, image)
        review = normalize_visual_review(
            reviewer(candidate, current_prompt, wcfg))
        score_ok = review["score"] >= cfg["minimum_review_score"]
        if review["publish"] and score_ok and not review["failures"]:
            accepted = image
            break
        failures = review["failures"] or [
            f"visual score {review['score']} is below "
            f"{cfg['minimum_review_score']}"]
        current_prompt = (
            prompt + "\n\nA previous generated attempt was rejected by an "
            "independent visual judge for these visible problems: "
            + "; ".join(failures)
            + ". Create a substantially improved composition that fixes every "
              "problem while preserving the core concept."
        )
    if accepted is None:
        raise GateError(
            "Azure image failed visual review after "
            f"{cfg['max_attempts']} attempt(s): " + "; ".join(failures))
    piece_path = out / "piece.png"
    _atomic_write_bytes(piece_path, accepted)
    prompt_path.unlink()
    digest = hashlib.sha256(accepted).hexdigest()
    meta["_image_generation"] = {
        "schema": IMAGE_GENERATION_SCHEMA,
        "profile": AZURE_REVIEWED_PNG_PROFILE,
        "provider": IMAGE_PROVIDER,
        "deployment": deployment,
        "attempts": attempt,
        "image_sha256": digest,
        "image": {
            "width": image_info["width"],
            "height": image_info["height"],
        },
        "review": {
            "schema": IMAGE_REVIEW_SCHEMA,
            "model": profile_snapshot["review_model"],
            "publish": True,
            "score": review["score"],
            "minimum_score": profile_snapshot["minimum_review_score"],
            "failures": [],
            "strengths": review["strengths"],
        },
    }
    validate_image_generation_receipt(meta, accepted, profile_snapshot)
    atomic_write_json(meta_path, meta)
    _atomic_write_bytes(archive, accepted)
    return {
        "archive": str(archive),
        "deployment": deployment,
        "attempts": attempt,
        "score": review["score"],
    }


def worker_enabled(cfg):
    """True when this instance delegates proactive art to this worker."""
    block = cfg.get("evolve_worker")
    return bool(isinstance(block, dict) and block.get("enabled"))


def roles_for(wcfg):
    """Rotation order, restricted to neighbors that actually have a chain."""
    declared = wcfg.get("roles") or []
    roles = [r for r in declared if r in NB.NEIGHBORS]
    return roles or list(NB.NEIGHBORS)


VISION_CHANNEL_SCHEMA = "rapp-vision-channel/1.0"
VISION_NETWORK_SCHEMA = "rapp-vision-network/1.0"


def _repo_relative_path(value, label):
    raw = str(value or "").strip()
    text = raw.rstrip("/")
    path = PurePosixPath(text)
    if (not text or raw.startswith("/") or "\\" in raw or "//" in raw
            or path.is_absolute() or path.as_posix() != text
            or ".." in path.parts
            or any(not part or part.startswith(".") for part in path.parts)):
        raise GateError(f"{label} must be a plain repository-relative path")
    return path.as_posix()


def vision_config(wcfg):
    defaults = dict(WORKER_DEFAULTS["rapp_vision"])
    block = wcfg.get("rapp_vision")
    if isinstance(block, dict):
        defaults.update(block)
    defaults["enabled"] = bool(defaults.get("enabled"))
    if not defaults["enabled"]:
        return defaults
    for key in ("channel_path", "media_dir", "registry_path"):
        if defaults.get(key):
            defaults[key] = _repo_relative_path(defaults[key], f"rapp_vision.{key}")
    if not str(defaults.get("channel_path", "")).endswith(".json"):
        raise GateError("rapp_vision.channel_path must end in .json")
    if defaults.get("registry_path") and not str(
            defaults["registry_path"]).endswith(".json"):
        raise GateError("rapp_vision.registry_path must end in .json")
    channel_id = str(defaults.get("channel_id") or "").strip()
    if not SLUG_RE.fullmatch(channel_id):
        raise GateError("rapp_vision.channel_id must be a lowercase slug")
    defaults["channel_id"] = channel_id
    defaults["channel_name"] = str(
        defaults.get("channel_name") or channel_id).strip()
    defaults["player_url"] = str(defaults.get("player_url") or "").rstrip("/")
    if not defaults["player_url"].startswith("https://"):
        raise GateError("rapp_vision.player_url must be https")
    defaults["collective_viewer_url"] = str(
        defaults.get("collective_viewer_url") or "").rstrip("/")
    if not defaults["collective_viewer_url"].startswith("https://"):
        raise GateError("rapp_vision.collective_viewer_url must be https")
    defaults["duration"] = max(12, min(600, int(defaults.get("duration") or 60)))
    return defaults


PROFILE_SNAPSHOT_KEYS = {
    "schema", "profile", "allowed_kinds", "max_piece_bytes", "provider",
    "deployments", "max_attempts", "review_schema", "review_model",
    "minimum_review_score", "rapp_vision_enabled",
}


def publication_profile_name(wcfg):
    """Resolve explicit profile selection plus the deployed legacy signal."""
    raw = wcfg.get("publication_profile")
    explicit = "" if raw is None else raw
    if not isinstance(explicit, str):
        raise GateError("publication_profile must be a string")
    explicit = explicit.strip()
    if explicit and explicit != AZURE_REVIEWED_PNG_PROFILE:
        raise GateError(
            f"unknown publication_profile {explicit!r}; supported profile is "
            f"{AZURE_REVIEWED_PNG_PROFILE!r}")
    azure = wcfg.get("azure_image")
    legacy_active = (
        isinstance(wcfg.get("allowed_kinds"), list)
        and wcfg.get("allowed_kinds") == ["png"]
        and isinstance(azure, dict)
        and azure.get("enabled") is True
    )
    return explicit or (
        AZURE_REVIEWED_PNG_PROFILE if legacy_active else "")


def validate_profile_snapshot(snapshot):
    """Validate a persisted profile without consulting mutable config."""
    if not isinstance(snapshot, dict) or set(snapshot) != PROFILE_SNAPSHOT_KEYS:
        raise GateError(
            "publication profile snapshot has the wrong fields")
    if snapshot.get("schema") != PROFILE_SNAPSHOT_SCHEMA:
        raise GateError("publication profile snapshot has the wrong schema")
    if snapshot.get("profile") != AZURE_REVIEWED_PNG_PROFILE:
        raise GateError("publication profile snapshot names the wrong profile")
    if snapshot.get("allowed_kinds") != ["png"]:
        raise GateError("publication profile snapshot must allow exactly png")
    cap = snapshot.get("max_piece_bytes")
    if (isinstance(cap, bool) or not isinstance(cap, int)
            or not PROFILE_MIN_PIECE_BYTES <= cap <= PROFILE_MAX_PIECE_BYTES):
        raise GateError(
            "publication profile snapshot has an unsafe max_piece_bytes")
    if snapshot.get("provider") != IMAGE_PROVIDER:
        raise GateError("publication profile snapshot has the wrong provider")
    deployments = snapshot.get("deployments")
    if (not isinstance(deployments, list)
            or not 1 <= len(deployments) <= 2
            or any(not isinstance(item, str) for item in deployments)):
        raise GateError(
            "publication profile snapshot has invalid Azure deployments")
    for index, deployment in enumerate(deployments):
        _bounded_receipt_text(
            deployment,
            "publication profile snapshot",
            f"deployments[{index}]",
            IMAGE_MAX_IDENTIFIER_CHARS,
            identifier=True,
        )
    if len(set(deployments)) != len(deployments):
        raise GateError(
            "publication profile snapshot has duplicate Azure deployments")
    attempts = snapshot.get("max_attempts")
    if (isinstance(attempts, bool) or not isinstance(attempts, int)
            or not 1 <= attempts <= 5):
        raise GateError(
            "publication profile snapshot has invalid max_attempts")
    if snapshot.get("review_schema") != IMAGE_REVIEW_SCHEMA:
        raise GateError("publication profile snapshot has the wrong review schema")
    _bounded_receipt_text(
        snapshot.get("review_model"),
        "publication profile snapshot",
        "review_model",
        IMAGE_MAX_IDENTIFIER_CHARS,
        identifier=True,
    )
    minimum = snapshot.get("minimum_review_score")
    if (isinstance(minimum, bool) or not isinstance(minimum, int)
            or not 8 <= minimum <= 10):
        raise GateError(
            "publication profile snapshot requires minimum_review_score 8..10")
    if snapshot.get("rapp_vision_enabled") is not True:
        raise GateError("publication profile snapshot requires RAPP Vision")
    return {
        **snapshot,
        "allowed_kinds": ["png"],
        "deployments": list(deployments),
    }


def publication_profile_snapshot(wcfg):
    """Return the immutable active profile contract, or None for legacy."""
    profile = publication_profile_name(wcfg)
    kinds = allowed_kinds(wcfg)
    if profile != AZURE_REVIEWED_PNG_PROFILE:
        if "png" in kinds:
            raise GateError(
                "PNG requires the azure-reviewed-png publication profile; "
                "legacy and mixed PNG configurations are not publishable")
        return None
    if (not isinstance(wcfg.get("allowed_kinds"), list)
            or wcfg.get("allowed_kinds") != ["png"]):
        raise GateError(
            f"{AZURE_REVIEWED_PNG_PROFILE} requires allowed_kinds exactly "
            "['png']")
    azure_block = wcfg.get("azure_image")
    if not isinstance(azure_block, dict) or azure_block.get("enabled") is not True:
        raise GateError(
            f"{AZURE_REVIEWED_PNG_PROFILE} requires azure_image.enabled=true")
    vision_block = wcfg.get("rapp_vision")
    if not isinstance(vision_block, dict) or vision_block.get("enabled") is not True:
        raise GateError(
            f"{AZURE_REVIEWED_PNG_PROFILE} requires rapp_vision.enabled=true")
    max_piece_bytes = wcfg.get("max_piece_bytes")
    if (isinstance(max_piece_bytes, bool)
            or not isinstance(max_piece_bytes, int)
            or not PROFILE_MIN_PIECE_BYTES <= max_piece_bytes
            <= PROFILE_MAX_PIECE_BYTES):
        raise GateError(
            f"{AZURE_REVIEWED_PNG_PROFILE} requires max_piece_bytes between "
            f"{PROFILE_MIN_PIECE_BYTES} and {PROFILE_MAX_PIECE_BYTES}")
    minimum = azure_block.get(
        "minimum_review_score",
        WORKER_DEFAULTS["azure_image"]["minimum_review_score"])
    if (isinstance(minimum, bool) or not isinstance(minimum, int)
            or not 8 <= minimum <= 10):
        raise GateError(
            f"{AZURE_REVIEWED_PNG_PROFILE} requires "
            "minimum_review_score between 8 and 10")
    attempts = azure_block.get(
        "max_attempts", WORKER_DEFAULTS["azure_image"]["max_attempts"])
    if (isinstance(attempts, bool) or not isinstance(attempts, int)
            or not 1 <= attempts <= 5):
        raise GateError(
            f"{AZURE_REVIEWED_PNG_PROFILE} requires max_attempts between 1 and 5")
    try:
        azure = azure_image_config(wcfg)
        vision = vision_config(wcfg)
    except (TypeError, ValueError) as exc:
        raise GateError(f"invalid {AZURE_REVIEWED_PNG_PROFILE} config: {exc}") from exc
    validate_reviewed_vision_config(vision)
    deployments = []
    for key in ("deployment", "fallback_deployment"):
        deployment = azure.get(key)
        if deployment is None or deployment == "":
            continue
        _bounded_receipt_text(
            deployment,
            "publication profile config",
            f"azure_image.{key}",
            IMAGE_MAX_IDENTIFIER_CHARS,
            identifier=True,
        )
        if deployment not in deployments:
            deployments.append(deployment)
    if not deployments:
        raise GateError(
            f"{AZURE_REVIEWED_PNG_PROFILE} requires an Azure deployment")
    model = _bounded_receipt_text(
        azure.get("review_model"),
        "publication profile config",
        "azure_image.review_model",
        IMAGE_MAX_IDENTIFIER_CHARS,
        identifier=True,
    )
    snapshot = {
        "schema": PROFILE_SNAPSHOT_SCHEMA,
        "profile": AZURE_REVIEWED_PNG_PROFILE,
        "allowed_kinds": ["png"],
        "max_piece_bytes": max_piece_bytes,
        "provider": IMAGE_PROVIDER,
        "deployments": deployments,
        "max_attempts": attempts,
        "review_schema": IMAGE_REVIEW_SCHEMA,
        "review_model": model,
        "minimum_review_score": minimum,
        "rapp_vision_enabled": True,
    }
    return validate_profile_snapshot(snapshot)


def enforce_reviewed_controller_contract(wcfg, profile_snapshot,
                                         configured=None):
    """Apply the pinned Collective identity or reject an explicit conflict."""
    if not _requires_collective_provenance(profile_snapshot):
        return None
    configured = configured if isinstance(configured, dict) else None
    expected = {
        "branch_prefix": REVIEWED_PNG_BRANCH_PREFIX,
        "git_author_name": REVIEWED_PNG_COMMIT_NAME,
        "git_author_email": REVIEWED_PNG_COMMIT_EMAIL,
    }
    for field, required in expected.items():
        if configured is not None:
            if field in configured and configured[field] != required:
                raise GateError(
                    f"{AZURE_REVIEWED_PNG_PROFILE} requires {field}="
                    f"{required!r}; configured override "
                    f"{configured[field]!r} is incompatible")
        else:
            actual = wcfg.get(field)
            if actual not in (WORKER_DEFAULTS[field], required):
                raise GateError(
                    f"{AZURE_REVIEWED_PNG_PROFILE} requires {field}="
                    f"{required!r}; value {actual!r} is incompatible")
        wcfg[field] = required
    return dict(expected)


VISUAL_CHILD_RULE = (
    "This publication profile is visual-only. Propose and critique concrete "
    "image concepts for a finished PNG: composition, spatial relationships, "
    "palette, light, texture, focal hierarchy, and visible failure modes. "
    "Never propose SVG, markdown, text, JSON, code, an interactive experience, "
    "or a nonvisual fallback."
)

VISUAL_DEFAULT_ROLES = [
    {
        "name": "novelty-archaeologist",
        "wave": 1,
        "brief": (
            "Compare the visible concept, composition, symbolism, and palette "
            "against prior work. Veto image concepts that merely repaint an "
            "existing premise."
        ),
    },
    {
        "name": "image-concept-designer",
        "wave": 1,
        "brief": (
            "Design image concepts that can be expressed as one finished "
            "generated PNG. State the composition, focal subject, spatial "
            "logic, medium, palette, lighting, texture, and exclusions."
        ),
    },
    {
        "name": "adversarial-verifier",
        "wave": 2,
        "verifier": True,
        "brief": (
            "Attack the proposed images for generic composition, incoherent "
            "geometry, accidental text, generation artifacts, weak hierarchy, "
            "and concepts that are not legible from pixels alone."
        ),
    },
]


def profiled_fanout_config(wcfg, profile_snapshot=None):
    """Apply visual-only child roles while leaving every legacy cast intact."""
    fcfg = SS.fanout_config(wcfg)
    snapshot = (validate_profile_snapshot(profile_snapshot)
                if profile_snapshot is not None
                else publication_profile_snapshot(wcfg))
    if not snapshot:
        return fcfg
    block = wcfg.get("fanout")
    declared = block.get("roles") if isinstance(block, dict) else None
    source = declared or VISUAL_DEFAULT_ROLES
    roles = []
    for role in source:
        if not isinstance(role, dict):
            roles.append(role)
            continue
        brief = str(role.get("brief") or "").strip()
        roles.append({
            **role,
            "brief": f"{VISUAL_CHILD_RULE} {brief}".strip(),
        })
    fcfg["roles"] = roles
    fcfg["_publication_profile"] = AZURE_REVIEWED_PNG_PROFILE
    fcfg["_prompt_constraint"] = VISUAL_CHILD_RULE
    return fcfg


def vision_worker_config(wcfg, vcfg=None):
    vcfg = vcfg or vision_config(wcfg)
    merged = dict(wcfg)
    merged.update({
        "repo": vcfg["repo"],
        "base_branch": vcfg.get("base_branch", "main"),
        "branch_prefix": vcfg.get("branch_prefix", "art/dada-vision"),
    })
    return merged


def vision_media_path(submission, vcfg):
    extension = Path(submission["piece_path"]).suffix.lower()
    return (PurePosixPath(vcfg["media_dir"]) /
            f"{submission['slug']}{extension}").as_posix()


def _safe_channel_relative(path, channel_path, label):
    parent = PurePosixPath(channel_path).parent.as_posix()
    relative = posixpath.relpath(path, parent if parent != "." else ".")
    parsed = PurePosixPath(relative)
    if (parsed.is_absolute() or ".." in parsed.parts
            or any(not part or part.startswith(".") for part in parsed.parts)):
        raise GateError(
            f"{label} cannot be represented as a safe channel-relative path")
    return parsed.as_posix()


def _vision_app_url(vcfg, slug):
    """Build the channel app field from a clean HTTPS viewer base."""
    if not isinstance(slug, str) or not SLUG_RE.fullmatch(slug):
        raise GateError("RAPP Vision app slug must be a lowercase slug")
    base = str(vcfg.get("collective_viewer_url") or "")
    parsed = urlsplit(base)
    if (parsed.scheme != "https" or not parsed.netloc
            or parsed.username is not None or parsed.password is not None
            or parsed.query or parsed.fragment
            or any(char.isspace() or ord(char) < 32 for char in base)):
        raise GateError(
            "rapp_vision.collective_viewer_url must be a clean HTTPS URL "
            "without credentials, query, or fragment")
    return f"{base}#/{quote(slug, safe='')}"


def validate_reviewed_vision_config(vcfg):
    """Prove reviewed-PNG channel paths are non-colliding and representable."""
    if not isinstance(vcfg, dict) or vcfg.get("enabled") is not True:
        raise GateError(
            f"{AZURE_REVIEWED_PNG_PROFILE} requires enabled RAPP Vision")
    channel = PurePosixPath(vcfg["channel_path"])
    media = PurePosixPath(vcfg["media_dir"])
    registry_raw = vcfg.get("registry_path")
    registry = PurePosixPath(registry_raw) if registry_raw else None

    if registry is None:
        raise GateError(
            f"{AZURE_REVIEWED_PNG_PROFILE} requires rapp_vision.registry_path")
    if channel == media or media in channel.parents:
        raise GateError(
            "rapp_vision.channel_path must not be inside media_dir")
    if registry == channel:
        raise GateError(
            "rapp_vision.registry_path must differ from channel_path")
    if registry == media or media in registry.parents:
        raise GateError(
            "rapp_vision.registry_path must not be inside media_dir")
    _safe_channel_relative(
        channel.as_posix(), registry.as_posix(),
        "RAPP Vision registry channel")

    _safe_channel_relative(
        (media / "profile-preflight.png").as_posix(),
        channel.as_posix(), "RAPP Vision thumb")
    _vision_app_url(vcfg, "profile-preflight")
    return vcfg


def vision_video(submission, vcfg, image_info=None):
    media = vision_media_path(submission, vcfg)
    receipt = ((submission.get("meta") or {}).get("_image_generation")
               if isinstance(submission.get("meta"), dict) else None)
    reviewed_png = (
        submission.get("kind") == "png"
        and isinstance(receipt, dict)
        and receipt.get("profile") == AZURE_REVIEWED_PNG_PROFILE)
    if reviewed_png:
        thumb = _safe_channel_relative(
            media, vcfg["channel_path"], "RAPP Vision thumb")
    else:
        channel_parent = PurePosixPath(vcfg["channel_path"]).parent.as_posix()
        thumb = posixpath.relpath(
            media, channel_parent if channel_parent != "." else "")
    submitted = str(submission["meta"].get("submitted_at") or "")
    try:
        published = datetime.fromisoformat(
            submitted.replace("Z", "+00:00")).strftime("%Y-%m-%d")
    except ValueError as e:
        raise GateError(f"submitted_at is not ISO-8601: {e}")
    concept = concept_sentence(submission["meta"])
    duration = int(vcfg["duration"])
    if reviewed_png:
        if not isinstance(image_info, dict):
            raise GateError(
                "RAPP Vision PNG metadata must be derived from validated IHDR")
        width, height = image_info["width"], image_info["height"]
        orientation = (
            "landscape" if width > height
            else "portrait" if height > width
            else "square")
    else:
        width, height, orientation = 1280, 800, "landscape"
    return {
        "id": submission["slug"],
        "title": submission["title"],
        "description": concept,
        "published": published,
        "duration": duration,
        "width": width,
        "height": height,
        "orientation": orientation,
        "tags": ["dada-collective", "public-art", "cc0"],
        "thumb": thumb,
        "sources": [],
        "live": {
            "kind": "rapp-vision-live/1.0",
            "scenes": [
                {
                    "t": 0,
                    "dur": 6,
                    "card": {
                        "title": submission["title"],
                        "sub": "Dada Collective / Public Art Collective",
                        "note": "CC0-1.0 - one finished autonomous artwork",
                    },
                },
                {
                    "t": 6,
                    "dur": duration - 6,
                    "app": _vision_app_url(vcfg, submission["slug"]),
                    "lower": {
                        "title": submission["title"],
                        "bench": "Public Art Collective",
                        "fix": concept[:180],
                    },
                    "actions": [],
                },
            ],
        },
    }


def vision_channel(vcfg):
    repo = normalize_repo(vcfg["repo"], vcfg)
    source = (f"https://github.com/{repo.owner}/{repo.name}"
              if repo.owner and repo.name else str(vcfg["repo"]))
    return {
        "schema": VISION_CHANNEL_SCHEMA,
        "id": vcfg["channel_id"],
        "name": vcfg["channel_name"],
        "handle": "@kody-w",
        "tagline": "Final CC0 works from the Dada Collective, deployed only after verification.",
        "avatar": "\U0001F3A8",
        "links": [
            {"label": "Public Art Collective",
             "url": "https://kody-w.github.io/public-art-collective/"},
            {"label": "Source repo", "url": source},
        ],
        "videos": [],
    }


def vision_registry_entry(vcfg):
    repo = normalize_repo(vcfg["repo"], vcfg)
    source = (f"https://github.com/{repo.owner}/{repo.name}"
              if repo.owner and repo.name else str(vcfg["repo"]))
    return {
        "id": vcfg["channel_id"],
        "name": vcfg["channel_name"],
        "url": _safe_channel_relative(
            vcfg["channel_path"], vcfg["registry_path"],
            "RAPP Vision registry channel"),
        "repo": source,
        "_why": "Verified Dada works mirrored from the Public Art Collective.",
    }


def _load_json_file(path, label):
    try:
        value = json.loads(Path(path).read_text(encoding="utf-8"))
    except FileNotFoundError:
        return None
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as e:
        raise GateError(f"{label} is unreadable: {type(e).__name__}: {e}")
    if not isinstance(value, dict):
        raise GateError(f"{label} must hold a JSON object")
    return value


def write_vision_files(clone, submission, piece_bytes, vcfg):
    """Materialize one idempotent RAPP Vision entry in a controller clone."""
    clone = Path(clone)
    image_info = (_check_png(piece_bytes)
                  if submission.get("kind") == "png" else None)
    entry = vision_video(submission, vcfg, image_info)
    changed = []

    media_rel = vision_media_path(submission, vcfg)
    media_path = clone / media_rel
    if media_path.exists():
        if media_path.read_bytes() != piece_bytes:
            raise GateError(f"RAPP Vision media id {submission['slug']!r} "
                            "already exists with different bytes")
    else:
        media_path.parent.mkdir(parents=True, exist_ok=True)
        media_path.write_bytes(piece_bytes)
        changed.append(media_rel)

    channel_rel = vcfg["channel_path"]
    channel_path = clone / channel_rel
    channel = _load_json_file(channel_path, channel_rel)
    if channel is None:
        channel = vision_channel(vcfg)
    if channel.get("schema") != VISION_CHANNEL_SCHEMA:
        raise GateError(f"{channel_rel} has schema {channel.get('schema')!r}")
    if channel.get("id") != vcfg["channel_id"]:
        raise GateError(f"{channel_rel} has channel id {channel.get('id')!r}")
    videos = channel.get("videos")
    if not isinstance(videos, list):
        raise GateError(f"{channel_rel}.videos is not a list")
    existing = next((video for video in videos
                     if isinstance(video, dict)
                     and video.get("id") == submission["slug"]), None)
    if existing is not None and existing != entry:
        raise GateError(f"RAPP Vision entry {submission['slug']!r} "
                        "already exists with different metadata")
    if existing is None:
        videos.append(entry)
        channel_path.parent.mkdir(parents=True, exist_ok=True)
        channel_path.write_text(
            json.dumps(channel, indent=2, ensure_ascii=False) + "\n",
            encoding="utf-8")
        changed.append(channel_rel)

    registry_rel = vcfg.get("registry_path")
    if registry_rel:
        registry_path = clone / registry_rel
        registry = _load_json_file(registry_path, registry_rel)
        if registry is None or registry.get("schema") != VISION_NETWORK_SCHEMA:
            raise GateError(f"{registry_rel} is not a {VISION_NETWORK_SCHEMA}")
        channels = registry.get("channels")
        if not isinstance(channels, list):
            raise GateError(f"{registry_rel}.channels is not a list")
        wanted = vision_registry_entry(vcfg)
        registered = next((channel for channel in channels
                           if isinstance(channel, dict)
                           and channel.get("id") == vcfg["channel_id"]), None)
        if registered is not None and any(
                registered.get(key) != wanted[key]
                for key in ("name", "url", "repo")):
            raise GateError(f"registry id {vcfg['channel_id']!r} points elsewhere")
        if registered is None:
            channels.append(wanted)
            revision = registry.setdefault("revision", {})
            revision["sequence"] = int(revision.get("sequence") or 0) + 1
            revision["updated"] = f"{sentinel.now():%Y-%m-%dT%H:%M:%SZ}"
            registry_path.write_text(
                json.dumps(registry, indent=2, ensure_ascii=False) + "\n",
                encoding="utf-8")
            changed.append(registry_rel)

    return entry, sorted(changed)


# ── durable state: strict on the way in, atomic on the way out ──────────────

def strict_load(path, default, expect=(dict, list)):
    """Load durable state, or refuse to run.

    sentinel.load_json answers "unreadable" with the default, which is right
    for caches and catastrophic for spend ledgers: a truncated
    evolve-worker-history.json would read as "no evolutions today" and hand
    the day's budget back every 30 minutes, forever. Absent is zero; corrupt
    is unknown; unknown must stop the run (#2).
    """
    p = Path(path)
    try:
        raw = p.read_text(encoding="utf-8")
    except FileNotFoundError:
        return default
    except OSError as e:
        raise LedgerError(f"{p.name} is unreadable ({type(e).__name__}: {e})")
    if not raw.strip():
        raise LedgerError(f"{p.name} is empty — a truncated ledger is not 'no spend'")
    try:
        data = json.loads(raw)
    except (json.JSONDecodeError, UnicodeDecodeError) as e:
        raise LedgerError(f"{p.name} is corrupt ({e}) — refusing to reset spend")
    if not isinstance(data, expect):
        raise LedgerError(f"{p.name} holds a {type(data).__name__}, not the expected shape")
    return data


def atomic_write_json(path, data):
    """Replace a ledger in one step, durably.

    A half-written history is a corrupt history, and a corrupt history stops
    the worker (see strict_load) — so the write path has to be all-or-nothing
    even across a power cut: temp file in the same directory, fsync, rename,
    fsync the directory.
    """
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    payload = json.dumps(data, indent=2, ensure_ascii=False) + "\n"
    try:
        with open(tmp, "w", encoding="utf-8") as fh:
            fh.write(payload)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, path)
        dfd = os.open(str(path.parent), os.O_RDONLY)
        try:
            os.fsync(dfd)
        finally:
            os.close(dfd)
    finally:
        if tmp.exists():
            try:
                tmp.unlink()
            except OSError:
                pass


def load_history(path=None):
    rows = strict_load(path or HISTORY_PATH, [], expect=list)
    for row in rows:
        if not isinstance(row, dict) or "at" not in row:
            raise LedgerError("history holds a row without a timestamp — "
                              "refusing to guess how much has been spent")
        try:
            datetime.fromisoformat(str(row["at"]))
        except ValueError:
            raise LedgerError(f"history row has an unparseable timestamp "
                              f"({row['at']!r}) — refusing to reset spend")
    return rows


def save_history(rows, path=None):
    atomic_write_json(path or HISTORY_PATH, rows[-HISTORY_KEEP:])


# ── guardrails ──────────────────────────────────────────────────────────────

def acquire_lock(path=None):
    """Nonblocking exclusive lock. Returns an fd, or None if held elsewhere.

    flock, not a pid file: the kernel releases it when the process dies, so a
    worker killed mid-run (launchd ceiling, reboot, SIGKILL) cannot leave a
    stale lock that wedges the art arm until someone notices.
    """
    path = Path(path or LOCK_PATH)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(str(path), os.O_CREAT | os.O_RDWR, 0o600)
    if not filelock.lock_nb(fd):
        os.close(fd)
        return None
    try:
        os.ftruncate(fd, 0)
        os.write(fd, json.dumps({
            "pid": os.getpid(),
            "at": sentinel.now().isoformat(timespec="seconds"),
        }).encode("utf-8") + b"\n")
        os.fsync(fd)
    except OSError:
        pass
    return fd


def release_lock(fd):
    if fd is None:
        return
    filelock.unlock(fd)
    try:
        os.close(fd)
    except OSError:
        pass


def spend_rows(history, hours=24):
    """Rows that consumed a model in the window. Skips never count (#50)."""
    cutoff = sentinel.now() - timedelta(hours=hours)
    out = []
    for row in history:
        if row.get("skipped"):
            continue
        if row.get("mode") != "evolve":
            continue
        if datetime.fromisoformat(str(row["at"])) > cutoff:
            out.append(row)
    return out


def within_budget(history, wcfg):
    cap = int(wcfg.get("daily_budget", 2))
    used = len(spend_rows(history))
    return used < cap, used, cap


def cadence_ready(history, wcfg):
    """One global creative cadence across every role, like the in-tick arm."""
    interval_h = max(0.0, float(wcfg.get("interval_hours", 4)))
    latest = None
    for row in history:
        if row.get("skipped") or row.get("mode") != "evolve":
            continue
        stamp = datetime.fromisoformat(str(row["at"]))
        latest = stamp if latest is None or stamp > latest else latest
    if latest is None:
        return True, "first evolution"
    age_h = (sentinel.now() - latest).total_seconds() / 3600
    if age_h < interval_h:
        return False, f"creative cadence ({age_h:.1f}h of {interval_h:.1f}h)"
    return True, f"creative cadence ready ({age_h:.1f}h)"


# Checks a degraded_allowlist may never silence. Everything else about the
# allowlist is an operator's judgement call; these two are not, because both
# mean "this loop cannot see or cannot speak", and art made while the alarm
# is broken is exactly the silence this repo exists to refuse:
#
#   alert_delivery  queued, unverified or dead-lettered alerts — the estate
#                   cannot reach a human. Since 232ce7e an unverifiable send
#                   is an explicit failure rather than an optimistic success,
#                   so this check now says out loud what used to be lost. Art
#                   published while it is red would be a system cheerfully
#                   texting about paintings from a channel that cannot text.
#   health_runtime  the health run did not finish. The verdict is unknown,
#                   and "unknown" must never be allowlisted into "fine".
NEVER_ALLOWLISTABLE = frozenset({"alert_delivery", "health_runtime"})

# health.py emits exactly these three. Anything else is a verdict this worker
# cannot reason about, and an unreadable verdict is never a green light.
KNOWN_STATUSES = frozenset({"healthy", "degraded", "critical"})


def health_gate(wcfg, verdict, phase="start"):
    """Health decides whether art may proceed. Returns (ok, reason).

    Critical is always a stop. Degraded is a stop UNLESS every failing id is
    named in `degraded_allowlist` — an explicit list of known-noisy checks,
    not `evolve_on_degraded`, which said "any degradation is fine" and would
    have let this worker push art through an estate that was quietly on fire
    in a way nobody had looked at yet (#3). A small set of ids refuses to be
    allowlisted at all (NEVER_ALLOWLISTABLE).
    """
    # A verdict is only evidence if it has the shape of a verdict. Reading
    # missing keys as empty defaults meant "{}" — a crashed health run, a
    # truncated json read, a mock nobody finished — scored as "healthy, no
    # criticals, nothing failing" and unlocked the model (#8).
    if not isinstance(verdict, dict):
        return False, f"health at {phase} returned {type(verdict).__name__}, not a verdict"
    missing = [k for k in ("status", "failed", "critical") if k not in verdict]
    if missing:
        return False, (f"health verdict at {phase} is missing "
                       f"{', '.join(missing)} — shape unknown, not healthy")
    status = verdict.get("status")
    if status not in KNOWN_STATUSES:
        return False, (f"health verdict at {phase} reports status "
                       f"{status!r}, which this worker does not understand")
    if not isinstance(verdict.get("failed"), list) or not isinstance(
            verdict.get("critical"), list):
        return False, (f"health verdict at {phase} has non-list failed/critical "
                       f"fields — shape unknown, not healthy")
    critical = list(verdict.get("critical") or [])
    if critical:
        return False, f"critical checks failing at {phase}: {', '.join(sorted(critical))}"
    failing = list(verdict.get("failed") or [])
    if status != "healthy" and not failing:
        return False, (f"health verdict at {phase} says {status!r} but names no "
                       f"failing check — the two disagree, so neither is trusted")
    if not failing:
        return True, f"healthy at {phase}"
    unskippable = sorted(set(failing) & NEVER_ALLOWLISTABLE)
    if unskippable:
        return False, (f"{', '.join(unskippable)} failing at {phase} and cannot "
                       f"be allowlisted — a loop that cannot report must not "
                       f"publish")
    allowed = {str(x) for x in (wcfg.get("degraded_allowlist") or [])}
    unlisted = sorted(set(failing) - allowed)
    if unlisted:
        return False, (f"degraded at {phase} with unlisted failing checks: "
                       f"{', '.join(unlisted)}")
    return True, (f"degraded at {phase}, every failing check allowlisted: "
                  f"{', '.join(sorted(failing))}")


# ── the prompt ──────────────────────────────────────────────────────────────

WORKER_SITUATION = """
You are a neighbor in a local twin neighborhood, acting on your own initiative.

This is NOT a task assignment. Nobody has told you what to make or whether to
make anything. You are being handed your situation and your boundaries, and the
discretion to decide what — if anything — you do with them. Declining is a
legitimate outcome and will be recorded as such.

WHO YOU ARE
  collective: {instance_name}
  neighbor:   {slug}
  role:       {role}
  rappid:     {rappid}

WHERE YOU ARE
  neighborhood: {nb_name}
  purpose:      {nb_purpose}
  your peers:   {peers}

The neighborhood is healthy enough to make things right now. That is the only
reason you were woken.

YOUR WORKSPACE — THE ONLY PLACE YOU MAY WRITE
  {workspace}/out/submission/  where your two submission files go (it exists)
  {workspace}/context/       what already exists, read-only to you in practice
  {workspace}/state-in.json  your private creative state from last cycle
                             ({state_in_note})
  {workspace}/state-out.json the private next state you must write

There is NO repository here. You have no clone of {repo}, no .git directory,
no git and no gh — by construction, not by instruction. Your file tools are
rooted at {workspace} and reach nothing else on this machine.

YOU MAY NOT PUBLISH. THIS IS ABSOLUTE.
You have no shell, no git, no gh and no network tool, so there is nothing to
run — and nothing you write can become a commit, a branch, a remote, a pull
request or a merge. Leave only the bounded submission inputs and private state
listed below; that is the whole job.

A controller — code, not a model — reads what you leave behind, checks it
against the submission protocol deterministically, turns the two submission
inputs into the two publishable files inside a clone you never see, and only
then creates the branch, the commit, the pull request and the merge.
{controller_action}

WHAT TO LEAVE BEHIND — THREE FILES, IN PATHS THAT ALREADY EXIST
You cannot create directories: you have file tools and no shell, and that is
deliberate. Every directory you need has been made for you. Write exactly:

  {workspace}/out/submission/meta.json      the protocol record (schema below)
  {piece_instruction}
  {workspace}/state-out.json                your private next state

Your slug goes in meta.json, NOT in a directory name — the controller creates
submissions/<your-slug>/ in its own clone from that field. Do not attempt to
create any directory, and do not leave anything else behind: a stray file, a
draft, or a hidden file like .probe fails the whole cycle.

The controller validates that directory, reads your slug from meta.json,
accepts exactly those two submission inputs, and refuses everything else.

meta.json:
{{
  "schema":       "{schema}",
  "title":        "<human title>",
  "slug":         "<your-slug>",
  "contributor":  "{contributor}",
  "kind":         "{allowed_kind_union}",
  "submitted_at": "<UTC ISO-8601, e.g. 2026-08-17T19:00:00Z>",
  "remix_of":     null or "<existing slug>",
  "license":      "{license}",
  "_dada_cycle":  {{ ... see below ... }}
}}

{azure_instruction}

Slug rules: lowercase letters, digits and single hyphens, at most {slug_max}
characters, and it must not already exist under submissions/.

Free-form keys are allowed ONLY with a leading underscore (e.g.
"_artist_statement", "_authored_by"). Any other extra key is a rejection.

THE CYCLE YOU MUST ACTUALLY RUN — meta._dada_cycle
Search first, build once. Run between {min_rounds} and {max_rounds} rounds. In
EVERY round produce EXACTLY {candidates} distinct candidate premises and score
each on all six dimensions ({dimensions}), each a number from {score_min} to
{score_max}. Pick one winner per round; the final round's winner is the piece
you actually build. {round_one}

{fanout}

{{
  "cycle": {cycle},
  "previous_slug": {previous_slug},
  "rounds": [
    {{"round": 1,
      "candidates": [
        {{"id": "r1c1", "premise": "...",
          "scores": {{{score_example}}}}}
        ... exactly {candidates} of them, unique ids ...
      ],
      "selected": "r1c4"}}
    ... rounds numbered 1..N, contiguous ...
  ],
  "winner": {{"round": <the last round number>,
             "candidate": "<that round's selected id>",
             "slug": "<your-slug>"}}
}}

"cycle" MUST be exactly {cycle} and "previous_slug" MUST be exactly
{previous_slug}. Those two values are how the controller proves this cycle
continues the last one rather than restarting the count.

{format_rules}

{workspace}/state-out.json (private, never published):
{{
  "cycle": {cycle},
  "last_slug": "<your-slug>",
  "notes": "<what you learned, bounded — this is the input to your next cycle>"
}}
Write it even if you decline; on a decline set "last_slug" to null.

YOUR OWN MEMORY
Your rapp/1 frame chain is at {chain_path}. Read it if you want; it is yours.

RECENT CYCLES (what this worker actually recorded)
{recent}

STANDING DIRECTIVE
{brief}

Decide for yourself. Then act, end to end, without checking back.

Finish with a single line starting exactly `SENTINEL_RESULT:` followed by
CONTRIBUTED, DECLINED or BLOCKED, then one sentence on what you decided and why.
"""


def build_prompt(cfg, wcfg, slug, workspace, expected_cycle,
                 expected_previous,                  history, finalists=None, digest=None):
    ids = NB.identities()
    roll = NB.roll_call()
    recent = [
        f"  - {row.get('at')} {row.get('role')}: {row.get('outcome')} "
        f"({str(row.get('result'))[:120]})"
        for row in history[-5:]
    ]
    state_in = Path(workspace) / "state-in.json"
    example = ", ".join(f'"{d}": 7' for d in SCORE_DIMENSIONS)
    if finalists:
        # The fan-out already ran the first round, in separate contexts, and
        # its ten survivors are binding: the gate checks round one against
        # these exact ids, so the piece cannot quietly ignore the deliberation
        # that justified it.
        fanout_block = (
            SS.finalists_block(finalists, digest or {})
            + "\n\nYour sub-sentinels are finished and cannot be consulted "
              "again. Do not start any further agents; you are the maker.\n")
        round_one = ('Round 1 MUST be exactly the ten finalist RECORDS in '
                     'round1.json — every field, copied verbatim, including '
                     '"from", "rationale", "evidence_digest" and all six '
                     'scores. Later rounds (up to 5) are yours.')
    else:
        fanout_block = ""
        round_one = ("Round 1 is yours to populate; every round needs exactly "
                     f"{CANDIDATES_PER_ROUND} candidates.")
    kinds = allowed_kinds(wcfg)
    profile_snapshot = publication_profile_snapshot(wcfg)
    if profile_snapshot:
        piece_instruction = (
            f"{workspace}/out/submission/piece.prompt  a detailed 1-4000 "
            "character visual image brief; this is the ONLY piece file you "
            "write, and the controller replaces it with reviewed pixels"
        )
        azure_instruction = (
            f'This is the {AZURE_REVIEWED_PNG_PROFILE} profile. Every candidate, '
            "critique, round winner, and final premise MUST be a visual image "
            "concept whose meaning is carried by pixels. There is no SVG, "
            "markdown, text, JSON, code, interactive, or nonvisual fallback. "
            'meta.json MUST include "_image_prompt" and its '
            "value MUST exactly match piece.prompt. Describe the composition, "
            "medium, palette, lighting, focal hierarchy, texture, and what to "
            "avoid. Do not put labels or prose into the image unless the concept "
            "requires a small amount of intentional, correctly spelled text. "
            "Azure generates the pixels and an independent multimodal Copilot "
            "judge sees the actual image; failed images are regenerated or the "
            "cycle is rejected. NEVER write piece.png yourself and NEVER add "
            '"_image_generation" to meta.json; those bytes and that receipt are '
            "created only by the trusted controller."
        )
        controller_action = (
            "For this profile the controller does not copy your piece.prompt "
            "into the repository. It validates the brief, generates a PNG with "
            "Azure, validates the complete PNG structure and pixels, obtains an "
            "approved multimodal review, then writes a digest-bound receipt. "
            "Only that reviewed PNG can reach publication."
        )
        format_rules = (
            "Your output is a visual concept brief, not an image file. Write "
            "meta.json, piece.prompt, and state-out.json only. Direct PNG, SVG, "
            "markdown, text, or JSON piece files fail the cycle; generation or "
            "review failure has no fallback."
        )
    else:
        piece_instruction = (
            f"{workspace}/out/submission/piece.<ext>    the work itself; ext is "
            f"one of {' '.join(KIND_EXTENSIONS[kind] for kind in kinds)} and "
            f"MUST match meta.kind; at most "
            f"{int(wcfg['max_piece_bytes']) // 1024} KB"
        )
        azure_instruction = ""
        controller_action = ""
        format_rules = (
            "If your piece is an SVG it must parse as XML and contain no "
            "<script>, no on* event attributes, and no external references — "
            "fragment (#id) references only. Everything must be self-contained."
        )
    return WORKER_SITUATION.format(
        instance_name=sentinel.instance_name(cfg),
        slug=slug,
        role=NB.NEIGHBORS[slug],
        rappid=ids[slug],
        nb_name=NB.NEIGHBORHOOD["name"],
        nb_purpose=NB.NEIGHBORHOOD["purpose"],
        peers=", ".join(f"{k} ({'alive' if v['alive'] else 'stale'})"
                        for k, v in roll.items() if k != slug),
        workspace=str(workspace),
        repo=wcfg["repo"],
        state_in_note="present" if state_in.exists() else "absent — this is cycle 1",
        max_piece_kb=int(wcfg["max_piece_bytes"]) // 1024,
        piece_instruction=piece_instruction,
        azure_instruction=azure_instruction,
        controller_action=controller_action,
        format_rules=format_rules,
        allowed_kind_union="|".join(allowed_kinds(wcfg)),
        schema=SUBMISSION_SCHEMA,
        contributor=wcfg.get("contributor") or _repo_owner(wcfg["repo"]),
        license=ALLOWED_LICENSES[0],
        slug_max=SLUG_MAX,
        min_rounds=MIN_ROUNDS,
        max_rounds=MAX_ROUNDS,
        candidates=CANDIDATES_PER_ROUND,
        dimensions=", ".join(SCORE_DIMENSIONS),
        score_min=SCORE_MIN,
        score_max=SCORE_MAX,
        score_example=example,
        cycle=expected_cycle,
        previous_slug=json.dumps(expected_previous),
        chain_path=str(NB.chain_path(slug)),
        recent="\n".join(recent) or "  (none yet)",
        fanout=fanout_block,
        round_one=round_one,
        brief=sentinel.evolve_brief(cfg) or
        "No additional standing directive. Decide from your role and memory.",
    )


def _repo_owner(repo):
    return str(repo).split("/")[0] if "/" in str(repo) else str(repo)


# ── subprocess seams (patched in tests) ─────────────────────────────────────

# The controller's git environment is an ALLOWLIST, not a denylist.
#
# A denylist has to be complete to be correct, and it was not: GIT_EXEC_PATH
# survived, which decides where git finds git-remote-https and git-upload-pack
# — so a hostile value replaces the TRANSPORT itself, before any config is
# read and long before any integrity check runs. (Reproduced: a fake
# git-upload-pack on GIT_EXEC_PATH executes during a plain local clone.) The
# same is true of LD_PRELOAD, DYLD_INSERT_LIBRARIES, GIT_TEMPLATE_DIR and
# whatever the next release adds. So nothing is inherited unless it is named
# here, and everything else the controller sets itself.
# PATH is deliberately absent: it is SET, never inherited (see trusted_path).
GIT_ENV_ALLOWLIST = ("LANG", "LC_ALL", "LC_CTYPE")

# Optional, and only when they point at something that exists: some builds
# need them to verify TLS, and a broken cert path is a confusing outage.
GIT_ENV_CERT_VARS = ("SSL_CERT_FILE", "SSL_CERT_DIR")

# Directories a hostile actor should not be able to write, and the only
# places the controller will accept a git binary from. /usr/bin/git exists on
# macOS and on Ubuntu CI; Homebrew's prefix is deliberately NOT here, because
# it is writable by the same user a compromised model process would run as.
TRUSTED_BIN_ROOTS = ("/usr/bin", "/bin", "/usr/sbin", "/sbin")

# PATH is not inherited at all. Git needs a PATH for its own helpers; it does
# not need the operator's, and "an absolute directory that exists" turned out
# to include an attacker's directory holding a fake `git`.
TRUSTED_PATH_DIRS = ("/usr/bin", "/bin", "/usr/sbin", "/sbin")

GIT_BINARY_CANDIDATES = ("/usr/bin/git", "/bin/git")

# Where gh is looked for, in order. A fixed list, not the ambient PATH: gh
# holds the GitHub credentials, so an attacker directory arriving early in
# PATH must not be able to answer for it. Package-manager prefixes are here
# because that is where a real gh lives; each candidate is still validated as
# an absolute, regular, non-group/world-writable executable.
GH_DISCOVERY_DIRS = ("/usr/bin", "/bin", "/usr/local/bin", "/opt/homebrew/bin",
                     "/home/linuxbrew/.linuxbrew/bin", "/opt/local/bin",
                     "/snap/bin")

# Only these protocols. `ext::` in particular runs a command of the URL's
# choosing, which is a remote that executes.
GIT_ALLOWED_PROTOCOLS = "https:file"

# No module-level caches. A cached binary or environment is a choice made in
# one pass leaking into the next — and, in tests, into the next test. Every
# pass builds one Controller and threads it through; callers without one get a
# context keyed by the inputs that could change it, so a patched PATH or HOME
# produces a different context rather than a stale answer.
_CTX_CACHE = {}


def _trusted_executable(path, roots, label):
    """An absolute, real, executable file under a root nobody else can write.

    resolve() first: a symlink under /usr/bin pointing at /tmp/git is a
    trusted path that is not a trusted binary.
    """
    candidate = Path(path)
    if not candidate.is_absolute():
        raise GateError(f"{label} {path!r} is not an absolute path")
    resolved = candidate.resolve()
    if not any(str(resolved).startswith(root.rstrip("/") + "/")
               for root in roots):
        raise GateError(f"{label} {resolved} is not under a trusted root "
                        f"({', '.join(roots)})")
    try:
        st = os.stat(resolved)
    except OSError as e:
        raise GateError(f"{label} {resolved} is unusable: {e}")
    if not stat.S_ISREG(st.st_mode):
        raise GateError(f"{label} {resolved} is not a regular file")
    if not os.access(resolved, os.X_OK):
        raise GateError(f"{label} {resolved} is not executable")
    if st.st_mode & (stat.S_IWGRP | stat.S_IWOTH):
        raise GateError(f"{label} {resolved} is group- or world-writable")
    return str(resolved)


def git_binary(wcfg=None):
    """The one git this controller will run, pinned independently of PATH.

    `_git` used to invoke a bare `git`, resolved through a PATH that kept any
    absolute existing directory — so an attacker's directory holding a fake
    `git` was consulted before /usr/bin. Everything downstream of that — the
    integrity check, the url validation, the environment allowlist — is
    reasoning about a binary somebody else chose.
    """
    configured = (wcfg or {}).get("git_binary")
    roots = tuple((wcfg or {}).get("trusted_bin_roots") or TRUSTED_BIN_ROOTS)
    if configured:
        return _trusted_executable(configured, roots, "git_binary")
    problems = []
    for candidate in GIT_BINARY_CANDIDATES:
        try:
            return _trusted_executable(candidate, roots, "git")
        except GateError as e:
            problems.append(str(e))
    raise GateError("no trusted git binary found: " + "; ".join(problems))


def gh_binary(wcfg=None):
    """The GitHub CLI, resolved once and validated.

    Softer than git: `gh` normally lives in a package-manager prefix that the
    operator owns, so requiring a system root would break every real install.
    It must still be an absolute, regular, executable file that is not
    group- or world-writable, and the choice is logged rather than assumed.
    """
    configured = (wcfg or {}).get("gh_binary")
    if configured:
        return _trusted_executable(configured, ("/",), "gh_binary")
    dirs = tuple((wcfg or {}).get("gh_discovery_dirs") or GH_DISCOVERY_DIRS)
    found = shutil.which("gh", path=os.pathsep.join(dirs))
    if not found:
        raise CommandError(f"no gh found in {', '.join(dirs)}; set "
                           f"evolve_worker.gh_binary to an absolute path")
    return _trusted_executable(found, ("/",), "gh")


def trusted_path(wcfg=None):
    """The PATH git runs with: trusted system directories, nothing inherited.

    Git needs a PATH for its own helpers (credential helpers, remote helpers
    it resolves by name). It does not need the operator's, and "an absolute
    directory that exists" included an attacker's directory holding a fake
    `git` — which is how a PATH sanitiser became an execution vector.
    """
    dirs = tuple((wcfg or {}).get("trusted_path_dirs") or TRUSTED_PATH_DIRS)
    keep = []
    for entry in dirs:
        if not str(entry).startswith("/"):
            raise GateError(f"trusted_path_dirs entry {entry!r} is not absolute")
        if os.path.isdir(entry):
            keep.append(str(entry))
    return os.pathsep.join(dict.fromkeys(keep)) or "/usr/bin:/bin"


def _minimal_env(source=None, wcfg=None):
    """Exactly the inherited variables the controller has decided to keep."""
    src = dict(source if source is not None else os.environ)
    env = {}
    for key in GIT_ENV_ALLOWLIST:
        if key in src and src[key]:
            env[key] = src[key]
    env["PATH"] = trusted_path(wcfg)
    for key in GIT_ENV_CERT_VARS:
        value = src.get(key)
        if value and value.startswith("/") and os.path.exists(value):
            env[key] = value
    return env


# The operator's real home, from the password database rather than $HOME, so
# isolating HOME later cannot move it.
REAL_HOME = Path(pwd.getpwuid(os.getuid()).pw_dir)


def real_gh_config_dir(wcfg=None):
    """The operator's gh configuration directory, validated.

    gh keeps the account in hosts.yml and the token in the login keyring, so
    the credential helper needs this directory — and, on macOS, the real HOME
    the keyring lives under (measured: with an isolated HOME, `gh auth token`
    reports "no oauth token found"). Both are handed to the HELPER PROCESS
    only, and only after checking: absolute, existing, a directory, under the
    real home, not writable by group or other.
    """
    configured = (wcfg or {}).get("gh_config_dir")
    candidate = Path(configured) if configured else (REAL_HOME / ".config" / "gh")
    if not candidate.is_absolute():
        raise GateError(f"gh_config_dir {candidate} is not absolute")
    resolved = candidate.resolve()
    if not str(resolved).startswith(str(REAL_HOME.resolve()) + os.sep):
        raise GateError(f"gh_config_dir {resolved} is not under {REAL_HOME}")
    if not resolved.is_dir():
        raise GateError(f"gh_config_dir {resolved} does not exist")
    st = os.stat(resolved)
    if st.st_mode & (stat.S_IWGRP | stat.S_IWOTH):
        raise GateError(f"gh_config_dir {resolved} is group- or world-writable")
    return resolved


def credential_helper_path(wcfg=None, git_home=None):
    """Write the controller's OWN credential helper and return its path.

    Three live cycles produced finished art and failed at `git push` with
    "could not read Username": the sanitized global config carried no helper
    at all, because the previous design copied whatever `credential.helper`
    the operator had — which on this machine is nothing.

    So the helper is generated, never inherited, and it is one fixed command:
    the already-validated absolute gh binary, `auth git-credential`, with the
    real HOME and validated gh config directory supplied to that process
    alone. git keeps its isolated HOME and pinned config, and no model process
    ever sees either value.
    """
    gh = gh_binary(wcfg)
    config_dir = real_gh_config_dir(wcfg)
    home = Path(git_home) if git_home else (STATE / "git-home")
    home.mkdir(parents=True, exist_ok=True)
    helper = home / "gh-credential-helper"
    helper.write_text(
        "#!/bin/sh\n"
        "# Generated by evolve_worker. The controller's only credential path:\n"
        "# a fixed `gh auth git-credential`, with the operator's real HOME and\n"
        "# gh config supplied to this process alone.\n"
        f"HOME={shlex.quote(str(REAL_HOME))} \\\n"
        f"GH_CONFIG_DIR={shlex.quote(str(config_dir))} \\\n"
        f"exec {shlex.quote(str(gh))} auth git-credential \"$@\"\n",
        encoding="utf-8")
    helper.chmod(0o700)
    return helper


def controller_gh_env(wcfg=None):
    """The environment gh itself runs in: minimal, plus what the keyring needs."""
    env = _minimal_env(wcfg=wcfg)
    env["HOME"] = str(REAL_HOME)
    env["GH_CONFIG_DIR"] = str(real_gh_config_dir(wcfg))
    env["GH_PROMPT_DISABLED"] = "1"
    env["GH_NO_UPDATE_NOTIFIER"] = "1"
    return env


def controller_git_env(home=None, wcfg=None):
    """The environment EVERY controller git call runs in, built from nothing.

    Start empty. Carry locale and existing cert paths; set PATH to trusted
    system directories; set an isolated HOME, XDG and TMPDIR; write the only
    global config git will see, holding exactly one credential helper that
    THIS CODE generated (a fixed `gh auth git-credential`, never a borrowed
    string); disable system config; refuse prompts; allow only https and file.

    Nothing ambient reaches git: not GIT_EXEC_PATH, which chooses the
    transport binaries, not LD_PRELOAD, not GIT_CONFIG_PARAMETERS, not a
    proxy, not an ssh command.
    """
    env = _minimal_env(wcfg=wcfg)
    git_home = Path(home) if home else (STATE / "git-home")
    for path in (git_home, git_home / "xdg", git_home / "tmp"):
        path.mkdir(parents=True, exist_ok=True)
    config = git_home / "sanitized.gitconfig"
    helper_line = ""
    try:
        helper = credential_helper_path(wcfg, git_home)
        helper_line = ('[credential "https://github.com"]\n'
                       f"\thelper = {helper}\n")
    except (GateError, CommandError) as e:
        # No helper is better than a borrowed one: the push fails loudly and
        # the preflight explains why, instead of a mystery "could not read
        # Username" arriving after the art is already made.
        log(f"no controller credential helper available: {e}")
    config.write_text(
        "# written by evolve_worker: the ONLY global git config the\n"
        "# controller runs with. No includes, no url rewrites, no proxy,\n"
        "# no hooksPath, no alternates, and exactly one credential helper\n"
        "# that this code generated.\n"
        + helper_line,
        encoding="utf-8")

    env.update({
        "HOME": str(git_home),
        "XDG_CONFIG_HOME": str(git_home / "xdg"),
        "TMPDIR": str(git_home / "tmp"),
        "GIT_CONFIG_GLOBAL": str(config),
        "GIT_CONFIG_SYSTEM": os.devnull,
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_TERMINAL_PROMPT": "0",
        "GIT_ASKPASS": "/usr/bin/false",
        "GIT_ALLOW_PROTOCOL": GIT_ALLOWED_PROTOCOLS,
        "GIT_PROTOCOL_FROM_USER": "0",
    })
    return env


PUSH_PERMISSIONS = ("ADMIN", "MAINTAIN", "WRITE")


def assert_publish_auth(wcfg, timeout=None, ctx=None):
    """Prove, BEFORE spending a model, that this cycle could publish.

    Three live cycles made finished art and then died at `git push`. Every
    guard in this file was working; the one thing nobody checked was whether
    the controller could authenticate at all — and the cheapest possible
    moment to learn that is before the first child process, not after the
    last one.

    Two questions, both answered from outside this process:
      * does GitHub say this account may push to the configured repo?
      * does `git credential fill`, through the sanitized controller
        environment, actually produce a username and a password?

    Neither answer is logged. What gets recorded is that they existed.
    """
    ctx = ctx or controller_for(wcfg)
    if ctx.repo is None:
        raise AbortError("no repository is configured")
    if ctx.repo.is_local:
        return f"local repo {ctx.repo.transport} needs no credentials"

    gh_t = int(timeout or wcfg.get("gh_timeout_s", 300))
    try:
        view = json.loads(_gh("repo", "view", ctx.gh_repo(), "--json",
                              "viewerPermission", timeout=gh_t, ctx=ctx))
    except (CommandError, subprocess.TimeoutExpired, json.JSONDecodeError) as e:
        raise AbortError(f"cannot read push permission for {ctx.gh_repo()}: "
                         f"{type(e).__name__}: {str(e)[:160]}")
    permission = str(view.get("viewerPermission") or "").upper()
    if permission not in PUSH_PERMISSIONS:
        raise AbortError(f"this account's permission on {ctx.gh_repo()} is "
                         f"{permission or 'unknown'}; push needs one of "
                         f"{', '.join(PUSH_PERMISSIONS)}")

    request = f"protocol=https\nhost={ctx.repo.host}\n\n"
    try:
        proc = subprocess.run([ctx.git_path, "credential", "fill"],
                              input=request, capture_output=True, text=True,
                              timeout=gh_t, env=ctx.git_env)
    except subprocess.TimeoutExpired:
        raise AbortError("git credential fill timed out")
    if proc.returncode != 0:
        raise AbortError(f"git credential fill failed: "
                         f"{(proc.stderr or '').strip()[:160]}")
    fields = {}
    for line in (proc.stdout or "").splitlines():
        key, _, value = line.partition("=")
        fields[key.strip()] = value
    missing = [k for k in ("username", "password") if not fields.get(k)]
    if missing:
        raise AbortError(f"the credential helper returned no "
                         f"{', '.join(missing)} for {ctx.repo.host}")
    # Lengths, never values.
    return (f"push permission {permission}; credentials present "
            f"(username {len(fields['username'])} chars, password "
            f"{len(fields['password'])} chars)")


class RepoNames:
    """The configured repository, in the two shapes tools actually accept.

    They are not interchangeable, and mixing them is how a cycle passed its
    auth preflight and then died at `gh pr create`: git wants a transport —
    an https URL or a path — and gh wants `[HOST/]OWNER/REPO`. One
    normalisation, done once, so nothing downstream has to guess which shape
    it was handed.
    """

    __slots__ = ("transport", "gh", "owner", "name", "host", "is_local")

    def __init__(self, transport, gh, owner, name, host, is_local):
        self.transport, self.gh = transport, gh
        self.owner, self.name, self.host = owner, name, host
        self.is_local = is_local

    def __repr__(self):
        return f"RepoNames(transport={self.transport!r}, gh={self.gh!r})"


def normalize_repo(repo, wcfg=None):
    """Validate once, and return every form the rest of the pass may need."""
    transport = validate_repo_url(repo, wcfg)
    if not transport.startswith("https://"):
        return RepoNames(transport, None, None, None, None, True)
    parts = urlsplit(transport)
    segments = [s for s in parts.path.split("/") if s]
    owner, name = segments[0], segments[1]
    if name.endswith(".git"):
        name = name[:-4]
    host = parts.hostname
    gh_name = (f"{owner}/{name}" if host == "github.com"
               else f"{host}/{owner}/{name}")
    return RepoNames(transport, gh_name, owner, name, host, False)


class Controller:
    """One validated set of choices, for one whole pass.

    The pinned git and gh binaries, the sanitized git environment with its
    generated credential helper, the gh environment that may see the real
    HOME and gh config, and both repository forms — resolved once, together,
    and threaded through every call. Before this, a preflight could validate
    one gh binary and the merge could resolve another, or a fake config could
    win after the checks had already passed.
    """

    def __init__(self, wcfg=None, git_home=None):
        self.wcfg = dict(wcfg or {})
        self.git_path = git_binary(self.wcfg)
        self.repo = normalize_repo(self.wcfg["repo"], self.wcfg) \
            if self.wcfg.get("repo") else None
        self._gh_path = None
        self._gh_env = None
        self.git_home = Path(git_home) if git_home else (STATE / "git-home")
        self.git_env = controller_git_env(self.git_home, self.wcfg)

    @property
    def gh_path(self):
        if self._gh_path is None:
            self._gh_path = gh_binary(self.wcfg)
        return self._gh_path

    @property
    def gh_env(self):
        if self._gh_env is None:
            self._gh_env = controller_gh_env(self.wcfg)
        return self._gh_env

    def gh_repo(self):
        """The `[HOST/]OWNER/REPO` gh insists on.

        A local path has no such name; that only happens in tests, where gh
        is a stand-in anyway, so it keeps the configured value and a real gh
        would say plainly that it is not a repository.
        """
        if not self.repo:
            raise GateError("no repository is configured")
        return self.repo.gh or self.repo.transport


def controller_for(wcfg=None, git_home=None):
    """A context for this pass, cached only by what can change it."""
    if git_home is not None:
        return Controller(wcfg, git_home)
    key = (json.dumps({k: v for k, v in sorted((wcfg or {}).items())
                       if k in ("repo", "git_binary", "gh_binary",
                                "gh_config_dir", "trusted_bin_roots",
                                "trusted_path_dirs", "allowed_repo_hosts",
                                "base_branch")}, default=str),
           os.environ.get("PATH", ""), str(REAL_HOME))
    ctx = _CTX_CACHE.get(key)
    if ctx is None:
        ctx = Controller(wcfg, git_home)
        _CTX_CACHE[key] = ctx
    return ctx


def reset_controllers():
    """Drop every cached context. Tests and long-lived processes call this."""
    _CTX_CACHE.clear()


def _git(cwd, *args, timeout=600, check=True, env=None, ctx=None):
    ctx = ctx or controller_for()
    r = subprocess.run([ctx.git_path, *args], cwd=str(cwd),
                       capture_output=True, text=True, timeout=timeout,
                       env=env if env is not None else ctx.git_env)
    if check and r.returncode != 0:
        raise CommandError(f"git {' '.join(args)} exited {r.returncode}: "
                           f"{(r.stderr or r.stdout or '').strip()[:300]}")
    return r.stdout


def _git_bytes(cwd, *args, timeout=600, ctx=None):
    ctx = ctx or controller_for()
    r = subprocess.run([ctx.git_path, *args], cwd=str(cwd),
                       capture_output=True, timeout=timeout, env=ctx.git_env)
    if r.returncode != 0:
        raise CommandError(f"git {' '.join(args)} exited {r.returncode}")
    return r.stdout


LOCAL_REPO_RE = re.compile(r"^/[^\0]+$")
REPO_SEGMENT_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")


def validate_repo_url(repo, wcfg=None):
    """The canonical URL, checked for shape before it is ever handed to git.

    A URL is an instruction. `ext::sh -c ...` is a remote that executes,
    `-u./payload` is an argument pretending to be a host, and
    `https://user:token@host/` leaks a credential into every reflog that
    records it. An explicit absolute path is allowed — the tests use local
    bare repositories on purpose — but nothing implicit is.
    """
    raw = str(repo).strip()
    if raw.startswith("-"):
        raise GateError(f"repo {raw!r} starts with a dash — that is an "
                        f"argument, not a repository")
    url = _repo_url(repo)
    allowed_hosts = tuple((wcfg or {}).get("allowed_repo_hosts")
                          or ("github.com",))
    if url.startswith("-"):
        raise GateError(f"repo {url!r} starts with a dash — that is an argument, "
                        f"not a repository")
    if "\n" in url or "\r" in url:
        raise GateError("repo url contains a newline")
    lowered = url.lower()
    for scheme in ("ext::", "ssh://", "git://", "http://", "git+ssh://"):
        if lowered.startswith(scheme):
            raise GateError(f"repo url uses {scheme} — only https and explicit "
                            f"local paths are allowed")
    if lowered.startswith("https://"):
        parts = urlsplit(url)
        if "@" in parts.netloc:
            raise GateError("repo url embeds credentials")
        if parts.hostname not in allowed_hosts:
            raise GateError(f"repo host {parts.hostname!r} is not one of "
                            f"{', '.join(allowed_hosts)}")
        segments = [p for p in parts.path.split("/") if p]
        if len(segments) != 2:
            raise GateError(f"repo path {parts.path!r} is not owner/name")
        for segment in segments:
            if not REPO_SEGMENT_RE.match(segment.removesuffix(".git")):
                raise GateError(f"repo path segment {segment!r} is not a plain "
                                f"owner or repository name")
        return url
    if LOCAL_REPO_RE.match(url):
        if not Path(url).exists():
            raise GateError(f"local repo {url!r} does not exist")
        return url
    raise GateError(f"repo {url!r} is neither an https url on an allowed host "
                    f"nor an existing absolute path")


NETWORK_GIT_VERBS = ("fetch", "push", "pull", "ls-remote", "clone",
                     "submodule", "archive")


def _git_remote(clone, wcfg, *args, timeout=600, check=True, ctx=None):
    """Every git call that can reach the network goes through here.

    Not because callers are careless, but because "remember to check first"
    is not an invariant. `_delete_remote_branch` was the proof: it pushed a
    branch deletion straight at `origin`, and with a pushurl injected after
    the normal push, the cleanup hit the attacker's remote and left the real
    branch orphaned on ours. The integrity check now lives at the chokepoint,
    so a new call site inherits it instead of having to remember it.
    """
    ctx = ctx or controller_for(wcfg)
    verb = next((a for a in args if not a.startswith("-")), "")
    if verb not in NETWORK_GIT_VERBS:
        raise CommandError(f"_git_remote used for a local verb: {verb!r}")
    assert_repo_integrity(clone, wcfg, ctx=ctx)
    return _git(clone, *args, timeout=timeout, check=check, ctx=ctx)


def _gh(*args, timeout=300, wcfg=None, ctx=None):
    ctx = ctx or controller_for(wcfg)
    r = subprocess.run([ctx.gh_path, *args], capture_output=True,
                       text=True, timeout=timeout, env=ctx.gh_env)
    if r.returncode != 0:
        raise CommandError(f"gh {' '.join(args)} exited {r.returncode}: "
                           f"{(r.stderr or r.stdout or '').strip()[:300]}")
    return r.stdout


def run_model(staging, prompt, wcfg, depth=0, runtime=None):
    """Spend the model in the workspace. Returns (outcome, output).

    Confined, not trusted: bounded file tools rooted at --add-dir, no shell,
    no git, no gh, no MCP, no network tool, an isolated HOME/XDG/temp inside
    the workspace, a strict environment allowlist, and inference auth handed
    over as one --secret-env-vars variable rather than the operator's real
    credential store (#1).

    Tracked, not fire-and-forget: its own process group, registered in _LIVE
    so a timeout or a SIGTERM takes the whole tree down (#4).

    Outcome is only ever "ok", "timeout" or "failed" here — whether the model
    actually CONTRIBUTED is not something its exit code or its last line is
    allowed to decide (R1).
    """
    fcfg = SS.fanout_config(wcfg)
    staging = Path(staging)
    # HOME, XDG, TMPDIR and the CLI's own logs live OUTSIDE the tool root, so
    # the maker's file tools cannot reach even its own process state — and
    # the staging root stays exactly what the gate expects to find (#1).
    runtime = Path(runtime) if runtime else staging.parent / "runtime"
    runtime.mkdir(parents=True, exist_ok=True)
    timeout_s = int(wcfg["timeout_s"])
    try:
        env = SS.confined_env(fcfg, runtime, depth)
    except SS.AuthUnavailable as e:
        return OUTCOME_FAILED, f"cannot confine the maker: {e}"
    argv = SS.sandbox_wrap(
        SS.confined_argv(prompt, wcfg["model"], staging,
                         tools=SS.MAKER_TOOLS,
                         add_dirs=[staging],
                         secret_vars=SS.secret_vars_for(fcfg),
                         log_dir=runtime / "copilot-logs"),
        # Both roots: tools stay rooted at staging via --add-dir, but the
        # process must be able to write its isolated HOME/XDG/TMPDIR in
        # runtime or the sandbox turns every run into a PermissionError.
        [staging, runtime], bool(fcfg.get("sandbox_exec")),
        profile_dir=runtime)
    try:
        proc = subprocess.Popen(argv, cwd=str(staging), env=env,
                                stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                text=True, start_new_session=True)
    except FileNotFoundError:
        return OUTCOME_FAILED, "copilot CLI not found on PATH"
    except OSError as e:
        return OUTCOME_FAILED, f"could not start the maker: {type(e).__name__}: {e}"
    track(proc)
    try:
        out, err = proc.communicate(timeout=timeout_s)
    except subprocess.TimeoutExpired:
        SS._kill_group(proc, float(fcfg.get("kill_grace_s", 5)))
        try:
            proc.communicate(timeout=10)
        except subprocess.TimeoutExpired:
            pass
        return OUTCOME_TIMEOUT, f"copilot timed out after {timeout_s}s"
    except BaseException:
        SS._kill_group(proc, float(fcfg.get("kill_grace_s", 5)))
        raise
    finally:
        untrack(proc)
    output = (out or "") + (err or "")
    if proc.returncode != 0:
        return OUTCOME_FAILED, output or f"copilot exited {proc.returncode}"
    return "ok", output


def maker_argv(wcfg, staging, prompt="P"):
    """The exact command line the maker would get. Exposed for tests."""
    fcfg = SS.fanout_config(wcfg)
    staging = Path(staging)
    return SS.sandbox_wrap(
        SS.confined_argv(prompt, wcfg.get("model", ""), staging,
                         tools=SS.MAKER_TOOLS, add_dirs=[staging],
                         secret_vars=SS.secret_vars_for(fcfg),
                         log_dir=staging.parent / "runtime" / "copilot-logs"),
        [staging, staging.parent / "runtime"], bool(fcfg.get("sandbox_exec")),
        profile_dir=staging.parent / "runtime")


# ── the deterministic gate ──────────────────────────────────────────────────

def working_tree_changes(clone, timeout=600, ctx=None):
    """Every change in the clone, as (code, path, origin) triples."""
    raw = _git(clone, "status", "--porcelain=v1", "-z", "-uall",
               timeout=timeout, ctx=ctx)
    parts = [p for p in raw.split("\0") if p]
    changes, i = [], 0
    while i < len(parts):
        entry = parts[i]
        i += 1
        if len(entry) < 4:
            raise GateError(f"unparseable git status entry {entry!r}")
        code, path = entry[:2], entry[3:]
        origin = None
        if code[0] in ("R", "C"):
            origin = parts[i] if i < len(parts) else None
            i += 1
        changes.append((code, path, origin))
    return changes


def _new_submission_dir(changes):
    """Exactly one new submissions/<slug>/ and nothing else touched.

    Paths must have exactly three components: a submission is two files at the
    ROOT of its folder. `submissions/<slug>/a/b.svg` used to pass the prefix
    test and then vanish from the two-file count, which is a directory tree
    smuggled through a check that thought it was counting files (#3).
    """
    slugs, files = set(), []
    for code, path, origin in changes:
        if code != "??":
            raise GateError(f"the model changed an existing path: {code} {path}"
                            + (f" (from {origin})" if origin else ""))
        parts = Path(path).parts
        if len(parts) != 3 or parts[0] != "submissions":
            raise GateError(f"not a root-level file in submissions/<slug>/: {path}")
        slugs.add(parts[1])
        files.append(path)
    if not slugs:
        raise GateError("no new submission was left in the clone")
    if len(slugs) > 1:
        raise GateError(f"more than one new submission: {', '.join(sorted(slugs))}")
    slug = slugs.pop()
    return slug, sorted(files)


def _regular_file(path, where):
    """lstat, never stat: the question is what this path IS, not what it
    points at. A symlink to ~/.ssh/id_ed25519 reads fine through stat() and
    commits as a symlink; git stores mode 120000 and the link target becomes
    the blob (#3)."""
    st = os.lstat(path)
    if stat.S_ISLNK(st.st_mode):
        raise GateError(f"{where} is a symlink")
    if not stat.S_ISREG(st.st_mode):
        raise GateError(f"{where} is not a regular file "
                        f"(mode {stat.S_IFMT(st.st_mode):#o})")
    if st.st_nlink > 1:
        raise GateError(f"{where} has {st.st_nlink} hard links")
    if st.st_mode & 0o111:
        raise GateError(f"{where} is executable")
    return st


def _reject_external_url(value, where):
    """CSS is a reference too: url(http...) and @import leave the file."""
    text = str(value)
    low = text.lower()
    if "@import" in low:
        raise GateError(f"{where} carries an @import")
    idx = 0
    while True:
        idx = low.find("url(", idx)
        if idx < 0:
            return
        ref = text[idx + 4:].lstrip().lstrip("'\"").strip()
        if not ref.startswith("#"):
            raise GateError(f"{where} references something outside itself: "
                            f"{ref[:60]}")
        idx += 4


def _regular_dir(path, label="the submission folder"):
    st = os.lstat(path)
    if stat.S_ISLNK(st.st_mode):
        raise GateError(f"{label} is a symlink")
    if not stat.S_ISDIR(st.st_mode):
        raise GateError(f"{label} is not a directory")


def _check_svg(raw, text):
    if b"<!DOCTYPE" in raw or b"<!ENTITY" in raw:
        raise GateError("svg carries a doctype/entity declaration")
    if "javascript:" in text.lower():
        raise GateError("svg carries a javascript: url")
    try:
        root = ET.fromstring(text)
    except ET.ParseError as e:
        raise GateError(f"svg does not parse as xml: {e}")

    def local(name):
        return name.split("}")[-1].lower() if isinstance(name, str) else ""

    if local(root.tag) != "svg":
        raise GateError(f"svg root element is <{local(root.tag)}>, not <svg>")
    for el in root.iter():
        tag = local(el.tag)
        if tag in ("script", "foreignobject"):
            raise GateError(f"svg contains <{tag}>")
        if tag == "style":
            _reject_external_url(el.text or "", "svg <style>")
        for attr, value in el.attrib.items():
            name = local(attr)
            if name.startswith("on"):
                raise GateError(f"svg carries the event attribute {name}")
            if name in ("href", "xlink:href", "src"):
                ref = str(value).strip()
                if not ref.startswith("#"):
                    raise GateError(f"svg references something outside itself: {ref[:60]}")
            _reject_external_url(value, f"svg attribute {name}")


def _check_png(raw):
    """Parse and inflate the complete supported PNG, rejecting ambiguity."""
    if not isinstance(raw, bytes) or not raw.startswith(azure_art.PNG_SIGNATURE):
        raise GateError("png has an invalid signature")
    offset = len(azure_art.PNG_SIGNATURE)
    chunks = 0
    ihdr = None
    idat = []
    seen_idat = False
    idat_closed = False
    seen_iend = False
    seen_plte = False

    while offset < len(raw):
        chunks += 1
        if chunks > PNG_MAX_CHUNKS:
            raise GateError(f"png has more than {PNG_MAX_CHUNKS} chunks")
        if len(raw) - offset < 12:
            raise GateError("png has a truncated chunk frame")
        length = struct.unpack(">I", raw[offset:offset + 4])[0]
        if length > PNG_MAX_CHUNK_BYTES:
            raise GateError(
                f"png chunk length {length} exceeds {PNG_MAX_CHUNK_BYTES}")
        chunk_type = raw[offset + 4:offset + 8]
        if (len(chunk_type) != 4
                or any(not (65 <= byte <= 90 or 97 <= byte <= 122)
                       for byte in chunk_type)
                or not 65 <= chunk_type[2] <= 90):
            raise GateError("png has an invalid chunk type")
        data_start = offset + 8
        data_end = data_start + length
        frame_end = data_end + 4
        if data_end < data_start or frame_end > len(raw):
            raise GateError(
                f"png chunk {chunk_type.decode('ascii')} is truncated")
        data = raw[data_start:data_end]
        expected_crc = struct.unpack(">I", raw[data_end:frame_end])[0]
        actual_crc = zlib.crc32(chunk_type)
        actual_crc = zlib.crc32(data, actual_crc) & 0xFFFFFFFF
        if expected_crc != actual_crc:
            raise GateError(
                f"png chunk {chunk_type.decode('ascii')} has an invalid CRC")

        if chunks == 1:
            if chunk_type != b"IHDR" or length != 13:
                raise GateError("png IHDR must be the first chunk with length 13")
        elif chunk_type == b"IHDR":
            raise GateError("png contains more than one IHDR")

        if chunk_type == b"IHDR":
            if ihdr is not None or length != 13:
                raise GateError("png has an invalid IHDR")
            width, height, bit_depth, color_type, compression, filtering, interlace = (
                struct.unpack(">IIBBBBB", data))
            if (width < PNG_MIN_DIMENSION or height < PNG_MIN_DIMENSION
                    or width > PNG_MAX_DIMENSION or height > PNG_MAX_DIMENSION):
                raise GateError(
                    f"png dimensions {width}x{height} are outside "
                    f"{PNG_MIN_DIMENSION}-{PNG_MAX_DIMENSION}")
            if width * height > PNG_MAX_PIXELS:
                raise GateError(
                    f"png has {width * height} pixels, over the "
                    f"{PNG_MAX_PIXELS} pixel cap")
            if bit_depth != 8 or color_type not in (2, 6):
                raise GateError(
                    "png must be 8-bit RGB or RGBA")
            if compression != 0 or filtering != 0 or interlace != 0:
                raise GateError(
                    "png must use standard compression/filtering and be "
                    "non-interlaced")
            ihdr = {
                "width": width,
                "height": height,
                "bit_depth": bit_depth,
                "color_type": color_type,
                "channels": 3 if color_type == 2 else 4,
            }
        elif chunk_type == b"PLTE":
            if (seen_plte or seen_idat or not 3 <= length <= 768
                    or length % 3):
                raise GateError("png has an invalid PLTE")
            seen_plte = True
        elif chunk_type == b"IDAT":
            if ihdr is None:
                raise GateError("png IDAT appears before IHDR")
            if idat_closed:
                raise GateError("png IDAT chunks are not consecutive")
            seen_idat = True
            idat.append(data)
        elif chunk_type == b"IEND":
            if length != 0:
                raise GateError("png IEND must have zero length")
            if not seen_idat:
                raise GateError("png has no IDAT")
            seen_iend = True
            offset = frame_end
            if offset != len(raw):
                raise GateError("png has trailing bytes after IEND")
            break
        else:
            if seen_idat:
                idat_closed = True
            if 65 <= chunk_type[0] <= 90:
                raise GateError(
                    f"png has unsupported critical chunk "
                    f"{chunk_type.decode('ascii')}")
        offset = frame_end

    if ihdr is None:
        raise GateError("png has no valid IHDR")
    if not seen_iend:
        raise GateError("png has no final IEND")
    compressed = b"".join(idat)
    if not compressed:
        raise GateError("png IDAT payload is empty")
    stride = 1 + ihdr["width"] * ihdr["channels"]
    expected_size = stride * ihdr["height"]
    inflater = zlib.decompressobj()
    try:
        pixels = inflater.decompress(compressed, expected_size + 1)
        if inflater.unconsumed_tail or len(pixels) > expected_size:
            raise GateError("png decompressed data exceeds its IHDR dimensions")
        pixels += inflater.flush(expected_size - len(pixels) + 1)
    except zlib.error as exc:
        raise GateError(f"png IDAT zlib stream is invalid: {exc}") from exc
    if (not inflater.eof or inflater.unused_data or inflater.unconsumed_tail):
        raise GateError("png IDAT does not contain exactly one complete zlib stream")
    if len(pixels) != expected_size:
        raise GateError(
            f"png decompressed to {len(pixels)} bytes, expected {expected_size}")
    if any(pixels[row * stride] > 4 for row in range(ihdr["height"])):
        raise GateError("png scanline has an invalid filter type")
    return ihdr


def validate_image_generation_receipt(meta, image_bytes, profile_snapshot):
    """Bind controller review evidence to exact PNG bytes and profile policy."""
    snapshot = validate_profile_snapshot(profile_snapshot)
    image_info = _check_png(image_bytes)
    contract = validate_reviewed_submission_contract(meta)
    subject = contract["slug"]
    receipt = meta.get("_image_generation") if isinstance(meta, dict) else None
    expected_receipt = {
        "schema", "profile", "provider", "deployment", "attempts",
        "image_sha256", "image", "review",
    }
    if not isinstance(receipt, dict) or set(receipt) != expected_receipt:
        raise GateError("reviewed PNG has a missing or malformed generation receipt")
    if receipt.get("schema") != IMAGE_GENERATION_SCHEMA:
        raise GateError("reviewed PNG receipt has the wrong schema")
    if receipt.get("profile") != snapshot["profile"]:
        raise GateError("reviewed PNG receipt has the wrong profile")
    if receipt.get("provider") != snapshot["provider"]:
        raise GateError("reviewed PNG receipt has the wrong provider")
    deployment = _bounded_receipt_text(
        receipt.get("deployment"),
        subject,
        "_image_generation.deployment",
        IMAGE_MAX_IDENTIFIER_CHARS,
        identifier=True,
    )
    if deployment not in snapshot["deployments"]:
        raise GateError("reviewed PNG receipt names an unconfigured deployment")
    attempts = receipt.get("attempts")
    if (isinstance(attempts, bool) or not isinstance(attempts, int)
            or not 1 <= attempts <= snapshot["max_attempts"]):
        raise GateError("reviewed PNG receipt has invalid attempts")
    digest = hashlib.sha256(image_bytes).hexdigest()
    if (not isinstance(receipt.get("image_sha256"), str)
            or receipt.get("image_sha256") != digest):
        raise GateError("reviewed PNG receipt digest does not match piece.png")
    dimensions = receipt.get("image")
    if (not isinstance(dimensions, dict)
            or set(dimensions) != {"width", "height"}
            or isinstance(dimensions.get("width"), bool)
            or not isinstance(dimensions.get("width"), int)
            or isinstance(dimensions.get("height"), bool)
            or not isinstance(dimensions.get("height"), int)
            or dimensions.get("width") != image_info["width"]
            or dimensions.get("height") != image_info["height"]):
        raise GateError("reviewed PNG receipt dimensions do not match IHDR")
    review = receipt.get("review")
    expected_review = {
        "schema", "model", "publish", "score", "minimum_score",
        "failures", "strengths",
    }
    if not isinstance(review, dict) or set(review) != expected_review:
        raise GateError("reviewed PNG receipt has a malformed review")
    if review.get("schema") != snapshot["review_schema"]:
        raise GateError("reviewed PNG receipt has the wrong review schema")
    model = _bounded_receipt_text(
        review.get("model"),
        subject,
        "_image_generation.review.model",
        IMAGE_MAX_IDENTIFIER_CHARS,
        identifier=True,
    )
    if model != snapshot["review_model"]:
        raise GateError("reviewed PNG receipt has the wrong review model")
    if review.get("publish") is not True:
        raise GateError("reviewed PNG receipt is not approved for publication")
    score = review.get("score")
    if (isinstance(score, bool) or not isinstance(score, int)
            or not 0 <= score <= 10):
        raise GateError("reviewed PNG receipt has an invalid score")
    minimum_score = review.get("minimum_score")
    if (isinstance(minimum_score, bool)
            or not isinstance(minimum_score, int)
            or minimum_score != snapshot["minimum_review_score"]):
        raise GateError("reviewed PNG receipt captured the wrong minimum score")
    if score < snapshot["minimum_review_score"]:
        raise GateError("reviewed PNG receipt score is below the captured minimum")
    if review.get("failures") != []:
        raise GateError("reviewed PNG receipt still contains review failures")
    strengths = review.get("strengths")
    if (not isinstance(strengths, list)
            or len(strengths) > IMAGE_MAX_STRENGTHS):
        raise GateError("reviewed PNG receipt strengths are not bounded")
    for index, strength in enumerate(strengths):
        _bounded_receipt_text(
            strength,
            subject,
            f"_image_generation.review.strengths[{index}]",
            IMAGE_MAX_STRENGTH_CHARS,
        )
    _reject_credential_material(meta, subject)
    return image_info


def _check_piece(path, kind, max_bytes):
    raw = path.read_bytes()
    if not raw:
        raise GateError("piece is empty")
    if len(raw) > max_bytes:
        raise GateError(f"piece is {len(raw)} bytes, over the {max_bytes} byte cap")
    if kind == "png":
        _check_png(raw)
        return raw
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError:
        raise GateError("piece is not valid utf-8")
    if "\0" in text:
        raise GateError("piece carries a NUL byte")
    if kind == "svg":
        _check_svg(raw, text)
    elif kind == "json":
        try:
            json.loads(text)
        except json.JSONDecodeError as e:
            raise GateError(f"piece is not valid json: {e}")
    return raw


def _check_number(value, where):
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise GateError(f"{where} is not a number")
    if value != value or value in (float("inf"), float("-inf")):
        raise GateError(f"{where} is not finite")
    if not (SCORE_MIN <= value <= SCORE_MAX):
        raise GateError(f"{where} is outside {SCORE_MIN}..{SCORE_MAX}")


def validate_dada_cycle(cycle, slug, expected_cycle, expected_previous,
                        expected_round1=None):
    """The cycle block is the search, checked. Reject on the first doubt."""
    if not isinstance(cycle, dict):
        raise GateError("meta._dada_cycle is missing or not an object")
    if cycle.get("cycle") != expected_cycle:
        raise GateError(f"_dada_cycle.cycle is {cycle.get('cycle')!r}, "
                        f"expected {expected_cycle} — cycle continuity broken")
    if cycle.get("previous_slug") != expected_previous:
        raise GateError(f"_dada_cycle.previous_slug is "
                        f"{cycle.get('previous_slug')!r}, expected "
                        f"{expected_previous!r} — cycle continuity broken")
    rounds = cycle.get("rounds")
    if not isinstance(rounds, list) or not (MIN_ROUNDS <= len(rounds) <= MAX_ROUNDS):
        raise GateError(f"_dada_cycle.rounds must hold {MIN_ROUNDS}..{MAX_ROUNDS} "
                        f"rounds, found "
                        f"{len(rounds) if isinstance(rounds, list) else 'none'}")
    for index, rnd in enumerate(rounds, start=1):
        if not isinstance(rnd, dict):
            raise GateError(f"round {index} is not an object")
        if rnd.get("round") != index:
            raise GateError(f"round {index} is numbered {rnd.get('round')!r} — "
                            f"rounds must be contiguous from 1")
        candidates = rnd.get("candidates")
        if not isinstance(candidates, list) or len(candidates) != CANDIDATES_PER_ROUND:
            raise GateError(
                f"round {index} has "
                f"{len(candidates) if isinstance(candidates, list) else 'no'} "
                f"candidates, exactly {CANDIDATES_PER_ROUND} required")
        seen = set()
        for cand in candidates:
            if not isinstance(cand, dict):
                raise GateError(f"round {index} holds a non-object candidate")
            cid = cand.get("id")
            if not isinstance(cid, str) or not cid.strip():
                raise GateError(f"round {index} holds a candidate without an id")
            if cid in seen:
                raise GateError(f"round {index} repeats candidate id {cid!r}")
            seen.add(cid)
            premise = cand.get("premise")
            if not isinstance(premise, str) or not premise.strip():
                raise GateError(f"candidate {cid} has no premise")
            scores = cand.get("scores")
            if not isinstance(scores, dict):
                raise GateError(f"candidate {cid} has no scores object")
            if set(scores) != set(SCORE_DIMENSIONS):
                raise GateError(
                    f"candidate {cid} scores {sorted(scores)}, expected exactly "
                    f"{list(SCORE_DIMENSIONS)}")
            for dim in SCORE_DIMENSIONS:
                _check_number(scores[dim], f"candidate {cid} score {dim}")
        selected = rnd.get("selected")
        if selected not in seen:
            raise GateError(f"round {index} selected {selected!r}, which is not "
                            f"one of its candidates")
        if index == 1 and expected_round1 is not None:
            # The fan-out's ten finalists ARE round one — and a finalist is
            # its content, not its label. Checking ids alone let a maker keep
            # the names and replace every premise and score underneath them,
            # which is the deliberation erased and its receipt kept (#2). The
            # digest covers premise, rationale, all six scores, which
            # sub-sentinel produced it, and that child's evidence.
            wanted = dict(expected_round1)
            if seen != set(wanted):
                missing = sorted(set(wanted) - seen)
                added = sorted(seen - set(wanted))
                raise GateError(
                    "round 1 must be exactly the ten finalists the "
                    "sub-sentinels produced"
                    + (f"; missing {missing}" if missing else "")
                    + (f"; unexpected {added}" if added else ""))
            for cand in candidates:
                cid = cand["id"]
                for field in SS.RECORD_FIELDS:
                    if field not in cand:
                        raise GateError(f"round 1 candidate {cid} is missing "
                                        f"{field}; round one must carry the "
                                        f"finalist record, not just its id")
                try:
                    actual = SS.record_digest(cand)
                except SS.FanoutError as e:
                    raise GateError(f"round 1 candidate {cid}: {e}")
                if actual != wanted[cid]:
                    raise GateError(
                        f"round 1 candidate {cid} does not match the finalist "
                        f"the sub-sentinels produced (digest {actual[:12]}… != "
                        f"{wanted[cid][:12]}…) — its content was rewritten")
                if "digest" in cand and cand["digest"] != actual:
                    raise GateError(f"round 1 candidate {cid} publishes a digest "
                                    f"that does not match its own fields")
    winner = cycle.get("winner")
    if not isinstance(winner, dict):
        raise GateError("_dada_cycle.winner is missing or not an object")
    last = rounds[-1]
    if winner.get("round") != last["round"]:
        raise GateError(f"winner.round is {winner.get('round')!r}, expected the "
                        f"final round {last['round']}")
    if winner.get("candidate") != last.get("selected"):
        raise GateError(f"winner.candidate is {winner.get('candidate')!r}, but the "
                        f"final round selected {last.get('selected')!r}")
    if winner.get("slug") != slug:
        raise GateError(f"winner.slug is {winner.get('slug')!r}, but the submission "
                        f"is {slug!r}")


def assert_no_git(root):
    """A staging workspace must contain no git metadata of any kind.

    The maker's file tools are rooted here. If a `.git` directory — or a
    `.git` FILE pointing at one — can exist inside that root, the model can
    write `remote.origin.pushurl` and the controller will happily push the
    submission to somebody else's server. The fix is structural: the model
    never gets a repository, so there is nothing to rewrite (#1).
    """
    root = Path(root)
    for path in [root, *root.rglob("*")]:
        if path.name in (".git", ".gitmodules", ".gitattributes"):
            raise GateError(f"the staging workspace contains {path.name} at "
                            f"{path.relative_to(root) if path != root else '.'}")


# The maker has file tools, not a shell — `create` writes files, nothing
# makes directories. A live cycle proved it: the model produced the art, could
# not create out/submissions/<its-new-slug>/, left a `.probe` behind trying,
# and the gate correctly rejected the lot. Asking a model to mkdir with no
# mkdir is a contract that cannot be honoured, so the controller precreates
# ONE fixed directory and the slug moves to meta.json where it belongs.
SUBMISSION_DIR = "submission"
META_NAME = "meta.json"
STATE_OUT_NAME = "state-out.json"


# One number, used by the writer and the reader. They disagreed before: the
# writer kept the last 50 cycles, the reader demanded 1..N, so the first state
# written after cycle 50 was one the worker itself refused to read — a ledger
# that bricks its own instance at cycle 51 and only on a long-lived install,
# which is the worst possible time to find out.
CREATIVE_HISTORY_LIMIT = 50


def history_limit(wcfg=None):
    raw = (wcfg or {}).get("creative_history_limit")
    if raw is None:
        return CREATIVE_HISTORY_LIMIT
    if isinstance(raw, bool) or not isinstance(raw, int):
        raise LedgerError(f"creative_history_limit {raw!r} is not an integer")
    if raw < 1:
        raise LedgerError(f"creative_history_limit {raw} is not positive")
    return raw


def _cycle_entries(state):
    """The `cycles` list, validated row by row. Anything odd fails closed."""
    raw = state.get("cycles")
    if raw is None:
        return []
    if not isinstance(raw, list):
        raise LedgerError("creative state 'cycles' is not a list")
    entries = []
    for i, row in enumerate(raw):
        if not isinstance(row, dict):
            raise LedgerError(f"creative state cycles[{i}] is not an object")
        number = row.get("cycle", row.get("n"))
        if isinstance(number, bool) or not isinstance(number, int) or number < 1:
            raise LedgerError(f"creative state cycles[{i}].cycle is "
                              f"{number!r}, not a positive integer")
        entries.append({**row, "cycle": number})
    return entries


def _validated_history(state, limit):
    """The history as a strictly ordered, unique, contiguous run.

    Two shapes are legitimate and they are not interchangeable:

      * a PREFIX starting at 1 — everything this instance has ever done.
      * a SUFFIX starting above 1 — the bounded tail this loop keeps. A tail
        is only readable if it says where it ends: it must carry an explicit
        canonical counter, be exactly `limit` long, end AT that counter, and
        therefore start at counter - limit + 1. A short or gapped tail is
        indistinguishable from a corrupted history, so it fails closed rather
        than being guessed at.
    """
    entries = _cycle_entries(state)
    if not entries:
        return entries, None
    numbers = [e["cycle"] for e in entries]
    expected = list(range(numbers[0], numbers[0] + len(numbers)))
    if numbers != expected:
        raise LedgerError(f"creative state cycles are not a strictly ordered "
                          f"contiguous run: {numbers}")
    return entries, numbers


def creative_position(state, wcfg=None):
    """(completed_cycle, previous_slug) from any state this loop has written.

    Canonical order: `cycle`, else `last_cycle`, else the history itself.
    Fields that disagree are not reconciled quietly — they fail closed,
    because "which of these numbers is the truth" is not a question this code
    gets to answer on its own.
    """
    if not isinstance(state, dict):
        raise LedgerError("creative state is not an object")
    if not state:
        return 0, None
    limit = history_limit(wcfg)
    entries, numbers = _validated_history(state, limit)

    declared = []
    for key in ("cycle", "last_cycle"):
        value = state.get(key)
        if value is None:
            continue
        if isinstance(value, bool) or not isinstance(value, int) or value < 0:
            raise LedgerError(f"creative state '{key}' is {value!r}, not a "
                              f"non-negative integer")
        declared.append((key, value))
    if len({v for _, v in declared}) > 1:
        raise LedgerError(f"creative state disagrees with itself: "
                          f"{', '.join(f'{k}={v}' for k, v in declared)}")
    counter = declared[0][1] if declared else None

    if numbers and numbers[0] > 1:
        if counter is None:
            raise LedgerError(
                f"creative state holds a bounded tail starting at cycle "
                f"{numbers[0]} with no 'cycle' or 'last_cycle' to say where it "
                f"ends")
        if len(numbers) != limit:
            raise LedgerError(
                f"creative state holds {len(numbers)} cycles starting at "
                f"{numbers[0]}; a tail must be exactly {limit} long")
        if numbers[-1] != counter:
            raise LedgerError(
                f"creative state's tail ends at cycle {numbers[-1]} but the "
                f"counter says {counter}")
        if numbers[0] != counter - limit + 1:
            raise LedgerError(
                f"creative state's tail starts at {numbers[0]}, expected "
                f"{counter - limit + 1} for a {limit}-cycle tail ending at "
                f"{counter}")

    completed = counter if counter is not None else (numbers[-1] if numbers else None)
    if completed is None:
        raise LedgerError("creative state has no cycle, last_cycle or cycles")
    if numbers and completed < numbers[-1]:
        raise LedgerError(f"creative state says cycle {completed} but its "
                          f"cycles list reaches {numbers[-1]}")

    previous = state.get("last_slug")
    if previous is None:
        for entry in reversed(entries):
            candidate = entry.get("slug") or entry.get("last_slug")
            if isinstance(candidate, str) and candidate:
                previous = candidate
                break
    if previous is not None and not isinstance(previous, str):
        raise LedgerError(f"creative state last_slug is {previous!r}, not a slug")
    return completed, previous


def next_creative_cycle(state, wcfg=None):
    """What the next cycle number and previous slug must be."""
    completed, previous = creative_position(state, wcfg)
    return completed + 1, previous


def merge_creative_state(previous_state, next_state, cycle, slug, receipts,
                         wcfg=None):
    """The state to write after a verified merge, preserving legacy history.

    Writes what the reader requires: a strictly ordered contiguous tail of at
    most `limit` cycles, ending at the cycle just merged, with `cycle`,
    `last_cycle` and `last_slug` all saying the same thing. The two halves of
    this ledger now share one constant instead of two opinions.
    """
    limit = history_limit(wcfg)
    merged = dict(next_state or {})
    entries, _ = _validated_history(previous_state or {}, limit)
    entries = [e for e in entries if e.get("cycle") != cycle]
    entries.append({
        "cycle": cycle,
        "slug": slug,
        "merge_commit": receipts.get("merge_commit", ""),
        "pr": receipts.get("pr_url", ""),
        "at": sentinel.now().isoformat(timespec="seconds"),
    })
    entries.sort(key=lambda e: e["cycle"])
    # Keep the tail contiguous: an older run with a gap in it cannot be
    # carried forward as history, because the reader would refuse it.
    tail = [entries[-1]]
    for entry in reversed(entries[:-1]):
        if entry["cycle"] != tail[0]["cycle"] - 1 or len(tail) >= limit:
            break
        tail.insert(0, entry)
    merged["cycles"] = tail
    merged["cycle"] = cycle
    merged["last_cycle"] = cycle
    merged["last_slug"] = slug
    merged["updated_at"] = sentinel.now().isoformat(timespec="seconds")
    merged["merge_commit"] = receipts.get("merge_commit", "")
    return merged


def prepare_staging(staging):
    """Everything the maker writes into must already exist when it starts."""
    staging = Path(staging)
    out = staging / "out" / SUBMISSION_DIR
    out.mkdir(parents=True, exist_ok=True)
    (staging / "context").mkdir(parents=True, exist_ok=True)
    return out


def _scan_staging(staging):
    """Every path under staging, by lstat, without following a single link."""
    staging = Path(staging)
    entries = {}
    for root, dirnames, filenames in os.walk(staging, followlinks=False):
        for name in list(dirnames) + list(filenames):
            path = Path(root) / name
            rel = path.relative_to(staging).as_posix()
            st = os.lstat(path)
            if stat.S_ISLNK(st.st_mode):
                kind, digest = "symlink", ""
            elif stat.S_ISDIR(st.st_mode):
                kind, digest = "dir", ""
            elif stat.S_ISREG(st.st_mode):
                kind = "file"
                digest = hashlib.sha256(path.read_bytes()).hexdigest()
            else:
                kind, digest = "other", ""
            entries[rel] = {"kind": kind, "mode": stat.S_IMODE(st.st_mode),
                            "sha256": digest}
    return entries


def staging_manifest(staging):
    """The canonical baseline, captured before the model is allowed to run.

    Everything the controller put in staging — the context it may read, the
    finalists it must reproduce, the empty output directory — recorded by
    path, type, mode and content digest. Anything that is not this, afterwards,
    is something the model did.
    """
    baseline = _scan_staging(staging)
    for rel, entry in sorted(baseline.items()):
        if entry["kind"] not in ("dir", "file"):
            raise GateError(f"prepared staging already contains a {entry['kind']} "
                            f"at {rel}")
    return baseline


def verify_staging_tree(staging, baseline, wcfg=None, profile_snapshot=None):
    """The WHOLE staging tree, not just the corner the submission lives in.

    The gate used to read out/, state-out.json and .git and call it done — so
    a model could leave a draft, a note, a second copy of its piece or a
    rewritten prior.json anywhere else in staging and the cycle would sail
    through. "Everything I did not check was fine" is the assumption this
    whole file exists to refuse.

    Returns the piece filename. Raises GateError on the first difference.
    """
    staging = Path(staging)
    snapshot = (validate_profile_snapshot(profile_snapshot)
                if profile_snapshot is not None
                else publication_profile_snapshot(wcfg or {}))
    allowed_new = {STATE_OUT_NAME, f"out/{SUBMISSION_DIR}/{META_NAME}"}
    if snapshot:
        piece_names = {f"out/{SUBMISSION_DIR}/piece.prompt"}
    else:
        piece_names = {f"out/{SUBMISSION_DIR}/piece{ext}"
                       for ext in KIND_EXTENSIONS.values()}
        piece_names.add(f"out/{SUBMISSION_DIR}/piece.prompt")
    current = _scan_staging(staging)

    for rel, before in sorted(baseline.items()):
        after = current.get(rel)
        if after is None:
            raise GateError(f"the model deleted {rel} from its context")
        if after["kind"] != before["kind"]:
            raise GateError(f"{rel} changed from {before['kind']} to "
                            f"{after['kind']}")
        if before["kind"] == "file" and after["sha256"] != before["sha256"]:
            raise GateError(f"the model modified {rel}, which is read-only "
                            f"context")
        if after["mode"] != before["mode"]:
            raise GateError(f"{rel} changed mode from {before['mode']:o} to "
                            f"{after['mode']:o}")

    new_paths = sorted(set(current) - set(baseline))
    pieces = []
    for rel in new_paths:
        entry = current[rel]
        if entry["kind"] == "dir":
            raise GateError(f"the model created the directory {rel}; every "
                            f"path it needs already exists")
        if entry["kind"] != "file":
            raise GateError(f"the model left a {entry['kind']} at {rel}")
        if Path(rel).name.startswith("."):
            raise GateError(f"the model left the hidden file {rel}")
        if rel in allowed_new:
            continue
        if rel in piece_names:
            pieces.append(rel)
            continue
        raise GateError(f"the model left {rel}, which is not part of a "
                        f"submission (allowed: out/{SUBMISSION_DIR}/{META_NAME}, "
                        f"out/{SUBMISSION_DIR}/piece.<ext>, {STATE_OUT_NAME})")
    if len(pieces) > 1:
        raise GateError(f"more than one piece: {', '.join(pieces)}")
    if snapshot:
        required = {
            STATE_OUT_NAME,
            f"out/{SUBMISSION_DIR}/{META_NAME}",
            f"out/{SUBMISSION_DIR}/piece.prompt",
        }
        if set(new_paths) != required:
            raise GateError(
                f"{AZURE_REVIEWED_PNG_PROFILE} maker output must be exactly "
                "meta.json + piece.prompt + state-out.json; direct piece.png, "
                "SVG, markdown, text, and JSON pieces are forbidden")
    return Path(pieces[0]).name if pieces else ""


def gate_directory(root, wcfg, expected_cycle, expected_previous,
                   expected_round1=None, known_slugs=(),
                   profile_snapshot=None):
    """Validate the fixed output directory. No git anywhere near it.

    `root` is `<staging>/out`, whose only child is the precreated
    `submission/` holding exactly meta.json and piece.<ext>. The slug is read
    from meta.json — the maker cannot name a directory it cannot create —
    and the controller materialises the gated bytes under that slug in its own
    clone. Returns the submission record INCLUDING the validated bytes.
    """
    snapshot = (validate_profile_snapshot(profile_snapshot)
                if profile_snapshot is not None
                else publication_profile_snapshot(wcfg))
    root = Path(root)
    if not root.is_dir():
        raise GateError("no staging output directory exists")
    top = sorted(p.name for p in root.iterdir())
    if top != [SUBMISSION_DIR]:
        extra = [n for n in top if n != SUBMISSION_DIR]
        if not extra:
            raise GateError(f"the staging output is missing {SUBMISSION_DIR}/")
        raise GateError(f"the staging output holds {extra}, expected only "
                        f"{SUBMISSION_DIR}/ — the maker may not create paths")

    directory = root / SUBMISSION_DIR
    _regular_dir(directory, f"out/{SUBMISSION_DIR}")
    entries = sorted(directory.iterdir(), key=lambda p: p.name)
    if not entries:
        raise GateError("no new submission was left in the staging workspace")
    names = []
    for entry in entries:
        rel = f"out/{SUBMISSION_DIR}/{entry.name}"
        if entry.name.startswith("."):
            raise GateError(f"{rel} is a hidden file; the submission is exactly "
                            f"{META_NAME} + piece.<ext>")
        _regular_file(entry, rel)
        names.append(entry.name)
    if len(names) != 2 or META_NAME not in names:
        raise GateError(f"a submission is exactly {META_NAME} + piece.<ext>, "
                        f"found {names}")
    piece_name = [n for n in names if n != META_NAME][0]
    piece_path = directory / piece_name
    meta_path = directory / META_NAME

    meta_bytes = meta_path.read_bytes()
    if len(meta_bytes) > int(wcfg.get("max_meta_bytes", 262144)):
        raise GateError("meta.json is implausibly large")
    try:
        meta = json.loads(meta_bytes.decode("utf-8"))
    except (json.JSONDecodeError, UnicodeDecodeError) as e:
        raise GateError(f"meta.json is not valid json: {e}")
    if not isinstance(meta, dict):
        raise GateError("meta.json is not an object")

    missing = [k for k in REQUIRED_META_KEYS if k not in meta]
    if missing:
        raise GateError(f"meta.json is missing {', '.join(missing)}")
    extra = [k for k in meta
             if k not in REQUIRED_META_KEYS and not k.startswith("_")]
    if extra:
        raise GateError(f"meta.json carries unknown keys: {', '.join(sorted(extra))}")
    if meta["schema"] != SUBMISSION_SCHEMA:
        raise GateError(f"meta.schema is {meta['schema']!r}, expected "
                        f"{SUBMISSION_SCHEMA!r}")

    slug = _submission_slug(meta.get("slug"))
    if slug in set(known_slugs):
        raise GateError(f"slug {slug!r} already exists on the base branch")

    if not isinstance(meta.get("title"), str) or not meta["title"].strip():
        raise GateError("meta.title is empty")
    if not isinstance(meta.get("contributor"), str) or not meta["contributor"].strip():
        raise GateError("meta.contributor is empty")
    if meta["license"] not in ALLOWED_LICENSES:
        raise GateError(f"meta.license is {meta['license']!r}, expected one of "
                        f"{', '.join(ALLOWED_LICENSES)}")
    kind = meta.get("kind")
    if kind not in KIND_EXTENSIONS:
        raise GateError(f"meta.kind is {kind!r}, expected one of "
                        f"{', '.join(sorted(KIND_EXTENSIONS))}")
    permitted = allowed_kinds(wcfg)
    if kind not in permitted:
        raise GateError(f"meta.kind is {kind!r}, but this worker requires "
                        f"{', '.join(permitted)}")
    if piece_name != f"piece{KIND_EXTENSIONS[kind]}":
        raise GateError(f"piece is {piece_name!r}, expected "
                        f"piece{KIND_EXTENSIONS[kind]} for kind {kind!r}")
    stamp = str(meta.get("submitted_at") or "")
    try:
        datetime.fromisoformat(stamp.replace("Z", "+00:00"))
    except ValueError:
        raise GateError(f"meta.submitted_at {stamp!r} is not ISO-8601")
    remix = meta.get("remix_of")
    if remix is not None:
        if not isinstance(remix, str) or not SLUG_RE.match(remix):
            raise GateError(f"meta.remix_of {remix!r} is not a slug or null")
        if remix not in set(known_slugs):
            raise GateError(f"meta.remix_of {remix!r} does not exist on the "
                            f"base branch")

    piece_bytes = _check_piece(piece_path, kind,
                              int(wcfg.get("max_piece_bytes", 51200)))
    image_info = None
    if snapshot:
        if kind != "png":
            raise GateError(
                f"{AZURE_REVIEWED_PNG_PROFILE} accepts only piece.png")
        validate_reviewed_submission_contract(meta, expected_slug=slug)
        image_info = validate_image_generation_receipt(
            meta, piece_bytes, snapshot)
    elif kind == "png":
        raise GateError(
            "PNG requires the azure-reviewed-png publication profile")
    validate_dada_cycle(meta.get("_dada_cycle"), slug, expected_cycle,
                        expected_previous, expected_round1)

    submission = {
        "slug": slug,
        "title": meta["title"],
        "kind": kind,
        "meta": meta,
        "meta_path": f"submissions/{slug}/{META_NAME}",
        "piece_path": f"submissions/{slug}/{piece_name}",
        "meta_bytes": meta_bytes,
        "piece_bytes": piece_bytes,
        "meta_sha256": hashlib.sha256(meta_bytes).hexdigest(),
        "piece_sha256": hashlib.sha256(piece_bytes).hexdigest(),
    }
    if image_info:
        submission["image"] = {
            "width": image_info["width"],
            "height": image_info["height"],
        }
    return submission


def base_branch_slugs(clone, base_branch, wcfg, ctx=None):
    """Every slug already published, read by the controller from its clone."""
    ctx = ctx or controller_for(wcfg)
    listing = _git(clone, "ls-tree", "--name-only", base_branch, "submissions/",
                   timeout=int(wcfg.get("git_timeout_s", 600)), ctx=ctx)
    return {line.strip("/").split("/")[-1]
            for line in listing.splitlines() if line.strip()}


def install_into_clone(clone, submission):
    """Copy the two VALIDATED files — as bytes — into the controller's clone.

    Bytes, not paths: the gate read them, the gate hashed them, and these are
    the same objects. Copying by path would re-open files in a directory a
    model process was writing to seconds ago.
    """
    clone = Path(clone)
    directory = clone / "submissions" / submission["slug"]
    if directory.exists():
        raise GateError(f"{directory} already exists in the controller's clone")
    directory.mkdir(parents=True)
    for rel, blob in ((submission["meta_path"], submission["meta_bytes"]),
                      (submission["piece_path"], submission["piece_bytes"])):
        target = clone / rel
        with open(target, "wb") as fh:
            fh.write(blob)
        os.chmod(target, 0o644)
    return directory


def verify_clone_scope(clone, submission, wcfg, base_sha=None, ctx=None):
    """After the copy, the controller's clone must hold exactly two new files.

    The maker never had this directory — this catches the controller's own
    mistakes, and anything else that touched the clone while we worked.
    """
    ctx = ctx or controller_for(wcfg)
    clone = Path(clone)
    git_t = int(wcfg.get("git_timeout_s", 600))
    if base_sha:
        head = _git(clone, "rev-parse", "HEAD", timeout=git_t, ctx=ctx).strip()
        if head != base_sha:
            raise GateError("the controller's clone moved its HEAD unexpectedly")
    changes = working_tree_changes(clone, timeout=git_t, ctx=ctx)
    slug, files = _new_submission_dir(changes)
    if slug != submission["slug"]:
        raise GateError(f"the clone holds {slug!r}, expected "
                        f"{submission['slug']!r}")
    expected = sorted([submission["meta_path"], submission["piece_path"]])
    if files != expected:
        raise GateError(f"the clone holds {files}, expected {expected}")
    for rel, digest in ((submission["meta_path"], submission["meta_sha256"]),
                        (submission["piece_path"], submission["piece_sha256"])):
        path = clone / rel
        _regular_file(path, rel)
        if hashlib.sha256(path.read_bytes()).hexdigest() != digest:
            raise GateError(f"{rel} in the clone is not the file that passed "
                            f"the gate")
    return expected


# Local git config keys that can redirect, intercept or execute. None of them
# belong in a clone this worker made ten minutes ago.
FORBIDDEN_CONFIG_PREFIXES = (
    "remote.origin.pushurl", "remote.", "url.", "core.hookspath",
    "core.sshcommand", "core.gitproxy", "core.fsmonitor", "credential.",
    "protocol.", "alias.", "include.", "includeif.", "filter.", "diff.",
    "uploadpack.", "receive.",
)
ALLOWED_CONFIG_KEYS = {
    "core.repositoryformatversion", "core.filemode", "core.bare",
    "core.logallrefupdates", "core.ignorecase", "core.precomposeunicode",
    "core.symlinks", "core.worktree", "remote.origin.url",
    "remote.origin.fetch", "branch.main.remote", "branch.main.merge",
    "extensions.objectformat", "init.defaultbranch",
}


def assert_repo_integrity(clone, wcfg, ctx=None):
    """Prove, immediately before touching git, that this is still OUR clone.

    A bounded repro of the real finding: the maker wrote into `clone/.git`,
    set `remote.origin.pushurl`, and the controller pushed the branch to an
    attacker's remote before failing later for an unrelated reason. The maker
    can no longer reach a repository at all — and this runs anyway, before
    every git operation, because "cannot happen" is not a check (#1).
    """
    ctx = ctx or controller_for(wcfg)
    clone = Path(clone)
    git_t = int(wcfg.get("git_timeout_s", 600))
    canonical = (ctx.repo.transport if ctx.repo
                 else validate_repo_url(wcfg["repo"], wcfg))

    dot_git = clone / ".git"
    if not dot_git.is_dir():
        raise GateError("the clone's .git is not a directory")
    alternates = dot_git / "objects" / "info" / "alternates"
    if alternates.exists():
        raise GateError("the clone has an objects/info/alternates file")
    hooks = dot_git / "hooks"
    if hooks.is_dir():
        live = [h.name for h in hooks.iterdir()
                if h.is_file() and not h.name.endswith(".sample")
                and os.access(h, os.X_OK)]
        if live:
            raise GateError(f"the clone has executable git hooks: {', '.join(live)}")

    # A pushurl is REJECTED, not quietly repaired: a clone that grew one
    # since we made it is a clone something else has been writing to, and
    # continuing would be treating an intrusion as a formatting problem.
    existing_push = _git(clone, "config", "--local", "--get-all",
                         "remote.origin.pushurl", timeout=git_t,
                         check=False, ctx=ctx).strip()
    if existing_push:
        raise GateError(f"the clone has a remote.origin.pushurl "
                        f"({existing_push.splitlines()[0][:80]}) — refusing to "
                        f"push anywhere but {canonical}")

    raw = _git(clone, "config", "--local", "--list", timeout=git_t,
               check=False, ctx=ctx)
    for line in raw.splitlines():
        key = line.split("=", 1)[0].strip().lower()
        if not key or key in ALLOWED_CONFIG_KEYS:
            continue
        if key.startswith("branch.") and key.endswith((".remote", ".merge")):
            continue
        if key.startswith(FORBIDDEN_CONFIG_PREFIXES):
            raise GateError(f"the clone carries an unexpected git config key: "
                            f"{key}")

    _git(clone, "remote", "set-url", "origin", canonical, timeout=git_t,
         ctx=ctx)
    fetch_url = _git(clone, "remote", "get-url", "origin", timeout=git_t,
                     ctx=ctx).strip()
    push_url = _git(clone, "remote", "get-url", "--push", "origin",
                    timeout=git_t, ctx=ctx).strip()
    if fetch_url != canonical or push_url != canonical:
        raise GateError(f"origin points at {fetch_url!r}/{push_url!r}, expected "
                        f"{canonical!r}")
    return canonical


def validate_next_state(path, wcfg, expected_cycle, slug):
    """The private next-state file, checked before it can become the ledger."""
    p = Path(path)
    if not p.exists():
        raise GateError("the model wrote no state-out.json")
    if p.stat().st_size > int(wcfg.get("max_state_bytes", 262144)):
        raise GateError("state-out.json is implausibly large")
    try:
        state = json.loads(p.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, UnicodeDecodeError) as e:
        raise GateError(f"state-out.json is not valid json: {e}")
    if not isinstance(state, dict):
        raise GateError("state-out.json is not an object")
    if state.get("cycle") != expected_cycle:
        raise GateError(f"state-out.cycle is {state.get('cycle')!r}, expected "
                        f"{expected_cycle}")
    if state.get("last_slug") != slug:
        raise GateError(f"state-out.last_slug is {state.get('last_slug')!r}, "
                        f"expected {slug!r}")
    return state


# ── the controller: git and GitHub are ours, not the model's ────────────────

def probe_health(health, phase, wcfg):
    """Run a health probe and treat any failure to produce one as a stop.

    A probe that raises used to escape publish() entirely and unwind through
    whatever caught RuntimeError next — which is not "the estate is fine", it
    is "we never asked". An unanswered question is not a yes (#5).
    """
    try:
        verdict = health(phase)
    except BaseException as e:                # including KeyboardInterrupt
        raise AbortError(f"health probe at {phase} raised "
                         f"{type(e).__name__}: {str(e)[:160]}")
    ok, why = health_gate(wcfg, verdict, phase=phase)
    if not ok:
        raise AbortError(why)
    return verdict


def verify_staged_tree(clone, submission, wcfg, paths, ctx=None):
    """What git will actually push, checked before anything leaves the machine.

    The working tree was gated; the INDEX is what gets pushed, and they are
    not the same object. A symlink or a mode-100755 blob or a different set of
    bytes can sit in the index while the files on disk look right, and
    post-merge verification would then be confirming something a human already
    merged (#3).
    """
    ctx = ctx or controller_for(wcfg)
    git_t = int(wcfg.get("git_timeout_s", 600))
    staged = []
    for line in _git(clone, "ls-files", "--stage", "--",
                     f"submissions/{submission['slug']}",
                     timeout=git_t, ctx=ctx).splitlines():
        if not line.strip():
            continue
        info, _, path = line.partition("\t")
        mode, blob, stage = (info.split() + ["", "", ""])[:3]
        staged.append((mode, blob, stage, path))
    if sorted(p for _, _, _, p in staged) != paths:
        raise GateError(f"the index holds {[p for _, _, _, p in staged]}, "
                        f"expected exactly {paths}")
    digests = {submission["meta_path"]: submission["meta_sha256"],
               submission["piece_path"]: submission["piece_sha256"]}
    for mode, blob, stage, path in staged:
        if mode != "100644":
            raise GateError(f"{path} is staged with mode {mode}, expected "
                            f"100644 (a symlink stages as 120000)")
        if stage != "0":
            raise GateError(f"{path} is staged unmerged (stage {stage})")
        kind = _git(clone, "cat-file", "-t", blob, timeout=git_t,
                    ctx=ctx).strip()
        if kind != "blob":
            raise GateError(f"{path} points at a {kind}, not a blob")
        body = _git_bytes(clone, "cat-file", "blob", blob, timeout=git_t,
                          ctx=ctx)
        if hashlib.sha256(body).hexdigest() != digests[path]:
            raise GateError(f"{path} in the index is not the file that passed "
                            f"the gate")
    return {path: blob for _, blob, _, path in staged}


def verify_staged_worktree_scope(clone, submission, wcfg, paths, ctx=None):
    """Require the validator's working tree to match the staged candidate."""
    ctx = ctx or controller_for(wcfg)
    git_t = int(wcfg.get("git_timeout_s", 600))
    changes = working_tree_changes(clone, timeout=git_t, ctx=ctx)
    actual = []
    for code, path, origin in changes:
        if code != "A " or origin is not None:
            raise GateError(
                f"the staged validator tree has an unexpected change: "
                f"{code} {path}"
                + (f" (from {origin})" if origin else "")
            )
        actual.append(path)
    if sorted(actual) != paths:
        raise GateError(
            f"the staged validator tree holds {sorted(actual)}, expected "
            f"exactly {paths}"
        )
    for rel, digest in (
        (submission["meta_path"], submission["meta_sha256"]),
        (submission["piece_path"], submission["piece_sha256"]),
    ):
        path = Path(clone) / rel
        _regular_file(path, rel)
        if hashlib.sha256(path.read_bytes()).hexdigest() != digest:
            raise GateError(
                f"{rel} in the validator working tree is not the file "
                "that passed the gate"
            )


def validate_staged_collective_candidate(clone, wcfg, ctx=None):
    """Run canonical target-main validation against the staged working tree."""
    ctx = ctx or controller_for(wcfg)
    clone = Path(clone)
    base = wcfg.get("base_branch", "main")
    git_t = int(wcfg.get("git_timeout_s", 600))
    validator = clone / COLLECTIVE_VALIDATOR_PATH
    listing = [
        line for line in _git(
            clone,
            "ls-tree",
            f"origin/{base}",
            "--",
            COLLECTIVE_VALIDATOR_PATH,
            timeout=git_t,
            ctx=ctx,
        ).splitlines()
        if line.strip()
    ]
    if len(listing) != 1:
        raise GateError(
            f"canonical origin/{base} is missing "
            f"{COLLECTIVE_VALIDATOR_PATH}"
        )
    info, separator, path = listing[0].partition("\t")
    fields = info.split()
    if (
        not separator
        or path != COLLECTIVE_VALIDATOR_PATH
        or len(fields) != 3
        or fields[0] != "100644"
        or fields[1] != "blob"
    ):
        raise GateError(
            f"canonical origin/{base} has an unsafe "
            f"{COLLECTIVE_VALIDATOR_PATH}"
        )
    _regular_file(validator, COLLECTIVE_VALIDATOR_PATH)
    trusted = _git_bytes(
        clone, "cat-file", "blob", fields[2], timeout=git_t, ctx=ctx
    )
    if validator.read_bytes() != trusted:
        raise GateError(
            f"{COLLECTIVE_VALIDATOR_PATH} does not match canonical "
            f"origin/{base}"
        )
    try:
        result = subprocess.run(
            [sys.executable, str(validator), "--validate"],
            cwd=str(clone),
            env=ctx.git_env,
            capture_output=True,
            text=True,
            timeout=COLLECTIVE_VALIDATOR_TIMEOUT_S,
        )
    except subprocess.TimeoutExpired as exc:
        raise GateError(
            "canonical Public Art Collective validation timed out after "
            f"{COLLECTIVE_VALIDATOR_TIMEOUT_S}s"
        ) from exc
    except OSError as exc:
        raise GateError(
            f"canonical Public Art Collective validator could not run: {exc}"
        ) from exc
    if result.returncode != 0:
        detail = (result.stderr or result.stdout or "").strip()[:600]
        raise GateError(
            "canonical Public Art Collective validation rejected the staged "
            f"candidate (exit {result.returncode})"
            + (f": {detail}" if detail else "")
        )
    return (result.stdout or "").strip()


def _requires_collective_provenance(profile_snapshot):
    return bool(
        isinstance(profile_snapshot, dict)
        and profile_snapshot.get("profile") == AZURE_REVIEWED_PNG_PROFILE
    )


def _provenance_poll_settings(wcfg):
    """Reuse the bounded, operator-configured publication probe cadence."""
    try:
        attempts = int(wcfg.get("view_probe_attempts", 5))
    except (TypeError, ValueError) as exc:
        raise GateError("view_probe_attempts must be an integer") from exc
    if not 1 <= attempts <= 12:
        raise GateError("view_probe_attempts must be between 1 and 12")
    raw_backoff = wcfg.get("view_probe_backoff") or (5, 10, 20, 30)
    if not isinstance(raw_backoff, (list, tuple)) or not raw_backoff:
        raise GateError("view_probe_backoff must be a non-empty array")
    try:
        backoff = [float(value) for value in raw_backoff]
    except (TypeError, ValueError) as exc:
        raise GateError("view_probe_backoff must contain numbers") from exc
    if any(value < 0 or value > 300 for value in backoff):
        raise GateError(
            "view_probe_backoff values must be between 0 and 300 seconds")
    return attempts, backoff


def _provenance_first_seen(value):
    if not isinstance(value, str) or not value:
        raise GateError("provenance check has no durable first-seen timestamp")
    try:
        stamp = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise GateError(
            "provenance check has an invalid durable first-seen timestamp") from exc
    if stamp.tzinfo is None:
        raise GateError(
            "provenance check first-seen timestamp must include a timezone")
    return stamp


def _provenance_grace_expired(first_seen, wcfg):
    try:
        grace = int(wcfg.get("provenance_absent_grace_s", 300))
    except (TypeError, ValueError) as exc:
        raise GateError("provenance_absent_grace_s must be an integer") from exc
    if not 0 <= grace <= 3600:
        raise GateError(
            "provenance_absent_grace_s must be between 0 and 3600 seconds")
    elapsed = (sentinel.now() - _provenance_first_seen(first_seen)).total_seconds()
    # A future clock must not grant an unlimited grace period.
    if elapsed < -60:
        raise GateError("provenance check first-seen timestamp is in the future")
    return elapsed >= grace


def _provenance_absence_expired(first_seen, wcfg):
    return _provenance_grace_expired(first_seen, wcfg)


def _is_exact_collective_provenance_check(check):
    if check.get("__typename") not in (None, "CheckRun"):
        return False
    if check.get("name") != COLLECTIVE_PROVENANCE_CHECK:
        return False
    for field in ("workflowName", "workflow"):
        if (field in check
                and check[field] != COLLECTIVE_PROVENANCE_WORKFLOW):
            return False
    return True


def _collective_pr_rollup(pr_number, wcfg, ctx=None):
    """Read the PR and its rollup through an exit-zero gh command."""
    ctx = ctx or controller_for(wcfg)
    repo = ctx.gh_repo()
    gh_t = int(wcfg.get("gh_timeout_s", 300))
    try:
        raw = _gh(
            "pr", "view", str(pr_number), "--repo", repo,
            "--json", PROVENANCE_ROLLUP_JSON_FIELDS,
            timeout=gh_t, ctx=ctx)
        value = json.loads(raw)
    except (CommandError, subprocess.TimeoutExpired, json.JSONDecodeError,
            TypeError, ValueError) as exc:
        raise CommandError(
            f"could not inspect {COLLECTIVE_PROVENANCE_FULL_NAME}: {exc}") from exc
    if not isinstance(value, dict):
        raise CommandError("gh pr view returned a non-object rollup")
    checks = value.get("statusCheckRollup")
    if not isinstance(checks, list):
        raise CommandError(
            "gh pr view returned no statusCheckRollup array")
    return value


def _classify_collective_provenance_rollup(value):
    """Classify only Collective's exact protected provenance CheckRun."""
    checks = value["statusCheckRollup"]
    matches = []
    for check in checks:
        if not isinstance(check, dict):
            raise CommandError(
                "gh pr view returned a non-object status check")
        if _is_exact_collective_provenance_check(check):
            status = check.get("status")
            conclusion = check.get("conclusion")
            if not isinstance(status, str) or not status.strip():
                raise CommandError(
                    "exact provenance CheckRun returned no usable status")
            status = status.strip().upper()
            if conclusion is not None and not isinstance(conclusion, str):
                raise CommandError(
                    "exact provenance CheckRun returned a non-string conclusion")
            conclusion = (
                conclusion.strip().upper()
                if isinstance(conclusion, str) and conclusion.strip() else "")
            if status in PROVENANCE_PENDING_STATUSES:
                if conclusion:
                    raise CommandError(
                        "pending provenance CheckRun returned a conclusion")
                classification = "pending"
            elif status == "COMPLETED":
                if conclusion == "SUCCESS":
                    classification = "success"
                elif conclusion == "CANCELLED":
                    classification = "cancelled"
                elif conclusion in PROVENANCE_FAILURE_CONCLUSIONS:
                    classification = "failure"
                elif not conclusion:
                    raise CommandError(
                        "completed provenance CheckRun returned no conclusion")
                else:
                    raise CommandError(
                        "completed provenance CheckRun returned unknown "
                        f"conclusion {conclusion!r}")
            else:
                raise CommandError(
                    "exact provenance CheckRun returned unknown status "
                    f"{status!r}")
            matches.append({
                "status": status,
                "conclusion": conclusion,
                "classification": classification,
            })
    if not matches:
        return {
            "classification": "absent",
            "present": False,
            "states": [],
            "detail": f"{COLLECTIVE_PROVENANCE_FULL_NAME} is absent",
            "pull_request": value,
        }
    classifications = {match["classification"] for match in matches}
    if "failure" in classifications:
        classification = "failure"
    elif "pending" in classifications:
        # cancel-in-progress can briefly expose both the cancelled old run and
        # its queued replacement. The replacement is actionable, not failure.
        classification = "pending"
    elif "success" in classifications:
        classification = "success"
    else:
        classification = "cancelled"
    states = [
        f"{match['status']}/{match['conclusion'] or '-'}"
        for match in matches
    ]
    return {
        "classification": classification,
        "present": True,
        "states": states,
        "detail": (
            f"{COLLECTIVE_PROVENANCE_FULL_NAME}: "
            + ", ".join(states)
        ),
        "pull_request": value,
    }


def collective_provenance_check_state(pr_number, wcfg, ctx=None):
    """Read and classify only Collective's exact protected provenance check.

    `gh pr view --json statusCheckRollup,...` exits zero while checks are
    pending or failed. CheckRun entries use `name`, `workflowName`, `status`,
    and `conclusion`; StatusContext entries are unrelated and ignored. A
    similarly named check can never turn an absent required check into success.
    """
    return _classify_collective_provenance_rollup(
        _collective_pr_rollup(pr_number, wcfg, ctx))


def wait_for_collective_provenance(pr_number, wcfg, first_seen, transaction,
                                   ctx=None, sleep=None,
                                   cancelled_first_seen=None):
    """Poll the exact required check without silently converting absence to OK."""
    attempts, backoff = _provenance_poll_settings(wcfg)
    sleep = sleep or time.sleep
    for attempt in range(attempts):
        try:
            result = collective_provenance_check_state(pr_number, wcfg, ctx)
        except (CommandError, subprocess.TimeoutExpired, json.JSONDecodeError,
                TypeError, ValueError) as exc:
            detail = (
                f"provenance check inspection failed: "
                f"{type(exc).__name__}: {str(exc)[:300]}")
            transaction(
                phase="checks-pending",
                provenance_check_state="error",
                provenance_check_detail=detail,
                provenance_first_seen_at=first_seen,
            )
            raise ChecksPending(detail) from exc
        classification = result["classification"]
        if classification == "cancelled":
            cancelled_first_seen = (
                cancelled_first_seen
                or sentinel.now().isoformat(timespec="seconds")
            )
        elif classification != "absent":
            cancelled_first_seen = ""
        transaction(
            phase="checks-pending",
            provenance_check_state=classification,
            provenance_check_detail=result["detail"][:400],
            provenance_check_states=result["states"],
            provenance_first_seen_at=first_seen,
            provenance_cancelled_first_seen_at=cancelled_first_seen,
        )
        if classification == "success":
            return result
        if classification == "failure":
            raise ProvenanceCheckFailed(
                f"required provenance check failed: {result['detail']}")
        if (classification == "cancelled"
                and _provenance_grace_expired(
                    cancelled_first_seen, wcfg)):
            raise ProvenanceCheckFailed(
                f"required provenance check remained cancelled beyond its "
                f"bounded replacement grace period: {result['detail']}")
        absence_first_seen = cancelled_first_seen or first_seen
        if (classification == "absent"
                and _provenance_absence_expired(
                    absence_first_seen, wcfg)):
            raise ProvenanceCheckFailed(
                f"required provenance check never appeared within its "
                f"bounded grace period: {result['detail']}")
        if attempt + 1 < attempts:
            sleep(backoff[min(attempt, len(backoff) - 1)])
    raise ChecksPending(
        f"required provenance check is still pending after {attempts} "
        f"bounded inspection(s)")


def collective_merge_readiness(pr_number, wcfg, ctx=None):
    """Re-read the PR merge gate after the provenance check has succeeded."""
    try:
        value = _collective_pr_rollup(pr_number, wcfg, ctx)
    except CommandError as exc:
        raise ChecksPending(
            f"could not inspect required PR merge state: "
            f"{type(exc).__name__}: {str(exc)[:300]}") from exc
    state = str(value.get("state") or "").upper()
    merge_state = str(value.get("mergeStateStatus") or "").upper()
    if state == "MERGED":
        return "merged"
    if state != "OPEN":
        raise ProvenanceCheckFailed(
            f"PR {pr_number} is {state or 'missing-state'}, not an open "
            "merge candidate")
    fresh_provenance = _classify_collective_provenance_rollup(value)
    if fresh_provenance["classification"] == "failure":
        raise ProvenanceCheckFailed(
            "required provenance check failed in the fresh merge rollup: "
            f"{fresh_provenance['detail']}")
    if fresh_provenance["classification"] != "success":
        raise ChecksPending(
            "required provenance check changed before merge readiness: "
            f"{fresh_provenance['detail']}")
    if merge_state in (
            "", "UNKNOWN", "UNSTABLE", "BLOCKED", "BEHIND"):
        raise ChecksPending(
            f"PR {pr_number} merge state is still pending "
            f"(mergeStateStatus={merge_state or 'missing'})")
    if merge_state != "CLEAN":
        raise ProvenanceCheckFailed(
            f"PR {pr_number} is not eligible for the required merge "
            f"(mergeStateStatus={merge_state or 'missing'})")
    return "ready"


def verify_collective_pr_scope(pr_number, branch, paths, wcfg, ctx=None):
    """Re-read GitHub's PR file scope before a resumed protected merge."""
    if (not isinstance(branch, str) or not branch
            or not paths or any(not isinstance(path, str) or not path
                                for path in paths)):
        raise GateError("protected PR lacks a durable branch or exact file scope")
    ctx = ctx or controller_for(wcfg)
    repo = ctx.gh_repo()
    base = wcfg.get("base_branch", "main")
    gh_t = int(wcfg.get("gh_timeout_s", 300))
    try:
        raw = _gh(
            "pr", "view", str(pr_number), "--repo", repo, "--json",
            "files,state,baseRefName,headRefName,isCrossRepository",
            timeout=gh_t, ctx=ctx)
        view = json.loads(raw)
    except (CommandError, subprocess.TimeoutExpired, json.JSONDecodeError,
            TypeError, ValueError) as exc:
        raise ChecksPending(
            f"could not re-read protected PR file scope: "
            f"{type(exc).__name__}: {str(exc)[:300]}") from exc
    if not isinstance(view, dict):
        raise ChecksPending("protected PR scope inspection returned a non-object")
    state = str(view.get("state") or "").upper()
    if state == "MERGED":
        return "merged"
    if state != "OPEN":
        raise ProvenanceCheckFailed(
            f"protected PR {pr_number} is {state or 'missing-state'}")
    files = view.get("files")
    if not isinstance(files, list):
        raise ChecksPending("protected PR scope inspection returned no file list")
    remote_paths = sorted(
        item.get("path") for item in files if isinstance(item, dict))
    if len(remote_paths) != len(files) or remote_paths != sorted(paths):
        raise ProvenanceCheckFailed(
            f"protected PR touches {remote_paths}, expected {sorted(paths)}")
    if any(int(item.get("deletions") or 0)
           for item in files if isinstance(item, dict)):
        raise ProvenanceCheckFailed(
            "protected PR deletes lines from an existing file")
    if view.get("baseRefName") != base or view.get("headRefName") != branch:
        raise ProvenanceCheckFailed(
            "protected PR branch or base conflicts with its durable transaction")
    return "open"


def _merge_collective_pr(clone, submission, wcfg, pr_number, paths, note,
                         branch="", commit="", pr_url="", ctx=None):
    """Invoke the canonical merge once; every later error is ambiguous."""
    ctx = ctx or controller_for(wcfg)
    repo = ctx.gh_repo()
    gh_t = int(wcfg.get("gh_timeout_s", 300))
    try:
        _gh("pr", "merge", str(pr_number), "--repo", repo, "--squash",
            "--delete-branch", timeout=gh_t, ctx=ctx)
        note(phase="merge-called", pr_number=str(pr_number))
        receipts = confirm_merge(
            clone, submission, wcfg, pr_number, paths, ctx)
        receipts.update({
            "branch": branch,
            "commit": commit,
            "pr_url": pr_url,
            "pr_number": str(pr_number),
        })
        note(phase="merged", **receipts)
        return receipts
    except (KeyboardInterrupt, SystemExit):
        raise
    except Exception as exc:
        detail = f"{type(exc).__name__}: {str(exc)[:300]}"
        try:
            note(phase="merge-ambiguous", last_merge_error=detail)
        except Exception as note_error:
            log("could not persist merge-ambiguous phase: "
                f"{type(note_error).__name__}: {note_error}")
        raise MergeAmbiguous(
            f"canonical merge invocation is ambiguous: {detail}") from exc


def _close_failed_provenance_pr(clone, submission, wcfg, pr_number, paths,
                                ctx=None):
    """Close a known-rejected PR only after proving it did not land."""
    try:
        closed = prove_non_merge(
            clone, submission, wcfg, pr_number, paths, ctx)
    except (CommandError, subprocess.TimeoutExpired, json.JSONDecodeError,
            KeyError, OSError, ValueError, GateError) as exc:
        raise ChecksPending(
            f"required provenance check failed but its PR could not be "
            f"closed and verified: {type(exc).__name__}: {str(exc)[:300]}") from exc
    if not closed:
        raise MergeAmbiguous(
            "required provenance check failed, but public state does not "
            "prove that the PR remained unmerged")
    return True


def publish(clone, submission, wcfg, health, branch=None, transaction=None,
            ctx=None, profile_snapshot=None):
    """Branch, commit, PR, verify, re-check health, merge, re-read main.

    Every remote step is preceded by a health run and followed by evidence
    read back from GitHub. Returns a dict of receipts; raises AbortError,
    GateError or CommandError. Before merge invocation, any abandoned PR is
    closed. After invocation, every timeout/error is ambiguous and the durable
    transaction is retained for public-state reconciliation.

    `transaction` is a callable the caller supplies to persist each step, so
    a process killed between the merge call and the ledger write can be
    reconciled on the next pass instead of losing a merged submission.
    """
    ctx = ctx or controller_for(wcfg)
    clone = Path(clone)
    repo = ctx.gh_repo()
    base = wcfg.get("base_branch", "main")
    git_t = int(wcfg.get("git_timeout_s", 600))
    gh_t = int(wcfg.get("gh_timeout_s", 300))
    slug = submission["slug"]
    requires_provenance = _requires_collective_provenance(profile_snapshot)
    if requires_provenance:
        enforce_reviewed_controller_contract(wcfg, profile_snapshot)
        expected_branch = re.compile(
            rf"^{re.escape(REVIEWED_PNG_BRANCH_PREFIX)}/"
            rf"{re.escape(slug)}-[0-9a-f]{{8}}$")
        branch = branch or (
            f"{REVIEWED_PNG_BRANCH_PREFIX}/{slug}-{uuid.uuid4().hex[:8]}")
        if not expected_branch.fullmatch(branch):
            raise GateError(
                "reviewed-PNG branch must be exactly "
                f"{REVIEWED_PNG_BRANCH_PREFIX}/{slug}-<8 lowercase hex>")
    else:
        branch = branch or (
            f"{wcfg.get('branch_prefix', 'art')}/"
            f"{slug}-{uuid.uuid4().hex[:8]}")
    paths = sorted([submission["meta_path"], submission["piece_path"]])
    note = transaction or (lambda **_: None)

    # Health immediately before the FIRST remote write (#3). Not the health we
    # ran before the model: that reading is up to half an hour old by now, and
    # the estate is exactly what may have changed while the model thought.
    probe_health(health, "pre-write", wcfg)
    assert_repo_integrity(clone, wcfg, ctx=ctx)

    _git_remote(clone, wcfg, "fetch", "--no-tags", "origin", base,
                timeout=git_t, ctx=ctx)
    _git(clone, "checkout", "-b", branch, f"origin/{base}", timeout=git_t,
         ctx=ctx)
    still_new = _git(clone, "ls-tree", "--name-only", f"origin/{base}",
                     f"submissions/{slug}/", timeout=git_t, ctx=ctx).strip()
    if still_new:
        raise GateError(f"slug {slug!r} appeared on origin/{base} while we worked")

    _git(clone, "add", "--", f"submissions/{slug}", timeout=git_t, ctx=ctx)
    staged = [ln.split("\t") for ln in
              _git(clone, "diff", "--cached", "--name-status",
                   timeout=git_t, ctx=ctx).splitlines() if ln.strip()]
    if sorted(p for _, p in staged) != paths or any(s != "A" for s, _ in staged):
        raise GateError(f"staged set is {staged}, expected exactly two additions "
                        f"{paths}")
    blobs = verify_staged_tree(clone, submission, wcfg, paths, ctx)
    verify_staged_worktree_scope(
        clone, submission, wcfg, paths, ctx
    )
    validate_staged_collective_candidate(clone, wcfg, ctx)
    verify_staged_worktree_scope(
        clone, submission, wcfg, paths, ctx
    )
    verified_again = verify_staged_tree(
        clone, submission, wcfg, paths, ctx
    )
    if verified_again != blobs:
        raise GateError(
            "the staged candidate changed during Collective validation"
        )

    role = str(wcfg.get("role") or "evolve")
    if requires_provenance and not REVIEWED_PNG_ROLE_RE.fullmatch(role):
        raise GateError(
            "reviewed-PNG controller role must be a clean 1-64 character name")
    collective = (
        REVIEWED_PNG_COMMIT_NAME if requires_provenance
        else wcfg.get("instance_name", "this collective"))
    provenance = (
        REVIEWED_PNG_COMMIT_BODY_TEMPLATE.format(role=role)
        if requires_provenance
        else f"Autonomous submission by the {role} neighbor of {collective}.")
    message = (
        f"art: {submission['title']} ({slug})\n\n"
        f"{provenance}\n"
        f"Dada cycle {submission['meta']['_dada_cycle']['cycle']}, "
        f"{len(submission['meta']['_dada_cycle']['rounds'])} round(s) of "
        f"{CANDIDATES_PER_ROUND} candidates.\n")
    if requires_provenance:
        commit_date = sentinel.now().isoformat(timespec="seconds")
        commit_env = {
            **ctx.git_env,
            "GIT_AUTHOR_NAME": REVIEWED_PNG_COMMIT_NAME,
            "GIT_AUTHOR_EMAIL": REVIEWED_PNG_COMMIT_EMAIL,
            "GIT_AUTHOR_DATE": commit_date,
            "GIT_COMMITTER_NAME": REVIEWED_PNG_COMMIT_NAME,
            "GIT_COMMITTER_EMAIL": REVIEWED_PNG_COMMIT_EMAIL,
            "GIT_COMMITTER_DATE": commit_date,
        }
        _git(
            clone, "commit", "-m", message, timeout=git_t,
            env=commit_env, ctx=ctx)
    else:
        _git(clone, "-c", f"user.name={wcfg['git_author_name']}",
             "-c", f"user.email={wcfg['git_author_email']}",
             "commit", "-m", message, timeout=git_t, ctx=ctx)
    head = _git(clone, "rev-parse", "HEAD", timeout=git_t, ctx=ctx).strip()
    note(phase="committed", branch=branch, commit=head, blobs=blobs, paths=paths)
    # The last thing before bytes leave this machine: the chokepoint proves
    # the remote is still the one we configured, not one somebody wrote into
    # .git after the last check.
    _git_remote(clone, wcfg, "push", "--set-upstream", "origin", branch,
                timeout=git_t, ctx=ctx)
    note(phase="pushed", branch=branch, commit=head)

    pr_url, pr_number = "", ""
    try:
        body = _pr_body(submission, wcfg)
        pr_url = _gh("pr", "create", "--repo", repo, "--base", base,
                     "--head", branch, "--title",
                     f"art: {submission['title']} ({slug})",
                     "--body", body, timeout=gh_t,
                     ctx=ctx).strip().splitlines()[-1].strip()
        pr_number = pr_url.rstrip("/").split("/")[-1]
        first_seen = (
            sentinel.now().isoformat(timespec="seconds")
            if requires_provenance else None
        )
        note(
            phase="pr-open", pr_url=pr_url, pr_number=pr_number,
            provenance_first_seen_at=first_seen,
        )

        # What GitHub says the PR contains, not what we think we pushed.
        view = json.loads(_gh("pr", "view", pr_number, "--repo", repo, "--json",
                              "files,state,baseRefName,headRefName,isCrossRepository",
                              timeout=gh_t, ctx=ctx))
        remote_paths = sorted(f.get("path") for f in view.get("files", []))
        if remote_paths != paths:
            raise GateError(f"the PR touches {remote_paths}, expected {paths}")
        if any(int(f.get("deletions") or 0) for f in view.get("files", [])):
            raise GateError("the PR deletes lines from an existing file")
        if view.get("baseRefName") != base or view.get("headRefName") != branch:
            raise GateError(f"the PR targets {view.get('baseRefName')!r} from "
                            f"{view.get('headRefName')!r}, expected {base!r} from "
                            f"{branch!r}")

        if requires_provenance:
            wait_for_collective_provenance(
                pr_number, wcfg, first_seen, note, ctx)
            merge_state = collective_merge_readiness(
                pr_number, wcfg, ctx)
            if merge_state == "merged":
                # The controller did not invoke a merge, so another actor may
                # have raced it. Reconcile fresh public state rather than
                # treating that as permission to run a second merge command.
                note(
                    phase="merge-ambiguous",
                    last_merge_error=(
                        "PR reported merged before the controller invoked "
                        "its canonical merge"),
                )
                raise MergeAmbiguous(
                    "PR reported merged before the controller invoked its "
                    "canonical merge")
            note(
                phase="provenance-succeeded",
                provenance_check_state="success",
            )

        # Health again, immediately before the merge (#3/#6).
        probe_health(health, "pre-merge", wcfg)

        note(phase="merging", pr_url=pr_url, pr_number=pr_number)
    except ChecksPending:
        # PR identity and every observed check state are already durable. A
        # later worker pass resumes this exact PR instead of spending again.
        raise
    except ProvenanceCheckFailed:
        try:
            _close_failed_provenance_pr(
                clone, submission, wcfg, pr_number, paths, ctx)
        except ChecksPending:
            note(phase="checks-pending")
            raise
        except MergeAmbiguous as exc:
            note(phase="merge-ambiguous", last_merge_error=str(exc)[:400])
            raise
        note(phase="checks-failed-closed")
        raise
    except MergeAmbiguous:
        # A PR already reported merged before our call is public-state
        # ambiguity, not an abandoned branch to close.
        raise
    except BaseException:
        # The merge command has not been invoked yet, so cleanup is still
        # unambiguous.
        if pr_number:
            _close_pr(repo, pr_number, gh_t, ctx)
        else:
            _delete_remote_branch(clone, branch, wcfg, git_t, ctx)
        note(phase="cleaned-up")
        raise

    # From this instruction onward, a timeout or exception cannot prove that
    # GitHub did not merge. Never close, clear, or spend again on that guess.
    return _merge_collective_pr(
        clone, submission, wcfg, pr_number, paths, note,
        branch=branch, commit=head, pr_url=pr_url, ctx=ctx)


def confirm_merge(clone, submission, wcfg, pr_number, paths, ctx=None):
    """Evidence, freshly re-read: merged state, merge commit, its file scope,
    and the bytes now living on the base branch (R1). Used both by publish()
    and by reconciliation of a cycle that died mid-flight."""
    ctx = ctx or controller_for(wcfg)
    clone = Path(clone)
    repo = ctx.gh_repo()
    base = wcfg.get("base_branch", "main")
    git_t = int(wcfg.get("git_timeout_s", 600))
    gh_t = int(wcfg.get("gh_timeout_s", 300))

    merged = json.loads(_gh("pr", "view", str(pr_number), "--repo", repo,
                            "--json", "state,mergeCommit",
                            timeout=gh_t, ctx=ctx))
    if str(merged.get("state")).upper() != "MERGED":
        raise MergeNotConfirmed(pr_number, merged.get("state"))
    merge_sha = ((merged.get("mergeCommit") or {}).get("oid") or "").strip()
    if not merge_sha:
        raise CommandError(f"PR {pr_number} reports no merge commit")

    _git_remote(clone, wcfg, "fetch", "--no-tags", "origin", base,
                timeout=git_t, ctx=ctx)
    main_sha = _git(clone, "rev-parse", f"origin/{base}", timeout=git_t,
                    ctx=ctx).strip()
    _git(clone, "merge-base", "--is-ancestor", merge_sha, f"origin/{base}",
         timeout=git_t, ctx=ctx)
    touched = [ln.split("\t") for ln in
               _git(clone, "show", "--name-status", "--format=", merge_sha,
                    timeout=git_t, ctx=ctx).splitlines() if ln.strip()]
    if sorted(p for _, p in touched) != paths or any(s != "A" for s, _ in touched):
        raise GateError(f"the merge commit touches {touched}, expected exactly "
                        f"two additions {paths}")
    for path, digest in ((submission["meta_path"], submission["meta_sha256"]),
                         (submission["piece_path"], submission["piece_sha256"])):
        blob = _git_bytes(clone, "cat-file", "blob", f"origin/{base}:{path}",
                          timeout=git_t, ctx=ctx)
        if hashlib.sha256(blob).hexdigest() != digest:
            raise GateError(f"{path} on origin/{base} is not the file we gated")
    return {"merge_commit": merge_sha, "base_sha": main_sha, "paths": paths}


def prove_non_merge(clone, submission, wcfg, pr_number, paths, ctx=None):
    """Close an open PR and prove both PR state and main exclude this change."""
    expected_paths = (
        sorted([submission.get("meta_path"), submission.get("piece_path")])
        if isinstance(submission, dict) else [])
    if (not paths or any(not isinstance(path, str) or not path for path in paths)
            or sorted(paths) != expected_paths):
        raise GateError(
            "cannot prove non-merge without the submission's exact paths")
    ctx = ctx or controller_for(wcfg)
    clone = Path(clone)
    repo = ctx.gh_repo()
    base = wcfg.get("base_branch", "main")
    git_t = int(wcfg.get("git_timeout_s", 600))
    gh_t = int(wcfg.get("gh_timeout_s", 300))

    def read_pr():
        value = json.loads(_gh(
            "pr", "view", str(pr_number), "--repo", repo,
            "--json", "state,mergeCommit", timeout=gh_t, ctx=ctx))
        return (
            str(value.get("state") or "").upper(),
            str((value.get("mergeCommit") or {}).get("oid") or "").strip(),
        )

    state, merge_sha = read_pr()
    if state == "MERGED" or merge_sha:
        return False
    if state == "OPEN":
        _gh("pr", "close", str(pr_number), "--repo", repo,
            "--delete-branch", timeout=gh_t, ctx=ctx)
        state, merge_sha = read_pr()
    if state != "CLOSED" or merge_sha:
        return False

    _git_remote(clone, wcfg, "fetch", "--no-tags", "origin", base,
                timeout=git_t, ctx=ctx)
    present = {
        line.strip() for line in _git(
            clone, "ls-tree", "-r", "--name-only", f"origin/{base}",
            "--", *paths, timeout=git_t, ctx=ctx).splitlines()
        if line.strip()
    }
    if present:
        return False
    return True


def revalidate_merged_profile_submission(clone, submission, wcfg,
                                         profile_snapshot, ctx=None):
    """Re-read and deterministically gate profile bytes already on base."""
    snapshot = validate_profile_snapshot(profile_snapshot)
    if not isinstance(submission, dict):
        raise GateError("profile transaction submission is not an object")
    required = {
        "slug", "kind", "meta", "meta_path", "piece_path",
        "meta_sha256", "piece_sha256",
    }
    if not required.issubset(submission):
        raise GateError("profile transaction submission is incomplete")
    slug = submission.get("slug")
    if (not isinstance(slug, str) or not SLUG_RE.fullmatch(slug)
            or submission.get("kind") != "png"
            or submission.get("meta_path") !=
            f"submissions/{slug}/{META_NAME}"
            or submission.get("piece_path") !=
            f"submissions/{slug}/piece.png"):
        raise GateError("profile transaction submission paths conflict")
    ctx = ctx or controller_for(wcfg)
    base = wcfg.get("base_branch", "main")
    git_t = int(wcfg.get("git_timeout_s", 600))
    meta_bytes = _git_bytes(
        clone, "cat-file", "blob",
        f"origin/{base}:{submission['meta_path']}", timeout=git_t, ctx=ctx)
    piece_bytes = _git_bytes(
        clone, "cat-file", "blob",
        f"origin/{base}:{submission['piece_path']}", timeout=git_t, ctx=ctx)
    if hashlib.sha256(meta_bytes).hexdigest() != submission["meta_sha256"]:
        raise GateError("merged profile meta digest conflicts with transaction")
    if hashlib.sha256(piece_bytes).hexdigest() != submission["piece_sha256"]:
        raise GateError("merged profile PNG digest conflicts with transaction")
    try:
        merged_meta = json.loads(meta_bytes.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise GateError(f"merged profile meta is invalid: {exc}") from exc
    if merged_meta != submission.get("meta"):
        raise GateError("merged profile meta conflicts with transaction state")
    if (not isinstance(merged_meta, dict)
            or merged_meta.get("slug") != slug
            or merged_meta.get("kind") != "png"):
        raise GateError("merged profile meta conflicts with submission identity")
    validate_reviewed_submission_contract(merged_meta, expected_slug=slug)
    image_info = validate_image_generation_receipt(
        merged_meta, piece_bytes, snapshot)
    captured = submission.get("image")
    expected_dimensions = {
        "width": image_info["width"], "height": image_info["height"]}
    if captured is not None and captured != expected_dimensions:
        raise GateError("profile transaction dimensions conflict with merged IHDR")
    return meta_bytes, piece_bytes, image_info


def _close_pr(repo, pr_number, timeout, ctx=None):
    if not pr_number:
        return
    try:
        _gh("pr", "close", str(pr_number), "--repo", repo, "--delete-branch",
            timeout=timeout, ctx=ctx)
        log(f"closed PR {pr_number} after aborting the cycle")
    except Exception as e:
        log(f"could not close PR {pr_number}: {type(e).__name__}: {e}")


def _delete_remote_branch(clone, branch, wcfg, timeout=None, ctx=None):
    """Remove a branch we pushed, from the canonical repo and nowhere else.

    Two paths, and neither can reach a remote we did not configure:

      * the clone still verifies -> delete through it, normally.
      * the clone does NOT verify (a pushurl appeared, a hostile config, a
        hook) -> do not touch that repository at all. Delete from a fresh,
        empty git directory with global and system config neutralised,
        addressing the canonical URL explicitly, then confirm with ls-remote.

    The second path exists because failing closed here would leave a real
    branch orphaned on the real origin as the price of someone else's
    tampering, and cleanup is exactly when the repository is least
    trustworthy.
    """
    timeout = int(timeout or wcfg.get("git_timeout_s", 600))
    try:
        ctx = ctx or controller_for(wcfg)
        canonical = ctx.repo.transport
    except (GateError, CommandError) as e:
        # Cleanup cannot invent a destination. Without a canonical repo there
        # is nothing safe to push a deletion to, so say so and stop.
        log(f"cannot delete branch {branch}: {type(e).__name__}: {e}")
        return False
    try:
        _git_remote(clone, wcfg, "push", "origin", "--delete", branch,
                    timeout=timeout, ctx=ctx)
        log(f"deleted the pushed branch {branch} after aborting the cycle")
        return True
    except GateError as e:
        log(f"the clone no longer verifies ({e}); deleting {branch} from "
            f"{canonical} through a sanitized repository instead")
    except Exception as e:
        log(f"could not delete branch {branch} via origin: "
            f"{type(e).__name__}: {e}")

    scratch = Path(clone).parent / f"cleanup-{uuid.uuid4().hex[:8]}"
    env = ctx.git_env
    try:
        scratch.mkdir(parents=True, exist_ok=True)
        _git(scratch, "init", "-q", timeout=timeout, env=env, ctx=ctx)
        # sanctioned-network-git: a fresh empty repo with no origin to
        # verify, addressing the validated canonical url explicitly, in the
        # sanitized environment. This is the path that exists BECAUSE the
        # clone could not be trusted.
        _git(scratch, "push", canonical, "--delete", branch, timeout=timeout,
             env=env, ctx=ctx)
        # sanctioned-network-git: read-back confirmation on the same
        # sanitized repo and the same validated url.
        left = _git(scratch, "ls-remote", "--heads", canonical, branch,
                    timeout=timeout, env=env, check=False, ctx=ctx)
        if left.strip():
            log(f"branch {branch} still exists on {canonical} after deletion")
            return False
        log(f"deleted {branch} from {canonical} through a sanitized repository")
        return True
    except Exception as e:
        log(f"sanitized deletion of {branch} failed: {type(e).__name__}: {e} — "
            f"failing closed, nothing was pushed anywhere else")
        return False
    finally:
        shutil.rmtree(scratch, ignore_errors=True)


def _pr_body(submission, wcfg):
    cycle = submission["meta"]["_dada_cycle"]
    return (
        f"Autonomous submission from the **{wcfg.get('role', 'evolve')}** neighbor "
        f"of {wcfg.get('instance_name', 'a RAPP sentinel')}, acting on its own "
        f"initiative through its operator's GitHub identity.\n\n"
        f"- slug: `{submission['slug']}`\n"
        f"- kind: `{submission['kind']}`\n"
        f"- dada cycle: {cycle['cycle']} "
        f"(previous: `{cycle['previous_slug']}`)\n"
        f"- search: {len(cycle['rounds'])} round(s) x {CANDIDATES_PER_ROUND} "
        f"candidates, scored on {', '.join(SCORE_DIMENSIONS)}\n\n"
        f"What this PR does NOT do: it touches no existing file. The model that "
        f"made the piece could not commit, push, open this PR or merge it — a "
        f"controller validated the working tree against the submission protocol "
        f"and owns every git and GitHub operation here.\n"
    )


def vision_urls(vcfg, submission):
    repo = normalize_repo(vcfg["repo"], vcfg)
    if not repo.owner or not repo.name:
        return {"watch_url": "", "channel_url": "", "media_url": "",
                "scene_url": "", "source_url": ""}
    root = f"https://{repo.owner.lower()}.github.io/{repo.name}"
    media_path = "/".join(
        quote(part, safe="") for part in vision_media_path(
            submission, vcfg).split("/"))
    channel_path = "/".join(
        quote(part, safe="") for part in vcfg["channel_path"].split("/"))
    return {
        "watch_url": (f"{vcfg['player_url']}/#/watch/"
                      f"{quote(submission['slug'], safe='')}"),
        "channel_url": f"{root}/{channel_path}",
        "media_url": f"{root}/{media_path}",
        "scene_url": (f"{vcfg['collective_viewer_url']}#/"
                      f"{quote(submission['slug'], safe='')}"),
        "source_url": (f"https://github.com/{repo.owner}/{repo.name}/blob/"
                       f"{quote(str(vcfg.get('base_branch', 'main')), safe='')}/"
                       f"{media_path}"),
    }


def confirm_vision_state(clone, submission, piece_bytes, vcfg, ctx,
                         merge_sha="", expected_paths=None):
    clone = Path(clone)
    base = vcfg.get("base_branch", "main")
    git_t = int(vcfg.get("git_timeout_s", 600))
    media_rel = vision_media_path(submission, vcfg)
    channel_rel = vcfg["channel_path"]
    media = _git_bytes(clone, "cat-file", "blob",
                       f"origin/{base}:{media_rel}", timeout=git_t, ctx=ctx)
    if media != piece_bytes:
        raise CommandError(f"{media_rel} on origin/{base} is not the gated art")
    channel_blob = _git_bytes(
        clone, "cat-file", "blob", f"origin/{base}:{channel_rel}",
        timeout=git_t, ctx=ctx)
    try:
        channel = json.loads(channel_blob.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as e:
        raise CommandError(f"{channel_rel} on origin/{base} is invalid: {e}")
    image_info = (_check_png(piece_bytes)
                  if submission.get("kind") == "png" else None)
    expected_entry = vision_video(submission, vcfg, image_info)
    actual = next((video for video in channel.get("videos", [])
                   if isinstance(video, dict)
                   and video.get("id") == submission["slug"]), None)
    if actual != expected_entry:
        raise CommandError(f"{channel_rel} does not contain the verified "
                           f"{submission['slug']!r} entry")
    registry_rel = vcfg.get("registry_path")
    if registry_rel:
        registry_blob = _git_bytes(
            clone, "cat-file", "blob", f"origin/{base}:{registry_rel}",
            timeout=git_t, ctx=ctx)
        try:
            registry = json.loads(registry_blob.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as e:
            raise CommandError(f"{registry_rel} on origin/{base} is invalid: {e}")
        registered = next((item for item in registry.get("channels", [])
                           if isinstance(item, dict)
                           and item.get("id") == vcfg["channel_id"]), None)
        wanted = vision_registry_entry(vcfg)
        if registered is None or any(
                registered.get(key) != wanted[key]
                for key in ("name", "url", "repo")):
            raise CommandError(f"{registry_rel} does not register "
                               f"{vcfg['channel_id']!r}")
    base_sha = _git(clone, "rev-parse", f"origin/{base}",
                    timeout=git_t, ctx=ctx).strip()
    receipts = {
        "merge_commit": merge_sha or base_sha,
        "base_sha": base_sha,
        "paths": sorted(expected_paths or [media_rel, channel_rel]),
        **vision_urls(vcfg, submission),
    }
    return receipts


def publish_rapp_vision(workspace, submission, piece_bytes, wcfg, health,
                        transaction=None, ctx=None, profile_snapshot=None):
    """Deploy the already-gated art into its RAPP Vision channel.

    The adapter is idempotent against origin/main. A crash after the merge but
    before the collective ledger moves simply re-enters here, sees the exact
    media and channel entry, and returns the same public receipts.
    """
    snapshot = (validate_profile_snapshot(profile_snapshot)
                if profile_snapshot is not None
                else publication_profile_snapshot(wcfg))
    if snapshot:
        validate_image_generation_receipt(
            submission.get("meta") or {}, piece_bytes, snapshot)
    vcfg = vision_config(wcfg)
    if snapshot:
        validate_reviewed_vision_config(vcfg)
    if snapshot and not vcfg["enabled"]:
        raise GateError(
            f"{AZURE_REVIEWED_PNG_PROFILE} cannot publish without RAPP Vision")
    if not vcfg["enabled"]:
        return {}
    vwcfg = vision_worker_config(wcfg, vcfg)
    ctx = ctx or controller_for(
        vwcfg, git_home=STATE / "git-home-rapp-vision")
    clone = Path(workspace) / "vision-clone"
    base = vcfg.get("base_branch", "main")
    git_t = int(vwcfg.get("git_timeout_s", 600))
    gh_t = int(vwcfg.get("gh_timeout_s", 300))
    repo = ctx.gh_repo()
    note = transaction or (lambda **_: None)

    _clone_repo(vwcfg, clone, ctx)
    assert_repo_integrity(clone, vwcfg, ctx=ctx)
    entry, changed = write_vision_files(
        clone, submission, piece_bytes, vcfg)
    if not changed:
        return confirm_vision_state(
            clone, submission, piece_bytes, vcfg, ctx)
    _git(clone, "reset", "--hard", f"origin/{base}", timeout=git_t, ctx=ctx)
    _git(clone, "clean", "-fd", timeout=git_t, ctx=ctx)

    branch = (f"{vcfg.get('branch_prefix', 'art/dada-vision')}/"
              f"{submission['slug']}-{uuid.uuid4().hex[:8]}")
    probe_health(health, "pre-vision-write", wcfg)
    _git(clone, "checkout", "-b", branch, f"origin/{base}",
         timeout=git_t, ctx=ctx)
    # Reapply after checkout because the base checkout replaced the prepared
    # working tree.
    entry, changed = write_vision_files(
        clone, submission, piece_bytes, vcfg)
    _git(clone, "add", "--", *changed, timeout=git_t, ctx=ctx)
    staged = [line.split("\t") for line in
              _git(clone, "diff", "--cached", "--name-status",
                   timeout=git_t, ctx=ctx).splitlines() if line.strip()]
    if sorted(path for _, path in staged) != sorted(changed):
        raise GateError(f"RAPP Vision staged set is {staged}, expected {changed}")
    if any(status not in ("A", "M") for status, _ in staged):
        raise GateError(f"RAPP Vision staged an unsupported change: {staged}")
    _git(clone, "-c", f"user.name={wcfg['git_author_name']}",
         "-c", f"user.email={wcfg['git_author_email']}",
         "commit", "-m",
         f"art: deploy {submission['title']} to RAPP Vision",
         timeout=git_t, ctx=ctx)
    head = _git(clone, "rev-parse", "HEAD", timeout=git_t, ctx=ctx).strip()
    note(vision_phase="committed", vision_branch=branch,
         vision_commit=head, vision_paths=changed)
    _git_remote(clone, vwcfg, "push", "--set-upstream", "origin", branch,
                timeout=git_t, ctx=ctx)
    note(vision_phase="pushed")

    pr_number = ""
    try:
        pr_url = _gh(
            "pr", "create", "--repo", repo, "--base", base, "--head", branch,
            "--title", f"art: {submission['title']} on RAPP Vision",
            "--body",
            "Deploy the exact gated CC0 artwork from Public Art Collective "
            f"as `{vcfg['channel_id']}` entry `{submission['slug']}`.",
            timeout=gh_t, ctx=ctx).strip().splitlines()[-1].strip()
        pr_number = pr_url.rstrip("/").split("/")[-1]
        note(vision_phase="pr-open", vision_pr_url=pr_url,
             vision_pr_number=pr_number)
        view = json.loads(_gh(
            "pr", "view", pr_number, "--repo", repo, "--json",
            "files,state,baseRefName,headRefName,isCrossRepository",
            timeout=gh_t, ctx=ctx))
        remote_paths = sorted(item.get("path") for item in view.get("files", []))
        if remote_paths != sorted(changed):
            raise GateError(f"RAPP Vision PR touches {remote_paths}, "
                            f"expected {sorted(changed)}")
        if view.get("baseRefName") != base or view.get("headRefName") != branch:
            raise GateError("RAPP Vision PR branch or base changed")
        probe_health(health, "pre-vision-merge", wcfg)
        note(vision_phase="merging")
        _gh("pr", "merge", pr_number, "--repo", repo, "--squash",
            "--delete-branch", timeout=gh_t, ctx=ctx)
        note(vision_phase="merge-called")
    except BaseException:
        if pr_number:
            _close_pr(repo, pr_number, gh_t, ctx)
        else:
            _delete_remote_branch(clone, branch, vwcfg, git_t, ctx)
        note(vision_phase="cleaned-up")
        raise

    merged = json.loads(_gh(
        "pr", "view", pr_number, "--repo", repo, "--json",
        "state,mergeCommit", timeout=gh_t, ctx=ctx))
    if str(merged.get("state")).upper() != "MERGED":
        raise CommandError(f"RAPP Vision PR {pr_number} is not merged")
    merge_sha = ((merged.get("mergeCommit") or {}).get("oid") or "").strip()
    _git_remote(clone, vwcfg, "fetch", "--no-tags", "origin", base,
                timeout=git_t, ctx=ctx)
    _git(clone, "merge-base", "--is-ancestor", merge_sha, f"origin/{base}",
         timeout=git_t, ctx=ctx)
    touched = [line.split("\t") for line in
               _git(clone, "show", "--name-status", "--format=", merge_sha,
                    timeout=git_t, ctx=ctx).splitlines() if line.strip()]
    if sorted(path for _, path in touched) != sorted(changed):
        raise CommandError(f"RAPP Vision merge touches {touched}, "
                           f"expected {changed}")
    if any(status not in ("A", "M") for status, _ in touched):
        raise CommandError(f"RAPP Vision merge has unsupported changes {touched}")
    receipts = confirm_vision_state(
        clone, submission, piece_bytes, vcfg, ctx, merge_sha, changed)
    receipts.update({"branch": branch, "commit": head, "pr_url": pr_url,
                     "pr_number": pr_number, "entry": entry})
    note(vision_phase="merged", vision_receipts=receipts)
    return receipts


def verify_dual_pages(cfg, wcfg, submission, vision_receipts, probe=None,
                      sleep=None, png_probe=None, profile_snapshot=None):
    """Require both actual GitHub Pages experiences before finalizing."""
    snapshot = (validate_profile_snapshot(profile_snapshot)
                if profile_snapshot is not None
                else publication_profile_snapshot(wcfg))
    vcfg = vision_config(wcfg)
    if snapshot:
        validate_reviewed_vision_config(vcfg)
    if snapshot and not vcfg["enabled"]:
        raise GateError(
            f"{AZURE_REVIEWED_PNG_PROFILE} cannot verify without RAPP Vision")
    if not vcfg["enabled"]:
        view, kind, note = verified_view(cfg, wcfg, submission, probe, sleep)
        return {"collective_url": view, "collective_kind": kind, "note": note}
    probe = probe or probe_url
    png_probe = png_probe or probe_png_url
    sleep = sleep or time.sleep
    collective_url, _ = art_urls(cfg, wcfg, submission)
    vision = vision_receipts or vision_urls(vcfg, submission)
    required_routes = [
        ("Public Art Collective", collective_url),
        ("RAPP Vision channel", vision.get("channel_url", "")),
        ("RAPP Vision scene", vision.get("scene_url", "")),
        ("RAPP Vision player", vcfg["player_url"]),
    ]
    required_media = []
    if snapshot:
        required_routes.append(
            ("RAPP Vision watch", vision.get("watch_url", "")))
        required_media = [
            ("Public Art piece", piece_pages_url(cfg, wcfg, submission)),
            ("RAPP Vision media", vision.get("media_url", "")),
        ]
    else:
        required_routes.insert(
            1, ("Public Art piece", piece_pages_url(cfg, wcfg, submission)))
        required_routes.insert(
            3, ("RAPP Vision media", vision.get("media_url", "")))
    timeout = int(wcfg.get("view_probe_timeout_s", 10))
    backoff = list(wcfg.get("view_probe_backoff") or (5, 10, 20, 30))
    attempts = max(1, int(wcfg.get("view_probe_attempts", len(backoff) + 1)))
    last = []
    for attempt in range(attempts):
        last = []
        for label, url in required_routes:
            ok, detail = probe(url, timeout) if url else (False, "no url")
            if not ok:
                last.append(f"{label}: {detail}")
        for label, url in required_media:
            ok, detail = png_probe(url, timeout) if url else (False, "no url")
            if not ok:
                last.append(f"{label}: {detail}")
        if not last:
            return {
                "collective_url": collective_url,
                "collective_kind": "pages",
                "vision_url": vision["watch_url"],
                "vision_channel_url": vision["channel_url"],
                "vision_media_url": vision["media_url"],
                "note": "",
            }
        if attempt + 1 < attempts:
            sleep(backoff[min(attempt, len(backoff) - 1)])
    raise DeploymentPending("public deployment is not ready: " + "; ".join(last))


def durable_deployment_receipt(cfg, wcfg, submission, collective_receipts,
                               vision_receipts, deployed,
                               profile_snapshot=None):
    """Bind already-probed public URLs to the gated bytes and profile."""
    snapshot = (validate_profile_snapshot(profile_snapshot)
                if profile_snapshot is not None
                else publication_profile_snapshot(wcfg))
    if snapshot:
        validate_reviewed_submission_contract(
            submission.get("meta") if isinstance(submission, dict) else None,
            expected_slug=(
                submission.get("slug") if isinstance(submission, dict) else None
            ),
        )
    if not isinstance(deployed, dict):
        raise GateError("deployment verification did not return an object")
    required = {
        "meta_sha256": submission.get("meta_sha256"),
        "piece_sha256": submission.get("piece_sha256"),
        "collective_merge_commit": collective_receipts.get("merge_commit"),
    }
    if any(
            not isinstance(value, str)
            or not re.fullmatch(r"[0-9a-f]{64}", value)
            for key, value in required.items()
            if key != "collective_merge_commit"):
        raise GateError("deployment receipt lacks gated submission digests")
    if (not isinstance(required["collective_merge_commit"], str)
            or not required["collective_merge_commit"]):
        raise GateError("deployment receipt lacks the Collective merge commit")
    vcfg = vision_config(wcfg)
    vision_merge = ""
    if vcfg["enabled"]:
        vision_merge = (vision_receipts or {}).get("merge_commit")
        if not isinstance(vision_merge, str) or not vision_merge:
            raise GateError("deployment receipt lacks the RAPP Vision merge commit")
    return {
        "schema": DEPLOYMENT_RECEIPT_SCHEMA,
        "profile": snapshot["profile"] if snapshot else "",
        **required,
        "vision_merge_commit": vision_merge,
        **deployed,
    }


def persisted_deployment_receipts(state, cfg, wcfg, submission,
                                  collective_receipts, profile_snapshot=None):
    """Validate and rehydrate a previously verified deployment without probing."""
    snapshot = (validate_profile_snapshot(profile_snapshot)
                if profile_snapshot is not None
                else publication_profile_snapshot(wcfg))
    if snapshot:
        validate_reviewed_submission_contract(
            submission.get("meta") if isinstance(submission, dict) else None,
            expected_slug=(
                submission.get("slug") if isinstance(submission, dict) else None
            ),
        )
    receipt = state.get("deployment_receipts")
    if not isinstance(receipt, dict):
        raise GateError("notification retry has no persisted deployment receipt")
    profile = snapshot["profile"] if snapshot else ""
    required = {
        "schema", "profile", "meta_sha256", "piece_sha256",
        "collective_merge_commit", "vision_merge_commit",
        "collective_url", "collective_kind", "note",
    }
    vcfg = vision_config(wcfg)
    if vcfg["enabled"]:
        required.update({
            "vision_url", "vision_channel_url", "vision_media_url",
        })
    if set(receipt) != required:
        raise GateError("persisted deployment receipt has an invalid shape")
    if receipt.get("schema") != DEPLOYMENT_RECEIPT_SCHEMA:
        raise GateError("persisted deployment receipt has an invalid schema")
    if receipt.get("profile") != profile:
        raise GateError("persisted deployment receipt profile conflicts with transaction")
    for key in ("meta_sha256", "piece_sha256"):
        expected = submission.get(key)
        actual = receipt.get(key)
        if (not isinstance(expected, str)
                or not re.fullmatch(r"[0-9a-f]{64}", expected)
                or actual != expected):
            raise GateError(
                f"persisted deployment receipt {key} conflicts with gated bytes")
    merge_commit = collective_receipts.get("merge_commit")
    if (not isinstance(merge_commit, str) or not merge_commit
            or receipt.get("collective_merge_commit") != merge_commit):
        raise GateError(
            "persisted deployment receipt conflicts with the Collective merge")
    if (not isinstance(receipt.get("collective_url"), str)
            or not isinstance(receipt.get("collective_kind"), str)
            or not isinstance(receipt.get("note"), str)
            or len(receipt["note"]) > 600):
        raise GateError("persisted deployment receipt has invalid public URL fields")

    collective_view, _ = art_urls(cfg, wcfg, submission)
    if vcfg["enabled"]:
        vision = vision_urls(vcfg, submission)
        expected_urls = {
            "collective_url": collective_view,
            "collective_kind": "pages",
            "vision_url": vision["watch_url"],
            "vision_channel_url": vision["channel_url"],
            "vision_media_url": vision["media_url"],
            "note": "",
        }
        if any(receipt.get(key) != value for key, value in expected_urls.items()):
            raise GateError(
                "persisted deployment receipt public routes conflict with "
                "the captured deployment contract")
        vision_receipts = state.get("vision_receipts")
        if not isinstance(vision_receipts, dict):
            raise GateError(
                "persisted deployment receipt lacks RAPP Vision merge evidence")
        vision_merge = vision_receipts.get("merge_commit")
        if (not isinstance(vision_merge, str) or not vision_merge
                or receipt.get("vision_merge_commit") != vision_merge):
            raise GateError(
                "persisted deployment receipt conflicts with the RAPP Vision merge")
    else:
        kind = receipt["collective_kind"]
        expected_url = (
            collective_view if kind == "pages"
            else raw_url(cfg, wcfg, submission) if kind == "raw"
            else ""
        )
        if not expected_url or receipt["collective_url"] != expected_url:
            raise GateError(
                "persisted deployment receipt has an invalid Collective route")
        if receipt.get("vision_merge_commit") != "":
            raise GateError(
                "persisted deployment receipt unexpectedly names a Vision merge")
        vision_receipts = {}

    return {
        **collective_receipts,
        **receipt,
        "vision": dict(vision_receipts),
    }


# ── bookkeeping ─────────────────────────────────────────────────────────────

def notification_for(outcome, role, detail, receipts=None):
    """The alert text, or None. Only a verified merge gets the paintbrush.

    The merged case is built by art_notification() instead — it is the only
    outcome that has a URL a human can tap.
    """
    if outcome == OUTCOME_CONTRIBUTED:
        url = (receipts or {}).get("pr_url", "")
        return (f"{SUCCESS_PREFIX} {role} contributed and it is merged:\n"
                f"{detail[:300]}\n{url}")
    if outcome == OUTCOME_DECLINED:
        return f"\u2022 {role} considered a contribution and declined:\n{detail[:300]}"
    return (f"\u26A0\uFE0F {role} evolution {outcome}:\n{detail[:400]}")


def _looks_like_a_path(repo):
    text = str(repo).strip()
    return text.startswith(("/", ".", "~")) or text.startswith("file://")


def art_repo(cfg, wcfg):
    """owner/name for the commons this instance publishes art to.

    commons_repo is the configured name for "the repository this instance
    contributes to", so it wins; the worker's own repo key is the fallback so
    an instance that set only one of them still gets links.
    """
    for candidate in (cfg.get("commons_repo"), wcfg.get("repo")):
        text = str(candidate or "").strip().rstrip("/")
        if not text or _looks_like_a_path(text):
            continue
        if text.endswith(".git"):
            text = text[:-4]
        if "://" in text:
            text = urlsplit(text).path
        elif text.startswith("git@") and ":" in text:
            text = text.split(":", 1)[1]
        parts = [p for p in text.strip("/").split("/") if p]
        if len(parts) >= 2:
            return parts[-2], parts[-1]
    return "", ""


def art_urls(cfg, wcfg, submission):
    """(view, source) for a merged piece, derived, never guessed at send time.

    The view URL is the GitHub Pages copy of the piece itself: one tap, the
    artwork, no navigation. The source URL is the same bytes on GitHub, which
    is what makes the message checkable by someone who does not trust it.
    """
    owner, name = art_repo(cfg, wcfg)
    if not owner or not name:
        return "", ""
    path = "/".join(quote(seg, safe="")
                    for seg in str(submission["piece_path"]).split("/"))
    branch = quote(str(wcfg.get("base_branch", "main")), safe="")
    slug = quote(str(submission.get("slug") or
                     Path(submission["piece_path"]).parent.name), safe="")
    return (f"https://{owner.lower()}.github.io/{name}/view.html#/{slug}",
            f"https://github.com/{owner}/{name}/blob/{branch}/{path}")


def piece_pages_url(cfg, wcfg, submission):
    owner, name = art_repo(cfg, wcfg)
    if not owner or not name:
        return ""
    path = "/".join(quote(seg, safe="")
                    for seg in str(submission["piece_path"]).split("/"))
    return f"https://{owner.lower()}.github.io/{name}/{path}"


def raw_url(cfg, wcfg, submission):
    """The bytes on GitHub, served as a file rather than as a page."""
    owner, name = art_repo(cfg, wcfg)
    if not owner or not name:
        return ""
    path = "/".join(quote(seg, safe="")
                    for seg in str(submission["piece_path"]).split("/"))
    branch = quote(str(wcfg.get("base_branch", "main")), safe="")
    return f"https://raw.githubusercontent.com/{owner}/{name}/{branch}/{path}"


def probe_url(url, timeout=10):
    """(ok, detail) for one HTTP probe. Never raises."""
    if not url:
        return False, "no url"
    request = urllib.request.Request(url, method="GET",
                                     headers={"User-Agent": "rapp-sentinel"})
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            code = getattr(response, "status", None) or response.getcode()
            response.read(1024)
            return (200 <= int(code) < 300), f"HTTP {code}"
    except urllib.error.HTTPError as e:
        return False, f"HTTP {e.code}"
    except Exception as e:
        return False, f"{type(e).__name__}: {str(e)[:80]}"


def probe_png_url(url, timeout=10, opener=None):
    """Verify a direct HTTP PNG response without trusting its URL suffix."""
    if not url:
        return False, "no url"
    request = urllib.request.Request(
        url, method="GET", headers={"User-Agent": "rapp-sentinel"})
    opener = opener or urllib.request.urlopen
    try:
        with opener(request, timeout=timeout) as response:
            code = getattr(response, "status", None) or response.getcode()
            headers = getattr(response, "headers", {})
            if hasattr(headers, "get_content_type"):
                content_type = headers.get_content_type()
            else:
                content_type = str(
                    headers.get("Content-Type", "")).split(";", 1)[0].strip()
            signature = response.read(len(azure_art.PNG_SIGNATURE))
            if not 200 <= int(code) < 300:
                return False, f"HTTP {code}"
            if str(content_type).lower() != "image/png":
                return False, (
                    f"HTTP {code} Content-Type "
                    f"{content_type or '(missing)'}, expected image/png")
            if signature != azure_art.PNG_SIGNATURE:
                return False, f"HTTP {code} body has no PNG signature"
            return True, f"HTTP {code} image/png PNG"
    except urllib.error.HTTPError as exc:
        return False, f"HTTP {exc.code}"
    except Exception as exc:
        return False, f"{type(exc).__name__}: {str(exc)[:80]}"


def verified_view(cfg, wcfg, submission, probe=None, sleep=None):
    """The URL a human can actually tap, proved before it is sent.

    GitHub Pages publishes on its own schedule, so a merge verified against
    origin/main can still 404 for a minute or two. Texting that URL is worse
    than texting nothing: it reads as "your art is live" and lands on a 404,
    which teaches the reader to distrust every future message. So the Pages
    copy is probed with a bounded backoff, the raw file is the fallback, and
    if neither answers the message says so rather than pretending (#10).

    Returns (url, kind, note) where kind is "pages", "raw" or "".
    """
    probe = probe or probe_url
    sleep = sleep or time.sleep
    view, _ = art_urls(cfg, wcfg, submission)
    raw = raw_url(cfg, wcfg, submission)
    timeout = int(wcfg.get("view_probe_timeout_s", 10))
    backoff = list(wcfg.get("view_probe_backoff") or (5, 10, 20, 30))
    attempts = max(1, int(wcfg.get("view_probe_attempts", len(backoff) + 1)))

    detail = "no url"
    for attempt in range(attempts):
        if view:
            ok, detail = probe(view, timeout)
            if ok:
                return view, "pages", ""
        if raw:
            ok_raw, detail_raw = probe(raw, timeout)
            if ok_raw:
                # Merged and readable, just not published as a page yet. Say
                # which one this is; a link that works is not a link that lies.
                if attempt + 1 >= attempts:
                    return raw, "raw", ("GitHub Pages has not published it yet — "
                                        "this link is the file itself.")
            else:
                detail = f"{detail}; raw {detail_raw}"
        if attempt + 1 < attempts:
            sleep(backoff[min(attempt, len(backoff) - 1)])
    if raw:
        ok_raw, _ = probe(raw, timeout)
        if ok_raw:
            return raw, "raw", ("GitHub Pages has not published it yet — this "
                                "link is the file itself.")
    return "", "", (f"the merge is verified on main, but no public URL answered "
                    f"yet ({detail}).")


def _first_sentence(text, limit=220):
    collapsed = " ".join(str(text).split())
    if not collapsed:
        return ""
    match = re.search(r"(?<=[.!?])\s", collapsed)
    sentence = (collapsed[:match.start()] if match else collapsed).rstrip()
    if len(sentence) > limit:
        sentence = sentence[:limit - 1].rstrip() + "\u2026"
    return sentence


def concept_sentence(meta, limit=220):
    """One sentence about what the thing IS, from the piece's own record.

    Preference order is "what the maker said about it", then the premise of
    the candidate that actually won its cycle — never a generated summary,
    because a summary nobody wrote is a claim nobody made.
    """
    for key in ("_concept", "_artist_statement", "_inspired_by"):
        value = meta.get(key)
        if isinstance(value, str) and value.strip():
            return _first_sentence(value, limit)
    cycle = meta.get("_dada_cycle") or {}
    rounds = cycle.get("rounds") or []
    winner = (cycle.get("winner") or {}).get("candidate")
    if rounds:
        for cand in rounds[-1].get("candidates") or []:
            if cand.get("id") == winner and isinstance(cand.get("premise"), str):
                return _first_sentence(cand["premise"], limit)
    return _first_sentence(meta.get("title") or "a new piece", limit)


def art_recipient(cfg):
    """Where art news goes: the reports number, else the alert handle."""
    for key in ("report_number", "notify_handle"):
        value = str(cfg.get(key) or "").strip()
        if value:
            return value
    return ""


def art_notification(cfg, wcfg, submission, receipts, view="", note=""):
    """The message a verified deployment earns. Only public experience links.

    `view` is a URL that was PROBED after the merge, not one that was derived
    and hoped for.
    """
    lines = [f"{SUCCESS_PREFIX} {sentinel.instance_name(cfg)}: "
             f"\u201c{submission['title']}\u201d is merged.",
             "",
             concept_sentence(submission["meta"])]
    if view:
        lines += ["", f"Public Art Collective: {view}"]
    vision_url = ((receipts or {}).get("vision_url")
                  or ((receipts or {}).get("vision") or {}).get("watch_url", ""))
    if vision_url:
        lines.append(f"RAPP Vision: {vision_url}")
    if note:
        lines += ["", note]
    if not view:
        lines += ["", "(no Public Art Collective Pages URL was verified)"]
    return "\n".join(lines)


def final_notification_id(row, submission, receipts):
    row_id = row.get("id") if isinstance(row, dict) else None
    slug = submission.get("slug") if isinstance(submission, dict) else None
    merge_commit = receipts.get("merge_commit") if isinstance(receipts, dict) else None
    if (not isinstance(row_id, str) or not row_id
            or not isinstance(slug, str) or not SLUG_RE.fullmatch(slug)
            or not isinstance(merge_commit, str) or not merge_commit):
        raise GateError(
            "final art notification identity lacks row, slug, or merge commit")
    material = f"{row_id}\0{slug}\0{merge_commit}".encode("utf-8")
    return "evolve-art:" + hashlib.sha256(material).hexdigest()


def final_notification_record(cfg, wcfg, row, submission, receipts,
                              view, view_note):
    recipient = art_recipient(cfg)
    required = bool(
        cfg.get("notify")
        and sentinel.notification_allowed(cfg, "art")
        and recipient)
    return {
        "schema": FINAL_NOTIFICATION_SCHEMA,
        "id": final_notification_id(row, submission, receipts),
        "required": required,
        "to": recipient,
        "text": art_notification(
            cfg, wcfg, submission, receipts, view, view_note),
    }


def validate_final_notification(record, expected):
    fields = {"schema", "id", "required", "to", "text"}
    if (not isinstance(record, dict) or set(record) != fields
            or record.get("schema") != FINAL_NOTIFICATION_SCHEMA
            or not isinstance(record.get("id"), str)
            or record.get("id") != expected["id"]
            or not isinstance(record.get("required"), bool)
            or not isinstance(record.get("to"), str)
            or not isinstance(record.get("text"), str)
            or not record.get("text")
            or (record.get("required") and not record.get("to"))):
        raise GateError("final art notification transaction is malformed")
    return dict(record)


def enqueue_final_art_notification(cfg, record, transaction=None):
    """Persist intent, idempotently enqueue, then persist confirmation."""
    if transaction:
        transaction(
            phase="notification-pending",
            final_notification=record,
            notification_id=record["id"])
    if not record["required"]:
        if transaction:
            transaction(phase="notification-skipped")
        return False

    delivery_cfg = dict(cfg)
    delivery_cfg["notify"] = True
    delivery_cfg["notification_mode"] = "art-only"
    queued = sentinel.notify(
        delivery_cfg, record["text"], to=record["to"], kind="art",
        attach_report=False, dedupe_key=record["id"])
    if queued is False:
        raise NotificationPending(
            f"outbox did not confirm {record['id']}")
    if transaction:
        transaction(phase="notification-enqueued")
    return True


def open_final_art(wcfg, receipts):
    if not azure_image_config(wcfg).get("open_in_browser"):
        return False
    urls = [
        str(receipts.get("collective_url") or "").strip(),
        str(receipts.get("vision_url") or "").strip(),
    ]
    urls = [url for url in urls if url.startswith("https://")]
    if not urls or sys.platform != "darwin":
        return False
    try:
        result = subprocess.run(
            ["/usr/bin/open", "-a", "Safari", *urls],
            capture_output=True, text=True, timeout=15)
    except (OSError, subprocess.TimeoutExpired) as exc:
        log(f"could not open final art in Safari: {type(exc).__name__}: {exc}")
        return False
    if result.returncode != 0:
        log(f"could not open final art in Safari: "
            f"{(result.stderr or result.stdout or '').strip()[:200]}")
        return False
    return True


def record(history, row, path=None):
    """Append one durable row and persist the whole ledger atomically."""
    history.append(row)
    save_history(history, path)
    return row


def _emit_frame(role, payload):
    try:
        NB.emit(role, "neighbor.acted", payload)
    except Exception as e:
        log(f"chain frame refused: {type(e).__name__}: {e}")


def _notify_once(cfg, key, text, hours=24):
    """Say an operational failure out loud, but not every 30 minutes."""
    try:
        stamps = strict_load(ALERT_PATH, {}, expect=dict)
    except LedgerError:
        stamps = {}
    last = stamps.get(key)
    if last:
        try:
            if (sentinel.now() - datetime.fromisoformat(last)).total_seconds() < hours * 3600:
                return
        except ValueError:
            pass
    stamps[key] = sentinel.now().isoformat(timespec="seconds")
    try:
        atomic_write_json(ALERT_PATH, stamps)
    except OSError:
        pass
    sentinel.notify(cfg, text)


# ── the run ─────────────────────────────────────────────────────────────────

def write_status(outcome, reason="", **extra):
    """The worker's heartbeat. Written on EVERY pass, including the ones that
    decide to do nothing.

    A job that runs and skips and a job launchd never loaded look identical
    from outside — both produce no art and no log line anybody reads. This
    file is what lets w_evolve_worker tell those two apart (#6).
    """
    payload = {
        "at": sentinel.now().isoformat(timespec="seconds"),
        "outcome": outcome,
        "reason": str(reason)[:400],
        "pid": os.getpid(),
        "depth": SS.current_depth(),
        **extra,
    }
    try:
        atomic_write_json(STATUS_PATH, payload)
    except OSError as e:
        log(f"could not write the heartbeat: {type(e).__name__}: {e}")
    return payload


def _skip(reason):
    log(f"skipped — {reason}")
    write_status("skipped", reason)
    return {"outcome": "skipped", "reason": reason}


# ── crash-window reconciliation ─────────────────────────────────────────────

def transaction_writer(row_id, base):
    """Persist what this cycle has done so far, atomically, at every step."""
    state = dict(base)
    state["row_id"] = row_id

    def note(**fields):
        state.update({k: v for k, v in fields.items() if v is not None})
        state["at"] = sentinel.now().isoformat(timespec="seconds")
        atomic_write_json(TRANSACTION_PATH, state)
        return state
    return note


def clear_transaction():
    try:
        Path(TRANSACTION_PATH).unlink()
    except FileNotFoundError:
        pass
    except OSError as e:
        log(f"could not clear the transaction file: {e}")


def finish_platform_deployments(cfg, wcfg, workspace, collective_clone,
                                submission, collective_receipts, health,
                                transaction=None, profile_snapshot=None):
    """Verify the canonical merge and, when enabled, its RAPP Vision mirror."""
    snapshot = (validate_profile_snapshot(profile_snapshot)
                if profile_snapshot is not None
                else publication_profile_snapshot(wcfg))
    vcfg = vision_config(wcfg)
    if snapshot:
        validate_reviewed_vision_config(vcfg)
    if snapshot and not vcfg["enabled"]:
        raise GateError(
            f"{AZURE_REVIEWED_PNG_PROFILE} cannot publish without RAPP Vision")
    if not vcfg["enabled"]:
        return dict(collective_receipts)
    base = wcfg.get("base_branch", "main")
    git_t = int(wcfg.get("git_timeout_s", 600))
    ctx = controller_for(wcfg)
    piece_bytes = _git_bytes(
        collective_clone, "cat-file", "blob",
        f"origin/{base}:{submission['piece_path']}",
        timeout=git_t, ctx=ctx)
    if hashlib.sha256(piece_bytes).hexdigest() != submission["piece_sha256"]:
        raise CommandError("the collective bytes changed before RAPP Vision deploy")
    if snapshot:
        validate_image_generation_receipt(
            submission.get("meta") or {}, piece_bytes, snapshot)
    vwcfg = vision_worker_config(wcfg, vcfg)
    vctx = controller_for(
        vwcfg, git_home=STATE / "git-home-rapp-vision")
    assert_publish_auth(vwcfg, ctx=vctx)
    vision_receipts = publish_rapp_vision(
        workspace, submission, piece_bytes, wcfg, health,
        transaction=transaction, ctx=vctx, profile_snapshot=snapshot)
    deployed = verify_dual_pages(
        cfg, wcfg, submission, vision_receipts,
        profile_snapshot=snapshot)
    deployment_receipts = durable_deployment_receipt(
        cfg, wcfg, submission, collective_receipts, vision_receipts, deployed,
        profile_snapshot=snapshot)
    receipts = dict(collective_receipts)
    receipts["vision"] = vision_receipts
    receipts.update(deployment_receipts)
    if transaction:
        transaction(phase="platforms-verified",
                    vision_receipts=vision_receipts,
                    deployment_receipts=deployment_receipts)
    return receipts


def clean_interrupted_vision_pr(state, wcfg):
    pr_number = str(state.get("vision_pr_number") or "")
    if not pr_number:
        return
    vcfg = vision_config(wcfg)
    if not vcfg["enabled"]:
        return
    vwcfg = vision_worker_config(wcfg, vcfg)
    vctx = controller_for(
        vwcfg, git_home=STATE / "git-home-rapp-vision")
    repo = vctx.gh_repo()
    gh_t = int(vwcfg.get("gh_timeout_s", 300))
    try:
        view = json.loads(_gh(
            "pr", "view", pr_number, "--repo", repo,
            "--json", "state", timeout=gh_t, ctx=vctx))
    except (CommandError, subprocess.TimeoutExpired, json.JSONDecodeError) as e:
        raise DeploymentPending(
            f"cannot inspect interrupted RAPP Vision PR {pr_number}: {e}")
    if str(view.get("state")).upper() == "MERGED":
        return
    _close_pr(repo, pr_number, gh_t, vctx)
    log(f"closed interrupted RAPP Vision PR {pr_number} before retrying")


def deployment_pending(history, row, error, transaction=None, wcfg=None):
    detail = f"dual publication pending: {type(error).__name__}: {error}"
    try:
        state = strict_load(TRANSACTION_PATH, {}, expect=dict)
    except LedgerError:
        state = {}
    attempts = int(state.get("deployment_attempts") or 0) + 1
    first = state.get("first_pending_at") or sentinel.now().isoformat(
        timespec="seconds")
    if transaction:
        transaction(phase="deployment-pending",
                    deployment_attempts=attempts,
                    first_pending_at=first,
                    last_deployment_error=detail[:400])
    limit = max(1, int((wcfg or {}).get("deployment_retry_limit") or
                       vision_config(wcfg or {}).get(
                           "deployment_retry_limit", 12)))
    outcome = "fail-closed" if attempts >= limit else "deployment-pending"
    row["detail"] = detail[:400]
    row["deployment_attempts"] = attempts
    row["first_pending_at"] = first
    save_history(history)
    write_status(outcome, detail, role=row.get("role", ""),
                 cycle=row.get("cycle"), slug=row.get("slug", ""),
                 deployment_attempts=attempts, first_pending_at=first)
    log(f"{detail} (attempt {attempts}/{limit})")
    return {"outcome": outcome, "reason": detail,
            "detail": detail}


def collective_checks_pending(history, row, error, transaction=None):
    """Retain an open protected PR until its exact gate is decisive."""
    detail = f"Collective provenance checks pending: {type(error).__name__}: {error}"
    if transaction:
        transaction(
            phase="checks-pending",
            last_check_error=detail[:400],
        )
    row["detail"] = detail[:400]
    save_history(history)
    write_status(
        "checks-pending", detail, role=row.get("role", ""),
        cycle=row.get("cycle"), slug=row.get("slug", ""))
    log(detail)
    return {"outcome": "checks-pending", "reason": detail, "detail": detail}


def canonical_merge_pending(history, row, error, transaction=None):
    """Keep an invoked canonical merge unresolved until public proof arrives."""
    detail = f"canonical merge pending reconciliation: {type(error).__name__}: {error}"
    if transaction:
        transaction(
            phase="merge-ambiguous",
            last_merge_error=detail[:400])
    row["detail"] = detail[:400]
    save_history(history)
    write_status(
        "merge-pending", detail, role=row.get("role", ""),
        cycle=row.get("cycle"), slug=row.get("slug", ""))
    log(detail)
    return {"outcome": "merge-pending", "reason": detail, "detail": detail}


def final_notification_pending(history, row, error):
    """The contribution is real; retain its transaction until enqueue is kept."""
    detail = f"final art notification pending: {type(error).__name__}: {error}"
    row["notification_error"] = detail[:400]
    save_history(history)
    write_status(
        "notification-pending", detail, role=row.get("role", ""),
        cycle=row.get("cycle"), slug=row.get("slug", ""))
    log(detail)
    return {
        "outcome": "notification-pending",
        "reason": detail,
        "detail": detail,
    }


def reconciliation_fail_closed(history, row, error):
    """Keep a conflicting public transaction pending and spend nothing."""
    detail = f"profile reconciliation failed closed: {error}"
    row["detail"] = detail[:400]
    save_history(history)
    write_status(
        "fail-closed", detail, role=row.get("role", ""),
        cycle=row.get("cycle"), slug=row.get("slug", ""))
    log(detail)
    return {"outcome": "fail-closed", "reason": detail, "detail": detail}


def reconcile(cfg, wcfg, history, ctx=None, health=None):
    """Finish, or clean up after, a cycle that died mid-publish.

    The dangerous window is between `gh pr merge` and the ledger write: the
    art is public, the ledger says "pending", and the next cycle would compute
    the wrong cycle number and never tell anyone the piece exists. So before
    planning anything, a leftover transaction is resolved against PUBLIC
    state — the PR and origin/main, not our own hopes (#5).

    Returns a summary dict when it did something, else None.
    """
    ctx = ctx or controller_for(wcfg)
    health = health or (lambda phase: sentinel.run_health(receipts=True))
    state = strict_load(TRANSACTION_PATH, {}, expect=dict)
    if not state:
        return None
    submission = state.get("submission") or {}
    receipt_profile = (
        ((submission.get("meta") or {}).get("_image_generation") or {})
        .get("profile")
        if isinstance(submission, dict) else None)
    profile_transaction = (
        state.get("profile_snapshot") is not None
        or receipt_profile == AZURE_REVIEWED_PNG_PROFILE)
    row_id = state.get("row_id")
    row = next((r for r in history if r.get("id") == row_id), None)
    if row is None:
        if profile_transaction:
            detail = (
                "profile reconciliation failed closed: transaction references "
                "a history row that does not exist")
            write_status("fail-closed", detail)
            log(detail)
            return {"outcome": "fail-closed", "reason": detail,
                    "detail": detail}
        log("transaction references a row this ledger does not have — clearing")
        clear_transaction()
        return None
    if (isinstance(submission, dict)
            and submission.get("kind") == "png"
            and not profile_transaction):
        return reconciliation_fail_closed(
            history, row,
            "PNG transaction has no reviewed-profile receipt and snapshot")
    transaction_phase = str(state.get("phase") or "")
    notification_finalization = (
        row.get("outcome") == OUTCOME_CONTRIBUTED
        and transaction_phase in {
            "platforms-verified",
            "notification-preparing",
            "notification-pending",
            "notification-enqueued",
            "notification-skipped",
        }
    )
    if row.get("outcome") != "pending" and not notification_finalization:
        if profile_transaction:
            return reconciliation_fail_closed(
                history, row,
                f"transaction row outcome is {row.get('outcome')!r}, "
                "expected 'pending'")
        clear_transaction()
        return None

    stored_snapshot = state.get("profile_snapshot")
    try:
        current_profile = publication_profile_name(wcfg)
    except GateError as exc:
        return reconciliation_fail_closed(history, row, exc)
    if stored_snapshot is None:
        if (current_profile == AZURE_REVIEWED_PNG_PROFILE
                or receipt_profile == AZURE_REVIEWED_PNG_PROFILE):
            return reconciliation_fail_closed(
                history, row,
                "reviewed-PNG transaction has no persisted profile snapshot")
        profile_snapshot = None
    else:
        try:
            profile_snapshot = validate_profile_snapshot(stored_snapshot)
        except GateError as exc:
            return reconciliation_fail_closed(history, row, exc)

    pr_number = str(state.get("pr_number") or "")
    slug = state.get("slug") or row.get("slug") or ""
    log(f"reconciling an interrupted cycle (phase={state.get('phase')}, "
        f"pr={pr_number or 'none'})")

    if not pr_number:
        # Nothing public was created, or we died before we learned its number.
        row["outcome"] = OUTCOME_ABORTED
        row["detail"] = (f"interrupted at {state.get('phase')} before a PR "
                         f"existed; nothing was published")
        save_history(history)
        clear_transaction()
        return {"outcome": "reconciled-aborted", "detail": row["detail"]}

    workspace = _make_workspace(wcfg)
    try:
        clone = workspace / "clone"
        _clone_repo(wcfg, clone, ctx=ctx)
        assert_repo_integrity(clone, wcfg, ctx=ctx)
        note = transaction_writer(row["id"], state)
        paths = sorted(state.get("paths") or [])
        waiting_for_provenance = (
            _requires_collective_provenance(profile_snapshot)
            and transaction_phase in {
                "pr-open", "checks-pending", "provenance-succeeded", "merging",
            }
        )
        if waiting_for_provenance:
            try:
                scope_state = verify_collective_pr_scope(
                    pr_number, state.get("branch", ""), paths, wcfg, ctx)
                if scope_state == "merged":
                    receipts = confirm_merge(
                        clone, submission, wcfg, pr_number, paths, ctx)
                else:
                    wait_for_collective_provenance(
                        pr_number, wcfg,
                        state.get("provenance_first_seen_at"), note, ctx,
                        cancelled_first_seen=state.get(
                            "provenance_cancelled_first_seen_at"))
                    merge_state = collective_merge_readiness(
                        pr_number, wcfg, ctx)
                    if merge_state == "merged":
                        receipts = confirm_merge(
                            clone, submission, wcfg, pr_number, paths, ctx)
                    else:
                        receipts = _merge_collective_pr(
                            clone, submission, wcfg, pr_number, paths, note,
                            branch=state.get("branch", ""),
                            commit=state.get("commit", ""),
                            pr_url=state.get("pr_url", ""), ctx=ctx)
            except ChecksPending as exc:
                return collective_checks_pending(
                    history, row, exc, transaction=note)
            except ProvenanceCheckFailed as exc:
                try:
                    _close_failed_provenance_pr(
                        clone, submission, wcfg, pr_number, paths, ctx)
                except ChecksPending as close_error:
                    return collective_checks_pending(
                        history, row, close_error, transaction=note)
                except MergeAmbiguous as close_error:
                    return canonical_merge_pending(
                        history, row, close_error, transaction=note)
                row["outcome"] = OUTCOME_ABORTED
                row["detail"] = (
                    f"required provenance check rejected PR {pr_number}: {exc}")
                save_history(history)
                clear_transaction()
                return {"outcome": "reconciled-aborted", "detail": row["detail"]}
            except MergeAmbiguous as exc:
                return canonical_merge_pending(
                    history, row, exc, transaction=note)
            except GateError as exc:
                return reconciliation_fail_closed(history, row, exc)
        else:
            try:
                receipts = confirm_merge(clone, submission, wcfg, pr_number,
                                         paths, ctx)
            except MergeNotConfirmed as exc:
                try:
                    not_merged = prove_non_merge(
                        clone, submission, wcfg, pr_number, paths, ctx)
                except (CommandError, subprocess.TimeoutExpired,
                        json.JSONDecodeError, KeyError, OSError, GateError,
                        ValueError) as proof_error:
                    return canonical_merge_pending(
                        history, row, proof_error, transaction=note)
                if not not_merged:
                    return canonical_merge_pending(
                        history, row, exc, transaction=note)
                log(f"interrupted cycle did not land: {exc}")
                row["outcome"] = OUTCOME_ABORTED
                row["detail"] = (f"interrupted at {state.get('phase')}; PR "
                                 f"{pr_number} is closed and its paths are absent "
                                 f"from origin/{wcfg.get('base_branch', 'main')}")
                save_history(history)
                clear_transaction()
                return {"outcome": "reconciled-aborted", "detail": row["detail"]}
            except GateError as exc:
                return reconciliation_fail_closed(history, row, exc)
            except (CommandError, subprocess.TimeoutExpired,
                    json.JSONDecodeError, KeyError, OSError, ValueError) as exc:
                return canonical_merge_pending(
                    history, row, exc, transaction=note)

        if profile_snapshot:
            try:
                expected_paths = sorted([
                    submission["meta_path"], submission["piece_path"]])
                if sorted(state.get("paths") or []) != expected_paths:
                    raise GateError(
                        "profile transaction paths conflict with submission")
                revalidate_merged_profile_submission(
                    clone, submission, wcfg, profile_snapshot, ctx)
            except (GateError, CommandError, KeyError, OSError) as exc:
                return reconciliation_fail_closed(history, row, exc)

        receipts.update({"pr_url": state.get("pr_url", ""),
                         "pr_number": pr_number,
                         "branch": state.get("branch", "")})
        deployment_wcfg = dict(wcfg)
        if isinstance(state.get("rapp_vision"), dict):
            deployment_wcfg["rapp_vision"] = dict(state["rapp_vision"])
        if notification_finalization and "deployment_receipts" in state:
            # Pages/CDN proof already succeeded and was durably bound to this
            # merge. A notification-only retry must reuse that evidence, not
            # turn a transient later CDN failure into a second deployment pass.
            try:
                receipts = persisted_deployment_receipts(
                    state, cfg, deployment_wcfg, submission, receipts,
                    profile_snapshot=profile_snapshot)
            except GateError as exc:
                return reconciliation_fail_closed(history, row, exc)
            try:
                finalize_success(
                    cfg, wcfg, history, row, submission, receipts,
                    state.get("next_state") or {},
                    int(state.get("cycle") or row.get("cycle") or 1),
                    transaction=note,
                    final_notification=state.get("final_notification"))
            except NotificationPending as exc:
                return final_notification_pending(history, row, exc)
            except GateError as exc:
                return reconciliation_fail_closed(history, row, exc)
            clear_transaction()
            log(f"reconciled notification for previously verified {slug}")
            return {"outcome": "reconciled-contributed", "slug": slug,
                    "receipts": receipts}
        try:
            clean_interrupted_vision_pr(state, deployment_wcfg)
            deployment_args = {
                "health": health,
                "transaction": note,
            }
            if profile_snapshot:
                deployment_args["profile_snapshot"] = profile_snapshot
            receipts = finish_platform_deployments(
                cfg, deployment_wcfg, workspace, clone, submission, receipts,
                **deployment_args)
        except GateError as e:
            if profile_snapshot:
                return reconciliation_fail_closed(history, row, e)
            return deployment_pending(
                history, row, e, transaction=note, wcfg=deployment_wcfg)
        except (DeploymentPending, AbortError, GateError, CommandError,
                subprocess.TimeoutExpired, json.JSONDecodeError, OSError,
                ValueError, KeyError) as e:
            return deployment_pending(
                history, row, e, transaction=note, wcfg=deployment_wcfg)
        try:
            finalize_success(
                cfg, wcfg, history, row, submission, receipts,
                state.get("next_state") or {},
                int(state.get("cycle") or row.get("cycle") or 1),
                transaction=note,
                final_notification=state.get("final_notification"))
        except NotificationPending as exc:
            return final_notification_pending(history, row, exc)
        except GateError as exc:
            return reconciliation_fail_closed(history, row, exc)
        clear_transaction()
        log(f"reconciled: {slug} was merged before the interruption and is now "
            f"recorded")
        return {"outcome": "reconciled-contributed", "slug": slug,
                "receipts": receipts}
    finally:
        shutil.rmtree(workspace, ignore_errors=True)


def _finish_art_notification(cfg, wcfg, history, row, submission, receipts,
                             transaction=None, final_notification=None):
    if receipts.get("collective_url"):
        view = receipts["collective_url"]
        kind = "pages"
        view_note = ""
    else:
        view, kind, view_note = verified_view(cfg, wcfg, submission)
    row["view_url"], row["view_kind"] = view, kind
    row["vision_url"] = (receipts.get("vision_url")
                         or (receipts.get("vision") or {}).get(
                             "watch_url", ""))
    save_history(history)

    expected = final_notification_record(
        cfg, wcfg, row, submission, receipts, view, view_note)
    record = (
        validate_final_notification(final_notification, expected)
        if final_notification is not None else expected)
    enqueue_final_art_notification(cfg, record, transaction)
    open_final_art(wcfg, receipts)
    return record


def finalize_success(cfg, wcfg, history, row, submission, receipts, next_state,
                     expected_cycle, transaction=None,
                     final_notification=None):
    """The one place a verified merge becomes ledger, chain, and message."""
    detail = (
        f"{submission['title']} ({submission['slug']}) merged as "
        f"{receipts['merge_commit'][:12]}")
    if transaction:
        transaction(
            phase="notification-preparing",
            notification_id=final_notification_id(
                row, submission, receipts))
    if row.get("outcome") == OUTCOME_CONTRIBUTED:
        if (row.get("slug") != submission["slug"]
                or row.get("merge_commit") != receipts["merge_commit"]):
            raise GateError(
                "contributed row conflicts with final notification transaction")
        _finish_art_notification(
            cfg, wcfg, history, row, submission, receipts,
            transaction=transaction, final_notification=final_notification)
        row.pop("notification_error", None)
        save_history(history)
        log(f"{row['role']}: {OUTCOME_CONTRIBUTED} — {row['detail'][:200]}")
        write_status(
            OUTCOME_CONTRIBUTED, row["detail"], role=row["role"],
            cycle=row.get("cycle"), children=row.get("children", 0),
            slug=row.get("slug", ""), pr=receipts.get("pr_url", ""))
        return {
            "outcome": OUTCOME_CONTRIBUTED,
            "role": row["role"],
            "detail": row["detail"],
            "receipts": receipts,
        }

    state_path = HOME / str(wcfg["creative_state_file"])
    previous_state = strict_load(state_path, {}, expect=dict)
    atomic_write_json(state_path,
                      merge_creative_state(previous_state, next_state,
                                           expected_cycle, submission["slug"],
                                           receipts, wcfg))
    return _finish(cfg, wcfg, history, row, OUTCOME_CONTRIBUTED,
                   detail, receipts, submission, transaction=transaction,
                   final_notification=final_notification)


def run_once(cfg=None, health=None, role=None, dry_run=False):
    """One worker pass. Returns a summary dict; never raises for policy."""
    cfg = cfg if cfg is not None else sentinel.config()
    wcfg = worker_config(cfg)
    health = health or (lambda phase: sentinel.run_health(receipts=True))

    if STOP.exists():
        return _skip("STOP file present")
    if not worker_enabled(cfg):
        return _skip("evolve_worker.enabled is false — the tick still owns art")
    if int(cfg.get("level", 1)) < 3:
        return _skip(f"level {cfg.get('level')} < 3; evolution is a level-3 act")
    # A worker that finds itself already inside a sentinel's process tree is a
    # recursion, not a cycle. Refuse the whole pass, not just the fan-out: a
    # nested run would try to publish, and one cycle must mean one submission.
    depth = SS.current_depth()
    if depth > 0:
        return _skip(f"nested run refused — {SS.DEPTH_ENV}={depth}; "
                     f"sub-sentinels may not run the worker")

    configured_worker = (
        cfg.get("evolve_worker")
        if isinstance(cfg.get("evolve_worker"), dict) else {})
    try:
        profile_snapshot = publication_profile_snapshot(wcfg)
        enforce_reviewed_controller_contract(
            wcfg, profile_snapshot, configured_worker)
    except (GateError, TypeError, ValueError) as e:
        return _skip(f"publication profile preflight failed: {e}")

    # One validated set of choices for the entire pass: pinned binaries, the
    # sanitized git environment with its generated credential helper, the gh
    # environment, and both repository forms. Everything below is handed this
    # context rather than re-deriving its own (#2).
    try:
        ctx = controller_for(wcfg)
    except (GateError, CommandError) as e:
        return _skip(f"controller cannot be built: {e}")

    lock = acquire_lock()
    if lock is None:
        return _skip("another worker holds the lock — a cycle is still running")

    workspace = None
    try:
        history = load_history()

        # Before anything else: finish or clean up an interrupted cycle. A
        # merged submission nobody recorded would otherwise make every future
        # cycle compute the wrong number and stay silent about live art (#5).
        healed = reconcile(cfg, wcfg, history, ctx, health)
        if healed:
            write_status(healed["outcome"], healed.get("detail", ""))
            return healed

        okb, used, cap = within_budget(history, wcfg)
        if not okb:
            return _skip(f"evolve budget spent ({used}/{cap}); "
                         f"repair capacity untouched")
        ready, why = cadence_ready(history, wcfg)
        if not ready:
            return _skip(why)

        turn = strict_load(TURN_PATH, {"i": 0}, expect=dict)
        roles = roles_for(wcfg)
        slug_role = role or roles[int(turn.get("i", 0)) % len(roles)]
        wcfg["role"] = slug_role
        wcfg["instance_name"] = sentinel.instance_name(cfg)

        # Health at the start: never spend a model on art while the estate is
        # broken, and never let a degraded-but-unlisted estate through (#3).
        # A probe that raises is a stop, not a shrug (#5).
        try:
            probe_health(health, "start", wcfg)
        except AbortError as e:
            return _skip(str(e))

        state_path = HOME / str(wcfg["creative_state_file"])
        creative = strict_load(state_path, {}, expect=dict)
        expected_cycle, expected_previous = next_creative_cycle(creative, wcfg)

        fcfg = profiled_fanout_config(wcfg, profile_snapshot)
        if dry_run:
            specs, note = SS.plan_children(fcfg, history, depth)
            summary = {"outcome": "dry-run", "role": slug_role,
                       "cycle": expected_cycle, "previous_slug": expected_previous,
                       "budget": f"{used}/{cap}", "depth": depth,
                       "publication_profile": (
                           profile_snapshot["profile"]
                           if profile_snapshot else ""),
                       "children": [s["name"] for s in specs], "fanout": note,
                       "maker_argv": maker_argv(
                           wcfg, HOME / "state" / "evolve-workspaces" /
                           "example" / "staging")}
            write_status("dry-run", note, role=slug_role, cycle=expected_cycle)
            return summary

        # Can this cycle publish at all? Ask before spending anything: three
        # live cycles made art and then failed at the push (#B).
        try:
            auth = assert_publish_auth(wcfg, ctx=ctx)
            log(f"publish auth ok — {auth}")
            vcfg = vision_config(wcfg)
            if vcfg["enabled"]:
                vwcfg = vision_worker_config(wcfg, vcfg)
                vctx = controller_for(
                    vwcfg, git_home=STATE / "git-home-rapp-vision")
                vision_auth = assert_publish_auth(vwcfg, ctx=vctx)
                log(f"RAPP Vision publish auth ok — {vision_auth}")
            if azure_image_config(wcfg)["enabled"]:
                visual = assert_visual_pipeline_ready(wcfg)
                log(f"visual generation ready — {visual}")
        except (AbortError, GateError, CommandError) as e:
            return _skip(f"publish auth unavailable: {e}")

        # The fan-out cast is decided BEFORE the workspace exists, so an
        # unavailable critic costs nothing (#7).
        specs, fanout_note = ([], "fan-out disabled")
        if SS.enabled(fcfg):
            specs, fanout_note = SS.plan_children(fcfg, history, depth)
            if not specs:
                return _skip(f"fan-out unavailable: {fanout_note}")

        install_signal_handlers()
        workspace = _make_workspace(wcfg)
        clone = workspace / "clone"          # controller-private, never shared
        staging = workspace / "staging"      # the maker's whole world
        # Precreated, because the maker has file tools and no shell: nothing
        # in its toolset can make a directory (#1).
        prepare_staging(staging)
        (workspace / "runtime").mkdir(parents=True, exist_ok=True)
        base_sha = _clone_repo(wcfg, clone, ctx)
        assert_repo_integrity(clone, wcfg, ctx)
        known_slugs = base_branch_slugs(clone, wcfg.get("base_branch", "main"),
                                        wcfg, ctx)
        if creative:
            atomic_write_json(staging / "state-in.json", creative)

        # One row per cycle, written BEFORE the first model process of any
        # kind, and the child debit lands with it: a future that raises, a
        # SIGTERM mid-wave or a crash must never hand back credit that was
        # already spent (#9).
        stamp = sentinel.now()
        row = {
            "id": uuid.uuid4().hex,
            "at": stamp.isoformat(timespec="seconds"),
            "mode": "evolve",
            "role": slug_role,
            "cycle": expected_cycle,
            "outcome": "pending",
            "children": len(specs),
            "child_failures": [],
            "result": "",
        }
        record(history, row)
        turn["i"] = int(turn.get("i", 0)) + 1
        atomic_write_json(TURN_PATH, turn)
        write_status("running", "cycle started", role=slug_role,
                     cycle=expected_cycle, children=len(specs))

        finalists, digest = None, None
        if specs:
            log(f"fanning out to {len(specs)} sub-sentinels ({fanout_note})")
            situation = fanout_situation(cfg, wcfg, slug_role, expected_cycle,
                                         expected_previous)
            # Whatever the per-cycle process cap leaves after the planned
            # children and the maker is what format repairs may spend (A2).
            spare = SS.SpareProcesses(
                int(fcfg.get("max_processes_per_cycle", 6)) - len(specs) - 1)
            transcripts = LOGS / "subsentinels"
            results = SS.run_children(specs, fcfg, workspace, expected_cycle,
                                      sentinel.instance_name(cfg), slug_role,
                                      _prior_submissions(clone), depth, log,
                                      situation, transcripts, spare)
            # Processes, not children: a repair is a real spend and is
            # debited like one.
            row["children"] = max(len(specs),
                                  sum(int(r.get("processes") or 1)
                                      for r in results))
            row["child_failures"] = [f"{r['role']}: {r['error']}"
                                     for r in results if not r["ok"]]
            row["child_repairs"] = sum(1 for r in results if r.get("repaired"))
            save_history(history)
            try:
                finalists, digest = SS.aggregate(results, fcfg)
                if profile_snapshot:
                    _reject_credential_material(
                        {"finalists": finalists, "digest": digest},
                        "reviewed-PNG fan-out",
                        path="fanout",
                    )
            except (SS.FanoutError, GateError) as e:
                return _finish(cfg, wcfg, history, row, OUTCOME_FANOUT, str(e))
            atomic_write_json(staging / "finalists.json",
                              {"finalists": finalists, "digest": digest})
            atomic_write_json(staging / "round1.json",
                              SS.round1_array(finalists))
            log(f"aggregated {len(finalists)} finalists from "
                f"{digest['healthy']}/{len(results)} healthy children")

        # The bounded read context the maker gets INSTEAD of a repository.
        atomic_write_json(staging / "context" / "prior.json",
                          _prior_submissions(clone))
        prompt = build_prompt(cfg, wcfg, slug_role, staging, expected_cycle,
                              expected_previous, history, finalists, digest)
        (workspace / "prompt.txt").write_text(prompt, encoding="utf-8")
        # Everything the controller put there, hashed, BEFORE the model runs.
        # Anything that differs afterwards is something the model did (#1).
        baseline = staging_manifest(staging)
        log(f"handing {slug_role} its situation (cycle {expected_cycle}, "
            f"budget {used + 1}/{cap})")
        status, output = run_model(staging, prompt, wcfg, depth,
                                   workspace / "runtime")
        try:
            (LOGS / f"evolve-worker-{slug_role}-{sentinel.now():%Y%m%d-%H%M%S}.log"
             ).write_text(output, encoding="utf-8")
        except OSError:
            pass
        row["result"] = sentinel.result_line(output)[:300]
        save_history(history)

        if status != "ok":
            return _finish(cfg, wcfg, history, row,
                           OUTCOME_TIMEOUT if status == OUTCOME_TIMEOUT
                           else OUTCOME_FAILED,
                           output.strip()[:300] or status)

        line = sentinel.result_line(output)
        try:
            # The staging tree first — no git anywhere near it — and only then
            # the two validated blobs into the controller's own clone.
            assert_no_git(staging)
            # Whole tree first: a draft, a note, a rewritten prior.json or a
            # second copy of the piece anywhere in staging fails the cycle
            # before its output is even read.
            verify_staging_tree(
                staging, baseline, wcfg, profile_snapshot=profile_snapshot)
            visual_receipt = materialize_azure_image(staging, wcfg)
            if visual_receipt:
                log("Azure image accepted by multimodal review — "
                    f"score {visual_receipt['score']}, "
                    f"attempt {visual_receipt['attempts']}")
            submission = gate_directory(staging / "out", wcfg, expected_cycle,
                                        expected_previous,
                                        SS.expected_round1(finalists)
                                        if finalists else None,
                                        known_slugs,
                                        profile_snapshot=profile_snapshot)
            install_into_clone(clone, submission)
            verify_clone_scope(clone, submission, wcfg, base_sha, ctx)
        except GateError as e:
            if str(line).upper().startswith("DECLINED") and "no new submission" in str(e):
                return _finish(cfg, wcfg, history, row, OUTCOME_DECLINED, line)
            return _finish(cfg, wcfg, history, row, OUTCOME_REJECTED,
                           f"gate: {e}")
        except (CommandError, subprocess.TimeoutExpired, OSError) as e:
            return _finish(cfg, wcfg, history, row, OUTCOME_FAILED,
                           f"gate: {type(e).__name__}: {e}")

        try:
            next_state = validate_next_state(staging / "state-out.json", wcfg,
                                             expected_cycle, submission["slug"])
        except GateError as e:
            return _finish(cfg, wcfg, history, row, OUTCOME_REJECTED, f"gate: {e}")

        note = transaction_writer(row["id"], {
            "phase": "gated", "slug": submission["slug"], "cycle": expected_cycle,
            "role": slug_role, "repo": wcfg["repo"],
            "base_branch": wcfg.get("base_branch", "main"),
            # The whole gated submission record, including meta: a
            # reconciled cycle must be able to write the same ledger entry
            # and send the same message the interrupted one would have.
            "submission": {k: submission[k] for k in
                           ("slug", "title", "kind", "meta", "meta_path",
                            "piece_path", "meta_sha256", "piece_sha256", "image")
                           if k in submission},
            # (bytes are deliberately not persisted: the digests are the
            # contract, and reconciliation re-reads the merged file anyway)
            "next_state": next_state,
            # A retry must deploy the entry that was gated in this cycle even
            # if the operator changes channel metadata before Pages finishes.
            "rapp_vision": vision_config(wcfg),
            "profile_snapshot": profile_snapshot,
        })
        row["slug"] = submission["slug"]
        save_history(history)
        note()
        try:
            receipts = publish(clone, submission, wcfg, health,
                               transaction=note, ctx=ctx,
                               profile_snapshot=profile_snapshot)
        except ChecksPending as e:
            return collective_checks_pending(
                history, row, e, transaction=note)
        except MergeAmbiguous as e:
            return canonical_merge_pending(
                history, row, e, transaction=note)
        except AbortError as e:
            clear_transaction()
            return _finish(cfg, wcfg, history, row, OUTCOME_ABORTED, str(e))
        except GateError as e:
            clear_transaction()
            return _finish(cfg, wcfg, history, row, OUTCOME_REJECTED, f"gate: {e}")
        except (CommandError, subprocess.TimeoutExpired, json.JSONDecodeError,
                OSError, ValueError, KeyError) as e:
            clear_transaction()
            return _finish(cfg, wcfg, history, row, OUTCOME_FAILED,
                           f"publish: {type(e).__name__}: {e}")

        note(phase="collective-merged", collective_receipts=receipts)
        try:
            receipts = finish_platform_deployments(
                cfg, wcfg, workspace, clone, submission, receipts, health,
                transaction=note)
        except (DeploymentPending, AbortError, GateError, CommandError,
                subprocess.TimeoutExpired, json.JSONDecodeError, OSError,
                ValueError, KeyError) as e:
            # The Public Art merge is already real. Keep the row and
            # transaction pending so the next pass retries only deployment;
            # never spend another model or announce a partial release.
            return deployment_pending(
                history, row, e, transaction=note, wcfg=wcfg)

        # Only here — both merged, re-read, byte-checked and live on Pages —
        # does the ledger move or a message leave the machine.
        try:
            summary = finalize_success(
                cfg, wcfg, history, row, submission, receipts,
                next_state, expected_cycle, transaction=note)
        except NotificationPending as exc:
            return final_notification_pending(history, row, exc)
        clear_transaction()
        return summary
    except LedgerError as e:
        log(f"FAIL-CLOSED: {e}")
        write_status("fail-closed", str(e))
        _notify_once(cfg, "ledger", f"\u26A0\uFE0F {sentinel.instance_name(cfg)}: "
                                    f"the evolve worker stopped — {e}")
        return {"outcome": "fail-closed", "reason": str(e)}
    except KeyboardInterrupt as e:
        # A signal arrived. The handler already killed the tree; record the
        # interruption honestly and let the finally block clean up.
        log(f"interrupted: {e}")
        write_status("interrupted", str(e))
        return {"outcome": "interrupted", "reason": str(e)}
    except Exception as e:
        log(f"worker crashed: {type(e).__name__}: {e}")
        write_status("crashed", f"{type(e).__name__}: {e}")
        _notify_once(cfg, "crash", f"\u26A0\uFE0F {sentinel.instance_name(cfg)}: "
                                   f"the evolve worker crashed — "
                                   f"{type(e).__name__}: {e}")
        return {"outcome": "crashed", "reason": f"{type(e).__name__}: {e}"}
    finally:
        # Order matters: kill everything this pass started, THEN delete the
        # workspace those processes were writing into, THEN release the lock.
        # Releasing first would let the next pass start while a 30-minute
        # model is still running against a directory we are deleting (#4).
        still_running = kill_tracked()
        if still_running:
            log(f"terminated {still_running} live model process tree(s) on the "
                f"way out")
        if workspace is not None:
            shutil.rmtree(workspace, ignore_errors=True)
        release_lock(lock)


def fanout_situation(cfg, wcfg, role, cycle, previous_slug):
    """The bounded description of the moment, handed to tool-less children."""
    if publication_profile_snapshot(wcfg):
        artifact = (
            "publication profile: azure-reviewed-png\n"
            "all candidates must be visual image concepts for one generated "
            "PNG; discuss composition, palette, lighting, texture, focal "
            "hierarchy, and visible risks only\n"
            "no SVG, markdown, text, JSON, code, interactive work, or "
            "nonvisual fallback is permitted\n"
            "the maker writes a visual piece.prompt; trusted controller code "
            "alone generates and reviews the PNG"
        )
    else:
        artifact = (
            f"the piece must be one self-contained file "
            f"({', '.join(allowed_kinds(wcfg))}) "
            f"under {int(wcfg.get('max_piece_bytes', 51200)) // 1024} KB, "
            "CC0-1.0, in submissions/<slug>/"
        )
    return (f"collective: {sentinel.instance_name(cfg)}\n"
            f"neighbor acting: {role}\n"
            f"cycle: {cycle} (the previous submission was "
            f"{previous_slug or 'none — this is the first'})\n"
            f"commons: {wcfg.get('repo')}\n"
            f"standing directive: "
            f"{sentinel.evolve_brief(cfg) or 'none'}\n"
            f"{artifact}")


def _finish(cfg, wcfg, history, row, outcome, detail, receipts=None,
            submission=None, transaction=None, final_notification=None):
    """Close the row out honestly, then chain-frame and notify."""
    row["outcome"] = outcome
    row["detail"] = str(detail)[:400]
    if receipts:
        row["pr_url"] = receipts.get("pr_url")
        row["merge_commit"] = receipts.get("merge_commit")
        row["paths"] = receipts.get("paths")
    if submission:
        row["slug"] = submission["slug"]
    save_history(history)

    _emit_frame(row["role"], {
        "act": "evolve", "outcome": outcome, "cycle": row.get("cycle"),
        "result": row["detail"][:300], "model": wcfg.get("model"),
        "merged": bool(receipts and receipts.get("merge_commit")),
        "pr": (receipts or {}).get("pr_url", ""),
        "children": int(row.get("children") or 0),
        "child_failures": row.get("child_failures") or [],
    })

    text = notification_for(outcome, row["role"], str(detail), receipts)
    if outcome == OUTCOME_CONTRIBUTED and submission and receipts:
        _finish_art_notification(
            cfg, wcfg, history, row, submission, receipts,
            transaction=transaction, final_notification=final_notification)
    elif (text
          and not (outcome == OUTCOME_REJECTED
                   and publication_profile_name(wcfg)
                   == AZURE_REVIEWED_PNG_PROFILE)
          and (outcome != OUTCOME_DECLINED or wcfg.get("notify_declines"))):
        sentinel.notify(cfg, text)
    log(f"{row['role']}: {outcome} — {row['detail'][:200]}")
    write_status(outcome, row["detail"], role=row["role"],
                 cycle=row.get("cycle"), children=row.get("children", 0),
                 slug=row.get("slug", ""), pr=(receipts or {}).get("pr_url", ""))
    return {"outcome": outcome, "role": row["role"], "detail": row["detail"],
            "receipts": receipts or {}}


def _make_workspace(wcfg):
    root = Path(wcfg["workspace_root"])
    if not root.is_absolute():
        root = HOME / root
    root.mkdir(parents=True, exist_ok=True)
    workspace = root / f"cycle-{sentinel.now():%Y%m%d-%H%M%S}-{uuid.uuid4().hex[:8]}"
    workspace.mkdir(parents=True)
    return workspace


def _clone_repo(wcfg, clone, ctx=None):
    """Build the controller's clone without ever running `git clone`.

    `git clone <url>` reads global and system config BEFORE it resolves the
    url, so an `url.<attacker>.insteadOf <canonical>` rewrite produced a
    flawless clone of somebody else's repository — and the integrity check,
    which reads local config, then confirmed it. The repository has to be
    assembled by the controller instead:

      init an empty repo -> set origin to the validated canonical url ->
      verify integrity (now meaningful: the config is ours and nothing has
      touched the network) -> fetch through the chokepoint -> check out.

    Every one of those commands runs in the sanitized environment, so no
    rewrite, proxy, helper, template or alternates file from outside can
    take part.
    """
    ctx = ctx or controller_for(wcfg)
    clone = Path(clone)
    canonical = (ctx.repo.transport if ctx.repo
                 else validate_repo_url(wcfg["repo"], wcfg))
    base = wcfg.get("base_branch", "main")
    git_t = int(wcfg.get("git_timeout_s", 600))
    depth = int(wcfg.get("clone_depth", 50) or 0)

    clone.mkdir(parents=True, exist_ok=True)
    _git(clone, "init", "-q", "-b", base, timeout=git_t, ctx=ctx)
    _git(clone, "config", "remote.origin.url", canonical, timeout=git_t,
         ctx=ctx)
    _git(clone, "config", "remote.origin.fetch",
         f"+refs/heads/{base}:refs/remotes/origin/{base}", timeout=git_t,
         ctx=ctx)
    # Before the first network byte, not after it.
    assert_repo_integrity(clone, wcfg, ctx=ctx)

    fetch_args = ["fetch", "--no-tags"]
    if depth > 0:
        fetch_args += ["--depth", str(depth)]
    fetch_args += ["origin", base]
    _git_remote(clone, wcfg, *fetch_args, timeout=git_t, ctx=ctx)
    _git(clone, "checkout", "-q", "-B", base, f"origin/{base}",
         timeout=git_t, ctx=ctx)
    return _git(clone, "rev-parse", "HEAD", timeout=git_t, ctx=ctx).strip()


def _repo_url(repo):
    """owner/name, a full URL, or a local path (used by the tests)."""
    text = str(repo).strip()
    if "://" in text or text.startswith(("git@", "/", ".", "~")):
        return os.path.expanduser(text)
    return f"https://github.com/{text}.git"


def _prior_submissions(clone, limit=200):
    """Every published submission, bounded, for the children to dig through.

    Read from the clone by the PARENT and handed over as plain JSON: a child
    that never sees a repository cannot be tempted to write to one.
    """
    root = Path(clone) / "submissions"
    prior = []
    if not root.is_dir():
        return prior
    for directory in sorted(p for p in root.iterdir() if p.is_dir()):
        meta_path = directory / "meta.json"
        try:
            meta = json.loads(meta_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError, UnicodeDecodeError):
            prior.append({"slug": directory.name, "title": "", "kind": "",
                          "statement": "(meta.json unreadable)"})
            continue
        statement = str(meta.get("_artist_statement")
                        or meta.get("_inspired_by") or "")
        prior.append({
            "slug": str(meta.get("slug") or directory.name),
            "title": str(meta.get("title") or ""),
            "kind": str(meta.get("kind") or ""),
            "submitted_at": str(meta.get("submitted_at") or ""),
            "remix_of": meta.get("remix_of"),
            "statement": statement[:600],
        })
        if len(prior) >= limit:
            break
    return prior


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--dry-run", action="store_true",
                    help="run every gate, spend no model")
    ap.add_argument("--role", default=None,
                    help="force one neighbor instead of the rotation")
    args = ap.parse_args(argv)
    summary = run_once(role=args.role, dry_run=args.dry_run)
    # Print the decision: a dry run that says nothing is indistinguishable from
    # a dry run that did nothing, and this is the command an operator uses to
    # check the wiring after install-launchd.sh --with-evolve-worker.
    print(json.dumps(summary, indent=2, default=str), flush=True)
    if summary.get("outcome") in ("fail-closed", "crashed"):
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
