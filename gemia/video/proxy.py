"""Proxy asset management for lightweight preview rendering."""
from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass
import hashlib
from pathlib import Path
from typing import Any

from gemia.video.export import proxy_generate


@dataclass(frozen=True)
class ProxyAsset:
    source_path: str
    proxy_path: str
    resolution: int


class ProxyManager:
    """Create and reuse lightweight proxy files for preview workflows."""

    def __init__(self, root_dir: str | Path) -> None:
        self.root_dir = Path(root_dir).expanduser().resolve()
        self.root_dir.mkdir(parents=True, exist_ok=True)

    def proxy_path_for(self, source_path: str | Path, *, resolution: int = 540) -> Path:
        source = Path(source_path).expanduser().resolve()
        suffix = source.suffix if source.suffix else ".mp4"
        digest = hashlib.sha1(str(source).encode("utf-8")).hexdigest()[:12]
        return self.root_dir / f"{source.stem}-{digest}.proxy_{int(resolution)}{suffix}"

    def ensure_proxy(self, source_path: str | Path, *, resolution: int = 540) -> ProxyAsset:
        source = Path(source_path).expanduser().resolve()
        proxy_path = self.proxy_path_for(source, resolution=resolution)
        if not proxy_path.exists() or proxy_path.stat().st_mtime < source.stat().st_mtime:
            proxy_generate(str(source), str(proxy_path), resolution=int(resolution))
        return ProxyAsset(
            source_path=str(source),
            proxy_path=str(proxy_path),
            resolution=int(resolution),
        )

    def attach_to_plan(self, plan: dict[str, Any], *, resolution: int = 540) -> tuple[dict[str, Any], dict[str, str]]:
        preview_plan = deepcopy(plan)
        proxy_map: dict[str, str] = {}
        for layer in preview_plan.get("layers", []):
            if layer.get("type") != "video" or not layer.get("source"):
                continue
            asset = self.ensure_proxy(str(layer["source"]), resolution=resolution)
            layer.setdefault("metadata", {})
            layer["metadata"]["source_path"] = layer["source"]
            layer["metadata"]["proxy_path"] = asset.proxy_path
            layer["source"] = asset.proxy_path
            proxy_map[asset.source_path] = asset.proxy_path
        return preview_plan, proxy_map


__all__ = [
    "ProxyAsset",
    "ProxyManager",
]
