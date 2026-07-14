"""Blended portrait slate delivery scene composition for Gemia."""
from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from gemia.video.slate_id import render_slate_id_metadata_plan
from gemia.video.ultrasharpen import render_ultrasharpen_plan
from gemia.video.face_age import render_face_age_plan
from gemia.video.face_reshaper import render_face_reshaper_plan
from gemia.video.blemish import render_blemish_removal_plan
from gemia.video.html_graphics import render_html_graphics_plan
from gemia.video.review import review_real_media_artifact

logger = logging.getLogger(__name__)

@dataclass(frozen=True)
class BlendedPortraitSlateDeliveryResult:
    output_path: str
    metadata_path: str
    report_path: str
    components: dict[str, str]

def render_blended_portrait_slate_delivery_scene(
    input_path: str,
    output_path: str,
    *,
    html: str | None = None,
    age_offset: int = 12,
    sharpen_strength: float = 0.72,
    blemish_strength: float = 0.68,
    max_long_edge: int | None = 540,
    temp_dir: str | Path | None = None,
    stock_catalog_path: str | Path | None = None,
) -> str:
    """
    Compose a full delivery scene: slate detection -> UltraSharpen -> 
    face age/reshape/blemish -> HTML graphics -> RealMedia review.
    """
    source = Path(input_path).expanduser().resolve()
    final_output = Path(output_path).expanduser().resolve()
    stock_catalog = _resolve_stock_catalog(stock_catalog_path)
    
    if temp_dir:
        working_dir = Path(temp_dir).expanduser().resolve()
    else:
        working_dir = final_output.parent / f"tmp_{final_output.stem}"
    
    working_dir.mkdir(parents=True, exist_ok=True)
    
    components = {}
    
    # 1. Slate ID Detection
    logger.info("Step 1: Slate ID Detection")
    slate_video = working_dir / f"{final_output.stem}_slate.mp4"
    render_slate_id_metadata_plan(str(source), str(slate_video), max_long_edge=max_long_edge)
    slate_meta_path = slate_video.with_suffix(".slate_id.json")
    slate_metadata = {}
    if slate_meta_path.exists():
        slate_metadata = json.loads(slate_meta_path.read_text())
    components["slate_id"] = str(slate_video)
    
    # 2. UltraSharpen
    logger.info("Step 2: UltraSharpen")
    sharp_video = working_dir / f"{final_output.stem}_sharp.mp4"
    render_ultrasharpen_plan(str(source), str(sharp_video), strength=sharpen_strength, max_long_edge=max_long_edge)
    components["ultrasharpen"] = str(sharp_video)
    
    # 3. Face Effects Chain (Age -> Reshape -> Blemish)
    # We apply them sequentially as per "composing existing primitives"
    logger.info("Step 3: Face Effects Chain")
    
    age_video = working_dir / f"{final_output.stem}_age.mp4"
    render_face_age_plan(str(sharp_video), str(age_video), age_offset=age_offset, max_long_edge=max_long_edge)
    components["face_age"] = str(age_video)
    
    reshape_video = working_dir / f"{final_output.stem}_reshape.mp4"
    render_face_reshaper_plan(str(age_video), str(reshape_video), max_long_edge=max_long_edge)
    components["face_reshaper"] = str(reshape_video)
    
    blemish_video = working_dir / f"{final_output.stem}_blemish.mp4"
    render_blemish_removal_plan(str(reshape_video), str(blemish_video), strength=blemish_strength, max_long_edge=max_long_edge)
    components["blemish"] = str(blemish_video)
    
    # 4. HTML Graphics
    logger.info("Step 4: HTML Graphics")
    if not html:
        title = slate_metadata.get("clip_metadata", {}).get("title") or source.stem
        html = f"<div style='position:absolute; bottom:10%; left:10%; color:white; font-size:40px; text-shadow:2px 2px 4px black;'>Delivery: {title}</div>"
    
    render_html_graphics_plan(str(blemish_video), str(final_output), html=html, max_long_edge=max_long_edge)
    
    # 5. Metadata Sidecar
    logger.info("Step 5: Writing Metadata Sidecar")
    final_meta_path = final_output.with_suffix(".blended_scene.json")
    
    # Check for faces in blemish metadata (as it's the last in face chain)
    blemish_meta_path = blemish_video.with_suffix(".blemish.json")
    has_faces = False
    face_detection: dict[str, Any] = {}
    if blemish_meta_path.exists():
        blemish_meta = json.loads(blemish_meta_path.read_text())
        face_detection = blemish_meta.get("face_detection", {})
        has_faces = int(face_detection.get("frames_with_faces", 0) or 0) > 0

    meta_content = {
        "schema_version": 1,
        "effect": "resolve21_blended_portrait_slate_delivery_scene",
        "rendered_at": datetime.now(timezone.utc).isoformat(),
        "source_path": str(source),
        "output_path": str(final_output),
        "has_faces": has_faces,
        "slate_detected": slate_metadata.get("preview_kind") == "slate_metadata_detected",
        "source_kind": "local_real_video" if stock_catalog else "unknown",
        "components": components,
        "diagnostics": {
            "face_chain": "age -> reshape -> blemish",
            "sharpen_strength": sharpen_strength,
            "has_faces": has_faces,
            "no_face_evidence": not has_faces,
            "face_detection": face_detection,
            "slate_preview_kind": slate_metadata.get("preview_kind"),
        },
        "continuity_review_hints": [
            "confirm slate/title-card continuity",
            "confirm portrait pass diagnostics",
            "confirm delivery graphic legibility",
        ],
    }
    final_meta_path.write_text(json.dumps(meta_content, indent=2))
    
    # 6. Real Media Review
    logger.info("Step 6: Real Media Review")
    report_path = final_output.with_suffix(".review_report.json")
    review_real_media_artifact(
        source_path=str(source),
        output_path=str(final_output),
        report_path=str(report_path),
        stock_catalog_path=stock_catalog,
    )
    
    return str(final_output)


def _resolve_stock_catalog(stock_catalog_path: str | Path | None) -> Path | None:
    if stock_catalog_path is not None:
        resolved = Path(stock_catalog_path).expanduser().resolve()
        return resolved if resolved.exists() else None
    default = Path.home() / ".gemia" / "automation" / "stock_catalog.json"
    return default if default.exists() else None
