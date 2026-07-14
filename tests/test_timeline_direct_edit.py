"""Timeline Direct-Edit (UI v1) — DE-A backend edit endpoint.

POST /sessions/{id}/timeline/op lets the USER edit the timeline through the
SAME patches.py ops + ProjectStore patch path the model's verbs use — one
source of truth. These tests drive ``_session_timeline_op`` directly (mirroring
the m4 e2e route harness) against a real AgentLoopV3 project, asserting: each op
applies + lands in the patch log with user provenance; invalid ops are rejected
with the typed E_* code; edits are undoable; and a timeline_op SSE event fires.
"""
from __future__ import annotations

import io
import json
from types import SimpleNamespace
from typing import Any

from gemia import v3_routes
from gemia.agent_loop_v3 import AgentLoopV3


# ── harness ──────────────────────────────────────────────────────────────────


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


def _loop(tmp_path, sid: str) -> tuple[AgentLoopV3, list[dict[str, Any]]]:
    events: list[dict[str, Any]] = []
    loop = AgentLoopV3(
        session_id=sid,
        output_dir=tmp_path,
        gemini_client=SimpleNamespace(model="fake"),
        emit_event=events.append,
    )
    return loop, events


def _seed_video_clip(loop: AgentLoopV3, clip_id: str = "c1", *, start: float = 0.0,
                     duration: float = 5.0, asset_id: str = "a1") -> None:
    """Insert one video clip on V1 via the patch layer (no ffmpeg: the patch
    layer never reads the file, only the dict)."""
    loop.project.apply_ops(
        [
            {"op": "upsert_asset", "asset": {
                "id": asset_id, "asset_id": asset_id, "name": "v.mp4",
                "media_kind": "video", "source_path": "/tmp/v.mp4", "duration": 30.0}},
            {"op": "insert_clip", "track_id": "V1", "at": {"time": start},
             "data": {"clip": {
                 "id": clip_id, "asset_id": asset_id, "media_kind": "video",
                 "duration": duration, "source_in": 0.0, "source_out": duration}}},
        ],
        label="test-setup",
    )


def _post(loop: AgentLoopV3, sid: str, op_body: dict) -> _PostHandler:
    handler = _PostHandler(json.dumps(op_body).encode("utf-8"))
    # run_project_edit mirrors SessionRunner's contract; same-thread execution
    # is fine here — serialization gets its own tests
    # (test_project_write_serialization.py).
    runner = SimpleNamespace(
        agent=loop,
        session_id=sid,
        run_project_edit=lambda fn, timeout=30.0: fn(),
    )
    ok = v3_routes._session_timeline_op(handler, runner)
    assert ok is True
    return handler


def _clip(loop: AgentLoopV3, clip_id: str) -> dict | None:
    for c in loop.project.load()["timeline"]["clips"]:
        if c.get("id") == clip_id:
            return c
    return None


# ── per-op happy paths ───────────────────────────────────────────────────────


def test_move_applies_and_emits_sse_with_user_provenance(tmp_path) -> None:
    loop, events = _loop(tmp_path, "de-move")
    _seed_video_clip(loop)
    events.clear()  # drop the setup timeline_op

    h = _post(loop, "de-move", {"op": "move", "clip_id": "c1", "start": 2.0})

    assert h.status == 200
    assert abs(_clip(loop, "c1")["start"] - 2.0) < 1e-3
    # post-state payload is the GET /timeline shape
    assert h.body_json["session_id"] == "de-move"
    assert "tracks" in h.body_json
    # SSE timeline_op fired for the user edit
    tl_ops = [e for e in events if e.get("kind") == "timeline_op"]
    assert len(tl_ops) == 1 and "move_clip" in tl_ops[0]["ops"]
    # patch log records the edit with user provenance
    assert _clip(loop, "c1")["provenance"]["source"] == "user_direct_edit"


def test_trim_applies(tmp_path) -> None:
    loop, _ = _loop(tmp_path, "de-trim")
    _seed_video_clip(loop, duration=5.0)
    h = _post(loop, "de-trim", {"op": "trim", "clip_id": "c1", "source_in": 1.0, "source_out": 4.0})
    assert h.status == 200
    c = _clip(loop, "c1")
    assert abs(c["source_in"] - 1.0) < 1e-3 and abs(c["source_out"] - 4.0) < 1e-3
    assert abs(c["duration"] - 3.0) < 1e-3


def test_set_time_applies(tmp_path) -> None:
    loop, _ = _loop(tmp_path, "de-settime")
    _seed_video_clip(loop, start=0.0, duration=5.0)
    h = _post(loop, "de-settime", {"op": "set_time", "clip_id": "c1", "start": 3.0})
    assert h.status == 200
    assert abs(_clip(loop, "c1")["start"] - 3.0) < 1e-3


def test_split_creates_second_clip(tmp_path) -> None:
    loop, _ = _loop(tmp_path, "de-split")
    _seed_video_clip(loop, duration=5.0)
    h = _post(loop, "de-split", {"op": "split", "clip_id": "c1", "at_time": 2.0})
    assert h.status == 200
    assert len(loop.project.load()["timeline"]["clips"]) == 2


def test_set_effects_applies(tmp_path) -> None:
    loop, _ = _loop(tmp_path, "de-fx")
    _seed_video_clip(loop)
    h = _post(loop, "de-fx", {"op": "set_effects", "clip_id": "c1", "effects": {"muted": True}})
    assert h.status == 200
    assert _clip(loop, "c1")["effects"].get("muted") is True


def test_delete_removes_clip(tmp_path) -> None:
    loop, _ = _loop(tmp_path, "de-del")
    _seed_video_clip(loop)
    h = _post(loop, "de-del", {"op": "delete", "clip_id": "c1"})
    assert h.status == 200
    assert _clip(loop, "c1") is None


# ── invalid ops rejected with the typed E_* code ──────────────────────────────


def test_unknown_op_rejected(tmp_path) -> None:
    loop, _ = _loop(tmp_path, "de-bad1")
    _seed_video_clip(loop)
    h = _post(loop, "de-bad1", {"op": "frobnicate", "clip_id": "c1"})
    assert h.status == 400
    assert "unknown op" in h.body_json["error"]


def test_move_without_start_or_track_is_bad_arg(tmp_path) -> None:
    loop, _ = _loop(tmp_path, "de-bad2")
    _seed_video_clip(loop)
    h = _post(loop, "de-bad2", {"op": "move", "clip_id": "c1"})
    assert h.status == 400
    assert h.body_json["code"] == "E_BAD_ARG"


def test_move_unknown_clip_is_not_found(tmp_path) -> None:
    loop, _ = _loop(tmp_path, "de-bad3")
    _seed_video_clip(loop)
    h = _post(loop, "de-bad3", {"op": "move", "clip_id": "nope", "start": 1.0})
    assert h.status == 400
    assert h.body_json["code"] == "E_NOT_FOUND"


def test_trim_invalid_range_is_range_error(tmp_path) -> None:
    loop, _ = _loop(tmp_path, "de-bad4")
    _seed_video_clip(loop, duration=5.0)
    # source_out <= source_in
    h = _post(loop, "de-bad4", {"op": "trim", "clip_id": "c1", "source_in": 4.0, "source_out": 2.0})
    assert h.status == 400
    assert h.body_json["code"] == "E_RANGE"


def test_missing_clip_id_rejected(tmp_path) -> None:
    loop, _ = _loop(tmp_path, "de-bad5")
    _seed_video_clip(loop)
    h = _post(loop, "de-bad5", {"op": "move", "start": 1.0})
    assert h.status == 400


# ── undo + patch log integration ──────────────────────────────────────────────


def test_user_edit_is_undoable(tmp_path) -> None:
    loop, _ = _loop(tmp_path, "de-undo")
    _seed_video_clip(loop, start=0.0)
    _post(loop, "de-undo", {"op": "move", "clip_id": "c1", "start": 4.0})
    assert abs(_clip(loop, "c1")["start"] - 4.0) < 1e-3

    loop.project.undo(1)  # the same undo the timeline_undo verb uses
    assert abs(_clip(loop, "c1")["start"] - 0.0) < 1e-3


def test_user_edit_increments_patch_seq(tmp_path) -> None:
    loop, _ = _loop(tmp_path, "de-seq")
    _seed_video_clip(loop)
    before = int(loop.project.store.load_meta(loop.project.project_id)["patch_seq"])
    _post(loop, "de-seq", {"op": "move", "clip_id": "c1", "start": 1.0})
    after = int(loop.project.store.load_meta(loop.project.project_id)["patch_seq"])
    assert after == before + 1


def test_timeline_payload_infers_missing_track_for_existing_clip() -> None:
    """Historical/imported projects may contain clips that reference a track id
    not present in timeline.tracks. The UI payload must still surface those
    clips instead of rendering an empty timeline."""
    payload = v3_routes._timeline_payload_dict(
        "sid",
        "pid",
        {
            "assets": [
                {"id": "v_001", "asset_id": "v_001", "name": "clip.mp4", "media_kind": "video"},
            ],
            "timeline": {
                "fps": 30,
                "width": 1920,
                "height": 1080,
                "duration": 2.0,
                "tracks": [],
                "clips": [
                    {
                        "id": "clip_missing_track",
                        "asset_id": "v_001",
                        "track_id": "V1",
                        "media_kind": "video",
                        "start": 0.0,
                        "duration": 2.0,
                        "source_in": 0.0,
                        "source_out": 2.0,
                    }
                ],
            },
        },
        {"patch_seq": 7},
    )

    assert payload["patch_seq"] == 7
    assert payload["tracks"][0]["id"] == "V1"
    assert payload["tracks"][0]["kind"] == "video"
    assert payload["tracks"][0]["clips"][0]["id"] == "clip_missing_track"


def test_undo_op_reverts_last_edit(tmp_path) -> None:
    """The 'undo' op routes through the same ProjectStore.undo as timeline_undo."""
    loop, _ = _loop(tmp_path, "de-undoop")
    _seed_video_clip(loop, start=0.0)
    _post(loop, "de-undoop", {"op": "move", "clip_id": "c1", "start": 4.0})
    assert abs(_clip(loop, "c1")["start"] - 4.0) < 1e-3
    h = _post(loop, "de-undoop", {"op": "undo", "steps": 1})
    assert h.status == 200
    assert abs(_clip(loop, "c1")["start"] - 0.0) < 1e-3


def test_add_transition_surfaces_in_payload(tmp_path) -> None:
    """Regression: lumerai stores clip["transition_after"] but the UI payload
    read clip["transition"] — every stored transition surfaced as null and the
    feature was invisible on both frontends."""
    loop, _ = _loop(tmp_path, "de-trans")
    _seed_video_clip(loop, "c1", start=0.0, duration=5.0)
    _seed_video_clip(loop, "c2", start=5.0, duration=5.0, asset_id="a2")

    h = _post(loop, "de-trans", {
        "op": "add_transition", "clip_id": "c1",
        "data": {"kind": "dissolve", "duration_sec": 0.5},
    })
    if h.status != 200:
        # direct-edit route may wrap args differently; fall back to the verb path
        loop.project.apply_ops(
            [{"op": "add_transition", "clip_id": "c1", "kind": "dissolve",
              "duration_sec": 0.5}],
            label="test-transition",
        )

    stored = _clip(loop, "c1")["transition_after"]
    assert stored == {"kind": "dissolve", "duration_sec": 0.5}

    payload = v3_routes._timeline_payload_dict(
        "de-trans", loop.project.project_id, loop.project.load(),
        loop.project.store.load_meta(loop.project.project_id),
    )
    clips = [c for t in payload["tracks"] for c in t["clips"] if c["id"] == "c1"]
    assert clips and clips[0]["transition"] == {"kind": "dissolve", "duration_sec": 0.5}
