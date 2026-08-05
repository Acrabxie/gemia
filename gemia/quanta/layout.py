"""Deterministic placed-block layout for Lumeri Quanta v1.

The semantic quanta IR deliberately does not contain pixel geometry.  This
module is the single projection from that IR to a flat, JSON-serializable
display list consumed by raster, pager, and (later) PPTX exporters.

Two rules are load-bearing:

* geometry is solved against the complete semantic leaf set before a build is
  selected, so advancing a build never moves content that is already visible;
* a block containing any CJK code point uses the CJK face for the *whole*
  block in v1.  Mixed-run shaping can be added later without making raster and
  PPTX choose different line breaks today.

The token values below are the machine-readable subset of
``lumeri-design-manuals/QUANTA.md``.  Geometry and type scale are intentionally
not theme-overridable in v1; a theme may change the documented color and font
roles only.  This keeps the 12-column identity a hard invariant.
"""
from __future__ import annotations

import json
import math
import re
from copy import deepcopy
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence

from gemia.text import TextLayoutError, autofit_text


LAYOUT_VERSION = "quanta-layout-v1"
TOKEN_VERSION = "quanta-v1"


class QuantaLayoutError(ValueError):
    """Raised when a slide cannot satisfy the deterministic v1 contract."""


# Flat keys match the syntax already stored in quanta.theme.tokens.  Keep this
# as ordinary JSON-shaped data: callers often snapshot it into manifests.
DEFAULT_QUANTA_TOKENS: dict[str, Any] = {
    "canvas.width": 1920,
    "canvas.height": 1080,
    "spacing.page-margin-x": 160,
    "spacing.page-margin-y": 120,
    "spacing.slot-gap": 48,
    "spacing.block-gap": 24,
    "spacing.bullet-gap": 16,
    "spacing.card-pad": 32,
    "spacing.group-gap": 32,
    "grid.columns": 12,
    "grid.gutter": 32,
    "grid.column-width": 104,
    "grid.content-width": 1600,
    "grid.content-height": 840,
    "color.surface": "#101419",
    "color.surface-raised": "#1A1F27",
    "color.surface-overlay": "#232A35",
    "color.text-primary": "#F2F5F7",
    "color.text-secondary": "#A8B2BC",
    "color.text-muted": "#8B949E",
    "color.border": "rgba(255,255,255,0.10)",
    "color.accent": "#5FC6DE",
    "color.accent-bright": "#8BD8EA",
    "color.accent-soft": "#ABE5F1",
    "color.accent-deep": "#239FC0",
    "color.on-accent": "#0A0C10",
    "color.scrim": (
        "linear-gradient(180deg, rgba(10,12,16,0) 0%, "
        "rgba(10,12,16,0.72) 100%)"
    ),
    "font.latin.display": {
        "family": "Avenir Next",
        "path": "/System/Library/Fonts/Avenir Next.ttc",
        "weight": 600,
    },
    "font.latin.body": {
        "family": "Helvetica Neue",
        "path": "/System/Library/Fonts/HelveticaNeue.ttc",
        "weight": 400,
    },
    "font.latin.strong": {
        "family": "Helvetica Neue",
        "path": "/System/Library/Fonts/HelveticaNeue.ttc",
        "weight": 500,
    },
    # Pillow cannot load the private PingFang UI collection reliably on this
    # host.  Hiragino Sans GB W6 is an actual, resolvable 600 face and is used
    # for both display and body CJK raster layout in v1.
    "font.cjk.display": {
        "family": "Hiragino Sans GB",
        "path": "/System/Library/Fonts/Hiragino Sans GB.ttc",
        "weight": 600,
    },
    "font.cjk.body": {
        "family": "Hiragino Sans GB",
        "path": "/System/Library/Fonts/Hiragino Sans GB.ttc",
        "weight": 600,
    },
    "type.display.size": 96,
    "type.display.fallback": 64,
    "type.display.lh-latin": 1.05,
    "type.display.lh-cjk": 1.15,
    "type.h1.size": 64,
    "type.h1.fallback": 44,
    "type.h1.lh-latin": 1.10,
    "type.h1.lh-cjk": 1.25,
    "type.h2.size": 44,
    "type.h2.fallback": 32,
    "type.h2.lh-latin": 1.15,
    "type.h2.lh-cjk": 1.30,
    "type.body.size": 32,
    "type.body.fallback": 24,
    "type.body.lh-latin": 1.40,
    "type.body.lh-cjk": 1.60,
    "type.caption.size": 24,
    "type.caption.fallback": 20,
    "type.caption.lh-latin": 1.30,
    "type.caption.lh-cjk": 1.50,
}

_COLOR_OVERRIDE_KEYS = frozenset(
    key for key in DEFAULT_QUANTA_TOKENS if key.startswith("color.")
)
_FONT_OVERRIDE_KEYS = frozenset(
    key for key in DEFAULT_QUANTA_TOKENS if key.startswith("font.")
)
_THEME_OVERRIDE_KEYS = _COLOR_OVERRIDE_KEYS | _FONT_OVERRIDE_KEYS
_NUMERIC_TOKEN_KEYS = frozenset(
    key
    for key, value in DEFAULT_QUANTA_TOKENS.items()
    if isinstance(value, (int, float)) and not isinstance(value, bool)
)
_LAYOUTS = frozenset({"title", "content", "stat", "full-bleed"})
_TEXT_ROLES_TITLE = frozenset({"title", "heading", "headline"})
_CAPTION_ROLES = frozenset({"caption", "subtitle", "eyebrow", "kicker"})
_COLOR_RE = re.compile(
    r"^(?:#[0-9a-fA-F]{3,8}|rgba?\([^\n\r]+\)|linear-gradient\([^\n\r]+\))$"
)


@dataclass(frozen=True)
class _Leaf:
    block: Mapping[str, Any]
    block_ref: str
    source_order: int

    @property
    def kind(self) -> str:
        return str(self.block.get("kind") or "").strip().lower()

    @property
    def role(self) -> str:
        return str(self.block.get("role") or "").strip().lower()


def _finite(value: Any, name: str, *, minimum: float | None = None) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise QuantaLayoutError(f"{name} must be a finite number")
    number = float(value)
    if not math.isfinite(number):
        raise QuantaLayoutError(f"{name} must be a finite number")
    if minimum is not None and number < minimum:
        raise QuantaLayoutError(f"{name} must be >= {minimum:g}")
    return number


def _validate_font_token(value: Any, name: str) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        raise QuantaLayoutError(f"{name} must be a family/path/weight mapping")
    unknown = set(value) - {"family", "path", "weight"}
    if unknown:
        raise QuantaLayoutError(f"{name} has unsupported fields: {sorted(unknown)!r}")
    family = str(value.get("family") or "").strip()
    path = str(value.get("path") or "").strip()
    weight = value.get("weight")
    if not family or not path:
        raise QuantaLayoutError(f"{name} must provide non-empty family and path")
    if not isinstance(weight, int) or isinstance(weight, bool) or not 1 <= weight <= 1000:
        raise QuantaLayoutError(f"{name}.weight must be an integer in [1, 1000]")
    if not Path(path).expanduser().is_file():
        raise QuantaLayoutError(f"{name}.path does not exist: {path!r}")
    return {"family": family, "path": path, "weight": weight}


def _resolve_tokens(theme_tokens: Mapping[str, Any] | None) -> dict[str, Any]:
    if theme_tokens is not None and not isinstance(theme_tokens, Mapping):
        raise QuantaLayoutError("theme_tokens must be a mapping when provided")
    overrides = dict(theme_tokens or {})
    unknown = sorted(set(overrides) - _THEME_OVERRIDE_KEYS)
    if unknown:
        raise QuantaLayoutError(
            "unsupported theme token override(s): " + ", ".join(unknown)
        )
    tokens = deepcopy(DEFAULT_QUANTA_TOKENS)
    for key, value in overrides.items():
        if key in _COLOR_OVERRIDE_KEYS:
            color = str(value or "").strip()
            if not _COLOR_RE.fullmatch(color):
                raise QuantaLayoutError(f"{key} must be a CSS hex/rgb(a)/gradient color")
            tokens[key] = color
        else:
            tokens[key] = _validate_font_token(value, key)

    for key in _NUMERIC_TOKEN_KEYS:
        _finite(tokens[key], key, minimum=0)
    for key in _COLOR_OVERRIDE_KEYS:
        color = str(tokens[key] or "").strip()
        if not _COLOR_RE.fullmatch(color):
            raise QuantaLayoutError(f"invalid built-in color token {key}")
    for key in _FONT_OVERRIDE_KEYS:
        tokens[key] = _validate_font_token(tokens[key], key)

    columns = int(tokens["grid.columns"])
    column_width = int(tokens["grid.column-width"])
    gutter = int(tokens["grid.gutter"])
    margin_x = int(tokens["spacing.page-margin-x"])
    canvas_width = int(tokens["canvas.width"])
    if columns * column_width + (columns - 1) * gutter + 2 * margin_x != canvas_width:
        raise QuantaLayoutError("quanta-v1 12-column grid identity is invalid")
    if canvas_width - 2 * margin_x != int(tokens["grid.content-width"]):
        raise QuantaLayoutError("quanta-v1 content-width identity is invalid")
    if (
        int(tokens["canvas.height"])
        - 2 * int(tokens["spacing.page-margin-y"])
        != int(tokens["grid.content-height"])
    ):
        raise QuantaLayoutError("quanta-v1 content-height identity is invalid")
    return tokens


def _canvas(value: Sequence[int]) -> tuple[int, int, float]:
    if isinstance(value, (str, bytes)):
        raise QuantaLayoutError("canvas must be a (width, height) pair")
    try:
        parts = tuple(value)
    except TypeError as exc:
        raise QuantaLayoutError("canvas must be a (width, height) pair") from exc
    if len(parts) != 2:
        raise QuantaLayoutError("canvas must be a (width, height) pair")
    width, height = parts
    if (
        not isinstance(width, int)
        or isinstance(width, bool)
        or not isinstance(height, int)
        or isinstance(height, bool)
        or width <= 0
        or height <= 0
    ):
        raise QuantaLayoutError("canvas width and height must be positive integers")
    if width * 9 != height * 16:
        raise QuantaLayoutError("quanta-v1 canvas must preserve the 16:9 aspect")
    return width, height, width / 1920.0


def _scaled(value: int | float, scale: float) -> int:
    return int(round(float(value) * scale))


def _scale_rect(rect: Sequence[int], scale: float) -> list[int]:
    x, y, width, height = rect
    left = _scaled(x, scale)
    top = _scaled(y, scale)
    right = _scaled(x + width, scale)
    bottom = _scaled(y + height, scale)
    return [left, top, right - left, bottom - top]


def _inset_rect(rect: Sequence[int], inset: int) -> list[int]:
    x, y, width, height = rect
    if width <= 2 * inset or height <= 2 * inset:
        raise QuantaLayoutError("card is too small for spacing.card-pad")
    return [x + inset, y + inset, width - 2 * inset, height - 2 * inset]


def _column_span(start: int, end: int, tokens: Mapping[str, Any]) -> list[int]:
    columns = int(tokens["grid.columns"])
    if not 1 <= start <= end <= columns:
        raise QuantaLayoutError(f"invalid quanta grid span {start}..{end}")
    margin = int(tokens["spacing.page-margin-x"])
    column = int(tokens["grid.column-width"])
    gutter = int(tokens["grid.gutter"])
    x = margin + (start - 1) * (column + gutter)
    width = (end - start + 1) * column + (end - start) * gutter
    return [x, 0, width, 0]


def _walk_blocks(blocks: Any) -> tuple[list[_Leaf], dict[int, _Leaf]]:
    leaves: list[_Leaf] = []
    by_identity: dict[int, _Leaf] = {}
    seen_ids: set[str] = set()

    def visit(raw: Any, path: tuple[int, ...]) -> None:
        if not isinstance(raw, Mapping):
            raise QuantaLayoutError(f"quanta block at {path!r} must be a mapping")
        kind = str(raw.get("kind") or "").strip().lower()
        if kind not in {"text", "stat", "image", "shape", "group"}:
            raise QuantaLayoutError(f"unsupported quanta block kind {kind!r} at {path!r}")
        if kind == "group":
            children = raw.get("children")
            if not isinstance(children, list):
                raise QuantaLayoutError(f"group at {path!r} must provide children")
            for index, child in enumerate(children, 1):
                visit(child, (*path, index))
            return
        block_ref = str(raw.get("id") or "").strip()
        if not block_ref:
            raise QuantaLayoutError(f"semantic leaf at {path!r} is missing a stable id")
        if block_ref in seen_ids:
            raise QuantaLayoutError(f"duplicate semantic leaf id {block_ref!r}")
        seen_ids.add(block_ref)
        leaf = _Leaf(raw, block_ref, len(leaves))
        leaves.append(leaf)
        by_identity[id(raw)] = leaf

    raw_blocks = blocks if isinstance(blocks, list) else []
    for index, block in enumerate(raw_blocks, 1):
        visit(block, (index,))
    return leaves, by_identity


def _is_cjk_codepoint(char: str) -> bool:
    code = ord(char)
    return (
        0x1100 <= code <= 0x11FF
        or 0x2E80 <= code <= 0x2FFF
        or 0x3000 <= code <= 0x303F
        or 0x3040 <= code <= 0x30FF
        or 0x3100 <= code <= 0x312F
        or 0x3130 <= code <= 0x318F
        or 0x31A0 <= code <= 0x31BF
        or 0x31C0 <= code <= 0x31EF
        or 0x3200 <= code <= 0x33FF
        or 0x3400 <= code <= 0x4DBF
        or 0x4E00 <= code <= 0x9FFF
        or 0xA960 <= code <= 0xA97F
        or 0xAC00 <= code <= 0xD7AF
        or 0xD7B0 <= code <= 0xD7FF
        or 0xF900 <= code <= 0xFAFF
        or 0x20000 <= code <= 0x2EE5F
        or 0x2F800 <= code <= 0x2FA1F
        or 0x30000 <= code <= 0x323AF
        or 0xFF65 <= code <= 0xFFDC
    )


def _contains_cjk(text: str) -> bool:
    return any(_is_cjk_codepoint(char) for char in text)


def _text_value(block: Mapping[str, Any]) -> tuple[str, str, list[str]]:
    original = str(block.get("text") or "")
    role = str(block.get("role") or "").strip().lower()
    raw_bullets = block.get("bullets")
    bullets = (
        [str(item) for item in raw_bullets if str(item or "")]
        if isinstance(raw_bullets, list)
        else []
    )
    parts = [f"• {original}" if role == "bullet" else original] if original else []
    parts.extend(f"• {item}" for item in bullets)
    return "\n".join(parts), original, bullets


def _text_role_style(role: str, style_token: Any, *, default: str) -> str:
    token = str(style_token or "").strip()
    if token:
        if token not in {"type.display", "type.h1", "type.h2", "type.body", "type.caption"}:
            raise QuantaLayoutError(f"unsupported text style token {token!r}")
        return token.removeprefix("type.")
    if role in _TEXT_ROLES_TITLE:
        return default
    if role in _CAPTION_ROLES:
        return "caption"
    return "body"


def _font_for(style: str, has_cjk: bool, tokens: Mapping[str, Any]) -> dict[str, Any]:
    if has_cjk:
        key = "font.cjk.display" if style in {"display", "h1"} else "font.cjk.body"
    elif style in {"display", "h1"}:
        key = "font.latin.display"
    elif style == "h2":
        key = "font.latin.strong"
    else:
        key = "font.latin.body"
    return dict(tokens[key])


def _primitive_base(
    *,
    kind: str,
    leaf: _Leaf,
    slot: str,
    rect: Sequence[int],
    z_index: int,
    scale: float,
) -> dict[str, Any]:
    return {
        "kind": kind,
        "block_ref": leaf.block_ref,
        "slot": slot,
        "rect_px": _scale_rect(rect, scale),
        "z_index": z_index,
        "source_order": leaf.source_order,
    }


def _text_primitive(
    leaf: _Leaf,
    rect: Sequence[int],
    slot: str,
    *,
    tokens: Mapping[str, Any],
    scale: float,
    default_style: str,
    style: str | None = None,
    text: str | None = None,
    original_text: str | None = None,
    bullets: list[str] | None = None,
    role: str | None = None,
    max_lines: int | None = None,
    color_token: str | None = None,
) -> dict[str, Any]:
    block = leaf.block
    if text is None:
        rendered, original, actual_bullets = _text_value(block)
    else:
        rendered = str(text)
        original = rendered if original_text is None else str(original_text)
        actual_bullets = list(bullets or [])
    actual_role = str(role if role is not None else block.get("role") or "").strip().lower()
    actual_style = style or _text_role_style(
        actual_role, block.get("style_token"), default=default_style
    )
    if actual_style not in {"display", "h1", "h2", "body", "caption"}:
        raise QuantaLayoutError(f"unsupported resolved text style {actual_style!r}")
    has_cjk = _contains_cjk(rendered)
    font = _font_for(actual_style, has_cjk, tokens)
    preferred = _scaled(tokens[f"type.{actual_style}.size"], scale)
    fallback = _scaled(tokens[f"type.{actual_style}.fallback"], scale)
    fallback = max(fallback, 1)
    preferred = max(preferred, fallback + 1)
    rect_px = _scale_rect(rect, scale)
    if rect_px[2] <= 0 or rect_px[3] <= 0:
        raise QuantaLayoutError(f"text block {leaf.block_ref!r} has an empty slot")
    if max_lines is None:
        max_lines = {
            "display": 2,
            "h1": 2,
            "h2": 2,
            "body": 10 if actual_bullets else 6,
            "caption": 3,
        }[actual_style]
    line_spacing = float(
        tokens[f"type.{actual_style}.lh-{'cjk' if has_cjk else 'latin'}"]
    )
    try:
        result = autofit_text(
            rendered,
            font_config=font,
            size_steps_px=(preferred, fallback),
            max_width_px=rect_px[2],
            max_height_px=rect_px[3],
            max_lines=max_lines,
            line_spacing=line_spacing,
        )
    except TextLayoutError as exc:
        raise QuantaLayoutError(f"text block {leaf.block_ref!r}: {exc}") from exc
    color_key = color_token or (
        "color.text-secondary" if actual_role in _CAPTION_ROLES or actual_role == "label"
        else "color.text-primary"
    )
    if color_key not in tokens:
        raise QuantaLayoutError(f"unknown text color token {color_key!r}")
    primitive = _primitive_base(
        kind="text", leaf=leaf, slot=slot, rect=rect, z_index=20, scale=scale
    )
    primitive.update(
        {
            "text": rendered,
            "original_text": original,
            "bullets": actual_bullets,
            "line_breaks": list(result.lines),
            "line_height_px": result.line_height_px,
            "style": {
                "token": f"type.{actual_style}",
                "family": result.family,
                "path": result.path,
                "weight": result.weight,
                "font_style": result.font_style,
                "face_index": result.face_index,
                "color": tokens[color_key],
                "color_token": color_key,
                "final_size_px": result.size_px,
            },
            "autofit": {
                "size_steps_px": [preferred, fallback],
                "selected_step": result.selected_step,
                "overflow": result.overflow or len(actual_bullets) > 5,
                "overflow_reasons": list(result.overflow_reasons)
                + (["bullet_count"] if len(actual_bullets) > 5 else []),
                "measured_bounds_px": [result.width_px, result.height_px],
                "max_lines": max_lines,
            },
            "contains_cjk": has_cjk,
        }
    )
    return primitive


def _image_primitive(
    leaf: _Leaf,
    rect: Sequence[int],
    slot: str,
    *,
    scale: float,
    default_fit: str,
) -> dict[str, Any]:
    fit = str(leaf.block.get("fit") or default_fit).strip().lower()
    if fit not in {"cover", "contain"}:
        raise QuantaLayoutError(f"image {leaf.block_ref!r} has unsupported fit {fit!r}")
    anchor = str(leaf.block.get("anchor") or "center").strip().lower()
    if anchor not in {
        "center", "top", "bottom", "left", "right",
        "top-left", "top-right", "bottom-left", "bottom-right",
    }:
        raise QuantaLayoutError(
            f"image {leaf.block_ref!r} has unsupported anchor {anchor!r}"
        )
    primitive = _primitive_base(
        kind="image", leaf=leaf, slot=slot, rect=rect, z_index=0, scale=scale
    )
    primitive.update(
        {
            "asset_id": str(leaf.block.get("asset_id") or "") or None,
            "source": str(leaf.block.get("source") or "") or None,
            "query": str(leaf.block.get("query") or "") or None,
            "fit": fit,
            "anchor": anchor,
        }
    )
    return primitive


def _shape_primitive(
    leaf: _Leaf,
    rect: Sequence[int],
    slot: str,
    *,
    tokens: Mapping[str, Any],
    scale: float,
    shape: str | None = None,
    fill_token: str | None = None,
    z_index: int = 10,
) -> dict[str, Any]:
    resolved_shape = str(shape or leaf.block.get("shape") or "rect").strip().lower()
    if resolved_shape not in {"rect", "rounded-rect", "line", "ellipse"}:
        raise QuantaLayoutError(
            f"shape {leaf.block_ref!r} has unsupported primitive {resolved_shape!r}"
        )
    token = str(
        fill_token
        or leaf.block.get("fill_token")
        or ("color.accent" if leaf.role == "accent" else "color.surface-raised")
    ).strip()
    if token not in tokens or not token.startswith("color."):
        raise QuantaLayoutError(f"shape {leaf.block_ref!r} has unknown fill token {token!r}")
    primitive = _primitive_base(
        kind="shape", leaf=leaf, slot=slot, rect=rect, z_index=z_index, scale=scale
    )
    primitive.update(
        {
            "shape": resolved_shape,
            "fill": tokens[token],
            "fill_token": token,
            "corner_radius_px": _scaled(24 if resolved_shape == "rounded-rect" else 0, scale),
        }
    )
    return primitive


def _synthetic_card(
    leaf: _Leaf,
    rect: Sequence[int],
    slot: str,
    *,
    tokens: Mapping[str, Any],
    scale: float,
) -> dict[str, Any]:
    # Same semantic ref means build filtering removes the card and its content
    # as one unit; group itself never becomes a primitive.
    return _shape_primitive(
        leaf,
        rect,
        slot,
        tokens=tokens,
        scale=scale,
        shape="rounded-rect",
        fill_token="color.surface-raised",
        z_index=10,
    )


def _stat_primitives(
    leaf: _Leaf,
    rect: Sequence[int],
    slot: str,
    *,
    tokens: Mapping[str, Any],
    scale: float,
) -> list[dict[str, Any]]:
    x, y, width, height = rect
    pad = int(tokens["spacing.card-pad"])
    if width <= 2 * pad or height <= 2 * pad + 120:
        raise QuantaLayoutError(f"stat card {leaf.block_ref!r} is too small")
    inner_width = width - 2 * pad
    inner_height = height - 2 * pad
    value_height = max(120, int(round(inner_height * 0.54)))
    gap = int(tokens["spacing.block-gap"])
    label_height = inner_height - value_height - gap
    if label_height <= 0:
        raise QuantaLayoutError(f"stat card {leaf.block_ref!r} cannot fit value and label")
    card = _synthetic_card(leaf, rect, f"{slot}.card", tokens=tokens, scale=scale)
    value = _text_primitive(
        leaf,
        [x + pad, y + pad, inner_width, value_height],
        f"{slot}.value",
        tokens=tokens,
        scale=scale,
        default_style="display",
        style="display",
        text=str(leaf.block.get("value") or ""),
        original_text=str(leaf.block.get("value") or ""),
        role="value",
        max_lines=1,
    )
    label = _text_primitive(
        leaf,
        [x + pad, y + pad + value_height + gap, inner_width, label_height],
        f"{slot}.label",
        tokens=tokens,
        scale=scale,
        default_style="body",
        style="body",
        text=str(leaf.block.get("label") or ""),
        original_text=str(leaf.block.get("label") or ""),
        role="label",
        max_lines=2,
    )
    return [card, value, label]


def _entity_fixed_height(raw: Mapping[str, Any]) -> int | None:
    kind = str(raw.get("kind") or "").strip().lower()
    role = str(raw.get("role") or "").strip().lower()
    if kind == "shape" and role == "accent":
        return 8
    if kind == "text" and role in _CAPTION_ROLES:
        return 96
    if kind == "text" and role == "bullet":
        return 96
    return None


def _stack_rects(
    entities: Sequence[Mapping[str, Any]],
    rect: Sequence[int],
    *,
    gap: int,
) -> list[list[int]]:
    if not entities:
        return []
    x, y, width, height = rect
    fixed = [_entity_fixed_height(entity) for entity in entities]
    available = height - gap * (len(entities) - 1) - sum(v or 0 for v in fixed)
    flexible_count = sum(value is None for value in fixed)
    if available < flexible_count:
        raise QuantaLayoutError("content entities do not fit the template body slot")
    flexible_sizes: list[int] = []
    if flexible_count:
        base, remainder = divmod(available, flexible_count)
        flexible_sizes = [base + (1 if i < remainder else 0) for i in range(flexible_count)]
    result: list[list[int]] = []
    cursor = y
    flexible_index = 0
    for fixed_height in fixed:
        entity_height = fixed_height
        if entity_height is None:
            entity_height = flexible_sizes[flexible_index]
            flexible_index += 1
        result.append([x, cursor, width, entity_height])
        cursor += entity_height + gap
    return result


def _horizontal_rects(rect: Sequence[int], count: int, *, gap: int) -> list[list[int]]:
    x, y, width, height = rect
    available = width - gap * (count - 1)
    if available < count:
        raise QuantaLayoutError("group is too narrow for its homogeneous children")
    base, remainder = divmod(available, count)
    widths = [base + (1 if index < remainder else 0) for index in range(count)]
    result: list[list[int]] = []
    cursor = x
    for child_width in widths:
        result.append([cursor, y, child_width, height])
        cursor += child_width + gap
    return result


def _layout_leaf_entity(
    leaf: _Leaf,
    rect: Sequence[int],
    slot: str,
    *,
    tokens: Mapping[str, Any],
    scale: float,
    cardish: bool = False,
) -> list[dict[str, Any]]:
    output: list[dict[str, Any]] = []
    content_rect = list(rect)
    if cardish:
        output.append(_synthetic_card(leaf, rect, f"{slot}.card", tokens=tokens, scale=scale))
        content_rect = _inset_rect(rect, int(tokens["spacing.card-pad"]))
    if leaf.kind == "text":
        output.append(
            _text_primitive(
                leaf,
                content_rect,
                slot,
                tokens=tokens,
                scale=scale,
                default_style="body",
            )
        )
    elif leaf.kind == "image":
        output.append(
            _image_primitive(leaf, content_rect, slot, scale=scale, default_fit="cover")
        )
    elif leaf.kind == "shape":
        output.append(
            _shape_primitive(leaf, content_rect, slot, tokens=tokens, scale=scale)
        )
    elif leaf.kind == "stat":
        output.extend(
            _stat_primitives(leaf, content_rect, slot, tokens=tokens, scale=scale)
        )
    else:  # pragma: no cover - _walk_blocks establishes the invariant.
        raise QuantaLayoutError(f"unsupported leaf kind {leaf.kind!r}")
    return output


def _layout_entity(
    raw: Mapping[str, Any],
    rect: Sequence[int],
    slot: str,
    *,
    by_identity: Mapping[int, _Leaf],
    tokens: Mapping[str, Any],
    scale: float,
) -> list[dict[str, Any]]:
    kind = str(raw.get("kind") or "").strip().lower()
    if kind != "group":
        leaf = by_identity.get(id(raw))
        if leaf is None:
            raise QuantaLayoutError("internal leaf identity mismatch")
        return _layout_leaf_entity(leaf, rect, slot, tokens=tokens, scale=scale)

    children = raw.get("children")
    if not isinstance(children, list) or not children:
        return []
    direct_kinds = [
        str(child.get("kind") or "").strip().lower()
        for child in children
        if isinstance(child, Mapping)
    ]
    group_role = str(raw.get("role") or "").strip().lower()
    homogeneous_horizontal = (
        len(children) in {3, 4, 5}
        and len(direct_kinds) == len(children)
        and len(set(direct_kinds)) == 1
        and direct_kinds[0] != "group"
        and group_role in {"body", "card", "cards", "grid", "grid-cards", "steps", "products"}
    )
    output: list[dict[str, Any]] = []
    if homogeneous_horizontal:
        horizontal_rect = list(rect)
        if horizontal_rect[3] > 360:
            horizontal_rect[1] += (horizontal_rect[3] - 360) // 2
            horizontal_rect[3] = 360
        rects = _horizontal_rects(
            horizontal_rect, len(children), gap=int(tokens["spacing.group-gap"])
        )
        for index, (child, child_rect) in enumerate(zip(children, rects), 1):
            leaf = by_identity.get(id(child))
            if leaf is None:
                raise QuantaLayoutError("horizontal groups may contain semantic leaves only")
            cardish = group_role in {
                "body", "card", "cards", "grid", "grid-cards", "steps", "products"
            } or leaf.role == "card"
            output.extend(
                _layout_leaf_entity(
                    leaf,
                    child_rect,
                    f"{slot}.item-{index}",
                    tokens=tokens,
                    scale=scale,
                    cardish=cardish,
                )
            )
        return output

    mapped_children = [child for child in children if isinstance(child, Mapping)]
    child_rects = _stack_rects(
        mapped_children,
        rect,
        gap=int(tokens[
            "spacing.bullet-gap" if group_role == "bullets" else "spacing.block-gap"
        ]),
    )
    for index, (child, child_rect) in enumerate(zip(mapped_children, child_rects), 1):
        output.extend(
            _layout_entity(
                child,
                child_rect,
                f"{slot}.item-{index}",
                by_identity=by_identity,
                tokens=tokens,
                scale=scale,
            )
        )
    return output


def _semantic_text(leaf: _Leaf) -> str:
    rendered, _, _ = _text_value(leaf.block)
    return " ".join(rendered.split()).casefold()


def _heading_leaf(leaves: Sequence[_Leaf], slide_title: str, *, fallback_first: bool) -> _Leaf | None:
    title_key = " ".join(slide_title.split()).casefold()
    role_candidates = [leaf for leaf in leaves if leaf.kind == "text" and leaf.role in _TEXT_ROLES_TITLE]
    matching = [
        leaf
        for leaf in leaves
        if leaf.kind == "text" and title_key and _semantic_text(leaf) == title_key
    ]
    candidates: list[_Leaf] = []
    for leaf in [*role_candidates, *matching]:
        if leaf not in candidates:
            candidates.append(leaf)
    if len(candidates) > 1:
        raise QuantaLayoutError("slide has multiple conflicting semantic title blocks")
    if candidates:
        return candidates[0]
    if fallback_first:
        return next((leaf for leaf in leaves if leaf.kind == "text"), None)
    return None


def _chrome_leaf(slide_id: str, title: str) -> _Leaf:
    block = {"id": f"slide:{slide_id}:title", "kind": "text", "role": "title", "text": title}
    return _Leaf(block, block["id"], -1)


def _layout_title(
    slide: Mapping[str, Any],
    blocks: Sequence[Mapping[str, Any]],
    leaves: Sequence[_Leaf],
    by_identity: Mapping[int, _Leaf],
    *,
    tokens: Mapping[str, Any],
    scale: float,
) -> tuple[list[dict[str, Any]], _Leaf | None, bool]:
    title_rect = [160, 408, 1192, 240]  # bottom = 648px
    subtitle_rect = [160, 680, 1192, 280]  # title bottom + spacing.l
    heading = _heading_leaf(leaves, str(slide.get("title") or ""), fallback_first=True)
    top_level_identities = {id(block) for block in blocks}
    output: list[dict[str, Any]] = []
    images = [leaf for leaf in leaves if leaf.kind == "image"]
    if len(images) > 1:
        raise QuantaLayoutError("title layout supports at most one hero image")
    special = [*images, *(leaf for leaf in leaves if leaf.kind == "shape" and leaf.role == "accent")]
    if heading is not None:
        special.append(heading)
    if any(id(leaf.block) not in top_level_identities for leaf in special):
        raise QuantaLayoutError(
            "title layout requires title, hero image, and accent slot blocks at top level"
        )
    background_image = bool(images)
    if images:
        output.append(
            _image_primitive(
                images[0], [0, 0, 1920, 1080], "background", scale=scale, default_fit="cover"
            )
        )
    if heading is not None:
        output.append(
            _text_primitive(
                heading,
                title_rect,
                "title",
                tokens=tokens,
                scale=scale,
                default_style="display",
                style="display",
                max_lines=2,
            )
        )
    accent_ids: set[int] = set()
    for leaf in leaves:
        if leaf.kind == "shape" and leaf.role == "accent":
            output.append(
                _shape_primitive(
                    leaf,
                    [160, title_rect[1] - 48, 104, 8],
                    "accent",
                    tokens=tokens,
                    scale=scale,
                    shape="rect",
                    fill_token="color.accent",
                    z_index=15,
                )
            )
            accent_ids.add(id(leaf.block))
    excluded = {id(leaf.block) for leaf in images}
    if heading is not None:
        excluded.add(id(heading.block))
    excluded |= accent_ids
    entities = [block for block in blocks if id(block) not in excluded]
    rects = _stack_rects(entities, subtitle_rect, gap=int(tokens["spacing.block-gap"]))
    for index, (entity, rect) in enumerate(zip(entities, rects), 1):
        output.extend(
            _layout_entity(
                entity,
                rect,
                f"subtitle.item-{index}",
                by_identity=by_identity,
                tokens=tokens,
                scale=scale,
            )
        )
    return output, heading, background_image


def _layout_content(
    slide: Mapping[str, Any],
    blocks: Sequence[Mapping[str, Any]],
    leaves: Sequence[_Leaf],
    by_identity: Mapping[int, _Leaf],
    *,
    tokens: Mapping[str, Any],
    scale: float,
) -> tuple[list[dict[str, Any]], _Leaf | None, bool]:
    heading_rect = [160, 120, 1600, 160]
    body_top = heading_rect[1] + heading_rect[3] + int(tokens["spacing.slot-gap"])
    body_height = 960 - body_top
    heading = _heading_leaf(leaves, str(slide.get("title") or ""), fallback_first=False)
    if heading is not None and id(heading.block) not in {id(block) for block in blocks}:
        raise QuantaLayoutError("content layout requires its semantic heading at top level")
    output: list[dict[str, Any]] = []
    if heading is not None:
        output.append(
            _text_primitive(
                heading,
                heading_rect,
                "heading",
                tokens=tokens,
                scale=scale,
                default_style="h1",
                style="h1",
                max_lines=2,
            )
        )
    top_images = [block for block in blocks if str(block.get("kind") or "").lower() == "image"]
    if len(top_images) > 1:
        raise QuantaLayoutError("content layout supports at most one top-level media block")
    if top_images:
        image_leaf = by_identity.get(id(top_images[0]))
        if image_leaf is None:
            raise QuantaLayoutError("content media must be a semantic image leaf")
        output.append(
            _image_primitive(
                image_leaf,
                [1112, body_top, 648, body_height],
                "media",
                scale=scale,
                default_fit="cover",
            )
        )
    body_width = 920 if top_images else 1192
    excluded_ids = {id(block) for block in top_images}
    if heading is not None:
        excluded_ids.add(id(heading.block))
    entities = [block for block in blocks if id(block) not in excluded_ids]
    rects = _stack_rects(
        entities,
        [160, body_top, body_width, body_height],
        gap=int(tokens["spacing.block-gap"]),
    )
    for index, (entity, rect) in enumerate(zip(entities, rects), 1):
        output.extend(
            _layout_entity(
                entity,
                rect,
                f"body.item-{index}",
                by_identity=by_identity,
                tokens=tokens,
                scale=scale,
            )
        )
    return output, heading, False


def _layout_stat(
    slide: Mapping[str, Any],
    blocks: Sequence[Mapping[str, Any]],
    leaves: Sequence[_Leaf],
    by_identity: Mapping[int, _Leaf],
    *,
    tokens: Mapping[str, Any],
    scale: float,
) -> tuple[list[dict[str, Any]], _Leaf | None, bool]:
    del blocks, by_identity  # stat geometry is leaf-oriented by design.
    heading_rect = [160, 120, 1600, 160]
    body_top = heading_rect[1] + heading_rect[3] + int(tokens["spacing.slot-gap"])
    heading = _heading_leaf(leaves, str(slide.get("title") or ""), fallback_first=False)
    output: list[dict[str, Any]] = []
    if heading is not None:
        output.append(
            _text_primitive(
                heading,
                heading_rect,
                "heading",
                tokens=tokens,
                scale=scale,
                default_style="h1",
                style="h1",
                max_lines=2,
            )
        )
    stats = [leaf for leaf in leaves if leaf.kind == "stat"]
    allowed_refs = {leaf.block_ref for leaf in stats}
    if heading is not None:
        allowed_refs.add(heading.block_ref)
    extras = [
        leaf
        for leaf in leaves
        if leaf.block_ref not in allowed_refs
        and not (leaf.kind == "shape" and leaf.role == "accent")
    ]
    if extras:
        raise QuantaLayoutError("stat layout only supports a heading, stat leaves, and accent shapes")
    count = len(stats)
    if count < 1 or count > 4:
        raise QuantaLayoutError("stat layout supports one to four stat blocks")
    spans = {
        1: [(1, 12)],
        2: [(1, 6), (7, 12)],
        3: [(1, 4), (5, 8), (9, 12)],
        4: [(1, 3), (4, 6), (7, 9), (10, 12)],
    }[count]
    remaining_height = 960 - body_top
    card_height = 360
    card_y = body_top + (remaining_height - card_height) // 2
    for index, (leaf, span) in enumerate(zip(stats, spans), 1):
        rect = _column_span(*span, tokens)
        rect[1] = card_y
        rect[3] = card_height
        output.extend(
            _stat_primitives(
                leaf, rect, f"stat-row.item-{index}", tokens=tokens, scale=scale
            )
        )
    for leaf in leaves:
        if leaf.kind == "shape" and leaf.role == "accent":
            output.append(
                _shape_primitive(
                    leaf,
                    [160, body_top - 32, 104, 8],
                    "accent",
                    tokens=tokens,
                    scale=scale,
                    shape="rect",
                    fill_token="color.accent",
                    z_index=15,
                )
            )
    return output, heading, False


def _layout_full_bleed(
    slide: Mapping[str, Any],
    blocks: Sequence[Mapping[str, Any]],
    leaves: Sequence[_Leaf],
    by_identity: Mapping[int, _Leaf],
    *,
    tokens: Mapping[str, Any],
    scale: float,
) -> tuple[list[dict[str, Any]], _Leaf | None, bool]:
    del blocks, by_identity
    images = [leaf for leaf in leaves if leaf.kind == "image"]
    if len(images) > 1:
        raise QuantaLayoutError("full-bleed layout supports exactly one image plane")
    output: list[dict[str, Any]] = []
    if images:
        output.append(
            _image_primitive(
                images[0], [0, 0, 1920, 1080], "image", scale=scale, default_fit="cover"
            )
        )
    text_leaves = [leaf for leaf in leaves if leaf.kind == "text"]
    heading = _heading_leaf(text_leaves, str(slide.get("title") or ""), fallback_first=False)
    ordered_text = ([heading] if heading is not None else []) + [
        leaf for leaf in text_leaves if leaf is not heading
    ]
    heights = [240 if leaf is heading else (96 if leaf.role in _CAPTION_ROLES else 160) for leaf in ordered_text]
    gap = int(tokens["spacing.block-gap"])
    total = sum(heights) + gap * max(len(heights) - 1, 0)
    if total > 840:
        raise QuantaLayoutError("full-bleed overlay text does not fit its caption slot")
    cursor = 960 - total
    for index, (leaf, height) in enumerate(zip(ordered_text, heights), 1):
        output.append(
            _text_primitive(
                leaf,
                [160, cursor, 784, height],
                "caption.title" if leaf is heading else f"caption.item-{index}",
                tokens=tokens,
                scale=scale,
                default_style="display" if leaf is heading else "caption",
                style="display" if leaf is heading else None,
                max_lines=2 if leaf is heading else None,
            )
        )
        cursor += height + gap
    other = [
        leaf
        for leaf in leaves
        if leaf.kind not in {"image", "text"}
        and not (leaf.kind == "shape" and leaf.role == "accent")
    ]
    if other:
        raise QuantaLayoutError("full-bleed layout supports image, text, and accent leaves only")
    if ordered_text:
        first_y = 960 - total
        for leaf in leaves:
            if leaf.kind == "shape" and leaf.role == "accent":
                output.append(
                    _shape_primitive(
                        leaf,
                        [160, first_y - 48, 104, 8],
                        "accent",
                        tokens=tokens,
                        scale=scale,
                        shape="rect",
                        fill_token="color.accent",
                        z_index=15,
                    )
                )
    return output, heading, bool(images)


def _selected_build(
    slide: Mapping[str, Any],
    leaf_ids: Sequence[str],
    build_id: str | None,
) -> tuple[str | None, float, set[str]]:
    raw_builds = slide.get("builds")
    builds = [item for item in raw_builds if isinstance(item, Mapping)] if isinstance(raw_builds, list) else []
    if not builds:
        if build_id is not None:
            raise QuantaLayoutError(f"unknown build id {build_id!r}")
        return None, 3.0, set(leaf_ids)
    selected: Mapping[str, Any] | None = None
    if build_id is None:
        selected = builds[-1]
    else:
        selected = next(
            (item for item in builds if str(item.get("id") or "") == build_id), None
        )
        if selected is None:
            raise QuantaLayoutError(f"unknown build id {build_id!r}")
    selected_id = str(selected.get("id") or "").strip()
    if not selected_id:
        raise QuantaLayoutError("selected build is missing an id")
    dwell = _finite(selected.get("dwell_sec", 3.0), "build.dwell_sec", minimum=0)
    if dwell <= 0:
        raise QuantaLayoutError("build.dwell_sec must be > 0")
    raw_visible = selected.get("visible_block_ids")
    if raw_visible is None:
        visible = list(leaf_ids)
    elif isinstance(raw_visible, list):
        visible = [str(value or "").strip() for value in raw_visible]
    else:
        raise QuantaLayoutError("build.visible_block_ids must be a list")
    if len(visible) != len(set(visible)):
        raise QuantaLayoutError("build.visible_block_ids contains duplicates")
    unknown = sorted(set(visible) - set(leaf_ids))
    if unknown:
        raise QuantaLayoutError(f"build references unknown semantic leaves: {unknown!r}")
    return selected_id, dwell, set(visible)


def _scrim(
    text_primitive: Mapping[str, Any],
    *,
    tokens: Mapping[str, Any],
    canvas: tuple[int, int],
) -> dict[str, Any]:
    return {
        "kind": "shape",
        "block_ref": text_primitive["block_ref"],
        "slot": "scrim",
        "rect_px": [0, 0, canvas[0], canvas[1]],
        "z_index": 10,
        "source_order": text_primitive["source_order"],
        "shape": "rect",
        "fill": tokens["color.scrim"],
        "fill_token": "color.scrim",
        "corner_radius_px": 0,
        "synthetic": True,
    }


def _sort_display_list(primitives: Iterable[dict[str, Any]]) -> list[dict[str, Any]]:
    annotated: list[tuple[int, dict[str, Any]]] = list(enumerate(primitives))
    annotated.sort(
        key=lambda item: (
            int(item[1]["z_index"]),
            int(item[1]["source_order"]),
            str(item[1]["block_ref"]),
            item[0],
        )
    )
    return [primitive for _, primitive in annotated]


def layout_slide(
    slide: Mapping[str, Any],
    *,
    theme_tokens: Mapping[str, Any] | None = None,
    canvas: tuple[int, int] = (1920, 1080),
    build_id: str | None = None,
) -> dict[str, Any]:
    """Project one normalized slide into deterministic placed primitives.

    ``visible_block_ids`` is a full build snapshot, not a reveal delta.  The
    complete slide is laid out first; selection only removes primitives whose
    ``block_ref`` is absent.  An explicit empty snapshot therefore produces an
    empty display list without moving anything in later builds.
    """
    if not isinstance(slide, Mapping):
        raise QuantaLayoutError("slide must be a mapping")
    tokens = _resolve_tokens(theme_tokens)
    width, height, scale = _canvas(canvas)
    layout = str(slide.get("layout") or "content").strip().lower()
    if layout not in _LAYOUTS:
        raise QuantaLayoutError(f"unknown quanta layout {layout!r}")
    slide_id = str(slide.get("id") or "").strip()
    if not slide_id:
        raise QuantaLayoutError("slide is missing an id")
    raw_blocks = slide.get("blocks")
    blocks = [block for block in raw_blocks if isinstance(block, Mapping)] if isinstance(raw_blocks, list) else []
    if len(blocks) != len(raw_blocks or []):
        raise QuantaLayoutError("slide.blocks must contain mappings only")
    leaves, by_identity = _walk_blocks(blocks)
    leaf_ids = [leaf.block_ref for leaf in leaves]
    selected_id, dwell, visible = _selected_build(slide, leaf_ids, build_id)

    layout_fn = {
        "title": _layout_title,
        "content": _layout_content,
        "stat": _layout_stat,
        "full-bleed": _layout_full_bleed,
    }[layout]
    full_primitives, heading, has_background_image = layout_fn(
        slide,
        blocks,
        leaves,
        by_identity,
        tokens=tokens,
        scale=scale,
    )

    selected = [
        primitive for primitive in full_primitives if primitive["block_ref"] in visible
    ]
    title = str(slide.get("title") or "")
    # slide.title is chrome, not a second copy of a semantic title.  If an
    # authored semantic title exists at any build, its visibility is respected
    # (we do not leak chrome into an earlier empty/background-only build).
    if title and heading is None:
        chrome = _chrome_leaf(slide_id, title)
        chrome_rect = {
            "title": [160, 408, 1192, 240],
            "content": [160, 120, 1600, 160],
            "stat": [160, 120, 1600, 160],
            "full-bleed": [160, 720, 784, 240],
        }[layout]
        chrome_style = "display" if layout in {"title", "full-bleed"} else "h1"
        selected.append(
            _text_primitive(
                chrome,
                chrome_rect,
                "title" if layout == "title" else ("caption.title" if layout == "full-bleed" else "heading"),
                tokens=tokens,
                scale=scale,
                default_style=chrome_style,
                style=chrome_style,
                max_lines=2,
            )
        )

    visible_images = [primitive for primitive in selected if primitive["kind"] == "image"]
    visible_text = [primitive for primitive in selected if primitive["kind"] == "text"]
    if has_background_image and visible_images and visible_text:
        selected.append(_scrim(visible_text[0], tokens=tokens, canvas=(width, height)))

    placed = _sort_display_list(selected)
    overflow: list[dict[str, Any]] = []
    for primitive in placed:
        if primitive["kind"] != "text" or not primitive["autofit"]["overflow"]:
            continue
        overflow.append(
            {
                "block_ref": primitive["block_ref"],
                "slot": primitive["slot"],
                "reasons": list(primitive["autofit"]["overflow_reasons"]),
                "selected_step": primitive["autofit"]["selected_step"],
                "measured_bounds_px": list(primitive["autofit"]["measured_bounds_px"]),
                "rect_px": list(primitive["rect_px"]),
            }
        )

    result = {
        "layout_version": LAYOUT_VERSION,
        "token_version": TOKEN_VERSION,
        "canvas_px": [width, height],
        "safe_rect_px": _scale_rect([160, 120, 1600, 840], scale),
        "slide_id": slide_id,
        "layout": layout,
        "build_id": selected_id,
        "dwell_sec": dwell,
        "background_color": tokens["color.surface"],
        "placed_blocks": placed,
        "overflow": overflow,
    }
    # Fail here, close to the producer, if a future edit adds a non-JSON value.
    try:
        json.dumps(result, ensure_ascii=False, sort_keys=True, allow_nan=False)
    except (TypeError, ValueError) as exc:  # pragma: no cover - invariant guard.
        raise QuantaLayoutError(f"layout result is not deterministic JSON: {exc}") from exc
    return result


__all__ = [
    "DEFAULT_QUANTA_TOKENS",
    "QuantaLayoutError",
    "LAYOUT_VERSION",
    "TOKEN_VERSION",
    "layout_slide",
]
