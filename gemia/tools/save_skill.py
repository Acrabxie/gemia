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

from gemia.skill_store import (
    DistilledSkillStore,
    SKILL_RECALL_GUIDANCE_STATE_KEY,
    recall_skills as _recall_skills,
    sanitize_skill_recall_guidance_memory,
)
from gemia.tools._context import ToolContext


def _compact_text(value: Any, *, max_len: int) -> str:
    return " ".join(str(value or "").split())[:max_len].strip()


def _skill_names(value: Any, *, max_items: int = 12) -> list[str]:
    if not isinstance(value, (list, tuple)):
        return []
    names: list[str] = []
    seen: set[str] = set()
    for item in value:
        name = _compact_text(item, max_len=120)
        folded = name.casefold()
        if not name or folded in seen:
            continue
        seen.add(folded)
        names.append(name)
        if len(names) >= max_items:
            break
    return names


def _normalized_query(value: str) -> str:
    return " ".join(str(value or "").split()).casefold()


def _audit_revision(value: Any) -> int:
    try:
        return max(0, int(value or 0))
    except (TypeError, ValueError):
        return 0


def _active_recall_guidance(
    args: dict[str, Any],
    ctx: ToolContext,
    *,
    query: str,
) -> dict[str, Any] | None:
    """Apply or update the main model's task-scoped recall audit.

    The memory is scoped to an exact normalized task query.  A different query
    is treated as a different task and does not inherit exclusions silently.
    The model may explicitly replace it with a new audit or reset it.
    """
    normalized_query = _normalized_query(query)
    memory = sanitize_skill_recall_guidance_memory(
        ctx.extra.get(SKILL_RECALL_GUIDANCE_STATE_KEY)
    )
    entries = memory["entries"]
    if bool(args.get("reset_routing_guidance")):
        if normalized_query:
            entries.pop(normalized_query, None)
        else:
            entries.clear()
        memory["last_query"] = normalized_query if normalized_query in entries else ""

    existing = entries.get(normalized_query, {})

    audit = args.get("routing_audit")
    if isinstance(audit, dict) and not any(
        (
            _compact_text(audit.get("failure_evidence"), max_len=1200),
            _compact_text(audit.get("guidance"), max_len=1200),
            _skill_names(audit.get("avoid_skills")),
        )
    ):
        # Some model transports materialize an optional object as an empty
        # schema-shaped value.  That is still "no audit", especially on the
        # first recall for a task; rejecting it makes ordinary skill lookup
        # impossible.
        audit = None
    if audit is not None:
        if not isinstance(audit, dict):
            raise ValueError("routing_audit must be an object")
        if not query:
            raise ValueError("routing_audit requires a non-empty query")
        if existing.get("scope_query") != normalized_query:
            # Some function-calling transports populate the optional audit
            # object on the first recall. There is no previous result to audit
            # yet, so ignore it without persisting exclusions; the lookup below
            # establishes the revision-zero baseline for a later real audit.
            audit = None
    if audit is not None:
        failure_evidence = _compact_text(
            audit.get("failure_evidence"), max_len=1200
        )
        guidance = _compact_text(audit.get("guidance"), max_len=1200)
        avoid_skills = _skill_names(audit.get("avoid_skills"))
        if not failure_evidence:
            raise ValueError(
                "routing_audit requires concrete failure_evidence"
            )
        if not (guidance or avoid_skills):
            raise ValueError(
                "routing_audit requires positive guidance or avoid_skills"
            )
        previous_skills = _skill_names(existing.get("last_result_names"))
        previous_folded = {name.casefold() for name in previous_skills}
        unknown_avoids = [
            name for name in avoid_skills if name.casefold() not in previous_folded
        ]
        if unknown_avoids:
            raise ValueError(
                "routing_audit avoid_skills must name skills from the previous "
                "result: " + ", ".join(unknown_avoids)
            )
        revision = _audit_revision(existing.get("revision")) + 1
        existing = {
            "revision": revision,
            "scope_query": normalized_query,
            "failure_evidence": failure_evidence,
            "guidance": guidance,
            "avoid_skills": avoid_skills,
            "previous_skills": previous_skills,
            "last_result_names": [],
        }
        entries[normalized_query] = existing
        memory["last_query"] = normalized_query

    ctx.extra[SKILL_RECALL_GUIDANCE_STATE_KEY] = (
        sanitize_skill_recall_guidance_memory(memory)
    )
    if existing.get("scope_query") != normalized_query:
        return None
    if _audit_revision(existing.get("revision")) <= 0:
        return None
    return existing


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
        routing_audit: optional structured correction after a failed route.
            Its positive guidance affects ranking and its avoid_skills list
            excludes exact previously returned skill names for this task query.
        reset_routing_guidance: clear the current session's recall audit.

    Returns:
        {"skills": [{name, source, when_to_use, steps, notes, tags}, ...],
         "count": int, "routing": {...}}
    """
    query = str(args.get("query") or args.get("task") or "").strip()
    limit_raw = args.get("limit", 5)
    try:
        limit = int(limit_raw)
    except (TypeError, ValueError):
        limit = 5
    limit = max(1, min(limit, 25))
    include_library = bool(args.get("include_library", True))

    guidance_state = _active_recall_guidance(args, ctx, query=query)
    guidance = (
        str(guidance_state.get("guidance") or "")
        if guidance_state is not None
        else ""
    )
    avoid_skills = (
        _skill_names(guidance_state.get("avoid_skills"))
        if guidance_state is not None
        else []
    )
    skills = _recall_skills(
        query,
        include_library=include_library,
        limit=limit,
        guidance=guidance,
        avoid_skills=avoid_skills,
    )
    names = _skill_names([skill.get("name") for skill in skills])
    # Keep every query's latest result available to the failure-direction
    # nudge.  Audited entries retain their guidance; unaudited entries are
    # plain revision-zero candidates for a later model audit.
    normalized_query = _normalized_query(query)
    memory = sanitize_skill_recall_guidance_memory(
        ctx.extra.get(SKILL_RECALL_GUIDANCE_STATE_KEY)
    )
    entry = dict(memory["entries"].get(normalized_query) or {
        "revision": 0,
        "scope_query": normalized_query,
        "failure_evidence": "",
        "guidance": "",
        "avoid_skills": [],
        "previous_skills": [],
    })
    entry["last_result_names"] = names
    memory["entries"][normalized_query] = entry
    memory["last_query"] = normalized_query
    ctx.extra[SKILL_RECALL_GUIDANCE_STATE_KEY] = (
        sanitize_skill_recall_guidance_memory(memory)
    )

    routing = {
        "audit_revision": _audit_revision(
            (guidance_state or {}).get("revision")
        ),
        "guidance_applied": bool(guidance_state),
        "guidance": guidance,
        "avoided_skills": avoid_skills,
        "previous_skills": _skill_names(
            (guidance_state or {}).get("previous_skills")
        ),
    }
    return {"skills": skills, "count": len(skills), "routing": routing}


__all__ = ["dispatch_save_skill", "dispatch_recall_skills"]
