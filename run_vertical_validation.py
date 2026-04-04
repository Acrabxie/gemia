#!/usr/bin/env python3
from __future__ import annotations

import argparse
import asyncio
import json
from pathlib import Path

from gemia.ai.ai_client import AIClient
from gemia.orchestrator import Orchestrator


async def run(args: argparse.Namespace) -> dict:
    root = Path(__file__).resolve().parent
    orchestrator = Orchestrator(root_dir=root, ai_client=AIClient())

    first_output = str((root / "outputs" / "vertical_validation_v1.mp4").resolve())
    plan = await orchestrator.ai_client.plan_from_prompt(
        args.prompt,
        input_path=str(Path(args.input).resolve()),
        output_path=first_output,
        context={"target": "before/after preview video"},
    )
    plan_path = root / "plans" / "vertical_validation_plan_v1.json"
    plan_path.parent.mkdir(parents=True, exist_ok=True)
    plan_path.write_text(json.dumps(plan, ensure_ascii=False, indent=2) + "\n")

    task = await orchestrator.run_plan(plan)
    revised = await orchestrator.revise_task(task["task_id"], args.feedback)

    report = {
        "initial_plan_path": str(plan_path.resolve()),
        "task_id": revised["task_id"],
        "task_path": str((root / "tasks" / f"{revised['task_id']}.json").resolve()),
        "current_revision_id": revised["current_revision_id"],
        "revisions": [
            {
                "revision_id": r["revision_id"],
                "feedback": r.get("feedback"),
                "output_path": (r.get("outputs") or [None])[0],
            }
            for r in revised["revisions"]
        ],
    }
    report_path = root / "outputs" / "vertical_validation_report.json"
    report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n")
    return report


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--input", required=True)
    parser.add_argument("--prompt", required=True)
    parser.add_argument("--feedback", required=True)
    args = parser.parse_args()
    result = asyncio.run(run(args))
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
