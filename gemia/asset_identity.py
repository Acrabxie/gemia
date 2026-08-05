"""Optional OpenAssetIO-style asset identity records for Gemia media assets."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Iterable
from urllib.parse import parse_qs, quote, unquote, urlencode, urlparse


SCHEMA_VERSION = 1
LOCAL_SCHEME = "gemia"


@dataclass(frozen=True)
class AssetEntityReference:
    entity_reference: str
    asset_id: str
    account_id: str
    version_id: str
    fingerprint: str


def openassetio_available() -> bool:
    try:
        import openassetio  # noqa: F401
        return True
    except Exception:
        return False


def asset_identity_for_record(record: dict[str, Any], *, namespace: str = LOCAL_SCHEME) -> dict[str, Any]:
    """Return an OpenAssetIO-inspired identity payload with deterministic local fallback."""
    asset_id = str(record.get("asset_id") or record.get("id") or "")
    account_id = str(record.get("account_id") or "local")
    fingerprint = str(record.get("fingerprint") or "")
    if not asset_id:
        raise ValueError("asset_id is required")
    if not fingerprint:
        raise ValueError("fingerprint is required")
    version_id = _version_id(fingerprint)
    entity_reference = build_entity_reference(
        asset_id=asset_id,
        account_id=account_id,
        fingerprint=fingerprint,
        version_id=version_id,
        namespace=namespace,
    )
    return {
        "schema_version": SCHEMA_VERSION,
        "backend": "openassetio_optional_local",
        "openassetio_available": openassetio_available(),
        "entity_reference": entity_reference,
        "asset_id": asset_id,
        "account_id": account_id,
        "version_id": version_id,
        "fingerprint": fingerprint,
        "traits": {
            "locatable_content": {
                "path": str(record.get("storage_path") or record.get("source_path") or record.get("original_path") or ""),
                "mime_type": str(record.get("mime_type") or ""),
            },
            "versioned_asset": {
                "name": str(record.get("name") or asset_id),
                "version": version_id,
                "source": "gemia_media_library",
            },
        },
    }


def attach_asset_identity(asset: dict[str, Any]) -> dict[str, Any]:
    """Copy an asset record and attach top-level plus metadata identity payloads."""
    result = dict(asset)
    identity = asset_identity_for_record(result)
    metadata = result.get("metadata") if isinstance(result.get("metadata"), dict) else {}
    metadata = dict(metadata)
    metadata["asset_identity"] = identity
    result["metadata"] = metadata
    result["asset_identity"] = identity
    return result


def build_entity_reference(
    *,
    asset_id: str,
    account_id: str,
    fingerprint: str,
    version_id: str | None = None,
    namespace: str = LOCAL_SCHEME,
) -> str:
    """Build a portable local entity reference suitable for later OpenAssetIO resolution."""
    version = version_id or _version_id(fingerprint)
    query = urlencode({"fingerprint": fingerprint, "version": version})
    safe_account = quote(str(account_id), safe="")
    safe_asset = quote(str(asset_id), safe="")
    return f"{namespace}://media/{safe_account}/{safe_asset}?{query}"


def parse_entity_reference(entity_reference: str) -> AssetEntityReference:
    parsed = urlparse(str(entity_reference))
    if parsed.scheme != LOCAL_SCHEME or parsed.netloc != "media":
        raise ValueError("unsupported Gemia asset entity reference")
    parts = [unquote(part) for part in parsed.path.strip("/").split("/") if part]
    if len(parts) != 2:
        raise ValueError("invalid Gemia asset entity reference path")
    query = parse_qs(parsed.query)
    fingerprint = str((query.get("fingerprint") or [""])[0])
    version_id = str((query.get("version") or [""])[0])
    if not fingerprint or not version_id:
        raise ValueError("entity reference must include fingerprint and version")
    return AssetEntityReference(
        entity_reference=str(entity_reference),
        account_id=parts[0],
        asset_id=parts[1],
        version_id=version_id,
        fingerprint=fingerprint,
    )


def resolve_asset_identity(entity_reference: str, assets: Iterable[dict[str, Any]]) -> dict[str, Any] | None:
    """Resolve a local entity reference against in-memory media-library assets."""
    ref = parse_entity_reference(entity_reference)
    for asset in assets:
        if str(asset.get("asset_id") or asset.get("id") or "") != ref.asset_id:
            continue
        if str(asset.get("account_id") or "local") != ref.account_id:
            continue
        if str(asset.get("fingerprint") or "") != ref.fingerprint:
            continue
        return attach_asset_identity(asset)
    return None


def _version_id(fingerprint: str) -> str:
    clean = "".join(ch for ch in str(fingerprint).lower() if ch in "0123456789abcdef")
    return clean[:16] or "unversioned"


__all__ = [
    "AssetEntityReference",
    "SCHEMA_VERSION",
    "asset_identity_for_record",
    "attach_asset_identity",
    "build_entity_reference",
    "openassetio_available",
    "parse_entity_reference",
    "resolve_asset_identity",
]
