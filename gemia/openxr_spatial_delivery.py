"Optional OpenXR-style spatial delivery records for Gemia immersive manifests."
from __future__ import annotations

import hashlib
import json
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


SCHEMA_VERSION = 1


def openxr_available() -> bool:
    try:
        import xr  # noqa: F401
        return True
    except Exception:
        return False


def build_openxr_spatial_delivery_profile(
    manifests: list[dict[str, Any]],
    *,
    profile_id: str = "gemia_openxr_spatial_delivery",
) -> dict[str, Any]:
    """Build deterministic OpenXR-inspired delivery metadata without requiring an XR runtime."""
    payloads = [payload for payload in manifests if isinstance(payload, dict)]
    if not payloads:
        raise ValueError("manifests must contain at least one manifest object")
    sources = _collect_sources(payloads)
    if not sources:
        raise ValueError("manifests must include at least one source with asset_ref")

    effects = [str(payload.get("effect") or "unknown") for payload in payloads]
    shared_refs = _shared_source_refs(payloads) or sorted({source["asset_ref"] for source in sources})
    profile = _safe_id(profile_id)
    return {
        "schema_version": SCHEMA_VERSION,
        "backend": "openxr_optional_local",
        "openxr_available": openxr_available(),
        "rendered_at": datetime.now(timezone.utc).isoformat(),
        "profile": {
            "profile_id": profile,
            "profile_identifier": f"gemia:openxr:{profile}:{_digest('|'.join(shared_refs + effects))}",
            "form_factor": "head_mounted_display",
            "view_configuration": "primary_stereo",
            "reference_space": "local_floor",
            "clip_count": len(shared_refs),
            "source_effects": effects,
        },
        "source_bindings": _source_bindings(sources),
        "view_configurations": _view_configurations(payloads),
        "swapchain_intents": _swapchain_intents(payloads),
        "action_sets": _action_sets(payloads),
        "validation": _validation(payloads, sources, shared_refs),
        "review_hints": [
            "use profile_identifier as the stable handoff id for headset preview and spatial delivery",
            "map view_configurations to real XrViewConfigurationView structs when an OpenXR runtime is available",
            "preserve source asset_ref values when replacing local proxies with final stereoscopic or MV-HEVC media",
        ],
    }


def write_openxr_spatial_delivery_package(
    manifest_paths: list[str],
    output_dir: str,
    *,
    profile_id: str = "gemia_openxr_spatial_delivery",
) -> str:
    """Write an OpenXR-style profile JSON plus a compact runtime action manifest."""
    if not manifest_paths:
        raise ValueError("manifest_paths must contain at least one JSON manifest path")
    payloads = []
    for raw_path in manifest_paths:
        path = Path(raw_path).expanduser().resolve()
        if not path.exists():
            raise FileNotFoundError(f"Manifest not found: {path}")
        payload = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(payload, dict):
            raise ValueError(f"Manifest JSON must be an object: {path}")
        payloads.append(payload)
    output_root = Path(output_dir).expanduser().resolve()
    output_root.mkdir(parents=True, exist_ok=True)
    profile = build_openxr_spatial_delivery_profile(payloads, profile_id=profile_id)
    action_manifest_path = output_root / "openxr_action_manifest.json"
    action_manifest_path.write_text(json.dumps(_action_manifest(profile), ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    profile["files"] = {"action_manifest": str(action_manifest_path)}
    profile_path = output_root / "openxr_spatial_delivery_profile.json"
    profile_path.write_text(json.dumps(profile, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return str(profile_path)


def _collect_sources(payloads: list[dict[str, Any]]) -> list[dict[str, Any]]:
    sources: list[dict[str, Any]] = []
    seen: set[str] = set()
    for payload in payloads:
        for item in payload.get("sources", []):
            if isinstance(item, dict) and item.get("asset_ref"):
                ref = str(item["asset_ref"])
                key = f"{ref}:{item.get('source_path')}"
                if key not in seen:
                    seen.add(key)
                    sources.append(item)
    return sources


def _source_bindings(sources: list[dict[str, Any]]) -> list[dict[str, Any]]:
    bindings = []
    for index, source in enumerate(sources):
        probe = source.get("source_probe") if isinstance(source.get("source_probe"), dict) else {}
        bindings.append({
            "binding_id": f"source_{index:02d}_{_safe_id(str(source.get('clip_id') or index))}",
            "asset_ref": str(source["asset_ref"]),
            "source_path": str(source.get("source_path") or ""),
            "entity_path": f"/session/sources/{index:02d}",
            "duration_seconds": round(float(probe.get("duration") or 0.0), 3),
            "resolution": f"{int(probe.get('width') or 0)}x{int(probe.get('height') or 0)}",
            "has_audio": bool(probe.get("has_audio")),
        })
    return bindings


def _view_configurations(payloads: list[dict[str, Any]]) -> list[dict[str, Any]]:
    configs = []
    for payload in payloads:
        for render_pass in payload.get("render_passes", []):
            profile = render_pass.get("profile") if isinstance(render_pass, dict) else {}
            if isinstance(profile, dict):
                configs.append({
                    "configuration_id": _safe_id(str(render_pass.get("pass_id") or profile.get("profile_id") or "view")),
                    "view_type": "primary_stereo",
                    "recommended_width": int(profile.get("eye_buffer_width") or 4320),
                    "recommended_height": int(profile.get("eye_buffer_height") or 4320),
                    "foveation": str(profile.get("foveation") or "balanced"),
                    "device_profile": str(render_pass.get("device_profile") or "headset"),
                })
    return configs or [{"configuration_id": "primary_stereo_default", "view_type": "primary_stereo", "recommended_width": 4320, "recommended_height": 4320, "foveation": "balanced", "device_profile": "headset"}]


def _swapchain_intents(payloads: list[dict[str, Any]]) -> list[dict[str, Any]]:
    intents = []
    for payload in payloads:
        for deliverable in payload.get("deliverables", []):
            settings = deliverable.get("mainconcept_settings") if isinstance(deliverable, dict) else {}
            if isinstance(settings, dict):
                intents.append({
                    "swapchain_id": _safe_id(str(deliverable.get("deliverable_id") or "deliverable")),
                    "codec": str(settings.get("codec") or "h265"),
                    "container": str(settings.get("container") or "mp4"),
                    "view_count": int(settings.get("view_count") or 1),
                    "target_bitrate_mbps": float(settings.get("target_bitrate_mbps") or 0.0),
                    "clip_asset_refs": deliverable.get("clip_asset_refs") if isinstance(deliverable.get("clip_asset_refs"), list) else [],
                })
    return intents


def _action_sets(payloads: list[dict[str, Any]]) -> list[dict[str, Any]]:
    has_pip = any(payload.get("effect") == "resolve21_picture_in_picture_resolvefx_layout" or payload.get("layouts") for payload in payloads)
    has_panomap = any(payload.get("effect") == "resolve21_panomap_ilpd_stereo_retarget_manifest" or payload.get("retargets") for payload in payloads)
    has_script = any(payload.get("effect") == "resolve21_finaldraft_intelliscript_ingest_manifest" or payload.get("assignments") for payload in payloads)
    actions = [{"name": "recenter_view", "type": "pose", "localized_name": "Recenter View"}]
    if has_panomap:
        actions.append({"name": "adjust_ipd", "type": "float", "localized_name": "Adjust IPD"})
    if has_pip:
        actions.append({"name": "toggle_picture_in_picture", "type": "boolean", "localized_name": "Toggle PiP"})
    if has_script:
        actions.append({"name": "next_script_marker", "type": "boolean", "localized_name": "Next Script Marker"})
    return [{"set_id": "gemia_spatial_review", "localized_name": "Gemia Spatial Review", "actions": actions}]


def _validation(payloads: list[dict[str, Any]], sources: list[dict[str, Any]], shared_refs: list[str]) -> dict[str, Any]:
    effects = {str(payload.get("effect") or "") for payload in payloads}
    return {
        "manifest_count": len(payloads),
        "source_binding_count": len(sources),
        "shared_asset_ref_count": len(shared_refs),
        "has_immersive_render_passes": any(payload.get("render_passes") for payload in payloads),
        "has_delivery_swapchains": bool(_swapchain_intents(payloads)),
        "has_panomap_controls": "resolve21_panomap_ilpd_stereo_retarget_manifest" in effects or any(payload.get("retargets") for payload in payloads),
        "has_pip_actions": "resolve21_picture_in_picture_resolvefx_layout" in effects or any(payload.get("layouts") for payload in payloads),
        "has_script_actions": "resolve21_finaldraft_intelliscript_ingest_manifest" in effects or any(payload.get("assignments") for payload in payloads),
    }


def _shared_source_refs(payloads: list[dict[str, Any]]) -> list[str]:
    sets = []
    for payload in payloads:
        refs = {str(item["asset_ref"]) for item in payload.get("sources", []) if isinstance(item, dict) and item.get("asset_ref")}
        if refs:
            sets.append(refs)
    return sorted(set.intersection(*sets)) if sets else []


def _action_manifest(profile: dict[str, Any]) -> dict[str, Any]:
    return {"schema_version": SCHEMA_VERSION, "profile_identifier": profile["profile"]["profile_identifier"], "action_sets": profile["action_sets"]}


def _safe_id(value: str) -> str:
    return re.sub(r"[^A-Za-z0-9_-]+", "_", value.strip()).strip("_").lower() or "item"


def _digest(value: str) -> str:
    return hashlib.sha1(value.encode("utf-8")).hexdigest()


__all__ = ["SCHEMA_VERSION", "build_openxr_spatial_delivery_profile", "openxr_available", "write_openxr_spatial_delivery_package"]
