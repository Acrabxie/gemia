"""Deterministic text measurement, line breaking, and two-step autofit.

The video and quanta products share this module so layout decisions and rendered
line breaks cannot quietly diverge.  It deliberately stays below either domain:
fonts are resolved through :mod:`gemia.video.fonts`, while every operation after
font resolution is a pure function of text, font bytes, and numeric bounds.

The line breaker implements Lumeri's v1 CJK minimum set:

* closing punctuation is not stranded at the start of a line;
* opening punctuation is not stranded at the end of a line;
* Latin-style words remain indivisible, including common internal connectors;
* grapheme clusters, original whitespace, and explicit line boundaries survive;
* non-breaking spaces and word joiners never become wrap points.

Font tokens are strict: a configured path/family/weight must resolve to one
specific font face (including a TTC face index), and CJK text is rejected when
that face would render a missing-glyph box.  The resolved face metadata is
returned so raster and PPTX projections cannot make different font choices.

Autofit is intentionally discrete.  Callers provide exactly two type-scale
steps, largest first.  If neither fits, the smallest layout is returned with a
structured ``overflow`` signal; text is never silently truncated or squeezed to
an arbitrary size.
"""
from __future__ import annotations

import math
import re
import unicodedata
from dataclasses import dataclass
from functools import lru_cache
from numbers import Real
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence

from PIL import Image, ImageDraw, ImageFont
import regex

from gemia.video.fonts import resolve_font_path

WidthMeasure = Callable[[str], float]

# Characters that should travel with the following/preceding text at a wrap.
_OPENING_PUNCTUATION = frozenset("([{（［｛【〔〈《「『〖〘〚“‘")
_CLOSING_PUNCTUATION = frozenset(
    ")]}）］｝】〕〉》」』〗〙〛”，、。！？：；,.!?;:%％…’"
)
_WORD_CONNECTORS = frozenset("'’._:+-/@")
_NO_BREAK_SPACES = frozenset("\u00a0\u202f\u2060\ufeff")
_LINE_BREAK_RE = re.compile(r"\r\n|[\n\r\v\f\x1c-\x1e\x85\u2028\u2029]")
_GRAPHEME_RE = regex.compile(r"\X")


class TextLayoutError(ValueError):
    """Raised when a deterministic layout cannot be constructed."""


@dataclass(frozen=True)
class TextMetrics:
    """Pixel metrics measured with one resolved font."""

    width_px: int
    height_px: int
    line_height_px: int
    bbox_px: tuple[int, int, int, int]

    def to_dict(self) -> dict[str, Any]:
        return {
            "width_px": self.width_px,
            "height_px": self.height_px,
            "line_height_px": self.line_height_px,
            "bbox_px": list(self.bbox_px),
        }


@dataclass(frozen=True)
class TextLayoutResult:
    """The selected type-scale step plus visible overflow evidence."""

    family: str
    path: str
    weight: int
    font_style: str
    face_index: int
    size_px: int
    preferred_size_px: int
    fallback_size_px: int
    selected_step: str
    lines: tuple[str, ...]
    width_px: int
    height_px: int
    line_height_px: int
    overflow: bool
    overflow_reasons: tuple[str, ...]

    def to_dict(self) -> dict[str, Any]:
        """Return the stable shape consumed by quanta ``placed blocks``."""
        return {
            "style": {
                "family": self.family,
                "path": self.path,
                "weight": self.weight,
                "font_style": self.font_style,
                "face_index": self.face_index,
                "size_px": self.preferred_size_px,
            },
            "autofit": {
                "final_size_px": self.size_px,
                "size_steps_px": [self.preferred_size_px, self.fallback_size_px],
                "selected_step": self.selected_step,
                "line_breaks": list(self.lines),
                "overflow": self.overflow,
                "overflow_reasons": list(self.overflow_reasons),
            },
            "bounds_px": [self.width_px, self.height_px],
            "line_height_px": self.line_height_px,
        }


@dataclass(frozen=True)
class _ResolvedFont:
    font: ImageFont.FreeTypeFont
    family: str
    path: str
    weight: int
    style: str
    face_index: int


def _normalize_font_name(value: str) -> str:
    normalized = unicodedata.normalize("NFKC", value).casefold()
    return "".join(char for char in normalized if char.isalnum())


def _style_weight(style: str) -> int | None:
    key = _normalize_font_name(style)
    match = re.search(r"w([1-9])", key)
    if match:
        return int(match.group(1)) * 100
    # Specific compound names must precede their shorter suffixes.
    for marker, weight in (
        ("ultralight", 100),
        ("thin", 100),
        ("extralight", 200),
        ("demibold", 600),
        ("semibold", 600),
        ("demi", 600),
        ("extrabold", 800),
        ("ultrabold", 800),
        ("heavy", 800),
        ("black", 900),
        ("light", 300),
        ("medium", 500),
        ("book", 400),
        ("regular", 400),
        ("roman", 400),
        ("normal", 400),
        ("bold", 700),
    ):
        if marker in key:
            return weight
    return None


@lru_cache(maxsize=256)
def _load_font(path: str, size_px: int, face_index: int) -> ImageFont.FreeTypeFont:
    try:
        return ImageFont.truetype(path, size_px, index=face_index)
    except Exception as exc:  # Pillow gives backend-specific exception types.
        raise TextLayoutError(
            f"unable to load font {path!r} face {face_index} at {size_px}px: {exc}"
        ) from exc


@lru_cache(maxsize=128)
def _font_faces(path: str, size_px: int) -> tuple[_ResolvedFont, ...]:
    faces: list[_ResolvedFont] = []
    for face_index in range(64):
        try:
            font = ImageFont.truetype(path, size_px, index=face_index)
        except OSError as exc:
            if face_index == 0:
                raise TextLayoutError(
                    f"unable to load font {path!r} at {size_px}px: {exc}"
                ) from exc
            break
        family, style = (str(value or "").strip() for value in font.getname())
        weight = _style_weight(style)
        if weight is None:
            # An unknown style cannot honestly satisfy the family/path/weight
            # contract shared by the rasterizer and PPTX projection.
            continue
        faces.append(
            _ResolvedFont(
                font=font,
                family=family or Path(path).stem,
                path=path,
                weight=weight,
                style=style or "Regular",
                face_index=face_index,
            )
        )
    if not faces:
        raise TextLayoutError(f"font {path!r} exposes no face with a known weight")
    return tuple(faces)


def _resolved_font(
    font_config: Mapping[str, Any],
    size_px: int,
) -> _ResolvedFont:
    if not isinstance(size_px, int) or isinstance(size_px, bool) or size_px <= 0:
        raise TextLayoutError("size_px must be a positive integer")
    size = size_px
    if not isinstance(font_config, Mapping):
        raise TextLayoutError(
            "font_config must explicitly provide family, path, and weight"
        )
    config = dict(font_config)
    missing: list[str] = []
    if not str(config.get("family") or "").strip():
        missing.append("family")
    if not str(config.get("path") or "").strip():
        missing.append("path")
    if "weight" not in config or config.get("weight") is None:
        missing.append("weight")
    if missing:
        raise TextLayoutError(
            "font_config must explicitly provide family, path, and weight; "
            f"missing={missing!r}"
        )
    direct_path = str(config.get("path") or "").strip()
    direct = Path(direct_path).expanduser()
    if not direct.exists() or not direct.is_file():
        raise TextLayoutError(f"configured font path does not exist: {direct_path!r}")
    resolved = resolve_font_path(config)
    if not resolved:
        raise TextLayoutError(
            "no usable font resolved; install/configure a local font instead of "
            "silently rendering with a non-CJK fallback"
        )
    path = str(Path(resolved).expanduser().resolve())
    requested_family = str(config.get("family") or "").strip()
    raw_weight = config.get("weight", 400)
    if not isinstance(raw_weight, int) or isinstance(raw_weight, bool):
        raise TextLayoutError("font weight must be an integer")
    if not 1 <= raw_weight <= 1000:
        raise TextLayoutError("font weight must be in [1, 1000]")

    faces = list(_font_faces(path, size))
    if requested_family:
        requested_key = _normalize_font_name(requested_family)
        faces = [face for face in faces if _normalize_font_name(face.family) == requested_key]
        if not faces:
            actual = sorted({face.family for face in _font_faces(path, size)})
            raise TextLayoutError(
                f"configured family {requested_family!r} does not match font faces {actual!r}"
            )
    faces = [face for face in faces if face.weight == raw_weight]
    if not faces:
        available = [
            f"{face.family} {face.style} ({face.weight})"
            for face in _font_faces(path, size)
        ]
        raise TextLayoutError(
            f"font {path!r} has no face for requested weight {raw_weight}; "
            f"available={available!r}"
        )
    faces.sort(
        key=lambda face: (
            "italic" in face.style.casefold() or "oblique" in face.style.casefold(),
            face.face_index,
        )
    )
    selected = faces[0]
    # Return the cached instance used everywhere else, including the exact TTC
    # face index chosen above.
    return _ResolvedFont(
        font=_load_font(path, size, selected.face_index),
        family=selected.family,
        path=path,
        weight=selected.weight,
        style=selected.style,
        face_index=selected.face_index,
    )


def _draw() -> ImageDraw.ImageDraw:
    return ImageDraw.Draw(Image.new("L", (1, 1), 0))


def _font_line_height(font: ImageFont.ImageFont) -> int:
    try:
        ascent, descent = font.getmetrics()
        height = int(ascent) + int(descent)
    except Exception:
        box = _draw().textbbox((0, 0), "Ag国", font=font)
        height = int(box[3] - box[1])
    return max(height, 1)


def _measure_with_font(text: str, font: ImageFont.ImageFont) -> TextMetrics:
    if text:
        box = _draw().textbbox((0, 0), text, font=font)
        width = max(int(math.ceil(box[2] - box[0])), 0)
        height = max(int(math.ceil(box[3] - box[1])), 0)
    else:
        box = (0, 0, 0, 0)
        width = 0
        height = 0
    return TextMetrics(
        width_px=width,
        height_px=height,
        line_height_px=_font_line_height(font),
        bbox_px=tuple(int(value) for value in box),
    )


def measure_text(
    text: str,
    *,
    font_config: Mapping[str, Any],
    size_px: int,
) -> TextMetrics:
    """Measure one text run using the canonical font resolver."""
    value = str(text)
    resolved = _resolved_font(font_config, size_px)
    _ensure_text_coverage(value, resolved)
    return _measure_with_font(value, resolved.font)


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
        or 0x20000 <= code <= 0x2A6DF
        or 0x2A700 <= code <= 0x2B73F
        or 0x2B740 <= code <= 0x2B81F
        or 0x2B820 <= code <= 0x2CEAF
        or 0x2CEB0 <= code <= 0x2EBEF
        or 0x2EBF0 <= code <= 0x2EE5F
        or 0x2F800 <= code <= 0x2FA1F
        or 0x30000 <= code <= 0x323AF
        or 0xFF65 <= code <= 0xFFDC
    )


def _graphemes(value: str) -> tuple[str, ...]:
    """Return Unicode UAX #29 extended grapheme clusters."""
    return tuple(_GRAPHEME_RE.findall(value))


def _cluster_base(cluster: str) -> str:
    for char in cluster:
        if unicodedata.category(char) not in {"Mn", "Mc", "Me", "Cf"}:
            return char
    return cluster[0]


def _is_cjk_cluster(cluster: str) -> bool:
    return any(_is_cjk_codepoint(char) for char in cluster)


def _is_word_base(cluster: str) -> bool:
    base = _cluster_base(cluster)
    return not _is_cjk_cluster(cluster) and unicodedata.category(base)[:1] in {"L", "N"}


def _is_no_break_token(token: str) -> bool:
    return bool(token) and all(char in _NO_BREAK_SPACES for char in token)


def _tokens(paragraph: str) -> list[str]:
    """Tokenize without splitting words, CJK characters, or grapheme clusters."""
    clusters = _graphemes(paragraph)
    tokens: list[str] = []
    index = 0
    while index < len(clusters):
        cluster = clusters[index]
        if cluster.isspace():
            no_break = _is_no_break_token(cluster)
            end = index + 1
            while (
                end < len(clusters)
                and clusters[end].isspace()
                and _is_no_break_token(clusters[end]) == no_break
            ):
                end += 1
            tokens.append("".join(clusters[index:end]))
            index = end
            continue
        if _is_word_base(cluster):
            end = index + 1
            while end < len(clusters):
                current = clusters[end]
                if _is_word_base(current):
                    end += 1
                    continue
                if (
                    current in _WORD_CONNECTORS
                    and end + 1 < len(clusters)
                    and _is_word_base(clusters[end + 1])
                ):
                    end += 1
                    continue
                break
            tokens.append("".join(clusters[index:end]))
            index = end
            continue
        tokens.append(cluster)
        index += 1

    # NBSP / NNBSP / WORD JOINER bind their nearest textual neighbours into
    # one indivisible token. This still permits wrapping at an earlier normal
    # space (``X A\u00a0B`` -> ``X `` / ``A\u00a0B``).
    merged: list[str] = []
    index = 0
    while index < len(tokens):
        token = tokens[index]
        if _is_no_break_token(token):
            left = ""
            if merged and not merged[-1].isspace():
                left = merged.pop()
            glued = left + token
            index += 1
            while index < len(tokens) and _is_no_break_token(tokens[index]):
                glued += tokens[index]
                index += 1
            if (
                index < len(tokens)
                and not (tokens[index].isspace() and not _is_no_break_token(tokens[index]))
            ):
                glued += tokens[index]
                index += 1
            merged.append(glued)
            continue
        merged.append(token)
        index += 1
    return merged


def _glyph_signature(font: ImageFont.ImageFont, char: str) -> tuple[Any, ...]:
    mask = font.getmask(char, mode="L")
    return mask.size, mask.getbbox(), bytes(mask)


def _ensure_text_coverage(text: str, resolved: _ResolvedFont) -> None:
    """Reject CJK tofu instead of silently measuring the ``.notdef`` box."""
    try:
        missing_signature = _glyph_signature(
            resolved.font, "\u0378"
        )  # permanently unassigned
    except Exception as exc:
        raise TextLayoutError(f"unable to inspect glyph coverage: {exc}") from exc
    for char in dict.fromkeys(text):
        # Do not trust ``unicodedata.category`` here: the runtime's Unicode
        # database can predate a newly assigned CJK extension (for example I),
        # leaving a real ideograph classified as Cn.
        if not _is_cjk_codepoint(char) or char.isspace():
            continue
        try:
            signature = _glyph_signature(resolved.font, char)
        except Exception as exc:
            raise TextLayoutError(
                f"unable to inspect glyph U+{ord(char):04X}: {exc}"
            ) from exc
        if signature == missing_signature:
            raise TextLayoutError(
                f"font {resolved.family!r} face {resolved.style!r} lacks CJK glyph "
                f"U+{ord(char):04X}; configure an explicit CJK-capable font token"
            )


def _validated_width(measure_width: WidthMeasure, text: str) -> float:
    try:
        raw_width = measure_width(text)
    except Exception as exc:
        raise TextLayoutError(f"width measurer failed for {text!r}: {exc}") from exc
    if not isinstance(raw_width, Real) or isinstance(raw_width, bool):
        raise TextLayoutError("width measurer must return a real number")
    width = float(raw_width)
    if not math.isfinite(width) or width < 0:
        raise TextLayoutError("width measurer must return a finite non-negative number")
    return width


def _split_opening_tail(line: str) -> tuple[str, str]:
    body = line
    carry = ""
    while body and body[-1] in _OPENING_PUNCTUATION:
        carry = body[-1] + carry
        body = body[:-1]
    return body, carry


def _finite_number(value: Any, name: str, *, minimum: float | None = None) -> float:
    if not isinstance(value, Real) or isinstance(value, bool):
        raise TextLayoutError(f"{name} must be a real number")
    number = float(value)
    if not math.isfinite(number):
        raise TextLayoutError(f"{name} must be finite")
    if minimum is not None and number <= minimum:
        raise TextLayoutError(f"{name} must be > {minimum:g}")
    return number


def break_lines(
    text: str,
    max_width_px: float,
    *,
    measure_width: WidthMeasure,
) -> tuple[str, ...]:
    """Break text with deterministic CJK/Latin rules and no truncation.

    A single indivisible word or punctuation pair may exceed ``max_width_px``;
    callers detect that through measured layout bounds rather than receiving a
    corrupted word or forbidden punctuation break.
    """
    max_width = _finite_number(max_width_px, "max_width_px", minimum=0)

    output: list[str] = []
    for paragraph in _LINE_BREAK_RE.split(str(text)):
        if paragraph == "":
            output.append("")
            continue

        current = ""
        pending_space = ""
        for token in _tokens(paragraph):
            if token.isspace():
                pending_space += token
                continue

            candidate = current + pending_space + token
            if not current or _validated_width(measure_width, candidate) <= max_width:
                current = candidate
                pending_space = ""
                continue

            if any(char in _NO_BREAK_SPACES for char in pending_space):
                current = candidate
                pending_space = ""
                continue

            # A closer belongs to the preceding line. Slight overflow is more
            # readable than placing Chinese/Latin punctuation at the next start.
            if token[0] in _CLOSING_PUNCTUATION:
                current = candidate
                pending_space = ""
                continue

            line, carry = _split_opening_tail(current)
            if line:
                output.append(line)
                if carry:
                    current = carry + pending_space + token
                else:
                    # Preserve breakable whitespace exactly. It remains at the
                    # prior line end, where it is visually inert but round-trips.
                    output[-1] += pending_space
                    current = token
            else:
                # The current fragment consists only of opening punctuation;
                # keep it attached even if the pair is wider than the slot.
                current = current + pending_space + token
            pending_space = ""

        output.append(current + pending_space)

    return tuple(output) or ("",)


def _wrap_with_font(
    text: str,
    font: ImageFont.ImageFont,
    max_width_px: float,
) -> tuple[str, ...]:
    return break_lines(
        text,
        max_width_px,
        measure_width=lambda value: _measure_with_font(value, font).width_px,
    )


def wrap_text(
    text: str,
    *,
    font_config: Mapping[str, Any],
    size_px: int,
    max_width_px: float,
) -> tuple[str, ...]:
    """Resolve a font and return the canonical line breaks."""
    value = str(text)
    resolved = _resolved_font(font_config, size_px)
    _ensure_text_coverage(value, resolved)
    return _wrap_with_font(value, resolved.font, max_width_px)


def _visible_line(line: str) -> str:
    """Ignore breakable trailing whitespace in bounds, while preserving payload."""
    end = len(line)
    while (
        end > 0
        and line[end - 1].isspace()
        and line[end - 1] not in _NO_BREAK_SPACES
    ):
        end -= 1
    return line[:end]


def _multiline_metrics(
    lines: tuple[str, ...],
    font: ImageFont.ImageFont,
    line_spacing: float,
) -> tuple[int, int, int, tuple[int, ...]]:
    spacing = _finite_number(line_spacing, "line_spacing")
    if spacing < 1.0:
        raise TextLayoutError("line_spacing must be finite and >= 1.0")
    line_metrics = tuple(_measure_with_font(_visible_line(line), font) for line in lines)
    widths = tuple(item.width_px for item in line_metrics)
    base_height = _font_line_height(font)
    line_advance = max(int(math.ceil(base_height * spacing)), base_height)
    height = base_height + max(len(lines) - 1, 0) * line_advance
    return max(widths, default=0), height, line_advance, widths


def autofit_text(
    text: str,
    *,
    font_config: Mapping[str, Any],
    size_steps_px: Sequence[int],
    max_width_px: float,
    max_height_px: float,
    max_lines: int | None = None,
    line_spacing: float = 1.2,
) -> TextLayoutResult:
    """Choose one of exactly two type-scale steps, or report overflow.

    ``size_steps_px`` must contain ``(preferred, fallback)`` with preferred
    strictly larger.  The smallest result is returned when neither fits, with
    one or more stable reasons: ``width``, ``height``, or ``line_count``.
    """
    try:
        steps = tuple(size_steps_px) if not isinstance(size_steps_px, (str, bytes)) else ()
    except TypeError:
        steps = ()
    if (
        len(steps) != 2
        or any(not isinstance(value, int) or isinstance(value, bool) or value <= 0 for value in steps)
        or steps[0] <= steps[1]
    ):
        raise TextLayoutError(
            "size_steps_px must be exactly two positive integers, largest first"
        )
    width_limit = _finite_number(max_width_px, "max_width_px", minimum=0)
    height_limit = _finite_number(max_height_px, "max_height_px", minimum=0)
    if max_lines is not None and (
        not isinstance(max_lines, int) or isinstance(max_lines, bool) or max_lines <= 0
    ):
        raise TextLayoutError("max_lines must be a positive integer when provided")

    selected: TextLayoutResult | None = None
    value = str(text)
    for step_index, raw_size in enumerate(steps):
        size = raw_size
        resolved = _resolved_font(font_config, size)
        _ensure_text_coverage(value, resolved)
        lines = _wrap_with_font(value, resolved.font, width_limit)
        width, height, line_height, widths = _multiline_metrics(
            lines, resolved.font, line_spacing
        )
        reasons: list[str] = []
        if any(value > width_limit for value in widths):
            reasons.append("width")
        if height > height_limit:
            reasons.append("height")
        if max_lines is not None and len(lines) > max_lines:
            reasons.append("line_count")
        selected = TextLayoutResult(
            family=resolved.family,
            path=resolved.path,
            weight=resolved.weight,
            font_style=resolved.style,
            face_index=resolved.face_index,
            size_px=size,
            preferred_size_px=steps[0],
            fallback_size_px=steps[1],
            selected_step="preferred" if step_index == 0 else "fallback",
            lines=lines,
            width_px=width,
            height_px=height,
            line_height_px=line_height,
            overflow=bool(reasons),
            overflow_reasons=tuple(reasons),
        )
        if not reasons:
            return selected

    assert selected is not None
    return selected


__all__ = [
    "TextLayoutError",
    "TextLayoutResult",
    "TextMetrics",
    "autofit_text",
    "break_lines",
    "measure_text",
    "wrap_text",
]
