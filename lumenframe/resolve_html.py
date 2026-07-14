"""Resolver for ``html`` layers — HTML/CSS/JS motion graphics as a video layer.

An ``html`` layer carries a tiny web document in its ``props`` (``html`` plus an
optional ``css`` / ``js``). This resolver renders that document to an ``mp4`` once
via :func:`gemia.hyperframes_adapter.render_hyperframes_clip` (HyperFrames), then
samples the produced video through the *exact same* path a normal ``video`` layer
uses (:func:`lumenframe.resolve._video_resolver`). The result composites like any
other video layer — transforms, effects, blend modes, masks all apply unchanged.

Why route through the video resolver instead of decoding frames here? So an html
layer's pixels are produced by one, well-tested sampler. The only new behaviour is
"compile HTML -> mp4", and that is cached so re-compiling the same content (e.g.
across many ``render_frame`` calls, or repeated ``compile_to_layer_stack`` runs of
an unchanged doc) renders the clip exactly **once**.

Mapping from the layer to the renderer:

* ``props.html``     -> ``stage_html`` (required; empty html resolves to nothing)
* ``props.css``      -> ``css``
* ``props.js``       -> appended to ``stage_html`` inside a ``<script>`` element
                        (HyperFrames takes html + css; JS rides along in the html)
* layer ``duration`` -> ``duration`` (falls back to the doc's total span)
* canvas ``w/h/fps`` -> ``width`` / ``height`` / ``fps``

The rendered clip's mp4 path is injected as a synthetic asset so the unmodified
``_video_resolver`` can resolve it by ``asset_id`` — keeping core layer behaviour
and the video sampling path untouched.
"""
from __future__ import annotations

import hashlib
import json
from typing import Any, Optional

from lumenframe.compile import ContentFn, ResolveContext

# ── render cache ─────────────────────────────────────────────────────────
#
# content hash -> rendered clip dict (as returned by render_hyperframes_clip).
# Keyed purely by the *content* that determines the pixels (html/css/js +
# duration + width/height/fps), so identical layers across re-compiles reuse the
# same render. This is what makes render_hyperframes_clip fire exactly once.
_RENDER_CACHE: dict[str, dict[str, Any]] = {}


def _content_hash(
    *,
    html: str,
    css: str,
    js: str,
    duration: float,
    width: int,
    height: int,
    fps: float,
) -> str:
    """Stable hash of everything that determines the rendered pixels."""
    payload = json.dumps(
        {
            "html": html,
            "css": css,
            "js": js,
            "duration": round(float(duration), 6),
            "width": int(width),
            "height": int(height),
            "fps": round(float(fps), 6),
        },
        ensure_ascii=False,
        sort_keys=True,
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def clear_render_cache() -> None:
    """Drop every cached html render (used by tests for isolation)."""
    _RENDER_CACHE.clear()


def _compose_stage_html(html: str, js: str) -> str:
    """Fold an optional ``js`` snippet into the stage html as a ``<script>``.

    HyperFrames' renderer takes ``stage_html`` + ``css``; there is no separate
    JS channel, so a layer's ``props.js`` rides along inside the html exactly as
    a hand-authored ``<script>`` block would.
    """
    if not js or not js.strip():
        return html
    return f"{html}\n<script>\n{js}\n</script>"


def _build_render_context(ctx: ResolveContext, layer: dict[str, Any]):
    """Build the HyperFramesContext the adapter requires.

    The context is render-bookkeeping only (workspace dir, ids, hashes); none of
    it changes the pixels, so it is derived deterministically from the canvas +
    layer so a stub can ignore it entirely.
    """
    import tempfile
    from pathlib import Path

    from gemia.hyperframes_adapter import HyperFramesContext

    workspace = Path(tempfile.mkdtemp(prefix="lumenframe_html_"))
    return HyperFramesContext(
        project_state={
            "width": int(ctx.width),
            "height": int(ctx.height),
            "fps": float(ctx.fps),
        },
        workspace_dir=workspace,
        session_id="lumenframe_html",
        ai_model="",
        script_hash="",
    )


def html_resolver(layer: dict[str, Any], ctx: ResolveContext) -> Optional[ContentFn]:
    """Resolve an ``html`` layer to a canvas-sized RGBA ``content_fn``.

    Renders the layer's HTML/CSS/JS to an mp4 once (cached by content hash), then
    delegates frame sampling to the existing :func:`_video_resolver`. Returns
    ``None`` (layer skipped) when there is no html to render or the render path
    cannot be produced — matching the resolver contract used elsewhere.
    """
    if str(layer.get("type", "")) != "html":
        return None

    props = layer.get("props") or {}
    html = str(props.get("html") or "")
    if not html.strip():
        return None
    css = str(props.get("css") or "")
    js = str(props.get("js") or "")

    # Layer duration maps to the clip duration; fall back to the doc's total span
    # so a zero-duration layer still produces a usable clip.
    duration = float(layer.get("duration") or 0.0)
    if duration <= 0.0:
        duration = max(float(ctx.total_frames) / float(ctx.fps or 1.0), 0.1)

    width = int(ctx.width)
    height = int(ctx.height)
    fps = float(ctx.fps)

    stage_html = _compose_stage_html(html, js)
    key = _content_hash(
        html=stage_html,
        css=css,
        js=js,
        duration=duration,
        width=width,
        height=height,
        fps=fps,
    )

    clip = _RENDER_CACHE.get(key)
    if clip is None:
        from gemia.hyperframes_adapter import render_hyperframes_clip

        render_ctx = _build_render_context(ctx, layer)
        clip = render_hyperframes_clip(
            stage_html,
            css=css,
            duration=duration,
            width=width,
            height=height,
            fps=fps,
            name=str(layer.get("id") or "html"),
            context=render_ctx,
        )
        _RENDER_CACHE[key] = clip

    path = clip.get("path") if isinstance(clip, dict) else None
    if not path:
        return None

    # Reuse the canonical video sampling path. We hand _video_resolver a synthetic
    # layer pointing at the rendered mp4 (via a synthetic asset injected into a
    # cloned context), so the produced video composites exactly like any other
    # video layer — no new sampling/decoding code lives here.
    from lumenframe.resolve import _video_resolver

    synthetic_asset_id = f"__html_render__{key[:12]}"
    synthetic_asset = {"id": synthetic_asset_id, "path": str(path)}

    # Clone the context with the synthetic asset appended so asset() resolves it
    # without mutating the caller's asset list.
    sampling_ctx = ResolveContext(
        width=ctx.width,
        height=ctx.height,
        fps=ctx.fps,
        total_frames=ctx.total_frames,
        assets=[*ctx.assets, synthetic_asset],
    )

    # Build a video-shaped layer: carry the original timing (source_in/out, speed,
    # duration) so trimming/retiming on the html layer behaves like a video clip.
    clip_duration = float(clip.get("duration") or duration) if isinstance(clip, dict) else duration
    video_layer = {
        "type": "video",
        "id": layer.get("id"),
        "asset_id": synthetic_asset_id,
        "duration": float(layer.get("duration") or clip_duration),
        "source_in": float(layer.get("source_in", 0.0)),
        "source_out": float(layer.get("source_out", 0.0)),
        "speed": float(layer.get("speed", 1.0)),
    }
    return _video_resolver(video_layer, sampling_ctx)


def register() -> None:
    """Register the ``html`` layer type so compile/resolve dispatch reaches here.

    Idempotent: a re-register (e.g. after ``reset_for_tests``) is treated as an
    override rather than an error.
    """
    from lumenframe.registry import layer_type_spec, register_layer_type

    spec = {
        "container": False,
        "defaults": {},
        "source": "core",
        "description": (
            "HTML/CSS/JS motion-graphics layer rendered to a video via "
            "HyperFrames and composited like a video layer."
        ),
    }
    if layer_type_spec("html") is None:
        register_layer_type("html", spec)
    else:
        register_layer_type("html", spec, override=True)


# Register on import so simply importing the module lights up the layer type.
register()


__all__ = ["html_resolver", "register", "clear_render_cache"]
