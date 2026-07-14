"""Optional OpenUSD-style stage identity records for Gemia USD manifests."""
from __future__ import annotations

import hashlib
import json
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from gemia.asset_identity import build_entity_reference


SCHEMA_VERSION = 1


def openusd_available() -> bool:
    try:
        import pxr.Usd  # noqa: F401
        return True
    except Exception:
        return False


def build_openusd_stage_identity(
    stage_manifest: dict[str, Any],
    *,
    package_id: str | None = None,
) -> dict[str, Any]:
    """Build a deterministic OpenUSD-inspired identity payload without requiring pxr."""
    stage = stage_manifest.get("stage")
    if not isinstance(stage, dict):
        raise ValueError("stage manifest must include a stage object")
    layers = stage_manifest.get("usd_layers")
    if not isinstance(layers, list) or not layers:
        raise ValueError("stage manifest must include at least one usd_layers item")

    stage_id = _safe_id(str(stage.get("stage_id") or package_id or "gemia_stage"))
    frame_range = _frame_range(stage.get("frame_range"))
    stage_digest = _digest(json.dumps(stage_manifest, sort_keys=True, ensure_ascii=True))
    prims = [_prim_identity(stage_id, item, index) for index, item in enumerate(layers) if isinstance(item, dict)]
    if not prims:
        raise ValueError("stage manifest did not contain usable USD prim layers")

    root_layer = f"anon:{stage_id}:root.usda"
    return {
        "schema_version": SCHEMA_VERSION,
        "backend": "openusd_optional_local",
        "openusd_available": openusd_available(),
        "rendered_at": datetime.now(timezone.utc).isoformat(),
        "stage": {
            "package_id": _safe_id(package_id or stage_id),
            "stage_id": stage_id,
            "stage_identifier": f"gemia:usd_stage:{stage_id}:{stage_digest}",
            "root_layer_identifier": root_layer,
            "default_prim": stage_id,
            "root_prim": str(stage.get("root_prim") or f"/{stage_id}"),
            "up_axis": str(stage.get("up_axis") or "Y"),
            "meters_per_unit": max(float(stage.get("meters_per_unit") or 1.0), 0.001),
            "frame_range": frame_range,
            "clip_count": len(prims),
        },
        "layer_stack": [
            {
                "identifier": root_layer,
                "role": "root",
                "sublayer_identifiers": [prim["layer_identifier"] for prim in prims],
            }
        ],
        "prim_identities": prims,
        "hydra_delegate_ids": [
            str(item.get("delegate_id"))
            for item in stage_manifest.get("hydra_delegates", [])
            if isinstance(item, dict) and item.get("delegate_id")
        ],
        "review_hints": [
            "use stage_identifier as the stable handoff id when moving between Lumeri manifests and OpenUSD tooling",
            "map layer_identifier values to real SdfLayer identifiers when pxr bindings are available",
            "preserve prim_path and asset_ref values when replacing local payload placeholders with real USD assets",
        ],
    }


def write_openusd_stage_identity_package(
    stage_manifest_path: str,
    output_dir: str,
    *,
    package_id: str | None = None,
) -> str:
    """Write JSON identity plus a deterministic USDA-like stage text file."""
    manifest_path = Path(stage_manifest_path).expanduser().resolve()
    if not manifest_path.exists():
        raise FileNotFoundError(f"Stage manifest not found: {manifest_path}")
    payload = json.loads(manifest_path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError("stage manifest JSON must be an object")
    output_root = Path(output_dir).expanduser().resolve()
    output_root.mkdir(parents=True, exist_ok=True)
    identity = build_openusd_stage_identity(payload, package_id=package_id)
    usda_path = output_root / "openusd_stage_identity.usda"
    json_path = output_root / "openusd_stage_identity.json"
    usda_path.write_text(_to_usda(identity), encoding="utf-8")
    identity["files"] = {"usda_stage": str(usda_path)}
    json_path.write_text(json.dumps(identity, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return str(json_path)


def _prim_identity(stage_id: str, raw: dict[str, Any], index: int) -> dict[str, Any]:
    layer_id = _safe_id(str(raw.get("layer_id") or f"layer_{index:02d}"))
    asset_ref = str(raw.get("asset_ref") or "")
    if not asset_ref:
        raise ValueError(f"usd_layers[{index}] is missing asset_ref")
    fingerprint = _digest(asset_ref)
    source_path = str(raw.get("source_path") or "")
    prim_path = str(raw.get("prim_path") or f"/{stage_id}/shots/{layer_id}")
    usd_prim = raw.get("usd_prim") if isinstance(raw.get("usd_prim"), dict) else {}
    payload_identifier = str(usd_prim.get("payload") or f"{fingerprint}.usd")
    return {
        "prim_path": prim_path,
        "layer_id": layer_id,
        "layer_identifier": f"anon:{stage_id}:{layer_id}.usda",
        "payload_identifier": payload_identifier,
        "asset_ref": asset_ref,
        "source_path": source_path,
        "entity_reference": build_entity_reference(
            asset_id=layer_id,
            account_id="usd_stage",
            fingerprint=fingerprint,
            version_id=fingerprint[:16],
        ),
        "usd_type": str(usd_prim.get("type") or "Xform"),
        "kind": str(usd_prim.get("kind") or "component"),
        "variant_sets": usd_prim.get("variant_sets") if isinstance(usd_prim.get("variant_sets"), dict) else {},
    }


def _to_usda(identity: dict[str, Any]) -> str:
    stage = identity["stage"]
    lines = [
        "#usda 1.0",
        "(",
        f"    defaultPrim = \"{_q(stage['default_prim'])}\"",
        f"    upAxis = \"{_q(stage['up_axis'])}\"",
        f"    metersPerUnit = {stage['meters_per_unit']}",
        f"    startTimeCode = {stage['frame_range'][0]}",
        f"    endTimeCode = {stage['frame_range'][1]}",
        "    customLayerData = {",
        f"        string gemia:stageIdentifier = \"{_q(stage['stage_identifier'])}\"",
        "    }",
        ")",
        "",
        f"def Xform \"{_q(stage['default_prim'])}\" (",
        "    kind = \"group\"",
        ")",
        "{",
    ]
    for prim in identity["prim_identities"]:
        name = _q(prim["layer_id"])
        lines.extend(
            [
                f"    def {prim['usd_type']} \"{name}\" (",
                f"        prepend payload = @{_q(prim['payload_identifier'])}@",
                "        customData = {",
                f"            string gemia:assetRef = \"{_q(prim['asset_ref'])}\"",
                f"            string gemia:entityReference = \"{_q(prim['entity_reference'])}\"",
                "        }",
                "    )",
                "    {",
                "    }",
            ]
        )
    lines.append("}")
    return "\n".join(lines) + "\n"


def _frame_range(value: Any) -> list[int]:
    if isinstance(value, (list, tuple)) and len(value) >= 2:
        start, end = int(value[0]), int(value[1])
    else:
        start, end = 1, 120
    if end < start:
        start, end = end, start
    return [max(start, 0), max(end, 0)]


def _safe_id(value: str) -> str:
    return re.sub(r"[^A-Za-z0-9_-]+", "_", value.strip()).strip("_").lower() or "item"


def _digest(value: str) -> str:
    return hashlib.sha1(value.encode("utf-8")).hexdigest()


def _q(value: Any) -> str:
    return str(value).replace("\\", "\\\\").replace("\"", "\\\"")


__all__ = [
    "SCHEMA_VERSION",
    "build_openusd_stage_identity",
    "openusd_available",
    "write_openusd_stage_identity_package",
]
