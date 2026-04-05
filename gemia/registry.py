"""Function registry — auto-discovers all primitives in gemia.picture/audio/video.

Usage::

    from gemia.registry import catalog_for_prompt, resolve, get_registry

    # Get the full catalog text for Gemini system prompt
    print(catalog_for_prompt())

    # Resolve a function by fully-qualified name
    func = resolve("gemia.picture.color.color_grade")
    result = func(img, preset="cyberpunk")
"""
from __future__ import annotations

import importlib
import inspect
import pkgutil
from dataclasses import dataclass, field
from typing import Any, Callable

# Packages to scan
_PACKAGES = ["gemia.picture", "gemia.audio", "gemia.video"]

# Functions that require ndarray args (beyond the standard first arg) —
# Gemini cannot produce these in JSON, so we exclude them from the AI catalog.
_EXCLUDED_FROM_CATALOG = {
    "gemia.picture.color.apply_lut",       # lut: ndarray
    "gemia.picture.pixel.convolve",        # kernel: ndarray
    "gemia.picture.composite.composite",   # needs fg + bg + mask
    "gemia.picture.composite.blend",       # needs a + b
    "gemia.picture.geometry.perspective_transform",  # src_points/dst_points: ndarray
    # blend modes: require two image inputs
    "gemia.picture.composite.blend_multiply",
    "gemia.picture.composite.blend_screen",
    "gemia.picture.composite.blend_overlay",
    "gemia.picture.composite.blend_soft_light",
    "gemia.picture.composite.blend_hard_light",
    "gemia.picture.composite.blend_color_dodge",
    "gemia.picture.composite.blend_color_burn",
    "gemia.picture.composite.blend_difference",
    "gemia.picture.composite.blend_exclusion",
    "gemia.picture.composite.blend_hue",
    "gemia.picture.composite.blend_saturation",
    "gemia.picture.composite.blend_color",
    "gemia.picture.composite.blend_luminosity",
    # optical flow: requires two image inputs
    "gemia.video.frames.optical_flow_interpolate",
    # keyframe: requires callable + KeyframeTrack objects
    "gemia.video.keyframe.apply_animated_op",
    # mixer: require numpy arrays as named args
    "gemia.audio.mixer.create_bus",
    "gemia.audio.mixer.sidechain_compress",
    "gemia.audio.mixer.auto_duck",
}


@dataclass
class PrimitiveInfo:
    """Metadata about a single primitive function."""
    fqn: str
    func: Callable
    domain: str         # "picture", "audio", "video"
    module: str
    name: str
    docstring: str
    params: dict[str, dict] = field(default_factory=dict)


_REGISTRY: dict[str, PrimitiveInfo] = {}


def _discover() -> None:
    """Walk primitive packages and register all public functions."""
    for pkg_name in _PACKAGES:
        try:
            pkg = importlib.import_module(pkg_name)
        except ImportError:
            continue
        domain = pkg_name.split(".")[-1]
        for _importer, modname, _ispkg in pkgutil.walk_packages(
            pkg.__path__, prefix=pkg_name + "."
        ):
            try:
                mod = importlib.import_module(modname)
            except ImportError:
                continue
            for name, obj in inspect.getmembers(mod, inspect.isfunction):
                if name.startswith("_"):
                    continue
                # Only register functions defined in this module (skip re-exports)
                if getattr(obj, "__module__", None) != modname:
                    continue
                fqn = f"{modname}.{name}"
                sig = inspect.signature(obj)
                params: dict[str, dict] = {}
                for pname, p in sig.parameters.items():
                    info: dict[str, Any] = {}
                    if p.annotation != inspect.Parameter.empty:
                        info["annotation"] = _format_annotation(p.annotation)
                    if p.default != inspect.Parameter.empty:
                        info["default"] = repr(p.default)
                    if p.kind == inspect.Parameter.KEYWORD_ONLY:
                        info["keyword_only"] = True
                    params[pname] = info
                _REGISTRY[fqn] = PrimitiveInfo(
                    fqn=fqn,
                    func=obj,
                    domain=domain,
                    module=modname,
                    name=name,
                    docstring=inspect.getdoc(obj) or "",
                    params=params,
                )


def _format_annotation(ann: Any) -> str:
    """Best-effort conversion of a type annotation to a readable string."""
    if isinstance(ann, str):
        return ann
    if hasattr(ann, "__name__"):
        return ann.__name__
    return str(ann).replace("typing.", "")


def get_registry() -> dict[str, PrimitiveInfo]:
    """Return the full registry dict."""
    return _REGISTRY


def resolve(fqn: str) -> Callable:
    """Resolve a fully-qualified function name to its callable."""
    info = _REGISTRY.get(fqn)
    if info is None:
        raise KeyError(f"Unknown primitive: {fqn}")
    return info.func


def get_info(fqn: str) -> PrimitiveInfo:
    """Return the PrimitiveInfo for a function."""
    info = _REGISTRY.get(fqn)
    if info is None:
        raise KeyError(f"Unknown primitive: {fqn}")
    return info


def catalog_for_prompt() -> str:
    """Build a compact text catalog for injection into a Gemini system prompt.

    Each function is one line: ``fqn(params) — first line of docstring``.
    Functions in _EXCLUDED_FROM_CATALOG are omitted.
    """
    lines: list[str] = []
    for domain_label in ("picture", "audio", "video"):
        domain_fns = sorted(
            (fqn, info) for fqn, info in _REGISTRY.items()
            if info.domain == domain_label and fqn not in _EXCLUDED_FROM_CATALOG
        )
        if not domain_fns:
            continue
        lines.append(f"## gemia.{domain_label}")
        for fqn, info in domain_fns:
            param_parts: list[str] = []
            for pname, pinfo in info.params.items():
                s = pname
                if "annotation" in pinfo:
                    s += f": {pinfo['annotation']}"
                if "default" in pinfo:
                    s += f" = {pinfo['default']}"
                if pinfo.get("keyword_only"):
                    s = f"*, {s}" if not param_parts or not param_parts[-1].startswith("*") else s
                param_parts.append(s)
            sig_str = ", ".join(param_parts)
            doc_first = info.docstring.split("\n")[0] if info.docstring else ""
            lines.append(f"  {fqn}({sig_str})")
            if doc_first:
                lines.append(f"    {doc_first}")
        lines.append("")
    return "\n".join(lines)


# Auto-discover at import time
_discover()
