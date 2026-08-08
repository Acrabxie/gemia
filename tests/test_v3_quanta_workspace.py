from __future__ import annotations

import io
import json
from pathlib import Path
from types import SimpleNamespace
from xml.etree import ElementTree

from gemia import v3_routes


ROOT = Path(__file__).resolve().parents[1]


class _Handler:
    def __init__(self) -> None:
        self.headers: dict[str, str] = {}
        self.path = "/"
        self.wfile = io.BytesIO()
        self.status: int | None = None

    def send_response(self, status: int) -> None:
        self.status = status

    def send_header(self, _key: str, _value: str) -> None:
        pass

    def end_headers(self) -> None:
        pass

    @property
    def body_json(self) -> dict:
        return json.loads(self.wfile.getvalue().decode("utf-8"))


def test_quanta_surface_inherits_shell_with_isolated_layout_state() -> None:
    source = (ROOT / "static/v3/v3.js").read_text(encoding="utf-8")
    css = (ROOT / "static/v3/v3.css").read_text(encoding="utf-8")

    assert 'startsWith("/quanta")' in source
    assert 'surfaceProductName = isQuantaSurface ? "Lumeri Quanta" : "Lumeri Video"' in source
    assert 'surfaceStoragePrefix = isQuantaSurface ? "lumeri:quanta" : "lumeri:v3"' in source
    assert 'quanta: { label: "状态树"' in source
    assert '`/sessions/${sessionId}/quanta`' in source
    assert '? ["quanta", "preview", "tasks", "files", "library", "skills"]' in source
    assert 'workspaceSizes.quanta = { ...workspaceSizes.quanta, width: 100, height: 72 }' in source
    assert 'isQuantaSurface && id === "quanta" ? { width: 100 }' in source
    assert ".quanta-canvas-sheet" in css
    assert ".quanta-tree-state.active" in css


def test_quanta_owns_warm_identity_without_changing_video_mark() -> None:
    source = (ROOT / "static/v3/v3.js").read_text(encoding="utf-8")
    gate_source = (ROOT / "static/v3/auth-gate.js").read_text(encoding="utf-8")
    css = (ROOT / "static/v3/v3.css").read_text(encoding="utf-8")
    quanta_mark = (ROOT / "static/v3/quanta-mark.svg").read_text(encoding="utf-8")
    quanta_favicon = (ROOT / "static/v3/quanta-favicon.svg").read_text(encoding="utf-8")
    video_mark = (ROOT / "static/v3/lumeri-mark.svg").read_text(encoding="utf-8")

    video_shapes = list(ElementTree.fromstring(video_mark))
    mark_shapes = list(ElementTree.fromstring(quanta_mark))
    top_line, top_point, bottom_point, bottom_line = mark_shapes
    video_top_line, video_top_point, video_bottom_line = video_shapes
    for name in ("x", "y", "width", "height", "rx"):
        assert top_line.attrib[name] == video_top_line.attrib[name]
    for name in ("cx", "cy", "r"):
        assert top_point.attrib[name] == video_top_point.attrib[name]
    assert bottom_point.attrib["r"] == top_point.attrib["r"]
    for name in ("y", "height", "rx"):
        assert bottom_line.attrib[name] == video_bottom_line.attrib[name]
    original_left = float(video_bottom_line.attrib["x"])
    original_right = original_left + float(video_bottom_line.attrib["width"])
    assert float(bottom_point.attrib["cx"]) - float(bottom_point.attrib["r"]) == original_left
    assert float(bottom_line.attrib["x"]) + float(bottom_line.attrib["width"]) == original_right
    top_gap = float(top_point.attrib["cx"]) - float(top_point.attrib["r"]) - (
        float(top_line.attrib["x"]) + float(top_line.attrib["width"])
    )
    bottom_gap = float(bottom_line.attrib["x"]) - (
        float(bottom_point.attrib["cx"]) + float(bottom_point.attrib["r"])
    )
    assert bottom_gap == top_gap
    assert "#FFD166" in quanta_mark
    assert 'circle cx="42.3" cy="78.88" r="11.8"' in quanta_favicon
    assert 'rect x="64.1" y="67.08" width="39.56" height="23.6"' in quanta_favicon
    assert "/video/quanta-mark.svg" in source
    assert "/video/quanta-favicon.svg" in source
    assert "/video/quanta-mark.svg" in gate_source
    assert "/video/quanta-favicon.svg" in gate_source
    assert 'classList.add("quanta-surface")' in gate_source
    assert "html.quanta-surface" in css
    assert "--m3-primary:              #ffd166;" in css
    assert "#8BD8EA" in video_mark
    assert "#FFD166" not in video_mark


def test_quanta_route_returns_lifted_project_tree(monkeypatch) -> None:
    flat = {
        "theme": {"aspect": "16:9"},
        "slides": [{
            "id": "intro",
            "title": "Intro",
            "blocks": [{"id": "title", "kind": "text", "text": "Hello"}],
            "builds": [{"id": "b1", "visible_block_ids": ["title"], "dwell_sec": 2}],
        }],
        "default_path": ["intro"],
    }
    project = SimpleNamespace(
        project_id="project-1",
        load=lambda: {"quanta": flat},
        store=SimpleNamespace(load_meta=lambda _project_id: {"patch_seq": 7}),
    )
    runner = SimpleNamespace(agent=SimpleNamespace(project=project), cached_project_revision=4)
    monkeypatch.setattr(
        v3_routes,
        "get_manager",
        lambda: SimpleNamespace(get=lambda session_id: runner if session_id == "session-1" else None),
    )

    handler = _Handler()
    assert v3_routes._session_quanta(handler, "session-1") is True
    assert handler.status == 200
    payload = handler.body_json
    assert payload["patch_seq"] == 7
    assert payload["project_revision"] == 4
    assert payload["quanta"]["version"] == 2
    assert payload["quanta"]["root"]["children"][0]["children"][0]["id"] == "intro_b1"
