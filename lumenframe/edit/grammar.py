"""Cut-craft vocabulary and the taste-floor math — where amateur edits get hard.

This module holds two things:

* the :data:`TRANSITIONS` :class:`~lumenframe.craft.registry.Registry` — the
  eight named ways two clips can join, each carrying a base duration, the
  renderer kind it lowers to, and a one-line "when appropriate"; and
* the pure, unit-testable **taste floor**: the functions that decide shot-length
  floors, how many joins may earn a transition, which joins earn one, how long a
  dissolve should be at a given pace, and the J/L audio splits. These functions
  take resolved axes and plain clip facts and return numbers — no I/O, no
  randomness except a passed-in seeded RNG that only ever breaks exact ties.

The governing principle (the whole reason the library exists): **a straight cut
is the default; transitions are seasoning.** The budget maths make it
structurally hard to salt every join, and the per-join reasons make every
non-default choice explain itself.
"""
from __future__ import annotations

import random
from typing import Any

from lumenframe.craft import Registry, clamp01, lerp

# ── the transition vocabulary ──────────────────────────────────────────────

TRANSITIONS = Registry("edit transitions")


def _verb(name: str, summary: str, *, base_ms: int, renders_as: str | None,
          straight: bool, seasoning: bool, when: str, color: str | None = None):
    """Register one transition with its craft metadata.

    ``base_ms`` is the *nominal* duration before pace-scaling (0 for straight
    joins). ``renders_as`` is the timeline renderer kind it lowers to (see
    :mod:`lumenframe.edit.render`), or ``None`` for a straight join that needs
    no transition op at all. ``seasoning`` marks the showy transitions that the
    per-style budget caps; ``straight`` marks the two joins (plain and match
    cut) that are always free to use.
    """
    return TRANSITIONS.verb(
        name, summary=summary, base_ms=base_ms, renders_as=renders_as,
        straight=straight, seasoning=seasoning, when=when, color=color,
    )(lambda: name)


_verb("cut", "Hard straight cut — no transition; the default join.",
      base_ms=0, renders_as=None, straight=True, seasoning=False,
      when="the default; use unless a join specifically earns more")
_verb("match_cut", "Straight cut on matched motion/shape/action — invisible continuity.",
      base_ms=0, renders_as=None, straight=True, seasoning=False,
      when="continuous action or a graphic match across the join")
_verb("dissolve", "Cross-dissolve — the two shots overlap and blend.",
      base_ms=800, renders_as="dissolve", straight=False, seasoning=True,
      when="a soft time/place shift, or a dreamy/documentary passage")
_verb("fade", "Fade through the layer's own opacity at the edge.",
      base_ms=500, renders_as="fade", straight=False, seasoning=True,
      when="a gentle in/out at a section boundary")
_verb("wipe", "Wipe — a moving edge reveals the next shot.",
      base_ms=500, renders_as="wipe_l", straight=False, seasoning=True,
      when="a deliberate, graphic section change (commercial/energetic)")
_verb("whip_pan", "Whip pan — a fast blurred slide between shots.",
      base_ms=200, renders_as="slide", straight=False, seasoning=True,
      when="a high-energy join, best hiding a real camera move")
_verb("dip_to_black", "Dip to black — fade out then in through black.",
      base_ms=700, renders_as="fade", straight=False, seasoning=True,
      when="a hard act/section break or a dramatic beat", color="#000000")
_verb("dip_to_white", "Dip to white — fade out then in through white.",
      base_ms=700, renders_as="fade", straight=False, seasoning=True,
      when="an upbeat punctuation or a bright product reveal", color="#ffffff")


def transition_meta(name: str) -> dict[str, Any]:
    """The catalogued metadata for one transition (raises on unknown name)."""
    for entry in TRANSITIONS.catalog():
        if entry["name"] == name:
            return entry
    raise KeyError(f"unknown transition {name!r} (use {TRANSITIONS.names()})")


def is_straight(name: str) -> bool:
    return bool(transition_meta(name)["straight"])


def seasoning_names() -> list[str]:
    """The showy transitions the per-style budget caps (straight cuts excluded)."""
    return sorted(e["name"] for e in TRANSITIONS.catalog() if e["seasoning"])


# ── the taste floor: pure maths on resolved axes + clip facts ───────────────

#: Absolute floor on shot length; even the fastest montage will not go below it.
MIN_SHOT_FLOOR_MS = 300
#: Longest a single transition may run — a guard the render validator enforces.
MAX_TRANSITION_MS = 3000


def min_shot_ms(pace: float) -> int:
    """Minimum comfortable shot length at a given pace.

    Fast pace tightens the floor; slow pace lengthens it. Clamped so it can
    never dip under :data:`MIN_SHOT_FLOOR_MS` — the structural guarantee that no
    style can machine-gun cuts into subliminal frames.
    """
    ms = lerp(900.0, MIN_SHOT_FLOOR_MS, clamp01(pace))
    return int(max(MIN_SHOT_FLOOR_MS, round(ms)))


def transition_budget(n_joins: int, cut_frac: float, drama: float) -> int:
    """How many of ``n_joins`` joins may carry a *seasoning* transition.

    ``cut_frac`` (from the style) is a hard ceiling on the fraction; drama pulls
    the actual count within ``[0.6·cap, cap]`` but can never breach it. The
    result rounds down for small sequences, so a three-shot invisible edit gets
    zero transitions — straight cuts, as it should be. This single function is
    what enforces "transitions are seasoning".
    """
    if n_joins <= 0:
        return 0
    scale = 0.6 + 0.4 * clamp01(drama)          # 0.6 … 1.0 of the ceiling
    frac = clamp01(cut_frac) * scale
    return int(min(round(n_joins * frac), round(n_joins * clamp01(cut_frac))))


def dissolve_ms(base_ms: int, pace: float, dissolve_scale: float = 1.0) -> int:
    """Scale a transition's nominal length by pace — slow pace, longer melt.

    A slow, dreamy cut wants a long overlap; a fast one wants the same move over
    in a couple of frames. ``dissolve_scale`` lets a style (dreamy) stretch its
    dissolves further. Result is clamped to :data:`MAX_TRANSITION_MS`.
    """
    if base_ms <= 0:
        return 0
    factor = lerp(1.6, 0.45, clamp01(pace)) * max(0.1, dissolve_scale)
    return int(min(MAX_TRANSITION_MS, max(60, round(base_ms * factor))))


def _tags(clip: dict[str, Any]) -> set[str]:
    return {str(t) for t in (clip.get("tags") or [])}


def join_worth(from_clip: dict[str, Any], to_clip: dict[str, Any]) -> float:
    """How much a join *wants* a transition, on an open-ended positive scale.

    Scene changes and topic changes (disjoint tags) score highest — those are
    the joins where a dissolve or dip reads as intentional punctuation. A join
    inside one continuous scene scores low: it should stay a straight cut. This
    ranking is what steers the scarce transition budget toward the joins that
    earn it, deterministically (ties broken later by the seeded RNG).
    """
    worth = 0.0
    a_scene, b_scene = from_clip.get("scene"), to_clip.get("scene")
    if a_scene is not None and b_scene is not None and a_scene != b_scene:
        worth += 1.0
    ta, tb = _tags(from_clip), _tags(to_clip)
    if ta and tb and not (ta & tb):
        worth += 0.4                              # fully disjoint topics
    if not from_clip.get("has_action") and not to_clip.get("has_action"):
        worth += 0.2                              # two static shots — smooth it
    return worth


def is_jump_risk(from_clip: dict[str, Any], to_clip: dict[str, Any]) -> bool:
    """Two same-scene, similar, static shots joined straight ⇒ a jump cut.

    Same ``scene``, overlapping tags, and neither carrying action means cutting
    straight between them will pop as a jump cut. The grammar must intervene —
    a cutaway note or a covering transition. Action on either side (a real
    movement to cut on) or a scene change removes the risk.
    """
    if from_clip.get("scene") is None or from_clip.get("scene") != to_clip.get("scene"):
        return False
    if from_clip.get("has_action") or to_clip.get("has_action"):
        return False
    ta, tb = _tags(from_clip), _tags(to_clip)
    if ta and tb and not (ta & tb):
        return False                              # different subjects — not a jump
    return True


def audio_split_ms(invisibility: float, style_audio: float, kind: str) -> int:
    """J-cut / L-cut audio lead-or-trail offset in ms.

    Seamless styles (documentary, invisible) let sound run ahead of or behind
    the picture so the ear never notices the picture cut. Magnitude scales with
    both the invisibility axis and the style's ``audio`` lean; ``kind`` only
    selects J vs L for variety. Returns 0 when the style does not split audio.
    """
    strength = clamp01(invisibility) * clamp01(style_audio)
    if strength < 0.12:
        return 0
    return int(round(lerp(120.0, 520.0, strength)))


def action_trim_ms(pace: float) -> int:
    """How far to nudge a trim to land the cut on the action.

    When a clip carries action, the cut should fall *on* the movement, not after
    it settles — so we shave a little off the outgoing tail (and ease into the
    incoming action). Tighter at speed. Returned as a positive magnitude; the
    caller applies the sign.
    """
    return int(round(lerp(60.0, 200.0, clamp01(pace))))


def choose_transition(
    palette: list[str],
    primary: str,
    variety: float,
    drama: float,
    slot_index: int,
    from_clip: dict[str, Any],
    to_clip: dict[str, Any],
    rng: random.Random,
) -> str:
    """Pick the transition verb for a join that has earned one.

    Low variety keeps to the style's ``primary`` move (consistency reads as
    intent). High variety rotates the palette by slot index — fully
    deterministic, not random, so the same brief always lays the same pattern
    (the seeded ``rng`` is spent upstream, only to break ties when *ranking*
    which joins earn a transition). One context rule overrides the rotation: a
    dramatic scene change reaches for a dip when the palette offers one.
    """
    del rng  # transition choice is deterministic; ties are broken during ranking
    if not palette:
        return primary
    scene_change = (from_clip.get("scene") is not None
                    and from_clip.get("scene") != to_clip.get("scene"))
    if scene_change and drama >= 0.6:
        for dip in ("dip_to_black", "dip_to_white"):
            if dip in palette:
                return dip
    if clamp01(variety) < 0.5:
        return primary
    return palette[slot_index % len(palette)]
