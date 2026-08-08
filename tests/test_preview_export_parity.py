from __future__ import annotations

from pathlib import Path

from gemia import project_render


def test_preview_is_a_draft_preset_of_canonical_export(monkeypatch, tmp_path: Path) -> None:
    seen = {}

    def fake_export(store, project_id, **kwargs):
        seen["store"] = store
        seen["project_id"] = project_id
        seen.update(kwargs)
        return {
            "export_id": "0010-deadbeef-draft-preview",
            "export_path": str(tmp_path / "preview.mp4"),
            "render_preset": "draft-640x360",
            "graph_hash": "deadbeef",
            "render_receipt": {"machine_status": "provisional"},
            "machine_status": "provisional",
        }

    monkeypatch.setattr(project_render, "export_project", fake_export)
    store = object()
    result = project_render.render_project_preview(
        store,
        "project-1",
        output_root=tmp_path,
        max_long_edge=640,
        label="inspect",
        timeout_sec=42,
    )

    assert seen["store"] is store
    assert seen["project_id"] == "project-1"
    assert seen["quality"] == "draft"
    assert seen["max_long_edge"] == 640
    assert seen["verify_decode"] is False
    assert seen["timeout_sec"] == 42
    assert result["render_id"] == result["export_id"]
    assert result["preview_path"] == result["export_path"]
    assert result["graph_hash"] == "deadbeef"
    assert result["render_receipt"]["machine_status"] == "provisional"
