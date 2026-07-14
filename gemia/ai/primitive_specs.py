from __future__ import annotations

import ast
import json
from typing import Any

from gemia.registry import PrimitiveInfo, get_info

from .skill_context import active_primitive_names


_MEDIA_PARAM_NAMES = {
    "img",
    "image",
    "frame",
    "frames",
    "audio",
    "audio_arr",
    "input_path",
    "input_paths",
    "input_video",
    "input_a",
    "input_b",
    "video_path",
    "path",
    "paths",
    "source",
    "source_path",
    "output_path",
    "output_dir",
    "dest",
    "destination",
}


def primitive_specs_for_skills(skills: list[str]) -> list[dict[str, Any]]:
    """Return compact JSON-compatible tool specs for active planner skills."""
    specs: list[dict[str, Any]] = []
    for fqn in active_primitive_names(skills):
        try:
            specs.append(primitive_spec_for_fqn(fqn))
        except KeyError:
            continue
    return specs


def primitive_spec_for_fqn(fqn: str) -> dict[str, Any]:
    info = get_info(fqn)
    args_schema, ask_if_missing = _args_schema(info)
    spec = {
        "name": info.fqn,
        "description": _compact_description(info.docstring),
        "input_media": _input_media(info),
        "output_media": _output_media(info),
        "args_schema": args_schema,
        "side_effects": _side_effects(info),
        "cost": _cost(info),
        "ask_if_missing": ask_if_missing,
    }
    _apply_creative_overrides(spec, info)
    spec["ask_policy"] = _ask_policy(spec)
    return spec


def trust_boundary_text(
    *,
    source: str,
    text: Any,
    trusted: bool = False,
    path: str | None = None,
) -> dict[str, Any] | None:
    value = str(text or "").strip()
    if not value:
        return None
    item: dict[str, Any] = {
        "source": source,
        "trusted": bool(trusted),
        "text": value[:500],
    }
    if path:
        item["path"] = path
    return item


def media_text_trust_boundaries(
    project_state: dict[str, Any] | None,
    video_context: dict[str, Any] | None = None,
    *,
    limit: int = 12,
) -> list[dict[str, Any]]:
    """Annotate media-derived text so models do not treat it as user commands."""
    items: list[dict[str, Any]] = []
    if isinstance(project_state, dict):
        clips = project_state.get("clips")
        if isinstance(clips, list):
            for idx, clip in enumerate(clips):
                if not isinstance(clip, dict):
                    continue
                for key in ("name", "title", "label"):
                    item = trust_boundary_text(
                        source="metadata",
                        trusted=False,
                        text=clip.get(key),
                        path=f"project_state.clips[{idx}].{key}",
                    )
                    if item:
                        items.append(item)
                summary = clip.get("summary")
                if isinstance(summary, dict):
                    for key in ("mood", "key_frame", "suggested_use", "description", "caption", "subtitle"):
                        item = trust_boundary_text(
                            source="metadata",
                            trusted=False,
                            text=summary.get(key),
                            path=f"project_state.clips[{idx}].summary.{key}",
                        )
                        if item:
                            items.append(item)
                if len(items) >= limit:
                    return items[:limit]
    if isinstance(video_context, dict):
        for key in ("mood", "key_frame", "suggested_use"):
            item = trust_boundary_text(
                source="metadata",
                trusted=False,
                text=video_context.get(key),
                path=f"video_context.{key}",
            )
            if item:
                items.append(item)
            if len(items) >= limit:
                break
    return items[:limit]


def _args_schema(info: PrimitiveInfo) -> tuple[dict[str, Any], list[str]]:
    properties: dict[str, dict[str, Any]] = {}
    required: list[str] = []
    for name, meta in info.params.items():
        if name in _MEDIA_PARAM_NAMES or name == "self":
            continue
        default_present = "default" in meta
        schema = _schema_for_param(meta)
        if default_present:
            schema["default"] = _json_default(meta.get("default"))
        properties[name] = schema
        if not default_present:
            required.append(name)
    schema: dict[str, Any] = {
        "type": "object",
        "properties": properties,
        "required": required,
        "additionalProperties": False,
    }
    return schema, required


def _apply_creative_overrides(spec: dict[str, Any], info: PrimitiveInfo) -> None:
    fqn = info.fqn
    if fqn == "gemia.video.layer_flow.render_layer_workflow":
        spec["description"] = (
            "Author a layer-first render on a source video or blank canvas: text/image/html/solid/lottie layers, "
            "positions, opacity, z-index, timing, and keyframes."
        )
        spec["input_media"] = ["video", "blank_canvas"]
        spec["output_media"] = "video|layer_manifest"
        spec["side_effects"] = sorted(set(spec["side_effects"] + ["writes_layer_manifest", "renders_layers"]))
        props = spec["args_schema"]["properties"]
        props["overlay_layers"] = {
            "type": "array",
            "description": "Layer specs with id/type/text/html/source/position/opacity/scale/z_index/start_frame/end_frame/keyframes.",
            "items": {
                "type": "object",
                "additionalProperties": True,
                "properties": {
                    "id": {"type": "string"},
                    "type": {"type": "string", "enum": ["text", "image", "video", "html", "solid", "lottie"]},
                    "text": {"type": "string"},
                    "html": {"type": "string"},
                    "source": {"type": "string"},
                    "color": {"type": "array", "items": {"type": "number"}},
                    "position": {"type": "array", "items": {"type": "number"}},
                    "size": {"type": "array", "items": {"type": "number"}},
                    "opacity": {"type": "number"},
                    "scale": {"type": "number"},
                    "z_index": {"type": "integer"},
                    "start_frame": {"type": "integer"},
                    "end_frame": {"type": "integer"},
                    "keyframes": {"type": "object"},
                },
            },
        }
        props.setdefault("include_source", {"type": "boolean", "default": True})
        spec["args_schema"]["additionalProperties"] = False
    elif fqn == "gemia.video.html_graphics.render_html_graphics_plan":
        spec["description"] = (
            "Render HTML/Lottie/text graphics over a source video or on a blank canvas; good for title cards, "
            "lower thirds, captions, prompt-only MG clips, info panels, and custom visual UI layers."
        )
        spec["input_media"] = ["video", "blank_canvas"]
        spec["output_media"] = "video|html_graphics_manifest"
        spec["side_effects"] = sorted(set(spec["side_effects"] + ["renders_html_layers", "writes_html_graphics_manifest"]))
        props = spec["args_schema"]["properties"]
        props["html"] = {
            "type": "string",
            "description": "Small trusted inline HTML/CSS overlay authored by the planner for visual text or panels.",
        }
        props["overlay_layers"] = {
            "type": "array",
            "description": "Optional explicit layer specs; use when multiple graphics need timing, position, opacity, or z-index.",
            "items": {"type": "object", "additionalProperties": True},
        }
    elif fqn == "gemia.video.creative_runtime.write_development_patch_brief":
        spec["description"] = (
            "Write a development patch brief when a creative request needs a new primitive or source change. "
            "It does not mutate source code by itself."
        )
        spec["input_media"] = ["video|image|audio|project"]
        spec["output_media"] = "video_passthrough|development_brief"
        spec["side_effects"] = sorted(set(spec["side_effects"] + ["writes_development_brief", "source_patch_proposal"]))
        spec["cost"] = "local_fast"
        props = spec["args_schema"]["properties"]
        for key in ("suggested_files", "proposed_primitives", "safety_notes"):
            props[key] = {"type": "array", "items": {"type": "string"}}
    elif fqn.startswith("gemia.video.ad_graphics."):
        spec["description"] = (
            "Render commercial advertising graphics: title packs, lower thirds, CTA cards, product callouts, "
            "price badges, shimmer text, and composited ad overlays with composition sidecars."
        )
        if fqn != "gemia.video.ad_graphics.compose_overlay_on_video":
            spec["input_media"] = ["video", "blank_canvas"]
        spec["output_media"] = "video|ad_composition"
        spec["side_effects"] = sorted(set(spec["side_effects"] + ["renders_ad_graphics", "writes_ad_composition_manifest"]))
        props = spec["args_schema"]["properties"]
        if "style" in props:
            props["style"]["enum"] = ["ice", "mono", "night"]


def _schema_for_param(meta: dict[str, Any]) -> dict[str, Any]:
    annotation = str(meta.get("annotation") or "").lower()
    default = _json_default(meta.get("default")) if "default" in meta else None
    typ = "string"
    if "bool" in annotation or isinstance(default, bool):
        typ = "boolean"
    elif any(token in annotation for token in ("list", "tuple", "sequence", "set")) or isinstance(default, (list, tuple)):
        typ = "array"
    elif "int" in annotation and "float" not in annotation:
        typ = "integer"
    elif "float" in annotation or "double" in annotation or isinstance(default, float):
        typ = "number"
    elif "dict" in annotation or "mapping" in annotation or isinstance(default, dict):
        typ = "object"
    elif "str" in annotation or "path" in annotation or isinstance(default, str):
        typ = "string"
    return {"type": typ}


def _json_default(raw: Any) -> Any:
    if raw is None:
        return None
    if not isinstance(raw, str):
        return raw
    try:
        value = ast.literal_eval(raw)
    except (ValueError, SyntaxError):
        return raw
    try:
        json.dumps(value)
    except TypeError:
        return str(value)
    return value


def _compact_description(docstring: str) -> str:
    text = " ".join((docstring or "").strip().split())
    if not text:
        return "Lumeri primitive."
    first = text.split(" Args:", 1)[0].split(" Returns:", 1)[0].strip()
    return first[:240]


def _input_media(info: PrimitiveInfo) -> list[str]:
    fqn = info.fqn
    if ".stock_media." in fqn:
        return []
    if "generate_image" in fqn or fqn.endswith(".generate_video") or fqn.endswith(".generate_broll"):
        return []
    if ".transitions." in fqn:
        return ["video", "video"]
    if info.domain == "picture":
        return ["image", "video_frames"]
    if info.domain == "audio":
        return ["audio"]
    if info.domain == "video":
        return ["video"]
    return ["media"]


def _output_media(info: PrimitiveInfo) -> str:
    fqn = info.fqn
    if info.name == "search_stock_media":
        return "metadata"
    if ".stock_media." in fqn:
        return "video|image"
    if ".analysis." in fqn or info.name in {"get_metadata", "detect_scenes", "scene_detect"}:
        return "metadata"
    if ".generative." in fqn and "video" in fqn:
        return "video"
    if ".generative." in fqn and "picture" in fqn:
        return "image"
    if info.domain == "picture":
        return "image"
    if info.domain == "audio":
        return "audio"
    if info.domain == "video":
        return "video"
    return "artifact"


def _side_effects(info: PrimitiveInfo) -> list[str]:
    effects: list[str] = []
    if any(name in info.params for name in ("output_path", "output_dir")) or info.domain == "video":
        effects.append("writes_output_file")
    if ".generative." in info.fqn:
        effects.append("external_model")
    if ".stock_media." in info.fqn and info.name != "search_stock_media":
        effects.append("external_download")
        effects.append("may_import_media_library")
    if "blender" in info.fqn or "blender" in info.docstring.lower():
        effects.append("blender_optional")
    if _output_media(info) == "metadata":
        effects.append("reads_metadata")
    return effects


def _cost(info: PrimitiveInfo) -> str:
    fqn = info.fqn.lower()
    if "generative" in fqn:
        return "external_model"
    if "stock_media" in fqn:
        return "external_api"
    if "blender" in fqn:
        return "blender_optional"
    if ".analysis." in fqn:
        return "local_fast"
    if info.domain == "video":
        return "local_slow"
    if info.domain in {"picture", "audio"}:
        return "local_medium"
    return "local_fast"


def _ask_policy(spec: dict[str, Any]) -> dict[str, Any]:
    schema = spec.get("args_schema") if isinstance(spec.get("args_schema"), dict) else {}
    props = schema.get("properties") if isinstance(schema.get("properties"), dict) else {}
    defaultable_args = [
        str(name)
        for name, meta in props.items()
        if isinstance(meta, dict) and "default" in meta
    ]
    side_effects = set(str(item) for item in spec.get("side_effects") or [])
    risk = "external_or_source_change" if side_effects & {
        "external_model",
        "external_download",
        "source_patch_proposal",
        "blender_optional",
    } else "local"
    ask_if_missing = [str(item) for item in spec.get("ask_if_missing") or []]
    return {
        "mode": "default_first" if not ask_if_missing else "ask_only_if_uninferable",
        "risk": risk,
        "defaultable_args": defaultable_args,
        "required_user_slots": ask_if_missing,
    }
