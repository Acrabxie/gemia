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
import os
import re
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from gemia import lus as _lus


def distilled_skills_dir() -> Path:
    """Return the directory where DISTILLED (user-authored) skills are stored.

    Resolution order:
    1. ``GEMIA_SKILL_STORE_DIR`` environment variable (used by tests via
       monkeypatch to redirect into a tmp dir).
    2. ``~/.gemia/skills`` — the durable per-user store.

    The directory is created on demand by callers that write into it.
    """
    override = os.environ.get("GEMIA_SKILL_STORE_DIR")
    if override:
        return Path(override).expanduser()
    return Path.home() / ".gemia" / "skills"


def _distill_slug(text: str) -> str:
    """Slug for a LEGACY distilled-skill JSON filename (keeps CJK).

    Only used to read/locate unmigrated ``<slug>.json`` files; new writes use
    the ``.lus`` machine name from :func:`gemia.lus.derive_name` (spec D2).
    """
    return _slugify(text)


# ── .lus mapping helpers (docs/lus-skill-format.md §7.1) ───────────────────


_STEP_PREFIX_RE = re.compile(r"^([0-9]+)\.\s+(.*)$")
_SEMVER_CORE_RE = re.compile(r"^(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)$")


def _known_tool_names() -> frozenset[str] | None:
    """``TOOL_NAMES`` as a frozenset, or ``None`` if the tools package is
    unavailable (keeps the store importable/testable standalone)."""
    try:
        from gemia.tools._schema import TOOL_NAMES
        return frozenset(TOOL_NAMES)
    except Exception:
        return None


def _plan_blocked_tools() -> frozenset[str]:
    try:
        from gemia.plan_mode import PLAN_BLOCKED_TOOLS
        return PLAN_BLOCKED_TOOLS
    except Exception:
        return frozenset()


def _single_line(text: Any, max_len: int) -> str:
    return " ".join(str(text or "").split())[:max_len].strip()


def _derive_triggers(tags: Any, title: str) -> list[str]:
    """§7.1: ``tags`` → ``triggers`` (deduplicated, capped at 16); the title
    is the fallback when no tags were given (``triggers`` is REQUIRED)."""
    source = [str(t) for t in (tags or []) if str(t).strip()] or [title]
    out: list[str] = []
    seen: set[str] = set()
    for tag in source:
        item = _single_line(tag, 64)
        if not item or item.lower() in seen:
            continue
        seen.add(item.lower())
        out.append(item)
        if len(out) == 16:
            break
    return out or [_single_line(title, 64)]


def _numbered_steps(steps_list: list[str]) -> list[str]:
    """Number the coerced steps; already-numbered strings are not
    double-numbered (§7.1, reusing the :func:`_coerce_steps` output)."""
    numbered: list[str] = []
    for index, step in enumerate(steps_list, 1):
        line = " ".join(str(step).split())
        if _STEP_PREFIX_RE.match(line):
            numbered.append(line)
        else:
            numbered.append(f"{index}. {line}")
    return numbered


def _bump_patch(version: str) -> str:
    match = _SEMVER_CORE_RE.match(str(version or ""))
    if not match:
        return "1.0.0"
    return f"{match.group(1)}.{match.group(2)}.{int(match.group(3)) + 1}"


def _build_lus_document(
    *,
    title: str,
    when_to_use: Any = "",
    steps: Any = None,
    notes: Any = "",
    tags: Any = None,
    version: str = "1.0.0",
    created_at: str,
    updated_at: str,
    domain: str = "video",
) -> tuple[str, "_lus.LusMeta", str, list[str], list["_lus.LusWarning"]]:
    """Map save_skill/legacy-JSON args onto a validated .lus document (§7.1).

    Returns ``(text, meta, body, step_list, warnings)``. Raises
    :class:`gemia.lus.LusValidationError` when the content violates the
    format (secrets, absolute paths, empty steps, oversize, ...) — callers
    surface the error, never swallow it (§7.1 step 1).
    """
    clean_title = _single_line(title, 80)
    when_text = str(when_to_use or "").strip()
    notes_text = str(notes or "").strip()
    steps_list = _coerce_steps(steps)
    description = _single_line(when_text, 500) or _single_line(clean_title, 500)
    numbered = _numbered_steps(steps_list)

    when_section = when_text or description
    parts = ["## When to use\n" + when_section, "## Steps\n" + "\n".join(numbered)]
    if notes_text:
        parts.append("## Pitfalls\n" + notes_text)
    body = "\n\n".join(parts)

    known_tools = _known_tool_names()
    tools_used = _lus.extract_tools_used("\n".join(numbered), known_tools or frozenset())
    meta = _lus.LusMeta(
        name=_lus.derive_name(clean_title),
        version=version,
        lus_version=1,
        title=clean_title,
        description=description,
        triggers=tuple(_derive_triggers(tags, clean_title)),
        domain=domain,
        tools_used=tuple(tools_used),
        parameters={"type": "object", "properties": {}},
        author="lumeri-agent",
        created_at=created_at,
        updated_at=updated_at,
        language=_lus.detect_language(body),
        safety_requires_paid_generation=bool(set(tools_used) & _lus.PAID_GENERATION_TOOLS),
        safety_mutates_project=bool(set(tools_used) & _plan_blocked_tools()),
        checksum=None,
        extra={},
    )
    text = _lus.serialize_lus(meta, body)
    validated, validated_body, warnings = _lus.validate_lus(text, known_tools=known_tools)
    return text, validated, validated_body, steps_list, warnings


def _meta_content_key(meta: "_lus.LusMeta", body: str) -> tuple:
    """Change-detection key: everything except version/updated_at/checksum
    (§7.1 step 2)."""
    return (
        meta.name, meta.title, meta.description, meta.triggers, meta.domain,
        meta.tools_used, json.dumps(meta.parameters, sort_keys=True, ensure_ascii=False),
        meta.author, meta.created_at, meta.language,
        meta.safety_requires_paid_generation, meta.safety_mutates_project,
        "\n" + str(body).strip("\n") + "\n",
    )


def _atomic_write_text(path: Path, text: str) -> None:
    """Temp file in the same directory + ``os.replace`` (§7.1 step 3 — the
    store is shared by concurrent sessions)."""
    fd, tmp_name = tempfile.mkstemp(dir=str(path.parent), prefix=".lus-tmp-")
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="") as handle:
            handle.write(text)
        os.replace(tmp_name, str(path))
    except BaseException:
        try:
            os.unlink(tmp_name)
        except OSError:
            pass
        raise


def _split_body_sections(body: str) -> dict[str, str]:
    """Split a .lus body into ``{'## Heading': content}`` (fence-aware)."""
    sections: dict[str, str] = {}
    current: str | None = None
    buffer: list[str] = []
    in_fence = False
    for line in str(body or "").split("\n"):
        if line.startswith("```"):
            in_fence = not in_fence
            if current is not None:
                buffer.append(line)
            continue
        if not in_fence and line.startswith("## "):
            if current is not None:
                sections[current] = "\n".join(buffer).strip("\n")
            current = line
            buffer = []
            continue
        if current is not None:
            buffer.append(line)
    if current is not None:
        sections[current] = "\n".join(buffer).strip("\n")
    return sections


def _steps_from_body(body: str) -> list[str]:
    """§7.2: the numbered items parsed from ``## Steps`` (numbering stripped,
    continuation lines folded into their step)."""
    section = _split_body_sections(body).get("## Steps", "")
    steps: list[str] = []
    for line in section.split("\n"):
        match = _STEP_PREFIX_RE.match(line)
        if match:
            steps.append(match.group(2).strip())
        elif steps and line.strip():
            steps[-1] = steps[-1] + " " + line.strip()
    return steps


def _lus_record(meta: "_lus.LusMeta", body: str,
                warnings: list["_lus.LusWarning"], path: Path) -> dict[str, Any]:
    """Project a parsed .lus file into the legacy record shape the recall
    pipeline consumes (``_recall_view`` stays unchanged, §7.2)."""
    notes = _split_body_sections(body).get("## Pitfalls", "").strip()
    if any(w.code == "W_LUS_CHECKSUM_STALE" for w in warnings):
        stale = "[warning: checksum stale — body was hand-edited since the last save]"
        notes = f"{notes}\n{stale}" if notes else stale
    return {
        "name": meta.title,
        "machine_name": meta.name,
        "source": "distilled",
        "when_to_use": meta.description,
        "steps": _steps_from_body(body),
        "notes": notes,
        "tags": list(meta.triggers),
        "triggers": list(meta.triggers),
        "version": meta.version,
        "domain": meta.domain,
        "language": meta.language,
        "created_at": meta.created_at,
        "updated_at": meta.updated_at,
        "file": str(path),
    }


def _coerce_steps(steps: Any) -> list[str]:
    """Normalize the ``steps``/``ops``/``recipe`` arg into a list of strings."""
    if steps is None:
        return []
    if isinstance(steps, str):
        text = steps.strip()
        return [text] if text else []
    if isinstance(steps, (list, tuple)):
        out: list[str] = []
        for item in steps:
            if item is None:
                continue
            if isinstance(item, str):
                value = item.strip()
                if value:
                    out.append(value)
            else:
                out.append(json.dumps(item, ensure_ascii=False))
        return out
    return [str(steps)]


class DistilledSkillStore:
    """Durable store for skills DISTILLED ("沉淀") from completed agent tasks.

    Unlike :class:`SkillStore` (which derives plan templates from v2 task
    runs), this store captures a compact human/agent-authored recipe:
    ``{name, when_to_use, steps, notes}``.  One JSON file per skill name,
    so re-distilling the same name UPDATES in place (idempotent, no dups).

    Stored under :func:`distilled_skills_dir` (``~/.gemia/skills`` or the
    ``GEMIA_SKILL_STORE_DIR`` override).
    """

    def __init__(self, root_dir: str | Path | None = None) -> None:
        self.root_dir = Path(root_dir).expanduser() if root_dir else distilled_skills_dir()

    def _ensure_dir(self) -> Path:
        self.root_dir.mkdir(parents=True, exist_ok=True)
        return self.root_dir

    def distill(
        self,
        name: str,
        *,
        when_to_use: str = "",
        steps: Any = None,
        notes: str = "",
        tags: list[str] | None = None,
        version: str | None = None,
    ) -> dict[str, Any]:
        """Distill a completed reusable task into a durable ``.lus`` skill.

        Idempotent by ``name`` (the display title): re-distilling the same
        title hits the same ``<machine-name>.lus`` file, preserving
        ``created_at``, keeping exactly one ``.lus.bak`` generation, and
        patch-bumping ``version`` when the content changed (spec §7.1).

        Raises :class:`gemia.lus.LusValidationError` when the content
        violates the format (secrets, absolute paths, oversize, no steps) —
        nothing is written in that case.

        Returns the stored skill dict (including ``file`` path).
        """
        clean_name = str(name or "").strip()
        if not clean_name:
            raise ValueError("distill requires a non-empty 'name'")

        self._ensure_dir()
        machine = _lus.derive_name(clean_name)
        path = self.root_dir / f"{machine}.lus"

        now = datetime.now(timezone.utc).isoformat()
        created_at = now
        old_meta: _lus.LusMeta | None = None
        old_body: str | None = None
        if path.exists():
            try:
                old_meta, old_body, _old_warnings = _lus.validate_lus(
                    path.read_text(encoding="utf-8"))
                created_at = old_meta.created_at or created_at
            except Exception:
                old_meta = None
                old_body = None
        else:
            # Transition nicety: an unmigrated legacy JSON twin keeps its
            # created_at when the re-save lands as .lus (dual-read, D9).
            legacy = self.root_dir / f"{_distill_slug(clean_name)}.json"
            if legacy.exists():
                try:
                    prior = json.loads(legacy.read_text(encoding="utf-8"))
                    prior_created = str(prior.get("created_at") or "")
                    if datetime.fromisoformat(prior_created).tzinfo is not None:
                        created_at = prior_created
                except Exception:
                    pass

        caller_version = str(version).strip() if version else ""
        caller_version_ok = bool(_SEMVER_CORE_RE.match(caller_version))
        if caller_version_ok:
            new_version = caller_version
        elif old_meta is not None:
            new_version = old_meta.version  # provisional; bumped below on change
        else:
            new_version = "1.0.0"

        text, meta, body, steps_list, warnings = _build_lus_document(
            title=clean_name, when_to_use=when_to_use, steps=steps,
            notes=notes, tags=tags, version=new_version,
            created_at=created_at, updated_at=now,
        )
        if old_meta is not None and old_body is not None and not caller_version_ok:
            if _meta_content_key(meta, body) != _meta_content_key(old_meta, old_body):
                new_version = _bump_patch(old_meta.version)
                text, meta, body, steps_list, warnings = _build_lus_document(
                    title=clean_name, when_to_use=when_to_use, steps=steps,
                    notes=notes, tags=tags, version=new_version,
                    created_at=created_at, updated_at=now,
                )

        if path.exists():
            # Exactly ONE .bak generation, overwritten on each re-save (§7.1).
            bak = self.root_dir / f"{machine}.lus.bak"
            bak.write_bytes(path.read_bytes())
        _atomic_write_text(path, text)

        return {
            "name": clean_name,
            "machine_name": machine,
            "source": "distilled",
            "when_to_use": str(when_to_use or "").strip() or meta.description,
            "steps": steps_list,
            "notes": str(notes or "").strip(),
            "tags": list(meta.triggers),
            "version": meta.version,
            "language": meta.language,
            "created_at": meta.created_at,
            "updated_at": meta.updated_at,
            "file": str(path),
            "warnings": [f"{w.code}: {w.message}" for w in warnings],
        }

    def list_distilled(self) -> list[dict[str, Any]]:
        """Return all distilled skills — dual-read (D9): ``*.lus`` first,
        then legacy ``*.json`` for names not yet migrated; on collision the
        ``.lus`` wins.  Dotfile/AppleDouble sidecars are ignored."""
        if not self.root_dir.exists():
            return []
        skills: list[dict[str, Any]] = []
        lus_titles: set[str] = set()
        lus_machine: set[str] = set()
        for p in sorted(self.root_dir.glob("*.lus")):
            if p.name.startswith("."):
                continue
            try:
                meta, body, warnings = _lus.validate_lus(
                    p.read_text(encoding="utf-8"), filename=p.name)
            except Exception:
                continue
            skills.append(_lus_record(meta, body, warnings, p))
            lus_titles.add(meta.title)
            lus_machine.add(meta.name)
        skills.extend(self._legacy_distilled(lus_titles, lus_machine))
        return skills

    def _legacy_distilled(
        self,
        exclude_titles: set[str] = frozenset(),
        exclude_machine: set[str] = frozenset(),
    ) -> list[dict[str, Any]]:
        """Unmigrated legacy ``*.json`` records, minus ``.lus`` collisions."""
        if not self.root_dir.exists():
            return []
        out: list[dict[str, Any]] = []
        for p in sorted(self.root_dir.glob("*.json")):
            if p.name.startswith("."):
                continue
            try:
                data = json.loads(p.read_text(encoding="utf-8"))
            except Exception:
                continue
            if not isinstance(data, dict):
                continue
            legacy_name = str(data.get("name") or "")
            if legacy_name in exclude_titles or _lus.derive_name(legacy_name) in exclude_machine:
                continue  # D9: same name in both formats → .lus wins
            data.setdefault("source", "distilled")
            data["file"] = str(p)
            out.append(data)
        return out

    def scan_distilled_metadata(self) -> list[dict[str, Any]]:
        """§7.2 cheap scan: metadata-only ``.lus`` candidates for ranking.

        Reads at most the first 8 KiB per file (the meta block is guaranteed
        to fit, D6); bodies are NOT read — the recall pipeline loads bodies
        for the top-``limit`` winners only.  Malformed files are skipped.
        """
        if not self.root_dir.exists():
            return []
        out: list[dict[str, Any]] = []
        for p in sorted(self.root_dir.glob("*.lus")):
            if p.name.startswith("."):
                continue
            try:
                with p.open("rb") as handle:
                    head = handle.read(_lus.META_MAX_BYTES)
                meta = _lus.scan_lus_meta(head)
            except Exception:
                continue
            out.append({
                "name": meta.title,
                "machine_name": meta.name,
                "source": "distilled",
                "when_to_use": meta.description,
                "steps": [],
                "notes": "",
                "tags": list(meta.triggers),
                "triggers": list(meta.triggers),
                "version": meta.version,
                "domain": meta.domain,
                "language": meta.language,
                "created_at": meta.created_at,
                "updated_at": meta.updated_at,
                "_lus_path": str(p),
            })
        return out

    def load(self, name: str) -> dict[str, Any]:
        """Load a distilled skill by title, machine name, or filename stem."""
        target = str(name or "").strip()
        for data in self.list_distilled():
            if data.get("name") == target or data.get("machine_name") == target:
                return data
        path = self.root_dir / f"{_lus.derive_name(target)}.lus"
        if path.exists():
            meta, body, warnings = _lus.validate_lus(
                path.read_text(encoding="utf-8"), filename=path.name)
            return _lus_record(meta, body, warnings, path)
        legacy = self.root_dir / f"{_distill_slug(target)}.json"
        if legacy.exists():
            data = json.loads(legacy.read_text(encoding="utf-8"))
            data["file"] = str(legacy)
            return data
        raise FileNotFoundError(f"Distilled skill not found: {name}")


def _library_skills() -> list[dict[str, Any]]:
    """Return the static library skills as recall-shaped dicts.

    Sourced from :func:`gemia.ai.skill_router.load_skill_metadata` so recall
    can surface built-in skills alongside user-distilled ones.  Import is
    done lazily and defensively so the store stays usable even if the AI
    package or its YAML deps are unavailable.
    """
    try:
        from gemia.ai.skill_router import load_skill_metadata
    except Exception:
        return []
    try:
        metadata = load_skill_metadata()
    except Exception:
        return []
    out: list[dict[str, Any]] = []
    for meta in metadata.values():
        triggers = list(meta.primary_triggers) + list(meta.secondary_triggers)
        out.append({
            "name": meta.id,
            "source": "library",
            "when_to_use": meta.description,
            "steps": list(meta.primitives),
            "notes": "",
            "tags": list(meta.primary_triggers),
            "triggers": triggers,
        })
    return out


def _relevance(skill: dict[str, Any], query_terms: list[str], query: str) -> float:
    """Score a skill against a lowercased query (substring + token overlap).

    For ``.lus`` candidates the §7.2 remap holds: ``name`` carries the title,
    ``machine_name`` the kebab-case key (both 3.0), ``when_to_use`` the
    description (2.0), ``tags``/``triggers`` the trigger phrases (2.0);
    metadata-only candidates have empty ``steps``/``notes`` so those 1.0
    haystacks only participate for legacy/library skills.
    """
    haystacks: list[tuple[str, float]] = [
        (str(skill.get("name", "")), 3.0),
        (str(skill.get("machine_name", "")), 3.0),
        (str(skill.get("when_to_use", "")), 2.0),
        (" ".join(str(t) for t in skill.get("tags", []) or []), 2.0),
        (" ".join(str(t) for t in skill.get("triggers", []) or []), 2.0),
        (" ".join(str(s) for s in skill.get("steps", []) or []), 1.0),
        (str(skill.get("notes", "")), 1.0),
    ]
    score = 0.0
    for text, weight in haystacks:
        lowered = text.lower()
        if not lowered:
            continue
        if query and query in lowered:
            score += weight * 2.0
        for term in query_terms:
            if term and term in lowered:
                score += weight
    return score


def recall_skills(
    query: str,
    *,
    store: "DistilledSkillStore | None" = None,
    include_library: bool = True,
    limit: int = 5,
) -> list[dict[str, Any]]:
    """Recall the most relevant saved/library skills for ``query``.

    Searches BOTH user-distilled skills (from :class:`DistilledSkillStore`)
    and the static skill library, ranks by relevance, and returns up to
    ``limit`` skills (name + when_to_use + steps + source).  When the query
    is empty, returns the most recent distilled skills first.

    Per spec §7.2, ``.lus`` skills are ranked on a cheap metadata-only scan
    (name/title, description, triggers); bodies are loaded only for the
    winning candidates.  Legacy ``*.json`` skills still participate (D9,
    ``.lus`` wins on name collision) and a corrupt file never makes recall
    throw — it is skipped.
    """
    store = store or DistilledSkillStore()
    lus_candidates = store.scan_distilled_metadata()
    candidates: list[dict[str, Any]] = list(lus_candidates)
    candidates.extend(store._legacy_distilled(
        {c["name"] for c in lus_candidates},
        {c["machine_name"] for c in lus_candidates},
    ))
    if include_library:
        candidates.extend(_library_skills())

    query = str(query or "").strip().lower()
    query_terms = [t for t in re.split(r"[\s,，。、/]+", query) if t]

    if not query:
        # No query: prefer freshly distilled skills, then library order.
        distilled = [s for s in candidates if s.get("source") == "distilled"]
        distilled.sort(key=lambda s: str(s.get("updated_at") or ""), reverse=True)
        others = [s for s in candidates if s.get("source") != "distilled"]
        ranked = distilled + others
    else:
        scored: list[tuple[float, int, dict[str, Any]]] = []
        for idx, skill in enumerate(candidates):
            score = _relevance(skill, query_terms, query)
            if score > 0:
                scored.append((score, idx, skill))
        # Highest score first; stable on original order for ties.
        scored.sort(key=lambda item: (-item[0], item[1]))
        ranked = [item[2] for item in scored]

    results: list[dict[str, Any]] = []
    for skill in ranked:
        if len(results) >= limit:
            break
        view = _materialize_recall_view(skill)
        if view is not None:
            results.append(view)
    return results


def _materialize_recall_view(skill: dict[str, Any]) -> dict[str, Any] | None:
    """Body-on-selection (§7.2): winners backed by a ``.lus`` file get a full
    validated read; anything else projects directly.  Returns ``None`` when
    the file went corrupt/missing since the scan (skip, never throw)."""
    lus_path = skill.get("_lus_path")
    if not lus_path:
        return _recall_view(skill)
    path = Path(lus_path)
    try:
        meta, body, warnings = _lus.validate_lus(
            path.read_text(encoding="utf-8"), filename=path.name)
    except Exception:
        return None
    return _recall_view(_lus_record(meta, body, warnings, path))


def _recall_view(skill: dict[str, Any]) -> dict[str, Any]:
    """Project a stored skill into a compact recall result.

    This is the MODEL-FACING contract of ``recall_skills`` — the key set
    must not change (spec §7.2; pinned by tests/test_skill_distill.py).
    """
    return {
        "name": skill.get("name", ""),
        "source": skill.get("source", "distilled"),
        "when_to_use": skill.get("when_to_use", ""),
        "steps": list(skill.get("steps", []) or []),
        "notes": skill.get("notes", ""),
        "tags": list(skill.get("tags", []) or []),
    }


# ── One-shot migration: legacy distilled JSON → .lus (spec §8) ─────────────


def _carry_timestamps(data: dict[str, Any], now: str) -> tuple[str, str, str]:
    """§8.2: carry legacy timestamps verbatim when parseable (tz-aware and
    ordered), else set both to migration time and note it in the report."""
    def _parse(value: Any) -> datetime | None:
        try:
            parsed = datetime.fromisoformat(str(value))
        except Exception:
            return None
        return parsed if parsed.tzinfo is not None else None

    created_dt = _parse(data.get("created_at"))
    updated_dt = _parse(data.get("updated_at"))
    if created_dt is not None and updated_dt is not None and updated_dt >= created_dt:
        return str(data["created_at"]), str(data["updated_at"]), ""
    return now, now, "timestamps unparseable; set to migration time"


def migrate_distilled_to_lus(
    root: str | Path | None = None,
    *,
    dry_run: bool = False,
) -> list[dict[str, Any]]:
    """Migrate every legacy distilled-skill JSON in the store root to .lus.

    Behavior per docs/lus-skill-format.md §8.3:

    * originals become ``<original-basename>.json.bak`` (never deleted);
    * skipped when the derived ``<name>.lus`` or the ``.json.bak`` sibling
      already exists — running twice produces a byte-identical tree;
    * files whose content violates §4.2 (secrets, absolute paths, ...) are
      skipped, reported, and left un-renamed for manual review;
    * derived-name collisions abort THAT file; the run continues.

    Returns a per-file report: ``{"file", "status", "detail"}`` with status
    ``migrated | skipped(already) | skipped(violation: E_…) | conflict(name)``
    (plus ``migrated(dry-run)`` when ``dry_run=True``, which writes nothing).
    """
    root_dir = Path(root).expanduser() if root else distilled_skills_dir()
    report: list[dict[str, Any]] = []
    if not root_dir.exists():
        return report
    # Names claimed by files migrated in THIS run (§8.2: two titles deriving
    # the same machine name is a conflict; a pre-existing .lus is merely
    # "already migrated"). Dry runs claim too, so the plan matches reality.
    run_claims: set[str] = set()
    now = datetime.now(timezone.utc).isoformat()
    for p in sorted(root_dir.glob("*.json")):
        if p.name.startswith("."):
            continue
        entry: dict[str, Any] = {"file": p.name, "status": "", "detail": ""}
        report.append(entry)
        try:
            data = json.loads(p.read_text(encoding="utf-8"))
            if not isinstance(data, dict):
                raise ValueError("not a JSON object")
        except Exception as exc:
            entry["status"] = "skipped(violation: invalid-json)"
            entry["detail"] = str(exc)
            continue
        title = str(data.get("name") or "").strip() or p.stem
        machine = _lus.derive_name(title)
        lus_path = root_dir / f"{machine}.lus"
        bak_path = p.with_name(p.name + ".bak")
        if machine in run_claims:
            entry["status"] = "conflict(name)"
            entry["detail"] = f"derived name '{machine}' already claimed in this run"
            continue
        if lus_path.exists() or bak_path.exists():
            entry["status"] = "skipped(already)"
            continue
        created_at, updated_at, ts_note = _carry_timestamps(data, now)
        try:
            text, _meta, _body, _steps, _warnings = _build_lus_document(
                title=title,
                when_to_use=data.get("when_to_use") or "",
                steps=data.get("steps"),
                notes=data.get("notes") or "",
                tags=data.get("tags"),
                version="1.0.0",
                created_at=created_at,
                updated_at=updated_at,
            )
        except _lus.LusValidationError as exc:
            entry["status"] = f"skipped(violation: {exc.code})"
            entry["detail"] = exc.message
            continue
        run_claims.add(machine)
        if dry_run:
            entry["status"] = "migrated(dry-run)"
            entry["detail"] = f"would write {lus_path.name}; original → {bak_path.name}" + (
                f"; {ts_note}" if ts_note else "")
            continue
        _atomic_write_text(lus_path, text)
        os.replace(str(p), str(bak_path))
        entry["status"] = "migrated"
        entry["detail"] = f"→ {lus_path.name}; original → {bak_path.name}" + (
            f"; {ts_note}" if ts_note else "")
    return report


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

        # Extract models used from task metadata
        models_used: list[str] = task.get("models_used", [])

        # Extract parameterizable values from plan steps
        parameters = _extract_parameters(template_steps)

        skill = {
            "name": name,
            "description": description or plan.get("goal", ""),
            "version": "2.0",
            "origin_task_id": task_id,
            "created_at": datetime.now().isoformat(),
            "models_used": models_used,
            "parameters": parameters,
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
                    "models_used": data.get("models_used", []),
                })
            except Exception:
                continue
        return skills

    def apply_parameters(self, skill_data: dict, overrides: dict) -> dict:
        """Apply parameter overrides to a skill's plan.

        Returns a deep copy of the plan with the specified parameter values
        replaced.  The plan itself is not mutated.

        Args:
            skill_data: Loaded skill dict (as returned by :meth:`load`).
            overrides: Dict mapping ``"step_id.arg"`` to the new value, e.g.
                ``{"step_1.preset": "vintage", "step_2.style_prompt": "watercolor"}``.

        Returns:
            Modified plan dict with overrides applied.  The ``"steps"``
            list entries have their ``"args"`` dicts updated in-place on
            copies — the original ``skill_data`` is not modified.
        """
        import copy
        plan = copy.deepcopy(skill_data.get("plan", {}))
        steps = plan.get("steps", [])

        # Build a lookup from step_id → step dict for O(1) access
        step_by_id: dict[str, dict] = {s["id"]: s for s in steps}

        for key, value in overrides.items():
            if "." not in key:
                continue
            step_id, arg = key.split(".", 1)
            if step_id in step_by_id:
                step_by_id[step_id].setdefault("args", {})[arg] = value

        return plan

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


def _extract_parameters(steps: list[dict]) -> list[dict]:
    """Extract parameterizable values from plan step args.

    Iterates over all steps and their ``args`` dicts.  For each arg whose
    value is a JSON-primitive (``str``, ``int``, or ``float``) and is **not**
    a file path (does not contain ``/`` or ``\\``), a parameter entry is
    created.

    Args:
        steps: List of step dicts from a plan (after path stripping).

    Returns:
        List of parameter dicts with keys:
        - ``step_id``: ID of the owning step.
        - ``arg``: Argument name.
        - ``type``: ``"str"``, ``"int"``, or ``"float"``.
        - ``current_value``: Current value of the argument.
        - ``description``: Human-readable label.
    """
    params: list[dict] = []
    for step in steps:
        step_id = step.get("id", "")
        args = step.get("args", {})
        for arg_name, value in args.items():
            if not isinstance(value, (str, int, float)):
                continue
            # Skip file paths
            if isinstance(value, str) and ("/" in value or "\\" in value):
                continue
            type_name = type(value).__name__  # "str", "int", or "float"
            params.append({
                "step_id": step_id,
                "arg": arg_name,
                "type": type_name,
                "current_value": value,
                "description": f"{arg_name} for {step_id}",
            })
    return params


def _slugify(text: str) -> str:
    """Convert text to a filesystem-safe slug."""
    # Keep CJK characters, alphanumeric, hyphens, underscores
    cleaned = re.sub(r'[^\w\u4e00-\u9fff\u3040-\u309f\u30a0-\u30ff-]', '_', text)
    cleaned = re.sub(r'_+', '_', cleaned).strip('_')
    return cleaned or "skill"
