"""Agent-facing API — a grading brief in, a grade recipe + plan out.

This is the **colourist**: the one layer that resolves a look + feelings +
overrides into the six axes, runs the taste-floor pipeline, and returns the pure
:dfn:`recipe` together with an explainable :dfn:`plan` (which look, which
operations, the split, the tone curve, the skin-drift budget) and a preview.

Brief shape (everything optional)::

    {"look": "teal_orange",          # look/style archetype or alias
     "feeling": ["moody", "faded"],  # bilingual adjectives (中/英)
     "intensity": 0.8,               # 0..1, scales the WHOLE grade toward neutral
     "params": {"contrast": 0.7},    # explicit axis overrides (win)
     "seed": 7}                       # determinism anchor (grain field)

Feedback: :func:`adjust_grade` folds "more teal" / "更暖" phrases into the brief
(see :mod:`lumenframe.grade.params`) and re-derives the whole grade with the
same seed — adjustment is a *re-derived* grade, never a nudged LUT.
"""
from __future__ import annotations

from typing import Any

from lumenframe.craft import new_rng
from lumenframe.craft.determinism import stable_digest

from lumenframe.grade import grade as _g
from lumenframe.grade.params import FEEDBACK, SPACE
from lumenframe.grade.render import grade_preview_svg, grade_ffmpeg_filter, validate_grade_svg
from lumenframe.grade.styles import STYLES

DEFAULT_SEED = 7
DEFAULT_INTENSITY = 1.0


class BriefError(ValueError):
    """Raised for a structurally unusable grading brief."""


def _look_key(brief: dict[str, Any]) -> str | None:
    """The requested look — accepts ``look`` or ``style`` (alias of each other)."""
    return brief.get("look") if brief.get("look") is not None else brief.get("style")


def _resolve_axes(brief: dict[str, Any]):
    """Resolve look baseline → feelings → overrides into axes+hints.

    Shared by build and feedback so a "more X" phrase compounds from the brief's
    *current* resolved axes. Unknown looks raise (via ``StyleBook``); unknown
    override axes raise; unknown feelings are collected, never fatal.
    """
    return STYLES.resolve_params(
        style=_look_key(brief),
        feelings=list(brief.get("feeling") or []),
        overrides=dict(brief.get("params") or {}),
    )


def _intensity(brief: dict[str, Any]) -> tuple[float, bool]:
    raw = brief.get("intensity", DEFAULT_INTENSITY)
    try:
        val = float(raw)
    except (TypeError, ValueError):
        raise BriefError(f"intensity must be a number in [0, 1], got {raw!r}")
    clamped = min(1.0, max(0.0, val))
    return clamped, (clamped != val)


def build_grade(brief: dict[str, Any]) -> dict[str, Any]:
    """Brief → ``{"recipe", "plan", "notes", "preview_svg", "ffmpeg_filter"}``.

    Deterministic per seed. Validates the brief and raises :class:`BriefError`
    on structurally-unusable input; unknown looks raise :class:`StyleError`.
    """
    if not isinstance(brief, dict):
        raise BriefError("brief must be a dict")
    params = brief.get("params")
    if params is not None and not isinstance(params, dict):
        raise BriefError("brief.params must be an object of axis:value")
    feeling = brief.get("feeling")
    if feeling is not None and not isinstance(feeling, (list, tuple)):
        raise BriefError("brief.feeling must be a list of adjective phrases")

    intensity, intensity_clamped = _intensity(brief)
    try:
        seed = int(brief.get("seed", DEFAULT_SEED))
    except (TypeError, ValueError):
        raise BriefError(f"seed must be an integer, got {brief.get('seed')!r}")

    level = _resolve_axes(brief)
    look = STYLES.resolve_name(_look_key(brief))
    rng = new_rng(seed)
    recipe, report = _g.derive_recipe(
        level.values, level.hints, intensity=intensity, rng=rng,
    )

    preview_svg = grade_preview_svg(recipe)
    validate_grade_svg(preview_svg)  # never hand back an unsafe preview
    ffmpeg = grade_ffmpeg_filter(recipe)

    plan = {
        "look": look,
        "seed": seed,
        "intensity": intensity,
        "axes": dict(level.values),
        "ops": report["ops"],
        "split": report["split"],
        "skin_drift_deg": report["skin_drift_deg"],
        "skin_protected": report["skin_protected"],
        "stylised": report["stylised"],
        "curve": report["curve"],
        "digest": stable_digest({"recipe": recipe, "look": look, "seed": seed}),
    }

    notes: list[str] = []
    if level.unknown_feelings:
        notes.append(f"unrecognised feelings ignored: {', '.join(level.unknown_feelings)}")
    if intensity_clamped:
        notes.append("intensity clamped into [0, 1]")
    if report["skin_protected"]:
        if report["skin_drift_deg"] <= _g.SKIN_TOLERANCE_DEG:
            notes.append(
                f"skin-tone protection reduced the colour cast to keep drift "
                f"≤ {_g.SKIN_TOLERANCE_DEG:.0f}° (this look is not stylised)"
            )
        else:
            # Honest fallback: never assert convergence the payload contradicts.
            notes.append(
                f"skin-tone protection attenuated the colour cast but the "
                f"reference skin tone still drifts {report['skin_drift_deg']:.1f}° "
                f"(> {_g.SKIN_TOLERANCE_DEG:.0f}°)"
            )
    if report["split"]["shadow_hue"] is not None and not report["split"]["complementary"] \
            and not report["stylised"]:
        notes.append("note: split hues are not complementary for a non-stylised look")

    return {
        "recipe": recipe,
        "plan": plan,
        "notes": notes,
        "preview_svg": preview_svg,
        "ffmpeg_filter": ffmpeg,
    }


def adjust_grade(brief: dict[str, Any], feedback_phrases: list[str]) -> dict[str, Any]:
    """Apply human feedback to a brief and rebuild with the SAME seed.

    Returns :func:`build_grade`'s result plus ``brief`` (the adjusted brief to
    persist). Recognised-but-inert feedback (every targeted axis already at a
    limit) is reported honestly rather than silently ignored.
    """
    before = build_grade(brief)
    new_brief, unknown = FEEDBACK.apply(brief, list(feedback_phrases or []), _resolve_axes)
    result = build_grade(new_brief)
    result["brief"] = new_brief
    if unknown:
        result["notes"].append(
            f"unrecognised feedback ignored: {', '.join(unknown)} "
            f"(known: {', '.join(FEEDBACK.vocabulary()[:12])}, …)"
        )
    recognised = [p for p in (feedback_phrases or []) if p not in unknown]
    if recognised and result["plan"]["digest"] == before["plan"]["digest"]:
        result["notes"].append(
            "feedback recognised but the grade did not change — the targeted "
            "dials are already at their limit"
        )
    return result
