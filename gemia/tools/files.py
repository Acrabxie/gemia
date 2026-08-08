"""Host-side file tools for the v3 agent loop.

Two surfaces intentionally coexist:

- ``file_*``: first-class Codex-like file tools. Workspace paths are fully
  writable; outside targets may only be newly created/copied/moved under the
  approved outside roots and are never overwritten.
- legacy ``read_file`` / ``write_file`` / ``copy_in`` / ``list_dir`` /
  ``move_file`` / ``organize_files``: compatibility wrappers used by the
  overnight agent/tool schema.
"""
from __future__ import annotations

import shutil
from pathlib import Path
from typing import Any

from gemia.errors import RECOVERY_FIX_ARGS, RECOVERY_NONE, ToolError
from gemia.project_workspace import FileMutationJournal
from gemia.sandbox_v4 import DEFAULT_CREDENTIAL_DENY, DEFAULT_OUTSIDE_CREATE_ROOTS
from gemia.tools._context import AssetRecord, ToolContext, infer_kind

_MAX_READ_BYTES = 512_000
_MAX_WRITE_BYTES = 512_000


def _resolve(path: str | Path, ctx: ToolContext) -> Path:
    raw = str(path or "").strip()
    if not raw:
        raise ValueError("path must be non-empty")
    if raw.startswith("project://"):
        edit_root = _project_program_root(ctx)
        source_root = _project_source_root(ctx)
        if edit_root is None:
            raise ValueError("project:// paths require a persistent project")
        relative = raw[len("project://") :].lstrip("/")
        if relative in {"design", "edit"}:
            return edit_root
        if relative == "source" and source_root is not None:
            return source_root
        if relative.startswith("design/") or relative.startswith("edit/"):
            prefix = "design/" if relative.startswith("design/") else "edit/"
            root = edit_root
            child = relative[len(prefix) :]
        elif relative.startswith("source/") and source_root is not None:
            root = source_root
            child = relative[len("source/") :]
        else:
            raise ValueError("project:// access is limited to project://source/ and project://edit/")
        candidate = (root / child).resolve()
        if not _is_within(candidate, root):
            raise ValueError("project:// path escapes the bound project directory")
        return candidate
    p = Path(raw).expanduser()
    if not p.is_absolute():
        p = (
            _project_source_root(ctx)
            or _project_program_root(ctx)
            or Path(ctx.output_dir)
        ) / p
    return p.resolve()


def _workspace(ctx: ToolContext) -> Path:
    return Path(ctx.output_dir).resolve()


def _project_program_root(ctx: ToolContext) -> Path | None:
    configured = str(ctx.extra.get("project_edit_root") or "").strip()
    if configured:
        return Path(configured).expanduser().resolve()
    project = getattr(ctx, "project", None)
    if project is None:
        return None
    try:
        return (
            project.store.project_dir(project.project_id) / "design"
        ).resolve()
    except (AttributeError, OSError, ValueError):
        return None


def _project_source_root(ctx: ToolContext) -> Path | None:
    configured = str(ctx.extra.get("project_source_root") or "").strip()
    return Path(configured).expanduser().resolve() if configured else None


def _is_within(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
        return True
    except ValueError:
        return False


def _is_workspace(path: Path, ctx: ToolContext) -> bool:
    project_root = _project_program_root(ctx)
    source_root = _project_source_root(ctx)
    return (
        _is_within(path, _workspace(ctx))
        or (project_root is not None and _is_within(path, project_root))
        or (source_root is not None and _is_within(path, source_root))
    )


def _credential_paths() -> list[Path]:
    return [Path(p).expanduser().resolve() for p in DEFAULT_CREDENTIAL_DENY]


def _outside_roots() -> list[Path]:
    return [Path(p).expanduser().resolve() for p in DEFAULT_OUTSIDE_CREATE_ROOTS]


def _is_credential_path(path: Path) -> bool:
    return any(_is_within(path, cred) or path == cred for cred in _credential_paths())


def _is_allowed_outside_target(path: Path) -> bool:
    return any(_is_within(path, root) or path == root for root in _outside_roots())


def _tool_error(message: str, *, code: str = "E_BAD_ARG", hint: str | None = None) -> ToolError:
    return ToolError(message, code=code, recovery=RECOVERY_FIX_ARGS, hint=hint)


def _ensure_readable(path: Path, ctx: ToolContext) -> None:
    if _is_credential_path(path):
        raise PermissionError(f"credential path is not readable: {path}")
    if not path.exists():
        raise FileNotFoundError(f"file does not exist: {path}")
    if not path.is_file():
        raise IsADirectoryError(f"expected a file: {path}")


def _ensure_existing(path: Path, ctx: ToolContext) -> None:
    if _is_credential_path(path):
        raise PermissionError(f"credential path is not accessible: {path}")
    if not path.exists():
        raise FileNotFoundError(f"path does not exist: {path}")


def _ensure_movable_source(path: Path, ctx: ToolContext) -> None:
    _ensure_existing(path, ctx)
    if _is_workspace(path, ctx) or _is_allowed_outside_target(path):
        return
    roots = ", ".join(str(root) for root in _outside_roots())
    raise PermissionError(
        f"outside move source must be inside workspace or an approved outside root ({roots}): {path}"
    )


def _ensure_write_target(path: Path, ctx: ToolContext, *, overwrite: bool) -> None:
    if _is_credential_path(path):
        raise PermissionError(f"credential path is not writable: {path}")
    if _is_workspace(path, ctx):
        if path.exists() and path.is_dir():
            raise IsADirectoryError(f"target is a directory: {path}")
        if path.exists() and not overwrite:
            raise FileExistsError(f"target exists; pass overwrite=true to replace inside workspace: {path}")
        return
    if not _is_allowed_outside_target(path):
        roots = ", ".join(str(root) for root in _outside_roots())
        raise PermissionError(f"outside target must be under an approved create root ({roots}): {path}")
    if path.exists():
        raise FileExistsError(f"outside target exists; refusing to overwrite: {path}")


def _rel(path: Path, ctx: ToolContext) -> str:
    source_root = _project_source_root(ctx)
    if source_root is not None and _is_within(path, source_root):
        suffix = str(path.relative_to(source_root))
        return "project://source" + ("/" + suffix if suffix != "." else "")
    project_root = _project_program_root(ctx)
    if project_root is not None and _is_within(path, project_root):
        suffix = str(path.relative_to(project_root))
        return "project://edit" + ("/" + suffix if suffix != "." else "")
    try:
        return str(path.relative_to(_workspace(ctx)))
    except ValueError:
        return str(path)


def _payload(path: Path, ctx: ToolContext) -> dict[str, Any]:
    exists = path.exists()
    return {
        "path": str(path),
        "workspace_relative_path": _rel(path, ctx) if _is_workspace(path, ctx) else "",
        "persistent_project_file": (
            _project_program_root(ctx) is not None
            and _is_within(path, _project_program_root(ctx))
        ),
        "inside_workspace": _is_workspace(path, ctx),
        "exists": exists,
        "size_bytes": path.stat().st_size if exists and path.is_file() else None,
    }


def _notify_project_program_change(
    ctx: ToolContext, *paths: Path
) -> dict[str, Any] | None:
    root = _project_program_root(ctx)
    if root is None or not any(_is_within(path, root) for path in paths):
        return None
    store = ctx.extra.get("production_store")
    project_id = str(ctx.extra.get("project_id") or "")
    run_id = str(ctx.extra.get("run_id") or "")
    if store is None or not project_id or not run_id:
        return None
    return store.observe_design_program(
        project_id,
        run_id,
        design_root=root,
        trace_id=str(ctx.extra.get("active_trace_id") or "") or None,
    )


def _project_file_journal(ctx: ToolContext, *paths: Path) -> FileMutationJournal | None:
    source_root = _project_source_root(ctx)
    edit_root = _project_program_root(ctx)
    project_id = str(ctx.extra.get("project_id") or "")
    store = ctx.extra.get("production_store")
    if store is None or not project_id:
        return None
    managed = [root for root in (source_root, edit_root) if root is not None]
    if not managed or not all(any(_is_within(path, root) for root in managed) for path in paths):
        return None
    return FileMutationJournal(
        store.project_dir(project_id) / "file-history",
        allowed_roots={"source": source_root, "edit": edit_root},
    )


def _basename(name: Any, *, fallback: str) -> str:
    raw = str(name or fallback).strip()
    base = Path(raw).name
    if not base:
        raise ValueError("target filename must be non-empty")
    return base


def _same_path(left: Path, right: Path) -> bool:
    return left.resolve() == right.resolve()


def _find_registered_asset(path: Path, ctx: ToolContext) -> AssetRecord | None:
    resolved = path.resolve()
    for record in ctx.registry.list_records():
        if record.path.resolve() == resolved:
            return record
    return None


def _register_workspace_asset(path: Path, ctx: ToolContext) -> dict[str, Any]:
    try:
        kind = infer_kind(path)
    except ValueError:
        return {"asset_id": None, "kind": None, "asset_registered": False}
    existing = _find_registered_asset(path, ctx)
    if existing is not None:
        return {
            "asset_id": existing.asset_id,
            "kind": existing.kind,
            "asset_registered": True,
            "asset_reused": True,
            "summary": existing.summary,
        }
    record = ctx.registry.add_external(path, summary=f"workspace import: {path.name}")
    return {
        "asset_id": record.asset_id,
        "kind": kind,
        "asset_registered": True,
        "asset_reused": False,
        "summary": record.summary,
    }


def _read_text(path: Path, *, max_bytes: int) -> tuple[str, bool, int, bool]:
    size = path.stat().st_size
    limit = max(1, min(int(max_bytes), 2_000_000))
    data = path.read_bytes()[:limit]
    truncated = size > limit
    binary = b"\x00" in data
    if binary:
        return f"<binary file: {size} bytes>", truncated, size, True
    return data.decode("utf-8"), truncated, size, False


async def dispatch_list(args: dict[str, Any], ctx: ToolContext) -> dict[str, Any]:
    root = _resolve(args.get("path") or ".", ctx)
    if _is_credential_path(root):
        raise PermissionError(f"credential path is not listable: {root}")
    if not root.exists():
        raise FileNotFoundError(f"directory does not exist: {root}")
    if not root.is_dir():
        raise NotADirectoryError(f"expected a directory: {root}")
    max_entries = max(1, min(int(args.get("max_entries") or 100), 500))
    entries = []
    for child in sorted(root.iterdir(), key=lambda p: p.name)[:max_entries]:
        if _is_credential_path(child):
            continue
        entries.append({
            "name": child.name,
            "path": str(child),
            "workspace_relative_path": _rel(child, ctx) if _is_workspace(child, ctx) else "",
            "kind": "dir" if child.is_dir() else "file",
            "size_bytes": child.stat().st_size if child.is_file() else None,
        })
    return {"directory": str(root), "inside_workspace": _is_workspace(root, ctx), "entries": entries}


async def dispatch_read(args: dict[str, Any], ctx: ToolContext) -> dict[str, Any]:
    path = _resolve(args.get("path"), ctx)
    _ensure_readable(path, ctx)
    max_bytes = int(args.get("max_bytes") or _MAX_READ_BYTES)
    size = path.stat().st_size
    if size > max(1, min(max_bytes, 2_000_000)):
        raise ValueError(f"file is {size} bytes, above max_bytes={max_bytes}")
    text, _truncated, _size, _binary = _read_text(path, max_bytes=max_bytes)
    return {**_payload(path, ctx), "content": text}


async def dispatch_write(args: dict[str, Any], ctx: ToolContext) -> dict[str, Any]:
    path = _resolve(args.get("path"), ctx)
    content = args.get("content", "")
    if not isinstance(content, str):
        raise ValueError("content must be a string")
    if len(content.encode("utf-8")) > _MAX_WRITE_BYTES:
        raise ValueError(f"content exceeds {_MAX_WRITE_BYTES} bytes")
    overwrite = bool(args.get("overwrite", False))
    _ensure_write_target(path, ctx, overwrite=overwrite)
    journal = _project_file_journal(ctx, path)
    history = None
    if journal is not None:
        history = journal.write(
            path,
            lambda target: target.write_text(content, encoding="utf-8"),
            label="write",
        )
    else:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
    revision = _notify_project_program_change(ctx, path)
    return {
        "status": "written",
        "file": _payload(path, ctx),
        "project_revision_commit": revision,
        "file_history": history,
    }


async def dispatch_copy(args: dict[str, Any], ctx: ToolContext) -> dict[str, Any]:
    source = _resolve(args.get("source"), ctx)
    dest = _resolve(args.get("dest"), ctx)
    _ensure_existing(source, ctx)
    overwrite = bool(args.get("overwrite", False))
    _ensure_write_target(dest, ctx, overwrite=overwrite)
    def _copy(target: Path) -> None:
        if source.is_dir():
            shutil.copytree(source, target)
        else:
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source, target)

    journal = _project_file_journal(ctx, dest)
    history = journal.write(dest, _copy, label="copy") if journal is not None else None
    if journal is None:
        _copy(dest)
    revision = _notify_project_program_change(ctx, dest)
    return {
        "status": "copied",
        "source": _payload(source, ctx),
        "dest": _payload(dest, ctx),
        "project_revision_commit": revision,
        "file_history": history,
    }


async def dispatch_move(args: dict[str, Any], ctx: ToolContext) -> dict[str, Any]:
    source = _resolve(args.get("source"), ctx)
    dest = _resolve(args.get("dest"), ctx)
    _ensure_movable_source(source, ctx)
    overwrite = bool(args.get("overwrite", False))
    _ensure_write_target(dest, ctx, overwrite=overwrite)
    journal = _project_file_journal(ctx, source, dest)
    history = journal.move(source, dest, label="move") if journal is not None else None
    if journal is None:
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(source), str(dest))
    revision = _notify_project_program_change(ctx, source, dest)
    return {
        "status": "moved",
        "source_path": str(source),
        "dest": _payload(dest, ctx),
        "project_revision_commit": revision,
        "file_history": history,
    }


async def dispatch_delete(args: dict[str, Any], ctx: ToolContext) -> dict[str, Any]:
    path = _resolve(args.get("path"), ctx)
    if not _is_workspace(path, ctx):
        raise PermissionError("file_delete is limited to the workspace")
    _ensure_existing(path, ctx)
    journal = _project_file_journal(ctx, path)
    if journal is not None:
        history = journal.delete(path, label="delete")
    else:
        history = None
        if path.is_dir():
            shutil.rmtree(path)
        else:
            path.unlink()
    revision = _notify_project_program_change(ctx, path)
    return {
        "status": "deleted",
        "path": str(path),
        "workspace_relative_path": _rel(path, ctx),
        "project_revision_commit": revision,
        "file_history": history,
    }


async def dispatch_read_file(args: dict[str, Any], ctx: ToolContext) -> dict[str, Any]:
    path = _resolve(args.get("path"), ctx)
    try:
        _ensure_readable(path, ctx)
        text, truncated, size, binary = _read_text(path, max_bytes=int(args.get("max_bytes") or _MAX_READ_BYTES))
    except FileNotFoundError as exc:
        raise ToolError(str(exc), code="E_NOT_FOUND", recovery=RECOVERY_FIX_ARGS) from exc
    except PermissionError as exc:
        raise ToolError(str(exc), code="E_DENIED", recovery=RECOVERY_NONE) from exc
    return {"path": str(path), "text": text, "truncated": truncated, "size": size, "binary": binary}


async def dispatch_write_file(args: dict[str, Any], ctx: ToolContext) -> dict[str, Any]:
    path = _resolve(args.get("path"), ctx)
    content = args.get("content", "")
    if not isinstance(content, str):
        raise _tool_error("content must be a string")
    if bool(args.get("append", False)):
        try:
            _ensure_write_target(path, ctx, overwrite=path.exists())
        except PermissionError as exc:
            raise ToolError(str(exc), code="E_DENIED", recovery=RECOVERY_NONE) from exc
        current = path.read_text(encoding="utf-8") if path.exists() else ""
        result = await dispatch_write(
            {"path": str(path), "content": current + content, "overwrite": True}, ctx
        )
        revision = result.get("project_revision_commit")
        history = result.get("file_history")
    else:
        result = await dispatch_write(
            {"path": str(path), "content": content, "overwrite": True}, ctx
        )
        revision = result.get("project_revision_commit")
        history = result.get("file_history")
    return {
        "path": str(path),
        "workspace_relative_path": _rel(path, ctx),
        "bytes_written": len(content.encode("utf-8")),
        "project_revision_commit": revision,
        "file_history": history,
    }


async def dispatch_copy_in(args: dict[str, Any], ctx: ToolContext) -> dict[str, Any]:
    source = _resolve(args.get("source") or args.get("path"), ctx)
    _ensure_readable(source, ctx)
    dest_name = _basename(args.get("as_name") or args.get("dest_name"), fallback=source.name)
    dest = _workspace(ctx) / dest_name
    if _same_path(source, dest):
        copied = False
    else:
        await dispatch_copy(
            {
                "source": str(source),
                "dest": str(dest),
                "overwrite": bool(args.get("overwrite", False)),
            },
            ctx,
        )
        copied = True
    asset = _register_workspace_asset(dest, ctx)
    size = dest.stat().st_size
    return {
        "source": str(source),
        "path": str(dest),
        "workspace_path": str(dest),
        "name": dest.name,
        "size": size,
        "size_bytes": size,
        "bytes_copied": size,
        "copied": copied,
        **asset,
    }


async def dispatch_list_dir(args: dict[str, Any], ctx: ToolContext) -> dict[str, Any]:
    out = await dispatch_list(args, ctx)
    return {"path": out["directory"], "entries": out["entries"]}


async def dispatch_move_file(args: dict[str, Any], ctx: ToolContext) -> dict[str, Any]:
    try:
        return await dispatch_move({
            "source": args.get("source") or args.get("src") or args.get("path"),
            "dest": args.get("dest") or args.get("destination"),
            "overwrite": bool(args.get("overwrite", False)),
        }, ctx)
    except PermissionError as exc:
        raise ToolError(str(exc), code="E_DENIED", recovery=RECOVERY_NONE) from exc


async def dispatch_organize_files(args: dict[str, Any], ctx: ToolContext) -> dict[str, Any]:
    moves = args.get("moves")
    if not isinstance(moves, list):
        raise _tool_error("organize_files requires moves=[{source,dest}, ...]")
    results = []
    for move in moves:
        if not isinstance(move, dict):
            continue
        results.append(await dispatch_move_file(move, ctx))
    return {"status": "organized", "count": len(results), "moves": results}


__all__ = [
    "dispatch_list",
    "dispatch_read",
    "dispatch_write",
    "dispatch_copy",
    "dispatch_move",
    "dispatch_delete",
    "dispatch_read_file",
    "dispatch_write_file",
    "dispatch_copy_in",
    "dispatch_list_dir",
    "dispatch_move_file",
    "dispatch_organize_files",
]
