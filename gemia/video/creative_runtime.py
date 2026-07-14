"""Creative-runtime primitives for Lumeri planner autonomy."""
from __future__ import annotations

import json
import shutil
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


def write_development_patch_brief(
    input_path: str,
    output_path: str,
    *,
    feature_request: str,
    reason: str = "",
    suggested_files: list[str] | None = None,
    proposed_primitives: list[str] | None = None,
    safety_notes: list[str] | None = None,
) -> str:
    """Write a developer patch brief when the requested edit needs new source code.

    This primitive is intentionally a bridge, not an automatic source mutator:
    it records exactly what capability is missing, which files/primitives should
    be touched, and what safety boundary applies. The visible media output is
    preserved by copying the input video to output_path when possible.
    """
    source = Path(input_path).expanduser().resolve()
    output = Path(output_path).expanduser().resolve()
    output.parent.mkdir(parents=True, exist_ok=True)

    if source.exists() and source.is_file() and source != output:
        shutil.copyfile(source, output)

    brief_path = output.with_suffix(".lumeri-dev-brief.json")
    md_path = output.with_suffix(".lumeri-dev-brief.md")
    payload: dict[str, Any] = {
        "schema_version": 1,
        "kind": "lumeri_development_patch_brief",
        "created_at": datetime.now(timezone.utc).isoformat(),
        "source_path": str(source),
        "output_path": str(output),
        "feature_request": str(feature_request or "").strip(),
        "reason": str(reason or "").strip(),
        "suggested_files": [str(item) for item in (suggested_files or [])],
        "proposed_primitives": [str(item) for item in (proposed_primitives or [])],
        "safety_notes": [str(item) for item in (safety_notes or [])],
        "next_executor": "Codex/Claude Code developer lane",
        "source_mutation_applied": False,
    }
    brief_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    md_lines = [
        "# Lumeri Development Patch Brief",
        "",
        f"- Feature request: {payload['feature_request'] or '(missing)'}",
        f"- Reason: {payload['reason'] or '(not specified)'}",
        f"- Source mutation applied: {payload['source_mutation_applied']}",
        f"- Next executor: {payload['next_executor']}",
        "",
        "## Suggested Files",
        *([f"- {item}" for item in payload["suggested_files"]] or ["- (not specified)"]),
        "",
        "## Proposed Primitives",
        *([f"- {item}" for item in payload["proposed_primitives"]] or ["- (not specified)"]),
        "",
        "## Safety Notes",
        *([f"- {item}" for item in payload["safety_notes"]] or ["- Do not mutate source automatically without developer-mode confirmation."]),
        "",
        f"JSON sidecar: `{brief_path}`",
    ]
    md_path.write_text("\n".join(md_lines) + "\n", encoding="utf-8")
    return str(output if output.exists() else md_path)


__all__ = ["write_development_patch_brief"]
