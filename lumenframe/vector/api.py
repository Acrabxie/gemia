"""Agent-facing API — a creative brief in, a choreographed scene + plan out.

This module is the **creative director**: the only layer that sees everything.
It resolves style + feelings + overrides into parameters, builds the subject,
ranks focal order, allocates the phase arc, *chooses behaviours* for each
phase (taste lives here, in one auditable table), applies them, validates,
and returns the scene together with an explainable :dfn:`plan` — the visual
plan / motion sequence / timeline the agent (or a human) reads instead of SVG.

Brief shape (everything optional except ``subject``)::

    {"subject": {"kind": "logo_text", "text": "Lumeri", "mark": "ring"},
     "intent": "reveal",              # reveal|intro|loop|transition|outro
     "style": "playful",              # archetype or alias ("google-like")
     "feeling": ["creative", "energetic"],
     "duration": 5.0,                 # seconds, capped at 58 (html layer ≤60)
     "canvas": {"width": 1920, "height": 1080},
     "palette": "lumeri",             # theme palette name or {role: hex}
     "background": "auto",            # "auto" | "none" | "#hex"
     "seed": 7,
     "params": {"energy": 0.8}}       # explicit semantic overrides (win)

Feedback: :func:`adjust_scene` folds "more playful" / "更高级" phrases into
the brief (see :mod:`lumenframe.vector.feedback`) and re-derives the whole
scene with the same seed — adjustment is re-choreography, never SVG surgery.
"""
from __future__ import annotations

import random
from typing import Any

from lumenframe.vector import builders, choreography, feedback as vfeedback
from lumenframe.vector import scene as vscene
from lumenframe.vector.behaviors import apply_behavior
from lumenframe.vector.params import ResolvedParams
from lumenframe.vector.render import scene_svg_document, scene_to_html_layer  # noqa: F401 (re-export)
from lumenframe.vector.styles import resolve_params, resolve_style_name, style_palette

#: html layers render through HyperFrames, whose hard clip cap is 60s.
MAX_DURATION = 58.0

DEFAULTS: dict[str, Any] = {
    "intent": "reveal",
    "duration": 5.0,
    "canvas": {"width": 1920, "height": 1080},
    "seed": 7,
}


class BriefError(ValueError):
    """Raised for a structurally unusable brief."""


def build_scene(brief: dict[str, Any]) -> dict[str, Any]:
    """Brief → ``{"scene", "plan", "notes"}`` (deterministic per seed)."""
    if not isinstance(brief, dict) or not isinstance(brief.get("subject"), dict):
        raise BriefError("brief must be a dict with a 'subject' dict")
    intent = str(brief.get("intent") or DEFAULTS["intent"])
    duration = min(float(brief.get("duration") or DEFAULTS["duration"]), MAX_DURATION)
    if duration <= 0:
        raise BriefError("duration must be > 0")
    canvas = {**DEFAULTS["canvas"], **(brief.get("canvas") or {})}
    seed = int(brief.get("seed", DEFAULTS["seed"]))

    style_name = resolve_style_name(brief.get("style"))
    level = resolve_params(
        style=style_name,
        feelings=list(brief.get("feeling") or []),
        overrides=dict(brief.get("params") or {}),
    )
    palette = style_palette(style_name, brief.get("palette"))

    background: str | None
    bg_spec = brief.get("background", "auto")
    if bg_spec in (None, "none", "transparent"):
        background = None
    elif bg_spec == "auto":
        background = str(palette["bg"])
    else:
        background = str(bg_spec)

    rng = random.Random(seed)
    vscene.reset_ids()
    scene = vscene.new_scene(
        width=int(canvas["width"]), height=int(canvas["height"]),
        duration=duration, background=background, seed=seed,
    )
    builders.build_subject(scene, brief["subject"], palette=palette, level=level, rng=rng)
    choreography.assign_roles(scene["nodes"], focal_id=brief.get("focal_id"))

    overrides, bad_overrides = _resolve_overrides(brief.get("behaviors"))
    windows = choreography.phase_windows(duration=duration, intent=intent, params=level)
    plan_phases = _choreograph(scene, windows, intent, level, rng, overrides)

    vscene.validate_scene(scene)
    plan = {
        "style": style_name,
        "intent": intent,
        "duration": duration,
        "seed": seed,
        "focal": next((n["id"] for n in vscene.walk(scene)
                       if (n.get("meta") or {}).get("role") == "focal"), None),
        "params": level.to_dict(),
        "phases": plan_phases,
        "structure": [
            {"id": n["id"], "kind": n["kind"], "name": n.get("name"),
             "role": (n.get("meta") or {}).get("role")}
            for n in vscene.walk(scene)
        ],
    }
    scene["meta"]["plan"] = plan
    notes: list[str] = []
    if level.unknown_feelings:
        notes.append(f"unrecognised feelings ignored: {', '.join(level.unknown_feelings)}")
    if bad_overrides:
        notes.append(f"unknown behaviour overrides ignored: {', '.join(bad_overrides)}")
    if float(brief.get("duration") or duration) > MAX_DURATION:
        notes.append(f"duration capped at {MAX_DURATION}s (html render limit)")
    return {"scene": scene, "plan": plan, "notes": notes}


def adjust_scene(brief: dict[str, Any], feedback_phrases: list[str]) -> dict[str, Any]:
    """Apply human feedback to a brief and rebuild. Returns build_scene's
    result plus ``brief`` (the adjusted brief to persist) and feedback notes."""
    before = build_scene(brief)
    new_brief, unknown = vfeedback.apply_feedback(brief, feedback_phrases)
    result = build_scene(new_brief)
    result["brief"] = new_brief
    if unknown:
        result["notes"].append(
            f"unrecognised feedback ignored: {', '.join(unknown)} "
            f"(known: {', '.join(vfeedback.feedback_vocabulary()[:12])}, …)"
        )
    # Honesty: if recognised feedback moved nothing (every targeted axis was
    # already at its ceiling/floor), say so instead of silently no-op'ing.
    recognised = [p for p in (feedback_phrases or []) if p not in unknown]
    if recognised and vscene.scene_signature(before["scene"]) == vscene.scene_signature(result["scene"]):
        result["notes"].append(
            "feedback recognised but the scene did not change — the targeted "
            "parameters are already at their limit"
        )
    return result


def scene_to_svg(scene: dict[str, Any]) -> str:
    """Compile a scene to its animated SVG document (re-export sugar)."""
    return scene_svg_document(scene)


# ── the taste table ──────────────────────────────────────────────────────


def _split_roles(scene: dict[str, Any]) -> dict[str, list[dict[str, Any]]]:
    groups: dict[str, list[dict[str, Any]]] = {
        "focal": [], "secondary": [], "decoration": [], "background": [],
    }
    for node in scene.get("nodes") or []:
        role = (node.get("meta") or {}).get("role") or "secondary"
        groups.setdefault(role, []).append(node)
    return groups


#: Phases an agent can override behaviour choice for, via ``brief["behaviors"]``.
#: A value is a verb string ("assemble.gather") or, for ``cycle``, a list.
OVERRIDABLE_PHASES: tuple[str, ...] = (
    "entrance", "entrance_particles", "emphasis", "exit", "cycle",
)


def _resolve_overrides(spec: Any) -> tuple[dict[str, Any], list[str]]:
    """Validate a brief's ``behaviors`` override map → (overrides, unknowns).

    This is what makes ALL 22 behaviour verbs reachable from a brief: an agent
    can pin any phase to any registered verb, e.g.
    ``{"entrance": "assemble.magnetic", "emphasis": "transform.spin_swap",
    "exit": "explode.energy_release", "cycle": ["flow.orbit"]}``. Unknown
    phases or verbs are dropped and reported (never fatal), so a typo restyles
    rather than raising mid-build.
    """
    from lumenframe.vector.behaviors import behavior_names

    if not isinstance(spec, dict):
        return {}, []
    known = set(behavior_names())
    out: dict[str, Any] = {}
    bad: list[str] = []
    for phase, value in spec.items():
        if phase not in OVERRIDABLE_PHASES:
            bad.append(f"{phase}?")
            continue
        if phase == "cycle":
            verbs = [value] if isinstance(value, str) else list(value or [])
            good = [v for v in verbs if v in known]
            bad.extend(v for v in verbs if v not in known)
            if good:
                out["cycle"] = good
        elif isinstance(value, str) and value in known:
            out[phase] = value
        else:
            bad.append(str(value))
    return out, bad


def _has_stroked_path(nodes: list[dict[str, Any]]) -> bool:
    return any(n.get("kind") == "path" and (n.get("style") or {}).get("stroke")
               for n in nodes)


def _entrance_verb(nodes: list[dict[str, Any]], level: ResolvedParams, rng: random.Random) -> str:
    """Choose how the main subject enters — the single most tasteful call.

    Scored, not random: geometry and parameters vote; the seeded rng only
    breaks ties so equal briefs stay varied across seeds but stable per seed.
    Draw-on is a *precise* gesture — its stroke bonus scales with elegance so
    it no longer swamps every style that happens to have a stroked path
    (a playful mark should pop in, not draw itself).
    """
    stroked = _has_stroked_path(nodes)
    ax = level.axes
    scores = {
        "reveal.draw_on": (2.2 * (0.3 + 0.7 * ax["elegance"]) if stroked else 0.0)
        + ax["organicness"] * 0.4,
        "reveal.grow": ax["playfulness"] * 2.2 + ax["energy"] * 0.5,
        "reveal.unfold": ax["playfulness"] * 1.0 + ax["complexity"] * 0.9,
        "reveal.rise": 0.8 + ax["energy"] * 0.7 - ax["elegance"] * 0.2,
        "reveal.fade_in": 0.5 + ax["elegance"] * 1.6 - ax["energy"] * 0.4,
        "assemble.gather": 0.4 + ax["energy"] * 0.9 + ax["complexity"] * 0.5,
        "assemble.magnetic": ax["playfulness"] * 0.7 + ax["energy"] * 1.0,
    }
    top = max(scores.values())
    finalists = sorted(k for k, v in scores.items() if v >= top - 0.12)
    return finalists[0] if len(finalists) == 1 else finalists[rng.randrange(len(finalists))]


def _emphasis_verb(level: ResolvedParams, focal_nodes: list[dict[str, Any]]) -> str | None:
    """Emphasis must RETURN to rest — explode verbs (which end at opacity 0)
    are exit material, never emphasis. Geometry-aware: reshape/spin only make
    sense with the right focal geometry."""
    ax = level.axes
    has_path = any(n.get("kind") == "path" for n in focal_nodes)
    if ax["organicness"] >= 0.65 and has_path:
        return "transform.reshape"
    if ax["playfulness"] >= 0.65 and ax["energy"] >= 0.5:
        return "transform.spin_swap"
    if ax["energy"] >= 0.6:
        return "flow.wave"
    return "flow.breathe"


def _exit_verb(level: ResolvedParams, nodes: list[dict[str, Any]]) -> str:
    """How the subject leaves (transition/outro). Explode verbs end at rest=0."""
    ax = level.axes
    has_particles = any(n.get("kind") == "particles" for n in nodes)
    has_path = any(n.get("kind") == "path" for n in nodes)
    if has_particles and ax["playfulness"] >= 0.5:
        return "explode.scatter"
    if ax["energy"] >= 0.7 and has_path:
        return "explode.energy_release"
    if ax["playfulness"] >= 0.5:
        return "explode.burst"
    return "explode.dissolve"


def _cycle_verbs(
    scene: dict[str, Any], groups: dict[str, list[dict[str, Any]]], level: ResolvedParams
) -> list[tuple[str, str]]:
    """(verb, role-group) pairs for a loop's cycle phase, kind-aware so no
    verb silently no-ops (flow.liquid only reaches path focals; text focals
    breathe/wave; secondary marks and decoration always get their own life)."""
    ax = level.axes
    verbs: list[tuple[str, str]] = []
    focal_paths = [n for n in groups["focal"] if n.get("kind") == "path"]
    focal_text = [n for n in groups["focal"] if n.get("kind") != "path"]
    if ax["organicness"] >= 0.6 and focal_paths:
        verbs.append(("flow.liquid", "focal"))     # reshape the mark itself
    if focal_text or not focal_paths:
        verbs.append(("flow.breathe" if ax["energy"] < 0.55 else "flow.wave", "focal"))
    # Secondary marks (ring, rule) must not sit frozen through the cycle.
    if groups["secondary"]:
        verbs.append(("flow.breathe", "secondary"))
    if groups["decoration"]:
        verbs.append(("flow.orbit" if ax["energy"] >= 0.6 else "flow.drift", "decoration"))
    return verbs


def _choreograph(
    scene: dict[str, Any],
    windows: list[dict[str, Any]],
    intent: str,
    level: ResolvedParams,
    rng: random.Random,
    overrides: dict[str, Any] | None = None,
) -> list[dict[str, Any]]:
    """Apply behaviours phase by phase; return the plan's phase records."""
    overrides = overrides or {}
    groups = _split_roles(scene)
    subject = groups["focal"] + groups["secondary"]
    subject_ordered = choreography.entrance_order(subject)
    plan: list[dict[str, Any]] = []

    def run(phase: str, verb: str, targets: list[dict[str, Any]], window: tuple[float, float]) -> None:
        if not targets or not verb:
            return
        apply_behavior(scene, verb, targets, window, level, rng)
        plan.append({
            "phase": phase, "behavior": verb,
            "targets": [t["id"] for t in targets],
            "t0": round(window[0], 4), "t1": round(window[1], 4),
        })

    # Particle decoration converging IS the anticipation: the dust gathers
    # while the audience waits, and the subject lands on top of it. When
    # energetic/playful the field FORMS (swirl → lock) instead — reaching
    # assemble.form. Decide up front so anticipation doesn't double-animate.
    converging: list[dict[str, Any]] = []
    converge_verb = "assemble.converge"
    if intent in ("reveal", "intro") and level.axes["organicness"] >= 0.5:
        converging = [n for n in groups["decoration"] if n.get("kind") == "particles"]
        if level.axes["energy"] >= 0.6 or level.axes["playfulness"] >= 0.6:
            converge_verb = "assemble.form"

    # Does the anticipation window actually have anything to show? If not,
    # fold it into the entrance so a reveal never opens on dead background.
    anticip = choreography.window_of(windows, "anticipation")
    still_deco = [n for n in groups["decoration"] if n not in converging]
    anticipation_has_content = bool(anticip and (still_deco or converging))

    for w in windows:
        name, t0, t1 = w["name"], float(w["t0"]), float(w["t1"])
        window = (t0, t1)
        if name == "anticipation":
            run(name, "reveal.fade_in", still_deco, window)
        elif name == "entrance":
            # Empty anticipation ⇒ start the subject at t=0 (no dead air).
            start = 0.0 if (anticip and not anticipation_has_content) else t0
            if converging:
                w0 = anticip
                run("anticipation+entrance", overrides.get("entrance_particles") or converge_verb,
                    converging, (w0[0] if w0 else start, t1))
            verb = overrides.get("entrance") or _entrance_verb(subject_ordered, level, rng)
            run(name, verb, subject_ordered, (start, t1))
        elif name == "emphasis":
            morphers = [n for n in subject if (n.get("meta") or {}).get("morph_to")]
            if "emphasis" in overrides:
                run(name, overrides["emphasis"], groups["focal"] or subject, window)
            elif morphers:
                run(name, "transform.morph", morphers, window)   # declared morph wins
            else:
                run(name, _emphasis_verb(level, groups["focal"]), groups["focal"], window)
        elif name == "cycle":
            cyc = overrides.get("cycle")
            if cyc:
                for verb in ([cyc] if isinstance(cyc, str) else cyc):
                    run(name, verb, subject or groups["focal"], window)
            else:
                for verb, role in _cycle_verbs(scene, groups, level):
                    run(name, verb, groups[role] or groups["focal"], window)
        elif name == "exit":
            verb = overrides.get("exit") or _exit_verb(level, subject)
            run(name, verb, subject_ordered + groups["decoration"], window)
        elif name == "hold":
            plan.append({"phase": "hold", "behavior": None, "targets": [],
                         "t0": round(t0, 4), "t1": round(t1, 4)})
    return plan
