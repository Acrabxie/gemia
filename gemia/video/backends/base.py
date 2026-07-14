"""Render backend contracts for graph-targeted video execution."""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any, Mapping, Protocol, TypeAlias

if TYPE_CHECKING:
    from gemia.video.compositing_graph import (
        CompiledCompositingPlan,
        CompositingGraph,
        GraphBackend,
    )
    from gemia.video.layers import LayerStack

RGBColor: TypeAlias = tuple[float, float, float]
LayerPlan: TypeAlias = Mapping[str, Any]
RenderSource: TypeAlias = "LayerStack | LayerPlan | CompositingGraph | CompiledCompositingPlan"


@dataclass(frozen=True)
class RenderProfile:
    """Output-specific render options."""

    name: str
    codec: str = "mp4v"
    background_color: RGBColor = (0.0, 0.0, 0.0)
    start_frame: int = 0
    end_frame: int | None = None
    step: int = 1

    def __post_init__(self) -> None:
        if int(self.step) <= 0:
            raise ValueError("RenderProfile.step must be >= 1.")

    @classmethod
    def preview(
        cls,
        *,
        codec: str = "mp4v",
        background_color: RGBColor = (0.0, 0.0, 0.0),
        start_frame: int = 0,
        end_frame: int | None = None,
        step: int = 2,
    ) -> "RenderProfile":
        return cls(
            name="preview",
            codec=codec,
            background_color=background_color,
            start_frame=start_frame,
            end_frame=end_frame,
            step=step,
        )

    @classmethod
    def final(
        cls,
        *,
        codec: str = "mp4v",
        background_color: RGBColor = (0.0, 0.0, 0.0),
        start_frame: int = 0,
        end_frame: int | None = None,
        step: int = 1,
    ) -> "RenderProfile":
        return cls(
            name="final",
            codec=codec,
            background_color=background_color,
            start_frame=start_frame,
            end_frame=end_frame,
            step=step,
        )


@dataclass(frozen=True)
class RenderResult:
    """Completed render artifact plus the execution metadata used to make it."""

    backend: str
    output_path: str
    source_kind: str
    profile: RenderProfile
    width: int
    height: int
    fps: float
    total_frames: int
    compiled_plan: "CompiledCompositingPlan | None" = None

    @property
    def output(self) -> Path:
        return Path(self.output_path)


class RenderBackend(Protocol):
    """Common surface for backend-targeted preview and final renders."""

    name: str
    graph_backend: "GraphBackend"

    def render(
        self,
        source: RenderSource,
        output_path: str | Path,
        *,
        profile: RenderProfile | None = None,
    ) -> RenderResult:
        ...

    def render_preview(
        self,
        source: RenderSource,
        output_path: str | Path,
        *,
        profile: RenderProfile | None = None,
    ) -> RenderResult:
        ...

    def render_final(
        self,
        source: RenderSource,
        output_path: str | Path,
        *,
        profile: RenderProfile | None = None,
    ) -> RenderResult:
        ...

