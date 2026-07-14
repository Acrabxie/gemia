"""Export honesty table — the executable form of docs/timeline-canonical-plan.md §4 (D3).

Every field writable via ``set_clip_effects`` / ``add_transition`` carries exactly
one classification — RENDERED / WARN_AT_WRITE / PREVIEW_ONLY — enforced at two
points: write time (tool-layer / route-layer ``warnings``) and export time
(manifest ``dropped_fields``). Silent dropping is a bug from Phase 1 onward
(plan rule 2); a key present in ``lumerai.patches._EFFECT_KEYS`` or
``_TRANSITION_KINDS`` but absent from these tables is a red drift test
(plan rule 1, tests/test_export_honesty.py).

This module is intentionally **pure data + pure functions, stdlib-only** so the
``lumerai`` patch layer, the ``gemia`` tool layer, and the HTTP route layer can
all import it without circular imports.

No-op exemption (documented deviation from a strictly field-level reading of
§4.2): ``gemia.project_model.normalize_project`` stamps ``_default_effects()``
(``rotation: 0, mirrored: False, muted: False, audioDetached: False, speed: 1``)
onto every clip that has no effects dict, so at export-read time most clips
"store" rotation/speed/mirrored. Dropping an identity value (rotation 0,
speed 1, mirrored False, blend_mode "normal", …) renders *exactly* what the
value describes — reporting it would flood every manifest with false drops and
train users to ignore the honesty surface. Identity values are therefore
excluded from both warnings and ``dropped_fields``; any non-identity value is
reported.
"""
from __future__ import annotations

from typing import Any

# ── classes ──────────────────────────────────────────────────────────────────

RENDERED = "RENDERED"
WARN_AT_WRITE = "WARN_AT_WRITE"
# PREVIEW_ONLY is an intentionally EMPTY class today: project_render.py renders
# no effects, so classifying anything preview-only would be dishonest (§4.1).
# The class exists so a future web-canvas preview can claim fields without
# touching this rule.
PREVIEW_ONLY = "PREVIEW_ONLY"


# ── the normative tables (plan §4.1, verified against HEAD 37a289b) ──────────
#
# EFFECT_FIELD_TABLE: field -> {media_kind or "*": class}. The media_kind key
# is the clip's ``media_kind`` ("video" clips live on video tracks per the
# patch layer's track-kind check, so media_kind doubles as the track proxy).

EFFECT_FIELD_TABLE: dict[str, dict[str, str]] = {
    # Audio attributes — read by export pass 3 (project_export.py).
    "muted":    {"*": RENDERED},
    "gain_db":  {"*": RENDERED},
    "fade_in":  {"*": RENDERED},
    "fade_out": {"*": RENDERED},
    # Placement — rendered only on the pass-2 overlay path (image/lottie; x/y
    # also for text). Video-track PIP is a Phase 3 candidate.
    "x":       {"image": RENDERED, "lottie": RENDERED, "text": RENDERED, "*": WARN_AT_WRITE},
    "y":       {"image": RENDERED, "lottie": RENDERED, "text": RENDERED, "*": WARN_AT_WRITE},
    "scale":   {"image": RENDERED, "lottie": RENDERED, "*": WARN_AT_WRITE},
    "opacity": {"image": RENDERED, "lottie": RENDERED, "*": WARN_AT_WRITE},
    # Never read by export today. rotation/mirrored/blur_radius flip to
    # RENDERED in Phase 3 (per-segment -vf); each flip lands in the same
    # commit as its renderer support (plan rule 1).
    "rotation":    {"*": WARN_AT_WRITE},
    "mirrored":    {"*": WARN_AT_WRITE},
    "blur_radius": {"*": WARN_AT_WRITE},
    # WARN_AT_WRITE indefinitely: implementing speed changes the
    # duration == source_out - source_in contract itself; needs its own spec.
    "speed":       {"*": WARN_AT_WRITE},
    # Validated against a renderer (compositing_graph) the export never calls.
    "blend_mode":  {"*": WARN_AT_WRITE},
}

TRANSITION_KIND_TABLE: dict[str, str] = {
    "cut":      RENDERED,       # trivially (clears the field)
    "fade":     RENDERED,       # Phase 1, plan §5.2 (per-segment fade filters)
    "dissolve": RENDERED,       # Phase 1, plan §5.2 (B-pre-handle window)
    "wipe":     WARN_AT_WRITE,  # Phase 3 (same window mechanism, xfade=wipeleft…)
}

# Export implements transition windows on the base-video pass only (§5.2);
# a stored transition on any other media kind goes to dropped_fields.
TRANSITION_RENDERED_MEDIA_KINDS: tuple[str, ...] = ("video",)

# Identity values: dropping these renders exactly what the value describes
# (see the no-op exemption in the module docstring).
_NOOP_VALUES: dict[str, tuple[Any, ...]] = {
    "rotation": (0, 0.0),
    "mirrored": (False,),
    "speed": (1, 1.0),
    "blur_radius": (0, 0.0),
    "opacity": (1, 1.0),
    "scale": (1, 1.0),
    "x": (0, 0.0),
    "y": (0, 0.0),
    "blend_mode": ("normal",),
    "muted": (False,),
}

_FIELD_PLANS: dict[str, str] = {
    "rotation": "planned: per-segment transpose/flip, Phase 3",
    "mirrored": "planned: per-segment hflip, Phase 3",
    "blur_radius": "planned: per-segment gblur, Phase 3",
    "speed": "reserved — needs its own duration-contract spec",
    "blend_mode": "planned: overlay-clip blend in pass 2, Phase 3",
    "x": "planned: PIP overlay, Phase 3",
    "y": "planned: PIP overlay, Phase 3",
    "scale": "planned: PIP overlay, Phase 3",
    "opacity": "planned: PIP overlay, Phase 3",
}


# ── lookups ──────────────────────────────────────────────────────────────────


def effect_class(field: str, media_kind: str = "video") -> str:
    """Classification of one effects field for a clip of ``media_kind``.

    Unknown fields default to WARN_AT_WRITE: a key that export does not
    explicitly render must never be silently dropped (plan rules 1-2).
    """
    row = EFFECT_FIELD_TABLE.get(str(field))
    if row is None:
        return WARN_AT_WRITE
    return row.get(str(media_kind) or "video", row.get("*", WARN_AT_WRITE))


def transition_class(kind: str) -> str:
    """Classification of one transition kind (unknown -> WARN_AT_WRITE)."""
    return TRANSITION_KIND_TABLE.get(str(kind), WARN_AT_WRITE)


def _is_noop(field: str, value: Any) -> bool:
    """True when dropping ``value`` for ``field`` cannot change the render."""
    if value is None:  # explicit null = delete the key
        return True
    identities = _NOOP_VALUES.get(str(field))
    if identities is not None:
        return any(value == ident for ident in identities)
    # Unknown fields (e.g. legacy ``audioDetached``): falsy is inert.
    return not value


def _effect_warning(field: str, media_kind: str) -> str:
    plan = _FIELD_PLANS.get(str(field), "not scheduled yet")
    return (
        f"W_NOT_EXPORTED:{field}:{media_kind}-clip {field} is stored and will "
        f"survive undo/OTIO round-trips, but final export does not render it "
        f"yet ({plan}; docs/timeline-canonical-plan.md §4)"
    )


# ── the two write-time functions (plan §4.2 item 1) ──────────────────────────


def effects_warnings(media_kind: str, effects: dict[str, Any]) -> list[str]:
    """Warnings for one ``set_clip_effects`` write (warn, never reject).

    ``effects`` is the effects object being written (explicit ``None`` values
    delete a key and never warn). Warning strings are stable and typed:
    ``W_NOT_EXPORTED:<field>:<human explanation>``.
    """
    if not isinstance(effects, dict):
        return []
    kind = str(media_kind) or "video"
    out: list[str] = []
    for field in sorted(effects):
        value = effects[field]
        if _is_noop(field, value):
            continue
        if effect_class(field, kind) != RENDERED:
            out.append(_effect_warning(str(field), kind))
    return out


def transition_warnings(kind: str) -> list[str]:
    """Warnings for one ``add_transition`` write (warn, never reject)."""
    k = str(kind)
    if transition_class(k) == RENDERED:
        return []
    return [
        f"W_NOT_EXPORTED:transition_after:transition '{k}' is stored and shown "
        f"on the timeline, but final export still renders a hard cut here "
        f"(fade/dissolve do render; '{k}' is planned — "
        f"docs/timeline-canonical-plan.md §5)"
    ]


# ── export-time helper (plan §4.2 item 3) ────────────────────────────────────


def clip_dropped_fields(clip: dict[str, Any]) -> list[dict[str, str]]:
    """Fields stored on ``clip`` that export will drop today.

    Returns ``[{"field": ..., "reason": ...}, ...]`` for the manifest's
    ``dropped_fields`` (the caller adds ``clip_id``). Covers the statically
    knowable drops:

    - non-rendered effects fields with non-identity values
      (``reason: "not_rendered"``),
    - a stored transition whose kind has no renderer, e.g. wipe
      (``reason: "kind_not_supported"``),
    - a renderable transition kind stored on a media kind the base-video
      window mechanism does not cover (``reason: "not_rendered"``).

    Render-time transition degradations (``no_handle`` / ``not_adjacent``) are
    decided inside project_export's pass-1 planner, not here.
    """
    if not isinstance(clip, dict):
        return []
    media_kind = str(clip.get("media_kind") or "video")
    out: list[dict[str, str]] = []
    effects = clip.get("effects") if isinstance(clip.get("effects"), dict) else {}
    for field in sorted(effects):
        if _is_noop(field, effects[field]):
            continue
        if effect_class(field, media_kind) != RENDERED:
            out.append({"field": str(field), "reason": "not_rendered"})
    transition = clip.get("transition_after")
    if isinstance(transition, dict):
        kind = str(transition.get("kind") or "")
        if kind and kind != "cut":
            if transition_class(kind) != RENDERED:
                out.append({"field": "transition_after", "reason": "kind_not_supported"})
            elif media_kind not in TRANSITION_RENDERED_MEDIA_KINDS:
                out.append({"field": "transition_after", "reason": "not_rendered"})
    return out
