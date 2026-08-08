from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from gemia import project_context
from gemia.production_store import ProductionStore
from gemia.project_store import ProjectHandle
from gemia.project_workspace import (
    FileMutationJournal,
    ProjectWorkspaceError,
    validate_source_root,
)
from gemia.session_manager import SessionManager
from gemia.tools._context import AssetRegistry, ToolContext
from gemia.tools.files import dispatch_delete, dispatch_move, dispatch_write


def _journal(store: ProductionStore, project_id: str) -> FileMutationJournal:
    record = store.load_project(project_id)
    return FileMutationJournal(
        store.project_dir(project_id) / "file-history",
        allowed_roots={
            "source": record["source_root"],
            "edit": record["edit_root"],
        },
    )


def _ctx(tmp_path: Path) -> tuple[ToolContext, ProductionStore, Path, Path]:
    source = tmp_path / "creator-folder"
    source.mkdir()
    store = ProductionStore(tmp_path / "lumeri-data")
    record = store.create_project("project-test", name="Creator", source_root=source)
    edit = Path(record["edit_root"])
    work = tmp_path / "session-work"
    work.mkdir()
    return (
        ToolContext(
            session_id="session-test",
            output_dir=work,
            registry=AssetRegistry(),
            emit_progress=lambda _update: None,
            extra={
                "project_id": "project-test",
                "production_store": store,
                "project_source_root": str(source),
                "project_edit_root": str(edit),
            },
        ),
        store,
        source,
        edit,
    )


def test_project_folder_rejects_broad_or_internal_roots(tmp_path: Path) -> None:
    internal = tmp_path / "lumeri-data"
    internal.mkdir()
    safe = tmp_path / "creator"
    safe.mkdir()
    assert validate_source_root(safe, internal_root=internal) == safe.resolve()
    with pytest.raises(ProjectWorkspaceError):
        validate_source_root(Path("/"), internal_root=internal)
    with pytest.raises(ProjectWorkspaceError):
        validate_source_root(Path.home(), internal_root=internal)
    with pytest.raises(ProjectWorkspaceError):
        validate_source_root(internal, internal_root=internal)


def test_production_project_records_source_edit_and_sessions(tmp_path: Path) -> None:
    source = tmp_path / "creator"
    source.mkdir()
    store = ProductionStore(tmp_path / "lumeri-data")
    record = store.create_project("project-a", name="Film A", source_root=source)
    assert record["name"] == "Film A"
    assert record["source_root"] == str(source.resolve())
    assert Path(record["edit_root"]).is_dir()
    assert store.list_projects()[0]["project_id"] == "project-a"


def test_relative_and_project_edit_paths_have_full_recoverable_access(tmp_path: Path) -> None:
    ctx, store, source, edit = _ctx(tmp_path)

    written = asyncio.run(
        dispatch_write(
            {"path": "notes/brief.txt", "content": "v1", "overwrite": False}, ctx
        )
    )
    assert (source / "notes/brief.txt").read_text(encoding="utf-8") == "v1"
    assert written["file"]["workspace_relative_path"] == "project://source/notes/brief.txt"
    assert written["file_history"]["status"] == "recorded"

    asyncio.run(
        dispatch_write(
            {"path": "project://edit/cut/main.txt", "content": "cut", "overwrite": False},
            ctx,
        )
    )
    assert (edit / "cut/main.txt").read_text(encoding="utf-8") == "cut"

    history = _journal(store, "project-test")
    history.undo()
    assert not (edit / "cut/main.txt").exists()
    history.redo()
    assert (edit / "cut/main.txt").read_text(encoding="utf-8") == "cut"


def test_relative_path_falls_back_to_timeline_project_design_root(tmp_path: Path) -> None:
    project = ProjectHandle.open(
        tmp_path / "timeline-projects",
        "project-fallback",
        session_id="session-fallback",
    )
    ctx = ToolContext(
        session_id="session-fallback",
        output_dir=tmp_path / "session-work",
        registry=AssetRegistry(),
        emit_progress=lambda _update: None,
        project=project,
    )

    asyncio.run(
        dispatch_write(
            {"path": "notes/fallback.txt", "content": "safe", "overwrite": False},
            ctx,
        )
    )

    assert (
        project.store.project_dir(project.project_id) / "design/notes/fallback.txt"
    ).read_text(encoding="utf-8") == "safe"


def test_delete_directory_and_move_are_undoable_without_copying_tree(tmp_path: Path) -> None:
    ctx, store, source, _edit = _ctx(tmp_path)
    folder = source / "takes"
    folder.mkdir()
    (folder / "one.txt").write_text("one", encoding="utf-8")

    deleted = asyncio.run(dispatch_delete({"path": "takes"}, ctx))
    assert deleted["file_history"]["label"] == "delete"
    assert not folder.exists()

    history = _journal(store, "project-test")
    history.undo()
    assert (folder / "one.txt").read_text(encoding="utf-8") == "one"
    history.redo()
    assert not folder.exists()
    history.undo()

    moved = asyncio.run(
        dispatch_move({"source": "takes", "dest": "archive/takes", "overwrite": False}, ctx)
    )
    assert moved["file_history"]["label"] == "move"
    assert not folder.exists()
    assert (source / "archive/takes/one.txt").exists()
    history.undo()
    assert (folder / "one.txt").exists()
    assert not (source / "archive/takes").exists()


def test_project_root_itself_cannot_be_deleted(tmp_path: Path) -> None:
    ctx, _store, source, _edit = _ctx(tmp_path)
    with pytest.raises(ProjectWorkspaceError):
        asyncio.run(dispatch_delete({"path": str(source)}, ctx))


def test_project_list_exposes_undo_redo_availability(tmp_path: Path) -> None:
    source = tmp_path / "creator"
    source.mkdir()
    manager = SessionManager(
        output_root=tmp_path / "lumeri-data",
        sweep_interval_sec=0,
    )
    project = manager.create_project(name="Creator", source_root=source)
    listed = manager.list_projects()
    assert listed[0]["project_id"] == project["project_id"]
    assert listed[0]["file_history"] == {
        "cursor": 0,
        "count": 0,
        "can_undo": False,
        "can_redo": False,
        "latest": None,
    }


def test_project_without_source_uses_private_edit_root_and_context(tmp_path: Path) -> None:
    manager = SessionManager(
        output_root=tmp_path / "lumeri-data",
        sweep_interval_sec=0,
    )
    project = manager.create_project(name="Internal only", source_root=None)
    assert project["source_root"] == ""
    edit = Path(project["edit_root"])
    assert edit.is_dir()
    assert project["context"] == {
        "memory_entries": 0,
        "log_entries": 0,
        "has_recent_log": False,
    }

    store = manager.production_store
    ctx = ToolContext(
        session_id="session-internal",
        output_dir=tmp_path / "session-work",
        registry=AssetRegistry(),
        emit_progress=lambda _update: None,
        extra={
            "project_id": project["project_id"],
            "production_store": store,
            "project_source_root": "",
            "project_edit_root": str(edit),
        },
    )
    written = asyncio.run(
        dispatch_write({"path": "notes/brief.txt", "content": "inside", "overwrite": False}, ctx)
    )
    assert (edit / "notes/brief.txt").read_text(encoding="utf-8") == "inside"
    assert written["file"]["workspace_relative_path"] == "project://edit/notes/brief.txt"

    project_context.remember_fact(store, project["project_id"], "Use a quiet opening", title="Opening")
    project_context.append_log(store, project["project_id"], "rough cut started")
    listed = manager.list_projects()[0]
    assert listed["context"]["memory_entries"] == 1
    assert listed["context"]["log_entries"] == 1
