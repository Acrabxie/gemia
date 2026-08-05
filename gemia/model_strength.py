"""Fail-closed model-strength policy for media generation.

The first entry in each backend-specific tuple is the strongest model Lumeri
supports on that API surface.  Runtime/config/call-site candidates are still
ranked for diagnostics, but cannot displace the code-owned strongest entry.
This deliberately avoids silent quality downgrades when an old environment
variable or config file names a faster/weaker model.
"""
from __future__ import annotations

import re
from collections.abc import Iterable


MEDIA_MODEL_PRIORITY: dict[str, dict[str, tuple[str, ...]]] = {
    "image": {
        "vertex": (
            "gemini-3.1-flash-image-preview",
            "gemini-2.5-flash-image",
        ),
        "openrouter": (
            "google/gemini-3.1-flash-image-preview",
            "google/gemini-2.5-flash-image",
        ),
        "imagen": (
            "imagen-4.0-ultra-generate-001",
            "imagen-4.0-generate-001",
            "imagen-4.0-fast-generate-001",
        ),
    },
    "video": {
        "vertex": (
            "veo-3.1-generate-preview",
            "veo-3.1-fast-generate-preview",
            "veo-3.0-generate-001",
            "veo-3.0-fast-generate-001",
        ),
        "openrouter": (
            "google/veo-3.1",
            "google/veo-3",
        ),
        "gemini": (
            "veo-3.1-generate-preview",
            "veo-3.1-fast-generate-preview",
        ),
    },
    "audio": {
        "vertex": (
            "lyria-3-pro-preview",
            "lyria-002",
            "lyria-3-clip-preview",
        ),
        "gemini": (
            "lyria-3-pro-preview",
            "lyria-002",
            "lyria-3-clip-preview",
        ),
    },
}


def _clean(model: object) -> str:
    return str(model or "").strip()


def _canonical(model: str) -> str:
    return re.sub(r"[_\s]+", "-", model.strip().lower())


def rank_media_models(slot: str, backend: str, candidates: Iterable[object]) -> list[str]:
    """Return unique candidates ordered strongest-first.

    Known, code-reviewed models always outrank unknown identifiers. Unknown
    identifiers are deterministically ordered by parsed version and quality
    markers so every supplied model still has a stable position.
    """
    priority = MEDIA_MODEL_PRIORITY.get(slot, {}).get(backend, ())
    known = {_canonical(model): len(priority) - index for index, model in enumerate(priority)}
    unique: list[str] = []
    seen: set[str] = set()
    for raw in candidates:
        model = _clean(raw)
        key = _canonical(model)
        if model and key not in seen:
            seen.add(key)
            unique.append(model)

    def strength(model: str) -> tuple[int, int, tuple[int, ...], str]:
        canonical = _canonical(model)
        if canonical in known:
            return (1, known[canonical], (), canonical)
        versions = tuple(int(part) for part in re.findall(r"\d+", canonical))
        quality = 0
        for marker, value in (("ultra", 40), ("pro", 30), ("quality", 20), ("fast", -20), ("flash", -10), ("clip", -30), ("lite", -40)):
            if marker in canonical:
                quality += value
        return (0, quality, versions, canonical)

    return sorted(unique, key=strength, reverse=True)


def strongest_media_model(slot: str, backend: str, candidates: Iterable[object] = ()) -> str:
    """Return the code-owned strongest model for a media API surface."""
    priority = MEDIA_MODEL_PRIORITY.get(slot, {}).get(backend, ())
    if not priority:
        raise ValueError(f"no media model strength policy for {slot}/{backend}")
    ranked = rank_media_models(slot, backend, (*priority, *tuple(candidates)))
    if not ranked:  # defensive; priority is required above
        raise ValueError(f"no media models available for {slot}/{backend}")
    return ranked[0]


def media_model_failover_chain(slot: str, backend: str, candidates: Iterable[object] = ()) -> list[str]:
    """Return the silent strongest-to-weakest retry chain for one API surface."""
    priority = MEDIA_MODEL_PRIORITY.get(slot, {}).get(backend, ())
    if not priority:
        raise ValueError(f"no media model strength policy for {slot}/{backend}")
    return rank_media_models(slot, backend, (*priority, *tuple(candidates)))


def is_model_unavailable_error(exc: BaseException) -> bool:
    """True only for errors that clearly mean the requested model is unusable."""
    status = getattr(exc, "status", None)
    text = " ".join(
        part for part in (str(exc), str(getattr(exc, "body_tail", ""))) if part
    ).lower()
    explicit_status = status in {400, 404, 422}
    if status is None:
        explicit_status = any(
            token in text
            for token in ("http 400", "http 404", "http 422", "400 invalid_argument", "404 not_found", "422 ")
        )
    model_marker = any(token in text for token in ("model", "publisher", "endpoint"))
    unavailable_marker = any(
        token in text
        for token in (
            "not found",
            "not available",
            "unavailable",
            "unsupported",
            "not supported",
            "does not exist",
            "invalid model",
            "unknown model",
            "not enabled",
        )
    )
    return explicit_status and model_marker and unavailable_marker
