"""Software render backend that adapts layer plans and compositing graphs."""
from __future__ import annotations

from collections.abc import Mapping, Sequence
from copy import deepcopy
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from gemia.video.backends.base import (
    LayerPlan,
    RenderProfile,
    RenderResult,
    RenderSource,
)
from gemia.video.compositing_graph import (
    CompiledCompositingPlan,
    CompiledNodeStep,
    CompositingGraph,
    NeutralGraphBackend,
    compile_compositing_graph,
)
from gemia.video.layers import LayerStack, execute_layer_plan


class SoftwareGraphBackend(NeutralGraphBackend):
    """Neutral compiled graph with an explicit software backend label."""

    name = "software"


@dataclass(slots=True)
class SoftwareRenderBackend:
    """Execute layer-plan and graph renders through the existing software stack."""

    graph_backend: SoftwareGraphBackend = field(default_factory=SoftwareGraphBackend)
    name: str = "software"

    def compile_graph(self, graph: CompositingGraph) -> CompiledCompositingPlan:
        return compile_compositing_graph(graph, backend=self.graph_backend)

    def render(
        self,
        source: RenderSource,
        output_path: str | Path,
        *,
        profile: RenderProfile | None = None,
    ) -> RenderResult:
        active_profile = profile or RenderProfile.final()
        if isinstance(source, LayerStack):
            return self.render_layer_stack(source, output_path, profile=active_profile)
        if isinstance(source, CompositingGraph):
            return self.render_graph(source, output_path, profile=active_profile)
        if isinstance(source, CompiledCompositingPlan):
            return self.render_compiled_plan(source, output_path, profile=active_profile)
        if isinstance(source, Mapping):
            return self.render_layer_plan(source, output_path, profile=active_profile)
        raise TypeError(f"Unsupported render source: {type(source)!r}")

    def render_preview(
        self,
        source: RenderSource,
        output_path: str | Path,
        *,
        profile: RenderProfile | None = None,
    ) -> RenderResult:
        return self.render(source, output_path, profile=profile or RenderProfile.preview())

    def render_final(
        self,
        source: RenderSource,
        output_path: str | Path,
        *,
        profile: RenderProfile | None = None,
    ) -> RenderResult:
        return self.render(source, output_path, profile=profile or RenderProfile.final())

    def render_layer_plan(
        self,
        plan: LayerPlan,
        output_path: str | Path,
        *,
        profile: RenderProfile,
    ) -> RenderResult:
        stack = execute_layer_plan(_materialize_layer_plan(plan))
        return self._render_stack(
            stack,
            output_path,
            profile=profile,
            source_kind="layer_plan",
        )

    def render_graph(
        self,
        graph: CompositingGraph,
        output_path: str | Path,
        *,
        profile: RenderProfile,
    ) -> RenderResult:
        compiled = self.compile_graph(graph)
        return self._render_compiled_plan(
            compiled,
            output_path,
            profile=profile,
            source_kind="compositing_graph",
        )

    def render_compiled_plan(
        self,
        compiled: CompiledCompositingPlan,
        output_path: str | Path,
        *,
        profile: RenderProfile,
    ) -> RenderResult:
        return self._render_compiled_plan(
            compiled,
            output_path,
            profile=profile,
            source_kind="compiled_compositing_plan",
        )

    def _render_compiled_plan(
        self,
        compiled: CompiledCompositingPlan,
        output_path: str | Path,
        *,
        profile: RenderProfile,
        source_kind: str,
    ) -> RenderResult:
        if compiled.backend not in {"neutral", self.name}:
            raise ValueError(
                f"SoftwareRenderBackend cannot execute compiled backend {compiled.backend!r}."
            )
        plan = _layer_plan_from_compiled_plan(compiled)
        stack = execute_layer_plan(plan)
        return self._render_stack(
            stack,
            output_path,
            profile=profile,
            source_kind=source_kind,
            compiled_plan=compiled,
        )

    def render_layer_stack(
        self,
        stack: LayerStack,
        output_path: str | Path,
        *,
        profile: RenderProfile,
    ) -> RenderResult:
        return self._render_stack(
            stack,
            output_path,
            profile=profile,
            source_kind="layer_stack",
        )

    def _render_stack(
        self,
        stack: LayerStack,
        output_path: str | Path,
        *,
        profile: RenderProfile,
        source_kind: str,
        compiled_plan: CompiledCompositingPlan | None = None,
    ) -> RenderResult:
        rendered = stack.render_to_video(
            output_path,
            codec=profile.codec,
            background_color=profile.background_color,
            start_frame=profile.start_frame,
            end_frame=profile.end_frame,
            step=profile.step,
        )
        return RenderResult(
            backend=self.name,
            output_path=rendered,
            source_kind=source_kind,
            profile=profile,
            width=int(stack.width),
            height=int(stack.height),
            fps=float(stack.fps) / max(int(profile.step), 1),
            total_frames=_selected_frame_count(
                total_frames=int(stack.total_frames),
                start_frame=int(profile.start_frame),
                end_frame=profile.end_frame,
                step=int(profile.step),
            ),
            compiled_plan=compiled_plan,
        )


def _materialize_layer_plan(plan: LayerPlan) -> dict[str, Any]:
    return deepcopy(dict(plan))


def _layer_plan_from_compiled_plan(compiled: CompiledCompositingPlan) -> dict[str, Any]:
    if str(compiled.metadata.get("source", "")) != "layer_plan":
        raise ValueError(
            "SoftwareRenderBackend can only reconstruct compositing graphs built from layer plans. "
            "Render LayerStack inputs directly instead of compiling them through the graph."
        )

    step_by_id = {step.id: step for step in compiled.steps}
    layer_order = _layer_order(compiled)
    layer_nodes = compiled.metadata.get("layer_nodes")
    ordered_layers: list[dict[str, Any]] = []
    inferred_total_frames = 1

    for layer_id in layer_order:
        node_ids = []
        if isinstance(layer_nodes, Mapping):
            raw_node_ids = layer_nodes.get(layer_id)
            if isinstance(raw_node_ids, Sequence) and not isinstance(raw_node_ids, (str, bytes)):
                node_ids = [str(node_id) for node_id in raw_node_ids]
        if not node_ids:
            node_ids = [step.id for step in compiled.steps if step.layer_id == layer_id]

        layer_spec = _layer_spec_from_steps(
            layer_id,
            [step_by_id[node_id] for node_id in node_ids if node_id in step_by_id],
        )
        if layer_spec is None:
            continue
        inferred_total_frames = max(inferred_total_frames, _layer_end_frame(layer_spec) or 1)
        ordered_layers.append(layer_spec)

    if not ordered_layers:
        raise ValueError("Compiled compositing plan does not contain any renderable layer steps.")

    plan: dict[str, Any] = {"layers": ordered_layers}
    declared_width = _as_int(compiled.metadata.get("width"))
    declared_height = _as_int(compiled.metadata.get("height"))
    declared_fps = _as_float(compiled.metadata.get("fps"))
    declared_total_frames = _as_int(compiled.metadata.get("total_frames"))

    if declared_width is not None:
        plan["width"] = declared_width
    if declared_height is not None:
        plan["height"] = declared_height
    if declared_fps is not None:
        plan["fps"] = declared_fps
    plan["total_frames"] = max(inferred_total_frames, declared_total_frames or 1)
    return plan


def _layer_order(compiled: CompiledCompositingPlan) -> list[str]:
    layer_order: list[str] = []
    raw_order = compiled.metadata.get("layer_order")
    if isinstance(raw_order, Sequence) and not isinstance(raw_order, (str, bytes)):
        layer_order.extend(str(item) for item in raw_order)
    for step in compiled.steps:
        if step.layer_id and step.layer_id not in layer_order:
            layer_order.append(step.layer_id)
    return layer_order


def _layer_spec_from_steps(
    layer_id: str,
    steps: Sequence[CompiledNodeStep],
) -> dict[str, Any] | None:
    spec: dict[str, Any] = {"id": layer_id}
    saw_primary_source = False

    for step in steps:
        if step.kind == "source":
            media_type = str(step.params.get("media_type", ""))
            if media_type == "mask":
                if step.params.get("source"):
                    spec["mask_source"] = str(step.params["source"])
                continue
            saw_primary_source = True
            spec["type"] = media_type
            _copy_if_present(
                step.params,
                spec,
                "name",
                "source",
                "text",
                "html",
                "color",
                "size",
                "font_config",
                "blur_radius",
                "gaussian_blur_radius",
                "metadata",
            )
            _copy_int_if_present(step.params, spec, "start_frame", "end_frame", "duration")
        elif step.kind == "picture_chain":
            if step.params.get("ops"):
                spec["primitives"] = deepcopy(step.params["ops"])
        elif step.kind == "transform":
            _copy_if_present(step.params, spec, "position")
            _copy_float_if_present(step.params, spec, "scale", "rotation_deg")
        elif step.kind == "automation":
            if step.params.get("tracks"):
                spec["keyframes"] = deepcopy(step.params["tracks"])
        elif step.kind == "composite":
            _copy_int_if_present(step.params, spec, "z_index", "start_frame", "end_frame")
            _copy_float_if_present(step.params, spec, "opacity")
            if "blend_mode" in step.params and step.params["blend_mode"] is not None:
                spec["blend_mode"] = str(step.params["blend_mode"])

    if not saw_primary_source:
        return None
    return spec


def _copy_if_present(source: Mapping[str, Any], target: dict[str, Any], *keys: str) -> None:
    for key in keys:
        if key in source and source[key] is not None:
            target[key] = deepcopy(source[key])


def _copy_int_if_present(source: Mapping[str, Any], target: dict[str, Any], *keys: str) -> None:
    for key in keys:
        value = _as_int(source.get(key))
        if value is not None:
            target[key] = value


def _copy_float_if_present(source: Mapping[str, Any], target: dict[str, Any], *keys: str) -> None:
    for key in keys:
        value = _as_float(source.get(key))
        if value is not None:
            target[key] = value


def _selected_frame_count(
    *,
    total_frames: int,
    start_frame: int,
    end_frame: int | None,
    step: int,
) -> int:
    start = max(int(start_frame), 0)
    stop = int(total_frames) if end_frame is None else min(int(end_frame), int(total_frames))
    if stop <= start:
        return 0
    return len(range(start, stop, max(int(step), 1)))


def _layer_end_frame(layer_spec: Mapping[str, Any]) -> int | None:
    end_frame = _as_int(layer_spec.get("end_frame"))
    if end_frame is not None:
        return end_frame
    start_frame = _as_int(layer_spec.get("start_frame")) or 0
    duration = _as_int(layer_spec.get("duration"))
    if duration is None:
        return None
    return start_frame + duration


def _as_int(value: Any) -> int | None:
    if value is None:
        return None
    return int(value)


def _as_float(value: Any) -> float | None:
    if value is None:
        return None
    return float(value)


__all__ = [
    "SoftwareGraphBackend",
    "SoftwareRenderBackend",
]
