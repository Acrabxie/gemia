"""Per-request identity resolution — the seam that kills the global-identity
architecture one phase at a time (docs/identity-architecture-plan.md).

Today's reality: ``accounts.current_account_id()`` reads one process-global
``active.json``. Every open UI shares it, and ``switch_account`` from any
client silently retargets what /library, /auth/session, and session snapshots
return for all the others.

This module is the resolution order every route goes through INSTEAD of
touching the global directly:

1. **Explicit per-request pin** — ``X-Lumeri-Account: <account_id>`` header.
   Honored only when the account exists locally; an explicit pin to an
   unknown account resolves to ``None`` (→ the caller's 401 path), NEVER a
   silent fallback — a stale pin acting as whichever account happens to be
   globally active is exactly the bug class this seam exists to kill.
2. **Process-global fallback** — ``accounts.current_account_id()`` — so
   existing single-user flows keep working unchanged while call sites
   migrate.

TRUST MODEL (deliberate, pre-security-pass): the header is NOT
authentication. The server is localhost-first and the project's one-shot
security pass is explicitly deferred by user decision; anyone who can reach
the socket can already flip the global via POST /accounts/switch. What the
pin fixes TODAY is cross-client identity bleed (client A switching accounts
no longer retargets client B's open views). The security pass later replaces
"header names an account" with "header carries a signed session token issued
at login" — same seam, one function to harden.
"""
from __future__ import annotations

from typing import Any

from gemia import public_identity as accounts

PIN_HEADER = "X-Lumeri-Account"


def resolve_account_id(handler: Any | None = None) -> str | None:
    """Resolve the acting account for one request. See module docstring."""
    if handler is not None:
        headers = getattr(handler, "headers", None)
        pinned = headers.get(PIN_HEADER) if headers is not None else None
        if pinned is not None and str(pinned).strip():
            pinned = str(pinned).strip()
            try:
                # account_profile_path validates the id shape (AuthError on
                # anything outside [a-zA-Z0-9_-]{8,80}) — a malformed pin is
                # an unknown account, not a 500.
                if accounts.account_profile_path(pinned).exists():
                    return pinned
            except Exception:
                pass
            return None
    return accounts.current_account_id()


__all__ = ["PIN_HEADER", "resolve_account_id"]
