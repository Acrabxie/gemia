"""Resolve 21 layer-list node graph version manifests."""
from __future__ import annotations

import json
import re
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


DEFAULT_GRAPH_VERSIONS: list[dict[str, Any]] = [
    {
        "id": "primary_balance",
        "label": "Primary balance",
        "look": "neutral_rec709",
        "nodes": ["media_input", "primary_balance", "serial_composite", "output"],
    },
    {
        "id": "alt_contrast_pop",
        "label": "Alt contrast pop",
        "look": "contrast_pop",
        "nodes": ["media_input", "contrast_curve", "soft_vignette", "serial_composite", "output"],
    },
]


def render_layer_list_node_graph_versions(
    input_paths: list[str],
    output_dir: str,
    *,
    graph_id: str = "resolve21_layer_list_graph",
    node_graph_versions: list[dict[str, Any]] | None = None,
) -> str:
    """Serialize Resolve-style layer-list node graph versions for real media clips."""
    if not input_paths:
        raise ValueError("input_paths must contain at least one media path")

    output_root = Path(output_dir).expanduser().resolve()
    output_root.mkdir(parents=True, exist_ok=True)
    sources = [_resolve_media(path) for path in input_paths]
    versions = [_normalize_version(raw, index) for index, raw in enumerate(node_graph_versions or DEFAULT_GRAPH_VERSIONS)]

    layers = []
    for index, source in enumerate(sources):
        probe = _probe_media(source)
        layers.append(
            {
                "layer_id": f"layer_{index:02d}_{_safe_id(source.stem)}",
                "track_index": index,
                "source_path": str(source),
                "source_probe": probe,
                "asset_ref": _asset_ref(source, probe),
                "enabled": True,
                "blend_mode": "normal" if index == 0 else "over",
            }
        )

    graph_versions = []
    for version_index, version in enumerate(versions):
        nodes = []
        for layer in layers:
            previous = None
            for node_index, kind in enumerate(version["nodes"]):
                node_id = f"{version['id']}:{layer['layer_id']}:{node_index:02d}_{_safe_id(kind)}"
                nodes.append(
                    {
                        "node_id": node_id,
                        "version_id": version["id"],
                        "layer_id": layer["layer_id"],
                        "kind": kind,
                        "inputs": [previous] if previous else [],
                        "asset_ref": layer["asset_ref"] if kind == "media_input" else None,
                        "parameters": _node_parameters(kind, version, layer),
                    }
                )
                previous = node_id
        graph_versions.append(
            {
                "version_id": version["id"],
                "label": version["label"],
                "look": version["look"],
                "node_count": len(nodes),
                "nodes": nodes,
                "output_node_ids": [node["node_id"] for node in nodes if node["kind"] == "output"],
            }
        )

    manifest = {
        "schema_version": 1,
        "effect": "resolve21_layer_list_node_graph_versions",
        "rendered_at": datetime.now(timezone.utc).isoformat(),
        "graph": {
            "graph_id": graph_id,
            "layer_count": len(layers),
            "version_count": len(graph_versions),
            "asset_refs": [layer["asset_ref"] for layer in layers],
        },
        "layers": layers,
        "graph_versions": graph_versions,
        "diagnostics": [
            f"{len(layers)} layer-list entries serialized across {len(graph_versions)} node graph versions",
            "each version keeps stable per-layer asset references for review and relink",
        ],
        "review_hints": [
            "confirm alternate graph versions keep identical layer asset_refs",
            "compare node_count and output_node_ids before approving color/composite variants",
            "use graph_id plus version_id as the stable Resolve 21 layer-list review key",
        ],
    }
    manifest_path = output_root / "layer_list_node_graph_versions.json"
    manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return str(manifest_path)


def _resolve_media(path: str) -> Path:
    resolved = Path(path).expanduser().resolve()
    if not resolved.exists():
        raise FileNotFoundError(f"Input media not found: {resolved}")
    return resolved


def _normalize_version(raw: dict[str, Any], index: int) -> dict[str, Any]:
    version_id = _safe_id(str(raw.get("id") or f"graph_version_{index}"))
    nodes = [str(item) for item in raw.get("nodes", []) if str(item).strip()]
    if not nodes:
        nodes = list(DEFAULT_GRAPH_VERSIONS[min(index, len(DEFAULT_GRAPH_VERSIONS) - 1)]["nodes"])
    if nodes[0] != "media_input":
        nodes.insert(0, "media_input")
    if nodes[-1] != "output":
        nodes.append("output")
    return {
        "id": version_id,
        "label": str(raw.get("label") or version_id.replace("_", " ").title()),
        "look": str(raw.get("look") or "custom"),
        "nodes": nodes,
    }


def _probe_media(path: Path) -> dict[str, Any]:
    proc = subprocess.run(
        ["ffprobe", "-v", "error", "-print_format", "json", "-show_format", "-show_streams", str(path)],
        capture_output=True,
        text=True,
        check=False,
    )
    if proc.returncode != 0:
        raise RuntimeError(f"ffprobe failed for {path}: {proc.stderr[-800:]}")
    payload = json.loads(proc.stdout or "{}")
    fmt = payload.get("format") or {}
    video = next((stream for stream in payload.get("streams", []) if stream.get("codec_type") == "video"), {})
    return {
        "duration_seconds": round(float(fmt.get("duration") or 0.0), 3),
        "size_bytes": int(fmt.get("size") or path.stat().st_size),
        "width": int(video.get("width") or 0),
        "height": int(video.get("height") or 0),
        "video_codec": str(video.get("codec_name") or ""),
    }


def _asset_ref(path: Path, probe: dict[str, Any]) -> str:
    return f"{_safe_id(path.stem)}:{path.stat().st_size}:{probe['duration_seconds']}:{probe['width']}x{probe['height']}"


def _node_parameters(kind: str, version: dict[str, Any], layer: dict[str, Any]) -> dict[str, Any]:
    if kind == "primary_balance":
        return {"look": version["look"], "exposure_offset": 0.0, "temperature_offset": 0}
    if kind == "contrast_curve":
        return {"look": version["look"], "contrast": 1.12, "pivot": 0.42}
    if kind == "soft_vignette":
        return {"radius": 0.72, "strength": 0.18}
    if kind == "serial_composite":
        return {"blend_mode": layer["blend_mode"], "track_index": layer["track_index"]}
    return {}


def _safe_id(value: str) -> str:
    return re.sub(r"[^A-Za-z0-9_-]+", "_", value.strip()).strip("_").lower() or "node"


__all__ = ["DEFAULT_GRAPH_VERSIONS", "render_layer_list_node_graph_versions"]
