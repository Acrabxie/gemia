from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def test_timeline_has_vertical_scrub_space_and_blank_area_scrubbing() -> None:
    source = (ROOT / "static/v3/v3.js").read_text(encoding="utf-8")
    css = (ROOT / "static/v3/v3.css").read_text(encoding="utf-8")

    assert "TL_LANE_PAD = 10" in source
    assert "timelineLanePad()" in source
    assert 'for (const surface of [ruler, scroll])' in source
    assert 'e.target.closest(".ptl-clip")' in source
    assert ".ptl-content { position: relative; min-width: 100%; cursor: ew-resize;" in css


def test_playhead_is_clamped_to_last_material_and_edge_drag_follows_viewport() -> None:
    source = (ROOT / "static/v3/v3.js").read_text(encoding="utf-8")

    assert "mediaEnd: lastEnd" in source
    assert "Math.min(mediaEnd, Number(t) || 0)" in source
    assert "scrub.velocity = scrub.velocity * 0.72 + targetVelocity * 0.28" in source
    assert "scroll.scrollLeft += scrub.velocity" in source
    assert "requestAnimationFrame(scrubStep)" in source


def test_timeline_playhead_and_preview_video_are_bidirectionally_synced() -> None:
    source = (ROOT / "static/v3/v3.js").read_text(encoding="utf-8")

    assert "function syncTimelinePreviewToPlayhead()" in source
    assert 'video.addEventListener("loadedmetadata", syncTimelinePreviewToPlayhead)' in source
    assert 'video.addEventListener("timeupdate", syncPlayheadFromPreview)' in source
    assert 'video.addEventListener("seeked", syncPlayheadFromPreview)' in source
    assert 'setPlayhead(video.currentTime, { syncPreview: false })' in source
    assert "if (syncPreview) syncTimelinePreviewToPlayhead();" in source


def test_timeline_zoom_reuses_the_pointer_anchor() -> None:
    source = (ROOT / "static/v3/v3.js").read_text(encoding="utf-8")

    assert "TL.zoomAnchor = { time: pxToTime(scroll.scrollLeft + px), px }" in source
    assert "const anchor = TL.zoomAnchor" in source
    assert "timeToX(anchorTime) - anchorPx" in source


def test_clips_can_move_between_compatible_timeline_tracks() -> None:
    source = (ROOT / "static/v3/v3.js").read_text(encoding="utf-8")

    assert 'if (mediaKind === "audio") return trackKind === "audio"' in source
    assert 'if (mediaKind === "video") return trackKind === "video"' in source
    assert 'if (mediaKind === "image") return trackKind === "video" || trackKind === "overlay"' in source
    assert 'if (mediaKind === "text" || mediaKind === "lottie") return trackKind === "overlay"' in source
    assert "d.pendTrack = tid" in source
    assert "if (d.el.parentNode !== lane) lane.appendChild(d.el)" in source
    assert "if (d.pendTrack) op.track_id = d.pendTrack" in source
