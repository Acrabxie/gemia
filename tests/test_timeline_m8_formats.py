"""M8 regression: OTIO interchange file formats.

Two concerns:
  * M8-C — otioz/otiod bundles actually contain the referenced media, and a
    missing/non-file media reference is dropped (MissingIfNotFile) rather than
    crashing the export.
  * M8-D — fidelity matrix: every available adapter round-trips a non-trivial
    project to a file and OTIO reads it back; lossless formats preserve clip
    count/order/timing + lumeri metadata (incl. duck_under); lossy formats
    degrade gracefully (no crash, cut points survive). EDL/FCP tests SKIP when
    the optional `interop` plugins are absent, so the suite stays green on a
    clean machine.
"""
from __future__ import annotations

import subprocess
import zipfile
from pathlib import Path
from typing import Any

import opentimelineio as otio
import pytest

from gemia.project_model import empty_project, normalize_project
from lumerai.otio_adapter import (
    _external_target_url,
    _source_path_from_target_url,
    available_formats,
    format_extension,
    read_project_from_file,
    write_project_to_file,
)


# ── helpers ──────────────────────────────────────────────────────────────────


def _real_video(tmp_path: Path, name: str, duration: float = 2.0) -> Path:
    out = tmp_path / name
    subprocess.run(
        ["ffmpeg", "-y", "-f", "lavfi",
         "-i", f"testsrc2=duration={duration}:size=128x128:rate=15",
         "-c:v", "libx264", "-pix_fmt", "yuv420p", str(out)],
        capture_output=True, check=True,
    )
    return out


def _video_asset(asset_id: str, path: Path, duration: float = 2.0) -> dict[str, Any]:
    return {
        "id": asset_id, "asset_id": asset_id, "name": path.name,
        "media_kind": "video", "mime_type": "video/mp4",
        "source_path": str(path), "duration": duration, "metadata": {},
    }


def _video_clip(clip_id: str, asset_id: str, name: str, *, start: float, duration: float,
                source_in: float = 0.0, track_id: str = "V1") -> dict[str, Any]:
    return {
        "id": clip_id, "asset_id": asset_id, "track_id": track_id, "name": name,
        "media_kind": "video", "start": start, "duration": duration,
        "source_in": source_in, "source_out": source_in + duration, "enabled": True,
    }


def _single_video_project(media: Path) -> dict[str, Any]:
    p = empty_project(title="Bundle")
    p["assets"] = [_video_asset("v1", media)]
    p["timeline"]["clips"] = [_video_clip("c1", "v1", media.name, start=0.0, duration=2.0)]
    return normalize_project(p)


# ── M8-C: media bundles (otioz / otiod) ─────────────────────────────────────


def test_existing_media_uses_file_url_and_roundtrips(tmp_path: Path) -> None:
    media = tmp_path / "clip with spaces.mp4"
    media.write_bytes(b"media")

    target_url = _external_target_url(str(media))

    assert target_url.startswith("file:")
    assert Path(_source_path_from_target_url(target_url)) == media.resolve()


def test_otioz_bundles_referenced_media(tmp_path: Path) -> None:
    media = _real_video(tmp_path, "clip.mp4")
    out = tmp_path / "bundle.otioz"
    write_project_to_file(_single_video_project(media), out, "otioz")

    assert out.exists()
    with zipfile.ZipFile(out) as zf:
        names = zf.namelist()
    assert "content.otio" in names
    assert any(n.endswith(".mp4") for n in names), f"media not bundled: {names}"


def test_otiod_bundles_referenced_media(tmp_path: Path) -> None:
    media = _real_video(tmp_path, "clip.mp4")
    out = tmp_path / "bundle.otiod"
    write_project_to_file(_single_video_project(media), out, "otiod")

    assert out.is_dir()
    files = [str(p.relative_to(out)) for p in out.rglob("*") if p.is_file()]
    assert any(f.endswith("content.otio") for f in files)
    assert any(f.endswith(".mp4") for f in files), f"media not bundled: {files}"


def test_otioz_missing_media_is_graceful(tmp_path: Path) -> None:
    """A clip whose source file does not exist must not crash the bundle export;
    the bundle is written without that media (MissingIfNotFile)."""
    project = _single_video_project(tmp_path / "does_not_exist.mp4")
    out = tmp_path / "bundle_missing.otioz"
    write_project_to_file(project, out, "otioz")  # must not raise

    assert out.exists()
    with zipfile.ZipFile(out) as zf:
        names = zf.namelist()
    assert "content.otio" in names
    assert not any(n.endswith(".mp4") for n in names)


# ── M8-D: fidelity matrix ────────────────────────────────────────────────────


def _real_audio(tmp_path: Path, name: str, duration: float = 4.0, freq: int = 330) -> Path:
    out = tmp_path / name
    subprocess.run(
        ["ffmpeg", "-y", "-f", "lavfi",
         "-i", f"sine=frequency={freq}:duration={duration}", str(out)],
        capture_output=True, check=True,
    )
    return out


def _rich_project(tmp_path: Path) -> dict[str, Any]:
    """Multi video clip + text overlay + music(duck_under voice) + voice."""
    v1, v2 = _real_video(tmp_path, "a.mp4", 2.0), _real_video(tmp_path, "b.mp4", 2.0)
    music, voice = _real_audio(tmp_path, "music.wav", 4.0), _real_audio(tmp_path, "voice.wav", 2.0, freq=880)
    p = empty_project(title="Rich")
    p["timeline"]["tracks"] = [
        {"id": "V1", "kind": "video", "name": "V1", "index": 0, "locked": False, "muted": False, "duck_under": None},
        {"id": "OV1", "kind": "overlay", "name": "OV1", "index": 1, "locked": False, "muted": False, "duck_under": None},
        {"id": "A1", "kind": "audio", "name": "Music", "index": 2, "locked": False, "muted": False, "duck_under": "A2"},
        {"id": "A2", "kind": "audio", "name": "Voice", "index": 3, "locked": False, "muted": False, "duck_under": None},
    ]
    p["assets"] = [
        _video_asset("v1", v1), _video_asset("v2", v2),
        {"id": "mus", "asset_id": "mus", "name": "music.wav", "media_kind": "audio", "source_path": str(music), "duration": 4.0, "metadata": {}},
        {"id": "voc", "asset_id": "voc", "name": "voice.wav", "media_kind": "audio", "source_path": str(voice), "duration": 2.0, "metadata": {}},
    ]
    p["timeline"]["clips"] = [
        _video_clip("c1", "v1", "a.mp4", start=0.0, duration=2.0),
        _video_clip("c2", "v2", "b.mp4", start=2.0, duration=2.0),
        {"id": "t1", "asset_id": "", "track_id": "OV1", "media_kind": "text", "start": 0.5, "duration": 1.5,
         "source_in": 0.0, "source_out": 1.5, "enabled": True,
         "text_config": {"content": "Hi", "font_size": 48.0, "color": "#ffffff", "position": None, "align": "center"}},
        {"id": "m1", "asset_id": "mus", "track_id": "A1", "media_kind": "audio", "start": 0.0, "duration": 4.0,
         "source_in": 0.0, "source_out": 4.0, "enabled": True, "effects": {"gain_db": -3.0, "fade_in": 0.5, "fade_out": 0.5}},
        {"id": "vo1", "asset_id": "voc", "track_id": "A2", "media_kind": "audio", "start": 1.0, "duration": 2.0,
         "source_in": 0.0, "source_out": 2.0, "enabled": True},
    ]
    return normalize_project(p)


_ALL_FORMATS = ["otio", "otioz", "otiod", "edl", "fcp7", "fcpx"]
_LOSSLESS = ["otio", "otioz", "otiod"]
_LOSSY = ["edl", "fcp7", "fcpx"]


def _skip_if_absent(fmt: str) -> None:
    if fmt not in available_formats():
        pytest.skip(f"{fmt} adapter not installed (optional `lumeri[interop]` extra)")


@pytest.mark.parametrize("fmt", _ALL_FORMATS)
def test_format_exports_and_reopens(fmt: str, tmp_path: Path) -> None:
    """Every available adapter exports the rich project to a file that OTIO
    re-reads back into a project with at least the surviving cut points."""
    _skip_if_absent(fmt)
    out = tmp_path / f"proj{format_extension(fmt)}"
    write_project_to_file(_rich_project(tmp_path), out, fmt)
    assert Path(out).exists()
    rp = read_project_from_file(out, fmt)
    assert len(rp.get("timeline", {}).get("clips") or []) >= 1


@pytest.mark.parametrize("fmt", _LOSSLESS)
def test_lossless_roundtrip_preserves_structure(fmt: str, tmp_path: Path) -> None:
    """otio/otioz/otiod preserve clip count, ids, timing and lumeri metadata
    (incl. track duck_under)."""
    _skip_if_absent(fmt)
    out = tmp_path / f"proj{format_extension(fmt)}"
    write_project_to_file(_rich_project(tmp_path), out, fmt)
    rp = read_project_from_file(out, fmt)

    rclips = rp["timeline"]["clips"]
    assert len(rclips) == 5
    by_id = {c["id"]: c for c in rclips}
    assert set(by_id) == {"c1", "c2", "t1", "m1", "vo1"}
    assert abs(by_id["c2"]["start"] - 2.0) < 1e-3
    assert abs(by_id["m1"]["duration"] - 4.0) < 1e-3
    assert abs(by_id["vo1"]["start"] - 1.0) < 1e-3

    duck = {t["id"]: t.get("duck_under") for t in rp["timeline"]["tracks"]}
    assert duck.get("A1") == "A2"


@pytest.mark.parametrize("fmt", _LOSSY)
def test_lossy_degrades_gracefully(fmt: str, tmp_path: Path) -> None:
    """edl/fcp7/fcpx export without crashing (project pre-simplified) and OTIO
    re-reads the result; cut points survive. Full fidelity is NOT asserted."""
    _skip_if_absent(fmt)
    out = tmp_path / f"proj{format_extension(fmt)}"
    write_project_to_file(_rich_project(tmp_path), out, fmt)  # must not raise
    assert Path(out).exists()
    rp = read_project_from_file(out, fmt)
    rclips = rp.get("timeline", {}).get("clips") or []
    assert len(rclips) >= 1  # at least the video cuts / media clips survive
    # No text clip should survive a lossy NLE export (defined degradation).
    assert all(c.get("media_kind") != "text" for c in rclips)


# ── M8-B regression: the export/import_otio VERBS (M5 left both broken) ───────


def test_export_import_otio_verbs_roundtrip(tmp_path: Path) -> None:
    """Drive project_export_otio + project_import_otio through the DISPATCHER.

    M5 left export crashing on allocate_id('otio') and import double-wrapping the
    patch with a wrong insert shape; both were untested. This locks the M8-B fix
    and the format arg end to end.
    """
    import asyncio

    from gemia.project_store import ProjectHandle
    from gemia.tools import DISPATCHER
    from gemia.tools._context import AssetRegistry, ToolContext

    def _ctx(session: str) -> ToolContext:
        reg = AssetRegistry()
        handle = ProjectHandle.open(tmp_path / session, session, session_id=session)
        return ToolContext(session_id=session, output_dir=tmp_path, registry=reg,
                           emit_progress=lambda _u: None, project=handle)

    def _call(verb: str, args: dict, ctx: ToolContext) -> dict:
        return asyncio.run(DISPATCHER[verb](args, ctx))

    src = _ctx("m8verb_src")
    vid = _real_video(tmp_path, "v.mp4", 2.0)
    aid = src.registry.add_external(vid, summary="v").asset_id
    _call("timeline_insert_clip", {"asset_id": aid}, src)

    out = _call("project_export_otio", {"label": "viaverb", "format": "otio"}, src)
    assert str(out["asset_id"]).startswith("otio")  # allocate_id('otio') no longer crashes
    assert out["format"] == "otio"
    assert out["clip_count"] == 1
    assert Path(out["otio_path"]).exists()

    dst = _ctx("m8verb_dst")
    imp = _call("project_import_otio", {"otio_path": out["otio_path"], "format": "otio"}, dst)
    assert imp["format"] == "otio"
    assert imp["clip_count"] == 1
    clips = dst.project.load()["timeline"]["clips"]
    assert len(clips) == 1 and clips[0]["media_kind"] == "video"

