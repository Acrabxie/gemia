"""Public, machine-local Lumeri configuration.

``~/.gemia/config.json`` predates the desktop product and may contain API
keys and other credentials.  It remains a compatibility/secret source, but it
must never be the file a desktop shell or CLI reads as public state.

This module owns the additive ``~/.lumeri/config.toml`` contract.  Only an
explicit non-secret schema is serialized.  Runtime callers may merge those
public selections over the legacy secret snapshot; values from this file win.
"""
from __future__ import annotations

import contextlib
import copy
import json
import os
import shutil
import tempfile
import tomllib
import urllib.request
from pathlib import Path
from typing import Any

SCHEMA_VERSION = 1
DEFAULT_RUNTIME_URL = "http://127.0.0.1:7788"
DEFAULT_CODEX_BRIDGE_URL = "http://127.0.0.1:7808"
_PUBLIC_PROFILE_FIELDS = (
    "provider",
    "name",
    "model",
    "effort",
    "location",
    "vertex_project",
    "vertex_location",
)


class LocalConfigError(ValueError):
    """The public local configuration request is invalid."""


def config_path() -> Path:
    override = os.environ.get("LUMERI_LOCAL_CONFIG_PATH", "").strip()
    return Path(override).expanduser() if override else Path.home() / ".lumeri" / "config.toml"


def _legacy_json_path(path: Path) -> Path:
    return path.with_suffix(".json")


def _read_payload(path: Path) -> dict[str, Any]:
    try:
        raw = path.read_text(encoding="utf-8")
        payload = json.loads(raw) if path.suffix == ".json" else tomllib.loads(raw)
    except (OSError, json.JSONDecodeError, tomllib.TOMLDecodeError):
        return {}
    return payload if isinstance(payload, dict) else {}


def _toml_key(value: str) -> str:
    return value if value.replace("_", "").isalnum() else json.dumps(value, ensure_ascii=False)


def _toml_value(value: Any) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, int):
        return str(value)
    if isinstance(value, float):
        return repr(value)
    return json.dumps(str(value), ensure_ascii=False)


def _toml_dump(payload: dict[str, Any]) -> str:
    """Serialize the small public config contract without a third-party writer."""

    lines: list[str] = []

    def write_table(table: dict[str, Any], prefix: tuple[str, ...] = ()) -> None:
        if prefix:
            lines.append("[" + ".".join(_toml_key(item) for item in prefix) + "]")
        for key, value in table.items():
            if not isinstance(value, dict):
                lines.append(f"{_toml_key(str(key))} = {_toml_value(value)}")
        nested = [(str(key), value) for key, value in table.items() if isinstance(value, dict)]
        for key, value in nested:
            if lines and lines[-1] != "":
                lines.append("")
            write_table(value, (*prefix, key))

    write_table(payload)
    return "\n".join(lines).rstrip() + "\n"


def _bridge_capability_snapshot(
    *, bridge_url: str = DEFAULT_CODEX_BRIDGE_URL, timeout: float = 0.35
) -> tuple[bool, bool]:
    request = urllib.request.Request(
        f"{bridge_url.rstrip('/')}/health",
        headers={"Accept": "application/json"},
    )
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    try:
        with opener.open(request, timeout=timeout) as response:
            payload = json.loads(response.read().decode("utf-8"))
    except Exception:
        return False, False
    if not isinstance(payload, dict) or payload.get("ok") is not True:
        return False, False
    capabilities = payload.get("capabilities")
    fast_mode = bool(
        isinstance(capabilities, dict) and capabilities.get("fast_mode") is True
    )
    return True, fast_mode


def detect_capabilities() -> dict[str, Any]:
    """Return only capabilities verified on this machine right now.

    The bridge health endpoint is intentionally credential-free.  OAuth state,
    tokens, account identifiers and bridge request bodies are never inspected.
    """

    bridge_available, bridge_fast_mode = _bridge_capability_snapshot()
    return {
        "codex_cli": {"available": bool(shutil.which("codex"))},
        "openai_subscription_bridge": {"available": bridge_available},
        "fast_mode": {
            "available": bridge_available and bridge_fast_mode,
            "provider": "openai_subscription",
            "quality_policy": "reasoning_unchanged",
        },
    }


def _legacy_profiles(secret_config: dict[str, Any]) -> tuple[dict[str, dict[str, Any]], str]:
    # Import lazily to avoid making brain_config depend on this module's path.
    from gemia import brain_config

    profiles, active = brain_config.provider_profiles(secret_config)
    return copy.deepcopy(profiles), str(active or "")


def _public_profiles(secret_config: dict[str, Any]) -> tuple[dict[str, dict[str, str]], str]:
    profiles, active = _legacy_profiles(secret_config)
    public: dict[str, dict[str, str]] = {}
    for profile_id, profile in profiles.items():
        item = {
            field: str(profile.get(field) or "")
            for field in _PUBLIC_PROFILE_FIELDS
        }
        item["provider"] = item["provider"] or str(profile_id)
        item["effort"] = item["effort"] or "medium"
        public[str(profile_id)] = item
    return public, active


def _normalize_stored(payload: dict[str, Any]) -> dict[str, Any]:
    model = payload.get("model") if isinstance(payload.get("model"), dict) else {}
    raw_profiles = model.get("profiles") if isinstance(model.get("profiles"), dict) else {}
    profiles: dict[str, dict[str, str]] = {}
    for profile_id, raw in raw_profiles.items():
        if not isinstance(raw, dict):
            continue
        profiles[str(profile_id)] = {
            field: str(raw.get(field) or "")
            for field in _PUBLIC_PROFILE_FIELDS
        }
    raw_fast = payload.get("features") if isinstance(payload.get("features"), dict) else {}
    fast = raw_fast.get("fast_mode")
    fast_enabled = fast.get("enabled") is True if isinstance(fast, dict) else fast is True
    return {
        "active_profile": str(model.get("active_profile") or ""),
        "profiles": profiles,
        "fast_mode": fast_enabled,
    }


def build_snapshot(
    secret_config: dict[str, Any],
    *,
    stored: dict[str, Any] | None = None,
    capabilities: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Build the complete public contract without copying any secret fields."""

    legacy_profiles, legacy_active = _public_profiles(secret_config)
    stored_payload = stored or {}
    normalized = _normalize_stored(stored_payload)
    runtime = stored_payload.get("runtime") if isinstance(stored_payload.get("runtime"), dict) else {}
    runtime_url = str(runtime.get("url") or DEFAULT_RUNTIME_URL).rstrip("/")
    if not runtime_url.startswith("http://127.0.0.1:") and not runtime_url.startswith("http://localhost:"):
        runtime_url = DEFAULT_RUNTIME_URL
    profiles = normalized["profiles"] or legacy_profiles
    active = normalized["active_profile"] or legacy_active
    if active not in profiles and legacy_active in profiles:
        active = legacy_active
    capability_snapshot = copy.deepcopy(capabilities or detect_capabilities())
    available = bool(
        (capability_snapshot.get("fast_mode") or {}).get("available")
    )
    provider = str((profiles.get(active) or {}).get("provider") or "")
    enabled = bool(normalized["fast_mode"])
    return {
        "schema_version": SCHEMA_VERSION,
        "runtime": {"url": runtime_url},
        "model": {"active_profile": active, "profiles": profiles},
        "features": {
            "fast_mode": {
                "enabled": enabled,
                "available": available,
                "effective": enabled
                and available
                and provider == "openai_subscription",
                "provider": "openai_subscription",
                "quality_policy": "reasoning_unchanged",
            }
        },
        "capabilities": capability_snapshot,
    }


def _atomic_write(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd, temporary = tempfile.mkstemp(prefix=".config-", suffix=".toml", dir=path.parent)
    tmp_path = Path(temporary)
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(_toml_dump(payload))
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp_path, path)
    finally:
        with contextlib.suppress(FileNotFoundError):
            tmp_path.unlink()


def load_or_create(
    secret_config: dict[str, Any],
    *,
    path: Path | None = None,
    capabilities: dict[str, Any] | None = None,
) -> dict[str, Any]:
    target = path or config_path()
    stored = _read_payload(target)
    if not stored and target.suffix != ".json":
        stored = _read_payload(_legacy_json_path(target))
    snapshot = build_snapshot(
        secret_config,
        stored=stored,
        capabilities=capabilities,
    )
    if _read_payload(target) != snapshot:
        _atomic_write(target, snapshot)
    return snapshot


def write_public_update(
    secret_config: dict[str, Any],
    body: dict[str, Any],
    *,
    path: Path | None = None,
    capabilities: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Persist validated public fields after brain_config applied ``body``."""

    if "fast_mode" in body and not isinstance(body.get("fast_mode"), bool):
        raise LocalConfigError("fast_mode must be a boolean")
    target = path or config_path()
    stored = _read_payload(target)
    if not stored and target.suffix != ".json":
        stored = _read_payload(_legacy_json_path(target))
    normalized = _normalize_stored(stored)
    public_profiles, active = _public_profiles(secret_config)
    fast_enabled = (
        bool(body["fast_mode"])
        if "fast_mode" in body
        else bool(normalized["fast_mode"])
    )
    seed = {
        "model": {"active_profile": active, "profiles": public_profiles},
        "features": {"fast_mode": {"enabled": fast_enabled}},
    }
    snapshot = build_snapshot(
        secret_config,
        stored=seed,
        capabilities=capabilities,
    )
    _atomic_write(target, snapshot)
    return snapshot


def merge_with_secret_config(
    secret_config: dict[str, Any], *, path: Path | None = None
) -> dict[str, Any]:
    """Overlay the public authority onto a copied compatibility snapshot."""

    target = path or config_path()
    stored = _read_payload(target)
    if not stored and target.suffix != ".json":
        stored = _read_payload(_legacy_json_path(target))
    if not stored:
        return copy.deepcopy(secret_config)
    normalized = _normalize_stored(stored)
    merged = copy.deepcopy(secret_config)
    secret_profiles, legacy_active = _legacy_profiles(merged)
    for profile_id, public_profile in normalized["profiles"].items():
        profile = dict(secret_profiles.get(profile_id) or {})
        for field in _PUBLIC_PROFILE_FIELDS:
            if field in public_profile:
                profile[field] = public_profile[field]
        secret_profiles[profile_id] = profile
    active = normalized["active_profile"] or legacy_active
    if active in secret_profiles:
        merged["brain_active_profile"] = active
    merged["brain_provider_profiles"] = secret_profiles
    merged["lumeri_fast_mode"] = bool(normalized["fast_mode"])
    active_profile = secret_profiles.get(active) or {}
    logical_provider = str(active_profile.get("provider") or "")
    merged["lumeri_v3_provider"] = (
        "openai" if logical_provider == "openai_subscription" else logical_provider
    )
    merged["lumeri_v3_model"] = str(active_profile.get("model") or "")
    merged["lumeri_v3_effort"] = str(active_profile.get("effort") or "medium")
    return merged


def fast_mode_preference(*, default: bool = False, path: Path | None = None) -> bool:
    target = path or config_path()
    stored = _read_payload(target)
    if not stored and target.suffix != ".json":
        stored = _read_payload(_legacy_json_path(target))
    if not stored:
        return bool(default)
    return bool(_normalize_stored(stored)["fast_mode"])
