"""arrange_timeline: sequence multiple video clips in order with optional transitions.

Transition kinds:
- ``cut``     — hard cut, no transition (default between any pair without an explicit entry).
- ``dissolve``— xfade dissolve over ``duration_sec`` (default 0.5s).
- ``wipe``    — xfade wiperight.
- ``fade``    — xfade fadeblack.

When the timeline has only ``cut`` transitions everywhere, we emit a single
ffmpeg ``concat`` demuxer pass which is fast and stream-copy-friendly when
inputs share the same codec/format. When any pair uses a real transition we
build an ``xfade`` filter graph that produces one continuous output.

Audio: each transition concurrently runs an ``acrossfade`` of the same
duration so audio doesn't pop on a visual dissolve. Silent inputs are
handled by routing through an empty audio source.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any

from gemia.tools._context import ToolContext
from gemia.tools._ffmpeg import audio_stream, ffprobe_duration, ffprobe_metadata, get_video_encoder_args, run_ffmpeg_with_progress


_XFADE_KINDS: dict[str, str] = {
    "dissolve": "fade",
    "wipe":     "wiperight",
    "fade":     "fadeblack",
}


async def dispatch(args: dict[str, Any], ctx: ToolContext) -> dict[str, Any]:
    asset_ids = list(args.get("asset_ids") or [])
    if len(asset_ids) < 1:
        raise ValueError("arrange_timeline requires at least 1 asset_id")
    if len(asset_ids) == 1:
        raise ValueError(
            "arrange_timeline with one asset is a no-op; call edit_video or copy directly"
        )

    paths: list[Path] = []
    durations: list[float] = []
    has_audio: list[bool] = []
    for aid in asset_ids:
        rec = ctx.registry.get(aid)
        if rec.kind != "video":
            raise ValueError(f"arrange_timeline input {aid} is {rec.kind!r}, expected video")
        paths.append(rec.path)
        durations.append(ffprobe_duration(rec.path))
        has_audio.append(audio_stream(ffprobe_metadata(rec.path)) is not None)

    transitions_raw = list(args.get("transitions") or [])
    transitions: dict[int, dict[str, Any]] = {}
    for entry in transitions_raw:
        if not isinstance(entry, dict):
            raise ValueError(f"transitions entries must be objects, got {entry!r}")
        idx = int(entry.get("between_index", -1))
        if idx < 0 or idx >= len(asset_ids) - 1:
            raise ValueError(
                f"transition between_index {idx} out of range for {len(asset_ids)} clips"
            )
        kind = str(entry.get("kind", "cut")).strip().lower()
        if kind not in {"cut", *_XFADE_KINDS.keys()}:
            raise ValueError(
                f"unknown transition kind {kind!r}. Known: cut, dissolve, wipe, fade"
            )
        duration_sec = float(entry.get("duration_sec", 0.5))
        if kind != "cut" and duration_sec <= 0:
            raise ValueError(f"transition duration_sec must be > 0, got {duration_sec}")
        transitions[idx] = {"kind": kind, "duration_sec": duration_sec}

    all_cut = all(
        transitions.get(i, {"kind": "cut"})["kind"] == "cut"
        for i in range(len(asset_ids) - 1)
    )

    new_id = ctx.registry.allocate_id("video")
    out_path = ctx.child_path(new_id, ".mp4")

    if all_cut:
        list_file = ctx.child_path(new_id, ".concat.txt")
        list_file.write_text(
            "\n".join(f"file '{p.resolve()}'" for p in paths) + "\n",
            encoding="utf-8",
        )
        total = sum(durations)
        cmd = [
            "ffmpeg", "-y",
            "-f", "concat", "-safe", "0",
            "-i", str(list_file),
            *get_video_encoder_args("h264"),
            "-c:a", "aac", "-b:a", "192k",
            "-movflags", "+faststart",
            str(out_path),
        ]
        try:
            await run_ffmpeg_with_progress(cmd, total_seconds=total, progress=ctx.emit_progress)
        finally:
            list_file.unlink(missing_ok=True)
        kind_summary = "cut"
        final_duration = total
    else:
        cmd, final_duration = _build_xfade_command(paths, durations, transitions, has_audio, out_path)
        await run_ffmpeg_with_progress(cmd, total_seconds=final_duration, progress=ctx.emit_progress)
        kinds_used = sorted({t["kind"] for t in transitions.values()})
        kind_summary = "+".join(kinds_used) or "cut"

    summary = (
        f"arranged {len(asset_ids)} clips ({', '.join(asset_ids)}) "
        f"with {kind_summary} transition(s) -> {final_duration:.2f}s"
    )
    record = ctx.registry.register_output(
        new_id, kind="video", path=out_path, summary=summary, lineage=list(asset_ids),
    )
    return {
        "asset_id": new_id,
        "summary": record.summary,
        "metadata": {
            "clip_count": len(asset_ids),
            "duration_sec": final_duration,
            "all_cut": all_cut,
            "transitions": [
                {"between_index": i, **transitions[i]}
                for i in sorted(transitions.keys())
            ],
        },
    }


def _build_xfade_command(
    paths: list[Path],
    durations: list[float],
    transitions: dict[int, dict[str, Any]],
    has_audio: list[bool],
    out_path: Path,
) -> tuple[list[str], float]:
    n = len(paths)
    filter_lines: list[str] = []

    # Normalize all video streams to the same SAR/FPS/format so xfade is happy.
    for i in range(n):
        filter_lines.append(f"[{i}:v]format=yuv420p,setsar=1,fps=30[v{i}]")

    # Walk transitions and assemble. xfade only operates on two inputs at a
    # time, so chain: v0 + (transition_0) v1 -> v01; v01 + (transition_1) v2
    # -> v012; ...
    cumulative_end = durations[0]
    v_label = "v0"
    for i in range(n - 1):
        next_label = f"v{i + 1}"
        t = transitions.get(i, {"kind": "cut", "duration_sec": 0.0})
        if t["kind"] == "cut":
            # Hard cut: feed via concat filter (no overlap).
            tag = f"vc{i}"
            filter_lines.append(f"[{v_label}][{next_label}]concat=n=2:v=1:a=0[{tag}]")
            v_label = tag
            cumulative_end += durations[i + 1]
        else:
            xkind = _XFADE_KINDS[t["kind"]]
            xd = float(t["duration_sec"])
            offset = cumulative_end - xd
            tag = f"vx{i}"
            filter_lines.append(
                f"[{v_label}][{next_label}]xfade=transition={xkind}:duration={xd:.3f}:offset={offset:.3f}[{tag}]"
            )
            v_label = tag
            cumulative_end += durations[i + 1] - xd

    final_duration = cumulative_end

    # Audio: only acrossfade when there's a real transition; otherwise concat.
    a_label: str | None = None
    have_any_audio = any(has_audio)
    if have_any_audio:
        # Map silent inputs to anullsrc so we always have an audio stream per input.
        for i in range(n):
            if has_audio[i]:
                filter_lines.append(f"[{i}:a]aformat=channel_layouts=stereo[a{i}]")
            else:
                filter_lines.append(
                    f"anullsrc=channel_layout=stereo:sample_rate=48000,atrim=duration={durations[i]:.3f}[a{i}]"
                )
        a_label = "a0"
        for i in range(n - 1):
            t = transitions.get(i, {"kind": "cut", "duration_sec": 0.0})
            tag = f"ac{i}" if t["kind"] == "cut" else f"ax{i}"
            if t["kind"] == "cut":
                filter_lines.append(f"[{a_label}][a{i + 1}]concat=n=2:v=0:a=1[{tag}]")
            else:
                xd = float(t["duration_sec"])
                filter_lines.append(
                    f"[{a_label}][a{i + 1}]acrossfade=d={xd:.3f}[{tag}]"
                )
            a_label = tag

    filter_complex = ";".join(filter_lines)
    cmd = [
        "ffmpeg", "-y",
    ]
    for p in paths:
        cmd += ["-i", str(p)]
    cmd += ["-filter_complex", filter_complex, "-map", f"[{v_label}]"]
    if a_label is not None:
        cmd += ["-map", f"[{a_label}]", "-c:a", "aac", "-b:a", "192k"]
    cmd += [
        *get_video_encoder_args("h264"),
        "-movflags", "+faststart",
        str(out_path),
    ]
    return cmd, final_duration


__all__ = ["dispatch"]
