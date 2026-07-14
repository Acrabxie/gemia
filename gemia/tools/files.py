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
from gemia.sandbox_v4 import DEFAULT_CREDENTIAL_DENY, DEFAULT_OUTSIDE_CREATE_ROOTS
from gemia.tools._context import AssetRecord, ToolContext, infer_kind

_MAX_READ_BYTES = 512_000
_MAX_WRITE_BYTES = 512_000


def _resolve(path: str | Path, ctx: ToolContext) -> Path:
    raw = str(path or "").strip()
    if not raw:
        raise ValueError("path must be non-empty")
    p = Path(raw).expanduser()
    if not p.is_absolute():
        p = Path(ctx.output_dir) / p
    return p.resolve()


def _workspace(ctx: ToolContext) -> Path:
    return Path(ctx.output_dir).resolve()


def _is_within(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
        return True
    except ValueError:
        return False


def _is_workspace(path: Path, ctx: ToolContext) -> bool:
    return _is_within(path, _workspace(ctx))


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


def _ensure_movable_source(path: Path, ctx: ToolContext) -> None:
    _ensure_readable(path, ctx)
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
    try:
        return str(path.relative_to(_workspace(ctx)))
    except ValueError:
        return str(path)


def _payload(path: Path, ctx: ToolContext) -> dict[str, Any]:
    exists = path.exists()
    return {
        "path": str(path),
        "workspace_relative_path": _rel(path, ctx) if _is_workspace(path, ctx) else "",
        "inside_workspace": _is_workspace(path, ctx),
        "exists": exists,
        "size_bytes": path.stat().st_size if exists and path.is_file() else None,
    }


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
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
    return {"status": "written", "file": _payload(path, ctx)}


async def dispatch_copy(args: dict[str, Any], ctx: ToolContext) -> dict[str, Any]:
    source = _resolve(args.get("source"), ctx)
    dest = _resolve(args.get("dest"), ctx)
    _ensure_readable(source, ctx)
    overwrite = bool(args.get("overwrite", False))
    _ensure_write_target(dest, ctx, overwrite=overwrite)
    dest.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(source, dest)
    return {"status": "copied", "source": _payload(source, ctx), "dest": _payload(dest, ctx)}


async def dispatch_move(args: dict[str, Any], ctx: ToolContext) -> dict[str, Any]:
    source = _resolve(args.get("source"), ctx)
    dest = _resolve(args.get("dest"), ctx)
    _ensure_movable_source(source, ctx)
    overwrite = bool(args.get("overwrite", False))
    _ensure_write_target(dest, ctx, overwrite=overwrite)
    dest.parent.mkdir(parents=True, exist_ok=True)
    shutil.move(str(source), str(dest))
    return {"status": "moved", "source_path": str(source), "dest": _payload(dest, ctx)}


async def dispatch_delete(args: dict[str, Any], ctx: ToolContext) -> dict[str, Any]:
    path = _resolve(args.get("path"), ctx)
    if not _is_workspace(path, ctx):
        raise PermissionError("file_delete is limited to the workspace")
    _ensure_readable(path, ctx)
    path.unlink()
    return {"status": "deleted", "path": str(path), "workspace_relative_path": _rel(path, ctx)}


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
        if _is_credential_path(path):
            raise ToolError(f"credential path is not writable: {path}", code="E_DENIED", recovery=RECOVERY_NONE)
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as fh:
            fh.write(content)
    else:
        await dispatch_write({"path": str(path), "content": content, "overwrite": True}, ctx)
    return {"path": str(path), "bytes_written": len(content.encode("utf-8"))}


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
