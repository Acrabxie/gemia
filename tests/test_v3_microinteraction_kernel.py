"""Regression contract for the Figma-like video microinteraction kernel.

These tests intentionally span the HTML, CSS, browser controller, and compact
timeline payload.  A spatial or temporal edit is one interaction regardless of
which surface starts it: canvas, timeline, contextual inspector, or Properties.
"""
from __future__ import annotations

import re
from html.parser import HTMLParser
from pathlib import Path

from gemia import v3_routes
from lumerai.patches import _validated_effect_value


ROOT = Path(__file__).resolve().parents[1]
HTML = (ROOT / "static/v3/index.html").read_text(encoding="utf-8")
CSS = (ROOT / "static/v3/v3.css").read_text(encoding="utf-8")
JS = (ROOT / "static/v3/v3.js").read_text(encoding="utf-8")
ROUTES = (ROOT / "gemia/v3_routes.py").read_text(encoding="utf-8")
LUMEN_CANVAS = (ROOT / "gemia/lumenframe_canvas.py").read_text(encoding="utf-8")


class _SemanticHtml(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.by_id: dict[str, tuple[str, dict[str, str | None]]] = {}
        self.canvas_handles: list[dict[str, str | None]] = []
        self.edit_fields: list[dict[str, str | None]] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        attributes = dict(attrs)
        if identifier := attributes.get("id"):
            self.by_id[identifier] = (tag, attributes)
        if attributes.get("data-canvas-handle"):
            self.canvas_handles.append(attributes)
        if attributes.get("data-edit-field"):
            self.edit_fields.append(attributes)


def _html() -> _SemanticHtml:
    parsed = _SemanticHtml()
    parsed.feed(HTML)
    return parsed


def _function_body(name: str, *, until: str) -> str:
    start = JS.index(f"function {name}")
    return JS[start:JS.index(until, start)]


def test_html_exposes_one_selectable_canvas_with_spatial_handles() -> None:
    parsed = _html()

    assert parsed.by_id["direct-canvas-shell"][1]["aria-label"] == "素材画布"
    canvas = parsed.by_id["direct-canvas-space"]
    assert canvas[1]["role"] == "application"
    assert canvas[1]["tabindex"] == "0"
    assert "方向键" in str(canvas[1]["aria-label"])
    assert parsed.by_id["direct-canvas-video"][0] == "video"
    assert parsed.by_id["direct-canvas-image"][0] == "img"
    assert "direct-selection-box" in parsed.by_id

    scale_handles = [item for item in parsed.canvas_handles if item["data-canvas-handle"] == "scale"]
    rotate_handles = [item for item in parsed.canvas_handles if item["data-canvas-handle"] == "rotate"]
    assert len(scale_handles) == 4
    assert len(rotate_handles) == 1
    assert all(item.get("aria-label") for item in parsed.canvas_handles)


def test_contextual_and_docked_inspectors_expose_the_same_edit_dimensions() -> None:
    parsed = _html()
    canvas_fields = {str(item["data-edit-field"]) for item in parsed.edit_fields}

    assert parsed.by_id["canvas-micro-inspector"][1]["aria-label"] == "空间微调"
    assert canvas_fields == {"x", "y", "scale", "rotation"}
    assert 'id="timeline-micro-inspector"' in JS
    assert 'aria-label="时间微调"' in JS
    for field in ("start", "duration", "source_in", "source_out"):
        assert f'data-edit-field="{field}"' in JS
    assert 'data-open-properties' in HTML
    assert 'data-open-properties' in JS


def test_css_preserves_hit_targets_focus_reduced_motion_and_narrow_containers() -> None:
    handle_rule = re.search(r"\.direct-handle\s*\{(?P<body>.*?)\}", CSS, re.S)
    assert handle_rule is not None
    assert re.search(r"width:\s*24px", handle_rule["body"])
    assert re.search(r"height:\s*24px", handle_rule["body"])

    assert ".direct-canvas-space:focus-visible" in CSS
    assert "button:focus-visible" in CSS
    assert "input:focus-visible" in CSS
    assert "@media (prefers-reduced-motion: reduce)" in CSS
    assert "animation-duration: 0.01ms !important" in CSS
    narrow_canvas = re.search(
        r"@container\s*\(max-width:\s*\d+px\)\s*\{[^}]*"
        r"\.canvas-micro-inspector label:nth-of-type\(n \+ 3\)",
        CSS,
        re.S,
    )
    assert narrow_canvas is not None
    assert re.search(
        r"@container\s*\(max-width:\s*\d+px\)\s*\{[^}]*\.properties-group",
        CSS,
        re.S,
    )
    assert ".ptl-micro-inspector" in CSS and "overflow-x: auto" in CSS


def test_selected_clip_is_the_single_authority_for_canvas_timeline_and_properties() -> None:
    select_clip = _function_body("selectClip", until="function focusEntity")

    assert "state.selectedClipId = clipId" in select_clip
    assert 'el.dataset.clipId === clipId' in select_clip
    assert "renderDirectCanvas()" in select_clip
    assert "renderTimelineMicroInspector()" in select_clip
    assert 'refreshPanel("properties")' in select_clip
    assert 'if (c.id === state.selectedClipId) return c' in JS


def test_canvas_gesture_supports_move_uniform_scale_rotate_and_option_snap_bypass() -> None:
    begin = _function_body("beginCanvasGesture", until="function moveCanvasGesture")
    move = _function_body("moveCanvasGesture", until="function finishCanvasGesture")

    assert 'handle?.dataset.canvasHandle || "move"' in begin
    assert 'drag.mode === "move"' in move
    assert 'drag.mode === "scale"' in move
    assert 'drag.mode === "rotate"' in move
    assert "geometry.scale * Math.hypot" in move
    assert "rotation = geometry.rotation + angle - drag.startAngle" in move
    assert "Math.round(rotation / 15) * 15" in move
    assert "if (!ev.altKey)" in move
    assert "showCanvasGuides(null, null)" in move
    assert "postTimelineOp(" not in move


def test_timeline_nudges_in_frames_and_option_bypasses_pointer_snapping() -> None:
    keys = _function_body("setupTimelineKeys", until="function setupTimelineDirectEdit")
    pointer = _function_body("setupClipPointer", until="function setupTimelineKeys")

    assert "const frames = ev.shiftKey ? 10 : 1" in keys
    assert "const delta = frames / Number(state.projectTimeline?.fps || 30)" in keys
    assert "snapSeconds(d.origStart + dt, d, ev.altKey)" in pointer
    assert "snapSeconds(d.origStart + d.origDur + dt, d, ev.altKey)" in pointer


def test_each_pointer_gesture_has_local_preview_and_one_atomic_commit() -> None:
    canvas_move = _function_body("moveCanvasGesture", until="function finishCanvasGesture")
    canvas_finish = _function_body("finishCanvasGesture", until="function nudgeCanvas")
    timeline_pointer = _function_body("setupClipPointer", until="function setupTimelineKeys")
    timeline_preview = timeline_pointer[timeline_pointer.index('content.addEventListener("pointermove"'):]
    timeline_preview = timeline_preview[:timeline_preview.index("const finish = () =>")]

    assert "postTimelineOp(" not in canvas_move
    assert canvas_finish.count("postTimelineOp(") == 1
    assert "postTimelineOp(" not in timeline_preview
    # A compound trim must be compiled server-side; never chain a second POST
    # from the same pointer-up gesture.
    assert ".then((r) => { if (r) postTimelineOp" not in timeline_pointer


def test_properties_is_a_real_workspace_module_using_the_shared_selection() -> None:
    assert 'properties: { label: "属性"' in JS
    panel_modules = re.search(r"const PANEL_MODULES = new Set\((?P<body>.*?)\);", JS, re.S)
    all_modules = re.search(r"const ALL_WORKSPACE_MODULES =(?P<body>.*?);", JS, re.S)
    assert panel_modules is not None and '"properties"' in panel_modules["body"]
    assert all_modules is not None and '"properties"' in all_modules["body"]
    assert 'else if (view === "properties") renderPropertiesPanel(body)' in JS
    assert 'if (!stageTabs.includes("properties"))' in JS
    assert 'setActiveTab("properties")' in JS

    properties = _function_body("renderPropertiesPanel", until="async function commitEditInput")
    assert "const clip = selectedClip()" in properties
    for label in ("空间", "时间", "X", "Y", "缩放", "旋转", "开始", "时长", "入点", "出点"):
        assert label in properties


def test_revision_conflicts_are_creator_readable_and_resync_authoritative_state() -> None:
    post = _function_body("postTimelineOp", until="function selectClip")

    assert 'data.code === "E_REVISION_CONFLICT"' in post
    assert "画面刚被更新，已同步最新版本，请再试一次" in post
    assert "这次微调没有生效，已恢复到最新状态" in post
    assert "await fetchProjectTimeline()" in post
    assert "expected_project_revision: state.projectRevision" in post
    assert "client_op_id: clientOpId" in post
    assert "renderDirectCanvas()" in post[post.index("finally {"):]


def test_undo_redo_have_buttons_shortcuts_and_authoritative_availability() -> None:
    keys = _function_body("setupTimelineKeys", until="function setupTimelineDirectEdit")
    hint = _function_body("updateEditHint", until="function splitSelected")

    assert 'id="ptl-undo"' in JS and 'id="ptl-redo"' in JS
    assert 'postTimelineOp({ op: "undo", steps: 1 })' in JS
    assert 'postTimelineOp({ op: "redo", steps: 1 })' in JS
    assert "ev.metaKey || ev.ctrlKey" in keys
    assert 'ev.shiftKey ? "redo" : "undo"' in keys
    assert "state.projectTimeline?.can_undo" in hint
    assert "state.projectTimeline?.can_redo" in hint


def test_timeline_payload_exposes_undo_and_redo_capabilities() -> None:
    project = {"assets": [], "timeline": {"tracks": [], "clips": []}}

    unavailable = v3_routes._timeline_payload_dict("session", "project", project, {})
    available = v3_routes._timeline_payload_dict(
        "session", "project", project, {"patch_seq": 2, "redo_stack": [{"files": []}]}
    )

    assert unavailable["can_undo"] is False
    assert unavailable["can_redo"] is False
    assert available["can_undo"] is True
    assert available["can_redo"] is True


def test_continuous_rotation_accepts_fractional_and_arbitrary_finite_angles() -> None:
    assert _validated_effect_value("rotation", 17.5) == 17.5
    assert _validated_effect_value("rotation", 450) == 90.0
    assert _validated_effect_value("rotation", -181.25) == 178.75


def test_lumenframe_adapter_reads_canvas_metadata_and_canonical_png_frames() -> None:
    fetch = _function_body("fetchLumenFrameCanvas", until="function startTimelinePoll")
    image_url = _function_body("lumenFrameImageUrl", until="function lumenFrameBackgroundImage")
    render = _function_body("renderLumenFrameCanvas", until="function renderDirectCanvas")

    assert "/lumenframe/canvas?frame=${frame}" in fetch
    assert "/lumenframe/frame.png?${params}" in image_url
    for parameter in ("frame", "revision", "mode"):
        assert parameter in image_url
    assert 'lumenFrameImageUrl("only", layer.id)' in render
    assert 'lumenFrameImageUrl("exclude", layer.id)' in render
    assert "lumenFrameImageUrl()" in render
    assert 'r"^/sessions/([^/]+)/lumenframe/canvas$"' in ROUTES
    assert 'r"^/sessions/([^/]+)/lumenframe/frame\\.png$"' in ROUTES


def test_lumenframe_click_selects_the_topmost_bounds_hit() -> None:
    hit_test = _function_body("selectLumenFrameAtPoint", until="function setupDirectManipulation")
    setup = _function_body("setupDirectManipulation", until="function updateEditHint")

    assert "[...state.lumenFrameCanvas.layers]" in hit_test
    assert ".sort((a, b) => Number(b.z || 0) - Number(a.z || 0))" in hit_test
    assert ".find((layer) =>" in hit_test
    for boundary in ("bounds.x", "bounds.y", "bounds.width", "bounds.height"):
        assert boundary in hit_test
    assert "selectLumenFrameLayer(hit?.id || null)" in hit_test
    assert 'els.directCanvasSpace.addEventListener("click", selectLumenFrameAtPoint)' in setup


def test_lumenframe_reuses_the_direct_canvas_selection_and_properties_surfaces() -> None:
    select = _function_body("selectLumenFrameLayer", until="function parseTimeValue")
    direct_canvas = _function_body("renderLumenFrameCanvas", until="function renderDirectCanvas")
    render_dispatch = _function_body("renderDirectCanvas", until="function renderTimelineMicroInspector")
    properties = _function_body("renderPropertiesPanel", until="async function commitEditInput")

    assert "state.selectedClipId = null" in select
    assert "state.selectedLumenLayerId = layerId || null" in select
    assert "renderDirectCanvas()" in select
    assert 'refreshPanel("properties")' in select
    assert "els.directCanvasImage" in direct_canvas
    assert "els.directSelectionBox" in direct_canvas
    assert "els.canvasMicroInspector" in direct_canvas
    assert "if (renderLumenFrameCanvas()) return true" in render_dispatch
    assert "const lumenLayer = selectedLumenFrameLayer()" in properties
    assert 'data-edit-field="x"' in properties
    assert 'data-edit-field="rotation"' in properties


def test_lumenframe_properties_exposes_layers_appearance_effects_gradient_and_masks() -> None:
    inspector = _function_body("lumenLayerInspectorHtml", until="function renderPropertiesPanel")
    commit = _function_body("commitLumenInspectorControl", until="async function handleLumenInspectorAction")
    actions = _function_body("handleLumenInspectorAction", until="function snappedCanvasCenter")

    for label in ("图层", "外观", "不透明度", "混合模式", "效果", "渐变", "蒙版", "调整层"):
        assert label in JS
    for control in (
        "opacity", "blend-mode", "clip-to-below", "effect-enabled", "effect-radius",
        "gradient-mode", "gradient-stop-color", "gradient-stop-position", "mask-kind",
        "mask-inset", "mask-feather", "mask-invert", "time-start", "time-duration",
    ):
        assert f'data-lumen-control="{control}"' in JS
    for operation in (
        "set_opacity", "set_blend_mode", "clip_to_below", "set_effect_enabled",
        "set_effect_params", "set_gradient", "set_mask", "clear_mask", "set_time",
    ):
        assert f'op: "{operation}"' in commit
    for operation in ("add_gradient", "add_shape", "add_adjustment_layer", "add_effect", "reorder_effect", "remove_effect"):
        assert f'op: "{operation}"' in actions or f'op: "{operation}"' in JS
    assert "lumenLayerCanTransform" in inspector
    assert "lumenLayerCanEdit" in inspector


def test_lumenframe_appearance_controls_wait_for_the_backend_capability_contract() -> None:
    available = _function_body(
        "lumenAppearanceInspectorAvailable", until="function selectLumenFrameLayer"
    )
    properties = _function_body("renderPropertiesPanel", until="async function commitEditInput")

    for capability in ("add_layer_types", "blend_modes", "effect_types", "mask_shapes"):
        assert capability in available
    assert "if (!lumenAppearanceInspectorAvailable(state.lumenFrameCanvas))" in properties
    assert "lumenLegacyTransformInspectorHtml" in properties
    assert properties.index("if (!lumenAppearanceInspectorAvailable") < properties.index(
        "lumenLayerToolbarHtml"
    )


def test_lumenframe_selected_preview_uses_canonical_composite_outside_a_transform_gesture() -> None:
    render = _function_body("renderLumenFrameCanvas", until="function renderDirectCanvas")
    layout = _function_body("layoutLumenFrameCanvas", until="function syncDirectCanvasMedia")

    assert "const localTransformPreview" in render
    assert 'lumenFrameImageUrl("only", layer.id)' in render
    assert 'lumenFrameImageUrl("exclude", layer.id)' in render
    assert "const compositeUrl = lumenFrameImageUrl()" in render
    assert "els.directCanvasLayerPreview.src = onlyUrl" in render
    assert "els.directCanvasImage.hidden = true" in render
    assert "els.directCanvasLayerPreview.hidden = false" in render
    assert "els.directCanvasLayerPreview.style.transform" in layout
    assert "els.directCanvasImage.style.transform =" not in layout
    assert render.index("const compositeUrl = lumenFrameImageUrl()") < render.index("if (localTransformPreview)")
    assert "Blend modes and adjustment layers" in render


def test_lumenframe_absolute_xy_round_trips_to_backend_center_offsets() -> None:
    geometry = _function_body("lumenFrameGeometry", until="function canvasGeometry")
    numeric_commit = _function_body("commitLumenFrameInput", until="function snappedCanvasCenter")
    pointer_preview = _function_body("moveLumenFrameGesture", until="function finishLumenFrameGesture")

    assert "Number(bounds.x || 0) + Number(bounds.width || 1) / 2" in geometry
    assert "Number(bounds.y || 0) + Number(bounds.height || 1) / 2" in geometry
    assert "Number(transform.x || 0) - Number(stored.x || 0)" in geometry
    assert "Number(transform.y || 0) - Number(stored.y || 0)" in geometry
    assert "Number(geometry.transform.x || 0) + value - geometry.centerX" in numeric_commit
    assert "Number(geometry.transform.y || 0) + value - geometry.centerY" in numeric_commit
    assert "Number(geometry.stored.x || 0) + centerX - geometry.initialCenterX" in pointer_preview
    assert "Number(geometry.stored.y || 0) + centerY - geometry.initialCenterY" in pointer_preview


def test_project_surfaces_drag_explicit_context_into_the_composer() -> None:
    parsed = _html()
    normalize = _function_body("normalizeComposerContextRefs", until="function composerContextKey")
    agent_message = _function_body("composerAgentMessage", until="function makeClientTurnId")
    drag_context = _function_body("contextFromDragElement", until="function addComposerContext")
    send = JS[JS.index('els.sendBtn.addEventListener("click"'):JS.index('els.promptInput.addEventListener("keydown"')]

    assert "composer-context" in parsed.by_id
    assert parsed.by_id["direct-selection-name"][1]["draggable"] == "true"
    assert parsed.by_id["direct-selection-name"][1]["data-context-drag"] == "preview-selection"
    for kind in ("timeline_clip", "canvas_layer", "outline_scene", "outline_shot"):
        assert kind in JS
    assert "COMPOSER_CONTEXT_KINDS.has(kind)" in normalize
    for surface in ("timeline-clip", "preview-selection", "outline-scene", "outline-shot"):
        assert surface in drag_context
    assert 'data-context-drag="timeline-clip"' in JS
    assert 'data-context-drag="outline-scene"' in JS
    assert 'data-context-drag="outline-shot"' in JS
    assert "Workspace context · current Project/session only" in agent_message
    assert "Do not infer or import context from other sessions" in agent_message
    assert "selectedComposerContextRefs()" in send
    assert "clearComposerContext()" in send


def test_lumenframe_rotation_respects_the_backend_to_css_sign_contract() -> None:
    geometry = _function_body("lumenFrameGeometry", until="function canvasGeometry")
    numeric_commit = _function_body("commitLumenFrameInput", until="function snappedCanvasCenter")
    pointer_preview = _function_body("moveLumenFrameGesture", until="function finishLumenFrameGesture")

    assert '"rotation_css_sign": -1' in LUMEN_CANVAS
    assert "state.lumenFrameCanvas?.rotation_css_sign || -1" in geometry
    assert "Number(transform.rotation || 0) * rotationSign" in geometry
    assert 'value / geometry.rotationSign' in numeric_commit
    assert "backendRotation = uiRotation / geometry.rotationSign" in pointer_preview


def test_lumenframe_pointer_gesture_previews_locally_and_posts_once_on_pointerup() -> None:
    begin = _function_body("beginLumenFrameGesture", until="function moveLumenFrameGesture")
    move = _function_body("moveLumenFrameGesture", until="function finishLumenFrameGesture")
    finish = _function_body("finishLumenFrameGesture", until="function nudgeLumenFrame")

    assert 'handle?.dataset.canvasHandle || "move"' in begin
    assert 'drag.mode === "move"' in move
    assert 'drag.mode === "scale"' in move
    assert 'drag.mode === "rotate"' in move
    assert "edit.localTransform =" in move
    assert "layoutLumenFrameCanvas()" in move
    assert "postLumenFrameOp(" not in move
    assert finish.count("postLumenFrameOp(") == 1
    assert 'op: "set_transform", layer_id: layer.id, transform' in finish


def test_lumenframe_writes_are_revisioned_idempotent_and_conflicts_force_a_reread() -> None:
    post = _function_body("postLumenFrameOp", until="function selectClip")

    assert "/lumenframe/op" in post
    assert "base_revision: canvas.revision" in post
    assert 'client_op_id: makeDirectEditId("lumen-edit")' in post
    assert "frame: Number(canvas.canvas?.frame || 0)" in post
    assert 'data.code === "E_REVISION_CONFLICT"' in post
    assert "画面刚被更新，已同步最新版本，请再试一次" in post
    assert "这次图层微调没有生效，已恢复到最新状态" in post
    # Success reconciles canonical pixels too; failures additionally guarantee
    # a forced authoritative read instead of retaining optimistic geometry.
    assert post.count("await fetchLumenFrameCanvas({ force: true })") >= 3
    assert "renderDirectCanvas()" in post[post.index("finally {"):]


def test_lumenframe_forced_reread_is_queued_while_a_poll_read_is_in_flight() -> None:
    fetch = _function_body("fetchLumenFrameCanvas", until="function startTimelinePoll")
    inflight_start = fetch.index("if (state.lumenFrameFetchInFlight)")
    inflight_branch = fetch[inflight_start:fetch.index("state.lumenFrameForceRefreshPending = false")]
    finally_body = fetch[fetch.index("finally {"):]

    # A successful write or revision conflict can race the five-second poll.
    # The forced reconciliation must be remembered rather than discarded by
    # the ordinary in-flight guard, then consumed after that read completes.
    assert "options.force" in inflight_branch
    assert "lumenFrameForceRefreshPending" in inflight_branch
    assert inflight_branch.index("lumenFrameForceRefreshPending") < inflight_branch.index("return null")
    assert "return new Promise" in inflight_branch
    assert "lumenFrameForceRefreshWaiters" in inflight_branch
    assert re.search(
        r"fetchLumenFrameCanvas\(\{[^}]*force\s*:",
        finally_body,
        re.S,
    )
    assert "Promise.resolve(followUp).then" in finally_body


def test_lumenframe_render_failures_are_creator_visible_and_deduplicated() -> None:
    fetch = _function_body("fetchLumenFrameCanvas", until="function startTimelinePoll")

    assert "const failure = await r.json()" in fetch
    assert "state.lumenFrameLastFetchError !== errorKey" in fetch
    assert "LumenFrame 无法渲染" in fetch
    assert "画布仍保留上一次成功状态" in fetch
    assert "state.lumenFrameLastFetchError = null" in fetch


def test_lumenframe_undo_redo_share_buttons_shortcuts_and_history_availability() -> None:
    shell = _function_body("buildTimelineShell", until="function sizeRuler")
    keys = _function_body("setupTimelineKeys", until="function setupTimelineDirectEdit")
    hint = _function_body("updateEditHint", until="function splitSelected")

    assert 'selectedLumenFrameLayer()) postLumenFrameOp({ op: "undo", steps: 1 })' in shell
    assert 'selectedLumenFrameLayer()) postLumenFrameOp({ op: "redo", steps: 1 })' in shell
    assert "if (selectedLumenFrameLayer()) postLumenFrameOp({ op, steps: 1 })" in keys
    assert 'ev.shiftKey ? "redo" : "undo"' in keys
    assert "state.lumenFrameCanvas?.can_undo" in hint
    assert "state.lumenFrameCanvas?.can_redo" in hint
    for operation in (
        "set_transform", "set_opacity", "set_blend_mode", "clip_to_below",
        "add_effect", "set_effect_params", "set_effect_enabled", "reorder_effect",
        "remove_effect", "add_gradient", "set_gradient", "add_shape",
        "add_adjustment_layer", "set_mask", "clear_mask", "set_time", "undo", "redo",
    ):
        assert f'"{operation}"' in LUMEN_CANVAS
