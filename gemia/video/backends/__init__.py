"""Backend surfaces for graph-targeted video rendering."""
from gemia.video.backends.base import RenderBackend, RenderProfile, RenderResult
from gemia.video.backends.opencv_flow import (
    FlowTrackingAnalysisResult,
    OpenCVFlowTrackingBackend,
    render_opencv_flow_tracking_backend_manifest,
)
from gemia.video.backends.selection import (
    BackendDecision,
    GRAPH_NATIVE_SOFTWARE_STRATEGY,
    SUPPORTED_RENDER_BACKENDS,
    choose_render_backend,
)
from gemia.video.backends.software import SoftwareGraphBackend, SoftwareRenderBackend

__all__ = [
    "BackendDecision",
    "FlowTrackingAnalysisResult",
    "GRAPH_NATIVE_SOFTWARE_STRATEGY",
    "OpenCVFlowTrackingBackend",
    "RenderBackend",
    "RenderProfile",
    "RenderResult",
    "SUPPORTED_RENDER_BACKENDS",
    "SoftwareGraphBackend",
    "SoftwareRenderBackend",
    "choose_render_backend",
    "render_opencv_flow_tracking_backend_manifest",
]
