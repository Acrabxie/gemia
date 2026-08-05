"""Agent-facing API — a framing brief in, a reframe recipe + guide plan out.

This module is the composition *director*: it validates the brief, resolves the
framing + feelings + overrides into the four axes, normalises the subjects,
picks the hero, and hands off to :mod:`lumenframe.compose.framing` for the
tasteful crop. It returns the pure **reframe recipe** (a crop in source 0..1,
scale, the anchor, and guide geometry) plus an explainable ``plan`` the agent
reads instead of coordinates. :mod:`lumenframe.compose.render` turns the recipe
into a self-contained guide-overlay SVG; the recipe itself feeds the transform /
crop layer.

Brief shape (everything optional except ``subjects``)::

    {"subjects": [{"bbox": [x, y, w, h],      # source 0..1, required
                   "weight": 1.0,             # optional saliency
                   "facing": "right"}],       # optional left|right|up|down
     "canvas": {"width": 1080, "height": 1920},  # or "aspect": "9:16"
     "framing": "golden",                     # archetype or alias ("phi")
     "feeling": ["airy", "tense"],
     "intent": "portrait",                    # free label; horizon intents noted
     "horizon": 0.5,                          # optional source-y of a horizon
     "source_aspect": 1.777,                  # optional; defaults to target
     "params": {"tension": 0.8},              # explicit axis overrides (win)
     "seed": 7}

``adjust`` folds "more tension" / "留白多一点" / "tighter" into the brief and
re-derives with the *same* seed — a re-composition, never a nudge of the crop.
"""
from __future__ import annotations

from typing import Any

from lumenframe.craft import new_rng
from lumenframe.craft.determinism import round_floats

from lumenframe.compose import framing as fr
from lumenframe.compose.params import COMPOSE_AXES, compose_feedback
from lumenframe.compose.styles import FRAMINGS

#: Intents that read as landscape/scene — a horizon, if given, matters most.
HORIZON_INTENTS: frozenset[str] = frozenset({
    "landscape", "establishing", "scenery", "horizon", "vista", "seascape",
})

DEFAULTS: dict[str, Any] = {"seed": 7, "aspect": 16.0 / 9.0}


class BriefError(ValueError):
    """Raised for a structurally unusable composition brief."""


def build_frame(brief: dict[str, Any]) -> dict[str, Any]:
    """Brief → ``{"reframe", "plan", "notes"}`` (deterministic per seed)."""
    if not isinstance(brief, dict):
        raise BriefError("brief must be a dict")
    raw = brief.get("subjects")
    if not isinstance(raw, list) or not raw:
        raise BriefError("brief needs a non-empty 'subjects' list")

    try:
        subjects = fr.normalise_subjects(raw)
    except ValueError as exc:
        raise BriefError(str(exc)) from exc

    target_aspect = _aspect(brief)
    source_aspect = _source_aspect(brief, target_aspect)
    # Crop w:h in *source-normalised* units. If the source is square-normalised
    # (the default), a target that is wider than the source needs a wider crop.
    r = target_aspect / source_aspect

    framing_name = FRAMINGS.resolve_name(brief.get("framing") or brief.get("style"))
    axes = FRAMINGS.resolve_params(
        style=framing_name,
        feelings=list(brief.get("feeling") or []),
        overrides=dict(brief.get("params") or {}),
    )
    grid = fr.grid_for(axes.hints.get("grid", "thirds"))

    seed = int(brief.get("seed", DEFAULTS["seed"]))
    rng = new_rng(seed)
    primary = fr.choose_primary(subjects, rng)

    horizon = _horizon(brief)
    result = fr.compute_reframe(subjects, primary, axes, r, grid, horizon, rng)

    recipe = _recipe(result)
    notes = list(result.notes)
    if axes.unknown_feelings:
        notes.append(f"unrecognised feelings ignored: {', '.join(axes.unknown_feelings)}")
    intent = str(brief.get("intent") or "").lower()
    if intent in HORIZON_INTENTS and horizon is None:
        notes.append(f"intent '{intent}' implies a horizon — pass brief['horizon'] "
                     "(source y, 0..1) to place it on a third")

    plan = {
        "framing": framing_name,
        "grid": grid.kind,
        "seed": seed,
        "target_aspect": round(target_aspect, 6),
        "source_aspect": round(source_aspect, 6),
        "axes": axes.to_dict(),
        "primary_index": primary.index,
        "fill": result.fill,
        "anchor_ideal": list(result.anchor_ideal),
        "subject_anchor": list(result.subject_anchor),
        "horizon_line": result.horizon_line,
        "balance_note": result.balance_note,
    }
    return round_floats({"reframe": recipe, "plan": plan, "notes": notes}, 6)


def adjust_frame(brief: dict[str, Any], feedback_phrases: list[str]) -> dict[str, Any]:
    """Fold human feedback into the brief and re-compose with the same seed.

    Returns :func:`build_frame`'s result plus ``brief`` (the adjusted brief to
    persist). Recognised-but-inert feedback (every targeted axis already at its
    limit) is reported honestly rather than silently no-op'ing.
    """
    before = build_frame(brief)
    vocab = compose_feedback()
    new_brief, unknown = vocab.apply(brief, list(feedback_phrases or []), _resolve_axes)
    result = build_frame(new_brief)
    result["brief"] = new_brief
    if unknown:
        known = vocab.vocabulary()
        result["notes"].append(
            f"unrecognised feedback ignored: {', '.join(unknown)} "
            f"(known: {', '.join(known[:12])}, …)")
    recognised = [p for p in (feedback_phrases or []) if p not in unknown]
    if recognised and result["reframe"] == before["reframe"]:
        result["notes"].append(
            "feedback recognised but the frame did not change — the targeted "
            "axes are already at their limit")
    return result


def _resolve_axes(brief: dict[str, Any]):
    """``brief -> ResolvedAxes`` for the feedback loop (compounds from current)."""
    name = FRAMINGS.resolve_name(brief.get("framing") or brief.get("style"))
    return FRAMINGS.resolve_params(
        style=name,
        feelings=list(brief.get("feeling") or []),
        overrides=dict(brief.get("params") or {}),
    )


# ── recipe assembly ─────────────────────────────────────────────────────────


def _recipe(result: fr.Reframe) -> dict[str, Any]:
    """Turn a :class:`~lumenframe.compose.framing.Reframe` into the output dict."""
    cx, cy, fw, fh = result.crop
    grid = result.grid

    markers: list[dict[str, Any]] = []
    for s in result.subjects:
        rx, ry = (s.x - cx) / fw, (s.y - cy) / fh
        rw, rh = s.w / fw, s.h / fh
        # Fully inside the crop, not merely overlapping it: a partially clipped
        # subject must report in_frame=False so consumers (and the overlay,
        # which skips out-of-frame markers) can detect the clip.
        in_frame = (rx >= -1e-6 and ry >= -1e-6
                    and rx + rw <= 1 + 1e-6 and ry + rh <= 1 + 1e-6)
        markers.append({
            "index": s.index,
            "bbox": [round(rx, 6), round(ry, 6), round(rw, 6), round(rh, 6)],
            "primary": s.index == result.primary.index,
            "in_frame": in_frame,
            "facing": s.facing,
        })

    guides: dict[str, Any] = {
        "grid": grid.kind,
        "v_lines": [round(v, 6) for v in grid.v],
        "h_lines": [round(h, 6) for h in grid.h],
        "spiral": grid.spiral,
        "anchor": [result.anchor_ideal[0], result.anchor_ideal[1]],
        "subject_markers": markers,
    }
    if result.horizon_line is not None:
        guides["horizon_line"] = round(result.horizon_line, 6)

    return {
        "crop": [round(cx, 6), round(cy, 6), round(fw, 6), round(fh, 6)],
        "scale": result.scale,
        "subject_anchor": [result.subject_anchor[0], result.subject_anchor[1]],
        "guides": guides,
        "balance_note": result.balance_note,
    }


# ── brief parsing helpers ────────────────────────────────────────────────────


def _aspect(brief: dict[str, Any]) -> float:
    canvas = brief.get("canvas")
    if isinstance(canvas, dict) and canvas.get("width") and canvas.get("height"):
        w, h = float(canvas["width"]), float(canvas["height"])
        if w > 0 and h > 0:
            return w / h
    return _parse_ratio(brief.get("aspect")) or DEFAULTS["aspect"]


def _source_aspect(brief: dict[str, Any], target: float) -> float:
    """Source pixel aspect. Defaults to the target — i.e. we assume an
    already-correctly-shaped source unless told otherwise, so a plain reframe
    works in a clean square-free normalised space."""
    sa = _parse_ratio(brief.get("source_aspect"))
    return sa if sa and sa > 0 else target


def _parse_ratio(value: Any) -> float | None:
    if isinstance(value, (int, float)) and value > 0:
        return float(value)
    if isinstance(value, str):
        s = value.strip()
        if ":" in s:
            a, _, b = s.partition(":")
            try:
                fa, fb = float(a), float(b)
                if fa > 0 and fb > 0:
                    return fa / fb
            except ValueError:
                return None
        try:
            f = float(s)
            return f if f > 0 else None
        except ValueError:
            return None
    return None


def _horizon(brief: dict[str, Any]) -> float | None:
    h = brief.get("horizon")
    if isinstance(h, (int, float)) and 0.0 <= float(h) <= 1.0:
        return float(h)
    return None
