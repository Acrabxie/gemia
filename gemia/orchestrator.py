from __future__ import annotations

import json
import os
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any

from .project_store import ProjectStore
from lumerai.patches import apply_timeline_patches
from lumerai.sandbox import execute_script


class GemiaOrchestrator:
    """Project-dir context + Lumeri runtime-script runner.

    The legacy plan/skill executor generations were retired on 2026-07-06;
    what remains is the `lumerai-script` path (`plan_from_script`) and the
    persistent `ProjectStore` used by the lumerai-* CLI commands.
    """

    def __init__(self, root_dir: str | Path | None = None) -> None:
        this_file = Path(__file__).resolve()
        self.root_dir = Path(root_dir) if root_dir else this_file.parent.parent
        self.tasks_dir = self.root_dir / "tasks"
        self.skills_dir = self.root_dir / "skills"
        self.plans_dir = self.root_dir / "plans"
        self.outputs_dir = self.root_dir / "outputs"
        self.frames_dir = self.root_dir / "frames"
        self.styled_dir = self.root_dir / "styled"
        self.temp_dir = self.root_dir / "temp"
        self.projects_dir = self.root_dir / "projects"
        self.workspaces_dir = self.root_dir / "workspaces"
        for p in [self.tasks_dir, self.skills_dir, self.plans_dir, self.outputs_dir, self.frames_dir, self.styled_dir, self.temp_dir, self.projects_dir, self.workspaces_dir]:
            p.mkdir(parents=True, exist_ok=True)
        self.project_store = ProjectStore(self.projects_dir)

    def plan_from_script(
        self,
        script: str,
        *,
        project_state: dict[str, Any] | None = None,
        project_id: str | None = None,
        session_id: str,
        ai_model: str = "unknown",
        timeout_sec: int = 30,
        dry_run: bool = False,
    ) -> dict[str, Any]:
        """Execute a Lumeri runtime script and apply its TimelinePatch output.

        Two modes:

        - **inline** (legacy): caller passes ``project_state``; patches are
          applied in-memory and the resulting state is embedded in the task.
        - **store** (new): caller passes ``project_id``; ``project_state`` must
          be ``None``. The state is loaded from the persistent ``ProjectStore``,
          each patch is appended to ``projects/<id>/patches/``, and the updated
          state.json is rewritten.

        This experimental path is deliberately gated so the existing Plan-v2
        executor remains the default production route.
        """
        if os.environ.get("LUMERAI_SCRIPT_MODE", "0") != "1":
            raise RuntimeError("LUMERAI_SCRIPT_MODE=1 is required for plan_from_script")
        if project_id is not None and project_state is not None:
            raise ValueError("plan_from_script: pass either project_id or project_state, not both")
        if project_id is not None:
            current_state = self.project_store.load(project_id)
        else:
            current_state = project_state or {}
        result = execute_script(
            script,
            project_state=current_state,
            output_dir=self.outputs_dir,
            project_root=self.root_dir,
            workspace_dir=self.workspaces_dir / session_id,
            session_id=session_id,
            ai_model=ai_model,
            timeout_sec=timeout_sec,
            dry_run=dry_run,
        )
        if dry_run:
            return {
                "status": "succeeded" if result.ok else "failed",
                "dry_run": True,
                "script_hash": result.script_hash,
                "stderr": result.stderr,
                "error": result.error,
            }
        if not result.ok:
            task_id = self._new_task_id()
            task = {
                "task_id": task_id,
                "status": "failed",
                "engine": "lumerai_script",
                "script_hash": result.script_hash,
                "error": result.error,
                "stderr": result.stderr,
                "created_at": datetime.now().isoformat(),
            }
            if project_id is not None:
                task["project_id"] = project_id
            self._save_task(task)
            return task
        if not result.patches:
            task_id = self._new_task_id()
            task = {
                "task_id": task_id,
                "status": "failed",
                "engine": "lumerai_script",
                "script_hash": result.script_hash,
                "error_code": "no_timeline_patches",
                "error": "Script executed but emitted no TimelinePatch operations.",
                "stdout": result.stdout,
                "stderr": result.stderr,
                "created_at": datetime.now().isoformat(),
            }
            if project_id is not None:
                task["project_id"] = project_id
            self._save_task(task)
            return task
        if project_id is not None:
            store_result = self.project_store.apply_patches(
                project_id,
                result.patches,
                session_id=session_id,
                script_hash=result.script_hash,
            )
            updated_project = store_result["project_state"]
            patch_seq_start = store_result["patch_seq_start"]
            patch_seq_end = store_result["patch_seq_end"]
            patch_files = store_result["patch_files"]
        else:
            updated_project = apply_timeline_patches(current_state, result.patches)
            patch_seq_start = 0
            patch_seq_end = 0
            patch_files = []
        task_id = self._new_task_id()
        task = {
            "task_id": task_id,
            "status": "succeeded",
            "engine": "lumerai_script",
            "script_hash": result.script_hash,
            "timeline_patches": result.patches,
            "project_state": updated_project,
            "stdout": result.stdout,
            "stderr": result.stderr,
            "created_at": datetime.now().isoformat(),
        }
        if project_id is not None:
            task["project_id"] = project_id
            task["patch_seq_start"] = patch_seq_start
            task["patch_seq_end"] = patch_seq_end
            task["patch_files"] = patch_files
        self._save_task(task)
        return task

    def get_task(self, task_id: str) -> dict:
        path = self.tasks_dir / f"{task_id}.json"
        return json.loads(path.read_text())

    def _new_task_id(self) -> str:
        return f"task_{datetime.now().strftime('%Y%m%d_%H%M%S')}_{uuid.uuid4().hex[:8]}"

    def _save_task(self, task: dict[str, Any]) -> None:
        path = self.tasks_dir / f"{task['task_id']}.json"
        path.write_text(json.dumps(task, ensure_ascii=False, indent=2) + "\n")
