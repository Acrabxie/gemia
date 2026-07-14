"""detect_beats: tempo/beat/onset analysis and beat-aligned cut suggestions.

Read-only analysis over an audio or video asset. Returns tempo (BPM), beat
times, optional onset times, and a list of suggested cut points on the beat —
the raw material for cutting a montage to music. Produces no new assets.
"""
from __future__ import annotations

import os
import subprocess
import tempfile
from pathlib import Path
from typing import Any

from gemia.audio.analysis import beat_info, detect_onsets, suggest_cut_points
from gemia.tools._context import ToolContext

_MAX = 256


def _resolve_audio(media_path: Path) -> tuple[str, str | None]:
    """Return (audio_path, tmp_to_cleanup); extract a mono WAV from video input."""
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


async def dispatch(args: dict[str, Any], ctx: ToolContext) -> dict[str, Any]:
    asset_id = args.get("asset_id")
    include_onsets = bool(args.get("include_onsets", False))
    cut_every = max(1, int(args.get("cut_every", 1)))

    def _err(msg: str) -> dict[str, Any]:
        return {"asset_id": asset_id or "unknown", "tempo_bpm": 0.0,
                "beat_count": 0, "beats": [], "cut_points": [], "summary": f"Error: {msg}"}

    if not asset_id:
        return _err("asset_id is required.")
    if not ctx.registry.contains(asset_id):
        return _err(f"asset {asset_id} not found in registry.")

    path = ctx.registry.get(asset_id).path
    tmp: str | None = None
    try:
        audio_path, tmp = _resolve_audio(path)
        info = beat_info(audio_path)
        tempo_bpm = float(info.get("tempo_bpm", 0.0))
        beats = [round(float(b), 4) for b in info.get("beats", [])][:_MAX]
        cut_points = [round(float(c), 4) for c in
                      suggest_cut_points(audio_path, source="beat",
                                         every=cut_every, max_points=_MAX)]
        onsets: list[float] = []
        if include_onsets:
            onsets = [round(float(o), 4) for o in detect_onsets(audio_path)][:_MAX]
    except Exception as exc:  # noqa: BLE001
        return _err(f"could not analyze {asset_id}: {exc}")
    finally:
        if tmp:
            try:
                os.unlink(tmp)
            except OSError:
                pass

    summary = (f"Detected {tempo_bpm:.1f} BPM, {len(beats)} beats; "
               f"{len(cut_points)} cut point(s) on the beat")
    if include_onsets:
        summary += f"; {len(onsets)} onset(s)"
    summary += "."

    result: dict[str, Any] = {
        "asset_id": asset_id,
        "tempo_bpm": round(tempo_bpm, 2),
        "beat_count": len(beats),
        "beats": beats,
        "cut_points": cut_points,
        "summary": summary,
    }
    if include_onsets:
        result["onsets"] = onsets
    return result
