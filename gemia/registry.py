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
import importlib.util
import inspect
import pkgutil
import sys
import types
from dataclasses import dataclass, field
from typing import Any, Callable, Iterable

# Packages to scan
_PACKAGES = ["gemia.picture", "gemia.audio", "gemia.video"]
_CATALOG_DOMAINS = ("picture", "audio", "video")
_CATALOG_CACHE: dict[tuple[str, ...], str] = {}
_CATEGORY_CATALOG_CACHE: dict[tuple[str, ...], str] = {}

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
    # composition / preview helpers: structured plan args, not stable AI primitive calls
    "gemia.video.layers.render_layer_plan",
    "gemia.video.preview.render_shadow_preview",
    "gemia.video.review.review_real_media_artifact",
    "gemia.video.html_graphics.lottie_metadata",
    "gemia.video.html_graphics.render_html_frame",
    "gemia.video.html_graphics.render_lottie_frame",
    "gemia.video.compositing_graph.build_compositing_graph",
    "gemia.video.compositing_graph.build_compositing_graph_from_layer_plan",
    "gemia.video.compositing_graph.build_compositing_graph_from_layer_stack",
    "gemia.video.compositing_graph.compile_compositing_graph",
    # mixer: require numpy arrays as named args
    "gemia.audio.mixer.create_bus",
    "gemia.audio.mixer.sidechain_compress",
    "gemia.audio.mixer.auto_duck",
    # audio repair: waveform: np.ndarray arg
    "gemia.audio.repair.reduce_noise",
    "gemia.audio.repair.remove_hum",
    "gemia.audio.repair.de_ess",
    "gemia.audio.repair.remove_reverb",
    # video analysis: returns coordinate/tracking data, not a media file
    "gemia.video.analysis.track_point",
    "gemia.video.analysis.track_plane",
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
_DEFERRED_PRIMITIVE_MODULES = {
    "gemia.video.html_graphics": "video",
    "gemia.video.layer_flow": "video",
    "gemia.video.advanced_noise_reduction": "video"
}
_DEFERRED_REGISTERING = False


_CORE_FQNS = {
    "gemia.video.timeline.cut",
    "gemia.video.timeline.ripple_trim",
    "gemia.video.timeline.speed",
    "gemia.video.timeline.speed_ramp",
    "gemia.video.timeline.concat",
    "gemia.video.timeline.nest_clips",
    "gemia.video.timeline.reverse",
    "gemia.video.timeline.rotate_video",
    "gemia.video.timeline.flip_video",
    "gemia.video.timeline.freeze_frame",
    "gemia.video.timeline.timeline_from_script",
    "gemia.video.transitions.transition_dissolve",
    "gemia.video.transitions.transition_wipe",
    "gemia.video.transitions.transition_push",
    "gemia.picture.color.color_grade",
    "gemia.picture.color.adjust_exposure",
    "gemia.picture.color.adjust_temperature",
    "gemia.picture.color.lift_gamma_gain",
    "gemia.video.frames.stabilize",
    "gemia.video.frames.retime",
    "gemia.video.subtitles.add_subtitle_track",
    "gemia.video.subtitles.add_text",
    "gemia.video.subtitles.auto_subtitle",
    "gemia.video.html_graphics.render_html_graphics_plan",
    "gemia.video.layer_flow.render_layer_workflow",
    "gemia.picture.generative.generate_image",
    "gemia.picture.generative.style_transfer",
    "gemia.picture.generative.edit_image",
    "gemia.video.generative.generate_video",
    "gemia.video.generative.generate_video_from_image",
    "gemia.video.generative.extend_video",
    "gemia.video.generative.generate_broll",
    "gemia.video.blender_link.render_blender_link_operation",
    "gemia.video.analysis.detect_scenes",
    "gemia.video.analysis.get_metadata",
    "gemia.video.analysis.auto_highlight",
    "gemia.video.export.export_preset",
    "gemia.video.export.batch_export",
    "gemia.video.keyframes_curves_manifest.render_keyframes_curves_loop_pingpong_manifest",
    "gemia.video.premiere_media_intelligence.render_premiere_media_intelligence_visual_marker_search_manifest",
    "gemia.video.premiere_generative_extend.render_premiere_generative_extend_edit_handle_manifest",
    "gemia.video.advanced_noise_reduction.render_advanced_noise_reduction_profile_manifest",
    "gemia.video.optical_flow_speed_change.render_optical_flow_speed_change_manifest",
    "gemia.video.track_follow_mask.render_track_follow_objects_mask_manifest",
    "gemia.video.fusion_effect_animation.render_animate_fusion_effects_edit_page_manifest",
    "gemia.video.replay_editor_multicam.render_replay_editor_multicam_action_manifest",
    "gemia.video.blended_denoise_flow_tracker_replay_scene.render_blended_denoise_flow_tracker_replay_scene",
}

_CATEGORY_MATCHERS: dict[str, tuple[str, ...]] = {
    "core": (),
    "timeline": (
        ".timeline.",
        ".transitions.",
        "transition",
        "crossfade",
        "concat",
        "trim",
        "speed",
        "retime",
        "reverse",
        "freeze_frame",
    ),
    "color": (
        ".color.",
        "color_",
        "_color",
        "grade",
        "lut",
        "exposure",
        "temperature",
        "gamma",
        "hdr",
        "tone",
    ),
    "blur": ("blur", "bokeh", "defocus"),
    "edge": ("edge", "sharpen", "sharp", "outline", "detail", "canny"),
    "stylize": (
        "styl",
        "film",
        "grain",
        "glitch",
        "vhs",
        "watercolor",
        "sketch",
        "pencil",
        "halftone",
        "solarize",
    ),
    "composite": (
        ".composite.",
        "composit",
        "blend",
        "mask",
        "chroma",
        "overlay",
        "watermark",
        "split",
        "layer_flow",
    ),
    "io": (
        ".export.",
        ".proxy.",
        ".timeline_assets.",
        "thumbnail",
        "probe_media",
        "resize",
        "crop",
        "pad",
        "geometry",
    ),
    "generative": (".generative.", "generate_", "generative", "broll", "b-roll", "veo"),
    "analysis": (
        ".analysis.",
        ".review.",
        ".summary.",
        ".intellisearch",
        "metadata",
        "detect",
        "slate",
        "monitor",
        "histogram",
        "vectorscope",
    ),
    "audio": (
        "gemia.audio.",
        "music",
        "sound",
        "speech",
        "dialogue",
        "subtitle_multicam",
        "normalize",
        "loudness",
    ),
    "spatial": ("blender", "lumerilink", "spatial", "parallax", "depth", "hologram", "3d"),
    "text_graphics": (
        "subtitle",
        "subtitles",
        "text",
        "title",
        "html_graphics",
        "lottie",
        "motion_graphics",
        "ad_graphics",
        "cta",
        "callout",
        "promo",
        "shimmer",
        "animated_subtitles",
        "lower_third",
        "layer_flow",
        "fonts",
    ),
    "face": ("face", "portrait", "skin", "blemish", "reshaper", "age"),
    "repair": ("repair", "denoise", "noise", "deblur", "ultrasharpen", "stabilize", "restore"),
}
_MAX_CATEGORY_ITEMS = 18
_CATEGORY_PINNED_FQNS: dict[str, tuple[str, ...]] = {
    "timeline": (
        "gemia.video.timeline.cut",
        "gemia.video.timeline.speed",
        "gemia.video.timeline.speed_ramp",
        "gemia.video.timeline.concat",
        "gemia.video.timeline.reverse",
        "gemia.video.transitions.transition_dissolve",
        "gemia.video.transitions.transition_wipe",
        "gemia.video.transitions.transition_push",
    ),
    "color": (
        "gemia.picture.color.color_grade",
        "gemia.picture.color.adjust_exposure",
        "gemia.picture.color.adjust_temperature",
        "gemia.picture.color.lift_gamma_gain",
        "gemia.picture.color.log_to_linear",
        "gemia.picture.color.color_space_convert",
        "gemia.video.generative.ai_color_grade",
        "gemia.video.effects.video_color_correct",
    ),
    "blur": (
        "gemia.picture.enhance.motion_blur",
        "gemia.video.effects.video_motion_blur",
        "gemia.video.cinefocus.render_cinefocus_plan",
    ),
    "edge": (
        "gemia.picture.analysis.edge_detect",
        "gemia.video.ultrasharpen.render_ultrasharpen_plan",
    ),
    "stylize": (
        "gemia.picture.generative.style_transfer",
        "gemia.picture.enhance.image_watercolor",
        "gemia.picture.enhance.image_sketch_color",
        "gemia.picture.enhance.image_color_halftone",
        "gemia.video.generative.film_look_creator",
    ),
    "composite": (
        "gemia.video.layer_flow.render_layer_workflow",
        "gemia.video.html_graphics.render_html_graphics_plan",
        "gemia.video.masking.render_chroma_key_preview",
        "gemia.video.masking.render_luma_key_preview",
        "gemia.video.masking.render_shape_mask_preview",
        "gemia.video.masking.render_masked_composite",
        "gemia.video.compositing.color_wheels",
    ),
    "io": (
        "gemia.video.export.export_preset",
        "gemia.video.export.batch_export",
        "gemia.video.export.proxy_generate",
        "gemia.video.timeline_assets.probe_media",
        "gemia.video.timeline_assets.generate_timeline_thumbnails",
    ),
    "generative": (
        "gemia.picture.generative.generate_image",
        "gemia.picture.generative.style_transfer",
        "gemia.picture.generative.edit_image",
        "gemia.video.generative.generate_video",
        "gemia.video.generative.generate_video_from_image",
        "gemia.video.generative.extend_video",
        "gemia.video.generative.generate_broll",
    ),
    "analysis": (
        "gemia.video.analysis.detect_scenes",
        "gemia.video.analysis.get_metadata",
        "gemia.video.analysis.auto_highlight",
        "gemia.video.slate_id.render_slate_id_metadata_plan",
        "gemia.picture.analysis.waveform_monitor",
        "gemia.picture.analysis.vectorscope",
    ),
    "audio": (
        "gemia.audio.effects.normalize_loudness",
        "gemia.audio.effects.match_eq",
        "gemia.audio.effects.fade_in",
        "gemia.audio.effects.fade_out",
        "gemia.video.music_editor.render_ai_music_editor_plan",
        "gemia.video.dialogue_matcher.render_dialogue_matcher_plan",
        "gemia.video.speech_generator.render_ai_speech_generator_plan",
    ),
    "spatial": (
        "gemia.video.blender_link.render_blender_link_operation",
        "gemia.video.blender_link.render_blender_spatial_scene",
        "gemia.video.blender_link.blender_link_capabilities",
    ),
    "text_graphics": (
        "gemia.video.subtitles.add_subtitle_track",
        "gemia.video.subtitles.add_text",
        "gemia.video.subtitles.auto_subtitle",
        "gemia.video.animated_subtitles.render_ai_animated_subtitles_plan",
        "gemia.video.html_graphics.render_html_graphics_plan",
        "gemia.video.motion_graphics.render_mg_title_card",
        "gemia.video.motion_graphics.render_mg_formula_reveal",
        "gemia.video.ad_graphics.render_ad_title_pack",
        "gemia.video.ad_graphics.render_lower_third",
        "gemia.video.ad_graphics.render_cta_card",
        "gemia.video.ad_graphics.render_product_callout",
        "gemia.video.ad_graphics.render_shimmer_sweep",
    ),
    "face": (
        "gemia.video.face_age.render_face_age_plan",
        "gemia.video.face_reshaper.render_face_reshaper_plan",
        "gemia.video.blemish.render_blemish_removal_plan",
        "gemia.picture.enhance.image_detect_faces",
        "gemia.picture.enhance.image_pixelate_faces",
    ),
    "repair": (
        "gemia.video.motion_deblur.render_motion_deblur_plan",
        "gemia.video.ultrasharpen.render_ultrasharpen_plan",
        "gemia.video.frames.stabilize",
    ),
}


def _discover() -> None:
    """Walk primitive packages and register all public functions."""
    for pkg_name in _PACKAGES:
        search_locations: list[str] | None = None
        try:
            pkg = importlib.import_module(pkg_name)
            search_locations = list(pkg.__path__)
        except ImportError:
            spec = importlib.util.find_spec(pkg_name)
            if spec is None or spec.submodule_search_locations is None:
                continue
            search_locations = list(spec.submodule_search_locations)
            # Some package __init__ files import optional media dependencies.
            # Registry discovery should still be able to import independent
            # submodules such as gemia.video.timeline in a lean test env.
            placeholder = types.ModuleType(pkg_name)
            placeholder.__path__ = search_locations  # type: ignore[attr-defined]
            placeholder.__package__ = pkg_name
            placeholder.__spec__ = spec
            sys.modules[pkg_name] = placeholder
        domain = pkg_name.split(".")[-1]
        for _importer, modname, _ispkg in pkgutil.walk_packages(
            search_locations, prefix=pkg_name + "."
        ):
            try:
                mod = importlib.import_module(modname)
            except ImportError:
                continue
            _register_module_functions(modname, mod, domain=domain)

    # Layer-flow imports can be skipped during package-level circular discovery.
    # Register it after the base package walk so planner-facing primitives stay reachable.
    for modname in ("gemia.video.layer_flow",):
        try:
            mod = importlib.import_module(modname)
        except ImportError:
            continue
        _register_module_functions(modname, mod, domain="video")


def _register_module_functions(modname: str, mod: Any, *, domain: str) -> None:
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
    clear_catalog_cache()


def _register_deferred_modules() -> None:
    """Register planner primitives that can be skipped during circular package import."""
    global _DEFERRED_REGISTERING
    if _DEFERRED_REGISTERING:
        return
    _DEFERRED_REGISTERING = True
    try:
        for modname, domain in _DEFERRED_PRIMITIVE_MODULES.items():
            if any(fqn.startswith(modname + ".") for fqn in _REGISTRY):
                continue
            try:
                mod = importlib.import_module(modname)
            except ImportError:
                continue
            _register_module_functions(modname, mod, domain=domain)
    finally:
        _DEFERRED_REGISTERING = False


def _format_annotation(ann: Any) -> str:
    """Best-effort conversion of a type annotation to a readable string."""
    if isinstance(ann, str):
        return ann
    if hasattr(ann, "__name__"):
        return ann.__name__
    return str(ann).replace("typing.", "")


def get_registry() -> dict[str, PrimitiveInfo]:
    """Return the full registry dict."""
    _register_deferred_modules()
    return _REGISTRY


def resolve(fqn: str) -> Callable:
    """Resolve a fully-qualified function name to its callable."""
    _register_deferred_modules()
    info = _REGISTRY.get(fqn)
    if info is None:
        raise KeyError(f"Unknown primitive: {fqn}")
    return info.func


def get_info(fqn: str) -> PrimitiveInfo:
    """Return the PrimitiveInfo for a function."""
    _register_deferred_modules()
    info = _REGISTRY.get(fqn)
    if info is None:
        raise KeyError(f"Unknown primitive: {fqn}")
    return info


def clear_catalog_cache() -> None:
    """Clear cached catalog text after tests or deferred registration."""
    _CATALOG_CACHE.clear()
    _CATEGORY_CATALOG_CACHE.clear()


def catalog_for_prompt(*domains: str) -> str:
    """Build a compact text catalog for injection into a Gemini system prompt.

    Each function is one line: ``fqn(params) — first line of docstring``.
    Functions in _EXCLUDED_FROM_CATALOG are omitted.
    """
    _register_deferred_modules()
    domain_filter = tuple(domain for domain in domains if domain)
    if not domain_filter:
        domain_filter = _CATALOG_DOMAINS
    cache_key = ("full", *domain_filter)
    cached = _CATALOG_CACHE.get(cache_key)
    if cached is not None:
        return cached
    lines: list[str] = []
    for domain_label in domain_filter:
        if domain_label not in _CATALOG_DOMAINS:
            continue
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
    text = "\n".join(lines)
    _CATALOG_CACHE[cache_key] = text
    return text


def catalog_for_categories(categories: Iterable[str] | None = None) -> str:
    """Return a small planner catalog for the requested semantic categories.

    ``core`` is always included so high-frequency timeline, color, text,
    generative, analysis, and LumeriLink primitives remain visible even when
    routing only identifies a specialized bucket.
    """
    _register_deferred_modules()
    normalized: list[str] = []
    for category in categories or ["core"]:
        value = str(category or "").strip().lower()
        if value and value in _CATEGORY_MATCHERS and value not in normalized:
            normalized.append(value)
    if not normalized:
        normalized = ["core"]
    if "core" not in normalized:
        normalized.insert(0, "core")
    cache_key = tuple(normalized)
    cached = _CATEGORY_CATALOG_CACHE.get(cache_key)
    if cached is not None:
        return cached

    selected: dict[str, PrimitiveInfo] = {}
    for category in normalized:
        for fqn, info in _category_infos(category):
            selected[fqn] = info

    grouped: dict[str, list[PrimitiveInfo]] = {}
    for info in selected.values():
        grouped.setdefault(info.domain, []).append(info)

    lines: list[str] = []
    lines.append("## selected primitive catalog")
    lines.append(f"categories: {', '.join(normalized)}")
    for domain in _CATALOG_DOMAINS:
        infos = sorted(grouped.get(domain, []), key=lambda item: item.fqn)
        if not infos:
            continue
        lines.append(f"### gemia.{domain}")
        for info in infos:
            lines.append(_compact_catalog_line(info))
    text = "\n".join(lines)
    _CATEGORY_CATALOG_CACHE[cache_key] = text
    return text


def _category_infos(category: str) -> list[tuple[str, PrimitiveInfo]]:
    if category == "core":
        return [
            (fqn, info)
            for fqn, info in sorted(_REGISTRY.items())
            if fqn in _CORE_FQNS and fqn not in _EXCLUDED_FROM_CATALOG
        ]
    needles = _CATEGORY_MATCHERS.get(category, ())
    matches: dict[str, PrimitiveInfo] = {}
    for fqn in _CATEGORY_PINNED_FQNS.get(category, ()):
        info = _REGISTRY.get(fqn)
        if info is not None and fqn not in _EXCLUDED_FROM_CATALOG:
            matches[fqn] = info
    for fqn, info in sorted(_REGISTRY.items()):
        if fqn in _EXCLUDED_FROM_CATALOG:
            continue
        if len(matches) >= _MAX_CATEGORY_ITEMS:
            break
        haystack = f"{fqn} {info.module} {info.name} {info.docstring}".lower()
        if any(needle.lower() in haystack for needle in needles):
            matches[fqn] = info
    return sorted(matches.items())


def _compact_catalog_line(info: PrimitiveInfo) -> str:
    params: list[str] = []
    for pname, pinfo in info.params.items():
        s = pname
        if "annotation" in pinfo:
            s += f": {pinfo['annotation']}"
        if "default" in pinfo:
            s += f"={pinfo['default']}"
        if pinfo.get("keyword_only"):
            s = f"*, {s}" if not params or not params[-1].startswith("*") else s
        params.append(s)
    doc_first = info.docstring.split("\n")[0] if info.docstring else ""
    if len(doc_first) > 110:
        doc_first = doc_first[:107].rstrip() + "..."
    suffix = f" — {doc_first}" if doc_first else ""
    return f"- {info.fqn}({', '.join(params)}){suffix}"


# Auto-discover at import time
_discover()
