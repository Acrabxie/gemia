"""Pure quanta-to-frame materialization and static-pager manifest helpers."""
from __future__ import annotations

from dataclasses import dataclass
import re
from typing import Any, Mapping, Sequence
from urllib.parse import urlencode

from gemia.quanta.layout import QuantaLayoutError, layout_slide
from gemia.quanta.raster import QuantaRasterError, rasterize_slide
from gemia.quanta.traverse import flat_view, is_tree_doc


class QuantaMaterializeError(ValueError):
    """Raised when a quanta cannot deterministically become build-state PNGs."""


_ID_RE = re.compile(r"^[A-Za-z0-9_-]{1,64}$")
_SESSION_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$")


@dataclass(frozen=True)
class RenderedQuantaFrame:
    scope_index: int
    state_index: int
    scope_id: str
    state_id: str
    dwell_sec: float
    png_bytes: bytes
    placed_slide: dict[str, Any]
    source_asset_ids: tuple[str, ...]

    @property
    def overflow(self) -> tuple[dict[str, Any], ...]:
        raw = self.placed_slide.get("overflow") or []
        return tuple(dict(item) for item in raw if isinstance(item, Mapping))

    def manifest_entry(self, asset_id: str) -> dict[str, Any]:
        asset = _asset_id(asset_id)
        return {
            "scope_index": self.scope_index,
            "state_index": self.state_index,
            "scope_id": self.scope_id,
            "state_id": self.state_id,
            "dwell_sec": self.dwell_sec,
            "asset_id": asset,
            "source_asset_ids": list(self.source_asset_ids),
            "overflow": [dict(item) for item in self.overflow],
        }


def _asset_id(value: Any) -> str:
    text = str(value or "").strip()
    if not _ID_RE.fullmatch(text):
        raise QuantaMaterializeError(f"invalid pager asset id {text!r}")
    return text


def _session_id(value: Any) -> str:
    text = str(value or "").strip()
    if not _SESSION_RE.fullmatch(text):
        raise QuantaMaterializeError(f"invalid pager session id {text!r}")
    return text


def _ordered_slides(quanta: Mapping[str, Any]) -> list[Mapping[str, Any]]:
    raw_slides = quanta.get("slides")
    if not isinstance(raw_slides, list) or any(not isinstance(item, Mapping) for item in raw_slides):
        raise QuantaMaterializeError("quanta.slides must be a list of slide mappings")
    slides = list(raw_slides)
    by_id: dict[str, Mapping[str, Any]] = {}
    for slide in slides:
        slide_id = str(slide.get("id") or "").strip()
        if not slide_id:
            raise QuantaMaterializeError("every slide must have a non-empty id")
        if slide_id in by_id:
            raise QuantaMaterializeError(f"duplicate slide id {slide_id!r}")
        by_id[slide_id] = slide
    raw_path = quanta.get("default_path")
    path = [str(value or "").strip() for value in raw_path] if isinstance(raw_path, list) else []
    if not path:
        path = list(by_id)
    if len(path) != len(by_id) or len(set(path)) != len(path) or set(path) != set(by_id):
        raise QuantaMaterializeError("quanta.default_path must cover every slide exactly once")
    return [by_id[slide_id] for slide_id in path]


def _build_ids(slide: Mapping[str, Any]) -> list[str]:
    raw_builds = slide.get("builds")
    builds = [item for item in raw_builds if isinstance(item, Mapping)] if isinstance(raw_builds, list) else []
    if not builds:
        return [""]
    result: list[str] = []
    for build in builds:
        build_id = str(build.get("id") or "").strip()
        if not build_id:
            raise QuantaMaterializeError(f"slide {slide.get('id')!r} has a build without an id")
        if build_id in result:
            raise QuantaMaterializeError(f"slide {slide.get('id')!r} has duplicate build id {build_id!r}")
        result.append(build_id)
    return result


def render_quanta_frames(
    quanta: Mapping[str, Any],
    *,
    image_sources: Mapping[str, bytes | bytearray | memoryview] | None = None,
    scale: int = 1,
    fail_on_overflow: bool = False,
) -> tuple[RenderedQuantaFrame, ...]:
    """Render every state in walk order without filesystem access.

    Consumes the canonical FLAT VIEW (``gemia.quanta.traverse.flat_view`` of
    the state tree, or any canonical v1-shaped quanta): the tree stays the
    source of truth one layer up; geometry solves once per content scope and
    states only filter visibility.
    """
    if not isinstance(quanta, Mapping):
        raise QuantaMaterializeError("quanta must be a mapping")
    if is_tree_doc(quanta):
        quanta = flat_view(quanta)
    theme = quanta.get("theme")
    theme = theme if isinstance(theme, Mapping) else {}
    tokens = theme.get("tokens")
    if tokens is not None and not isinstance(tokens, Mapping):
        raise QuantaMaterializeError("quanta.theme.tokens must be a mapping")
    sources = image_sources or {}
    frames: list[RenderedQuantaFrame] = []
    try:
        for slide_index, slide in enumerate(_ordered_slides(quanta)):
            slide_id = str(slide.get("id"))
            for build_index, build_id in enumerate(_build_ids(slide)):
                placed = layout_slide(
                    slide,
                    theme_tokens=tokens,
                    build_id=build_id or None,
                )
                if fail_on_overflow and placed.get("overflow"):
                    raise QuantaMaterializeError(
                        f"slide {slide_id} build {placed.get('build_id')} overflowed; refine the copy"
                    )
                source_ids = tuple(dict.fromkeys(
                    str(item.get("asset_id"))
                    for item in placed.get("placed_blocks") or []
                    if isinstance(item, Mapping)
                    and item.get("kind") == "image"
                    and item.get("asset_id")
                ))
                png = rasterize_slide(placed, image_sources=sources, scale=scale)
                frames.append(RenderedQuantaFrame(
                    scope_index=slide_index,
                    state_index=build_index,
                    scope_id=slide_id,
                    state_id=str(placed.get("build_id") or "b1"),
                    dwell_sec=float(placed.get("dwell_sec") or 0.0),
                    png_bytes=png,
                    placed_slide=placed,
                    source_asset_ids=source_ids,
                ))
    except QuantaMaterializeError:
        raise
    except (QuantaLayoutError, QuantaRasterError) as exc:
        raise QuantaMaterializeError(str(exc)) from exc
    return tuple(frames)


def build_quanta_pager_url(
    session_id: str,
    frames: Sequence[RenderedQuantaFrame],
    frame_asset_ids: Sequence[str],
    *,
    first_build_only: bool = False,
) -> str:
    """Build the only URL shape accepted by ``static/v3/quanta.js``."""
    if len(frames) != len(frame_asset_ids):
        raise QuantaMaterializeError("frame_asset_ids must match rendered frames one-for-one")
    entries = [
        {
            "scope_index": frame.scope_index,
            "state_index": frame.state_index,
            "asset_id": asset_id,
        }
        for frame, asset_id in zip(frames, frame_asset_ids)
    ]
    return build_quanta_pager_url_from_manifest(
        session_id, entries, first_build_only=first_build_only
    )


def build_quanta_pager_url_from_manifest(
    session_id: str,
    frames: Sequence[Mapping[str, Any]],
    *,
    first_build_only: bool = False,
) -> str:
    """Build the pager URL from a registered-frame manifest.

    The manifest form lets single-slide refinement reuse unchanged registered
    frames without retaining raster bytes or private renderer objects in the
    session cache.
    """
    session = _session_id(session_id)
    try:
        pairs = [
            (
                int(frame.get("scope_index", -1)),
                int(frame.get("state_index", -1)),
                _asset_id(frame.get("asset_id")),
            )
            for frame in frames
            if isinstance(frame, Mapping)
        ]
    except QuantaMaterializeError:
        raise
    except (TypeError, ValueError) as exc:
        raise QuantaMaterializeError("pager frame indices must be integers") from exc
    if len(pairs) != len(frames):
        raise QuantaMaterializeError("pager frame manifest must contain mappings")
    if any(slide_index < 0 or build_index < 0 for slide_index, build_index, _ in pairs):
        raise QuantaMaterializeError("pager frame indices must be non-negative")
    if first_build_only:
        pairs = [item for item in pairs if item[1] == 0]
    if len(pairs) > 512:
        raise QuantaMaterializeError("pager supports at most 512 frames")
    params: list[tuple[str, str]] = [("session_id", session)]
    params.extend(
        ("frame", f"{slide_index}:{build_index}:{asset_id}")
        for slide_index, build_index, asset_id in pairs
    )
    return "/video/quanta.html?" + urlencode(params)


__all__ = [
    "QuantaMaterializeError",
    "RenderedQuantaFrame",
    "build_quanta_pager_url",
    "build_quanta_pager_url_from_manifest",
    "render_quanta_frames",
]
