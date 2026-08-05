"""Incremental Creative IR for algorithm-driven productions.

The IR is not a generated timeline dump.  It is a small, revisioned bridge
between human intent and deterministic project code: stable beat identities,
design-system decisions, program entrypoint, and targeted revision scopes.
Each mutation is one JSON-pointer patch so the model never needs to replace a
mega-document in one response.
"""
from __future__ import annotations

import copy
import hashlib
import json
from datetime import datetime, timezone
from typing import Any, Mapping


MAX_PATCH_VALUE_BYTES = 64 * 1024


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def contract_digest(contract: Mapping[str, Any]) -> str:
    payload = json.dumps(
        contract,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def default_creative_ir(contract: Mapping[str, Any]) -> dict[str, Any]:
    deliverable = contract.get("deliverable") if isinstance(contract.get("deliverable"), Mapping) else {}
    return {
        "schema": "lumeri.creative-ir",
        "version": 1,
        "revision": 0,
        "updated_at": _now(),
        "contract_digest": contract_digest(contract),
        "intent": {
            "brief": str(contract.get("brief") or ""),
            "audience": "",
            "release_context": "",
            "creative_thesis": "",
        },
        "canvas": {
            "duration_sec": deliverable.get("duration_sec"),
            "width": deliverable.get("width"),
            "height": deliverable.get("height"),
            "fps": deliverable.get("fps"),
        },
        # Maps keep stable ids patchable; order is a separate small list.
        "beats": {},
        "beat_order": [],
        "systems": {
            "edit": {},
            "motion": {},
            "composition": {},
            "typography": {},
            "color": {},
            "audio": {},
            "spatial": {},
            "continuity": {},
        },
        "asset_strategy": {},
        "program": {
            "entrypoint": "project://design/main.py",
            "inputs": {},
            "outputs": {},
        },
        "active_revision_scope": None,
    }


def normalize_creative_ir(
    value: Mapping[str, Any] | None,
    *,
    contract: Mapping[str, Any],
) -> dict[str, Any]:
    if value is None:
        return default_creative_ir(contract)
    if not isinstance(value, Mapping):
        raise ValueError("creative_ir must be an object")
    result = copy.deepcopy(dict(value))
    result["schema"] = "lumeri.creative-ir"
    result["version"] = 1
    revision = result.get("revision", 0)
    if isinstance(revision, bool) or not isinstance(revision, int) or revision < 0:
        raise ValueError("creative_ir.revision must be a non-negative integer")
    for field, expected_type in (
        ("intent", dict),
        ("canvas", dict),
        ("beats", dict),
        ("beat_order", list),
        ("systems", dict),
        ("asset_strategy", dict),
        ("program", dict),
    ):
        if not isinstance(result.get(field), expected_type):
            raise ValueError(f"creative_ir.{field} must be {expected_type.__name__}")
    if any(not isinstance(value, str) or not value for value in result["beat_order"]):
        raise ValueError("creative_ir.beat_order must contain non-empty beat ids")
    if len(set(result["beat_order"])) != len(result["beat_order"]):
        raise ValueError("creative_ir.beat_order contains duplicate ids")
    missing = [beat_id for beat_id in result["beat_order"] if beat_id not in result["beats"]]
    if missing:
        raise ValueError(f"creative_ir.beat_order references missing beats: {missing}")
    result["contract_digest"] = contract_digest(contract)
    result["updated_at"] = str(result.get("updated_at") or _now())
    return result


def _tokens(path: str) -> list[str]:
    raw = str(path or "").strip()
    if not raw.startswith("/") or raw == "/":
        raise ValueError("patch path must be a non-root JSON pointer such as /systems/motion")
    return [token.replace("~1", "/").replace("~0", "~") for token in raw[1:].split("/")]


def _parent(document: Any, tokens: list[str]) -> tuple[Any, str]:
    current = document
    for token in tokens[:-1]:
        if isinstance(current, dict):
            if token not in current:
                current[token] = {}
            current = current[token]
        elif isinstance(current, list):
            try:
                current = current[int(token)]
            except (ValueError, IndexError) as exc:
                raise ValueError(f"invalid list index in patch path: {token!r}") from exc
        else:
            raise ValueError("patch path traverses a scalar value")
    return current, tokens[-1]


def _merge(target: dict[str, Any], patch: Mapping[str, Any]) -> None:
    for key, value in patch.items():
        if isinstance(value, Mapping) and isinstance(target.get(key), dict):
            _merge(target[key], value)
        else:
            target[str(key)] = copy.deepcopy(value)


def apply_ir_patch(
    document: Mapping[str, Any],
    *,
    operation: str,
    path: str,
    value: Any = None,
) -> dict[str, Any]:
    """Apply one bounded patch and return a new document."""

    op = str(operation or "").strip().lower()
    if op not in {"set", "merge", "remove", "append"}:
        raise ValueError("operation must be set, merge, remove, or append")
    if op != "remove":
        encoded = json.dumps(value, ensure_ascii=False, allow_nan=False, default=str).encode(
            "utf-8"
        )
        if len(encoded) > MAX_PATCH_VALUE_BYTES:
            raise ValueError(
                f"one Creative IR patch may not exceed {MAX_PATCH_VALUE_BYTES} bytes"
            )
    result = copy.deepcopy(dict(document))
    tokens = _tokens(path)
    parent, leaf = _parent(result, tokens)
    if isinstance(parent, dict):
        if op == "remove":
            if leaf not in parent:
                raise ValueError(f"patch path does not exist: {path}")
            del parent[leaf]
        elif op == "merge":
            if not isinstance(value, Mapping):
                raise ValueError("merge value must be an object")
            existing = parent.get(leaf)
            if existing is None:
                parent[leaf] = copy.deepcopy(dict(value))
            elif isinstance(existing, dict):
                _merge(existing, value)
            else:
                raise ValueError("merge target must be an object")
        elif op == "append":
            existing = parent.get(leaf)
            if not isinstance(existing, list):
                raise ValueError("append target must be an array")
            existing.append(copy.deepcopy(value))
        else:
            parent[leaf] = copy.deepcopy(value)
    elif isinstance(parent, list):
        if op == "append" and leaf == "-":
            parent.append(copy.deepcopy(value))
        else:
            try:
                index = int(leaf)
            except ValueError as exc:
                raise ValueError(f"invalid list index: {leaf!r}") from exc
            if index < 0 or index >= len(parent):
                raise ValueError(f"list index out of range: {index}")
            if op == "remove":
                parent.pop(index)
            elif op == "set":
                parent[index] = copy.deepcopy(value)
            else:
                raise ValueError(f"{op} is not valid for a list index")
    else:
        raise ValueError("patch target parent is a scalar")
    return result


def compact_creative_ir(document: Mapping[str, Any]) -> dict[str, Any]:
    """Return the small model/UI view; detailed beats remain addressable by id."""

    beats = document.get("beats") if isinstance(document.get("beats"), Mapping) else {}
    systems = document.get("systems") if isinstance(document.get("systems"), Mapping) else {}
    return {
        "revision": int(document.get("revision") or 0),
        "intent": copy.deepcopy(document.get("intent") or {}),
        "canvas": copy.deepcopy(document.get("canvas") or {}),
        "beat_order": list(document.get("beat_order") or []),
        "beats": {key: beats[key] for key in list(document.get("beat_order") or []) if key in beats},
        "configured_systems": [key for key, value in systems.items() if value],
        "asset_strategy": copy.deepcopy(document.get("asset_strategy") or {}),
        "program": copy.deepcopy(document.get("program") or {}),
        "active_revision_scope": copy.deepcopy(document.get("active_revision_scope")),
    }


__all__ = [
    "apply_ir_patch",
    "compact_creative_ir",
    "contract_digest",
    "default_creative_ir",
    "normalize_creative_ir",
]
