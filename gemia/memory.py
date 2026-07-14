from __future__ import annotations

import copy
import json
import os
import re
from datetime import date, datetime
from pathlib import Path
from typing import Any


# Sentinel for "argument not provided" in selection setters, so callers can
# distinguish "leave unchanged" from "reset to default" (None / "" / "default").
_UNSET = object()


SECRET_FIELD_NAMES = {
    "api_key",
    "apikey",
    "authorization",
    "credential",
    "credentials",
    "password",
    "secret",
    "token",
}
SECRET_FIELD_SUFFIXES = (
    "_api_key",
    "_apikey",
    "_authorization",
    "_password",
    "_secret",
    "_token",
)

DEFAULT_MODEL_PROFILE: dict[str, Any] = {
    "schema_version": 1,
    "updated_at": "2026-05-04",
    "description": "Gemia canonical model defaults. API keys and credentials must not be stored here.",
    "models": {
        "planner": {
            "default": "google/gemini-3.1-pro-preview",
            "provider": "openrouter",
            "aliases": ["LumeriPlanner", "Gemini31Pro", "Gemini3.1pro"],
            "variants": {"fast": "google/gemini-3-flash-preview", "reviewer": "openai/gpt-5.4"},
            # Ordered brain/orchestrator catalog. Index 0 is the backend default;
            # `/model` lets any client switch to another entry (see
            # `active_model_selection` / `apply_model_selection`). Reorder here to
            # change the default and the pick order in one place.
            "priority": [
                {"id": "google/gemini-3.1-pro-preview", "label": "Gemini 3.1 Pro", "provider": "openrouter"},
                {"id": "google/gemini-3.5-flash", "label": "Gemini 3.5 Flash", "provider": "openrouter"},
                {"id": "google/gemini-3-flash-preview", "label": "Gemini 3 Flash", "provider": "openrouter"},
                {"id": "anthropic/claude-sonnet-4.6", "label": "Claude Sonnet 4.6", "provider": "openrouter"},
                {"id": "openai/gpt-5.4", "label": "GPT-5.4 (reviewer)", "provider": "openrouter"},
            ],
            # Reasoning/thinking-effort tiers offered by `/model`. Applied to
            # reasoning-capable models via the OpenRouter `reasoning.effort` field.
            "efforts": ["low", "medium", "high", "max"],
            "default_effort": "medium",
            "env": ["OPENROUTER_MODEL", "GEMIA_OPENROUTER_MODEL", "GEMIA_PLANNER_MODEL", "GEMINI_MODEL"],
            "config": ["openrouter_model", "planner_model", "gemini_model"],
            "description": "Primary planning model through OpenRouter. Legacy Gemini model names are mapped where possible.",
        },
        "image": {
            "default": "google/gemini-2.5-flash-image",
            "provider": "openrouter/nano-banana",
            "aliases": ["Nano Banana", "Gemini 2.5 Flash Image"],
            "tiers": {
                "flash": "google/gemini-2.5-flash-image",
                "pro": "google/gemini-3.1-flash-image-preview",
            },
            "env": ["GEMIA_IMAGE_MODEL", "NANO_BANANA_MODEL", "OPENROUTER_IMAGE_MODEL"],
            "env_by_tier": {
                "flash": ["GEMIA_IMAGE_MODEL", "NANO_BANANA_MODEL", "OPENROUTER_IMAGE_MODEL"],
                "pro": ["GEMIA_IMAGE_PRO_MODEL", "NANO_BANANA_PRO_MODEL", "OPENROUTER_IMAGE_PRO_MODEL"],
            },
            "config": ["image_model", "nano_banana_model", "openrouter_image_model"],
            "config_by_tier": {
                "flash": ["image_model", "nano_banana_model", "openrouter_image_model"],
                "pro": ["image_pro_model", "nano_banana_pro_model", "openrouter_image_pro_model"],
            },
            "description": "Default image generation and image editing model through OpenRouter Nano Banana.",
        },
        "video": {
            "default": "veo-3.1-generate-preview",
            "aliases": ["veo3.1quality", "Veo 3.1 quality"],
            "variants": {"fast": "veo-3.1-fast-generate-preview"},
            "env": ["GEMIA_VIDEO_MODEL", "GEMIA_GEMINI_VIDEO_MODEL", "VEO_MODEL"],
            "config": ["video_model", "veo_model", "gemini_video_model"],
            "description": "Default video generation model.",
        },
        "audio": {
            "default": "lyria-3-pro-preview",
            "aliases": ["lyric2pro", "Lyria 3 Pro"],
            "variants": {"clip": "lyria-3-clip-preview"},
            "env": ["GEMIA_AUDIO_MODEL", "LYRIA_MODEL"],
            "config": ["audio_model", "lyria_model"],
            "description": "Default audio generation model reserved for future audio-generation primitives.",
        },
    },
    "safety": {
        "no_secrets": True,
        "secret_storage": "~/.gemia/config.json",
    },
}


def memory_root() -> Path:
    return Path.home() / ".gemia" / "memory"


def roles_path() -> Path:
    return memory_root() / "ROLES.md"


def queue_path() -> Path:
    return memory_root() / "QUEUE.md"


def durable_memory_path() -> Path:
    return memory_root() / "MEMORY.md"


def model_profile_path() -> Path:
    return memory_root() / "MODEL_PROFILE.json"


def daily_dir() -> Path:
    return memory_root() / "daily"


def daily_path(day: str | date | None = None) -> Path:
    if day is None:
        day_str = date.today().isoformat()
    elif isinstance(day, date):
        day_str = day.isoformat()
    else:
        day_str = day
    return daily_dir() / f"{day_str}.md"


def config_path() -> Path:
    return Path.home() / ".gemia" / "config.json"


def default_model_profile() -> dict[str, Any]:
    return copy.deepcopy(DEFAULT_MODEL_PROFILE)


def _read_json(path: Path, default: Any) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return copy.deepcopy(default)


def read_user_config() -> dict[str, Any]:
    data = _read_json(config_path(), {})
    return data if isinstance(data, dict) else {}


def write_user_config(patch: dict[str, Any]) -> dict[str, Any]:
    """Merge ``patch`` into ``~/.gemia/config.json`` and return the new config.

    This is the credentials/config file (NOT the memory store), so no
    secret-scanning guard is applied. Keys whose patch value is ``None`` are
    removed — this is how ``/model`` reverts a selection back to the backend
    default. Writes are atomic-ish (whole-file rewrite, pretty-printed).
    """
    path = config_path()
    config = read_user_config()
    for key, value in patch.items():
        if value is None:
            config.pop(key, None)
        else:
            config[key] = value
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(config, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return config


def _clean_string(value: Any) -> str:
    if not isinstance(value, str):
        return ""
    return value.strip()


def _has_secret_key(key: str) -> bool:
    lowered = key.lower()
    return lowered in SECRET_FIELD_NAMES or any(lowered.endswith(suffix) for suffix in SECRET_FIELD_SUFFIXES)


def assert_memory_safe(payload: Any, *, path: str = "$") -> None:
    """Reject payloads that try to store obvious secret-bearing keys.

    For dict/list payloads this walks every key and rejects secret-like field
    names (api_key, token, ...). For *string* payloads (free text written by the
    ``remember`` / ``log_note`` verbs) it additionally scans the content for
    secret-looking material — ``sk-...`` keys, ``password = ...`` assignments,
    bearer tokens — so a durable note or daily-log line can never smuggle a
    credential into the memory store.
    """
    if isinstance(payload, dict):
        for key, value in payload.items():
            key_str = str(key)
            if _has_secret_key(key_str):
                raise ValueError(f"Refusing to write secret-like field to Gemia memory: {path}.{key_str}")
            assert_memory_safe(value, path=f"{path}.{key_str}")
    elif isinstance(payload, list):
        for index, item in enumerate(payload):
            assert_memory_safe(item, path=f"{path}[{index}]")
    elif isinstance(payload, str):
        if _text_looks_secret(payload):
            raise ValueError(f"Refusing to write secret-like content to Gemia memory: {path}")


# Secret-looking free-text patterns. Conservative on purpose: matches obvious
# credential shapes (provider key prefixes, `password = ...` / `api_key: ...`
# assignments, `Bearer <token>`) without flagging ordinary prose. Used to guard
# the free-text ``remember`` / ``log_note`` payloads, which carry strings rather
# than the structured dicts ``assert_memory_safe`` was originally built for.
_SECRET_TEXT_PATTERNS = (
    re.compile(r"\bsk-[A-Za-z0-9_\-]{16,}"),
    re.compile(r"\b(?:AKIA|ASIA)[A-Z0-9]{12,}"),
    re.compile(r"\bgh[pousr]_[A-Za-z0-9]{20,}"),
    re.compile(r"\bxox[baprs]-[A-Za-z0-9-]{10,}"),
    re.compile(r"\bBearer\s+[A-Za-z0-9._\-]{16,}", re.IGNORECASE),
    re.compile(
        r"(?:password|passwd|secret|api[_\-]?key|access[_\-]?token|auth[_\-]?token|client[_\-]?secret|private[_\-]?key)"
        r"\s*[:=]\s*\S+",
        re.IGNORECASE,
    ),
    re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----"),
)


def _text_looks_secret(text: str) -> bool:
    if not isinstance(text, str) or not text:
        return False
    return any(pattern.search(text) for pattern in _SECRET_TEXT_PATTERNS)


def write_memory_json(path: Path, payload: Any) -> None:
    assert_memory_safe(payload)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def _write_text_if_missing(path: Path, text: str) -> None:
    if path.exists():
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def _write_json_if_missing(path: Path, payload: Any) -> None:
    if path.exists():
        return
    write_memory_json(path, payload)


def bootstrap_memory(day: str | date | None = None) -> dict[str, str]:
    """Create Gemia memory seed files without overwriting existing notes."""
    root = memory_root()
    root.mkdir(parents=True, exist_ok=True)
    daily_dir().mkdir(parents=True, exist_ok=True)

    _write_text_if_missing(
        root / "README.md",
        "# Gemia Memory\n\n"
        "Gemia project memory lives here. Read shared agent memory first, then this layer.\n\n"
        "Do not store secrets, tokens, passwords, API keys, or raw private conversations here.\n",
    )
    _write_text_if_missing(
        roles_path(),
        "# Gemia Memory Roles\n\n"
        "## Model Defaults\n\n"
        "- Primary planner: `google/gemini-3.1-pro-preview` through OpenRouter (`LumeriPlanner`)\n"
        "- Image generation: `google/gemini-2.5-flash-image` through OpenRouter (`Nano Banana`)\n"
        "- Video generation: `veo-3.1-generate-preview` (`veo3.1quality`)\n"
        "- Audio generation: `lyria-3-pro-preview` (`lyric2pro`)\n",
    )
    _write_text_if_missing(
        queue_path(),
        "# Gemia Memory Queue\n\n## Active\n\n_None._\n\n## Pending\n\n_None._\n\n## Blocked\n\n_None._\n\n## Done\n\n_None._\n",
    )
    _write_text_if_missing(
        durable_memory_path(),
        "# Gemia Durable Memory\n\n"
        "- Gemia local memory root: `~/.gemia/memory/`\n"
        "- API keys remain in `~/.gemia/config.json`, not memory.\n",
    )
    _write_json_if_missing(model_profile_path(), default_model_profile())
    _write_text_if_missing(
        daily_path(day),
        f"# {daily_path(day).stem}\n\n"
        "- Gemia memory bootstrap is available. Add short raw progress notes here.\n",
    )
    return {
        "root": str(root),
        "roles": str(roles_path()),
        "queue": str(queue_path()),
        "memory": str(durable_memory_path()),
        "model_profile": str(model_profile_path()),
        "daily": str(daily_path(day)),
    }


# ──────────────────────────────────────────────────────────────────────
# Prompt injection + write helpers for the v3 agent loop
# ──────────────────────────────────────────────────────────────────────

# Hard cap on the memory block injected into the system prompt. A few KB is
# plenty for durable facts + a model-profile digest; anything larger is almost
# certainly accumulated cruft and would bloat every model call.
MEMORY_PROMPT_MAX_CHARS = 4000


def _read_text_safe(path: Path, *, limit: int | None = None) -> str:
    """Read text from ``path``, never raising. Missing/unreadable -> ""."""
    try:
        text = path.read_text(encoding="utf-8")
    except Exception:
        return ""
    if limit is not None and len(text) > limit:
        text = text[:limit]
    return text


def _model_profile_digest() -> str:
    """A one-glance summary of the canonical model defaults, never raising."""
    try:
        profile = _read_json(model_profile_path(), {})
        if not isinstance(profile, dict):
            return ""
        models = profile.get("models")
        if not isinstance(models, dict):
            return ""
        lines: list[str] = []
        for slot in ("planner", "image", "video", "audio"):
            info = models.get(slot)
            if isinstance(info, dict):
                default = _clean_string(info.get("default"))
                if default:
                    lines.append(f"- {slot}: {default}")
        return "\n".join(lines)
    except Exception:
        return ""


def format_memory_for_prompt(*, max_chars: int = MEMORY_PROMPT_MAX_CHARS) -> str:
    """Build a compact, size-capped memory block for the system prompt.

    Emits durable memory (``MEMORY.md``) only. The model-profile digest is
    deliberately NOT injected here: the orchestrator must not be told which
    underlying model it is running on — that both leaks the engine identity to
    end users (the model would recite "planner: <model-id>" when asked) and is
    routinely stale, since the live model is pinned by env/config that this
    static profile does not reflect. Model routing is a host concern; the model
    never needs its own id. Never raises: a missing file yields a short
    placeholder so the ``{{memory}}`` slot always has coherent content. The
    whole block is hard-capped at ``max_chars`` so accumulated memory cannot
    bloat every model call.
    """
    sections: list[str] = []

    durable = _read_text_safe(durable_memory_path()).strip()
    if durable:
        sections.append(durable)

    if not sections:
        return "(no durable memory recorded yet)"

    block = "\n\n".join(sections).strip()
    if len(block) > max_chars:
        block = block[: max_chars - 1].rstrip() + "…"
    return block


def remember_fact(
    content: str,
    *,
    title: str | None = None,
    kind: str | None = None,
    day: str | date | None = None,
) -> dict[str, str]:
    """Persist a durable fact to ``MEMORY.md``, validated against secrets.

    Appends a bullet to the durable memory file. When ``title`` is given the
    write is idempotent-ish: an existing bullet whose visible text starts with
    the same ``**title**`` marker is REPLACED in place rather than duplicated,
    so re-remembering an updated preference does not pile up stale copies.

    Rejects secret-bearing content via :func:`assert_memory_safe` (raises
    ``ValueError``). Creates the memory file/dirs if missing.
    """
    text = _clean_string(content)
    if not text:
        raise ValueError("remember_fact requires non-empty 'content'")
    title_clean = _clean_string(title) if title else ""
    kind_clean = _clean_string(kind) if kind else ""

    # Content + title + kind are all free text the model supplies — guard each.
    assert_memory_safe(text)
    if title_clean:
        assert_memory_safe(title_clean)
    if kind_clean:
        assert_memory_safe(kind_clean)

    path = durable_memory_path()
    path.parent.mkdir(parents=True, exist_ok=True)

    stamp = datetime.now().strftime("%Y-%m-%d")
    label_bits: list[str] = []
    if title_clean:
        label_bits.append(f"**{title_clean}**")
    if kind_clean:
        label_bits.append(f"({kind_clean})")
    prefix = " ".join(label_bits)
    bullet_body = f"{prefix} — {text}" if prefix else text
    bullet = f"- {bullet_body}  _(updated {stamp})_"

    existing = _read_text_safe(path)
    updated = False
    if title_clean and existing:
        marker = f"- **{title_clean}**"
        out_lines: list[str] = []
        for line in existing.splitlines():
            if line.startswith(marker) and not updated:
                out_lines.append(bullet)
                updated = True
            else:
                out_lines.append(line)
        new_text = "\n".join(out_lines)
        if not new_text.endswith("\n"):
            new_text += "\n"
    else:
        new_text = existing
        if new_text and not new_text.endswith("\n"):
            new_text += "\n"
        if not new_text:
            new_text = "# Gemia Durable Memory\n\n"
        new_text += bullet + "\n"

    path.write_text(new_text, encoding="utf-8")
    return {
        "path": str(path),
        "action": "updated" if updated else "appended",
        "title": title_clean,
        "kind": kind_clean,
        "entry": bullet_body,
    }


def append_daily_entry(text: str, day: str | date | None = None) -> dict[str, Any]:
    """Append a timestamped one-line entry to today's daily log.

    Creates the daily dir/file as needed. Never raises: secret-looking content
    is rejected with a no-op (returns ``{"written": False, ...}``) rather than
    blowing up the caller, and any filesystem error is swallowed the same way —
    logging must never break a turn. The entry is collapsed to a single line so
    one call is one log row.
    """
    result: dict[str, Any] = {"written": False, "path": "", "entry": ""}
    try:
        line = _clean_string(text)
        if not line:
            result["reason"] = "empty"
            return result
        # Collapse to a single line so a multi-line ask can't break the log.
        line = " ".join(line.split())
        if _text_looks_secret(line):
            result["reason"] = "secret"
            return result

        path = daily_path(day)
        path.parent.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now().strftime("%H:%M")
        entry = f"- {stamp} {line}"

        header = ""
        if not path.exists():
            header = f"# {path.stem}\n\n"
        with path.open("a", encoding="utf-8") as fh:
            if header:
                fh.write(header)
            fh.write(entry + "\n")

        result.update(written=True, path=str(path), entry=entry)
        return result
    except Exception as exc:  # noqa: BLE001 — logging must never break a turn
        result["reason"] = f"error: {type(exc).__name__}"
        return result


def load_model_profile(*, bootstrap: bool = True) -> dict[str, Any]:
    if bootstrap:
        bootstrap_memory()
    profile = _read_json(model_profile_path(), default_model_profile())
    if not isinstance(profile, dict):
        return default_model_profile()
    models = profile.get("models")
    if not isinstance(models, dict):
        profile["models"] = default_model_profile()["models"]
    _migrate_model_profile(profile)
    return profile


def _migrate_model_profile(profile: dict[str, Any]) -> None:
    """Patch stale local defaults in memory without touching user secrets."""
    defaults = default_model_profile()
    models = profile.setdefault("models", {})
    if not isinstance(models, dict):
        profile["models"] = defaults["models"]
        return
    planner = models.get("planner")
    if isinstance(planner, dict) and _clean_string(planner.get("default")) in {"google/gemini-2.5-pro", "gemini-2.5-pro"}:
        planner["default"] = defaults["models"]["planner"]["default"]
    # Backfill the `/model` priority catalog + effort tiers onto profiles that
    # predate them, so existing installs pick up the switcher without a reset.
    if isinstance(planner, dict):
        planner_defaults = defaults["models"]["planner"]
        for key in ("priority", "efforts", "default_effort"):
            if not planner.get(key):
                planner[key] = copy.deepcopy(planner_defaults[key])
    image = models.get("image")
    if isinstance(image, dict):
        image_default = _clean_string(image.get("default")).lower()
        image_provider = _clean_string(image.get("provider")).lower()
        if image_default in {"gpt-image-2", "gpt_image2", "gpt image2"} or image_provider == "sisyphus":
            models["image"] = copy.deepcopy(defaults["models"]["image"])


def _slot_default(slot: str, profile: dict[str, Any], tier: str | None = None) -> str:
    model_info = (profile.get("models") or {}).get(slot, {})
    if not isinstance(model_info, dict):
        return ""
    if tier:
        tiers = model_info.get("tiers")
        if isinstance(tiers, dict):
            tier_value = _clean_string(tiers.get(tier))
            if tier_value:
                return tier_value
    return _clean_string(model_info.get("default"))


def _slot_names(slot: str, profile: dict[str, Any], key: str, tier: str | None = None) -> list[str]:
    model_info = (profile.get("models") or {}).get(slot, {})
    if not isinstance(model_info, dict):
        return []
    names: list[str] = []
    by_tier = model_info.get(f"{key}_by_tier")
    if tier and isinstance(by_tier, dict):
        tier_names = by_tier.get(tier, [])
        if isinstance(tier_names, list):
            names.extend(str(item) for item in tier_names)
    base_names = model_info.get(key, [])
    if isinstance(base_names, list):
        names.extend(str(item) for item in base_names)
    return names


def resolve_model_with_source(
    slot: str,
    *,
    env_names: tuple[str, ...] | list[str] | None = None,
    config_keys: tuple[str, ...] | list[str] | None = None,
    fallback: str | None = None,
    tier: str | None = None,
) -> dict[str, str]:
    """Resolve a Gemia model by priority: env, config, profile, fallback."""
    profile = load_model_profile()
    env_candidates = list(env_names) if env_names is not None else _slot_names(slot, profile, "env", tier)
    config_candidates = list(config_keys) if config_keys is not None else _slot_names(slot, profile, "config", tier)

    for name in env_candidates:
        value = _clean_string(os.environ.get(name))
        if value:
            return {"model": value, "source": "env", "source_name": name, "slot": slot}

    config = read_user_config()
    for key in config_candidates:
        value = _clean_string(config.get(key))
        if value:
            return {"model": value, "source": "config", "source_name": key, "slot": slot}

    profile_default = _slot_default(slot, profile, tier)
    if profile_default:
        return {"model": profile_default, "source": "profile", "source_name": slot, "slot": slot}

    return {"model": fallback or "", "source": "fallback", "source_name": slot, "slot": slot}


def resolve_model(
    slot: str,
    *,
    env_names: tuple[str, ...] | list[str] | None = None,
    config_keys: tuple[str, ...] | list[str] | None = None,
    fallback: str | None = None,
    tier: str | None = None,
) -> str:
    return resolve_model_with_source(
        slot,
        env_names=env_names,
        config_keys=config_keys,
        fallback=fallback,
        tier=tier,
    )["model"]


# ── /model: priority catalog + active selection ─────────────────────────────
#
# The backend arranges an ordered priority list per slot (index 0 = default);
# `/model` (web + CLI) lets any client switch to another entry and pick a
# thinking-effort tier. The switch is stored in the runtime override channel the
# orchestrator already reads — ``config.json:lumeri_v3_model`` /
# ``lumeri_v3_effort`` (or the matching env vars) — so it persists across
# sessions and restarts, mirroring Claude Code's "default model" behavior.

_MODEL_OVERRIDE_ENV = "LUMERI_V3_MODEL"
_MODEL_OVERRIDE_CONFIG = "lumeri_v3_model"
_EFFORT_OVERRIDE_ENV = "LUMERI_V3_EFFORT"
_EFFORT_OVERRIDE_CONFIG = "lumeri_v3_effort"


def _slot_info(slot: str, profile: dict[str, Any] | None = None) -> dict[str, Any]:
    profile = profile or load_model_profile()
    info = (profile.get("models") or {}).get(slot, {})
    return info if isinstance(info, dict) else {}


def model_catalog(slot: str = "planner") -> list[dict[str, str]]:
    """Ordered priority list for ``slot``: ``[{id,label,provider}, …]``.

    Index 0 is the backend default. Falls back to the slot's ``default`` when no
    explicit ``priority`` list is configured, so callers always get ≥1 entry
    (unless the slot has no default at all).
    """
    profile = load_model_profile()
    info = _slot_info(slot, profile)
    items: list[dict[str, str]] = []
    priority = info.get("priority")
    if isinstance(priority, list):
        for entry in priority:
            if not isinstance(entry, dict):
                continue
            model_id = _clean_string(entry.get("id"))
            if not model_id:
                continue
            items.append({
                "id": model_id,
                "label": _clean_string(entry.get("label")) or model_id,
                "provider": _clean_string(entry.get("provider")),
            })
    if not items:
        default = _slot_default(slot, profile)
        if default:
            items.append({"id": default, "label": default, "provider": _clean_string(info.get("provider"))})
    return items


def effort_options(slot: str = "planner") -> list[str]:
    info = _slot_info(slot)
    efforts = info.get("efforts")
    if isinstance(efforts, list) and efforts:
        return [str(e) for e in efforts]
    return ["low", "medium", "high", "max"]


def default_effort(slot: str = "planner") -> str:
    return _clean_string(_slot_info(slot).get("default_effort")) or "medium"


def active_model_selection(slot: str = "planner") -> dict[str, Any]:
    """Resolve the currently-active model + effort for ``slot``.

    Reflects the same override precedence the orchestrator uses (env → config →
    backend default) so the UI shows exactly what a turn will run with.
    """
    catalog = model_catalog(slot)
    default_model = catalog[0]["id"] if catalog else _slot_default(slot, load_model_profile())

    model_override = (
        _clean_string(os.environ.get(_MODEL_OVERRIDE_ENV))
        or _clean_string(read_user_config().get(_MODEL_OVERRIDE_CONFIG))
    )
    effort_override = (
        _clean_string(os.environ.get(_EFFORT_OVERRIDE_ENV)).lower()
        or _clean_string(read_user_config().get(_EFFORT_OVERRIDE_CONFIG)).lower()
    )

    model = model_override or default_model
    effort = effort_override or default_effort(slot)
    label = next((item["label"] for item in catalog if item["id"] == model), model)
    return {
        "model": model,
        "label": label,
        "effort": effort,
        "is_default_model": not model_override,
        "is_default_effort": not effort_override,
        "default_model": default_model,
        "default_effort": default_effort(slot),
    }


def _resolve_model_choice(choice: str, catalog: list[dict[str, str]]) -> str | None:
    """Map a user-supplied model token to a catalog id, or ``None`` if unknown.

    Accepts an exact id, a 1-based index into the priority list, or a
    case-insensitive substring of the id or label.
    """
    token = choice.strip()
    if not token:
        return None
    for item in catalog:
        if item["id"] == token:
            return item["id"]
    if token.isdigit():
        idx = int(token) - 1
        if 0 <= idx < len(catalog):
            return catalog[idx]["id"]
    low = token.lower()
    for item in catalog:
        if low in item["id"].lower() or low in item["label"].lower():
            return item["id"]
    return None


def set_model_selection(
    *,
    model: Any = _UNSET,
    effort: Any = _UNSET,
    slot: str = "planner",
) -> dict[str, Any]:
    """Persist a ``/model`` selection and return the new active selection.

    ``model`` / ``effort`` semantics: omitted (``_UNSET``) = leave unchanged;
    ``None`` / ``""`` / ``"default"`` = reset to the backend default; any other
    value = set it (model accepts id, index, or fuzzy match).
    """
    catalog = model_catalog(slot)
    patch: dict[str, Any] = {}

    if model is not _UNSET:
        if model in (None, "", "default"):
            patch[_MODEL_OVERRIDE_CONFIG] = None
        else:
            chosen = _resolve_model_choice(str(model), catalog)
            if not chosen:
                raise ValueError(f"unknown model: {model}")
            patch[_MODEL_OVERRIDE_CONFIG] = chosen

    if effort is not _UNSET:
        if effort in (None, "", "default"):
            patch[_EFFORT_OVERRIDE_CONFIG] = None
        else:
            eff = str(effort).strip().lower()
            if eff not in effort_options(slot):
                raise ValueError(f"unknown effort: {effort}")
            patch[_EFFORT_OVERRIDE_CONFIG] = eff

    if patch:
        write_user_config(patch)
    return active_model_selection(slot)


def apply_model_selection(payload: dict[str, Any], slot: str = "planner") -> dict[str, Any]:
    """HTTP-friendly wrapper: honor only the keys present in ``payload``.

    A missing ``model`` / ``effort`` key leaves that dimension unchanged; a
    present key (even with a null/empty value → reset) is applied.
    """
    if not isinstance(payload, dict):
        raise ValueError("request body must be a JSON object")
    return set_model_selection(
        model=payload["model"] if "model" in payload else _UNSET,
        effort=payload["effort"] if "effort" in payload else _UNSET,
        slot=slot,
    )


def model_selection_payload(slot: str = "planner") -> dict[str, Any]:
    """Full ``/model`` GET payload: catalog + effort options + active selection."""
    return {
        "slot": slot,
        "priority": model_catalog(slot),
        "efforts": effort_options(slot),
        "active": active_model_selection(slot),
    }


def public_model_profile() -> dict[str, Any]:
    """Return model memory details safe for UI/API exposure."""
    profile = load_model_profile()
    return {
        "memory_root": str(memory_root()),
        "model_profile_path": str(model_profile_path()),
        "schema_version": profile.get("schema_version"),
        "updated_at": profile.get("updated_at"),
        "models": profile.get("models", {}),
        "resolved_models": {
            "planner": resolve_model_with_source("planner"),
            "image": resolve_model_with_source("image", tier="flash"),
            "video": resolve_model_with_source("video"),
            "audio": resolve_model_with_source("audio"),
        },
        "planner_selection": model_selection_payload("planner"),
        "safety": {
            "no_secrets": True,
            "secret_storage": str(config_path()),
        },
    }
