from __future__ import annotations

import argparse
import asyncio
import json

from .orchestrator import GemiaOrchestrator, get_assets, get_task, run_plan, run_skill


def main() -> None:
    parser = argparse.ArgumentParser(prog="python -m gemia")
    sub = parser.add_subparsers(dest="command", required=True)

    # ── New primary command: run ───────────────────────────────────────
    p_run = sub.add_parser("run", help="AI-driven execution via primitive functions")
    p_run.add_argument("--video", required=True, help="Input video path")
    p_run.add_argument("--prompt", required=True, help="Natural language instruction")
    p_run.add_argument("--output", default=None, help="Output path (auto-generated if omitted)")

    # ── Skill commands ────────────────────────────────────────────────
    p_save_skill = sub.add_parser("save-skill", help="Save last run as a reusable skill")
    p_save_skill.add_argument("--name", required=True, help="Skill name (e.g. '赛博朋克调色')")
    p_save_skill.add_argument("--from-last-run", action="store_true", default=True,
                              help="Use most recent succeeded task (default)")
    p_save_skill.add_argument("--from-task", default=None, help="Specific task ID to save from")
    p_save_skill.add_argument("--description", default=None, help="Optional description")

    p_list_skills = sub.add_parser("list-skills", help="List all saved v2 skills")

    p_run_skill_v2 = sub.add_parser("run-skill-v2", help="Run a saved v2 skill")
    p_run_skill_v2.add_argument("skill_name", help="Skill name")
    p_run_skill_v2.add_argument("--video", required=True, help="Input video path")
    p_run_skill_v2.add_argument("--output", default=None, help="Output path")

    # ── Legacy commands ────────────────────────────────────────────────
    p_run_plan = sub.add_parser("run-plan")
    p_run_plan.add_argument("plan_path")

    p_run_skill = sub.add_parser("run-skill", help="Run a saved skill (v2 or legacy)")
    p_run_skill.add_argument("skill_id")
    p_run_skill.add_argument("--video", required=True)
    p_run_skill.add_argument("--style", default=None, help="Style (legacy only)")
    p_run_skill.add_argument("--output", default=None, help="Output path (v2 only)")

    p_get_task = sub.add_parser("get-task")
    p_get_task.add_argument("task_id")

    p_get_assets = sub.add_parser("get-assets")
    p_get_assets.add_argument("task_id")

    p_plan = sub.add_parser("plan")
    p_plan.add_argument("request")
    p_plan.add_argument("--video", required=True)

    args = parser.parse_args()

    if args.command == "run":
        _cmd_run(args)
    elif args.command == "save-skill":
        _cmd_save_skill(args)
    elif args.command == "list-skills":
        _cmd_list_skills()
    elif args.command == "run-skill-v2":
        _cmd_run_skill_v2(args)
    elif args.command == "run-plan":
        task_id = run_plan(args.plan_path)
        print(task_id)
    elif args.command == "run-skill":
        _cmd_run_skill(args)
    elif args.command == "get-task":
        print(json.dumps(get_task(args.task_id), ensure_ascii=False, indent=2))
    elif args.command == "get-assets":
        print(json.dumps(get_assets(args.task_id), ensure_ascii=False, indent=2))
    elif args.command == "plan":
        print(json.dumps(GemiaOrchestrator().plan_from_prompt(args.request, input_path=args.video), ensure_ascii=False, indent=2))


def _cmd_run(args: argparse.Namespace) -> None:
    """AI-driven execution: prompt → plan v2 → engine → output."""
    from pathlib import Path

    from .ai.ai_client import AIClient
    from .engine import PlanEngine

    engine = PlanEngine()
    output_path = args.output or str(
        (engine.outputs_dir / f"gemia_out_{__import__('uuid').uuid4().hex[:8]}.mp4").resolve()
    )

    print(f"Input:  {args.video}")
    print(f"Prompt: {args.prompt}")
    print(f"Output: {output_path}")
    print()

    # Step 1: Ask AI for a plan
    print("Asking AI for a plan...")
    client = AIClient()
    plan = asyncio.run(client.plan_from_primitives(
        args.prompt,
        input_path=str(Path(args.video).resolve()),
        output_path=output_path,
    ))

    if plan.get("ask"):
        print("\nAI needs more info:")
        for q in plan.get("questions", []):
            print(f"  - {q}")
        print("\n(In interactive mode, answers would be collected here.)")
        return

    # Step 2: Show the plan
    steps = plan.get("steps", [])
    print(f"Plan: {plan.get('goal', '?')} ({len(steps)} step{'s' if len(steps) != 1 else ''})")
    for s in steps:
        print(f"  {s['id']}: {s['function']}({s.get('args', {})})")
    print()

    # Step 3: Execute
    print("Executing...")
    task_id = engine.run_with_task(plan, str(Path(args.video).resolve()), output_path)
    print(f"\nDone! task_id={task_id}")
    print(f"Output: {output_path}")


def _cmd_run_skill(args: argparse.Namespace) -> None:
    """Run a skill — tries v2 first, falls back to legacy."""
    from .skill_store import SkillStore

    store = SkillStore()
    try:
        skill = store.load(args.skill_id)
        # Found a v2 skill — delegate to v2 runner
        args.skill_name = args.skill_id
        _cmd_run_skill_v2(args)
        return
    except FileNotFoundError:
        pass

    # Legacy fallback
    if not args.style:
        raise SystemExit(f"Skill '{args.skill_id}' not found in v2 store, and --style is required for legacy skills.")
    task_id = run_skill(args.skill_id, {"video": args.video, "style": args.style})
    print(task_id)


def _cmd_save_skill(args: argparse.Namespace) -> None:
    """Save a completed task as a reusable v2 skill."""
    from .skill_store import SkillStore

    store = SkillStore()
    task_id = args.from_task  # None means "last succeeded"
    path = store.save_from_task(name=args.name, task_id=task_id, description=args.description)
    skill = json.loads(path.read_text())
    steps = skill.get("plan", {}).get("steps", [])
    print(f"Skill saved: {args.name}")
    print(f"  File: {path}")
    print(f"  Steps: {len(steps)}")
    print(f"  Origin task: {skill.get('origin_task_id')}")


def _cmd_list_skills() -> None:
    """List all saved v2 skills."""
    from .skill_store import SkillStore

    store = SkillStore()
    skills = store.list_skills()
    if not skills:
        print("No v2 skills saved yet.")
        return
    for s in skills:
        print(f"  {s['name']}  ({s['step_count']} steps)  — {s['description'][:60]}")


def _cmd_run_skill_v2(args: argparse.Namespace) -> None:
    """Run a saved v2 skill on a video."""
    from pathlib import Path

    from .engine import PlanEngine
    from .skill_store import SkillStore

    store = SkillStore()
    engine = PlanEngine()

    skill = store.load(args.skill_name)
    plan = skill["plan"]

    output_path = args.output or str(
        (engine.outputs_dir / f"gemia_out_{__import__('uuid').uuid4().hex[:8]}.mp4").resolve()
    )

    print(f"Skill:  {skill['name']}")
    print(f"Input:  {args.video}")
    print(f"Output: {output_path}")
    steps = plan.get("steps", [])
    print(f"Plan: {len(steps)} step{'s' if len(steps) != 1 else ''}")
    for s in steps:
        print(f"  {s['id']}: {s['function']}({s.get('args', {})})")
    print()

    print("Executing...")
    task_id = engine.run_with_task(plan, str(Path(args.video).resolve()), output_path)
    print(f"\nDone! task_id={task_id}")
    print(f"Output: {output_path}")


if __name__ == "__main__":
    main()
