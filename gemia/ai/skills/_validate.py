from __future__ import annotations

import argparse
import json
from dataclasses import dataclass
from typing import Any

from gemia.ai.prompt_slimming import estimate_tokens
from gemia.ai.skill_router import load_skill_metadata
from gemia.registry import get_registry


@dataclass
class SkillValidationReport:
    ok: bool
    errors: list[str]
    warnings: list[str]
    stats: dict[str, Any]


def validate_skills() -> SkillValidationReport:
    skills = load_skill_metadata(refresh=True)
    errors: list[str] = []
    warnings: list[str] = []
    registry = get_registry()
    primary_owner: dict[str, str] = {}
    secondary_owner: dict[str, str] = {}
    total_tokens = 0

    for skill_id, metadata in sorted(skills.items()):
        if metadata.id != skill_id:
            errors.append(f"{metadata.path}: frontmatter id {metadata.id!r} does not match directory/cache id {skill_id!r}")
        if "何时不用我" not in metadata.description:
            errors.append(f"{skill_id}: description must include 何时不用我")
        if not metadata.primary_triggers:
            errors.append(f"{skill_id}: missing primary triggers")
        if not metadata.primitives:
            errors.append(f"{skill_id}: missing primitives")
        for trigger in metadata.primary_triggers:
            key = trigger.lower()
            if key in primary_owner and primary_owner[key] != skill_id:
                errors.append(f"primary trigger conflict {trigger!r}: {primary_owner[key]} and {skill_id}")
            primary_owner[key] = skill_id
        for trigger in metadata.secondary_triggers:
            key = trigger.lower()
            if key in secondary_owner and secondary_owner[key] != skill_id:
                warnings.append(f"secondary trigger overlap {trigger!r}: {secondary_owner[key]} and {skill_id}")
            secondary_owner[key] = skill_id
        for primitive in metadata.primitives:
            if primitive not in registry:
                errors.append(f"{skill_id}: unknown primitive {primitive}")
        total_tokens += estimate_tokens(metadata.path.read_text(encoding="utf-8"))

    avg_tokens = round(total_tokens / max(len(skills), 1), 1)
    stats = {
        "skill_count": len(skills),
        "index_tokens_est": estimate_tokens("\n".join(m.description for m in skills.values())),
        "avg_skill_md_tokens_est": avg_tokens,
        "total_skill_md_tokens_est": total_tokens,
    }
    return SkillValidationReport(ok=not errors, errors=errors, warnings=warnings, stats=stats)


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(prog="python -m gemia.ai.skills._validate")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)
    report = validate_skills()
    if args.json:
        print(json.dumps({
            "ok": report.ok,
            "errors": report.errors,
            "warnings": report.warnings,
            "stats": report.stats,
        }, ensure_ascii=False, indent=2, sort_keys=True))
    else:
        print(f"ok: {report.ok}")
        print(f"stats: {report.stats}")
        for warning in report.warnings:
            print(f"warning: {warning}")
        for error in report.errors:
            print(f"error: {error}")
    if not report.ok:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
