"""Real-media review artifacts for generated video outputs."""
from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import cv2
import numpy as np

REAL_STOCK_BACKENDS = {"local_real_video", "pexels", "pixabay", "phone_real_video"}


@dataclass(frozen=True)
class RealMediaReviewResult:
    report_path: str
    status: str
    findings: list[dict[str, str]]
    artifact_paths: list[str]


@dataclass(frozen=True)
class VideoProbe:
    path: str
    exists: bool
    readable: bool
    width: int = 0
    height: int = 0
    fps: float = 0.0
    frame_count: int = 0
    duration_seconds: float = 0.0
    sampled_frames: int = 0
    sample_mean: float | None = None
    sample_stddev: float | None = None
    error: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "path": self.path,
            "exists": self.exists,
            "readable": self.readable,
            "width": self.width,
            "height": self.height,
            "fps": self.fps,
            "frame_count": self.frame_count,
            "duration_seconds": self.duration_seconds,
            "sampled_frames": self.sampled_frames,
            "sample_mean": self.sample_mean,
            "sample_stddev": self.sample_stddev,
            "error": self.error,
        }


def review_real_media_artifact(
    source_path: str | Path,
    output_path: str | Path,
    *,
    report_path: str | Path | None = None,
    preview_manifest_path: str | Path | None = None,
    layer_flow_manifest_path: str | Path | None = None,
    stock_catalog_path: str | Path | None = None,
    min_output_frames: int = 1,
    require_real_source: bool = True,
) -> RealMediaReviewResult:
    """Write a review report for an output rendered from real footage."""
    source = _probe_video(source_path)
    output = _probe_video(output_path)
    preview_manifest = _read_json_file(preview_manifest_path)
    layer_flow_manifest = _read_json_file(layer_flow_manifest_path)
    stock_evidence = _match_stock_catalog(source.path, stock_catalog_path)

    findings: list[dict[str, str]] = []
    _collect_probe_findings(findings, "source", source, min_frames=1)
    _collect_probe_findings(findings, "output", output, min_frames=max(int(min_output_frames), 1))
    _collect_visual_signal_findings(findings, output)
    _collect_manifest_findings(
        findings,
        output_path=output.path,
        preview_manifest=preview_manifest,
        layer_flow_manifest=layer_flow_manifest,
    )
    real_source = _real_source_evidence(source, stock_evidence)
    if require_real_source and not real_source["confirmed"]:
        _add_finding(
            findings,
            "error",
            "real_source_unconfirmed",
            "The review pass could not confirm that the source clip came from real footage.",
        )
    elif real_source["confirmed"]:
        _add_finding(
            findings,
            "info",
            "real_source_confirmed",
            f"Source confirmed through {real_source['method']}.",
        )

    status = _status_from_findings(findings)
    resolved_report = _default_report_path(output.path, report_path)
    payload = {
        "schema_version": 1,
        "review_kind": "real_media_review_pass",
        "reviewed_at": datetime.now(timezone.utc).isoformat(),
        "status": status,
        "source": source.to_dict(),
        "output": output.to_dict(),
        "real_source": real_source,
        "stock_catalog_evidence": stock_evidence,
        "artifacts": {
            "preview_manifest_path": _resolve_optional(preview_manifest_path),
            "layer_flow_manifest_path": _resolve_optional(layer_flow_manifest_path),
            "stock_catalog_path": _resolve_optional(stock_catalog_path),
        },
        "render_context": _render_context(preview_manifest, layer_flow_manifest),
        "quality_findings": findings,
    }
    resolved_report.parent.mkdir(parents=True, exist_ok=True)
    resolved_report.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    artifact_paths = [str(resolved_report)]
    for path in (preview_manifest_path, layer_flow_manifest_path):
        resolved = _resolve_optional(path)
        if resolved:
            artifact_paths.append(resolved)
    return RealMediaReviewResult(
        report_path=str(resolved_report),
        status=status,
        findings=findings,
        artifact_paths=artifact_paths,
    )


def _probe_video(path: str | Path) -> VideoProbe:
    resolved = Path(path).expanduser().resolve()
    if not resolved.exists():
        return VideoProbe(path=str(resolved), exists=False, readable=False, error="missing")

    cap = cv2.VideoCapture(str(resolved))
    try:
        if not cap.isOpened():
            return VideoProbe(path=str(resolved), exists=True, readable=False, error="not_readable")

        width = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH) or 0)
        height = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT) or 0)
        fps = float(cap.get(cv2.CAP_PROP_FPS) or 0.0)
        frame_count = int(cap.get(cv2.CAP_PROP_FRAME_COUNT) or 0)
        means: list[float] = []
        stddevs: list[float] = []
        for frame_index in _sample_indexes(frame_count):
            cap.set(cv2.CAP_PROP_POS_FRAMES, max(int(frame_index), 0))
            ok, frame = cap.read()
            if not ok or frame is None:
                continue
            arr = frame.astype(np.float32) / 255.0
            means.append(float(np.mean(arr)))
            stddevs.append(float(np.std(arr)))

        duration = float(frame_count) / fps if fps > 0.0 and frame_count > 0 else 0.0
        return VideoProbe(
            path=str(resolved),
            exists=True,
            readable=True,
            width=width,
            height=height,
            fps=fps,
            frame_count=frame_count,
            duration_seconds=duration,
            sampled_frames=len(means),
            sample_mean=float(np.mean(means)) if means else None,
            sample_stddev=float(np.mean(stddevs)) if stddevs else None,
        )
    finally:
        cap.release()


def _sample_indexes(frame_count: int, max_samples: int = 5) -> list[int]:
    if frame_count <= 0:
        return [0]
    if frame_count <= max_samples:
        return list(range(frame_count))
    candidates = {0, frame_count - 1}
    for ratio in (0.25, 0.5, 0.75):
        candidates.add(min(frame_count - 1, max(0, int(round((frame_count - 1) * ratio)))))
    return sorted(candidates)[:max_samples]


def _collect_probe_findings(
    findings: list[dict[str, str]],
    label: str,
    probe: VideoProbe,
    *,
    min_frames: int,
) -> None:
    if not probe.exists:
        _add_finding(findings, "error", f"{label}_missing", f"{label} video is missing.")
        return
    if not probe.readable:
        _add_finding(findings, "error", f"{label}_unreadable", f"{label} video is not readable.")
        return
    if probe.width <= 0 or probe.height <= 0:
        _add_finding(findings, "error", f"{label}_invalid_dimensions", f"{label} video has invalid dimensions.")
    if probe.frame_count < min_frames:
        _add_finding(findings, "error", f"{label}_too_few_frames", f"{label} video has fewer than {min_frames} frames.")
    if probe.sampled_frames == 0:
        _add_finding(findings, "error", f"{label}_no_samples", f"{label} video yielded no sample frames.")
    _add_finding(
        findings,
        "info",
        f"{label}_readable",
        f"{label} video is readable at {probe.width}x{probe.height} with {probe.frame_count} frames.",
    )


def _collect_visual_signal_findings(findings: list[dict[str, str]], output: VideoProbe) -> None:
    if not output.readable or output.sample_stddev is None:
        return
    if output.sample_stddev < 0.002:
        _add_finding(
            findings,
            "error",
            "output_blank_or_flat",
            "Output sample frames have near-zero visual variance.",
        )
    elif output.sample_stddev < 0.02:
        _add_finding(
            findings,
            "warning",
            "output_low_visual_variance",
            "Output sample frames have low visual variance; inspect the render manually.",
        )
    else:
        _add_finding(findings, "info", "output_visual_signal", "Output sample frames contain visual detail.")


def _collect_manifest_findings(
    findings: list[dict[str, str]],
    *,
    output_path: str,
    preview_manifest: dict[str, Any],
    layer_flow_manifest: dict[str, Any],
) -> None:
    cinefocus_metadata = _read_json_file(Path(output_path).expanduser().resolve().with_suffix(".cinefocus.json"))
    motion_deblur_metadata = _read_json_file(Path(output_path).expanduser().resolve().with_suffix(".motion_deblur.json"))
    html_graphics_metadata = _read_json_file(Path(output_path).expanduser().resolve().with_suffix(".html_graphics.json"))
    slate_id_metadata = _read_json_file(Path(output_path).expanduser().resolve().with_suffix(".slate_id.json"))
    blemish_metadata = _read_json_file(Path(output_path).expanduser().resolve().with_suffix(".blemish.json"))
    face_age_metadata = _read_json_file(Path(output_path).expanduser().resolve().with_suffix(".face_age.json"))
    face_reshaper_metadata = _read_json_file(Path(output_path).expanduser().resolve().with_suffix(".face_reshaper.json"))
    ultrasharpen_metadata = _read_json_file(Path(output_path).expanduser().resolve().with_suffix(".ultrasharpen.json"))
    if preview_manifest:
        manifest_output = str(preview_manifest.get("output_path", ""))
        if manifest_output and Path(manifest_output).expanduser().resolve() != Path(output_path):
            _add_finding(findings, "warning", "preview_manifest_output_mismatch", "Preview manifest points to a different output.")
        backend = preview_manifest.get("render_backend")
        if isinstance(backend, dict) and backend.get("selected"):
            _add_finding(findings, "info", "render_backend_recorded", f"Render backend recorded as {backend['selected']}.")
        else:
            _add_finding(findings, "warning", "render_backend_missing", "Preview manifest does not record a selected backend.")
    elif cinefocus_metadata.get("effect") == "resolve21_ai_cinefocus":
        _add_finding(findings, "info", "cinefocus_metadata_recorded", "CineFocus metadata sidecar is attached.")
    elif motion_deblur_metadata.get("effect") == "resolve21_ai_motion_deblur":
        _add_finding(findings, "info", "motion_deblur_metadata_recorded", "Motion Deblur metadata sidecar is attached.")
    elif html_graphics_metadata.get("effect") == "resolve21_html_graphics_lottie_support":
        _add_finding(findings, "info", "html_graphics_metadata_recorded", "HTML graphics metadata sidecar is attached.")
    elif slate_id_metadata.get("effect") == "resolve21_ai_slate_id_metadata":
        _add_finding(findings, "info", "slate_id_metadata_recorded", "Slate ID metadata sidecar is attached.")
    elif blemish_metadata.get("effect") == "resolve21_ai_blemish_removal":
        _add_finding(findings, "info", "blemish_metadata_recorded", "Blemish Removal metadata sidecar is attached.")
    elif face_age_metadata.get("effect") == "resolve21_ai_face_age_transformer":
        _add_finding(findings, "info", "face_age_metadata_recorded", "Face Age metadata sidecar is attached.")
    elif face_reshaper_metadata.get("effect") == "resolve21_ai_face_reshaper":
        _add_finding(findings, "info", "face_reshaper_metadata_recorded", "Face Reshaper metadata sidecar is attached.")
    elif ultrasharpen_metadata.get("effect") == "resolve21_ai_ultrasharpen":
        _add_finding(findings, "info", "ultrasharpen_metadata_recorded", "UltraSharpen metadata sidecar is attached.")
    else:
        _add_finding(findings, "warning", "preview_manifest_missing", "No preview manifest was attached to the review.")

    if layer_flow_manifest:
        layer_count = layer_flow_manifest.get("layer_count")
        _add_finding(findings, "info", "layer_flow_recorded", f"Layer-flow manifest records {layer_count} layers.")
    elif cinefocus_metadata.get("effect") == "resolve21_ai_cinefocus":
        rendered = cinefocus_metadata.get("rendered_frames")
        _add_finding(findings, "info", "cinefocus_focus_plan_recorded", f"CineFocus metadata records {rendered} rendered frames.")
    elif motion_deblur_metadata.get("effect") == "resolve21_ai_motion_deblur":
        sharpness = motion_deblur_metadata.get("sharpness") or {}
        delta = sharpness.get("delta")
        _add_finding(findings, "info", "motion_deblur_sharpness_recorded", f"Motion Deblur sharpness delta is {delta}.")
    elif html_graphics_metadata.get("effect") == "resolve21_html_graphics_lottie_support":
        overlay_count = html_graphics_metadata.get("overlay_count")
        overlay_types = ", ".join(str(item) for item in html_graphics_metadata.get("overlay_types", []))
        _add_finding(
            findings,
            "info",
            "html_graphics_alpha_recorded",
            f"HTML graphics metadata records {overlay_count} alpha overlays: {overlay_types}.",
        )
    elif slate_id_metadata.get("effect") == "resolve21_ai_slate_id_metadata":
        detection = slate_id_metadata.get("slate_detection", {}) if isinstance(slate_id_metadata.get("slate_detection"), dict) else {}
        if detection.get("frames_with_slate", 0):
            _add_finding(findings, "info", "slate_id_detected_recorded", "Slate ID metadata records detected slate frames.")
        else:
            _add_finding(findings, "info", "slate_id_no_slate_diagnostic_recorded", "Slate ID metadata records a no-slate diagnostic.")
    elif blemish_metadata.get("effect") == "resolve21_ai_blemish_removal":
        cleanup = blemish_metadata.get("cleanup", {}) if isinstance(blemish_metadata.get("cleanup"), dict) else {}
        _add_finding(
            findings,
            "info",
            "blemish_cleanup_recorded",
            f"Blemish metadata records average cleanup delta {cleanup.get('average_delta')}.",
        )
    elif face_age_metadata.get("effect") == "resolve21_ai_face_age_transformer":
        face_detection = face_age_metadata.get("face_detection", {}) if isinstance(face_age_metadata.get("face_detection"), dict) else {}
        if face_detection.get("no_face_evidence"):
            _add_finding(findings, "info", "face_age_no_face_evidence_recorded", "Face Age metadata records a no-face diagnostic.")
        else:
            _add_finding(
                findings,
                "info",
                "face_age_tracks_recorded",
                f"Face Age metadata records age offset {face_age_metadata.get('age_offset_years')}.",
            )
    elif face_reshaper_metadata.get("effect") == "resolve21_ai_face_reshaper":
        face_detection = face_reshaper_metadata.get("face_detection", {}) if isinstance(face_reshaper_metadata.get("face_detection"), dict) else {}
        tracking = face_reshaper_metadata.get("tracking", {}) if isinstance(face_reshaper_metadata.get("tracking"), dict) else {}
        if face_detection.get("no_face_evidence"):
            _add_finding(findings, "info", "face_reshaper_no_face_evidence_recorded", "Face Reshaper metadata records a no-face diagnostic.")
        else:
            _add_finding(
                findings,
                "info",
                "face_reshaper_warp_recorded",
                f"Face Reshaper metadata records {tracking.get('tracked_frames', 0)} tracked frames.",
            )
    elif ultrasharpen_metadata.get("effect") == "resolve21_ai_ultrasharpen":
        sharpness = ultrasharpen_metadata.get("sharpness", {}) if isinstance(ultrasharpen_metadata.get("sharpness"), dict) else {}
        _add_finding(
            findings,
            "info",
            "ultrasharpen_sharpness_recorded",
            f"UltraSharpen metadata records sharpness delta {sharpness.get('delta')}.",
        )
    else:
        _add_finding(findings, "warning", "layer_flow_manifest_missing", "No layer-flow manifest was attached to the review.")


def _real_source_evidence(source: VideoProbe, stock_evidence: dict[str, Any]) -> dict[str, Any]:
    backend = str(stock_evidence.get("backend", ""))
    confirmed = source.readable and backend in REAL_STOCK_BACKENDS
    return {
        "confirmed": confirmed,
        "method": "stock_catalog" if stock_evidence else "",
        "backend": backend,
        "catalog_id": stock_evidence.get("id"),
        "source_origin": stock_evidence.get("source"),
    }


def _match_stock_catalog(source_path: str, stock_catalog_path: str | Path | None) -> dict[str, Any]:
    catalog = _read_json_list(stock_catalog_path)
    if not catalog:
        return {}
    source_resolved = Path(source_path).expanduser().resolve()
    for item in catalog:
        if not isinstance(item, dict):
            continue
        outputs = [Path(str(path)).expanduser().resolve() for path in item.get("outputs", [])]
        source = item.get("source")
        sources = [Path(str(source)).expanduser().resolve()] if source else []
        if source_resolved in outputs or source_resolved in sources:
            return {
                "id": item.get("id"),
                "kind": item.get("kind"),
                "status": item.get("status"),
                "backend": item.get("backend"),
                "source": item.get("source"),
                "outputs": item.get("outputs", []),
            }
    return {}


def _read_json_file(path: str | Path | None) -> dict[str, Any]:
    if path is None:
        return {}
    try:
        payload = json.loads(Path(path).expanduser().read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return payload if isinstance(payload, dict) else {}


def _read_json_list(path: str | Path | None) -> list[Any]:
    if path is None:
        return []
    try:
        payload = json.loads(Path(path).expanduser().read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return []
    return payload if isinstance(payload, list) else []


def _render_context(preview_manifest: dict[str, Any], layer_flow_manifest: dict[str, Any]) -> dict[str, Any]:
    compiled_graph = preview_manifest.get("compiled_graph", {}) if preview_manifest else {}
    metadata = compiled_graph.get("metadata", {}) if isinstance(compiled_graph, dict) else {}
    return {
        "render_backend": preview_manifest.get("render_backend", {}) if preview_manifest else {},
        "execution_backend": preview_manifest.get("execution_graph", {}).get("backend") if preview_manifest else None,
        "compiled_metadata": metadata,
        "authoring_mode": layer_flow_manifest.get("authoring_mode") if layer_flow_manifest else None,
        "layer_count": layer_flow_manifest.get("layer_count") if layer_flow_manifest else None,
    }


def _add_finding(findings: list[dict[str, str]], severity: str, code: str, message: str) -> None:
    findings.append({"severity": severity, "code": code, "message": message})


def _status_from_findings(findings: list[dict[str, str]]) -> str:
    if any(finding["severity"] == "error" for finding in findings):
        return "failed"
    if any(finding["severity"] == "warning" for finding in findings):
        return "passed_with_warnings"
    return "passed"


def _default_report_path(output_path: str, report_path: str | Path | None) -> Path:
    if report_path is not None:
        return Path(report_path).expanduser().resolve()
    return Path(output_path).expanduser().resolve().with_suffix(".real-media-review.json")


def _resolve_optional(path: str | Path | None) -> str | None:
    if path is None:
        return None
    return str(Path(path).expanduser().resolve())


__all__ = ["RealMediaReviewResult", "review_real_media_artifact"]
