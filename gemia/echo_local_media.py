"""Deterministic, zero-dollar local media baking for the Echo Protocol case.

This module deliberately has no provider, network, session, or budget dependency.
It turns project-owned stills into short 1080p motion units and synthesizes a
small set of local sound-design cues with ffmpeg.  Every output is accompanied
by a content-addressed sidecar so a production restart can distinguish a valid
cache hit from a stale or corrupt file.

The returned ``registration`` mapping is shaped for
``AssetRegistry.register_output``.  Image-derived renders are only marked ready
for registration when the caller supplies the real source image asset id; the
input path and hash are still returned when it does not, rather than inventing
lineage.  Purely programmatic title and audio outputs have no input lineage and
use the ``owned_video`` / ``owned_audio`` source kinds.
"""

from __future__ import annotations

import hashlib
import json
import math
import os
import re
import subprocess
import uuid
from contextlib import suppress
from pathlib import Path
from typing import Any

from gemia.compat import ffmpeg_path, ffprobe_path

SCHEMA_VERSION = 1
VIDEO_WIDTH = 1920
VIDEO_HEIGHT = 1080
VIDEO_FPS = 30
AUDIO_SAMPLE_RATE = 48_000

SUPPORTED_IMAGE_STYLES = frozenset(
    {
        "ken_burns",
        "hud",
        "hero",
        "memory_fold",
        "white_collapse",
        "iris",
        "title",
    }
)

# FFmpeg selects the first face from a TTC.  On the production Mac the private
# PingFang UI collection exists and advertises the glyphs, but drawtext renders
# some of them as LastResort boxes.  Prefer system CJK collections whose first
# face is a complete Simplified Chinese face; keep PingFang as a later fallback.
# An explicit override remains useful for packaged builds and tests.  Never
# substitute a Latin-only font: drawtext can exit zero while delivery text is
# tofu.
PINGFANG_FONT_CANDIDATES = (
    Path("/System/Library/Fonts/STHeiti Medium.ttc"),
    Path("/System/Library/Fonts/STHeiti Light.ttc"),
    Path("/System/Library/Fonts/Hiragino Sans GB.ttc"),
    Path("/System/Library/Fonts/Supplemental/Arial Unicode.ttf"),
    Path("/System/Library/Fonts/PingFang.ttc"),
    Path(
        "/System/Library/PrivateFrameworks/FontServices.framework/Versions/A/"
        "Resources/Reserved/PingFangUI.ttc"
    ),
    Path("/Library/Fonts/PingFang.ttc"),
    Path("/Library/Fonts/PingFang.ttf"),
)


class LocalMediaError(RuntimeError):
    """A local bake failed or produced media outside the required spec."""


def sha256_file(path: str | Path) -> str:
    """Return the SHA-256 of a real file, failing clearly for absent inputs."""

    source = Path(path).expanduser()
    if not source.is_file():
        raise FileNotFoundError(f"media file does not exist: {source}")
    digest = hashlib.sha256()
    with source.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def probe_media(path: str | Path) -> dict[str, Any]:
    """Run ffprobe and return its JSON media facts."""

    media_path = Path(path).expanduser()
    if not media_path.is_file():
        raise FileNotFoundError(f"media file does not exist: {media_path}")
    command = [
        ffprobe_path(),
        "-v",
        "error",
        "-print_format",
        "json",
        "-show_format",
        "-show_streams",
        str(media_path),
    ]
    try:
        completed = subprocess.run(
            command,
            check=True,
            capture_output=True,
            text=True,
            timeout=30,
        )
        value = json.loads(completed.stdout)
    except (OSError, subprocess.SubprocessError, json.JSONDecodeError) as exc:
        raise LocalMediaError(f"ffprobe failed for {media_path.name}: {exc}") from exc
    if not isinstance(value, dict):
        raise LocalMediaError(f"ffprobe returned an invalid payload for {media_path.name}")
    return value


def render_image_motion(
    input_png: str | Path,
    output_mp4: str | Path,
    duration: float,
    style: str,
    unit_index: int,
    *,
    source_asset_id: str | None = None,
) -> dict[str, Any]:
    """Bake one deterministic 1080p/30fps H.264 motion unit.

    ``title`` is a pure programmatic title card and does not consume the image;
    it therefore returns ``source.kind=owned_video`` with empty lineage.  All
    other styles derive from ``input_png`` and return ``source.kind=local_mg``.
    Image-derived callers must pass ``source_asset_id`` so the returned registry
    suggestion points at the real project image rather than inventing an id.
    """

    normalized_style = str(style).strip().lower()
    if normalized_style not in SUPPORTED_IMAGE_STYLES:
        choices = ", ".join(sorted(SUPPORTED_IMAGE_STYLES))
        raise ValueError(f"unsupported local motion style {style!r}; expected one of: {choices}")
    if isinstance(unit_index, bool) or not isinstance(unit_index, int) or unit_index < 0:
        raise ValueError("unit_index must be a non-negative integer")
    normalized_source_asset_id = None
    if normalized_style != "title":
        normalized_source_asset_id = str(source_asset_id or "").strip()
        if re.fullmatch(r"img_[0-9]{3,}", normalized_source_asset_id) is None:
            raise ValueError(
                "image-derived motion requires a real project source_asset_id such as img_001; "
                "the caller must verify that id, input path, and hash against AssetRegistry"
            )
    try:
        requested_duration = float(duration)
    except (TypeError, ValueError) as exc:
        raise ValueError("duration must be a finite positive number") from exc
    if not math.isfinite(requested_duration) or requested_duration <= 0:
        raise ValueError("duration must be a finite positive number")

    frame_count = max(1, round(requested_duration * VIDEO_FPS))
    effective_duration = frame_count / VIDEO_FPS
    if abs(requested_duration - effective_duration) > (0.5 / VIDEO_FPS) + 1e-9:
        raise ValueError("duration cannot be represented at 30fps without excessive rounding")

    destination = _prepare_output_path(output_mp4, suffix=".mp4")
    source_path = Path(input_png).expanduser().resolve()
    is_programmatic_title = normalized_style == "title"
    input_hash = ""
    if not is_programmatic_title:
        if not source_path.is_file():
            raise FileNotFoundError(f"input image does not exist: {source_path}")
        if source_path == destination:
            raise ValueError("input image and output video must be different files")
        input_hash = sha256_file(source_path)

    font_path: Path | None = None
    if normalized_style in {"hud", "title"}:
        font_path = _resolve_pingfang_font()

    fingerprint_payload: dict[str, Any] = {
        "schema_version": SCHEMA_VERSION,
        "operation": "render_image_motion",
        "style": normalized_style,
        "unit_index": unit_index,
        "duration_seconds": effective_duration,
        "frame_count": frame_count,
        "output_spec": {
            "width": VIDEO_WIDTH,
            "height": VIDEO_HEIGHT,
            "fps": VIDEO_FPS,
            "codec": "h264",
            "pixel_format": "yuv420p",
            "audio_streams": 0,
        },
        "input": None
        if is_programmatic_title
        else {
            "path": str(source_path),
            "sha256": input_hash,
            # Lineage is durable production input even though it does not
            # change pixels. Including it prevents a cache created without a
            # real asset id from keeping a registration-incomplete sidecar.
            "source_asset_id": normalized_source_asset_id,
        },
        "font": None
        if font_path is None
        else {"path": str(font_path), "sha256": sha256_file(font_path)},
    }
    fingerprint = _json_fingerprint(fingerprint_payload)

    if is_programmatic_title:
        lineage: list[str] = []
    else:
        assert normalized_source_asset_id is not None  # validated above
        lineage = [normalized_source_asset_id]
    source: dict[str, Any]
    license_data: dict[str, Any]
    registration_ready = True
    if is_programmatic_title:
        source = {
            "kind": "owned_video",
            "provider": "lumeri_local_ffmpeg",
            "role": "title",
            "cost_usd": 0.0,
            "generator": "echo_local_media",
        }
        license_data = {"basis": "project_created_programmatic_video"}
    else:
        source = {
            "kind": "local_mg",
            "provider": "lumeri_local_ffmpeg",
            "cost_usd": 0.0,
            "generator": "echo_local_media",
            "input_path": str(source_path),
            "input_sha256": input_hash,
        }
        license_data = {
            "basis": "derived_from_project_asset",
            "source_asset_ids": list(lineage),
        }
        registration_ready = True

    registration = {
        "kind": "video",
        "path": str(destination),
        "summary": f"Echo Protocol local {normalized_style} motion unit {unit_index}",
        "lineage": list(lineage),
        "source": source,
        "license": license_data,
    }
    common_result = {
        "path": str(destination),
        "sidecar_path": str(_sidecar_path(destination)),
        "fingerprint": fingerprint,
        "source": source,
        "license": license_data,
        "lineage": list(lineage),
        "lineage_input": None
        if is_programmatic_title
        else {"path": str(source_path), "sha256": input_hash},
        "registration": registration,
        "registration_ready": registration_ready,
    }

    cached = _validated_cache(
        destination,
        fingerprint=fingerprint,
        expected_registration=registration,
        media_kind="video",
        expected_duration=effective_duration,
    )
    if cached is not None:
        return {**common_result, **cached, "reused": True}

    temp_media = _temporary_sibling(destination)
    try:
        command = _video_command(
            source_path=source_path,
            output_path=temp_media,
            duration=effective_duration,
            frame_count=frame_count,
            style=normalized_style,
            unit_index=unit_index,
            font_path=font_path,
        )
        _run_ffmpeg(
            command,
            label=f"{normalized_style} unit {unit_index}",
            timeout=max(60, int(effective_duration * 20)),
        )
        if not is_programmatic_title and sha256_file(source_path) != input_hash:
            raise LocalMediaError(f"input image changed during render: {source_path.name}")
        if (
            font_path is not None
            and sha256_file(font_path) != fingerprint_payload["font"]["sha256"]
        ):
            raise LocalMediaError(f"font changed during render: {font_path.name}")
        media_probe = probe_media(temp_media)
        _assert_video_spec(media_probe, effective_duration)
        output_hash = sha256_file(temp_media)
        sidecar = {
            "schema_version": SCHEMA_VERSION,
            "fingerprint": fingerprint,
            "fingerprint_payload": fingerprint_payload,
            "output_sha256": output_hash,
            "probe": _probe_summary(media_probe),
            "registration": registration,
        }
        _publish_with_sidecar(temp_media, destination, sidecar)
        return {
            **common_result,
            "sha256": output_hash,
            "probe": media_probe,
            "reused": False,
        }
    finally:
        _unlink_if_exists(temp_media)


def synthesize_sfx(output_dir: str | Path) -> dict[str, dict[str, Any]]:
    """Create deterministic 48kHz stereo PCM sound-design cues locally."""

    directory = Path(output_dir).expanduser().resolve()
    _assert_not_tmp(directory)
    directory.mkdir(parents=True, exist_ok=True)
    if not directory.is_dir():
        raise NotADirectoryError(f"SFX output is not a directory: {directory}")

    # Expressions intentionally avoid random/noise generators so byte hashes
    # stay stable across repeated runs on the same ffmpeg build.
    cue_specs: dict[str, dict[str, Any]] = {
        "impact": {
            "duration": 0.8,
            "expression": "0.72*sin(2*PI*(54+138*exp(-8*t))*t)*exp(-6.5*t)",
        },
        "alarm_glitch": {
            "duration": 1.92,
            "expression": (
                "0.24*sin(2*PI*(630+150*floor(mod(t\\,0.48)/0.24))*t)*lt(mod(t\\,0.24)\\,0.17)"
            ),
        },
        "riser": {
            "duration": 2.4,
            "expression": "0.20*sin(2*PI*(92*t+176*t*t))*t/2.4",
        },
        "collapse": {
            "duration": 1.6,
            "expression": (
                "0.50*sin(2*PI*(45+26*t)*t)*exp(-1.45*t)+0.14*sin(2*PI*173*t)*exp(-4.2*t)"
            ),
        },
    }

    results: dict[str, dict[str, Any]] = {}
    for cue_name, spec in cue_specs.items():
        duration = float(spec["duration"])
        expression = str(spec["expression"])
        destination = _prepare_output_path(directory / f"{cue_name}.wav", suffix=".wav")
        fingerprint_payload = {
            "schema_version": SCHEMA_VERSION,
            "operation": "synthesize_sfx",
            "cue": cue_name,
            "expression": expression,
            "duration_seconds": duration,
            "output_spec": {
                "codec": "pcm_s16le",
                "sample_rate": AUDIO_SAMPLE_RATE,
                "channels": 2,
                "channel_layout": "stereo",
                "video_streams": 0,
            },
        }
        fingerprint = _json_fingerprint(fingerprint_payload)
        source = {
            "kind": "owned_audio",
            "provider": "lumeri_local_ffmpeg",
            "role": "sfx",
            "cue": cue_name,
            "cost_usd": 0.0,
            "generator": "echo_local_media",
        }
        license_data = {"basis": "project_created_programmatic_audio"}
        registration = {
            "kind": "audio",
            "path": str(destination),
            "summary": f"Echo Protocol local SFX: {cue_name}",
            "lineage": [],
            "source": source,
            "license": license_data,
        }
        common_result = {
            "path": str(destination),
            "sidecar_path": str(_sidecar_path(destination)),
            "fingerprint": fingerprint,
            "source": source,
            "license": license_data,
            "lineage": [],
            "registration": registration,
            "registration_ready": True,
        }
        cached = _validated_cache(
            destination,
            fingerprint=fingerprint,
            expected_registration=registration,
            media_kind="audio",
            expected_duration=duration,
        )
        if cached is not None:
            results[cue_name] = {**common_result, **cached, "reused": True}
            continue

        temp_media = _temporary_sibling(destination)
        try:
            sample_count = round(duration * AUDIO_SAMPLE_RATE)
            command = [
                ffmpeg_path(),
                "-hide_banner",
                "-loglevel",
                "error",
                "-nostdin",
                "-y",
                "-f",
                "lavfi",
                "-i",
                f"aevalsrc=exprs={expression}:s={AUDIO_SAMPLE_RATE}:d={duration:.9f}",
                "-frames:a",
                str(sample_count),
                "-ar",
                str(AUDIO_SAMPLE_RATE),
                "-ac",
                "2",
                "-channel_layout",
                "stereo",
                "-c:a",
                "pcm_s16le",
                "-map_metadata",
                "-1",
                "-fflags",
                "+bitexact",
                "-flags:a",
                "+bitexact",
                str(temp_media),
            ]
            _run_ffmpeg(command, label=f"SFX {cue_name}", timeout=60)
            media_probe = probe_media(temp_media)
            _assert_audio_spec(media_probe, duration)
            output_hash = sha256_file(temp_media)
            sidecar = {
                "schema_version": SCHEMA_VERSION,
                "fingerprint": fingerprint,
                "fingerprint_payload": fingerprint_payload,
                "output_sha256": output_hash,
                "probe": _probe_summary(media_probe),
                "registration": registration,
            }
            _publish_with_sidecar(temp_media, destination, sidecar)
            results[cue_name] = {
                **common_result,
                "sha256": output_hash,
                "probe": media_probe,
                "reused": False,
            }
        finally:
            _unlink_if_exists(temp_media)
    return results


def _video_command(
    *,
    source_path: Path,
    output_path: Path,
    duration: float,
    frame_count: int,
    style: str,
    unit_index: int,
    font_path: Path | None,
) -> list[str]:
    command = [
        ffmpeg_path(),
        "-hide_banner",
        "-loglevel",
        "error",
        "-nostdin",
        "-y",
    ]
    if style == "title":
        command.extend(
            [
                "-f",
                "lavfi",
                "-i",
                f"color=c=0x050812:s={VIDEO_WIDTH}x{VIDEO_HEIGHT}:r={VIDEO_FPS}:d={duration:.9f}",
            ]
        )
    else:
        command.extend(["-loop", "1", "-framerate", str(VIDEO_FPS), "-i", str(source_path)])

    filter_chain = _style_filter(
        style=style,
        duration=duration,
        frame_count=frame_count,
        unit_index=unit_index,
        font_path=font_path,
    )
    command.extend(
        [
            "-vf",
            filter_chain,
            "-frames:v",
            str(frame_count),
            "-an",
            "-c:v",
            "libx264",
            "-preset",
            "veryfast",
            "-crf",
            "18",
            "-threads",
            "1",
            "-pix_fmt",
            "yuv420p",
            "-r",
            str(VIDEO_FPS),
            "-map_metadata",
            "-1",
            "-metadata",
            "creation_time=1970-01-01T00:00:00Z",
            "-fflags",
            "+bitexact",
            "-flags:v",
            "+bitexact",
            "-movflags",
            "+faststart",
            str(output_path),
        ]
    )
    return command


def _style_filter(
    *,
    style: str,
    duration: float,
    frame_count: int,
    unit_index: int,
    font_path: Path | None,
) -> str:
    if style == "title":
        if font_path is None:
            raise LocalMediaError("PingFang is required for the Echo Protocol title card")
        font = _escape_filter_value(str(font_path))
        fade_out_start = max(0.0, duration - min(0.35, duration / 2))
        return ",".join(
            [
                f"drawtext=fontfile='{font}':text='回声协议':fontsize=126:fontcolor=white:"
                "x=(w-text_w)/2:y=(h-text_h)/2-78",
                f"drawtext=fontfile='{font}':text='ECHO PROTOCOL':fontsize=34:"
                "fontcolor=0x83E9FF:x=(w-text_w)/2:y=(h-text_h)/2+88",
                "drawbox=x=760:y=681:w=400:h=2:color=0x83E9FF@0.7:t=fill",
                f"fade=t=in:st=0:d={min(0.35, duration / 2):.6f}:color=black",
                f"fade=t=out:st={fade_out_start:.6f}:d={duration - fade_out_start:.6f}:color=black",
                "setsar=1",
                "format=yuv420p",
            ]
        )

    phase = (unit_index % 17) * 0.37
    last = max(1, frame_count - 1)
    zoom_amount = 0.055 + (unit_index % 5) * 0.006
    pan_x = 0.38 + (unit_index % 4) * 0.07
    pan_y = 0.42 + (unit_index % 3) * 0.06
    motion = (
        f"scale=2304:1296:force_original_aspect_ratio=increase,"
        "crop=2304:1296,"
        f"zoompan=z='1+{zoom_amount:.6f}*on/{last}':"
        f"x='(iw-iw/zoom)*({pan_x:.6f}+0.08*sin({phase:.6f}+on/{last}*PI))':"
        f"y='(ih-ih/zoom)*({pan_y:.6f}+0.06*cos({phase:.6f}+on/{last}*PI))':"
        f"d=1:s={VIDEO_WIDTH}x{VIDEO_HEIGHT}:fps={VIDEO_FPS}"
    )
    filters = [motion]
    if style == "ken_burns":
        filters.append("eq=contrast=1.035:saturation=1.045")
    elif style == "hud":
        if font_path is None:
            raise LocalMediaError("PingFang is required for the Echo Protocol HUD")
        font = _escape_filter_value(str(font_path))
        filters.extend(
            [
                "eq=contrast=1.08:saturation=0.78:brightness=-0.025",
                "drawbox=x=72:y=72:w=1776:h=936:color=0x70EEFF@0.28:t=3",
                "drawbox=x=96:y=96:w=360:h=68:color=0x061822@0.72:t=fill",
                f"drawtext=fontfile='{font}':text='曙光系统 / 因果预测':fontsize=28:"
                "fontcolor=0x8EF6FF:x=116:y=113",
                "drawbox=x=96:y=912:w=690:h=3:color=0xFF4A61@0.86:t=fill",
            ]
        )
    elif style == "hero":
        filters.extend(
            [
                "eq=contrast=1.13:saturation=0.84:brightness=-0.018:gamma=0.96",
                "vignette=angle=PI/5.4",
            ]
        )
    elif style == "memory_fold":
        filters.extend(
            [
                f"rotate='0.011*sin(2*PI*t/{duration:.9f})':fillcolor=0x050812",
                "lenscorrection=k1=-0.09:k2=0.035",
                "eq=contrast=1.08:saturation=0.58:gamma=0.92",
                "colorchannelmixer=rr=0.96:gg=1.02:bb=1.10",
            ]
        )
    elif style == "white_collapse":
        fade_duration = min(0.55, duration / 2)
        filters.extend(
            [
                "eq=contrast=1.11:saturation=0.68:brightness=0.03",
                f"fade=t=out:st={duration - fade_duration:.6f}:d={fade_duration:.6f}:color=white",
            ]
        )
    elif style == "iris":
        filters.extend(
            [
                "eq=contrast=1.08:saturation=0.72:brightness=-0.02",
                f"vignette=angle='PI/7+1.24*t/{duration:.9f}':eval=frame",
            ]
        )
    filters.extend(["setsar=1", "format=yuv420p"])
    return ",".join(filters)


def _resolve_pingfang_font() -> Path:
    override = os.environ.get("LUMERI_PINGFANG_FONT")
    candidates = ((Path(override).expanduser(),) if override else ()) + PINGFANG_FONT_CANDIDATES
    for candidate in candidates:
        if candidate.is_file():
            return candidate.resolve()
    checked = ", ".join(str(candidate) for candidate in candidates) or "no candidates configured"
    raise LocalMediaError(
        "A complete CJK font is required for Chinese HUD/title rendering and was not found; "
        f"checked: {checked}. Set LUMERI_PINGFANG_FONT to an installed font file."
    )


def _prepare_output_path(path: str | Path, *, suffix: str) -> Path:
    destination = Path(path).expanduser().resolve()
    if destination.suffix.lower() != suffix:
        raise ValueError(f"output must use the {suffix} extension: {destination}")
    _assert_not_tmp(destination)
    destination.parent.mkdir(parents=True, exist_ok=True)
    if not destination.parent.is_dir():
        raise NotADirectoryError(f"output parent is not a directory: {destination.parent}")
    return destination


def _assert_not_tmp(path: Path) -> None:
    resolved = path.resolve()
    for forbidden in (Path("/tmp"), Path("/private/tmp")):
        try:
            resolved.relative_to(forbidden)
        except ValueError:
            continue
        raise ValueError(
            f"local production media must not be written under {forbidden}: {resolved}"
        )


def _temporary_sibling(destination: Path) -> Path:
    token = uuid.uuid4().hex
    return destination.with_name(
        f".{destination.stem}.{os.getpid()}.{token}.part{destination.suffix}"
    )


def _sidecar_path(media_path: Path) -> Path:
    return media_path.with_suffix(media_path.suffix + ".lumeri.json")


def _json_fingerprint(value: dict[str, Any]) -> str:
    encoded = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def _validated_cache(
    media_path: Path,
    *,
    fingerprint: str,
    expected_registration: dict[str, Any],
    media_kind: str,
    expected_duration: float,
) -> dict[str, Any] | None:
    sidecar_path = _sidecar_path(media_path)
    if not media_path.is_file() or not sidecar_path.is_file():
        return None
    try:
        sidecar = json.loads(sidecar_path.read_text(encoding="utf-8"))
        if not isinstance(sidecar, dict) or sidecar.get("fingerprint") != fingerprint:
            return None
        fingerprint_payload = sidecar.get("fingerprint_payload")
        if not isinstance(fingerprint_payload, dict):
            return None
        if _json_fingerprint(fingerprint_payload) != fingerprint:
            return None
        if sidecar.get("registration") != expected_registration:
            return None
        actual_hash = sha256_file(media_path)
        if actual_hash != sidecar.get("output_sha256"):
            return None
        media_probe = probe_media(media_path)
        if media_kind == "video":
            _assert_video_spec(media_probe, expected_duration)
        elif media_kind == "audio":
            _assert_audio_spec(media_probe, expected_duration)
        else:
            raise ValueError(f"unsupported cache media kind: {media_kind}")
    except (OSError, ValueError, LocalMediaError, json.JSONDecodeError):
        return None
    return {"sha256": actual_hash, "probe": media_probe}


def _publish_with_sidecar(temp_media: Path, destination: Path, sidecar: dict[str, Any]) -> None:
    sidecar_path = _sidecar_path(destination)
    temp_sidecar = _temporary_sibling(sidecar_path)
    try:
        temp_sidecar.write_text(
            json.dumps(sidecar, ensure_ascii=False, sort_keys=True, indent=2) + "\n",
            encoding="utf-8",
        )
        with temp_sidecar.open("rb") as handle:
            os.fsync(handle.fileno())
        _fsync_file(temp_media)
        os.replace(temp_media, destination)
        os.replace(temp_sidecar, sidecar_path)
        _fsync_directory(destination.parent)
    except OSError as exc:
        raise LocalMediaError(f"failed to atomically publish {destination.name}: {exc}") from exc
    finally:
        _unlink_if_exists(temp_sidecar)


def _run_ffmpeg(command: list[str], *, label: str, timeout: int) -> None:
    try:
        completed = subprocess.run(
            command,
            check=False,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise LocalMediaError(f"ffmpeg failed while baking {label}: {exc}") from exc
    if completed.returncode != 0:
        detail = (completed.stderr or completed.stdout or "unknown ffmpeg error").strip()[-3000:]
        raise LocalMediaError(f"ffmpeg failed while baking {label}: {detail}")


def _fsync_file(path: Path) -> None:
    try:
        with path.open("rb") as handle:
            os.fsync(handle.fileno())
    except OSError as exc:
        raise LocalMediaError(f"failed to flush local media {path.name}: {exc}") from exc


def _fsync_directory(directory: Path) -> None:
    # APFS supports directory fsync. Some test/container filesystems reject it;
    # file fsync plus same-directory atomic renames remain the best contract.
    descriptor: int | None = None
    try:
        descriptor = os.open(directory, os.O_RDONLY)
        os.fsync(descriptor)
    except OSError:
        pass
    finally:
        if descriptor is not None:
            os.close(descriptor)


def _assert_video_spec(metadata: dict[str, Any], expected_duration: float) -> None:
    streams = metadata.get("streams") if isinstance(metadata.get("streams"), list) else []
    videos = [stream for stream in streams if stream.get("codec_type") == "video"]
    audios = [stream for stream in streams if stream.get("codec_type") == "audio"]
    if len(videos) != 1 or audios:
        raise LocalMediaError("local motion output must contain one video stream and no audio")
    video = videos[0]
    checks = {
        "codec": str(video.get("codec_name") or "") == "h264",
        "width": int(video.get("width") or 0) == VIDEO_WIDTH,
        "height": int(video.get("height") or 0) == VIDEO_HEIGHT,
        "pixel_format": str(video.get("pix_fmt") or "") == "yuv420p",
        "fps": abs(
            _fraction_value(video.get("avg_frame_rate") or video.get("r_frame_rate")) - VIDEO_FPS
        )
        < 0.001,
        "duration": abs(_duration_value(metadata) - expected_duration) <= (1.0 / VIDEO_FPS) + 0.001,
    }
    failed = [name for name, passed in checks.items() if not passed]
    if failed:
        raise LocalMediaError(f"local motion output failed spec checks: {', '.join(failed)}")


def _assert_audio_spec(metadata: dict[str, Any], expected_duration: float) -> None:
    streams = metadata.get("streams") if isinstance(metadata.get("streams"), list) else []
    audios = [stream for stream in streams if stream.get("codec_type") == "audio"]
    videos = [stream for stream in streams if stream.get("codec_type") == "video"]
    if len(audios) != 1 or videos:
        raise LocalMediaError("local SFX output must contain one audio stream and no video")
    audio = audios[0]
    checks = {
        "codec": str(audio.get("codec_name") or "") == "pcm_s16le",
        "sample_rate": int(audio.get("sample_rate") or 0) == AUDIO_SAMPLE_RATE,
        "channels": int(audio.get("channels") or 0) == 2,
        # ffprobe 8 omits channel_layout for ordinary two-channel PCM WAV even
        # when ffmpeg was explicitly given ``-channel_layout stereo``.  Two
        # channels in this container are the interoperable stereo contract;
        # reject an explicit conflicting layout, but not the absent field.
        "channel_layout": str(audio.get("channel_layout") or "stereo") == "stereo",
        "duration": abs(_duration_value(metadata) - expected_duration)
        <= (1.0 / AUDIO_SAMPLE_RATE) + 0.001,
    }
    failed = [name for name, passed in checks.items() if not passed]
    if failed:
        raise LocalMediaError(f"local SFX output failed spec checks: {', '.join(failed)}")


def _duration_value(metadata: dict[str, Any]) -> float:
    raw = (metadata.get("format") or {}).get("duration")
    if raw is None:
        streams = metadata.get("streams") if isinstance(metadata.get("streams"), list) else []
        raw = next(
            (stream.get("duration") for stream in streams if stream.get("duration") is not None), 0
        )
    try:
        return float(raw)
    except (TypeError, ValueError):
        return 0.0


def _fraction_value(value: Any) -> float:
    try:
        numerator, denominator = str(value).split("/", 1)
        return float(numerator) / float(denominator)
    except (TypeError, ValueError, ZeroDivisionError):
        return 0.0


def _probe_summary(metadata: dict[str, Any]) -> dict[str, Any]:
    streams = metadata.get("streams") if isinstance(metadata.get("streams"), list) else []
    return {
        "duration": _duration_value(metadata),
        "streams": [
            {
                key: stream.get(key)
                for key in (
                    "codec_type",
                    "codec_name",
                    "width",
                    "height",
                    "pix_fmt",
                    "avg_frame_rate",
                    "sample_rate",
                    "channels",
                    "channel_layout",
                )
                if stream.get(key) is not None
            }
            for stream in streams
        ],
    }


def _escape_filter_value(value: str) -> str:
    return value.replace("\\", "\\\\").replace(":", "\\:").replace("'", "\\'")


def _unlink_if_exists(path: Path) -> None:
    with suppress(OSError):
        path.unlink(missing_ok=True)


__all__ = [
    "AUDIO_SAMPLE_RATE",
    "LocalMediaError",
    "SCHEMA_VERSION",
    "SUPPORTED_IMAGE_STYLES",
    "VIDEO_FPS",
    "VIDEO_HEIGHT",
    "VIDEO_WIDTH",
    "probe_media",
    "render_image_motion",
    "sha256_file",
    "synthesize_sfx",
]
