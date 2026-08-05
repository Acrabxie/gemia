"""Central defaults for timeouts, paths, and other tunables.

Anything imported here is treated as a stable constant across the codebase
so deployment tuning (e.g. raising network timeouts behind a slow proxy) only
needs to touch one file.

Environment variables override individual values where useful — see the
``getenv_*`` helpers below.
"""
from __future__ import annotations

import os
from pathlib import Path

# ── Paths ─────────────────────────────────────────────────────────────
GEMIA_HOME = Path.home() / ".gemia"
CONFIG_PATH = GEMIA_HOME / "config.json"
WORKSPACE_DIR = GEMIA_HOME / "workspace"
ACCOUNTS_DIR = GEMIA_HOME / "accounts"
CACHE_DIR = GEMIA_HOME / "cache"

# ── HTTP timeouts (seconds) ───────────────────────────────────────────
# Native Gemini planning request (text-only or small inline payloads).
HTTP_TIMEOUT_GEMINI_PLAN = 90
# Resumable upload start handshake.
HTTP_TIMEOUT_GEMINI_UPLOAD_START = 60
# Large media upload finalize.
HTTP_TIMEOUT_GEMINI_UPLOAD_FINISH = 300
# File status polling for the Files API.
HTTP_TIMEOUT_GEMINI_FILE_STATUS = 60
# Google OAuth token exchange.
HTTP_TIMEOUT_GOOGLE_OAUTH = 20
# Google tokeninfo verification fallback.
HTTP_TIMEOUT_GOOGLE_TOKENINFO = 10

# ── AI retry / polling ────────────────────────────────────────────────
# Native Gemini retry attempts on transient failure (5xx, network, timeout).
GEMINI_RETRY_ATTEMPTS = 3
# Veo long-running operation polling (see veo_client for live values).
VEO_POLL_INITIAL_SEC = 5.0
VEO_POLL_MAX_SEC = 60.0
VEO_POLL_GROWTH = 2.0
VEO_POLL_JITTER = 0.2

# ── Plan response cache ───────────────────────────────────────────────
PLAN_CACHE_TTL_SEC = int(os.environ.get("GEMIA_PLAN_CACHE_TTL_SEC", "3600"))
PLAN_CACHE_MAX_ENTRIES = int(os.environ.get("GEMIA_PLAN_CACHE_MAX", "512"))
PLAN_CACHE_ENABLED = os.environ.get("GEMIA_PLAN_CACHE", "1") != "0"


def getenv_int(name: str, default: int) -> int:
    """Read an int env var, falling back to ``default`` on parse error."""
    raw = os.environ.get(name, "").strip()
    if not raw:
        return default
    try:
        return int(raw)
    except ValueError:
        return default
