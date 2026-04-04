"""Skill store — save, list, and load reusable v2 skills.

A v2 skill is a plan template derived from a successful task run.
It strips the concrete input/output paths and preserves the step definitions
so the same pipeline can be applied to any video.

Skills are stored as JSON in ``skills_v2/`` with metadata (name, description,
origin task_id, created_at).

Usage::

    from gemia.skill_store import SkillStore
    store = SkillStore()

    # Save from last run
    store.save_from_task(name="赛博朋克调色")

    # List
    for s in store.list_skills():
        print(s["name"], s["description"])

    # Load and execute
    skill = store.load("赛博朋克调色")
    engine.execute(skill["plan"], input_path, output_path)
"""
from __future__ import annotations

import json
import re
from datetime import datetime
from pathlib import Path
from typing import Any


class SkillStore:
    """Manage reusable v2 skills."""

    def __init__(self, root_dir: str | Path | None = None) -> None:
        this_file = Path(__file__).resolve()
        self.root_dir = Path(root_dir) if root_dir else this_file.parent.parent
        self.skills_dir = self.root_dir / "skills_v2"
        self.tasks_dir = self.root_dir / "tasks"
        self.plans_dir = self.root_dir / "plans"
        self.skills_dir.mkdir(parents=True, exist_ok=True)

    def save_from_task(self, name: str, task_id: str | None = None,
                       description: str | None = None) -> Path:
        """Save a skill from a completed task.

        Args:
            name: Human-readable skill name (e.g. "赛博朋克调色").
            task_id: Task to derive from. ``None`` = most recent succeeded task.
            description: Optional description. Defaults to the plan's goal.

        Returns:
            Path to the saved skill JSON file.
        """
        if task_id is None:
            task_id = self._find_last_succeeded_task()

        task = self._load_json(self.tasks_dir / f"{task_id}.json")
        plan = self._load_plan_for_task(task_id)

        # Build the reusable skill template
        template_steps = _strip_concrete_paths(plan.get("steps", []))

        skill = {
            "name": name,
            "description": description or plan.get("goal", ""),
            "version": "2.0",
            "origin_task_id": task_id,
            "created_at": datetime.now().isoformat(),
            "plan": {
                "version": "2.0",
                "goal": plan.get("goal", name),
                "steps": template_steps,
            },
        }

        filename = _slugify(name) + ".json"
        path = self.skills_dir / filename
        # Avoid overwriting — append a counter if needed
        if path.exists():
            i = 1
            while path.exists():
                path = self.skills_dir / f"{_slugify(name)}_{i}.json"
                i += 1

        path.write_text(json.dumps(skill, ensure_ascii=False, indent=2) + "\n")
        return path

    def list_skills(self) -> list[dict[str, Any]]:
        """Return a list of all saved v2 skills (metadata only)."""
        skills = []
        for p in sorted(self.skills_dir.glob("*.json")):
            try:
                data = json.loads(p.read_text())
                skills.append({
                    "name": data.get("name", p.stem),
                    "description": data.get("description", ""),
                    "file": str(p),
                    "origin_task_id": data.get("origin_task_id"),
                    "created_at": data.get("created_at"),
                    "step_count": len(data.get("plan", {}).get("steps", [])),
                })
            except Exception:
                continue
        return skills

    def load(self, name: str) -> dict[str, Any]:
        """Load a skill by name. Searches by exact name match in JSON files."""
        for p in self.skills_dir.glob("*.json"):
            try:
                data = json.loads(p.read_text())
                if data.get("name") == name:
                    return data
            except Exception:
                continue
        # Fallback: try filename match
        slug = _slugify(name)
        path = self.skills_dir / f"{slug}.json"
        if path.exists():
            return json.loads(path.read_text())
        raise FileNotFoundError(f"Skill not found: {name}")

    # ── Internal ───────────────────────────────────────────────────────

    def _find_last_succeeded_task(self) -> str:
        """Find the most recent succeeded v2 task."""
        candidates = []
        for p in self.tasks_dir.glob("*.json"):
            try:
                data = json.loads(p.read_text())
                if data.get("status") == "succeeded" and data.get("version") == "2.0":
                    candidates.append((p.stat().st_mtime, data["task_id"]))
            except Exception:
                continue
        if not candidates:
            raise FileNotFoundError("No succeeded v2 tasks found.")
        candidates.sort(reverse=True)
        return candidates[0][1]

    def _load_plan_for_task(self, task_id: str) -> dict:
        """Load the plan JSON associated with a task."""
        plan_path = self.plans_dir / f"{task_id}_plan.json"
        if not plan_path.exists():
            raise FileNotFoundError(f"Plan not found for task: {task_id}")
        return json.loads(plan_path.read_text())

    def _load_json(self, path: Path) -> dict:
        if not path.exists():
            raise FileNotFoundError(f"Not found: {path}")
        return json.loads(path.read_text())


def _strip_concrete_paths(steps: list[dict]) -> list[dict]:
    """Remove concrete file paths from steps, keeping only $-references and args.

    This makes the plan reusable: $input and $output are re-bound at execution time.
    """
    cleaned = []
    for step in steps:
        s = {
            "id": step["id"],
            "function": step["function"],
            "args": step.get("args", {}),
        }
        # Preserve $-references, drop concrete paths
        inp = step.get("input")
        if isinstance(inp, str) and inp.startswith("$"):
            s["input"] = inp
        out = step.get("output")
        if isinstance(out, str) and out.startswith("$"):
            s["output"] = out
        if step.get("depends_on"):
            s["depends_on"] = step["depends_on"]
        cleaned.append(s)
    return cleaned


def _slugify(text: str) -> str:
    """Convert text to a filesystem-safe slug."""
    # Keep CJK characters, alphanumeric, hyphens, underscores
    cleaned = re.sub(r'[^\w\u4e00-\u9fff\u3040-\u309f\u30a0-\u30ff-]', '_', text)
    cleaned = re.sub(r'_+', '_', cleaned).strip('_')
    return cleaned or "skill"
