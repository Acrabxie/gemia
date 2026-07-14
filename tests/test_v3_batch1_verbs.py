"""Smoke tests for v3 batch-1 verbs (composite / arrange_timeline / mix_audio
/ transform_geometry / edit_image / extract_frame).

Follows the existing pattern from ``test_v3_infra_regressions.py``: stub
``run_ffmpeg_with_progress`` and ``ffprobe_*`` helpers, then assert on
ffmpeg cmd composition + registry shape. Real ffmpeg is never invoked.
"""
from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from gemia.errors import ToolError
from gemia.tools import (
    adjust_media as adjust_media_tool,
    arrange_timeline as arrange_timeline_tool,
    composite as composite_tool,
    edit_audio as edit_audio_tool,
    edit_image as edit_image_tool,
    extract_frame as extract_frame_tool,
    mix_audio as mix_audio_tool,
    probe_media as probe_media_tool,
    safe_areas as safe_areas_tool,
    smart_reframe as smart_reframe_tool,
    transform_geometry as transform_geometry_tool,
)
from gemia.tools._context import AssetRegistry, ToolContext


def _ctx(tmp_path: Path) -> ToolContext:
    return ToolContext(
        session_id="test-batch1",
        output_dir=tmp_path,
        registry=AssetRegistry(),
        emit_progress=lambda _u: None,
    )


def _make_video(ctx: ToolContext, tmp_path: Path, name: str) -> str:
    path = tmp_path / f"{name}.mp4"
    path.write_bytes(b"video")
    return ctx.registry.add_external(path).asset_id


def _make_image(ctx: ToolContext, tmp_path: Path, name: str) -> str:
    path = tmp_path / f"{name}.png"
    path.write_bytes(b"image")
    return ctx.registry.add_external(path).asset_id


def _make_audio(ctx: ToolContext, tmp_path: Path, name: str) -> str:
    path = tmp_path / f"{name}.wav"
    path.write_bytes(b"audio")
    return ctx.registry.add_external(path).asset_id


# ───────────────────────────── composite ─────────────────────────────


def test_adjust_media_video_builds_direct_eq_filter(monkeypatch, tmp_path: Path) -> None:
    ctx = _ctx(tmp_path)
    vid = _make_video(ctx, tmp_path, "base")
    seen = {}

    async def fake_run(cmd, *, total_seconds, progress) -> None:
        seen["cmd"] = cmd
        seen["total"] = total_seconds
        Path(cmd[-1]).write_bytes(b"out")

    monkeypatch.setattr(adjust_media_tool, "ffprobe_duration", lambda _p: 2.5)
    monkeypatch.setattr(adjust_media_tool, "run_ffmpeg_with_progress", fake_run)

    result = asyncio.run(
        adjust_media_tool.dispatch(
            {
                "asset_id": vid,
                "brightness": 0.12,
                "contrast": 1.25,
                "saturation": 0.8,
                "exposure": 1.0,
                "gamma": 0.95,
            },
            ctx,
        )
    )

    cmd = " ".join(seen["cmd"])
    assert "lutrgb=" in cmd
    assert "eq=brightness=0.120000:contrast=1.250000:saturation=0.800000:gamma=0.950000" in cmd
    assert seen["total"] == pytest.approx(2.5)
    assert ctx.registry.get(result["asset_id"]).kind == "video"
    assert result["metadata"]["exposure"] == pytest.approx(1.0)


def test_adjust_media_image_outputs_image(monkeypatch, tmp_path: Path) -> None:
    ctx = _ctx(tmp_path)
    img = _make_image(ctx, tmp_path, "still")

    async def fake_run(cmd, *, total_seconds, progress) -> None:
        fake_run.cmd = cmd  # type: ignore[attr-defined]
        Path(cmd[-1]).write_bytes(b"out")

    monkeypatch.setattr(adjust_media_tool, "run_ffmpeg_with_progress", fake_run)

    result = asyncio.run(
        adjust_media_tool.dispatch(
            {"asset_id": img, "brightness": -0.1, "contrast": 0.9, "saturation": 0.0},
            ctx,
        )
    )

    cmd = " ".join(fake_run.cmd)  # type: ignore[attr-defined]
    assert "-frames:v 1" in cmd
    assert "saturation=0.000000" in cmd
    assert ctx.registry.get(result["asset_id"]).kind == "image"


def test_adjust_media_rejects_out_of_range(tmp_path: Path) -> None:
    ctx = _ctx(tmp_path)
    vid = _make_video(ctx, tmp_path, "base")
    with pytest.raises(ToolError, match="brightness"):
        asyncio.run(adjust_media_tool.dispatch({"asset_id": vid, "brightness": 2.0}, ctx))


def test_composite_alpha_overlay_includes_overlay_input(monkeypatch, tmp_path: Path) -> None:
    ctx = _ctx(tmp_path)
    base = _make_video(ctx, tmp_path, "base")
    overlay = _make_image(ctx, tmp_path, "logo")
    seen = {}

    async def fake_run(cmd, *, total_seconds, progress) -> None:
        seen["cmd"] = cmd
        Path(cmd[-1]).write_bytes(b"out")

    monkeypatch.setattr(composite_tool, "ffprobe_duration", lambda _p: 3.0)
    monkeypatch.setattr(composite_tool, "run_ffmpeg_with_progress", fake_run)

    result = asyncio.run(
        composite_tool.dispatch(
            {
                "base_asset_id": base,
                "overlay_asset_id": overlay,
                "mode": "alpha",
                "opacity": 0.6,
                "position": {"x": 40, "y": 80},
            },
            ctx,
        )
    )

    cmd_text = " ".join(seen["cmd"])
    assert "[0:v][ovl]overlay=40:80[out]" in cmd_text
    assert "colorchannelmixer=aa=0.6000" in cmd_text
    record = ctx.registry.get(result["asset_id"])
    assert record.kind == "video"
    assert result["metadata"]["mode"] == "alpha"


def test_composite_blend_modes_use_blend_filter(monkeypatch, tmp_path: Path) -> None:
    ctx = _ctx(tmp_path)
    base = _make_image(ctx, tmp_path, "base")
    overlay = _make_image(ctx, tmp_path, "ov")
    seen = {}

    async def fake_run(cmd, *, total_seconds, progress) -> None:
        seen["cmd"] = cmd
        Path(cmd[-1]).write_bytes(b"out")

    monkeypatch.setattr(composite_tool, "ffprobe_duration", lambda _p: 0.0)
    monkeypatch.setattr(composite_tool, "run_ffmpeg_with_progress", fake_run)

    result = asyncio.run(
        composite_tool.dispatch(
            {"base_asset_id": base, "overlay_asset_id": overlay, "mode": "multiply"},
            ctx,
        )
    )
    assert "blend=all_mode='multiply'" in " ".join(seen["cmd"])
    assert ctx.registry.get(result["asset_id"]).kind == "image"


def test_composite_rejects_bad_opacity(tmp_path: Path) -> None:
    ctx = _ctx(tmp_path)
    base = _make_image(ctx, tmp_path, "b")
    overlay = _make_image(ctx, tmp_path, "o")
    with pytest.raises(ValueError, match="opacity"):
        asyncio.run(
            composite_tool.dispatch(
                {"base_asset_id": base, "overlay_asset_id": overlay, "mode": "alpha", "opacity": 1.5},
                ctx,
            )
        )


# ─────────────────────────── arrange_timeline ───────────────────────────


def test_arrange_timeline_all_cut_uses_concat_demuxer(monkeypatch, tmp_path: Path) -> None:
    ctx = _ctx(tmp_path)
    a = _make_video(ctx, tmp_path, "a")
    b = _make_video(ctx, tmp_path, "b")
    seen = {}

    async def fake_run(cmd, *, total_seconds, progress) -> None:
        seen["cmd"] = cmd
        seen["total"] = total_seconds
        Path(cmd[-1]).write_bytes(b"out")

    monkeypatch.setattr(arrange_timeline_tool, "ffprobe_duration", lambda _p: 2.0)
    monkeypatch.setattr(
        arrange_timeline_tool, "ffprobe_metadata",
        lambda _p: {"streams": [{"codec_type": "video"}]},
    )
    monkeypatch.setattr(arrange_timeline_tool, "run_ffmpeg_with_progress", fake_run)

    result = asyncio.run(
        arrange_timeline_tool.dispatch({"asset_ids": [a, b]}, ctx)
    )

    cmd = " ".join(seen["cmd"])
    assert "-f concat" in cmd
    assert seen["total"] == pytest.approx(4.0)
    assert result["metadata"]["all_cut"] is True
    assert result["metadata"]["clip_count"] == 2


def test_arrange_timeline_dissolve_uses_xfade_with_offset(monkeypatch, tmp_path: Path) -> None:
    ctx = _ctx(tmp_path)
    a = _make_video(ctx, tmp_path, "a")
    b = _make_video(ctx, tmp_path, "b")
    seen = {}

    async def fake_run(cmd, *, total_seconds, progress) -> None:
        seen["cmd"] = cmd
        seen["total"] = total_seconds
        Path(cmd[-1]).write_bytes(b"out")

    monkeypatch.setattr(arrange_timeline_tool, "ffprobe_duration", lambda _p: 4.0)
    monkeypatch.setattr(
        arrange_timeline_tool, "ffprobe_metadata",
        lambda _p: {"streams": [{"codec_type": "video"}, {"codec_type": "audio"}]},
    )
    monkeypatch.setattr(arrange_timeline_tool, "run_ffmpeg_with_progress", fake_run)

    result = asyncio.run(
        arrange_timeline_tool.dispatch(
            {
                "asset_ids": [a, b],
                "transitions": [{"between_index": 0, "kind": "dissolve", "duration_sec": 0.5}],
            },
            ctx,
        )
    )

    cmd = " ".join(seen["cmd"])
    assert "xfade=transition=fade" in cmd
    assert "duration=0.500" in cmd
    assert "offset=3.500" in cmd
    # 4 + 4 - 0.5 = 7.5
    assert seen["total"] == pytest.approx(7.5)
    assert "acrossfade=d=0.500" in cmd
    assert result["metadata"]["all_cut"] is False


def test_arrange_timeline_rejects_out_of_range_transition(monkeypatch, tmp_path: Path) -> None:
    ctx = _ctx(tmp_path)
    a = _make_video(ctx, tmp_path, "a")
    b = _make_video(ctx, tmp_path, "b")
    monkeypatch.setattr(arrange_timeline_tool, "ffprobe_duration", lambda _p: 2.0)
    monkeypatch.setattr(
        arrange_timeline_tool, "ffprobe_metadata",
        lambda _p: {"streams": [{"codec_type": "video"}]},
    )
    with pytest.raises(ValueError, match="out of range"):
        asyncio.run(
            arrange_timeline_tool.dispatch(
                {
                    "asset_ids": [a, b],
                    "transitions": [{"between_index": 5, "kind": "dissolve", "duration_sec": 0.5}],
                },
                ctx,
            )
        )


# ───────────────────────────── mix_audio ─────────────────────────────


def test_mix_audio_concat_with_levels_applies_volume_filters(monkeypatch, tmp_path: Path) -> None:
    ctx = _ctx(tmp_path)
    a = _make_audio(ctx, tmp_path, "a")
    b = _make_audio(ctx, tmp_path, "b")
    seen = {}

    async def fake_run(cmd, *, total_seconds, progress) -> None:
        seen["cmd"] = cmd
        seen["total"] = total_seconds
        Path(cmd[-1]).write_bytes(b"out")

    monkeypatch.setattr(mix_audio_tool, "ffprobe_duration", lambda _p: 5.0)
    monkeypatch.setattr(mix_audio_tool, "run_ffmpeg_with_progress", fake_run)

    asyncio.run(
        mix_audio_tool.dispatch(
            {"asset_ids": [a, b], "mode": "concat", "levels_db": [0.0, -6.0]},
            ctx,
        )
    )
    cmd = " ".join(seen["cmd"])
    assert "volume=0.501187" in cmd  # 10^(-6/20)
    assert "concat=n=2:v=0:a=1" in cmd
    assert seen["total"] == pytest.approx(10.0)


def test_mix_audio_duck_chains_sidechain(monkeypatch, tmp_path: Path) -> None:
    ctx = _ctx(tmp_path)
    bed = _make_audio(ctx, tmp_path, "bed")
    vox = _make_audio(ctx, tmp_path, "vox")

    async def fake_run(cmd, *, total_seconds, progress) -> None:
        Path(cmd[-1]).write_bytes(b"out")
        fake_run.cmd = cmd  # type: ignore[attr-defined]

    monkeypatch.setattr(mix_audio_tool, "ffprobe_duration", lambda _p: 3.0)
    monkeypatch.setattr(mix_audio_tool, "run_ffmpeg_with_progress", fake_run)

    asyncio.run(
        mix_audio_tool.dispatch({"asset_ids": [bed, vox], "mode": "duck"}, ctx)
    )
    cmd = " ".join(fake_run.cmd)  # type: ignore[attr-defined]
    assert "sidechaincompress" in cmd
    assert "amix=inputs=2:duration=longest" in cmd


def test_mix_audio_rejects_single_input(tmp_path: Path) -> None:
    ctx = _ctx(tmp_path)
    a = _make_audio(ctx, tmp_path, "a")
    with pytest.raises(ValueError, match="at least 2"):
        asyncio.run(mix_audio_tool.dispatch({"asset_ids": [a], "mode": "mix"}, ctx))


def test_mix_audio_rejects_levels_length_mismatch(tmp_path: Path) -> None:
    ctx = _ctx(tmp_path)
    a = _make_audio(ctx, tmp_path, "a")
    b = _make_audio(ctx, tmp_path, "b")
    with pytest.raises(ValueError, match="length"):
        asyncio.run(
            mix_audio_tool.dispatch(
                {"asset_ids": [a, b], "mode": "mix", "levels_db": [0.0]}, ctx
            )
        )


# ─────────────────────────── transform_geometry ───────────────────────────


def test_transform_geometry_crop_builds_crop_filter(monkeypatch, tmp_path: Path) -> None:
    ctx = _ctx(tmp_path)
    asset = _make_video(ctx, tmp_path, "src")

    async def fake_run(cmd, *, total_seconds, progress) -> None:
        Path(cmd[-1]).write_bytes(b"out")
        fake_run.cmd = cmd  # type: ignore[attr-defined]

    monkeypatch.setattr(transform_geometry_tool, "ffprobe_duration", lambda _p: 2.0)
    monkeypatch.setattr(transform_geometry_tool, "run_ffmpeg_with_progress", fake_run)

    result = asyncio.run(
        transform_geometry_tool.dispatch(
            {
                "asset_id": asset,
                "operation": "crop",
                "params": {"x": 10, "y": 20, "w": 640, "h": 360},
            },
            ctx,
        )
    )
    assert "-vf crop=640:360:10:20" in " ".join(fake_run.cmd)  # type: ignore[attr-defined]
    assert result["metadata"]["params"] == {"x": 10, "y": 20, "w": 640, "h": 360}


def test_transform_geometry_scale_with_factor(monkeypatch, tmp_path: Path) -> None:
    ctx = _ctx(tmp_path)
    asset = _make_image(ctx, tmp_path, "src")

    async def fake_run(cmd, *, total_seconds, progress) -> None:
        Path(cmd[-1]).write_bytes(b"out")
        fake_run.cmd = cmd  # type: ignore[attr-defined]

    monkeypatch.setattr(transform_geometry_tool, "ffprobe_duration", lambda _p: 0.0)
    monkeypatch.setattr(transform_geometry_tool, "run_ffmpeg_with_progress", fake_run)

    asyncio.run(
        transform_geometry_tool.dispatch(
            {"asset_id": asset, "operation": "scale", "params": {"factor": 2.0}}, ctx
        )
    )
    assert "scale=iw*2.0:ih*2.0" in " ".join(fake_run.cmd)  # type: ignore[attr-defined]


def test_transform_geometry_perspective_requires_all_four_corners(tmp_path: Path) -> None:
    ctx = _ctx(tmp_path)
    asset = _make_image(ctx, tmp_path, "src")
    with pytest.raises(ValueError, match="top_right"):
        asyncio.run(
            transform_geometry_tool.dispatch(
                {
                    "asset_id": asset,
                    "operation": "perspective",
                    "params": {
                        "top_left": [0, 0],
                        # top_right intentionally missing
                        "bottom_left": [0, 100],
                        "bottom_right": [100, 100],
                    },
                },
                ctx,
            )
        )


def test_transform_geometry_scale_rejects_factor_with_dim(tmp_path: Path) -> None:
    ctx = _ctx(tmp_path)
    asset = _make_image(ctx, tmp_path, "src")
    with pytest.raises(ValueError, match="factor OR w/h"):
        asyncio.run(
            transform_geometry_tool.dispatch(
                {"asset_id": asset, "operation": "scale", "params": {"factor": 2.0, "w": 100}},
                ctx,
            )
        )


# ───────────────────────────── edit_image ─────────────────────────────


def test_edit_image_blur_builds_gblur_filter(monkeypatch, tmp_path: Path) -> None:
    ctx = _ctx(tmp_path)
    asset = _make_image(ctx, tmp_path, "src")

    async def fake_run(cmd, *, total_seconds, progress) -> None:
        Path(cmd[-1]).write_bytes(b"out")
        fake_run.cmd = cmd  # type: ignore[attr-defined]

    monkeypatch.setattr(edit_image_tool, "run_ffmpeg_with_progress", fake_run)

    result = asyncio.run(
        edit_image_tool.dispatch(
            {"asset_id": asset, "operation": "blur", "params": {"radius": 6.0}}, ctx
        )
    )
    assert "gblur=sigma=3.000" in " ".join(fake_run.cmd)  # type: ignore[attr-defined]
    assert ctx.registry.get(result["asset_id"]).kind == "image"


def test_edit_image_denoise_uses_nlmeans(monkeypatch, tmp_path: Path) -> None:
    ctx = _ctx(tmp_path)
    asset = _make_image(ctx, tmp_path, "src")

    async def fake_run(cmd, *, total_seconds, progress) -> None:
        Path(cmd[-1]).write_bytes(b"out")
        fake_run.cmd = cmd  # type: ignore[attr-defined]

    monkeypatch.setattr(edit_image_tool, "run_ffmpeg_with_progress", fake_run)

    asyncio.run(
        edit_image_tool.dispatch(
            {"asset_id": asset, "operation": "denoise", "params": {"strength": 2.5}}, ctx
        )
    )
    assert "nlmeans=s=2.50" in " ".join(fake_run.cmd)  # type: ignore[attr-defined]


def test_edit_image_remove_background_produces_alpha_cutout(tmp_path: Path) -> None:
    # remove_background is now real ML/fallback matting: it returns a transparent
    # RGBA PNG cutout (no longer an honest not-implemented failure).
    import numpy as np
    from PIL import Image

    ctx = _ctx(tmp_path)
    arr = np.zeros((200, 150, 3), np.uint8)
    arr[:] = (18, 18, 18)
    arr[35:175, 45:110] = (205, 185, 170)   # a bright "subject" GrabCut can find
    src = tmp_path / "real_src.png"
    Image.fromarray(arr).save(src)
    asset = ctx.registry.add_external(src).asset_id

    res = asyncio.run(
        edit_image_tool.dispatch(
            {"asset_id": asset, "operation": "remove_background", "params": {}}, ctx
        )
    )
    out = ctx.registry.get(res["asset_id"])
    assert out.path.suffix == ".png"
    assert Image.open(out.path).mode == "RGBA"
    assert res["metadata"]["operation"] == "remove_background"
    assert 0.0 <= res["metadata"]["coverage"] <= 1.0


def test_edit_image_rejects_video_input(tmp_path: Path) -> None:
    ctx = _ctx(tmp_path)
    asset = _make_video(ctx, tmp_path, "src")
    with pytest.raises(ToolError, match="image asset") as exc_info:
        asyncio.run(
            edit_image_tool.dispatch(
                {"asset_id": asset, "operation": "blur", "params": {"radius": 3.0}}, ctx
            )
        )
    assert exc_info.value.code == "E_UNSUPPORTED"


# ───────────────────────────── extract_frame ─────────────────────────────


def test_extract_frame_clamps_past_end(monkeypatch, tmp_path: Path) -> None:
    ctx = _ctx(tmp_path)
    asset = _make_video(ctx, tmp_path, "src")

    async def fake_run(cmd, *, total_seconds, progress) -> None:
        Path(cmd[-1]).write_bytes(b"out")
        fake_run.cmd = cmd  # type: ignore[attr-defined]

    monkeypatch.setattr(extract_frame_tool, "ffprobe_duration", lambda _p: 2.0)
    monkeypatch.setattr(extract_frame_tool, "run_ffmpeg_with_progress", fake_run)

    result = asyncio.run(
        extract_frame_tool.dispatch({"asset_id": asset, "time_sec": 99.0}, ctx)
    )
    assert result["metadata"]["clamped"] is True
    assert result["metadata"]["time_sec"] == pytest.approx(1.95, abs=1e-3)
    cmd = " ".join(fake_run.cmd)  # type: ignore[attr-defined]
    assert "-ss 1.950" in cmd


def test_extract_frame_within_duration_not_clamped(monkeypatch, tmp_path: Path) -> None:
    ctx = _ctx(tmp_path)
    asset = _make_video(ctx, tmp_path, "src")

    async def fake_run(cmd, *, total_seconds, progress) -> None:
        Path(cmd[-1]).write_bytes(b"out")

    monkeypatch.setattr(extract_frame_tool, "ffprobe_duration", lambda _p: 10.0)
    monkeypatch.setattr(extract_frame_tool, "run_ffmpeg_with_progress", fake_run)

    result = asyncio.run(
        extract_frame_tool.dispatch({"asset_id": asset, "time_sec": 4.5}, ctx)
    )
    assert result["metadata"]["clamped"] is False
    assert result["metadata"]["time_sec"] == pytest.approx(4.5)
    record = ctx.registry.get(result["asset_id"])
    assert record.kind == "image"
    assert record.path.suffix == ".png"


def test_extract_frame_rejects_negative_time(tmp_path: Path) -> None:
    ctx = _ctx(tmp_path)
    asset = _make_video(ctx, tmp_path, "src")
    with pytest.raises(ValueError, match=">= 0"):
        asyncio.run(
            extract_frame_tool.dispatch({"asset_id": asset, "time_sec": -1.0}, ctx)
        )


# ─────────────────────── probe / audio / social canvas ───────────────────────


def test_probe_media_returns_physical_metadata(monkeypatch, tmp_path: Path) -> None:
    ctx = _ctx(tmp_path)
    vid = _make_video(ctx, tmp_path, "probe")

    monkeypatch.setattr(
        probe_media_tool,
        "ffprobe_metadata",
        lambda _p: {
            "format": {"duration": "1.234", "size": "98765", "bit_rate": "640000"},
            "streams": [
                {
                    "codec_type": "video",
                    "width": 1920,
                    "height": 1080,
                    "avg_frame_rate": "30000/1001",
                    "codec_name": "h264",
                },
                {
                    "codec_type": "audio",
                    "codec_name": "aac",
                    "channels": 2,
                    "sample_rate": "48000",
                },
            ],
        },
    )

    result = asyncio.run(probe_media_tool.dispatch({"asset_id": vid}, ctx))

    assert result["duration_ms"] == 1234
    assert result["width"] == 1920
    assert result["height"] == 1080
    assert result["fps"] == pytest.approx(29.97003)
    assert result["video_codec"] == "h264"
    assert result["audio_codec"] == "aac"
    assert result["channels"] == 2
    assert result["sample_rate"] == 48000


def test_edit_audio_gain_and_fades_build_filter(monkeypatch, tmp_path: Path) -> None:
    ctx = _ctx(tmp_path)
    audio = _make_audio(ctx, tmp_path, "voice")
    seen = {}

    async def fake_run(cmd, *, total_seconds, progress) -> None:
        seen["cmd"] = cmd
        seen["total"] = total_seconds
        Path(cmd[-1]).write_bytes(b"wav")

    monkeypatch.setattr(edit_audio_tool, "ffprobe_duration", lambda _p: 10.0)
    monkeypatch.setattr(edit_audio_tool, "run_ffmpeg_with_progress", fake_run)

    result = asyncio.run(
        edit_audio_tool.dispatch(
            {"asset_id": audio, "gain_db": 6.0, "fade_in_sec": 1.0, "fade_out_sec": 2.0},
            ctx,
        )
    )

    cmd = " ".join(seen["cmd"])
    assert "volume=1.99526231" in cmd
    assert "afade=t=in:st=0:d=1.000000" in cmd
    assert "afade=t=out:st=8.000000:d=2.000000" in cmd
    assert seen["total"] == pytest.approx(10.0)
    assert ctx.registry.get(result["asset_id"]).kind == "audio"


def test_edit_audio_requires_an_actual_change(tmp_path: Path) -> None:
    ctx = _ctx(tmp_path)
    audio = _make_audio(ctx, tmp_path, "flat")
    with pytest.raises(ToolError, match="at least one change"):
        asyncio.run(edit_audio_tool.dispatch({"asset_id": audio}, ctx))


def test_smart_reframe_center_crop_video(monkeypatch, tmp_path: Path) -> None:
    ctx = _ctx(tmp_path)
    vid = _make_video(ctx, tmp_path, "wide")
    seen = {}

    async def fake_run(cmd, *, total_seconds, progress) -> None:
        seen["cmd"] = cmd
        seen["total"] = total_seconds
        Path(cmd[-1]).write_bytes(b"mp4")

    monkeypatch.setattr(smart_reframe_tool, "ffprobe_duration", lambda _p: 4.0)
    monkeypatch.setattr(smart_reframe_tool, "run_ffmpeg_with_progress", fake_run)

    result = asyncio.run(
        smart_reframe_tool.dispatch(
            {"asset_id": vid, "target": "9:16", "anchor_x": 0.35, "anchor_y": 0.5},
            ctx,
        )
    )

    cmd = " ".join(seen["cmd"])
    assert "scale=1080:1920:force_original_aspect_ratio=increase" in cmd
    assert "crop=1080:1920:(iw-ow)*0.350000:(ih-oh)*0.500000" in cmd
    assert seen["total"] == pytest.approx(4.0)
    assert result["metadata"]["width"] == 1080
    assert result["metadata"]["height"] == 1920


def test_smart_reframe_fit_pad_image(monkeypatch, tmp_path: Path) -> None:
    ctx = _ctx(tmp_path)
    img = _make_image(ctx, tmp_path, "still")

    async def fake_run(cmd, *, total_seconds, progress) -> None:
        fake_run.cmd = cmd  # type: ignore[attr-defined]
        Path(cmd[-1]).write_bytes(b"png")

    monkeypatch.setattr(smart_reframe_tool, "run_ffmpeg_with_progress", fake_run)

    result = asyncio.run(
        smart_reframe_tool.dispatch(
            {"asset_id": img, "target": "1:1", "mode": "fit_pad", "background": "#101010"},
            ctx,
        )
    )

    cmd = " ".join(fake_run.cmd)  # type: ignore[attr-defined]
    assert "force_original_aspect_ratio=decrease" in cmd
    assert "pad=1080:1080:(ow-iw)/2:(oh-ih)/2:color=0x101010" in cmd
    assert "-frames:v 1" in cmd
    assert ctx.registry.get(result["asset_id"]).kind == "image"


def test_get_safe_areas_scales_platform_preset(tmp_path: Path) -> None:
    ctx = _ctx(tmp_path)

    result = asyncio.run(
        safe_areas_tool.dispatch({"platform": "tiktok", "width": 540, "height": 960}, ctx)
    )

    assert result["platform"] == "tiktok"
    assert result["title_safe_box"]["x"] == 48
    assert result["title_safe_box"]["width"] < 540
    assert any(zone["id"] == "right_action_stack" for zone in result["avoid_zones"])


# ─────────────────────────── DISPATCHER wiring ───────────────────────────


def test_dispatcher_registers_batch1_local_verbs() -> None:
    from gemia.tools import DISPATCHER

    for name in (
        "composite",
        "arrange_timeline",
        "mix_audio",
        "edit_audio",
        "transform_geometry",
        "smart_reframe",
        "edit_image",
        "extract_frame",
        "probe_media",
        "get_safe_areas",
    ):
        assert name in DISPATCHER
        # The dispatcher must not be a stub anymore — stubs are named "stub_*".
        assert not DISPATCHER[name].__name__.startswith("stub_"), (
            f"{name} dispatcher is still a stub"
        )


def test_provider_media_dispatchers_are_real() -> None:
    from gemia.tools import DISPATCHER

    # generate_image, generate_video, and generate_audio all use Vertex now.
    # search_library moved to a real cheap lookup in v4 M3.
    for name in ("generate_image", "generate_video", "generate_audio", "search_library"):
        assert not DISPATCHER[name].__name__.startswith("stub_"), (
            f"{name} dispatcher should not be a stub"
        )
