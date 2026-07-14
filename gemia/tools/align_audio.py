"""align_audio: detect time offsets between multiple audio/video assets.

Read-only analysis. Aligns each asset to a reference by waveform
cross-correlation (see gemia.audio.analysis.align_offset) and returns the
offset plus a natural-language sync suggestion. Produces no new assets.
"""
from __future__ import annotations

import os
import subprocess
import tempfile
from pathlib import Path
from typing import Any

from gemia.audio.analysis import align_offset
from gemia.tools._context import ToolContext


def _resolve_audio(media_path: Path) -> tuple[str, str | None]:
    """Return (audio_path, tmp_to_cleanup).

    If ``media_path`` carries a video stream, extract a mono 22.05 kHz WAV to a
    temp file and return its path (plus the temp path so the caller can delete
    it). Otherwise return the original path and ``None``.
    """
    media_path = Path(media_path)
    try:
        probe = subprocess.run(
            ["ffprobe", "-v", "error", "-select_streams", "v:0",
             "-show_entries", "stream=codec_type",
             "-of", "default=noprint_wrappers=1:nokey=1", str(media_path)],
            capture_output=True, text=True, timeout=10,
        )
        has_video = "video" in probe.stdout
    except Exception:
        has_video = False

    if not has_video:
        return str(media_path), None

    fd, tmp = tempfile.mkstemp(suffix=".wav")
    os.close(fd)
    subprocess.run(
        ["ffmpeg", "-y", "-i", str(media_path), "-vn", "-ac", "1",
         "-ar", "22050", tmp],
        capture_output=True, check=True,
    )
    return tmp, tmp


def _path_for(ctx: ToolContext, asset_id: str) -> Path | None:
    if not ctx.registry.contains(asset_id):
        return None
    return ctx.registry.get(asset_id).path


async def dispatch(args: dict[str, Any], ctx: ToolContext) -> dict[str, Any]:
    reference_asset_id = args.get("reference_asset_id")
    asset_ids = args.get("asset_ids")
    max_offset_sec = args.get("max_offset_sec")

    if not reference_asset_id or not isinstance(asset_ids, list) or not asset_ids:
        return {
            "reference": reference_asset_id or "unknown",
            "alignments": [],
            "summary": "Error: reference_asset_id and a non-empty asset_ids array are required.",
        }

    ref_path = _path_for(ctx, reference_asset_id)
    if ref_path is None:
        return {
            "reference": reference_asset_id,
            "alignments": [],
            "summary": f"Error: reference asset {reference_asset_id} not found in registry.",
        }

    cleanup: list[str] = []
    alignments: list[dict[str, Any]] = []
    try:
        ref_audio, ref_tmp = _resolve_audio(ref_path)
        if ref_tmp:
            cleanup.append(ref_tmp)

        for asset_id in asset_ids:
            path = _path_for(ctx, asset_id)
            if path is None:
                alignments.append({
                    "asset_id": asset_id, "offset_sec": 0.0, "confidence": 0.0,
                    "suggestion": f"Asset {asset_id} not found in registry.",
                })
                continue
            try:
                audio_path, tmp = _resolve_audio(path)
                if tmp:
                    cleanup.append(tmp)
                result = align_offset(ref_audio, audio_path, sr=22050,
                                      max_offset_sec=max_offset_sec)
                off = float(result["offset_sec"])
                conf = float(result["confidence"])
                if abs(off) < 0.01:
                    suggestion = (f"{asset_id} is already aligned with the reference "
                                  f"(offset {off:+.3f}s).")
                elif off > 0:
                    suggestion = (f"{asset_id} lags the reference by {off:.3f}s — "
                                  f"trim {off:.3f}s from its head, or prepend {off:.3f}s "
                                  f"of silence to the reference, to sync.")
                else:
                    suggestion = (f"{asset_id} leads the reference by {abs(off):.3f}s — "
                                  f"trim {abs(off):.3f}s from the reference's head, or "
                                  f"prepend {abs(off):.3f}s of silence to {asset_id}, to sync.")
                alignments.append({
                    "asset_id": asset_id, "offset_sec": round(off, 4),
                    "confidence": conf, "suggestion": suggestion,
                })
            except Exception as exc:  # noqa: BLE001
                alignments.append({
                    "asset_id": asset_id, "offset_sec": 0.0, "confidence": 0.0,
                    "suggestion": f"Could not analyze {asset_id}: {exc}",
                })
    finally:
        for tmp in cleanup:
            try:
                os.unlink(tmp)
            except OSError:
                pass

    confident = sum(1 for a in alignments if a["confidence"] > 0.5)
    return {
        "reference": reference_asset_id,
        "alignments": alignments,
        "summary": (f"Measured offsets for {len(alignments)} asset(s) against "
                    f"{reference_asset_id}; {confident} with confident (>0.5) sync."),
    }
