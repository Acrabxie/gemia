"""Renderer adapters — a VectorScene as deliverables lumenframe understands.

Two v1 outputs:

* :func:`scene_svg_document` — the raw animated SVG (a shareable file: logo
  handoff, web embed, design review).
* :func:`scene_to_html_layer` — a lumenframe ``html`` layer dict carrying the
  SVG. Dropped into any LumenDoc it rides the existing HyperFrames path
  (`lumenframe.resolve_html`): rendered once to an mp4 (content-hash cached),
  then composited exactly like a video layer — transforms, masks, blend
  modes, timeline placement all apply. `lumen_seek` / `lumen_render` /
  `lumen_render_range` work on it with zero new tooling.

The adapter protocol for future renderers (native ops, Lottie, WebGL): read
``nodes[*].tracks``, honour what you can, and return an honest
:class:`AdapterReport` — never silently approximate a focal behaviour.

Determinism note: the html layer's render cache keys on the *content hash* of
html+css+geometry (`resolve_html._content_hash`), so a byte-identical SVG
(same brief, same seed) never re-renders, and any change re-renders honestly.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from lumenframe.model import new_layer
from lumenframe.vector.svg import compile_scene

#: CSS injected next to the SVG inside the HyperFrames stage. Kept url()-free
#: (the adapter's CSS validator rejects any url(...)); the SVG carries its own
#: sizing via width/height attributes, this only removes inline-block gaps.
_STAGE_CSS = "#lumeri-stage svg { display: block; }"


@dataclass
class AdapterReport:
    """What a renderer adapter honoured vs dropped (honesty contract)."""

    renderer: str
    honored: list[str] = field(default_factory=list)
    dropped: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {"renderer": self.renderer, "honored": list(self.honored), "dropped": list(self.dropped)}


def scene_svg_document(scene: dict[str, Any]) -> str:
    """The scene as one self-contained animated ``.svg`` file (with xmlns)."""
    return compile_scene(scene, standalone=True)


def validate_html_layer(layer: dict[str, Any]) -> None:
    """Run the HyperFrames render-safety validators on a built html layer.

    Raises ``HyperFramesRenderError`` if the layer's html/css would be rejected
    at render time — call this BEFORE writing the layer into a doc so a bad
    scene never poisons the whole document's render. Degrades to a no-op if the
    adapter is unavailable (offline / stubbed), matching the optional-dependency
    convention in the rest of the vector module.
    """
    props = layer.get("props") or {}
    try:
        from gemia.hyperframes_adapter import (
            _validate_local_only_css,
            _validate_local_only_html,
        )
    except Exception:  # pragma: no cover - adapter optional
        return
    _validate_local_only_html(str(props.get("html") or ""))
    _validate_local_only_css(str(props.get("css") or ""))


def scene_to_html_layer(
    scene: dict[str, Any],
    *,
    id: str | None = None,
    name: str = "Vector Motion",
    start: float = 0.0,
    lane: int = 0,
    brief: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Wrap the scene into a lumenframe ``html`` layer dict.

    The layer's ``duration`` is the scene duration; ``props.vector_brief``
    preserves the creative brief so a later ``adjust`` can re-derive the
    scene deterministically (feedback edits the brief, never the SVG text).
    """
    svg_doc = compile_scene(scene)
    props: dict[str, Any] = {
        "html": svg_doc,
        "css": _STAGE_CSS,
        "vector_scene": {
            "seed": scene.get("seed"),
            "width": scene.get("width"),
            "height": scene.get("height"),
            "duration": scene.get("duration"),
            "plan": (scene.get("meta") or {}).get("plan"),
        },
    }
    if brief is not None:
        props["vector_brief"] = dict(brief)
    return new_layer(
        "html",
        id=id,
        name=name,
        start=float(start),
        duration=float(scene["duration"]),
        lane=int(lane),
        props=props,
    )


def html_layer_report(scene: dict[str, Any]) -> AdapterReport:
    """The html/SVG adapter honours the full v1 track vocabulary."""
    report = AdapterReport(renderer="svg+html_layer")
    seen: set[str] = set()
    from lumenframe.vector.scene import walk

    for node in walk(scene):
        seen.update((node.get("tracks") or {}).keys())
        if node.get("kind") == "particles":
            for inst in (node.get("particles") or {}).get("instances") or []:
                seen.update((inst.get("tracks") or {}).keys())
    report.honored = sorted(seen)
    return report
