"""Resolve 21 Fusion macro editor inspector manifests."""
from __future__ import annotations

import json
import re
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


DEFAULT_CONTROLS: list[dict[str, Any]] = [
    {
        "id": "strength",
        "label": "Strength",
        "type": "float",
        "default": 0.65,
        "minimum": 0.0,
        "maximum": 1.0,
        "publish_group": "Look",
    },
    {
        "id": "accent_color",
        "label": "Accent color",
        "type": "color",
        "default": "#40e6ff",
        "publish_group": "Look",
    },
    {
        "id": "safe_title",
        "label": "Safe title",
        "type": "bool",
        "default": True,
        "publish_group": "Layout",
    },
]


def render_fusion_macro_editor_inspector_manifest(
    input_paths: list[str],
    output_dir: str,
    *,
    macro_id: str = "resolve21_fusion_macro_inspector",
    macro_name: str = "Lumeri Fusion Review Macro",
    controls: list[dict[str, Any]] | None = None,
    node_template: list[dict[str, Any]] | None = None,
    publish_groups: list[str] | None = None,
) -> str:
    """Emit a Resolve-style Fusion macro inspector manifest for real clips."""
    if not input_paths:
        raise ValueError("input_paths must contain at least one media path")
    output_root = Path(output_dir).expanduser().resolve()
    output_root.mkdir(parents=True, exist_ok=True)

    sources = []
    for index, raw_path in enumerate(input_paths):
        source = Path(raw_path).expanduser().resolve()
        if not source.exists():
            raise FileNotFoundError(f"Input media not found: {source}")
        if not source.is_file():
            raise OSError(f"Input media is not a file: {source}")
        probe = _probe_media(source)
        sources.append(
            {
                "clip_id": f"macro_clip_{index:02d}_{_safe_id(source.stem)}",
                "source_path": str(source),
                "source_probe": probe,
                "asset_ref": _asset_ref(source, probe),
            }
        )

    normalized_controls = [_normalize_control(raw, index) for index, raw in enumerate(controls or DEFAULT_CONTROLS)]
    groups = _normalize_groups(publish_groups, normalized_controls)
    nodes = _normalize_nodes(node_template, normalized_controls)
    validation = _validation_hints(normalized_controls, nodes, sources)

    manifest = {
        "schema_version": 1,
        "effect": "resolve21_fusion_macro_editor_inspector_manifest",
        "rendered_at": datetime.now(timezone.utc).isoformat(),
        "macro": {
            "macro_id": _safe_id(macro_id),
            "macro_name": macro_name,
            "clip_count": len(sources),
            "control_count": len(normalized_controls),
            "publish_group_count": len(groups),
        },
        "sources": sources,
        "inspector": {
            "controls": normalized_controls,
            "publish_groups": groups,
            "validation_hints": validation,
        },
        "node_template": nodes,
        "template_bindings": _template_bindings(nodes, normalized_controls),
        "diagnostics": [
            f"{len(normalized_controls)} inspector controls published across {len(groups)} groups",
            f"{len(sources)} real clips attached as macro review inputs",
        ],
        "review_hints": [
            "confirm each published control has a stable id, label, type, default, and publish_group",
            "check controls with numeric ranges before exposing the macro template to users",
            "use asset_ref values to reproduce the macro inspector review on the same source clips",
        ],
    }
    manifest_path = output_root / "fusion_macro_editor_inspector_manifest.json"
    manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return str(manifest_path)


def _normalize_control(raw: dict[str, Any], index: int) -> dict[str, Any]:
    control_type = str(raw.get("type") or "float").strip().lower()
    if control_type not in {"float", "int", "bool", "enum", "color", "text"}:
        control_type = "text"
    control_id = _safe_id(str(raw.get("id") or raw.get("label") or f"control_{index}"))
    default = raw.get("default")
    normalized = {
        "control_id": control_id,
        "label": str(raw.get("label") or control_id.replace("_", " ").title()),
        "type": control_type,
        "default": _default_for(control_type, default),
        "publish_group": str(raw.get("publish_group") or "General"),
        "tooltip": str(raw.get("tooltip") or f"Published {control_type} control for {control_id}"),
        "connectable": bool(raw.get("connectable", True)),
    }
    if control_type in {"float", "int"}:
        minimum = float(raw.get("minimum", raw.get("min", 0)))
        maximum = float(raw.get("maximum", raw.get("max", 1)))
        if maximum < minimum:
            minimum, maximum = maximum, minimum
        normalized["minimum"] = int(minimum) if control_type == "int" else round(minimum, 4)
        normalized["maximum"] = int(maximum) if control_type == "int" else round(maximum, 4)
    if control_type == "enum":
        options = [str(item) for item in raw.get("options", []) if str(item).strip()]
        normalized["options"] = options or ["default"]
        if normalized["default"] not in normalized["options"]:
            normalized["default"] = normalized["options"][0]
    return normalized


def _default_for(control_type: str, value: Any) -> Any:
    if control_type == "bool":
        return bool(value) if value is not None else False
    if control_type == "int":
        return int(value or 0)
    if control_type == "float":
        return round(float(value if value is not None else 0.0), 4)
    if control_type == "color":
        text = str(value or "#ffffff").strip()
        return text if re.fullmatch(r"#[0-9A-Fa-f]{6}", text) else "#ffffff"
    return str(value or "")


def _normalize_groups(groups: list[str] | None, controls: list[dict[str, Any]]) -> list[dict[str, Any]]:
    group_names = [str(group).strip() for group in groups or [] if str(group).strip()]
    for control in controls:
        if control["publish_group"] not in group_names:
            group_names.append(control["publish_group"])
    return [
        {
            "group_id": _safe_id(name),
            "label": name,
            "control_ids": [control["control_id"] for control in controls if control["publish_group"] == name],
        }
        for name in group_names
    ]


def _normalize_nodes(nodes: list[dict[str, Any]] | None, controls: list[dict[str, Any]]) -> list[dict[str, Any]]:
    if not nodes:
        nodes = [
            {"id": "media_in", "kind": "MediaIn", "inputs": []},
            {"id": "macro_adjust", "kind": "CustomTool", "inputs": ["media_in"]},
            {"id": "title_safe", "kind": "Transform", "inputs": ["macro_adjust"]},
            {"id": "media_out", "kind": "MediaOut", "inputs": ["title_safe"]},
        ]
    normalized = []
    control_ids = {control["control_id"] for control in controls}
    for index, raw in enumerate(nodes):
        node_id = _safe_id(str(raw.get("id") or raw.get("node_id") or f"node_{index}"))
        published = [str(item) for item in raw.get("published_controls", []) if str(item) in control_ids]
        if not published and index == 1:
            published = sorted(control_ids)
        normalized.append(
            {
                "node_id": node_id,
                "kind": str(raw.get("kind") or "CustomTool"),
                "inputs": [_safe_id(str(item)) for item in raw.get("inputs", []) if str(item).strip()],
                "published_controls": published,
            }
        )
    return normalized


def _template_bindings(nodes: list[dict[str, Any]], controls: list[dict[str, Any]]) -> list[dict[str, str]]:
    labels = {control["control_id"]: control["label"] for control in controls}
    bindings = []
    for node in nodes:
        for control_id in node["published_controls"]:
            bindings.append({"node_id": node["node_id"], "control_id": control_id, "label": labels.get(control_id, control_id)})
    return bindings


def _validation_hints(controls: list[dict[str, Any]], nodes: list[dict[str, Any]], sources: list[dict[str, Any]]) -> list[str]:
    hints = []
    if not any(node["published_controls"] for node in nodes):
        hints.append("no node publishes inspector controls")
    for control in controls:
        if control["type"] in {"float", "int"} and control.get("minimum") == control.get("maximum"):
            hints.append(f"{control['control_id']} has no usable numeric range")
    if any(source["source_probe"]["duration_seconds"] <= 0 for source in sources):
        hints.append("one or more source clips has unknown duration")
    return hints or ["macro inspector manifest is ready for review"]


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
    audio = next((stream for stream in payload.get("streams", []) if stream.get("codec_type") == "audio"), None)
    return {
        "duration_seconds": round(float(fmt.get("duration") or 0.0), 3),
        "size_bytes": int(fmt.get("size") or path.stat().st_size),
        "width": int(video.get("width") or 0),
        "height": int(video.get("height") or 0),
        "video_codec": str(video.get("codec_name") or ""),
        "has_audio": audio is not None,
    }


def _asset_ref(path: Path, probe: dict[str, Any]) -> str:
    return f"{_safe_id(path.stem)}:{path.stat().st_size}:{probe['duration_seconds']}:{probe['width']}x{probe['height']}"


def _safe_id(value: str) -> str:
    return re.sub(r"[^A-Za-z0-9_-]+", "_", value.strip()).strip("_").lower() or "item"


__all__ = ["DEFAULT_CONTROLS", "render_fusion_macro_editor_inspector_manifest"]
