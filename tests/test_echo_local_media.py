from __future__ import annotations

import hashlib
import json
import os
import shutil
from pathlib import Path

import pytest
from PIL import Image, ImageDraw

import gemia.echo_local_media as local_media
from gemia.echo_local_media import (
    AUDIO_SAMPLE_RATE,
    SUPPORTED_IMAGE_STYLES,
    VIDEO_FPS,
    VIDEO_HEIGHT,
    VIDEO_WIDTH,
    LocalMediaError,
    probe_media,
    render_image_motion,
    sha256_file,
    synthesize_sfx,
)

pytestmark = pytest.mark.skipif(
    shutil.which("ffmpeg") is None or shutil.which("ffprobe") is None,
    reason="real ffmpeg and ffprobe are required",
)


def _still(path: Path) -> None:
    image = Image.new("RGB", (640, 360), (5, 13, 28))
    draw = ImageDraw.Draw(image)
    draw.rectangle((40, 40, 600, 320), outline=(62, 223, 242), width=8)
    draw.ellipse((220, 80, 420, 280), fill=(203, 44, 82))
    draw.line((40, 300, 600, 60), fill=(245, 196, 76), width=12)
    image.save(path)


def _safe_media_root(tmp_path: Path) -> Path:
    """Keep test media out of /tmp without weakening the production guard."""

    resolved = tmp_path.resolve()
    if _is_relative_to(resolved, Path("/tmp")) or _is_relative_to(resolved, Path("/private/tmp")):
        token = hashlib.sha256(f"{resolved}:{os.getpid()}".encode()).hexdigest()[:16]
        root = Path.cwd() / ".pytest_cache" / "echo_local_media" / token
        root.mkdir(parents=True, exist_ok=True)
        return root
    return tmp_path


def _is_relative_to(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
    except ValueError:
        return False
    return True


def _require_pingfang() -> None:
    try:
        local_media._resolve_pingfang_font()
    except LocalMediaError:
        pytest.skip("PingFang is not installed on this non-production host")


def _streams(metadata: dict, kind: str) -> list[dict]:
    return [stream for stream in metadata.get("streams", []) if stream.get("codec_type") == kind]


def _fps(stream: dict) -> float:
    numerator, denominator = str(stream["avg_frame_rate"]).split("/", 1)
    return float(numerator) / float(denominator)


def test_render_image_motion_bakes_real_spec_and_reuses_verified_cache(tmp_path: Path) -> None:
    tmp_path = _safe_media_root(tmp_path)
    source = tmp_path / "img_001.png"
    output = tmp_path / "baked" / "unit_01.mp4"
    _still(source)

    first = render_image_motion(
        source,
        output,
        0.3,
        "ken_burns",
        1,
        source_asset_id="img_001",
    )

    assert first["reused"] is False
    assert first["registration_ready"] is True
    assert first["source"]["kind"] == "local_mg"
    assert first["lineage"] == ["img_001"]
    assert first["lineage_input"] == {
        "path": str(source.resolve()),
        "sha256": sha256_file(source),
    }
    assert first["license"] == {
        "basis": "derived_from_project_asset",
        "source_asset_ids": ["img_001"],
    }
    assert Path(first["sidecar_path"]).is_file()

    metadata = probe_media(output)
    videos = _streams(metadata, "video")
    assert len(videos) == 1
    assert not _streams(metadata, "audio")
    assert videos[0]["codec_name"] == "h264"
    assert videos[0]["width"] == VIDEO_WIDTH
    assert videos[0]["height"] == VIDEO_HEIGHT
    assert videos[0]["pix_fmt"] == "yuv420p"
    assert _fps(videos[0]) == pytest.approx(VIDEO_FPS)
    assert float(metadata["format"]["duration"]) == pytest.approx(0.3, abs=0.04)

    first_mtime = output.stat().st_mtime_ns
    second = render_image_motion(
        source,
        output,
        0.3,
        "ken_burns",
        1,
        source_asset_id="img_001",
    )
    assert second["reused"] is True
    assert second["sha256"] == first["sha256"]
    assert output.stat().st_mtime_ns == first_mtime

    sidecar_path = Path(second["sidecar_path"])
    tampered = json.loads(sidecar_path.read_text(encoding="utf-8"))
    tampered["registration"]["lineage"] = ["img_999"]
    sidecar_path.write_text(json.dumps(tampered), encoding="utf-8")
    repaired = render_image_motion(
        source,
        output,
        0.3,
        "ken_burns",
        1,
        source_asset_id="img_001",
    )
    assert repaired["reused"] is False
    repaired_sidecar = json.loads(sidecar_path.read_text(encoding="utf-8"))
    assert repaired["registration"] == repaired_sidecar["registration"]
    assert repaired["lineage"] == ["img_001"]


def test_render_rebuilds_stale_or_corrupt_existing_output(tmp_path: Path) -> None:
    tmp_path = _safe_media_root(tmp_path)
    source = tmp_path / "img_007.png"
    output = tmp_path / "unit_20.mp4"
    _still(source)
    initial = render_image_motion(
        source,
        output,
        0.2,
        "hero",
        20,
        source_asset_id="img_007",
    )

    output.write_bytes(b"not a video")
    rebuilt = render_image_motion(
        source,
        output,
        0.2,
        "hero",
        20,
        source_asset_id="img_007",
    )
    assert rebuilt["reused"] is False
    assert rebuilt["sha256"] == initial["sha256"]
    assert probe_media(output)["streams"][0]["codec_name"] == "h264"

    changed = render_image_motion(
        source,
        output,
        0.2,
        "white_collapse",
        20,
        source_asset_id="img_007",
    )
    assert changed["reused"] is False
    assert changed["fingerprint"] != rebuilt["fingerprint"]
    sidecar = json.loads(Path(changed["sidecar_path"]).read_text(encoding="utf-8"))
    assert sidecar["fingerprint"] == changed["fingerprint"]
    assert sidecar["output_sha256"] == sha256_file(output)


def test_image_derivative_requires_real_registry_lineage_and_binds_it_to_cache(
    tmp_path: Path,
) -> None:
    tmp_path = _safe_media_root(tmp_path)
    source = tmp_path / "concept.png"
    output = tmp_path / "concept.mp4"
    _still(source)

    with pytest.raises(ValueError, match="requires a real project source_asset_id"):
        render_image_motion(source, output, 0.2, "iris", 2)
    with pytest.raises(ValueError, match="requires a real project source_asset_id"):
        render_image_motion(source, output, 0.2, "iris", 2, source_asset_id="   ")

    resolved = render_image_motion(
        source,
        output,
        0.2,
        "iris",
        2,
        source_asset_id="img_011",
    )
    assert resolved["reused"] is False
    assert resolved["registration_ready"] is True
    assert resolved["lineage"] == ["img_011"]
    assert resolved["lineage_input"]["path"] == str(source.resolve())
    assert resolved["lineage_input"]["sha256"] == sha256_file(source)
    sidecar = json.loads(Path(resolved["sidecar_path"]).read_text(encoding="utf-8"))
    assert sidecar["registration"]["lineage"] == ["img_011"]
    assert sidecar["fingerprint_payload"]["input"]["source_asset_id"] == "img_011"

    rebound = render_image_motion(
        source,
        output,
        0.2,
        "iris",
        2,
        source_asset_id="img_012",
    )
    assert rebound["reused"] is False
    assert rebound["fingerprint"] != resolved["fingerprint"]
    rebound_sidecar = json.loads(Path(rebound["sidecar_path"]).read_text(encoding="utf-8"))
    assert rebound["registration"] == rebound_sidecar["registration"]
    assert rebound["lineage"] == rebound_sidecar["registration"]["lineage"] == ["img_012"]

    cached = render_image_motion(
        source,
        output,
        0.2,
        "iris",
        2,
        source_asset_id="img_012",
    )
    assert cached["reused"] is True
    assert cached["registration"] == rebound_sidecar["registration"]

    with pytest.raises(ValueError, match="requires a real project source_asset_id"):
        render_image_motion(source, output, 0.2, "iris", 2, source_asset_id="aud_011")


def test_title_is_programmatic_owned_video_with_no_lineage(tmp_path: Path) -> None:
    tmp_path = _safe_media_root(tmp_path)
    _require_pingfang()
    result = render_image_motion(
        tmp_path / "unused.png",
        tmp_path / "title.mp4",
        0.4,
        "title",
        42,
    )

    assert result["registration_ready"] is True
    assert result["source"]["kind"] == "owned_video"
    assert result["lineage"] == []
    assert result["lineage_input"] is None
    assert result["license"]["basis"] == "project_created_programmatic_video"
    assert not _streams(probe_media(result["path"]), "audio")


def test_production_mac_prefers_ffmpeg_safe_cjk_collection() -> None:
    expected = Path("/System/Library/Fonts/STHeiti Medium.ttc")
    if not expected.is_file():
        pytest.skip("production macOS CJK font is not installed")

    from gemia.echo_local_media import _resolve_pingfang_font

    assert _resolve_pingfang_font() == expected.resolve()


@pytest.mark.parametrize("style", ["hud", "memory_fold"])
def test_remaining_complex_styles_reach_the_same_delivery_spec(tmp_path: Path, style: str) -> None:
    tmp_path = _safe_media_root(tmp_path)
    if style == "hud":
        _require_pingfang()
    source = tmp_path / "img_009.png"
    _still(source)
    result = render_image_motion(
        source,
        tmp_path / f"{style}.mp4",
        0.2,
        style,
        27,
        source_asset_id="img_009",
    )

    video = _streams(result["probe"], "video")[0]
    assert (video["width"], video["height"]) == (VIDEO_WIDTH, VIDEO_HEIGHT)
    assert video["pix_fmt"] == "yuv420p"
    assert _fps(video) == pytest.approx(VIDEO_FPS)


def test_text_styles_fail_clearly_without_complete_cjk_font(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    tmp_path = _safe_media_root(tmp_path)
    source = tmp_path / "img_002.png"
    _still(source)
    monkeypatch.delenv("LUMERI_PINGFANG_FONT", raising=False)
    monkeypatch.setattr(local_media, "PINGFANG_FONT_CANDIDATES", (tmp_path / "missing.ttc",))

    with pytest.raises(LocalMediaError, match="complete CJK font is required"):
        render_image_motion(
            source,
            tmp_path / "hud.mp4",
            0.2,
            "hud",
            3,
            source_asset_id="img_002",
        )


def test_supported_style_contract_includes_echo_motion_language() -> None:
    expected_styles = {
        "ken_burns",
        "hud",
        "hero",
        "memory_fold",
        "white_collapse",
        "iris",
        "title",
    }
    assert expected_styles == SUPPORTED_IMAGE_STYLES


def test_synthesize_sfx_bakes_real_48k_stereo_wav_and_reuses(tmp_path: Path) -> None:
    tmp_path = _safe_media_root(tmp_path)
    output_dir = tmp_path / "sfx"
    first = synthesize_sfx(output_dir)

    assert set(first) == {"impact", "alarm_glitch", "riser", "collapse"}
    assert all(not cue["reused"] for cue in first.values())
    for cue_name, artifact in first.items():
        assert artifact["source"]["kind"] == "owned_audio"
        assert artifact["source"]["role"] == "sfx"
        assert artifact["source"]["cue"] == cue_name
        assert artifact["lineage"] == []
        assert artifact["registration_ready"] is True
        assert artifact["license"]["basis"] == "project_created_programmatic_audio"
        assert artifact["sha256"] == sha256_file(artifact["path"])
        metadata = probe_media(artifact["path"])
        audios = _streams(metadata, "audio")
        assert len(audios) == 1
        assert not _streams(metadata, "video")
        assert audios[0]["codec_name"] == "pcm_s16le"
        assert int(audios[0]["sample_rate"]) == AUDIO_SAMPLE_RATE
        assert audios[0]["channels"] == 2
        assert audios[0].get("channel_layout", "stereo") == "stereo"

    second = synthesize_sfx(output_dir)
    assert all(cue["reused"] for cue in second.values())
    assert {name: cue["sha256"] for name, cue in second.items()} == {
        name: cue["sha256"] for name, cue in first.items()
    }
    assert not list(output_dir.glob(".*.part.*"))


def test_rejects_legacy_tmp_output_root() -> None:
    with pytest.raises(ValueError, match=r"must not be written under /(private/)?tmp"):
        render_image_motion("/does/not/matter.png", "/tmp/echo-unit.mp4", 0.2, "title", 1)
