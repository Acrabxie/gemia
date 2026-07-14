"""Skill distillation + recall verbs for the v3 agent.

Two responsibilities:

``save_skill`` (``dispatch_save_skill``)
    DISTILL a completed reusable multi-step task into a durable skill so it
    can be reused in later sessions.  A distilled skill captures a compact
    recipe ``{name, when_to_use, steps, notes}`` and is persisted as one
    ``.lus`` file per name (docs/lus-skill-format.md) under
    ``~/.gemia/skills`` via :class:`gemia.skill_store.DistilledSkillStore`.
    The store validates before writing: skills containing secrets, absolute
    user paths, or no steps are rejected with a typed
    :class:`gemia.lus.LusValidationError` and nothing is written.

    For backward compatibility with the v4 build-artifact workflow, when the
    caller supplies a ``source`` (a workspace-relative file to archive), this
    verb delegates to :func:`gemia.tools.build.dispatch_save_skill`, which
    copies the file into the skills dir and writes its metadata.  This keeps
    the existing ``save_skill`` semantics intact while ADDING distillation.

``recall_skills`` (``dispatch_recall_skills``)
    Look up the most relevant saved/library skills for a query/task BEFORE
    working, so the agent can reuse prior know-how.  Searches both the
    user-distilled store AND the static skill library.

Dispatchers must NOT swallow errors; the agent loop wraps each call.
"""
from __future__ import annotations

from pathlib import PurePath
from typing import Any

from gemia.skill_store import DistilledSkillStore, recall_skills as _recall_skills
from gemia.tools._context import ToolContext


def _looks_like_distillation(args: dict[str, Any]) -> bool:
    """True when args carry a distillation recipe rather than a file source."""
    if args.get("source"):
        return False
    for key in ("when_to_use", "trigger", "steps", "ops", "recipe", "notes"):
        if args.get(key):
            return True
    return False


async def dispatch_save_skill(args: dict[str, Any], ctx: ToolContext) -> dict[str, Any]:
    """Distill a reusable task into a durable skill (or archive a build file).

    Distillation args:
        name: required, human-readable skill name (idempotent key).
        when_to_use / trigger: when this skill applies.
        steps / ops / recipe: the reusable step list or compact recipe.
        notes: optional extra guidance / caveats.
        tags: optional list of keyword tags.

    Backward-compat (build artifact) args:
        source: workspace-relative file to archive as a skill (delegates to
                gemia.tools.build.dispatch_save_skill).

    Returns the stored skill dict.
    """
    name = str(args.get("name") or "").strip()
    if not name:
        raise ValueError("save_skill requires a 'name' argument")

    # Backward-compat: a build artifact path → archive via the build verb.
    if args.get("source"):
        from gemia.tools import build as _build

        return await _build.dispatch_save_skill(args, ctx)

    when_to_use = str(args.get("when_to_use") or args.get("trigger") or "").strip()
    steps = args.get("steps")
    if steps is None:
        steps = args.get("ops")
    if steps is None:
        steps = args.get("recipe")
    notes = str(args.get("notes") or "").strip()
    tags = args.get("tags")
    if isinstance(tags, str):
        tags = [tags]

    store = DistilledSkillStore()
    skill = store.distill(
        name,
        when_to_use=when_to_use,
        steps=steps,
        notes=notes,
        tags=list(tags) if isinstance(tags, (list, tuple)) else None,
        version=str(args.get("version") or "").strip() or None,
    )
    summary = (
        f"Distilled skill '{skill['name']}' v{skill['version']} "
        f"({len(skill['steps'])} step(s)) → {PurePath(skill['file']).name} for reuse."
    )
    lus_warnings = skill.get("warnings") or []
    if lus_warnings:
        summary += " Warnings: " + "; ".join(lus_warnings)
    return {
        "skill": skill["name"],
        "source": "distilled",
        "when_to_use": skill["when_to_use"],
        "steps": skill["steps"],
        "notes": skill["notes"],
        "path": skill["file"],
        "summary": summary,
    }


async def dispatch_recall_skills(args: dict[str, Any], ctx: ToolContext) -> dict[str, Any]:
    """Recall the most relevant saved/library skills for a query or task.

    Args:
        query / task: free-text describing the work; matched against skill
            name, when_to_use, tags/triggers, steps, and notes.
        limit: optional max number of skills to return (default 5).
        include_library: optional bool, default True; also search built-in
            library skills (not just user-distilled ones).

    Returns:
        {"skills": [{name, source, when_to_use, steps, notes, tags}, ...],
         "count": int}
    """
    query = str(args.get("query") or args.get("task") or "").strip()
    limit_raw = args.get("limit", 5)
    try:
        limit = int(limit_raw)
    except (TypeError, ValueError):
        limit = 5
    limit = max(1, min(limit, 25))
    include_library = bool(args.get("include_library", True))

    skills = _recall_skills(query, include_library=include_library, limit=limit)
    return {"skills": skills, "count": len(skills)}


__all__ = ["dispatch_save_skill", "dispatch_recall_skills"]
