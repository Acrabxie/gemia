"""Resolve 21 Fusion USD/Hydra toolset manifests."""
from __future__ import annotations

import json
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from gemia.video.timeline_assets import probe_media


DEFAULT_HYDRA_DELEGATES: list[dict[str, Any]] = [
    {"id": "storm_preview", "purpose": "interactive viewport", "quality": "draft"},
    {"id": "karma_final", "purpose": "final lookdev review", "quality": "final"},
]


def render_fusion_usd_hydra_toolset_manifest(
    input_paths: list[str],
    output_dir: str,
    *,
    stage_id: str = "resolve21_fusion_usd_hydra_toolset",
    hydra_delegates: list[dict[str, Any]] | None = None,
    frame_range: tuple[int, int] | None = None,
    meters_per_unit: float = 1.0,
) -> str:
    """Emit a Fusion-style USD stage manifest with Hydra preview/final delegates."""
    if not input_paths:
        raise ValueError("input_paths must contain at least one media path")
    output_root = Path(output_dir).expanduser().resolve()
    output_root.mkdir(parents=True, exist_ok=True)
    stage_name = _safe_id(stage_id)
    delegates = [_normalize_delegate(raw, index) for index, raw in enumerate(hydra_delegates or DEFAULT_HYDRA_DELEGATES)]
    start_frame, end_frame = _normalize_frame_range(frame_range)
    meters = max(float(meters_per_unit), 0.001)

    layers = []
    references = []
    for index, raw_path in enumerate(input_paths):
        source = Path(raw_path).expanduser().resolve()
        if not source.exists():
            raise FileNotFoundError(f"Input media not found: {source}")
        if not source.is_file():
            raise OSError(f"Input media is not a file: {source}")
        probe = probe_media(str(source))
        prim_name = f"media_plane_{index:02d}_{_safe_id(source.stem)}"
        asset_ref = _asset_ref(source, probe)
        layer = {
            "layer_id": prim_name,
            "prim_path": f"/{stage_name}/shots/{prim_name}",
            "source_path": str(source),
            "asset_ref": asset_ref,
            "source_probe": probe,
            "usd_prim": {
                "type": "Xform",
                "purpose": "render",
                "kind": "component",
                "payload": f"{asset_ref}.usd",
                "variant_sets": {
                    "resolution": _resolution_variant(probe),
                    "media_role": "video_plate" if probe.get("duration") else "still_plate",
                },
            },
            "fusion_loader": {
                "tool": "USDStageLoader",
                "hydra_delegate": delegates[0]["delegate_id"],
                "prim_path": f"/{stage_name}/shots/{prim_name}",
            },
        }
        layers.append(layer)
        references.append({"asset_ref": asset_ref, "prim_path": layer["prim_path"], "source_path": str(source)})

    manifest = {
        "schema_version": 1,
        "effect": "resolve21_fusion_usd_hydra_toolset_manifest",
        "rendered_at": datetime.now(timezone.utc).isoformat(),
        "stage": {
            "stage_id": stage_name,
            "root_prim": f"/{stage_name}",
            "meters_per_unit": round(meters, 4),
            "up_axis": "Y",
            "frame_range": [start_frame, end_frame],
            "clip_count": len(layers),
        },
        "hydra_delegates": delegates,
        "usd_layers": layers,
        "stage_references": references,
        "fusion_tools": _fusion_tools(stage_name, delegates),
        "diagnostics": [
            f"{len(layers)} real media clips mapped into USD prim references",
            f"{len(delegates)} Hydra delegates configured for Fusion preview/final review",
        ],
        "review_hints": [
            "confirm USD payload paths are resolved by the target workstation before rendering",
            "validate Hydra delegate availability in the Resolve/Fusion environment",
            "preserve asset_ref values when replacing local media with real OpenUSD assets",
        ],
    }
    manifest_path = output_root / "fusion_usd_hydra_toolset_manifest.json"
    manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return str(manifest_path)


def _normalize_delegate(raw: dict[str, Any], index: int) -> dict[str, Any]:
    delegate_id = _safe_id(str(raw.get("id") or raw.get("delegate_id") or f"delegate_{index}"))
    purpose = str(raw.get("purpose") or "viewport")
    quality = str(raw.get("quality") or "draft").lower()
    if quality not in {"draft", "balanced", "final"}:
        quality = "draft"
    return {
        "delegate_id": delegate_id,
        "purpose": purpose,
        "quality": quality,
        "supports_motion_blur": quality == "final",
        "supports_materialx": quality in {"balanced", "final"},
    }


def _normalize_frame_range(frame_range: tuple[int, int] | None) -> tuple[int, int]:
    if frame_range is None:
        return (1, 120)
    start = int(frame_range[0])
    end = int(frame_range[1])
    if end < start:
        start, end = end, start
    if start < 0:
        start = 0
    return start, max(end, start)


def _resolution_variant(probe: dict[str, Any]) -> str:
    width = int(probe.get("width") or 0)
    height = int(probe.get("height") or 0)
    if width >= 3840 or height >= 2160:
        return "uhd"
    if width >= 1920 or height >= 1080:
        return "hd"
    return "proxy"


def _fusion_tools(stage_name: str, delegates: list[dict[str, Any]]) -> list[dict[str, Any]]:
    tools = [
        {
            "tool_id": "usd_stage_loader",
            "tool": "USDStageLoader",
            "root_prim": f"/{stage_name}",
            "active_delegate": delegates[0]["delegate_id"],
        },
        {
            "tool_id": "hydra_viewport_preview",
            "tool": "HydraViewport",
            "delegate_options": [delegate["delegate_id"] for delegate in delegates],
        },
        {
            "tool_id": "usd_render_switch",
            "tool": "Switch",
            "draft_input": delegates[0]["delegate_id"],
            "final_input": delegates[-1]["delegate_id"],
        },
    ]
    return tools


def _asset_ref(path: Path, probe: dict[str, Any]) -> str:
    return f"{_safe_id(path.stem)}:{path.stat().st_size}:{round(float(probe.get('duration') or 0.0), 3)}:{int(probe.get('width') or 0)}x{int(probe.get('height') or 0)}"


def _safe_id(value: str) -> str:
    return re.sub(r"[^A-Za-z0-9_-]+", "_", value.strip()).strip("_").lower() or "item"


__all__ = ["DEFAULT_HYDRA_DELEGATES", "render_fusion_usd_hydra_toolset_manifest"]
