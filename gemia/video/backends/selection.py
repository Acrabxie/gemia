"""Backend selection for graph-native render execution."""
from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from gemia.video.backends.base import RenderBackend, RenderSource
from gemia.video.backends.software import SoftwareRenderBackend
from gemia.video.compositing_graph import CompiledCompositingPlan, CompositingGraph
from gemia.video.layers import LayerStack

GRAPH_NATIVE_SOFTWARE_STRATEGY = "graph_native_software_orchestrator"
SUPPORTED_RENDER_BACKENDS = ("software",)


@dataclass(frozen=True)
class BackendDecision:
    """Resolved render backend plus the reason it was chosen."""

    requested: str
    selected: str
    strategy: str
    source_kind: str
    reason: str

    def to_dict(self) -> dict[str, str]:
        return {
            "requested": self.requested,
            "selected": self.selected,
            "strategy": self.strategy,
            "source_kind": self.source_kind,
            "reason": self.reason,
        }


def choose_render_backend(
    source: RenderSource,
    *,
    requested: str | None = None,
) -> tuple[RenderBackend, BackendDecision]:
    """Choose the concrete backend for the next graph-native execution slice."""
    requested_name = (requested or "auto").strip().lower() or "auto"
    source_kind = _source_kind(source)

    if requested_name in {"auto", "graph", "graph-native", "software"}:
        selected = "software"
        reason = (
            "software backend is the only wired production target; "
            "graph-native orchestration keeps layer/DAG execution on the backend seam "
            "until a PyAV/OpenTimelineIO target is ready"
        )
        if requested_name == "software":
            reason = "software backend was explicitly requested"
        return (
            SoftwareRenderBackend(),
            BackendDecision(
                requested=requested_name,
                selected=selected,
                strategy=GRAPH_NATIVE_SOFTWARE_STRATEGY,
                source_kind=source_kind,
                reason=reason,
            ),
        )

    supported = ", ".join(("auto", *SUPPORTED_RENDER_BACKENDS))
    raise ValueError(
        f"Unsupported render backend {requested_name!r}. Supported values: {supported}."
    )


def _source_kind(source: RenderSource) -> str:
    if isinstance(source, LayerStack):
        return "layer_stack"
    if isinstance(source, CompositingGraph):
        return "compositing_graph"
    if isinstance(source, CompiledCompositingPlan):
        return "compiled_compositing_plan"
    if isinstance(source, Mapping):
        return "layer_plan"
    return type(source).__name__


__all__ = [
    "BackendDecision",
    "GRAPH_NATIVE_SOFTWARE_STRATEGY",
    "SUPPORTED_RENDER_BACKENDS",
    "choose_render_backend",
]
