"""OpenTimelineIO ↔ Gemia project adapter (Timeline v1 M5).

Bidirectional conversion between canonical Gemia project dicts and OTIO Timeline
objects.  All Gemia-specific data that has no native OTIO counterpart is round-
tripped through the ``metadata["lumeri"]`` namespace.

Mapping contract
----------------
Gemia track kind  → OTIO TrackKind / metadata
  video            → TrackKind.Video
  overlay          → TrackKind.Video + metadata["lumeri"]["track_kind"] = "overlay"
  audio            → TrackKind.Audio

Gemia clip        → OTIO Clip
  media_kind != text  → ExternalReference(target_url=source_path)
  media_kind == text  → GeneratorReference(generator_kind="lumeri_text") +
                         metadata["lumeri"]["text_config"]

Round-trip fields stored in metadata["lumeri"] per clip
  clip_id, asset_id, track_id, name, media_kind, enabled,
  effects, transition_after, keyframes, provenance, thumbnails,
  waveform_peaks, summary
"""
from __future__ import annotations

import copy
import mimetypes
import uuid
from pathlib import Path
from typing import Any
from urllib.parse import unquote, urlparse
from urllib.request import url2pathname

import opentimelineio as otio
from opentimelineio import opentime, schema as otio_schema

from gemia.project_model import (
    DEFAULT_FPS,
    DEFAULT_HEIGHT,
    DEFAULT_WIDTH,
    IMAGE_DURATION,
    MEDIA_KINDS,
    TRACK_KINDS,
    _asset_id as _make_asset_id,
    _default_effects,
    _float_or,
    _normalize_timeline_clip,
    _utc_now,
    empty_project,
    normalize_project,
)

# OTIO rational rate used when none can be determined from metadata.
_DEFAULT_RATE = DEFAULT_FPS

_LUMERI_NS = "lumeri"

# Generator kind sentinel used for text clips.
_TEXT_GENERATOR_KIND = "lumeri_text"


def _external_target_url(source_path: str) -> str:
    """Return a bundle-safe URL for an existing local media file."""

    if not source_path:
        return ""
    path = Path(source_path).expanduser()
    if path.is_file():
        return path.resolve().as_uri()
    return source_path


def _source_path_from_target_url(target_url: str) -> str:
    """Convert a local file URL back to the host-native filesystem path."""

    parsed = urlparse(target_url)
    if parsed.scheme.lower() != "file":
        return target_url
    netloc = f"//{parsed.netloc}" if parsed.netloc else ""
    return url2pathname(unquote(f"{netloc}{parsed.path}"))


# ---------------------------------------------------------------------------
# project_to_otio
# ---------------------------------------------------------------------------


def project_to_otio(project: dict[str, Any]) -> otio_schema.Timeline:
    """Convert a canonical Gemia project dict to an OTIO Timeline."""
    project = normalize_project(project)
    timeline_data = project.get("timeline") or {}
    fps = _float_or(timeline_data.get("fps"), _DEFAULT_RATE)
    rate = fps if fps > 0 else _DEFAULT_RATE

    tl = otio_schema.Timeline(
        name=str(project.get("title") or "Untitled Project"),
        metadata={
            _LUMERI_NS: {
                "project_id": project.get("project_id"),
                "account_id": project.get("account_id"),
                "width": timeline_data.get("width", DEFAULT_WIDTH),
                "height": timeline_data.get("height", DEFAULT_HEIGHT),
                "fps": rate,
                "render_settings": project.get("render_settings"),
            }
        },
    )

    # Build asset lookup by id for ExternalReference URLs.
    assets: dict[str, dict[str, Any]] = {
        str(a.get("id") or ""): a
        for a in (project.get("assets") or [])
        if isinstance(a, dict)
    }

    # Group clips by track_id preserving insertion order.
    clips_by_track: dict[str, list[dict[str, Any]]] = {}
    for clip in (timeline_data.get("clips") or []):
        if not isinstance(clip, dict):
            continue
        tid = str(clip.get("track_id") or "V1")
        clips_by_track.setdefault(tid, []).append(clip)

    # Build tracks from the canonical tracks list (preserves order).
    tracks = timeline_data.get("tracks") or []
    for track_def in tracks:
        if not isinstance(track_def, dict):
            continue
        tid = str(track_def.get("id") or "")
        kind_str = str(track_def.get("kind") or "video")
        otio_kind = (
            otio_schema.TrackKind.Audio
            if kind_str == "audio"
            else otio_schema.TrackKind.Video
        )
        track = otio_schema.Track(
            name=str(track_def.get("name") or tid),
            kind=otio_kind,
            metadata={
                _LUMERI_NS: {
                    "track_id": tid,
                    "track_kind": kind_str,
                    "locked": bool(track_def.get("locked", False)),
                    "muted": bool(track_def.get("muted", False)),
                    "index": track_def.get("index", 0),
                    "duck_under": track_def.get("duck_under"),
                }
            },
        )

        track_clips = clips_by_track.get(tid) or []
        # Sort by start time so OTIO ordering is canonical.
        track_clips = sorted(track_clips, key=lambda c: _float_or(c.get("start"), 0.0))

        cursor = 0.0
        for clip in track_clips:
            clip_start = _float_or(clip.get("start"), 0.0)
            clip_dur = _float_or(clip.get("duration"), 0.0)
            # Insert a gap if there is dead space before this clip.
            gap_dur = clip_start - cursor
            if gap_dur > 1e-6:
                gap_rt = _secs_to_rt(gap_dur, rate)
                track.append(
                    otio_schema.Gap(
                        source_range=opentime.TimeRange(
                            start_time=opentime.RationalTime(0.0, rate),
                            duration=gap_rt,
                        )
                    )
                )
            track.append(_clip_to_otio(clip, assets, rate))
            cursor = clip_start + clip_dur

        tl.tracks.append(track)

    # Markers → OTIO global markers.
    for m in (timeline_data.get("markers") or []):
        if not isinstance(m, dict):
            continue
        tl.tracks.markers.append(
            otio_schema.Marker(
                name=str(m.get("label") or ""),
                marked_range=opentime.TimeRange(
                    start_time=_secs_to_rt(_float_or(m.get("time"), 0.0), rate),
                    duration=opentime.RationalTime(0.0, rate),
                ),
                color=_marker_color(m.get("color")),
                metadata={_LUMERI_NS: {"id": m.get("id"), "note": m.get("note")}},
            )
        )

    return tl


def _clip_to_otio(
    clip: dict[str, Any], assets: dict[str, dict[str, Any]], rate: float
) -> otio_schema.Clip:
    """Convert a single canonical timeline clip to an OTIO Clip."""
    media_kind = str(clip.get("media_kind") or "video")
    clip_dur = _float_or(clip.get("duration"), 0.0)
    source_in = _float_or(clip.get("source_in"), 0.0)
    source_out = _float_or(clip.get("source_out"), source_in + clip_dur)

    available_range = opentime.TimeRange(
        start_time=_secs_to_rt(source_in, rate),
        duration=_secs_to_rt(source_out - source_in, rate),
    )
    source_range = opentime.TimeRange(
        start_time=_secs_to_rt(source_in, rate),
        duration=_secs_to_rt(clip_dur, rate),
    )

    if media_kind == "text":
        media_ref: otio_schema.MediaReference = otio_schema.GeneratorReference(
            generator_kind=_TEXT_GENERATOR_KIND,
            available_range=available_range,
        )
    else:
        asset = assets.get(str(clip.get("asset_id") or "")) or {}
        source_path = str(asset.get("source_path") or "")
        media_ref = otio_schema.ExternalReference(
            target_url=_external_target_url(source_path),
            available_range=available_range,
        )

    lumeri_meta: dict[str, Any] = {
        "clip_id": clip.get("id"),
        "asset_id": clip.get("asset_id"),
        "track_id": clip.get("track_id"),
        "name": clip.get("name"),
        "media_kind": media_kind,
        "enabled": clip.get("enabled", True),
    }
    # Optional rich fields — only persist when non-empty.
    for key in ("effects", "transition_after", "keyframes", "provenance", "thumbnails", "waveform_peaks", "summary", "text_config"):
        val = clip.get(key)
        if val is not None and val != [] and val != {}:
            lumeri_meta[key] = val

    return otio_schema.Clip(
        name=str(clip.get("name") or "clip"),
        media_reference=media_ref,
        source_range=source_range,
        metadata={_LUMERI_NS: lumeri_meta},
    )


# ---------------------------------------------------------------------------
# otio_to_project
# ---------------------------------------------------------------------------


def otio_to_project(tl: otio_schema.Timeline, *, account_id: str | None = None) -> dict[str, Any]:
    """Convert an OTIO Timeline to a canonical Gemia project dict."""
    tl_meta = _lumeri(tl.metadata)
    fps = _float_or(tl_meta.get("fps"), _DEFAULT_RATE)
    rate = fps if fps > 0 else _DEFAULT_RATE

    project = empty_project(
        account_id=account_id or tl_meta.get("account_id"),
        title=str(tl.name or "Untitled Project"),
    )
    if tl_meta.get("project_id"):
        project["project_id"] = str(tl_meta["project_id"])

    project["timeline"]["fps"] = rate
    project["timeline"]["width"] = int(tl_meta.get("width") or DEFAULT_WIDTH)
    project["timeline"]["height"] = int(tl_meta.get("height") or DEFAULT_HEIGHT)

    if isinstance(tl_meta.get("render_settings"), dict):
        project["render_settings"].update(tl_meta["render_settings"])

    otio_tracks = list(tl.tracks)
    gemia_tracks: list[dict[str, Any]] = []
    gemia_clips: list[dict[str, Any]] = []
    assets_by_key: dict[str, dict[str, Any]] = {}

    for otio_track in otio_tracks:
        if not isinstance(otio_track, otio_schema.Track):
            continue
        track_meta = _lumeri(otio_track.metadata)
        track_id = str(track_meta.get("track_id") or otio_track.name or f"V{len(gemia_tracks)+1}")
        # Restore the original Gemia kind (overlay / video / audio).
        kind_str = str(track_meta.get("track_kind") or "")
        if kind_str not in TRACK_KINDS:
            kind_str = "audio" if otio_track.kind == otio_schema.TrackKind.Audio else "video"
        gemia_tracks.append(
            {
                "id": track_id,
                "kind": kind_str,
                "name": str(otio_track.name or track_id),
                "index": int(_float_or(track_meta.get("index"), len(gemia_tracks))),
                "locked": bool(track_meta.get("locked", False)),
                "muted": bool(track_meta.get("muted", False)),
                "duck_under": str(track_meta.get("duck_under")) if track_meta.get("duck_under") else None,
            }
        )

        # Walk the track items (Clips + Gaps).
        cursor = 0.0
        for item in otio_track:
            item_range = item.source_range
            if item_range is not None:
                item_dur = item_range.duration.to_seconds()
            else:
                item_dur = 0.0

            if isinstance(item, otio_schema.Gap):
                cursor += item_dur
                continue

            if not isinstance(item, otio_schema.Clip):
                cursor += item_dur
                continue

            clip_meta = _lumeri(item.metadata)
            media_kind = str(clip_meta.get("media_kind") or "video")
            if media_kind not in MEDIA_KINDS:
                media_kind = "video"

            clip_id = str(clip_meta.get("clip_id") or f"clip_{uuid.uuid4().hex[:8]}")
            asset_id = str(clip_meta.get("asset_id") or "")
            name = str(clip_meta.get("name") or item.name or "clip")

            # Compute source_in / source_out from the OTIO source_range.
            if item_range is not None:
                source_in_s = item_range.start_time.to_seconds()
                clip_dur_s = item_range.duration.to_seconds()
                source_out_s = source_in_s + clip_dur_s
            else:
                source_in_s = 0.0
                clip_dur_s = item_dur
                source_out_s = source_in_s + clip_dur_s

            # Rebuild asset from ExternalReference.
            if isinstance(item.media_reference, otio_schema.ExternalReference):
                source_path = _source_path_from_target_url(
                    str(item.media_reference.target_url or "")
                )
                if not asset_id:
                    asset_id = _make_asset_id(source_path or name)
                if asset_id not in assets_by_key:
                    avail = item.media_reference.available_range
                    avail_dur = avail.duration.to_seconds() if avail else clip_dur_s
                    assets_by_key[asset_id] = _build_asset(
                        asset_id=asset_id,
                        name=name,
                        source_path=source_path,
                        media_kind=media_kind,
                        duration=avail_dur,
                    )
            elif isinstance(item.media_reference, otio_schema.GeneratorReference):
                if not asset_id:
                    asset_id = _make_asset_id(f"text_{clip_id}")

            gemia_clip: dict[str, Any] = {
                "id": clip_id,
                "asset_id": asset_id,
                "track_id": track_id,
                "name": name,
                "media_kind": media_kind,
                "start": round(cursor, 6),
                "duration": round(clip_dur_s, 6),
                "source_in": round(source_in_s, 6),
                "source_out": round(source_out_s, 6),
                "enabled": bool(clip_meta.get("enabled", True)),
                "effects": clip_meta.get("effects") or _default_effects(),
                "transition_after": clip_meta.get("transition_after"),
                "keyframes": clip_meta.get("keyframes") or [],
                "provenance": clip_meta.get("provenance"),
                "thumbnails": clip_meta.get("thumbnails") or [],
                "waveform_peaks": clip_meta.get("waveform_peaks") or [],
                "summary": clip_meta.get("summary"),
                "text_config": clip_meta.get("text_config") if media_kind == "text" else None,
            }
            gemia_clips.append(gemia_clip)
            cursor += clip_dur_s

    # Restore global markers.
    gemia_markers: list[dict[str, Any]] = []
    for m in (tl.tracks.markers or []):
        if not isinstance(m, otio_schema.Marker):
            continue
        m_meta = _lumeri(m.metadata)
        gemia_markers.append(
            {
                "id": str(m_meta.get("id") or f"marker_{uuid.uuid4().hex[:8]}"),
                "time": round(m.marked_range.start_time.to_seconds(), 6),
                "label": str(m.name or ""),
                "color": _otio_color_to_hex(m.color),
                "note": m_meta.get("note"),
            }
        )

    project["assets"] = list(assets_by_key.values())
    project["timeline"]["tracks"] = gemia_tracks or _default_tracks_fallback()
    project["timeline"]["clips"] = [_normalize_timeline_clip(c) for c in gemia_clips]
    project["timeline"]["markers"] = gemia_markers

    # Recompute duration.
    end = 0.0
    for c in project["timeline"]["clips"]:
        end = max(end, _float_or(c.get("start"), 0.0) + _float_or(c.get("duration"), 0.0))
    project["timeline"]["duration"] = round(end, 6)

    return project


# ---------------------------------------------------------------------------
# Convenience: serialize / deserialize to otio_json string
# ---------------------------------------------------------------------------


def project_to_otio_json(project: dict[str, Any]) -> str:
    """Serialize a Gemia project to an OTIO JSON string."""
    tl = project_to_otio(project)
    return otio.adapters.write_to_string(tl, adapter_name="otio_json")


def otio_json_to_project(otio_json: str, *, account_id: str | None = None) -> dict[str, Any]:
    """Deserialize an OTIO JSON string to a canonical Gemia project dict."""
    tl = otio.adapters.read_from_string(otio_json, adapter_name="otio_json")
    return otio_to_project(tl, account_id=account_id)


# ---------------------------------------------------------------------------
# Interchange file formats (M8): route to OTIO file adapters
# ---------------------------------------------------------------------------

# Stable Lumeri format token -> (OTIO adapter name, file extension).
_FORMAT_ADAPTERS: dict[str, tuple[str, str]] = {
    "otio": ("otio_json", ".otio"),
    "otioz": ("otioz", ".otioz"),
    "otiod": ("otiod", ".otiod"),
    "edl": ("cmx_3600", ".edl"),
    "fcp7": ("fcp_xml", ".xml"),
    "fcpx": ("fcpx_xml", ".fcpxml"),
}

# Non-core adapters ship as optional pip plugins (the `interop` extra). Names
# are used purely for honest, actionable "not installed" errors.
_ADAPTER_PACKAGES: dict[str, str] = {
    "cmx_3600": "otio-cmx3600-adapter",
    "fcp_xml": "otio-fcp-adapter",
    "fcpx_xml": "otio-fcpx-xml-adapter",
}

# Lossy interchange formats — callers should flag reduced fidelity.
LOSSY_FORMATS = frozenset({"edl", "fcp7", "fcpx"})


class OtioFormatError(ValueError):
    """Unknown interchange format token, or a required OTIO adapter is missing."""


def format_extension(fmt: str) -> str:
    try:
        return _FORMAT_ADAPTERS[fmt][1]
    except KeyError:
        raise OtioFormatError(
            f"unknown OTIO format {fmt!r}; valid: {', '.join(sorted(_FORMAT_ADAPTERS))}"
        ) from None


def available_formats() -> list[str]:
    """Format tokens whose underlying OTIO adapter is importable right now."""
    have = set(otio.adapters.available_adapter_names())
    return [fmt for fmt, (adapter, _ext) in _FORMAT_ADAPTERS.items() if adapter in have]


def _adapter_for(fmt: str) -> str:
    entry = _FORMAT_ADAPTERS.get(fmt)
    if entry is None:
        raise OtioFormatError(
            f"unknown OTIO format {fmt!r}; valid: {', '.join(sorted(_FORMAT_ADAPTERS))}"
        )
    adapter_name = entry[0]
    if adapter_name not in set(otio.adapters.available_adapter_names()):
        pkg = _ADAPTER_PACKAGES.get(adapter_name)
        hint = f"; install the optional plugin `pip install {pkg}`" if pkg else ""
        raise OtioFormatError(
            f"{fmt!r} needs the OTIO adapter {adapter_name!r}, which is not installed{hint} "
            f"(or `pip install lumeri[interop]`)"
        )
    return adapter_name


def _simplify_for_lossy(project: dict[str, Any], fmt: str) -> dict[str, Any]:
    """Reduce a project to what a lossy NLE adapter can represent — defined,
    non-crashing degradation (the OTIO cmx_3600/fcp adapters raise on richer
    input).

    - edl  (cmx_3600): a single video track of cuts; overlay/audio/text dropped.
    - fcp7 (fcp_xml) / fcpx (fcpx_xml): drop text/generator clips (they have no
      ExternalReference); keep media (video/audio/image) clips and their tracks.
    """
    project = copy.deepcopy(normalize_project(project))
    timeline = project["timeline"]
    clips = timeline.get("clips") or []
    tracks = timeline.get("tracks") or []
    if fmt == "edl":
        video_tracks = [t for t in tracks if str(t.get("kind")) == "video"]
        primary = str(video_tracks[0]["id"]) if video_tracks else "V1"
        timeline["clips"] = [
            c for c in clips
            if str(c.get("media_kind")) == "video" and str(c.get("track_id")) == primary
        ]
        timeline["tracks"] = [t for t in tracks if str(t.get("id")) == primary]
    else:  # fcp7 / fcpx
        timeline["clips"] = [c for c in clips if str(c.get("media_kind")) != "text"]
        used = {str(c.get("track_id")) for c in timeline["clips"]}
        timeline["tracks"] = [t for t in tracks if str(t.get("id")) in used]
    return project


def write_project_to_file(project: dict[str, Any], path: str | Path, fmt: str = "otio") -> str:
    """Write a Gemia project to ``path`` in interchange format ``fmt``.

    otioz/otiod bundle the referenced media; media that is missing or not a
    local file is dropped (MissingIfNotFile) rather than crashing the export.
    Lossy formats (edl/fcp7/fcpx) are pre-simplified to what their adapter can
    represent (see _simplify_for_lossy) so export degrades instead of crashing.
    Raises OtioFormatError for unknown tokens or uninstalled adapters.
    """
    adapter_name = _adapter_for(fmt)
    src = _simplify_for_lossy(project, fmt) if fmt in LOSSY_FORMATS else project
    tl = project_to_otio(src)
    kwargs: dict[str, Any] = {}
    if adapter_name in {"otioz", "otiod"}:
        from opentimelineio.adapters.otioz import utils as _bundle_utils
        kwargs["media_policy"] = _bundle_utils.MediaReferencePolicy.MissingIfNotFile
    otio.adapters.write_to_file(tl, str(path), adapter_name=adapter_name, **kwargs)
    return str(path)


def read_project_from_file(
    path: str | Path, fmt: str = "otio", *, account_id: str | None = None
) -> dict[str, Any]:
    """Read an interchange file in format ``fmt`` into a canonical project dict."""
    adapter_name = _adapter_for(fmt)
    tl = otio.adapters.read_from_file(str(path), adapter_name=adapter_name)
    return otio_to_project(tl, account_id=account_id)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _to_python(obj: Any) -> Any:
    """Recursively convert OTIO AnyDictionary/AnyVector to plain Python types."""
    try:
        from opentimelineio._otio import AnyDictionary, AnyVector
        if isinstance(obj, AnyDictionary):
            return {str(k): _to_python(v) for k, v in obj.items()}
        if isinstance(obj, AnyVector):
            return [_to_python(v) for v in obj]
    except ImportError:
        pass
    if isinstance(obj, dict):
        return {str(k): _to_python(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_to_python(v) for v in obj]
    return obj


def _lumeri(metadata: Any) -> dict[str, Any]:
    if metadata is None:
        return {}
    try:
        val = metadata.get(_LUMERI_NS)
    except (AttributeError, TypeError):
        return {}
    if val is None:
        return {}
    converted = _to_python(val)
    return converted if isinstance(converted, dict) else {}


def _secs_to_rt(seconds: float, rate: float) -> opentime.RationalTime:
    return opentime.RationalTime.from_seconds(seconds, rate)


def _build_asset(
    *,
    asset_id: str,
    name: str,
    source_path: str,
    media_kind: str,
    duration: float,
) -> dict[str, Any]:
    mime_type = mimetypes.guess_type(name)[0] or ""
    return {
        "id": asset_id,
        "asset_id": asset_id,
        "name": name,
        "media_kind": media_kind,
        "mime_type": mime_type,
        "source_path": source_path,
        "preview_src": None,
        "duration": duration if media_kind != "image" else IMAGE_DURATION,
        "metadata": {},
        "thumbnail_src": None,
        "thumbnails": [],
        "waveform_peaks": [],
        "fingerprint": None,
        "status": "ready",
        "created_at": _utc_now(),
    }


def _default_tracks_fallback() -> list[dict[str, Any]]:
    return [
        {"id": "V1", "kind": "video", "name": "Video 1", "index": 0, "locked": False, "muted": False, "duck_under": None},
        {"id": "A1", "kind": "audio", "name": "Audio 1", "index": 1, "locked": False, "muted": False, "duck_under": None},
    ]


# OTIO marker colors: keep a small subset that OTIO schema.Marker accepts.
_HEX_TO_OTIO: dict[str, str] = {
    "#ff0000": otio_schema.MarkerColor.RED,
    "#00ff00": otio_schema.MarkerColor.GREEN,
    "#0000ff": otio_schema.MarkerColor.BLUE,
    "#ffff00": otio_schema.MarkerColor.YELLOW,
    "#ff8000": otio_schema.MarkerColor.ORANGE,
    "#ff00ff": otio_schema.MarkerColor.MAGENTA,
    "#00ffff": otio_schema.MarkerColor.CYAN,
    "#ffffff": otio_schema.MarkerColor.WHITE,
    "#000000": otio_schema.MarkerColor.BLACK,
}
_OTIO_TO_HEX: dict[str, str] = {v: k for k, v in _HEX_TO_OTIO.items()}


def _marker_color(color: Any) -> str:
    if isinstance(color, str):
        return _HEX_TO_OTIO.get(color.lower(), otio_schema.MarkerColor.RED)
    return otio_schema.MarkerColor.RED


def _otio_color_to_hex(color: Any) -> str:
    return _OTIO_TO_HEX.get(str(color), "#ff0000")


__all__ = [
    "otio_json_to_project",
    "otio_to_project",
    "project_to_otio",
    "project_to_otio_json",
    "write_project_to_file",
    "read_project_from_file",
    "available_formats",
    "format_extension",
    "LOSSY_FORMATS",
    "OtioFormatError",
]
