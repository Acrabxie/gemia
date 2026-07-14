"""Backend-agnostic compositing graph IR for layer-first editing."""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Mapping, Protocol, Sequence

from gemia.video.keyframe import KeyframeTrack
from gemia.video.layers import Layer, LayerStack, materialize_layer_plan

METRIC_KEYS = ("width", "height", "fps", "total_frames")
METRIC_DEFAULTS = {
    "width": 1920,
    "height": 1080,
    "fps": 30.0,
    "total_frames": 1,
}
METRIC_SOURCE_VALUES = {"explicit", "inferred", "default"}


@dataclass(frozen=True)
class NodeOutputRef:
    """Reference a specific output emitted by a graph node."""

    node_id: str
    output: str = "output"

    def to_dict(self) -> dict[str, str]:
        return {"node_id": self.node_id, "output": self.output}


@dataclass(frozen=True)
class CompositingEdge:
    """Connect a node output to a named input on another node."""

    source: str
    target: str
    source_output: str = "output"
    target_input: str = "input"

    def source_ref(self) -> NodeOutputRef:
        return NodeOutputRef(node_id=self.source, output=self.source_output)

    def to_dict(self) -> dict[str, str]:
        return {
            "source": self.source,
            "target": self.target,
            "source_output": self.source_output,
            "target_input": self.target_input,
        }


@dataclass
class CompositingNode:
    """Single compositing operation or adapter node in the graph."""

    id: str
    kind: str
    params: dict[str, Any] = field(default_factory=dict)
    layer_id: str | None = None
    tags: tuple[str, ...] = ()
    backend_hints: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "kind": self.kind,
            "layer_id": self.layer_id,
            "params": dict(self.params),
            "tags": list(self.tags),
            "backend_hints": dict(self.backend_hints),
        }


@dataclass
class CompositingGraph:
    """DAG of layer, track, and compositing nodes."""

    nodes: dict[str, CompositingNode] = field(default_factory=dict)
    edges: list[CompositingEdge] = field(default_factory=list)
    outputs: dict[str, NodeOutputRef] = field(default_factory=dict)
    metadata: dict[str, Any] = field(default_factory=dict)

    def add_node(self, node: CompositingNode) -> CompositingNode:
        if node.id in self.nodes:
            raise ValueError(f"Duplicate compositing node id: {node.id}")
        self.nodes[node.id] = node
        return node

    def node(self, node_id: str) -> CompositingNode:
        try:
            return self.nodes[node_id]
        except KeyError as exc:
            raise KeyError(f"Unknown compositing node: {node_id}") from exc

    def connect(
        self,
        source: str,
        target: str,
        *,
        source_output: str = "output",
        target_input: str = "input",
    ) -> CompositingEdge:
        if source not in self.nodes:
            raise KeyError(f"Unknown source node: {source}")
        if target not in self.nodes:
            raise KeyError(f"Unknown target node: {target}")
        for edge in self.edges:
            if edge.target == target and edge.target_input == target_input:
                raise ValueError(
                    f"Input '{target_input}' on node '{target}' is already connected."
                )
        edge = CompositingEdge(
            source=source,
            target=target,
            source_output=source_output,
            target_input=target_input,
        )
        self.edges.append(edge)
        return edge

    def incoming_edges(self, node_id: str) -> list[CompositingEdge]:
        return [edge for edge in self.edges if edge.target == node_id]

    def outgoing_edges(self, node_id: str) -> list[CompositingEdge]:
        return [edge for edge in self.edges if edge.source == node_id]

    def layer_nodes(self, layer_id: str) -> list[CompositingNode]:
        return [node for node in self.nodes.values() if node.layer_id == layer_id]

    def topological_order(self) -> list[CompositingNode]:
        node_ids = _stable_topological_order(self)
        return [self.nodes[node_id] for node_id in node_ids]

    def validate(self) -> None:
        seen_inputs: set[tuple[str, str]] = set()
        for edge in self.edges:
            if edge.source not in self.nodes:
                raise ValueError(f"Edge references unknown source node: {edge.source}")
            if edge.target not in self.nodes:
                raise ValueError(f"Edge references unknown target node: {edge.target}")
            key = (edge.target, edge.target_input)
            if key in seen_inputs:
                raise ValueError(
                    f"Input '{edge.target_input}' on node '{edge.target}' has multiple sources."
                )
            seen_inputs.add(key)
        for name, output in self.outputs.items():
            if output.node_id not in self.nodes:
                raise ValueError(
                    f"Output '{name}' references unknown node '{output.node_id}'."
                )
        _stable_topological_order(self)

    def to_dict(self) -> dict[str, Any]:
        return {
            "metadata": dict(self.metadata),
            "nodes": [node.to_dict() for node in self.nodes.values()],
            "edges": [edge.to_dict() for edge in self.edges],
            "outputs": {
                name: output.to_dict() for name, output in self.outputs.items()
            },
        }


@dataclass(frozen=True)
class CompiledNodeStep:
    """Backend-targeted execution step emitted from the graph."""

    id: str
    kind: str
    inputs: dict[str, NodeOutputRef]
    params: dict[str, Any]
    layer_id: str | None = None
    backend_hints: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "kind": self.kind,
            "layer_id": self.layer_id,
            "inputs": {
                name: value.to_dict() for name, value in self.inputs.items()
            },
            "params": dict(self.params),
            "backend_hints": dict(self.backend_hints),
        }


@dataclass
class CompiledCompositingPlan:
    """Ordered backend plan emitted from a compositing graph."""

    backend: str
    steps: list[CompiledNodeStep]
    outputs: dict[str, NodeOutputRef]
    metadata: dict[str, Any] = field(default_factory=dict)

    def step(self, node_id: str) -> CompiledNodeStep:
        for item in self.steps:
            if item.id == node_id:
                return item
        raise KeyError(f"Unknown compiled node step: {node_id}")

    def to_dict(self) -> dict[str, Any]:
        return {
            "backend": self.backend,
            "metadata": dict(self.metadata),
            "steps": [step.to_dict() for step in self.steps],
            "outputs": {
                name: output.to_dict() for name, output in self.outputs.items()
            },
        }


class GraphBackend(Protocol):
    """Compile compositing nodes into a backend-specific execution step."""

    name: str

    def compile_node(
        self,
        node: CompositingNode,
        *,
        inputs: Mapping[str, NodeOutputRef],
        graph: CompositingGraph,
    ) -> CompiledNodeStep:
        ...


class NeutralGraphBackend:
    """Pass-through backend that preserves the graph as a stable ordered plan."""

    name = "neutral"

    def compile_node(
        self,
        node: CompositingNode,
        *,
        inputs: Mapping[str, NodeOutputRef],
        graph: CompositingGraph,
    ) -> CompiledNodeStep:
        del graph
        return CompiledNodeStep(
            id=node.id,
            kind=node.kind,
            layer_id=node.layer_id,
            inputs=dict(inputs),
            params=dict(node.params),
            backend_hints=dict(node.backend_hints),
        )


def build_compositing_graph(
    source: LayerStack | Mapping[str, Any],
) -> CompositingGraph:
    """Build a compositing graph from either the current LayerStack or a layer plan."""
    if isinstance(source, LayerStack):
        return build_compositing_graph_from_layer_stack(source)
    return build_compositing_graph_from_layer_plan(source)


def build_compositing_graph_from_layer_plan(
    plan: Mapping[str, Any],
) -> CompositingGraph:
    """Translate a Gemini-style layer plan into a compositing DAG."""
    source_plan = dict(plan)
    materialized_plan = materialize_layer_plan(source_plan)
    graph = CompositingGraph(
        metadata=_layer_plan_metadata(materialized_plan, source_plan=source_plan),
    )
    track_node = graph.add_node(
        CompositingNode(
            id="track_main",
            kind="track",
            params={
                "track_id": "main",
                "width": graph.metadata["width"],
                "height": graph.metadata["height"],
                "fps": graph.metadata["fps"],
                "total_frames": graph.metadata["total_frames"],
            },
            tags=("track", "root"),
            backend_hints={"attachable_backends": ["neutral", "mlt", "gstreamer", "gpu"]},
        )
    )
    current_canvas = track_node.id
    layer_order: list[str] = []

    for index, layer_spec in _sorted_layer_specs(materialized_plan.get("layers", [])):
        layer_id = _plan_layer_id(layer_spec, index=index, used=layer_order)
        current_canvas = _append_plan_layer_branch(
            graph,
            layer_spec,
            layer_id=layer_id,
            layer_index=index,
            current_canvas=current_canvas,
        )
        layer_order.append(layer_id)

    graph.outputs["frame"] = NodeOutputRef(node_id=current_canvas)
    graph.metadata["layer_order"] = list(layer_order)
    graph.validate()
    return graph


def build_compositing_graph_from_layer_stack(stack: LayerStack) -> CompositingGraph:
    """Translate the current LayerStack renderer state into a compositing DAG."""
    graph = CompositingGraph(
        metadata={
            "source": "layer_stack",
            "width": int(stack.width),
            "height": int(stack.height),
            "fps": float(stack.fps),
            "total_frames": int(stack.total_frames),
        }
    )
    track_node = graph.add_node(
        CompositingNode(
            id="track_main",
            kind="track",
            params={
                "track_id": "main",
                "width": int(stack.width),
                "height": int(stack.height),
                "fps": float(stack.fps),
                "total_frames": int(stack.total_frames),
            },
            tags=("track", "root"),
            backend_hints={"attachable_backends": ["neutral", "mlt", "gstreamer", "gpu"]},
        )
    )
    current_canvas = track_node.id
    ordered_layers = sorted(
        list(stack.layers),
        key=lambda layer: (int(layer.z_index), str(layer.id)),
    )
    layer_order: list[str] = []

    for layer in ordered_layers:
        current_canvas = _append_stack_layer_branch(
            graph,
            layer,
            current_canvas=current_canvas,
        )
        layer_order.append(str(layer.id))

    graph.outputs["frame"] = NodeOutputRef(node_id=current_canvas)
    graph.metadata["layer_order"] = list(layer_order)
    graph.validate()
    return graph


def compile_compositing_graph(
    graph: CompositingGraph,
    *,
    backend: GraphBackend | None = None,
) -> CompiledCompositingPlan:
    """Compile a compositing DAG into an ordered backend plan."""
    active_backend = backend or NeutralGraphBackend()
    graph.validate()

    steps: list[CompiledNodeStep] = []
    for node in graph.topological_order():
        inputs = {
            edge.target_input: edge.source_ref()
            for edge in graph.incoming_edges(node.id)
        }
        steps.append(
            active_backend.compile_node(
                node,
                inputs=inputs,
                graph=graph,
            )
        )

    return CompiledCompositingPlan(
        backend=active_backend.name,
        steps=steps,
        outputs=dict(graph.outputs),
        metadata=dict(graph.metadata),
    )


def infer_layer_plan_metric_sources(
    plan: Mapping[str, Any],
    *,
    materialized_plan: Mapping[str, Any] | None = None,
) -> dict[str, str]:
    """Describe whether layer-plan canvas metrics were authored, inferred, or defaulted."""
    source_plan = dict(plan)
    materialized = (
        dict(materialized_plan)
        if materialized_plan is not None
        else materialize_layer_plan(source_plan)
    )
    sources: dict[str, str] = {}
    for key in METRIC_KEYS:
        if _has_explicit_metric(source_plan, key):
            sources[key] = "explicit"
        elif _can_infer_metric_from_layers(source_plan, materialized, key):
            sources[key] = "inferred"
        else:
            sources[key] = "default"
    return sources


def _layer_plan_metadata(
    plan: Mapping[str, Any],
    *,
    source_plan: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    authored_plan = source_plan or plan
    metric_sources = infer_layer_plan_metric_sources(
        authored_plan,
        materialized_plan=plan,
    )
    metric_sources.update(_metadata_metric_sources(authored_plan))

    metadata = {
        "source": "layer_plan",
        "width": int(plan.get("width", 1920) or 1920),
        "height": int(plan.get("height", 1080) or 1080),
        "fps": float(plan.get("fps", 30.0) or 30.0),
        "total_frames": max(1, int(plan.get("total_frames", 1) or 1)),
        "metric_sources": metric_sources,
        "explicit_metrics": [
            key for key in METRIC_KEYS if metric_sources.get(key) == "explicit"
        ],
        "inferred_metrics": [
            key for key in METRIC_KEYS if metric_sources.get(key) == "inferred"
        ],
        "default_metrics": [
            key for key in METRIC_KEYS if metric_sources.get(key) == "default"
        ],
    }
    authored_metric_sources = _metadata_metric_sources(
        authored_plan,
        metadata_key="authored_metric_sources",
    )
    if authored_metric_sources:
        metadata["authored_metric_sources"] = authored_metric_sources
    return metadata


def _metadata_metric_sources(
    plan: Mapping[str, Any],
    *,
    metadata_key: str = "metric_sources",
) -> dict[str, str]:
    metadata = plan.get("metadata")
    if not isinstance(metadata, Mapping):
        return {}
    raw_sources = metadata.get(metadata_key)
    if not isinstance(raw_sources, Mapping):
        return {}
    sources: dict[str, str] = {}
    for key in METRIC_KEYS:
        value = str(raw_sources.get(key, "")).strip()
        if value in METRIC_SOURCE_VALUES:
            sources[key] = value
    return sources


def _has_explicit_metric(plan: Mapping[str, Any], key: str) -> bool:
    if key not in plan:
        return False
    try:
        if key == "fps":
            return float(plan.get(key, 0.0) or 0.0) > 0.0
        return int(plan.get(key, 0) or 0) > 0
    except (TypeError, ValueError):
        return False


def _can_infer_metric_from_layers(
    plan: Mapping[str, Any],
    materialized_plan: Mapping[str, Any],
    key: str,
) -> bool:
    layers = [
        layer
        for layer in list(plan.get("layers", []) or [])
        if isinstance(layer, Mapping)
    ]
    if key in {"width", "height"}:
        return any(
            layer.get("type") in {"video", "image"} and bool(layer.get("source"))
            for layer in layers
        )
    if key == "fps":
        return any(
            layer.get("type") == "video" and bool(layer.get("source"))
            for layer in layers
        )
    if key == "total_frames":
        return _can_infer_total_frames(layers, materialized_plan)
    return False


def _can_infer_total_frames(
    layers: list[Mapping[str, Any]],
    materialized_plan: Mapping[str, Any],
) -> bool:
    if any(
        "duration" in layer
        or "end_frame" in layer
        or (layer.get("type") == "video" and bool(layer.get("source")))
        for layer in layers
    ):
        return True
    try:
        return int(materialized_plan.get("total_frames", 1) or 1) != METRIC_DEFAULTS["total_frames"]
    except (TypeError, ValueError):
        return False


def _sorted_layer_specs(
    layers: Any,
) -> list[tuple[int, Mapping[str, Any]]]:
    indexed_layers = [
        (index, layer_spec)
        for index, layer_spec in enumerate(list(layers or []))
        if isinstance(layer_spec, Mapping)
    ]
    return sorted(
        indexed_layers,
        key=lambda item: (
            int(item[1].get("z_index", 0) or 0),
            str(item[1].get("id", "")),
            item[0],
        ),
    )


def _plan_layer_id(
    layer_spec: Mapping[str, Any],
    *,
    index: int,
    used: list[str],
) -> str:
    candidate = str(
        layer_spec.get("id")
        or layer_spec.get("name")
        or f"layer_{index}"
    )
    if candidate not in used:
        return candidate
    suffix = 1
    while f"{candidate}_{suffix}" in used:
        suffix += 1
    return f"{candidate}_{suffix}"


def _append_plan_layer_branch(
    graph: CompositingGraph,
    layer_spec: Mapping[str, Any],
    *,
    layer_id: str,
    layer_index: int,
    current_canvas: str,
) -> str:
    source_node = graph.add_node(
        CompositingNode(
            id=_unique_node_id(graph, f"{layer_id}_source"),
            kind="source",
            layer_id=layer_id,
            params={
                "media_type": str(layer_spec.get("type", "unknown")),
                "name": str(layer_spec.get("name", layer_id)),
                "source": layer_spec.get("source"),
                "text": layer_spec.get("text"),
                "html": layer_spec.get("html"),
                "color": layer_spec.get("color"),
                "size": layer_spec.get("size"),
                "font_config": dict(layer_spec.get("font_config", {}) or {}),
                "blur_radius": layer_spec.get("blur_radius"),
                "gaussian_blur_radius": layer_spec.get("gaussian_blur_radius"),
                "metadata": dict(layer_spec.get("metadata", {}) or {}),
                "start_frame": int(layer_spec.get("start_frame", 0) or 0),
                "end_frame": _optional_int(layer_spec.get("end_frame")),
                "duration": _optional_int(layer_spec.get("duration")),
            },
            tags=("layer", "source"),
            backend_hints={"origin": "layer_plan"},
        )
    )
    graph.metadata.setdefault("layer_nodes", {}).setdefault(layer_id, []).append(source_node.id)
    current_layer = source_node.id

    automation_node_id = _append_automation_node(
        graph,
        layer_id=layer_id,
        keyframes=layer_spec.get("keyframes"),
    )
    if automation_node_id is not None:
        graph.metadata["layer_nodes"][layer_id].append(automation_node_id)

    primitives = list(layer_spec.get("primitives", []) or [])
    if primitives:
        picture_chain_node = graph.add_node(
            CompositingNode(
                id=_unique_node_id(graph, f"{layer_id}_picture_chain"),
                kind="picture_chain",
                layer_id=layer_id,
                params={"ops": primitives},
                tags=("layer", "effects"),
                backend_hints={"origin": "layer_plan"},
            )
        )
        graph.connect(current_layer, picture_chain_node.id, target_input="input")
        current_layer = picture_chain_node.id
        graph.metadata["layer_nodes"][layer_id].append(picture_chain_node.id)

    needs_transform = (
        "position" in layer_spec
        or "scale" in layer_spec
        or "rotation_deg" in layer_spec
        or bool(automation_node_id)
    )
    if needs_transform:
        transform_node = graph.add_node(
            CompositingNode(
                id=_unique_node_id(graph, f"{layer_id}_transform"),
                kind="transform",
                layer_id=layer_id,
                params={
                    "position": tuple(layer_spec.get("position", (0, 0))),
                    "scale": float(layer_spec.get("scale", 1.0) or 1.0),
                    "rotation_deg": float(layer_spec.get("rotation_deg", 0.0) or 0.0),
                },
                tags=("layer", "transform"),
                backend_hints={"origin": "layer_plan"},
            )
        )
        graph.connect(current_layer, transform_node.id, target_input="input")
        if automation_node_id is not None:
            graph.connect(automation_node_id, transform_node.id, target_input="automation")
        current_layer = transform_node.id
        graph.metadata["layer_nodes"][layer_id].append(transform_node.id)

    mask_node_id: str | None = None
    if layer_spec.get("mask_source"):
        mask_node = graph.add_node(
            CompositingNode(
                id=_unique_node_id(graph, f"{layer_id}_mask"),
                kind="source",
                layer_id=layer_id,
                params={
                    "media_type": "mask",
                    "source": layer_spec["mask_source"],
                    "layer_index": layer_index,
                },
                tags=("layer", "mask"),
                backend_hints={"origin": "layer_plan"},
            )
        )
        mask_node_id = mask_node.id
        graph.metadata["layer_nodes"][layer_id].append(mask_node.id)

    composite_node = graph.add_node(
        CompositingNode(
            id=_unique_node_id(graph, f"{layer_id}_composite"),
            kind="composite",
            layer_id=layer_id,
            params={
                "z_index": int(layer_spec.get("z_index", 0) or 0),
                # Read blend_mode/opacity from the top level first, then fall back
                # to a nested effects map (timeline clips store the direct-UI
                # BLEND/PIP ops under effects). Absent => "normal"/1.0 as before.
                "blend_mode": str(_spec_effect(layer_spec, "blend_mode", "normal")),
                "opacity": float(_spec_effect(layer_spec, "opacity", 1.0) or 1.0),
                "start_frame": int(layer_spec.get("start_frame", 0) or 0),
                "end_frame": _optional_int(layer_spec.get("end_frame")),
            },
            tags=("layer", "composite"),
            backend_hints={"origin": "layer_plan"},
        )
    )
    graph.connect(current_canvas, composite_node.id, target_input="background")
    graph.connect(current_layer, composite_node.id, target_input="foreground")
    if mask_node_id is not None:
        graph.connect(mask_node_id, composite_node.id, target_input="mask")
    if automation_node_id is not None:
        graph.connect(automation_node_id, composite_node.id, target_input="automation")
    graph.metadata["layer_nodes"][layer_id].append(composite_node.id)
    return composite_node.id


def _append_stack_layer_branch(
    graph: CompositingGraph,
    layer: Layer,
    *,
    current_canvas: str,
) -> str:
    layer_id = str(layer.id)
    source_node = graph.add_node(
        CompositingNode(
            id=_unique_node_id(graph, f"{layer_id}_source"),
            kind="layer_source",
            layer_id=layer_id,
            params={
                "name": str(layer.name),
                "start_frame": int(layer.start_frame),
                "end_frame": _optional_int(layer.end_frame),
                "z_index": int(layer.z_index),
                "position": tuple(layer.position),
                "content_adapter": "callable",
            },
            tags=("layer", "source"),
            backend_hints={"origin": "layer_stack"},
        )
    )
    graph.metadata.setdefault("layer_nodes", {}).setdefault(layer_id, []).append(source_node.id)
    current_layer = source_node.id

    automation_node_id = _append_automation_node(
        graph,
        layer_id=layer_id,
        keyframes=layer.keyframes,
    )
    if automation_node_id is not None:
        graph.metadata["layer_nodes"][layer_id].append(automation_node_id)

    if (
        tuple(layer.position) != (0, 0)
        or float(layer.scale) != 1.0
        or float(layer.rotation_deg) != 0.0
        or automation_node_id is not None
    ):
        transform_node = graph.add_node(
            CompositingNode(
                id=_unique_node_id(graph, f"{layer_id}_transform"),
                kind="transform",
                layer_id=layer_id,
                params={
                    "position": tuple(layer.position),
                    "scale": float(layer.scale),
                    "rotation_deg": float(layer.rotation_deg),
                },
                tags=("layer", "transform"),
                backend_hints={"origin": "layer_stack"},
            )
        )
        graph.connect(current_layer, transform_node.id, target_input="input")
        if automation_node_id is not None:
            graph.connect(automation_node_id, transform_node.id, target_input="automation")
        current_layer = transform_node.id
        graph.metadata["layer_nodes"][layer_id].append(transform_node.id)

    mask_node_id: str | None = None
    if layer.mask_fn is not None:
        mask_node = graph.add_node(
            CompositingNode(
                id=_unique_node_id(graph, f"{layer_id}_mask"),
                kind="layer_mask",
                layer_id=layer_id,
                params={"mask_adapter": "callable"},
                tags=("layer", "mask"),
                backend_hints={"origin": "layer_stack"},
            )
        )
        mask_node_id = mask_node.id
        graph.metadata["layer_nodes"][layer_id].append(mask_node.id)

    composite_node = graph.add_node(
        CompositingNode(
            id=_unique_node_id(graph, f"{layer_id}_composite"),
            kind="composite",
            layer_id=layer_id,
            params={
                "z_index": int(layer.z_index),
                "blend_mode": str(layer.blend_mode),
                "opacity": float(layer.opacity),
                "start_frame": int(layer.start_frame),
                "end_frame": _optional_int(layer.end_frame),
            },
            tags=("layer", "composite"),
            backend_hints={"origin": "layer_stack"},
        )
    )
    graph.connect(current_canvas, composite_node.id, target_input="background")
    graph.connect(current_layer, composite_node.id, target_input="foreground")
    if mask_node_id is not None:
        graph.connect(mask_node_id, composite_node.id, target_input="mask")
    if automation_node_id is not None:
        graph.connect(automation_node_id, composite_node.id, target_input="automation")
    graph.metadata["layer_nodes"][layer_id].append(composite_node.id)
    return composite_node.id


def _append_automation_node(
    graph: CompositingGraph,
    *,
    layer_id: str,
    keyframes: Mapping[str, Any] | None,
) -> str | None:
    if not keyframes:
        return None
    automation_node = graph.add_node(
        CompositingNode(
            id=_unique_node_id(graph, f"{layer_id}_automation"),
            kind="automation",
            layer_id=layer_id,
            params={"tracks": _serialize_keyframes(keyframes)},
            tags=("layer", "automation"),
            backend_hints={"origin": "layer_graph"},
        )
    )
    return automation_node.id


def _serialize_keyframes(keyframes: Mapping[str, Any]) -> dict[str, list[dict[str, Any]] | Any]:
    serialized: dict[str, list[dict[str, Any]] | Any] = {}
    for name, track in keyframes.items():
        if isinstance(track, KeyframeTrack):
            serialized[name] = track.to_curve_metadata()
        elif isinstance(track, Mapping):
            serialized[name] = _serialize_keyframe_spec(track)
        else:
            serialized[name] = track
    return serialized


def _serialize_keyframe_spec(track_spec: Mapping[str, Any]) -> dict[str, Any]:
    mode = str(track_spec.get("mode", "clamp"))
    relative_to = float(track_spec.get("relative_to", 0.0))
    raw_points = track_spec.get("keyframes", track_spec.get("points", track_spec))
    if isinstance(raw_points, Sequence) and not isinstance(raw_points, (str, bytes)):
        items = [
            (float(item.get("time", item.get("frame", 0.0))), item)
            for item in raw_points
            if isinstance(item, Mapping)
        ]
    elif isinstance(raw_points, Mapping):
        ignored = {"mode", "relative_to", "keyframes", "points"}
        items = [
            (float(frame_key), value_spec)
            for frame_key, value_spec in raw_points.items()
            if frame_key not in ignored
        ]
    else:
        items = []
    if any(_is_vector_keyframe_value(value_spec) for _, value_spec in items):
        return {
            "mode": mode,
            "relative_to": relative_to,
            "points": [
                _serialize_vector_keyframe_point(timestamp, value_spec)
                for timestamp, value_spec in sorted(items, key=lambda item: item[0])
            ],
        }
    track = KeyframeTrack(mode=mode, relative_to=relative_to)
    for timestamp, value_spec in sorted(items, key=lambda item: item[0]):
        if isinstance(value_spec, Mapping):
            value = float(value_spec.get("value", 0.0))
            easing = str(value_spec.get("easing", "linear"))
        else:
            value = float(value_spec)
            easing = "linear"
        track.add_keyframe(timestamp, value, easing=easing)
    return track.to_curve_metadata()


def _is_vector_keyframe_value(value_spec: Any) -> bool:
    value = value_spec.get("value", value_spec) if isinstance(value_spec, Mapping) else value_spec
    if isinstance(value, Mapping):
        return any(key in value for key in ("x", "y", "left", "top"))
    return isinstance(value, Sequence) and not isinstance(value, (str, bytes)) and len(value) >= 2


def _serialize_vector_keyframe_point(timestamp: float, value_spec: Any) -> dict[str, Any]:
    easing = "linear"
    value = value_spec
    if isinstance(value_spec, Mapping):
        value = value_spec.get("value", value_spec)
        easing = str(value_spec.get("easing", "linear"))
    if isinstance(value, Mapping):
        x_value = value.get("x", value.get("left", 0.0))
        y_value = value.get("y", value.get("top", 0.0))
    else:
        x_value, y_value = value[0], value[1]
    return {
        "frame": float(timestamp),
        "value": [float(x_value), float(y_value)],
        "easing": easing,
    }


def _optional_int(value: Any) -> int | None:
    if value is None:
        return None
    return int(value)


def _spec_effect(layer_spec: Mapping[str, Any], key: str, default: Any) -> Any:
    """Read a compositing effect from a layer spec, top-level first.

    Timeline clips carry compositing settings under a nested ``effects`` map
    (e.g. ``effects.blend_mode``, set via the direct-UI BLEND op through
    ``set_clip_effects``), whereas Gemini layer plans put the same keys at the
    top level. Prefer the explicit top-level value so existing plans stay
    byte-identical; otherwise fall back to ``effects[key]``; otherwise the
    caller's default (``"normal"`` blend / ``1.0`` opacity — same as before).
    """
    if key in layer_spec and layer_spec.get(key) is not None:
        return layer_spec.get(key)
    effects = layer_spec.get("effects")
    if isinstance(effects, Mapping) and effects.get(key) is not None:
        return effects.get(key)
    return default


def _unique_node_id(graph: CompositingGraph, base: str) -> str:
    if base not in graph.nodes:
        return base
    suffix = 1
    while f"{base}_{suffix}" in graph.nodes:
        suffix += 1
    return f"{base}_{suffix}"


def _stable_topological_order(graph: CompositingGraph) -> list[str]:
    incoming_count = {node_id: 0 for node_id in graph.nodes}
    outgoing: dict[str, list[str]] = {node_id: [] for node_id in graph.nodes}
    insertion_order = {node_id: index for index, node_id in enumerate(graph.nodes)}

    for edge in graph.edges:
        if edge.source not in graph.nodes or edge.target not in graph.nodes:
            raise ValueError(
                f"Invalid edge '{edge.source}' -> '{edge.target}' in compositing graph."
            )
        incoming_count[edge.target] += 1
        outgoing[edge.source].append(edge.target)

    ready = sorted(
        [node_id for node_id, count in incoming_count.items() if count == 0],
        key=lambda node_id: insertion_order[node_id],
    )
    ordered: list[str] = []

    while ready:
        node_id = ready.pop(0)
        ordered.append(node_id)
        for target in sorted(
            outgoing[node_id],
            key=lambda item: insertion_order[item],
        ):
            incoming_count[target] -= 1
            if incoming_count[target] == 0:
                ready.append(target)
                ready.sort(key=lambda item: insertion_order[item])

    if len(ordered) != len(graph.nodes):
        raise ValueError("Compositing graph contains a cycle.")
    return ordered


__all__ = [
    "CompositingEdge",
    "CompositingGraph",
    "CompositingNode",
    "CompiledCompositingPlan",
    "CompiledNodeStep",
    "GraphBackend",
    "NeutralGraphBackend",
    "NodeOutputRef",
    "build_compositing_graph",
    "build_compositing_graph_from_layer_plan",
    "build_compositing_graph_from_layer_stack",
    "compile_compositing_graph",
    "infer_layer_plan_metric_sources",
]
