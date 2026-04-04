#!/usr/bin/env python3
import argparse
import json
from gemia.orchestrator import run_skill, get_task, get_assets


def main() -> int:
    parser = argparse.ArgumentParser(description="Run a Gemia skill JSON with minimal local runtime")
    parser.add_argument("--skill", required=True, help="Skill id, e.g. stylize_preview_v1")
    parser.add_argument("--video", required=True, help="Input video path")
    parser.add_argument("--style", required=True, help="Style prompt")
    parser.add_argument("--json", action="store_true", help="Print task + assets as JSON")
    args = parser.parse_args()

    task_id = run_skill(args.skill, {"video": args.video, "style": args.style})
    task = get_task(task_id)
    assets = get_assets(task_id)
    payload = {"task": task, "assets": assets}

    if args.json:
        print(json.dumps(payload, ensure_ascii=False, indent=2))
    else:
        print(f"task_id: {task_id}")
        for output in task.get("outputs", []):
            print(f"output: {output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
