"""Agent-facing API — a text brief in, a choreographed text scene + SVG out.

This is the creative director for kinetic type: the only layer that sees the
whole brief. It resolves style + feelings + overrides into the six axes, builds
the :class:`~lumenframe.kinetic.typography.TypeContext` (the modular scale and
safe box), normalises the copy into the roles the chosen layout expects, lays it
out under the taste floor, assigns the reveal rhythm from ``pace``, compiles the
self-contained SVG, and returns the scene together with an explainable *plan*
(the type scale, the hierarchy, the timing) a human or agent reads instead of
the SVG.

Brief shape (``text`` **or** ``lines`` is required; everything else optional)::

    {"text": "The Future, Today",         # or "lines": ["Name", "Role"]
     "layout": "title_card",              # see kinetic_catalog()["layouts"]
     "style": "title_hero",               # archetype or alias ("apple")
     "feeling": ["bold", "fast"],
     "reveal": "rise_fade",               # overrides the style's default
     "emphasis": ["Future"],              # words lifted to the accent hue
     "duration": 5.0,                     # seconds, capped at 58
     "canvas": {"width": 1920, "height": 1080},
     "palette": "ink",                    # theme name or {role: hex}
     "params": {"weight": 0.9},           # explicit axis overrides (win)
     "seed": 7}

``adjust`` folds feedback ("bolder", "更紧凑") into the brief and re-derives the
whole scene with the *same seed* — adjustment is re-typesetting, never SVG
surgery.
"""
from __future__ import annotations

from typing import Any

from lumenframe.craft import new_rng, stable_digest
from lumenframe.craft.determinism import round_floats
from lumenframe.templates import theme

from lumenframe.kinetic.params import kinetic_feedback
from lumenframe.kinetic.render import scene_to_svg, validate_svg
from lumenframe.kinetic.styles import STYLES
from lumenframe.kinetic.typography import (
    BASELINE_FRAC,
    LAYOUTS,
    assign_reveals,
    build_context,
)

#: html layers render through HyperFrames, whose hard clip cap is 60s.
MAX_DURATION = 58.0

DEFAULTS: dict[str, Any] = {
    "duration": 5.0,
    "canvas": {"width": 1920, "height": 1080},
    "seed": 7,
}


class BriefError(ValueError):
    """Raised for a structurally unusable brief."""


def _load_text(brief: dict[str, Any]) -> tuple[str, list[str]]:
    """Return ``(text, lines)`` from a brief, or raise if there is no copy."""
    lines = brief.get("lines")
    if isinstance(lines, list):
        lines = [str(x) for x in lines if str(x).strip()]
    else:
        lines = []
    text = brief.get("text")
    text = str(text).strip() if text is not None else ""
    if not text and not lines:
        raise BriefError("brief needs 'text' (a string) or 'lines' (a non-empty list)")
    if not text:
        text = lines[0]
    return text, lines


def _split_pair(line: str) -> tuple[str, str]:
    """Split a credits line ``"Label — Value"`` (— / : / tab) into a pair."""
    for sep in (" — ", " – ", " - ", "\t", ": ", "："):
        if sep in line:
            a, b = line.split(sep, 1)
            return a.strip(), b.strip()
    return line.strip(), ""


def _copy_from_role_key(brief: dict[str, Any], layout: str) -> tuple[str, list[str]]:
    """Derive ``(text, lines)`` from a layout's role key when it is the sole copy.

    ``list_reveal``'s ``bullets`` and ``credits_roll``'s ``pairs`` are genuine
    copy sources; a brief that supplies only those (and no ``text``/``lines``) is
    usable, not empty. Falls back to the standard no-copy error otherwise.
    """
    if layout == "list_reveal":
        bullets = [str(b) for b in (brief.get("bullets") or []) if str(b).strip()]
        if bullets:
            return bullets[0], bullets
    if layout == "credits_roll":
        flat: list[str] = []
        for pair in (brief.get("pairs") or []):
            try:
                a, b = pair
            except (TypeError, ValueError):
                continue
            a, b = str(a).strip(), str(b).strip()
            if not a and not b:
                continue
            flat.append(f"{a} — {b}" if b else a)
        if flat:
            return flat[0], flat
    raise BriefError("brief needs 'text' (a string) or 'lines' (a non-empty list), "
                     "or the layout's copy key ('bullets' / 'pairs')")


def _normalise_content(brief: dict[str, Any], layout: str) -> dict[str, Any]:
    """Map the brief's copy onto the roles the chosen layout reads.

    Explicit role keys on the brief (``subtitle``, ``attribution`` …) win;
    otherwise the roles are derived from ``text``/``lines`` in the natural way
    for that layout (second line → subtitle/role/attribution, all lines →
    bullets, ``"a — b"`` lines → credit pairs). A brief whose *only* copy is the
    layout's own role key (``bullets`` for ``list_reveal``, ``pairs`` for
    ``credits_roll``) is valid copy too, not an empty brief.
    """
    try:
        text, lines = _load_text(brief)
    except BriefError:
        text, lines = _copy_from_role_key(brief, layout)
    second = lines[1] if len(lines) > 1 else ""
    data: dict[str, Any] = dict(brief)  # allow explicit role keys to pass through
    data["title"] = brief.get("title") or text
    if layout == "title_card":
        data.setdefault("subtitle", second)
    elif layout == "lower_third":
        data["name"] = brief.get("name") or text
        data["role"] = brief.get("role") or second
    elif layout == "quote":
        data["quote"] = brief.get("quote") or text
        data["attribution"] = brief.get("attribution") or second
    elif layout == "kinetic_lyric":
        data["phrase"] = brief.get("phrase") or text
    elif layout == "list_reveal":
        data["bullets"] = brief.get("bullets") or (lines or [text])
    elif layout == "caption":
        data["caption"] = brief.get("caption") or text
    elif layout == "credits_roll":
        pairs = brief.get("pairs")
        if not pairs:
            pairs = [_split_pair(ln) for ln in (lines or [text])]
        data["pairs"] = [(str(a), str(b)) for a, b in pairs]
    return data


def _emphasis_set(brief: dict[str, Any]) -> list[str]:
    words = brief.get("emphasis") or []
    norm = ["".join(c for c in str(w).lower() if c.isalnum()) for w in words]
    return sorted({w for w in norm if w})


def _scroll_for_credits(runs: list[dict[str, Any]], ctx, duration: float) -> dict[str, Any]:
    """Scroll geometry for a credits roll: start below frame, end above it."""
    if not runs:
        return {"from": ctx.height, "to": 0.0, "dur": duration, "delay": 0.0}
    bottom = max(r["y"] + r["size"] * (1 - BASELINE_FRAC) for r in runs)
    top = min(r["y"] - r["size"] * BASELINE_FRAC for r in runs)
    content_h = bottom - top
    from_y = round(ctx.height - top, 2)                     # first line at frame bottom
    to_y = round(-(top + content_h) - ctx.height * 0.05, 2)  # last line clears the top
    return {"from": from_y, "to": to_y, "dur": round(duration * 0.94, 3), "delay": 0.0}


def build(brief: dict[str, Any]) -> dict[str, Any]:
    """Brief → ``{"scene", "svg", "plan", "notes"}`` (deterministic per seed)."""
    if not isinstance(brief, dict):
        raise BriefError("brief must be an object")

    duration = min(float(brief.get("duration") or DEFAULTS["duration"]), MAX_DURATION)
    if duration <= 0:
        raise BriefError("duration must be > 0")
    canvas = {**DEFAULTS["canvas"], **(brief.get("canvas") or {})}
    width, height = int(canvas["width"]), int(canvas["height"])
    if width < 64 or height < 64:
        raise BriefError("canvas is too small (min 64×64)")
    seed = int(brief.get("seed", DEFAULTS["seed"]))
    rng = new_rng(seed)

    # style → axes → palette (style resolution raises on an unknown style).
    style_name = STYLES.resolve_name(brief.get("style"))
    hints = STYLES.spec(style_name).hints
    level = STYLES.resolve_params(
        style=style_name,
        feelings=list(brief.get("feeling") or []),
        overrides=dict(brief.get("params") or {}),
    )
    palette = theme.resolve_palette(brief.get("palette") or hints.get("palette"))

    # layout + reveal (fall back to the style's own defaults).
    layout = str(brief.get("layout") or hints.get("layout") or "title_card")
    if LAYOUTS.get(layout) is None:
        raise BriefError(f"unknown layout {layout!r} (use {LAYOUTS.names()})")
    reveal = str(brief.get("reveal") or hints.get("reveal") or "rise_fade")

    background = _background(brief.get("background", "auto"), palette)
    ctx = build_context(level, width, height, palette, hints)
    data = _normalise_content(brief, layout)
    runs = LAYOUTS.require(layout)(ctx, data, rng)
    if not runs:
        raise BriefError("no renderable text after layout (empty copy?)")
    for i, run in enumerate(runs):
        run["id"] = f"run_{i:03d}"

    emphasis = _emphasis_set(brief)
    scroll = None
    if layout == "credits_roll":
        scroll = _scroll_for_credits(runs, ctx, duration)
        for run in runs:
            run["reveal"] = None
        rhythm = {"reveal": "scroll", "unit": "group", "stagger": 0.0,
                  "unit_duration": scroll["dur"], "sequence_end": scroll["dur"],
                  "compressed": False}
    else:
        rhythm = assign_reveals(runs, reveal, level.axis("pace"), duration)

    scene = {
        "canvas": {"width": width, "height": height},
        "duration": round(duration, 4),
        "background": background,
        "palette": palette,
        "style": style_name,
        "layout": layout,
        "safe": {"x": ctx.safe_x, "y": ctx.safe_y, "left": ctx.left,
                 "right": ctx.right, "top": ctx.top, "bottom": ctx.bottom},
        "grid": {"align": runs[0]["align"], "ratio": ctx.ratio,
                 "base": ctx.base, "family": ctx.family,
                 "max_chars": ctx.max_chars, "tracking": ctx.tracking},
        "runs": round_floats(runs, 3),
        "emphasis": emphasis,
        "scroll": scroll,
        "seed": seed,
    }
    scene = round_floats(scene, 4)
    svg = validate_svg(scene_to_svg(scene))
    plan = _plan(scene, level, style_name, layout, rhythm)
    scene["digest"] = stable_digest(round_floats(scene, 4))

    notes: list[str] = []
    if level.unknown_feelings:
        notes.append(f"unrecognised feelings ignored: {', '.join(level.unknown_feelings)}")
    if float(brief.get("duration") or duration) > MAX_DURATION:
        notes.append(f"duration capped at {MAX_DURATION}s (html render limit)")
    if rhythm.get("compressed"):
        notes.append("reveal rhythm compressed to fit the clip duration")
    return {"scene": scene, "svg": svg, "plan": plan, "notes": notes}


def adjust(brief: dict[str, Any], feedback: list[str]) -> dict[str, Any]:
    """Fold feedback phrases into the brief and rebuild with the same seed.

    Returns :func:`build`'s result plus ``brief`` (the adjusted brief to persist)
    and a note listing any unrecognised phrases.
    """
    if not isinstance(feedback, list) or not feedback:
        raise BriefError("feedback must be a non-empty list of phrases")
    before = build(brief)
    vocab = kinetic_feedback()
    new_brief, unknown = vocab.apply(
        brief, [str(p) for p in feedback],
        lambda b: STYLES.resolve_params(
            style=STYLES.resolve_name(b.get("style")),
            feelings=list(b.get("feeling") or []),
            overrides=dict(b.get("params") or {}),
        ),
    )
    result = build(new_brief)
    result["brief"] = new_brief
    if unknown:
        result["notes"].append(
            f"unrecognised feedback ignored: {', '.join(unknown)} "
            f"(known: {', '.join(vocab.vocabulary()[:12])}, …)")
    recognised = [p for p in feedback if p not in unknown]
    if recognised and before["scene"]["digest"] == result["scene"]["digest"]:
        result["notes"].append(
            "feedback recognised but nothing changed — the targeted axes are "
            "already at their limit")
    return result


# ── helpers ───────────────────────────────────────────────────────────────────


def _background(spec: Any, palette: dict[str, Any]) -> str | None:
    if spec in (None, "none", "transparent"):
        return None
    if spec == "auto":
        return str(palette["bg"])
    return str(spec)


def _plan(scene: dict[str, Any], level, style: str, layout: str,
          rhythm: dict[str, Any]) -> dict[str, Any]:
    """The explainable plan — the type scale, the hierarchy, the rhythm.

    This is what an agent reads to *reason* about the title; the SVG is just the
    artefact. It surfaces the modular scale, each tier's three contrast channels,
    and the derived timing so the taste floor is auditable.
    """
    tiers: list[dict[str, Any]] = []
    seen: set[str] = set()
    for run in scene["runs"]:
        if run["role"] in seen:
            continue
        seen.add(run["role"])
        tiers.append({
            "role": run["role"], "size": run["size"], "weight": run["weight"],
            "fill_opacity": run["fill_opacity"], "color": run["color"],
        })
    return {
        "style": style, "layout": layout, "reveal": rhythm.get("reveal"),
        "duration": scene["duration"], "seed": scene["seed"],
        "canvas": scene["canvas"], "safe_inset": scene["safe"],
        "type_scale": {"ratio": scene["grid"]["ratio"], "base": scene["grid"]["base"],
                       "max_chars": scene["grid"]["max_chars"]},
        "hierarchy": tiers,
        "rhythm": {"stagger": rhythm.get("stagger"),
                   "unit_duration": rhythm.get("unit_duration"),
                   "unit": rhythm.get("unit"),
                   "sequence_end": rhythm.get("sequence_end")},
        "axes": level.to_dict(),
        "structure": [f'{r["role"]}:{r["id"]}' for r in scene["runs"]],
    }
