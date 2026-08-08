"""Persistent on-disk store for canonical Lumeri project_state.

This module is the host-side complement to the sandbox: scripts run inside the
sandbox and emit TimelinePatch JSON, but only the host can apply those patches
to a real project. ``ProjectStore`` owns the disk layout::

    <root>/<project_id>/state.json          # current normalized snapshot
    <root>/<project_id>/patches/0001.json   # append-only history (one file/patch)
    <root>/<project_id>/meta.json           # created_at, updated_at, patch_seq

It is intentionally small and dependency-free beyond ``gemia.project_model``
and ``lumerai.patches``.
"""
from __future__ import annotations

import copy
import fcntl
import json
import re
import threading
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Iterator

from gemia.project_model import empty_project, normalize_project
from lumerai.patches import apply_timeline_patches


_PROJECT_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_\-]{0,63}$")


class ProjectStoreError(ValueError):
    """Raised for invalid project ids or missing projects."""


class ProjectStore:
    def __init__(self, root: str | Path) -> None:
        self.root = Path(root).expanduser().resolve()
        self.root.mkdir(parents=True, exist_ok=True)
        # Writers are read-modify-write cycles (load state+meta → apply →
        # write patch files+state+meta). /timeline/op and undo run on HTTP
        # threads (ThreadingHTTPServer) while agent verbs run on the session
        # loop thread, so without a lock two writers can both read patch_seq
        # N and clobber patches/000(N+1).json — one history entry silently
        # lost, last state.json wins. Individual files stay uncorrupted
        # (_write_json is atomic), so readers need no lock.
        self._locks_guard = threading.Lock()
        self._project_locks: dict[str, threading.RLock] = {}

    def _project_lock(self, project_id: str) -> threading.RLock:
        with self._locks_guard:
            lock = self._project_locks.get(project_id)
            if lock is None:
                lock = self._project_locks[project_id] = threading.RLock()
            return lock

    def _process_lock_path(self, project_id: str) -> Path:
        self._validate_id(project_id)
        return self.root / ".locks" / f"{project_id}.lock"

    @contextmanager
    def _project_write_lock(self, project_id: str) -> Iterator[None]:
        """Serialize one project's read-modify-write cycle across processes."""
        # Every writer acquires locks in this order and none re-enters this
        # helper, so the existing thread lock and the process lock cannot form
        # a lock-order inversion.
        with self._project_lock(project_id):
            lock_path = self._process_lock_path(project_id)
            lock_path.parent.mkdir(parents=True, exist_ok=True)
            with lock_path.open("a+b") as lock_file:
                fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX)
                try:
                    yield
                finally:
                    fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)

    # ── path helpers ────────────────────────────────────────────────
    def project_dir(self, project_id: str) -> Path:
        self._validate_id(project_id)
        return self.root / project_id

    def state_path(self, project_id: str) -> Path:
        return self.project_dir(project_id) / "state.json"

    def patches_dir(self, project_id: str) -> Path:
        return self.project_dir(project_id) / "patches"

    def meta_path(self, project_id: str) -> Path:
        return self.project_dir(project_id) / "meta.json"

    def seed_path(self, project_id: str) -> Path:
        return self.project_dir(project_id) / "seed.json"

    def discarded_dir(self, project_id: str) -> Path:
        return self.project_dir(project_id) / "patches_discarded"

    def renders_dir(self, project_id: str) -> Path:
        return self.project_dir(project_id) / "renders"

    def render_manifest_path(self, project_id: str, render_id: str) -> Path:
        return self.renders_dir(project_id) / f"{render_id}.json"

    def exists(self, project_id: str) -> bool:
        try:
            return self.state_path(project_id).exists()
        except ProjectStoreError:
            return False

    # ── create / load ───────────────────────────────────────────────
    def create(self, project_id: str, *, seed: dict[str, Any] | None = None) -> dict[str, Any]:
        with self._project_write_lock(project_id):
            pdir = self.project_dir(project_id)
            if (pdir / "state.json").exists():
                raise ProjectStoreError(f"project already exists: {project_id}")
            pdir.mkdir(parents=True, exist_ok=True)
            self.patches_dir(project_id).mkdir(parents=True, exist_ok=True)
            state = normalize_project(seed) if seed else empty_project()
            # The storage key is authoritative.  ``empty_project()`` creates a
            # random document id, which previously left every newly created
            # durable project with two identities and made migrated history
            # reopen under an "Untitled" inner project.
            state["project_id"] = project_id
            now = datetime.now(timezone.utc).isoformat()
            self._write_json(self.state_path(project_id), state)
            self._write_json(self.seed_path(project_id), state)
            self._write_json(
                self.meta_path(project_id),
                {
                    "project_id": project_id,
                    "created_at": now,
                    "updated_at": now,
                    "patch_seq": 0,
                    "undo_log": [],
                    "redo_stack": [],
                    "client_ops": [],
                },
            )
            return copy.deepcopy(state)

    def load(self, project_id: str) -> dict[str, Any]:
        path = self.state_path(project_id)
        if not path.exists():
            raise ProjectStoreError(f"project not found: {project_id}")
        raw = json.loads(path.read_text(encoding="utf-8"))
        return normalize_project(raw)

    def load_meta(self, project_id: str) -> dict[str, Any]:
        path = self.meta_path(project_id)
        if not path.exists():
            raise ProjectStoreError(f"project meta not found: {project_id}")
        return json.loads(path.read_text(encoding="utf-8"))

    # ── patch application ──────────────────────────────────────────
    def apply_patches(
        self,
        project_id: str,
        patches: list[dict[str, Any]],
        *,
        session_id: str,
        script_hash: str,
        client_op_id: str | None = None,
    ) -> dict[str, Any]:
        """Apply patches to the stored project, persist each patch, return new state.

        Returns a dict::

            {
                "project_state": <normalized state>,
                "patch_seq_start": int,  # first new seq (0 if no patches)
                "patch_seq_end": int,    # last new seq (0 if no patches)
                "patch_files": [str, ...],
            }
        """
        with self._project_write_lock(project_id):
            current = self.load(project_id)
            meta = self.load_meta(project_id)
            last_seq = int(meta.get("patch_seq") or 0)
            if client_op_id and self._client_op_seen_meta(meta, client_op_id):
                return {
                    "project_state": current,
                    "patch_seq_start": 0,
                    "patch_seq_end": last_seq,
                    "patch_files": [],
                    "duplicate": True,
                }
            if not patches:
                return {
                    "project_state": current,
                    "patch_seq_start": 0,
                    "patch_seq_end": 0,
                    "patch_files": [],
                }
            updated = apply_timeline_patches(current, patches)
            updated["project_id"] = project_id
            patches_dir = self.patches_dir(project_id)
            patches_dir.mkdir(parents=True, exist_ok=True)
            now = datetime.now(timezone.utc).isoformat()
            written: list[str] = []
            for patch in patches:
                last_seq += 1
                entry = {
                    "seq": last_seq,
                    "applied_at": now,
                    "session_id": session_id,
                    "script_hash": script_hash,
                    "patch": patch,
                }
                path = patches_dir / f"{last_seq:04d}.json"
                self._write_json(path, entry)
                written.append(str(path))
            self._write_json(self.state_path(project_id), updated)
            meta["updated_at"] = now
            meta["patch_seq"] = last_seq
            # A new edit after undo creates a new branch. Discarded patch files
            # stay on disk for audit, but they are no longer redo candidates.
            meta["redo_stack"] = []
            if client_op_id:
                client_ops = list(meta.get("client_ops") or [])
                client_ops.append({
                    "id": str(client_op_id),
                    "seq": last_seq,
                    "at": now,
                })
                meta["client_ops"] = client_ops[-200:]
            self._write_json(self.meta_path(project_id), meta)
            start = last_seq - len(patches) + 1
            return {
                "project_state": updated,
                "patch_seq_start": start,
                "patch_seq_end": last_seq,
                "patch_files": written,
                "duplicate": False,
            }

    @staticmethod
    def _client_op_seen_meta(meta: dict[str, Any], client_op_id: str) -> bool:
        return any(
            str(item.get("id") or "") == str(client_op_id)
            for item in (meta.get("client_ops") or [])
            if isinstance(item, dict)
        )

    def client_op_seen(self, project_id: str, client_op_id: str | None) -> bool:
        if not client_op_id:
            return False
        return self._client_op_seen_meta(self.load_meta(project_id), str(client_op_id))

    def load_seed(self, project_id: str) -> dict[str, Any]:
        path = self.seed_path(project_id)
        if not path.exists():
            raise ProjectStoreError(f"seed missing for project: {project_id}")
        return normalize_project(json.loads(path.read_text(encoding="utf-8")))

    def undo_to_seq(self, project_id: str, target_seq: int) -> dict[str, Any]:
        """Rewind the project to the state right after patch ``target_seq``.

        ``target_seq == 0`` rewinds to the original seed. Discarded patch files
        are moved (not deleted) into ``patches_discarded/`` for audit.
        Returns ``{project_state, from_seq, to_seq, discarded: [seq, ...]}``.
        """
        with self._project_write_lock(project_id):
            return self._undo_to_seq_locked(project_id, target_seq)

    def _undo_to_seq_locked(self, project_id: str, target_seq: int) -> dict[str, Any]:
        if not isinstance(target_seq, int) or target_seq < 0:
            raise ProjectStoreError(f"target_seq must be a non-negative int, got {target_seq!r}")
        meta = self.load_meta(project_id)
        current_seq = int(meta.get("patch_seq") or 0)
        if target_seq > current_seq:
            raise ProjectStoreError(
                f"target_seq {target_seq} is beyond current patch_seq {current_seq}"
            )
        if target_seq == current_seq:
            return {
                "project_state": self.load(project_id),
                "from_seq": current_seq,
                "to_seq": current_seq,
                "discarded": [],
            }
        history = self.history(project_id)
        keep = [entry for entry in history if int(entry.get("seq") or 0) <= target_seq]
        discard = [entry for entry in history if int(entry.get("seq") or 0) > target_seq]
        # Rebuild from seed by replaying kept patches.
        state = self.load_seed(project_id)
        if keep:
            state = apply_timeline_patches(state, [e["patch"] for e in keep])
        state["project_id"] = project_id
        # Move discarded patch files aside.
        discarded_dir = self.discarded_dir(project_id)
        discarded_dir.mkdir(parents=True, exist_ok=True)
        moved: list[int] = []
        now = datetime.now(timezone.utc).isoformat()
        timestamp_tag = now.replace(":", "").replace("-", "").replace(".", "")
        redo_files: list[dict[str, Any]] = []
        for entry in discard:
            seq = int(entry.get("seq") or 0)
            src = self.patches_dir(project_id) / f"{seq:04d}.json"
            if src.exists():
                dest = discarded_dir / f"{seq:04d}.{timestamp_tag}.json"
                src.replace(dest)
                redo_files.append({"seq": seq, "file": dest.name})
            moved.append(seq)
        # Persist rebuilt state + meta.
        self._write_json(self.state_path(project_id), state)
        meta["updated_at"] = now
        meta["patch_seq"] = target_seq
        log = list(meta.get("undo_log") or [])
        log.append(
            {
                "at": now,
                "from_seq": current_seq,
                "to_seq": target_seq,
                "discarded": moved,
            }
        )
        meta["undo_log"] = log
        redo_stack = list(meta.get("redo_stack") or [])
        if redo_files:
            redo_stack.append({
                "at": now,
                "from_seq": current_seq,
                "to_seq": target_seq,
                "files": redo_files,
            })
        meta["redo_stack"] = redo_stack
        self._write_json(self.meta_path(project_id), meta)
        return {
            "project_state": state,
            "from_seq": current_seq,
            "to_seq": target_seq,
            "discarded": moved,
        }

    def redo(self, project_id: str, steps: int = 1) -> dict[str, Any]:
        """Restore up to ``steps`` patches from the most recent undo branch."""
        if not isinstance(steps, int) or steps < 1:
            raise ProjectStoreError(f"redo steps must be >= 1, got {steps!r}")
        with self._project_write_lock(project_id):
            meta = self.load_meta(project_id)
            current_seq = int(meta.get("patch_seq") or 0)
            redo_stack = list(meta.get("redo_stack") or [])
            state = self.load(project_id)
            restored: list[int] = []
            remaining = min(steps, 50)

            while remaining > 0 and redo_stack:
                batch = dict(redo_stack[-1])
                files = [dict(item) for item in (batch.get("files") or [])]
                files.sort(key=lambda item: int(item.get("seq") or 0))
                if not files:
                    redo_stack.pop()
                    continue
                item = files[0]
                seq = int(item.get("seq") or 0)
                if seq != current_seq + 1:
                    raise ProjectStoreError(
                        f"redo history is not contiguous: expected {current_seq + 1}, got {seq}"
                    )
                source = self.discarded_dir(project_id) / str(item.get("file") or "")
                if not source.is_file():
                    raise ProjectStoreError(f"redo patch file missing for seq {seq}")
                entry = json.loads(source.read_text(encoding="utf-8"))
                patch = entry.get("patch")
                if not isinstance(patch, dict):
                    raise ProjectStoreError(f"redo patch {seq} is invalid")
                state = apply_timeline_patches(state, [patch])
                destination = self.patches_dir(project_id) / f"{seq:04d}.json"
                if destination.exists():
                    raise ProjectStoreError(f"redo destination already exists for seq {seq}")
                source.replace(destination)
                restored.append(seq)
                current_seq = seq
                remaining -= 1
                files.pop(0)
                if files:
                    batch["files"] = files
                    batch["to_seq"] = current_seq
                    redo_stack[-1] = batch
                else:
                    redo_stack.pop()

            state["project_id"] = project_id
            now = datetime.now(timezone.utc).isoformat()
            self._write_json(self.state_path(project_id), state)
            meta["updated_at"] = now
            meta["patch_seq"] = current_seq
            meta["redo_stack"] = redo_stack
            log = list(meta.get("undo_log") or [])
            log.append({
                "at": now,
                "direction": "redo",
                "from_seq": int(meta.get("patch_seq") or 0) - len(restored),
                "to_seq": current_seq,
                "restored": restored,
            })
            meta["undo_log"] = log
            self._write_json(self.meta_path(project_id), meta)
            return {
                "project_state": state,
                "from_seq": current_seq - len(restored),
                "to_seq": current_seq,
                "restored": restored,
            }

    def history(self, project_id: str) -> list[dict[str, Any]]:
        pdir = self.patches_dir(project_id)
        if not pdir.exists():
            return []
        entries: list[dict[str, Any]] = []
        for path in sorted(pdir.glob("*.json")):
            if path.name.startswith("._"):
                continue
            try:
                entries.append(json.loads(path.read_text(encoding="utf-8")))
            except (OSError, json.JSONDecodeError):
                continue
        entries.sort(key=lambda e: int(e.get("seq") or 0))
        return entries

    # ── internals ───────────────────────────────────────────────────
    @staticmethod
    def _validate_id(project_id: str) -> None:
        if not isinstance(project_id, str) or not _PROJECT_ID_RE.match(project_id):
            raise ProjectStoreError(
                f"invalid project_id (must match [A-Za-z0-9][A-Za-z0-9_-]{{0,63}}): {project_id!r}"
            )

    @staticmethod
    def _write_json(path: Path, payload: Any) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(path.suffix + ".tmp")
        tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        tmp.replace(path)


_QUANTA_PATCH_OPS = frozenset({
    "set_quanta",
    "patch_quantum",
    "insert_quantum",
    "remove_quantum",
    "move_quantum",
})
_SHOTLIST_PATCH_OPS = frozenset({"set_shotlist", "update_shot"})
_PROJECT_PATCH_OPS = frozenset({"set_project_title"})
_SEGMENT_PATCH_OPS = frozenset({"segment_edit"})


def _patch_state_scope(ops: list[dict[str, Any]]) -> str:
    """Classify an applied patch for additive ``timeline_op`` consumers."""
    scopes: set[str] = set()
    for op in ops:
        name = str(op.get("op") or "") if isinstance(op, dict) else ""
        if name in _QUANTA_PATCH_OPS:
            scopes.add("quanta")
        elif name in _SHOTLIST_PATCH_OPS:
            scopes.add("shotlist")
        elif name in _PROJECT_PATCH_OPS:
            scopes.add("project")
        elif name in _SEGMENT_PATCH_OPS:
            scopes.add("segment")
        else:
            scopes.add("timeline")
    return next(iter(scopes)) if len(scopes) == 1 else "project"


class ProjectHandle:
    """Session-scoped binding of one project to the v3 agent loop.

    The loop owns exactly one handle per session; every timeline mutation in
    that session flows through ``apply_ops`` so it lands in the append-only
    patch log (undo/audit for free). ``on_patch`` lets the loop surface each
    applied patch as an SSE event without this module knowing about SSE.
    """

    def __init__(
        self,
        store: ProjectStore,
        project_id: str,
        *,
        session_id: str,
        on_patch: Callable[[dict[str, Any]], None] | None = None,
    ) -> None:
        self.store = store
        self.project_id = project_id
        self.session_id = session_id
        self.on_patch = on_patch

    @classmethod
    def open(
        cls,
        root: str | Path,
        project_id: str,
        *,
        session_id: str,
        on_patch: Callable[[dict[str, Any]], None] | None = None,
    ) -> "ProjectHandle":
        """Open (creating if needed) the project backing this session.

        ``project_id`` values that don't satisfy the store's id rule are
        mapped to a stable ``p_<hash>`` so any session id is acceptable.
        """
        if not _PROJECT_ID_RE.match(project_id or ""):
            import hashlib

            project_id = "p_" + hashlib.sha1((project_id or "session").encode("utf-8")).hexdigest()[:12]
        store = ProjectStore(root)
        if not store.exists(project_id):
            store.create(project_id)
        return cls(store, project_id, session_id=session_id, on_patch=on_patch)

    def load(self) -> dict[str, Any]:
        return self.store.load(self.project_id)

    def apply_ops(
        self, ops: list[dict[str, Any]], *, label: str = "v3-verb",
        client_op_id: str | None = None,
    ) -> dict[str, Any]:
        """Apply one patch of ``ops`` atomically; returns the store result."""
        patch = {"version": 1, "ops": ops}
        result = self.store.apply_patches(
            self.project_id, [patch], session_id=self.session_id, script_hash=label,
            client_op_id=client_op_id,
        )
        if self.on_patch is not None and not result.get("duplicate"):
            timeline = (result.get("project_state") or {}).get("timeline") or {}
            info = {
                "project_id": self.project_id,
                "seq": result.get("patch_seq_end"),
                "ops": [str(op.get("op") or "") for op in ops if isinstance(op, dict)],
                "label": label,
                "state_scope": _patch_state_scope(ops),
                "duration": timeline.get("duration"),
                "clip_count": len(timeline.get("clips") or []),
            }
            # Surface the last Clip touched by either a timeline or segment
            # operation so completion can return the creator to the actual
            # edit target, not merely to whichever preview was rendered.
            for candidate in reversed(ops):
                if not isinstance(candidate, dict):
                    continue
                touched_clip_id = str(candidate.get("clip_id") or "")
                if not touched_clip_id and isinstance(candidate.get("data"), dict):
                    raw_clip = candidate["data"].get("clip")
                    if isinstance(raw_clip, dict):
                        touched_clip_id = str(raw_clip.get("id") or "")
                if touched_clip_id:
                    info["clip_id"] = touched_clip_id
                    break
            segment_ops = [op for op in ops if isinstance(op, dict) and str(op.get("op") or "") == "segment_edit"]
            if segment_ops:
                seg_op = segment_ops[-1]
                clip_id = str(seg_op.get("clip_id") or "")
                clip = next((c for c in timeline.get("clips") or [] if str(c.get("id") or "") == clip_id), {})
                seg_id = str(clip.get("segment_ref") or seg_op.get("segment_id") or "")
                segment = (result.get("project_state") or {}).get("segments", {}).get(seg_id, {})
                info.update({
                    "clip_id": clip_id,
                    "segment_id": seg_id,
                    "segment_revision": int(segment.get("revision") or 0),
                    "reservations": segment.get("reservations") if isinstance(segment, dict) else {},
                    "handback_required": bool(segment.get("handback_required")) if isinstance(segment, dict) else False,
                })
            try:
                self.on_patch(info)
            except Exception:
                pass  # an SSE hiccup must never fail a timeline mutation
        return result

    def undo(self, steps: int = 1) -> dict[str, Any]:
        """Rewind the last ``steps`` patches (verb calls)."""
        if steps < 1:
            raise ProjectStoreError(f"undo steps must be >= 1, got {steps}")
        meta = self.store.load_meta(self.project_id)
        current = int(meta.get("patch_seq") or 0)
        return self.store.undo_to_seq(self.project_id, max(0, current - steps))

    def redo(self, steps: int = 1) -> dict[str, Any]:
        """Restore the next ``steps`` patches from the current undo branch."""
        return self.store.redo(self.project_id, steps)

    def inspect(self, *, history: int = 0) -> dict[str, Any]:
        from gemia.project_inspect import inspect_project  # local: avoids import cycle

        return inspect_project(self.store, self.project_id, history=history)

    def compact_text(self) -> str:
        """Prompt-ready one-screen timeline summary; degrades, never raises."""
        from gemia.project_inspect import inspect_project, render_text  # local: avoids import cycle

        try:
            return render_text(inspect_project(self.store, self.project_id)).rstrip("\n")
        except Exception as exc:
            return f"(timeline unavailable: {exc})"

    def segment_compact_text(self) -> str:
        """Prompt-ready segment structure/revision/reservation digest.

        Flat legacy clips are represented as implicit single material/layer/state
        documents without writing them. The digest is rebuilt at each model
        boundary so an Agent cannot rely on a stale reservation snapshot.
        """
        try:
            from gemia.segment_document import persisted_segment
            project = self.load()
            rows: list[str] = []
            for clip in (project.get("timeline", {}).get("clips") or []):
                if not isinstance(clip, dict):
                    continue
                doc = persisted_segment(project, clip, create=False)
                reservations = doc.get("reservations") or {}
                reserved = ",".join(f"{key}:{value.get('owner')}" for key, value in reservations.items() if isinstance(value, dict)) or "none"
                layers = ",".join(f"{item.get('id')}@r{item.get('revision', 0)}" for item in doc.get("layers") or [] if isinstance(item, dict))
                states = ",".join(f"{item.get('id')}@r{item.get('revision', 0)}" for item in doc.get("states") or [] if isinstance(item, dict))
                rows.append(
                    f"clip={clip.get('id')} segment={doc.get('segment_id')} persisted={bool(doc.get('persisted'))} "
                    f"segment_revision={doc.get('revision', 0)} layers=[{layers}] states=[{states}] "
                    f"reserved=[{reserved}] handback_required={bool(doc.get('handback_required'))}"
                )
            return "SegmentDocuments:\n" + ("\n".join(rows) if rows else "(none)")
        except Exception as exc:
            return f"(segments unavailable: {exc})"
