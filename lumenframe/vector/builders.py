"""Subject builders — a brief's subject becomes an initial node structure.

Builders create *still* scenes: geometry, styling, roles, particle fields —
no tracks. Behaviours (chosen by the planner in :mod:`api`) bring them to
life. Keeping build and animate separate is what lets one subject render in
any style and any intent.

Subjects:

* ``logo_text`` — a wordmark: focal text + optional accent underline.
* ``title``     — headline + optional subtitle (kicker register).
* ``mark``      — a vector mark: named preset (``ring`` / ``hex`` / ``star`` /
  ``blob`` / ``wave`` / ``orbit``) or an explicit shape spec.
* ``abstract``  — no focal glyph; an organic composition for backgrounds.

Every builder receives the resolved palette + params and the scene's seeded
rng; decoration (particle fields, accent rings) is added according to
``params.decoration_share``.
"""
from __future__ import annotations

import random
from typing import Any

from lumenframe.templates import theme
from lumenframe.vector import geometry
from lumenframe.vector import scene as vscene
from lumenframe.vector.params import ResolvedParams

MARK_PRESETS: tuple[str, ...] = ("ring", "hex", "star", "blob", "wave", "orbit")

SUBJECT_KINDS: tuple[str, ...] = ("logo_text", "title", "mark", "abstract")


class BuildError(ValueError):
    """Raised for an unusable subject spec."""


def build_subject(
    scene: dict[str, Any],
    subject: dict[str, Any],
    *,
    palette: dict[str, Any],
    level: ResolvedParams,
    rng: random.Random,
) -> None:
    """Populate ``scene.nodes`` for a subject spec. Mutates the scene."""
    kind = str(subject.get("kind") or "")
    if kind not in SUBJECT_KINDS:
        raise BuildError(f"unknown subject kind {kind!r} (use {SUBJECT_KINDS})")
    if kind == "logo_text":
        _build_logo_text(scene, subject, palette, level, rng)
    elif kind == "title":
        _build_title(scene, subject, palette, level, rng)
    elif kind == "mark":
        _build_mark(scene, subject, palette, level, rng)
    else:
        _build_abstract(scene, subject, palette, level, rng)
    _add_decoration(scene, palette, level, rng)


# ── shared bits ──────────────────────────────────────────────────────────


def _text_fill(palette: dict[str, Any], level: ResolvedParams) -> Any:
    if level.hints.get("gradient_fill"):
        return {"gradient": [[0.0, palette["accent"]], [1.0, palette["accent_soft"]]],
                "angle": 20.0}
    return palette["text"]


def _font_family(level: ResolvedParams) -> str:
    if level.hints.get("serif_text"):
        return "Georgia, 'Times New Roman', serif"
    return "system-ui, -apple-system, 'SF Pro Display', 'Segoe UI', sans-serif"


#: Rough per-glyph advance as a fraction of font size for a bold sans wordmark.
#: Good enough to size an underline / lockup without a font metrics engine
#: (glyph-accurate width is roadmap — see docs §11).
_AVG_ADVANCE = 0.56


def _estimate_text_width(text: str, font_size: float, letter_spacing: float = 0.0) -> float:
    """Estimate rendered width of a centred wordmark, in px."""
    n = max(1, len(text))
    return n * font_size * _AVG_ADVANCE + max(0, n - 1) * letter_spacing


def _hue_cycle(palette: dict[str, Any], level: ResolvedParams) -> list[str]:
    """Colours a particle field / accents cycle through. ``multi_hue`` styles
    (playful = colorful) get a spread; everyone else stays on-brand duotone."""
    if level.hints.get("multi_hue"):
        # A bright, friendly spread anchored on the palette accent.
        return [palette["accent"], "#FF7A59", "#FFC24B", "#4C8BF5", "#28C7A8",
                palette["accent_soft"]]
    return [palette["accent"], palette["accent_soft"]]


def _mark_path(preset: str, radius: float, rng: random.Random, level: ResolvedParams) -> list:
    if preset == "ring":
        return geometry.circle((0.0, 0.0), radius)
    if preset == "hex":
        return geometry.polygon((0.0, 0.0), radius, 6, rotation=0.0)
    if preset == "star":
        return geometry.star((0.0, 0.0), radius, points=5)
    if preset == "blob":
        return geometry.blob((0.0, 0.0), radius, wobble=0.25 + 0.4 * level.wobble,
                             lobes=6 + int(level.axes["complexity"] * 4), rng=rng)
    if preset == "wave":
        pts = []
        n = 7
        for i in range(n):
            x = -radius * 1.6 + (2 * radius * 1.6) * i / (n - 1)
            y = (radius * 0.35) * (1 if i % 2 else -1) * (0.6 + 0.4 * rng.random())
            pts.append((x, y))
        return geometry.smooth_through(pts, tension=0.9)
    if preset == "orbit":
        return geometry.ellipse((0.0, 0.0), radius * 1.35, radius * 0.55)
    raise BuildError(f"unknown mark preset {preset!r} (use {MARK_PRESETS})")


# ── builders ─────────────────────────────────────────────────────────────


def _build_logo_text(scene, subject, palette, level, rng) -> None:
    text = str(subject.get("text") or "").strip()
    if not text:
        raise BuildError("logo_text subject requires 'text'")
    height = int(scene["height"])
    size = theme.type_size("display", height)
    # Long wordmarks step down so they respect the side safe area.
    if len(text) > 8:
        size = max(int(size * 8.0 / len(text)), theme.type_size("title", height))

    cap = str(level.hints.get("line_cap") or "round")
    letter_spacing = size * (0.02 + 0.06 * level.axes["elegance"])
    word_w = _estimate_text_width(text, size, letter_spacing)
    word_y = size * (0.32 if subject.get("mark") else 0.0)

    mark_preset = subject.get("mark")
    if mark_preset:
        radius = size * 0.6
        word_top = word_y - size * 0.5
        # Sit the mark clear ABOVE the cap line (a comfortable gap), not
        # kissing it — bottom of the ring lands ~0.22·size above the caps.
        mark_y = word_top - size * 0.22 - radius
        mark = vscene.path_node(
            _mark_path(str(mark_preset), radius, rng, level),
            name="Mark",
            style={
                "fill": None,
                "stroke": _text_fill(palette, level),
                "stroke_width": float(level.hints.get("stroke_weight") or 4.0),
                "line_cap": cap,
                "glow": bool(level.hints.get("glow")),
            },
            transform={"x": 0.0, "y": mark_y},
        )
        mark["meta"]["role"] = "secondary"
        vscene.add_node(scene, mark)

    word = vscene.text_node(
        text,
        name="Wordmark",
        font_size=size,
        font_family=_font_family(level),
        font_weight=300 if level.hints.get("serif_text") else 700,
        letter_spacing=letter_spacing,
        style={"fill": _text_fill(palette, level), "glow": bool(level.hints.get("glow"))},
        transform={"x": 0.0, "y": word_y},
    )
    word["meta"]["role"] = "focal"
    vscene.add_node(scene, word)

    # Accent underline — a stroke path so draw-on has something to draw. Its
    # width tracks the wordmark (slightly inset) rather than a fixed fraction.
    if level.axes["complexity"] >= 0.3:
        span = word_w * 0.82
        y = word_y + size * 0.72
        rule = vscene.path_node(
            geometry.line((-span / 2, y), (span / 2, y)),
            name="Accent rule",
            style={
                "fill": None,
                "stroke": palette["accent"],
                "stroke_width": max(2.0, float(level.hints.get("stroke_weight") or 4.0) * 0.6),
                "line_cap": cap,
            },
        )
        rule["meta"]["role"] = "secondary"
        vscene.add_node(scene, rule)


def _build_title(scene, subject, palette, level, rng) -> None:
    text = str(subject.get("text") or "").strip()
    if not text:
        raise BuildError("title subject requires 'text'")
    height = int(scene["height"])
    size = theme.type_size("title", height)
    title = vscene.text_node(
        text, name="Title", font_size=size, font_family=_font_family(level),
        letter_spacing=size * 0.03 * level.axes["elegance"],
        style={"fill": palette["text"]},
        transform={"y": -size * 0.2},
    )
    title["meta"]["role"] = "focal"
    vscene.add_node(scene, title)
    subtitle = str(subject.get("subtitle") or "").strip()
    if subtitle:
        sub = vscene.text_node(
            subtitle, name="Subtitle",
            font_size=theme.type_size("subhead", height),
            font_family=_font_family(level), font_weight=400,
            style={"fill": palette["subtext"]},
            transform={"y": size * 0.85},
        )
        sub["meta"]["role"] = "secondary"
        vscene.add_node(scene, sub)


def _build_mark(scene, subject, palette, level, rng) -> None:
    height = int(scene["height"])
    radius = height * 0.18
    preset = str(subject.get("preset") or "ring")
    mark = vscene.path_node(
        _mark_path(preset, radius, rng, level),
        name=f"Mark ({preset})",
        style={
            "fill": (_text_fill(palette, level)
                     if subject.get("filled") else None),
            "stroke": _text_fill(palette, level),
            "stroke_width": float(level.hints.get("stroke_weight") or 4.0),
            "line_cap": str(level.hints.get("line_cap") or "round"),
            "glow": bool(level.hints.get("glow")),
        },
    )
    mark["meta"]["role"] = "focal"
    mark["meta"]["morph_to"] = _mark_path(
        str(subject.get("morph_to")), radius, rng, level
    ) if subject.get("morph_to") else None
    if mark["meta"]["morph_to"] is None:
        mark["meta"].pop("morph_to")
    vscene.add_node(scene, mark)


def _build_abstract(scene, subject, palette, level, rng) -> None:
    width, height = int(scene["width"]), int(scene["height"])
    count = 2 + int(level.axes["complexity"] * 3)
    for i in range(count):
        radius = height * (0.12 + 0.18 * rng.random())
        cx = (rng.random() - 0.5) * width * 0.7
        cy = (rng.random() - 0.5) * height * 0.6
        node = vscene.path_node(
            geometry.blob((cx, cy), radius, wobble=0.2 + 0.5 * level.wobble,
                          lobes=6 + int(level.wobble * 5), rng=rng),
            name=f"Form {i + 1}",
            style={
                "fill": {"gradient": [[0.0, palette["accent"]],
                                      [1.0, palette["accent_soft"]]],
                         "angle": rng.random() * 360.0}
                if level.hints.get("gradient_fill") else palette["accent"],
                "opacity": 0.25 + 0.35 * rng.random(),
                "glow": bool(level.hints.get("glow")),
            },
        )
        node["meta"]["role"] = "focal" if i == count - 1 else "secondary"
        vscene.add_node(scene, node)


# ── decoration ───────────────────────────────────────────────────────────


def _add_decoration(scene, palette, level, rng) -> None:
    """Ambient particle field sized by decoration_share, on a ring around
    the composition so it frames rather than crowds the subject."""
    share = level.decoration_share
    if share <= 0.05:
        return
    width, height = int(scene["width"]), int(scene["height"])
    count = max(6, int(level.particle_count * share))
    ring_r = min(width, height) * 0.38
    spread = min(width, height) * 0.14
    shape = str(level.hints.get("particle_shape") or "dot")
    hues = _hue_cycle(palette, level)   # colorful for multi_hue styles
    instances = []
    ring = geometry.circle((0.0, 0.0), ring_r)
    for i, (bx, by) in enumerate(geometry.sample_on_path(ring, count)):
        jx = (rng.random() - 0.5) * spread * 2.0
        jy = (rng.random() - 0.5) * spread * 2.0
        instances.append({
            "x": round(bx + jx, 2), "y": round(by + jy, 2),
            "r": round(1.5 + rng.random() * (2.5 + 2.0 * level.axes["density"]), 2),
            "shape": shape,
            "delay": round(i / max(1, count - 1), 4),
            "fill": hues[i % len(hues)],
        })
    field = vscene.particles_node(
        instances, name="Ambient field",
        style={"fill": palette["accent_soft"], "glow": bool(level.hints.get("glow"))},
    )
    field["meta"]["role"] = "decoration"
    vscene.add_node(scene, field)
