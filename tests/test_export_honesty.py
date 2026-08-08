"""Export honesty gates (docs/timeline-canonical-plan.md §4, Phase 1).

Every field writable via set_clip_effects / add_transition carries exactly one
class in lumerai/export_support.py; writes of unrendered fields warn at the
tool layer AND the /timeline/op route (warn, never reject), and export records
every stored-but-unrendered field in the manifest's ``dropped_fields``.
"""
from __future__ import annotations

import asyncio
import io
import json
import subprocess
from pathlib import Path
from types import SimpleNamespace
from typing import Any

from gemia import v3_routes
from gemia.agent_loop_v3 import AgentLoopV3
from gemia.project_export import _EPSILON, export_project
from gemia.project_store import ProjectHandle
from gemia.tools import DISPATCHER
from gemia.tools._context import AssetRegistry, ToolContext
from lumerai import export_support
from lumerai.patches import _EFFECT_KEYS, _TRANSITION_KINDS, EPSILON


# ── drift gates (plan rule 1: classification travels with the field) ─────────


def test_every_writable_field_and_kind_is_classified() -> None:
    """A key in _EFFECT_KEYS / _TRANSITION_KINDS with no class is a red test."""
    for key in sorted(_EFFECT_KEYS):
        assert key in export_support.EFFECT_FIELD_TABLE, (
            f"effects key {key!r} is writable but unclassified in "
            "lumerai/export_support.py — plan rule 1 (docs/timeline-canonical-plan.md §7)"
        )
    for kind in sorted(_TRANSITION_KINDS):
        assert kind in export_support.TRANSITION_KIND_TABLE, (
            f"transition kind {kind!r} is writable but unclassified in "
            "lumerai/export_support.py — plan rule 1"
        )
    valid = {export_support.RENDERED, export_support.WARN_AT_WRITE,
             export_support.PREVIEW_ONLY}
    for field, row in export_support.EFFECT_FIELD_TABLE.items():
        assert "*" in row, f"{field!r} row needs a '*' default"
        assert set(row.values()) <= valid
    assert set(export_support.TRANSITION_KIND_TABLE.values()) <= valid


def test_export_epsilon_pins_patch_layer_epsilon() -> None:
    """project_export re-checks adjacency with the same tolerance the patch
    layer validated it with."""
    assert _EPSILON == EPSILON


def test_preview_only_class_is_empty_today() -> None:
    """project_render draws no effects; claiming PREVIEW_ONLY would be a lie
    (plan §4.1). This flips only when a preview renderer actually claims it."""
    for row in export_support.EFFECT_FIELD_TABLE.values():
        assert export_support.PREVIEW_ONLY not in row.values()
    assert export_support.PREVIEW_ONLY not in export_support.TRANSITION_KIND_TABLE.values()


# ── pure-function surface ────────────────────────────────────────────────────


def test_effects_warnings_typed_and_media_kind_aware() -> None:
    warnings = export_support.effects_warnings(
        "video", {"blend_mode": "screen", "speed": 2.0, "rotation": 90},
    )
    assert len(warnings) == 2
    assert {w.split(":", 2)[1] for w in warnings} == {"blend_mode", "speed"}
    assert all(w.startswith("W_NOT_EXPORTED:") for w in warnings)
    # Geometry renders on both the overlay and base-video paths.
    assert export_support.effects_warnings("image", {"scale": 0.5, "opacity": 0.4}) == []
    assert export_support.effects_warnings(
        "video", {"x": 10, "y": 20, "scale": 0.5, "rotation": 17}
    ) == []
    # x/y render for text; scale/opacity do not.
    assert export_support.effects_warnings("text", {"x": 10, "y": 10}) == []
    assert len(export_support.effects_warnings("text", {"scale": 2.0})) == 1
    # Rendered audio fields never warn.
    assert export_support.effects_warnings(
        "video", {"muted": True, "gain_db": -3.0, "fade_in": 0.5, "fade_out": 0.5},
    ) == []


def test_noop_values_and_deletions_do_not_warn() -> None:
    """normalize_project stamps rotation 0 / speed 1 / mirrored False on every
    effects-less clip; identity values must not flood the honesty surface."""
    assert export_support.effects_warnings(
        "video",
        {"rotation": 0, "speed": 1, "mirrored": False, "blur_radius": 0,
         "blend_mode": "normal", "audioDetached": False},
    ) == []
    # Explicit null deletes a key — deleting an unrendered key is fine.
    assert export_support.effects_warnings("video", {"blend_mode": None}) == []
    # Continuous video rotation is now a rendered geometry field.
    assert export_support.effects_warnings("video", {"rotation": 37}) == []


def test_transition_warnings_per_kind() -> None:
    assert export_support.transition_warnings("cut") == []
    assert export_support.transition_warnings("fade") == []
    assert export_support.transition_warnings("dissolve") == []
    wipe = export_support.transition_warnings("wipe")
    assert len(wipe) == 1 and wipe[0].startswith("W_NOT_EXPORTED:transition_after:")


def test_clip_dropped_fields_covers_effects_and_transitions() -> None:
    # wipe on a video clip: kind has no renderer.
    assert export_support.clip_dropped_fields(
        {"media_kind": "video", "transition_after": {"kind": "wipe", "duration_sec": 0.5}},
    ) == [{"field": "transition_after", "reason": "kind_not_supported"}]
    # fade on an overlay clip: renderable kind, uncovered media kind (§5.2
    # out-of-scope: export only implements video-track windows).
    assert export_support.clip_dropped_fields(
        {"media_kind": "image", "transition_after": {"kind": "fade", "duration_sec": 0.5}},
    ) == [{"field": "transition_after", "reason": "not_rendered"}]
    # fade/dissolve on a video clip: render-time decision, not a static drop.
    assert export_support.clip_dropped_fields(
        {"media_kind": "video", "transition_after": {"kind": "dissolve", "duration_sec": 0.5}},
    ) == []
    # Default-stamped effects (identity values) are not drops.
    assert export_support.clip_dropped_fields(
        {"media_kind": "video",
         "effects": {"rotation": 0, "mirrored": False, "muted": False,
                     "audioDetached": False, "speed": 1}},
    ) == []
    # Real unrendered values are.
    dropped = export_support.clip_dropped_fields(
        {"media_kind": "video", "effects": {"blur_radius": 4.0, "gain_db": -3.0}},
    )
    assert dropped == [{"field": "blur_radius", "reason": "not_rendered"}]


# ── tool-layer wiring (dispatch_effects / dispatch_transition) ───────────────


def _ctx(tmp_path: Path, name: str = "honesty01") -> ToolContext:
    handle = ProjectHandle.open(tmp_path / "project", name, session_id=name)
    return ToolContext(
        session_id=name,
        output_dir=tmp_path,
        registry=AssetRegistry(),
        emit_progress=lambda _u: None,
        project=handle,
    )


def _call(verb: str, args: dict[str, Any], ctx: ToolContext) -> dict[str, Any]:
    return asyncio.run(DISPATCHER[verb](args, ctx))


def _seed_video_clip(project, clip_id: str = "c1", *, start: float = 0.0,
                     duration: float = 5.0) -> None:
    project.apply_ops(
        [
            {"op": "upsert_asset", "asset": {
                "id": "a1", "asset_id": "a1", "name": "v.mp4",
                "media_kind": "video", "source_path": "/tmp/v.mp4", "duration": 30.0}},
            {"op": "insert_clip", "track_id": "V1", "at": {"time": start},
             "data": {"clip": {
                 "id": clip_id, "asset_id": "a1", "media_kind": "video",
                 "duration": duration, "source_in": 0.0, "source_out": duration}}},
        ],
        label="test-seed",
    )


def test_set_clip_effects_tool_result_carries_warnings(tmp_path: Path) -> None:
    ctx = _ctx(tmp_path)
    _seed_video_clip(ctx.project)

    out = _call(
        "timeline_set_clip_effects",
        {"clip_id": "c1", "effects": {"blend_mode": "screen", "speed": 2.0, "rotation": 90}},
        ctx,
    )
    assert out["applied"] is True  # warn, never reject
    assert len(out["warnings"]) == 2
    assert {w.split(":", 2)[1] for w in out["warnings"]} == {"blend_mode", "speed"}

    # Rendered fields produce no warnings key at all.
    out_ok = _call(
        "timeline_set_clip_effects",
        {"clip_id": "c1", "effects": {"muted": True, "gain_db": -2.0}},
        ctx,
    )
    assert out_ok["applied"] is True
    assert "warnings" not in out_ok


# ── route wiring (POST /sessions/{id}/timeline/op) ───────────────────────────


class _PostHandler:
    """Minimal POST handler: rfile carries the JSON body, wfile captures output."""

    def __init__(self, body: bytes) -> None:
        self.headers = {"Content-Length": str(len(body))}
        self.path = "/"
        self.rfile = io.BytesIO(body)
        self.wfile = io.BytesIO()
        self.status: int | None = None
        self.response_headers: dict[str, str] = {}
        self.connection = None

    def send_response(self, status: int) -> None:
        self.status = status

    def send_header(self, key: str, value: str) -> None:
        self.response_headers[key.lower()] = value

    def end_headers(self) -> None:
        pass

    @property
    def body_json(self) -> dict:
        return json.loads(self.wfile.getvalue().decode("utf-8"))


def _loop(tmp_path, sid: str) -> AgentLoopV3:
    return AgentLoopV3(
        session_id=sid,
        output_dir=tmp_path,
        gemini_client=SimpleNamespace(model="fake"),
        emit_event=lambda _e: None,
    )


def _post(loop: AgentLoopV3, sid: str, op_body: dict) -> _PostHandler:
    handler = _PostHandler(json.dumps(op_body).encode("utf-8"))
    runner = SimpleNamespace(
        agent=loop,
        session_id=sid,
        run_project_edit=lambda fn, timeout=30.0: fn(),
    )
    assert v3_routes._session_timeline_op(handler, runner) is True
    return handler


def test_route_set_effects_response_carries_warnings(tmp_path) -> None:
    loop = _loop(tmp_path, "hn-fx")
    _seed_video_clip(loop.project)

    h = _post(loop, "hn-fx", {
        "op": "set_effects", "clip_id": "c1",
        "effects": {"blend_mode": "screen", "speed": 2.0, "rotation": 90},
    })
    assert h.status == 200  # warn, never reject
    warnings = h.body_json["warnings"]
    assert len(warnings) == 2
    assert {w.split(":", 2)[1] for w in warnings} == {"blend_mode", "speed"}
    # ...and the edit still applied + returned the normal timeline payload.
    assert "tracks" in h.body_json

    h_ok = _post(loop, "hn-fx", {
        "op": "set_effects", "clip_id": "c1", "effects": {"muted": True},
    })
    assert h_ok.status == 200
    assert "warnings" not in h_ok.body_json


def test_route_add_transition_response_warns_only_for_unrendered_kinds(tmp_path) -> None:
    loop = _loop(tmp_path, "hn-tr")
    _seed_video_clip(loop.project, "c1", start=0.0, duration=5.0)
    _seed2 = loop.project.apply_ops(
        [{"op": "insert_clip", "track_id": "V1", "at": {"time": 5.0},
          "data": {"clip": {
              "id": "c2", "asset_id": "a1", "media_kind": "video",
              "duration": 5.0, "source_in": 0.0, "source_out": 5.0}}}],
        label="test-seed-2",
    )
    assert _seed2 is not None

    h_wipe = _post(loop, "hn-tr", {
        "op": "add_transition", "clip_id": "c1", "kind": "wipe", "duration_sec": 0.5,
    })
    assert h_wipe.status == 200
    assert any(
        w.startswith("W_NOT_EXPORTED:transition_after:")
        for w in h_wipe.body_json["warnings"]
    )

    h_fade = _post(loop, "hn-tr", {
        "op": "add_transition", "clip_id": "c1", "kind": "fade", "duration_sec": 0.5,
    })
    assert h_fade.status == 200
    assert "warnings" not in h_fade.body_json


# ── export manifest honesty (real ffmpeg) ────────────────────────────────────


def test_export_manifest_lists_dropped_fields(tmp_path: Path) -> None:
    """Unrendered blend/speed plus a wipe reach the honesty manifest, while
    the now-rendered continuous video rotation does not appear as a drop."""
    src = tmp_path / "clip.mp4"
    subprocess.run(
        ["ffmpeg", "-y", "-f", "lavfi",
         "-i", "color=c=red:s=128x128:r=30:d=2.0",
         "-c:v", "libx264", "-pix_fmt", "yuv420p", str(src)],
        capture_output=True, check=True,
    )
    handle = ProjectHandle.open(tmp_path / "project", "manifesthonesty",
                                session_id="manifesthonesty")
    handle.apply_ops(
        [
            {"op": "upsert_asset", "asset": {
                "id": "a1", "asset_id": "a1", "name": "clip.mp4",
                "media_kind": "video", "source_path": str(src), "duration": 2.0}},
            {"op": "insert_clip", "track_id": "V1", "at": {"time": 0.0},
             "data": {"clip": {
                 "id": "cA", "asset_id": "a1", "media_kind": "video",
                 "duration": 2.0, "source_in": 0.0, "source_out": 2.0}}},
            {"op": "insert_clip", "track_id": "V1", "at": {"time": 2.0},
             "data": {"clip": {
                 "id": "cB", "asset_id": "a1", "media_kind": "video",
                 "duration": 2.0, "source_in": 0.0, "source_out": 2.0}}},
            {"op": "set_clip_effects", "clip_id": "cA",
             "effects": {"blend_mode": "screen", "speed": 2.0, "rotation": 90}},
            {"op": "add_transition", "clip_id": "cA", "kind": "wipe",
             "duration_sec": 0.5},
        ],
        label="seed-dropped-fields",
    )

    manifest = export_project(
        handle.store, handle.project_id,
        output_root=tmp_path / "out", quality="draft", label="honesty",
    )

    dropped = manifest["dropped_fields"]
    by_field = {(d["clip_id"], d["field"]): d["reason"] for d in dropped}
    assert by_field[("cA", "blend_mode")] == "not_rendered"
    assert by_field[("cA", "speed")] == "not_rendered"
    assert by_field[("cA", "transition_after")] == "kind_not_supported"
    # The untouched clip contributes nothing (no-op exemption).
    assert not any(d["clip_id"] == "cB" for d in dropped)
    # wipe is never planned as a render.
    assert manifest["transitions_rendered"] == 0
    assert manifest["machine_status"] == "rejected"
    assert any(
        blocker.get("code") == "dropped_fields"
        for blocker in manifest["machine_blockers"]
    )
    assert manifest["render_receipt"]["accepted"] is False
    # Manifest file on disk carries the same honesty payload.
    on_disk = json.loads(Path(manifest["manifest_path"]).read_text(encoding="utf-8"))
    assert on_disk["dropped_fields"] == dropped
    assert "transitions" in on_disk
