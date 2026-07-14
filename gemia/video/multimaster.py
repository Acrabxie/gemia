"""Resolve 21 MultiMaster trim-pass deliverable manifests."""
from __future__ import annotations

import json
import re
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


DEFAULT_TRIM_PASSES: list[dict[str, Any]] = [
    {
        "id": "hdr10_master",
        "label": "HDR10 master",
        "target": "hdr10_pq",
        "start_trim_seconds": 0.0,
        "end_trim_seconds": 0.0,
        "peak_nits": 1000,
        "output_suffix": "hdr10",
    },
    {
        "id": "sdr_rec709_trim",
        "label": "SDR Rec.709 trim",
        "target": "sdr_rec709",
        "start_trim_seconds": 0.05,
        "end_trim_seconds": 0.05,
        "peak_nits": 100,
        "output_suffix": "sdr",
    },
]


def render_multimaster_trim_pass_manager(
    input_path: str,
    output_dir: str,
    *,
    timeline_id: str = "resolve21_multimaster_timeline",
    trim_passes: list[dict[str, Any]] | None = None,
    review_proxy_long_edge: int = 360,
) -> str:
    """Create linked HDR/SDR trim-pass deliverable metadata for one timeline."""
    source = Path(input_path).expanduser().resolve()
    if not source.exists():
        raise FileNotFoundError(f"Input media not found: {source}")
    if review_proxy_long_edge <= 0:
        raise ValueError("review_proxy_long_edge must be greater than 0")

    output_root = Path(output_dir).expanduser().resolve()
    proxies_dir = output_root / "trim_pass_proxies"
    proxies_dir.mkdir(parents=True, exist_ok=True)

    media = _probe_media(source)
    if media["duration_seconds"] <= 0:
        raise ValueError(f"Could not determine duration for {source}")

    passes = [_normalize_pass(raw, index) for index, raw in enumerate(trim_passes or DEFAULT_TRIM_PASSES)]
    deliverables: list[dict[str, Any]] = []
    diagnostics: list[str] = []
    for index, pass_spec in enumerate(passes):
        start_trim = float(pass_spec["start_trim_seconds"])
        end_trim = float(pass_spec["end_trim_seconds"])
        if start_trim + end_trim >= media["duration_seconds"]:
            raise ValueError("start_trim_seconds + end_trim_seconds must be smaller than input duration")
        start = start_trim
        duration = max(media["duration_seconds"] - start_trim - end_trim, 0.05)
        proxy = proxies_dir / f"{index:02d}_{_safe_id(pass_spec['id'])}_{pass_spec['output_suffix']}.mp4"
        _write_proxy(source, proxy, start_seconds=start, duration_seconds=duration, long_edge=review_proxy_long_edge)
        proxy_media = _probe_media(proxy)
        deliverables.append(
            {
                "index": index,
                "pass_id": pass_spec["id"],
                "label": pass_spec["label"],
                "target": pass_spec["target"],
                "linked_master_id": timeline_id,
                "trim": {
                    "start_trim_seconds": round(start_trim, 3),
                    "end_trim_seconds": round(end_trim, 3),
                    "source_in_seconds": round(start, 3),
                    "source_out_seconds": round(start + duration, 3),
                    "deliverable_duration_seconds": round(duration, 3),
                },
                "color_delivery": {
                    "peak_nits": int(pass_spec["peak_nits"]),
                    "transfer": "pq" if "hdr" in pass_spec["target"].lower() else "gamma_2_4",
                    "gamut": "rec2020" if "hdr" in pass_spec["target"].lower() else "rec709",
                },
                "review_proxy_path": str(proxy),
                "review_proxy_probe": proxy_media,
                "asset_identity": f"{timeline_id}:{pass_spec['id']}:{_fingerprint(source, media)}",
            }
        )
        diagnostics.append(
            f"{pass_spec['id']} trims {start_trim:.3f}s start and {end_trim:.3f}s end for {duration:.3f}s deliverable"
        )

    manifest = {
        "schema_version": 1,
        "effect": "resolve21_multimaster_trim_pass_manager",
        "rendered_at": datetime.now(timezone.utc).isoformat(),
        "timeline": {
            "timeline_id": timeline_id,
            "source_path": str(source),
            "source_probe": media,
            "linked_deliverable_count": len(deliverables),
            "source_fingerprint": _fingerprint(source, media),
        },
        "deliverables": deliverables,
        "sync_groups": [
            {
                "group_id": f"{timeline_id}:trim-pass-links",
                "members": [item["pass_id"] for item in deliverables],
                "sync_basis": "same_source_timeline_with_independent_head_tail_trims",
            }
        ],
        "diagnostics": diagnostics,
        "review_hints": [
            "compare HDR and SDR proxy durations before approving deliverables",
            "confirm all trim pass asset identities share the same source fingerprint",
            "review head/tail trims against the source timeline action safe area",
        ],
    }
    manifest_path = output_root / "multimaster_trim_pass_manifest.json"
    manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return str(manifest_path)


def _normalize_pass(raw: dict[str, Any], index: int) -> dict[str, Any]:
    target = str(raw.get("target") or ("hdr10_pq" if index == 0 else "sdr_rec709"))
    pass_id = _safe_id(str(raw.get("id") or target or f"trim_pass_{index}"))
    peak = int(raw.get("peak_nits") or (1000 if "hdr" in target.lower() else 100))
    start_trim = max(float(raw.get("start_trim_seconds") or 0.0), 0.0)
    end_trim = max(float(raw.get("end_trim_seconds") or 0.0), 0.0)
    return {
        "id": pass_id,
        "label": str(raw.get("label") or pass_id.replace("_", " ").title()),
        "target": target,
        "start_trim_seconds": start_trim,
        "end_trim_seconds": end_trim,
        "peak_nits": peak,
        "output_suffix": _safe_id(str(raw.get("output_suffix") or target)),
    }


def _probe_media(path: Path) -> dict[str, Any]:
    proc = subprocess.run(
        [
            "ffprobe",
            "-v",
            "error",
            "-print_format",
            "json",
            "-show_format",
            "-show_streams",
            str(path),
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    if proc.returncode != 0:
        raise RuntimeError(f"ffprobe failed for {path}: {proc.stderr[-800:]}")
    payload = json.loads(proc.stdout or "{}")
    fmt = payload.get("format") or {}
    video = next((s for s in payload.get("streams", []) if s.get("codec_type") == "video"), {})
    audio = next((s for s in payload.get("streams", []) if s.get("codec_type") == "audio"), None)
    return {
        "duration_seconds": round(float(fmt.get("duration") or 0.0), 3),
        "size_bytes": int(fmt.get("size") or path.stat().st_size),
        "width": int(video.get("width") or 0),
        "height": int(video.get("height") or 0),
        "video_codec": str(video.get("codec_name") or ""),
        "has_audio": audio is not None,
        "audio_codec": str((audio or {}).get("codec_name") or ""),
    }


def _write_proxy(source: Path, output: Path, *, start_seconds: float, duration_seconds: float, long_edge: int) -> None:
    output.parent.mkdir(parents=True, exist_ok=True)
    scale = f"scale='if(gte(iw,ih),{long_edge},-2)':'if(gte(iw,ih),-2,{long_edge})'"
    cmd = [
        "ffmpeg",
        "-y",
        "-ss",
        f"{start_seconds:.3f}",
        "-i",
        str(source),
        "-t",
        f"{duration_seconds:.3f}",
        "-vf",
        scale,
        "-c:v",
        "libx264",
        "-pix_fmt",
        "yuv420p",
        "-c:a",
        "aac",
        "-movflags",
        "+faststart",
        str(output),
    ]
    proc = subprocess.run(cmd, capture_output=True, text=True, check=False)
    if proc.returncode != 0:
        raise RuntimeError(f"ffmpeg proxy render failed for {source}: {proc.stderr[-1000:]}")


def _fingerprint(path: Path, media: dict[str, Any]) -> str:
    return f"{path.name}:{path.stat().st_size}:{media['duration_seconds']}:{media['width']}x{media['height']}"


def _safe_id(value: str) -> str:
    return re.sub(r"[^A-Za-z0-9_-]+", "_", value.strip()).strip("_").lower() or "trim_pass"


__all__ = ["DEFAULT_TRIM_PASSES", "render_multimaster_trim_pass_manager"]
