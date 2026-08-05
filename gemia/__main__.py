from __future__ import annotations

import argparse
import asyncio
import json
from pathlib import Path

from .orchestrator import GemiaOrchestrator
from .bridge import (
    BridgeDaemon,
    BridgePaths,
    BridgeTask,
    ClaudeCodeAdapter,
    ControllerAdapter,
    MasterBridgeController,
    QueueBridgeAdapter,
)
from .video.layers import render_layer_plan
from .video.preview import render_shadow_preview
from .video.review import review_real_media_artifact
from .video.intellisearch import index_real_media, search_media_index


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

    p_migrate_skills = sub.add_parser(
        "migrate-skills",
        help="One-shot: migrate legacy distilled skill JSONs to .lus (docs/lus-skill-format.md §8)")
    p_migrate_skills.add_argument("--dry-run", action="store_true",
                                  help="Print the migration plan without writing anything")
    p_migrate_skills.add_argument("--root", default=None,
                                  help="Skills root to migrate (default: the distilled skill store)")

    p_run_skill_v2 = sub.add_parser("run-skill-v2", help="Run a saved v2 skill")
    p_run_skill_v2.add_argument("skill_name", help="Skill name")
    p_run_skill_v2.add_argument("--video", required=True, help="Input video path")
    p_run_skill_v2.add_argument("--output", default=None, help="Output path")

    p_render_layer_plan = sub.add_parser("render-layer-plan", help="Render a layer-plan JSON into a preview video")
    p_render_layer_plan.add_argument("plan_path", help="Path to a layer-plan JSON file")
    p_render_layer_plan.add_argument("--output", required=True, help="Output video path")
    p_render_layer_plan.add_argument("--step", type=int, default=1, help="Render every Nth frame for low-fi previews")

    p_shadow_preview = sub.add_parser("render-shadow-preview", help="Render a low-fi shadow preview from a layer-plan JSON")
    p_shadow_preview.add_argument("plan_path", help="Path to a layer-plan JSON file")
    p_shadow_preview.add_argument("--output", required=True, help="Output preview video path")
    p_shadow_preview.add_argument("--step", type=int, default=2, help="Render every Nth frame")
    p_shadow_preview.add_argument("--max-long-edge", type=int, default=540, help="Scale preview canvas so its longest edge is this size")
    p_shadow_preview.add_argument("--proxy-resolution", type=int, default=540, help="Resolution used for generated video proxies")
    p_shadow_preview.add_argument("--proxy-root", default=None, help="Directory for generated proxy assets")
    p_shadow_preview.add_argument("--backend", default=None, help="Render backend target: auto or software")

    p_real_media_review = sub.add_parser("review-real-media", help="Write a real-media review artifact for an output")
    p_real_media_review.add_argument("--source", required=True, help="Real source video path")
    p_real_media_review.add_argument("--output", required=True, help="Rendered output video path")
    p_real_media_review.add_argument("--report", default=None, help="Review report JSON path")
    p_real_media_review.add_argument("--preview-manifest", default=None, help="Preview manifest JSON path")
    p_real_media_review.add_argument("--layer-flow-manifest", default=None, help="Layer-flow manifest JSON path")
    p_real_media_review.add_argument("--stock-catalog", default=None, help="Stock catalog JSON for real-footage evidence")
    p_real_media_review.add_argument("--min-output-frames", type=int, default=1, help="Minimum accepted output frame count")
    p_real_media_review.add_argument("--allow-unconfirmed-source", action="store_true", help="Do not require stock catalog real-footage evidence")

    p_intellisearch_index = sub.add_parser("intellisearch-index", help="Build a searchable real-media semantic index")
    p_intellisearch_index.add_argument("--media", action="append", required=True, help="Real video path to index")
    p_intellisearch_index.add_argument("--review", action="append", default=[], help="Real-media review report JSON")
    p_intellisearch_index.add_argument("--stock-catalog", default=None, help="Stock catalog JSON for semantic labels")
    p_intellisearch_index.add_argument("--label", action="append", default=[], help="Extra label to attach to every indexed clip")
    p_intellisearch_index.add_argument("--output", required=True, help="Output index JSON path")
    p_intellisearch_index.add_argument("--max-samples", type=int, default=6, help="Maximum video sample frames per clip")

    p_intellisearch_search = sub.add_parser("intellisearch-search", help="Search a real-media semantic index")
    p_intellisearch_search.add_argument("--index", required=True, help="IntelliSearch index JSON path")
    p_intellisearch_search.add_argument("--query", required=True, help="Search query")
    p_intellisearch_search.add_argument("--output", default=None, help="Optional output result JSON path")
    p_intellisearch_search.add_argument("--limit", type=int, default=5, help="Maximum matches")

    p_skill_stats = sub.add_parser("skill-stats", help="Show Lumeri planner skill router telemetry")
    p_skill_stats.add_argument("--days", type=int, default=7)
    p_skill_stats.add_argument("--db", default=None)
    p_skill_stats.add_argument("--json", action="store_true")

    p_lumerai_script = sub.add_parser(
        "lumerai-script",
        help="Developer entry: run a Lumeri runtime script through the sandbox (gated by LUMERAI_SCRIPT_MODE=1)",
    )
    p_lumerai_script.add_argument("--script", required=True, help="Path to the AI/developer-authored Python script")
    p_lumerai_script.add_argument("--project-state", default=None, help="(legacy) Path to a JSON project state file; mutually exclusive with --project-id")
    p_lumerai_script.add_argument("--project-id", default=None, help="Persistent project id under <root>/projects/<id>/; auto-created on first reference")
    p_lumerai_script.add_argument("--project-init-from", default=None, help="When auto-creating, seed the new project from this JSON file")
    p_lumerai_script.add_argument("--session-id", required=True, help="Session identifier for provenance")
    p_lumerai_script.add_argument("--ai-model", default="developer-cli", help="Provenance label for the calling model")
    p_lumerai_script.add_argument("--timeout-sec", type=int, default=30)
    p_lumerai_script.add_argument("--dry-run", action="store_true", help="Validate only; do not execute")
    p_lumerai_script.add_argument("--root", default=None, help="Override Gemia root_dir (used in tests)")

    p_lumerai_undo = sub.add_parser(
        "lumerai-undo",
        help="Rewind a Lumeri project to an earlier patch seq (gated by LUMERAI_SCRIPT_MODE=1)",
    )
    p_lumerai_undo.add_argument("--project-id", required=True)
    p_lumerai_undo.add_argument(
        "--to-seq",
        required=True,
        type=int,
        help="Target patch sequence number; 0 rewinds to the original seed",
    )
    p_lumerai_undo.add_argument("--root", default=None, help="Override Gemia root_dir (used in tests)")

    p_lumerai_inspect = sub.add_parser(
        "lumerai-inspect",
        help="Read-only summary of a stored Lumeri project (timeline + history)",
    )
    p_lumerai_inspect.add_argument("--project-id", required=True)
    p_lumerai_inspect.add_argument(
        "--format",
        choices=["json", "text"],
        default="json",
        help="Output format (default: json)",
    )
    p_lumerai_inspect.add_argument(
        "--history",
        type=int,
        default=0,
        help="Include metadata for the N most recent patches",
    )
    p_lumerai_inspect.add_argument("--root", default=None, help="Override Gemia root_dir (used in tests)")

    p_lumerai_render = sub.add_parser(
        "lumerai-render",
        help="Render a low-res preview MP4 for a stored Lumeri project (gated by LUMERAI_SCRIPT_MODE=1)",
    )
    p_lumerai_render.add_argument("--project-id", required=True)
    p_lumerai_render.add_argument(
        "--max-long-edge",
        type=int,
        default=640,
        help="Scale preview so its longest edge is at most this many pixels",
    )
    p_lumerai_render.add_argument("--label", default="preview")
    p_lumerai_render.add_argument("--timeout-sec", type=int, default=120)
    p_lumerai_render.add_argument("--root", default=None, help="Override Gemia root_dir (used in tests)")

    p_server = sub.add_parser("server", help="Start the web server")
    p_server.add_argument("--host", default=None, help="Bind host; defaults to GEMIA_HOST/LUMERI_HOST or 0.0.0.0")
    p_server.add_argument("--port", type=int, default=None, help="Bind port; defaults to GEMIA_PORT/LUMERI_PORT or 7788")

    p_mcp_serve = sub.add_parser(
        "mcp-serve",
        help="Serve Lumeri as an MCP server (Direction A). Phase 1: stdio transport.",
    )
    p_mcp_serve.add_argument(
        "--transport",
        choices=["stdio", "http"],
        default="stdio",
        help="Transport (Phase 1 ships stdio; http is Phase 2).",
    )
    p_mcp_serve.add_argument(
        "--http-port",
        type=int,
        default=7789,
        help="Port for the streamable-HTTP transport (Phase 2; ignored for stdio).",
    )

    p_setup = sub.add_parser("setup", help="Run (or re-run) the first-run onboarding wizard")

    p_bridge_init = sub.add_parser("bridge-init", help="Create bridge inbox/outbox directories")
    p_bridge_init.add_argument("--root", default="~/.gemia/bridge")

    p_bridge_submit = sub.add_parser("bridge-submit", help="Submit a file-based bridge task")
    p_bridge_submit.add_argument("--root", default="~/.gemia/bridge")
    p_bridge_submit.add_argument("--source", default="antigravity")
    p_bridge_submit.add_argument("--intent", required=True)
    p_bridge_submit.add_argument("--prompt", required=True)
    p_bridge_submit.add_argument("--asset", action="append", default=[])
    p_bridge_submit.add_argument(
        "--task-class",
        default=None,
        help="Routing class: architecture | review | frontend",
    )
    p_bridge_submit.add_argument("--agent", default=None, help="Preferred sub-agent (e.g. claude_code, antigravity)")
    p_bridge_submit.add_argument("--allow-agent", action="append", default=[], help="Restrict routing to these agents")
    p_bridge_submit.add_argument("--cwd", default=None)
    p_bridge_submit.add_argument("--context-json", default=None)
    p_bridge_submit.add_argument("--permissions-json", default=None)

    p_bridge_once = sub.add_parser("bridge-run-once", help="Process all pending bridge tasks once")
    p_bridge_once.add_argument("--root", default="~/.gemia/bridge")
    p_bridge_once.add_argument("--claude-bin", default="claude")
    p_bridge_once.add_argument("--antigravity-root", default=None, help="Queue root for delegated Antigravity tasks")
    p_bridge_once.add_argument("--timeout-sec", type=int, default=600)
    p_bridge_once.add_argument("--cwd", default=None)

    p_bridge_daemon = sub.add_parser("bridge-daemon", help="Run the file-based bridge daemon")
    p_bridge_daemon.add_argument("--root", default="~/.gemia/bridge")
    p_bridge_daemon.add_argument("--claude-bin", default="claude")
    p_bridge_daemon.add_argument("--antigravity-root", default=None, help="Queue root for delegated Antigravity tasks")
    p_bridge_daemon.add_argument("--timeout-sec", type=int, default=600)
    p_bridge_daemon.add_argument("--poll-interval", type=float, default=1.0)
    p_bridge_daemon.add_argument("--heartbeat-interval-sec", type=int, default=7200)
    p_bridge_daemon.add_argument("--heartbeat-source", default="codex")
    p_bridge_daemon.add_argument("--heartbeat-instructions", default=None, help="Path to HEARTBEAT.md-style instructions file")
    p_bridge_daemon.add_argument("--cwd", default=None)

    p_bridge_heartbeat = sub.add_parser("bridge-heartbeat-once", help="Submit and process one local heartbeat poll")
    p_bridge_heartbeat.add_argument("--root", default="~/.gemia/bridge")
    p_bridge_heartbeat.add_argument("--source", default="codex")
    p_bridge_heartbeat.add_argument("--instructions", default=None, help="Path to HEARTBEAT.md-style instructions file")
    p_bridge_heartbeat.add_argument("--min-interval-sec", type=int, default=0)
    p_bridge_heartbeat.add_argument("--claude-bin", default="claude")
    p_bridge_heartbeat.add_argument("--antigravity-root", default=None, help="Queue root for delegated Antigravity tasks")
    p_bridge_heartbeat.add_argument("--timeout-sec", type=int, default=600)
    p_bridge_heartbeat.add_argument("--cwd", default=None)

    p_agent_once = sub.add_parser("bridge-agent-run-once", help="Process one file-based agent queue once")
    p_agent_once.add_argument("--queue-root", default="~/.gemia/bridge/agents/antigravity")
    p_agent_once.add_argument(
        "--adapter",
        choices=["claude", "openclaw", "openclaw-with-claude-fallback"],
        default="claude",
    )
    p_agent_once.add_argument("--openclaw-bin", default="openclaw")
    p_agent_once.add_argument("--openclaw-agent", default="worker")
    p_agent_once.add_argument("--claude-bin", default="claude")
    p_agent_once.add_argument("--timeout-sec", type=int, default=600)
    p_agent_once.add_argument("--cwd", default=None)
    p_agent_once.add_argument("--task-id", default=None, help="Process only this task id from the agent inbox")

    p_agent_daemon = sub.add_parser("bridge-agent-daemon", help="Run a file-based agent queue daemon")
    p_agent_daemon.add_argument("--queue-root", default="~/.gemia/bridge/agents/antigravity")
    p_agent_daemon.add_argument(
        "--adapter",
        choices=["claude", "openclaw", "openclaw-with-claude-fallback"],
        default="claude",
    )
    p_agent_daemon.add_argument("--openclaw-bin", default="openclaw")
    p_agent_daemon.add_argument("--openclaw-agent", default="worker")
    p_agent_daemon.add_argument("--claude-bin", default="claude")
    p_agent_daemon.add_argument("--timeout-sec", type=int, default=600)
    p_agent_daemon.add_argument("--poll-interval", type=float, default=1.0)
    p_agent_daemon.add_argument("--cwd", default=None)

    args = parser.parse_args()

    # ── First-run onboarding gate ──────────────────────────────────────
    # The "setup" subcommand always re-runs the wizard. For commands that need
    # a model, if no provider is configured, run onboarding interactively on a
    # TTY or print instructions (and exit) otherwise. Non-LLM commands (e.g.
    # render-layer-plan, get-task, bridge-*) keep working even when unconfigured
    # and never hang non-interactively.
    from .onboarding import ensure_onboarded, needs_onboarding, run_setup

    if args.command == "setup":
        raise SystemExit(run_setup())

    _LLM_COMMANDS = {
        "run", "run-skill-v2", "save-skill",
        "lumerai-script",
    }
    if args.command in _LLM_COMMANDS and needs_onboarding():
        if not ensure_onboarded():
            # Headless + unconfigured: instructions already printed; exit cleanly
            # rather than hang or crash deep in a model call.
            raise SystemExit(1)

    if args.command == "run":
        _cmd_run(args)
    elif args.command == "save-skill":
        _cmd_save_skill(args)
    elif args.command == "list-skills":
        _cmd_list_skills()
    elif args.command == "migrate-skills":
        _cmd_migrate_skills(args)
    elif args.command == "run-skill-v2":
        _cmd_run_skill_v2(args)
    elif args.command == "render-layer-plan":
        _cmd_render_layer_plan(args)
    elif args.command == "render-shadow-preview":
        _cmd_render_shadow_preview(args)
    elif args.command == "review-real-media":
        _cmd_review_real_media(args)
    elif args.command == "intellisearch-index":
        _cmd_intellisearch_index(args)
    elif args.command == "intellisearch-search":
        _cmd_intellisearch_search(args)
    elif args.command == "skill-stats":
        _cmd_skill_stats(args)
    elif args.command == "lumerai-script":
        raise SystemExit(_cmd_lumerai_script(args))
    elif args.command == "lumerai-undo":
        raise SystemExit(_cmd_lumerai_undo(args))
    elif args.command == "lumerai-inspect":
        raise SystemExit(_cmd_lumerai_inspect(args))
    elif args.command == "lumerai-render":
        raise SystemExit(_cmd_lumerai_render(args))
    elif args.command == "server":
        import importlib.util, pathlib
        _spec = importlib.util.spec_from_file_location(
            "gemia_server",
            pathlib.Path(__file__).parent.parent / "server.py",
        )
        _srv = importlib.util.module_from_spec(_spec)
        _spec.loader.exec_module(_srv)
        _srv.main(host=args.host, port=args.port)
    elif args.command == "mcp-serve":
        _cmd_mcp_serve(args)
    elif args.command == "bridge-init":
        _cmd_bridge_init(args)
    elif args.command == "bridge-submit":
        _cmd_bridge_submit(args)
    elif args.command == "bridge-run-once":
        _cmd_bridge_run_once(args)
    elif args.command == "bridge-daemon":
        _cmd_bridge_daemon(args)
    elif args.command == "bridge-heartbeat-once":
        _cmd_bridge_heartbeat_once(args)
    elif args.command == "bridge-agent-run-once":
        _cmd_bridge_agent_run_once(args)
    elif args.command == "bridge-agent-daemon":
        _cmd_bridge_agent_daemon(args)


def _cmd_mcp_serve(args: argparse.Namespace) -> None:
    """Serve Lumeri as an MCP server (docs/mcp-interface-plan.md, Direction A).

    Phase 1 ships the stdio transport only. ``--transport http`` is reserved for
    Phase 2 (in-process streamable HTTP on 127.0.0.1:7789) and exits cleanly
    with a not-yet-implemented message rather than pretending.
    """
    transport = getattr(args, "transport", "stdio")
    if transport == "http":
        raise SystemExit(
            "mcp-serve --transport http is a Phase 2 deliverable and is not "
            "implemented yet; use the default stdio transport."
        )
    # stdio: run_stdio imports the optional `mcp` SDK lazily and prints a
    # friendly install hint (exit 1) if it is missing.
    from .mcp.server import run_stdio

    run_stdio()


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

    while plan.get("ask"):
        print("\nAI needs more info:")
        answers: dict[str, str] = {}
        for q in plan.get("questions", []):
            print(f"  {q}")
            try:
                ans = input("  > ").strip()
            except (EOFError, KeyboardInterrupt):
                print("\nAborted.")
                return
            answers[str(q.get("id", len(answers)))] = ans
        print()
        print("Re-planning with your answers...")
        plan = asyncio.run(client.plan_from_primitives(
            args.prompt,
            input_path=str(Path(args.video).resolve()),
            output_path=output_path,
            answers=answers,
        ))

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


def _cmd_lumerai_script(args: argparse.Namespace) -> int:
    """Developer entry: script file -> sandbox -> TimelinePatch -> task.

    Returns a process exit code. Always writes a JSON document to stdout —
    either a success summary or a structured error. Raw sandbox stderr is kept
    in the task JSON instead of being printed directly.
    """
    import os
    import sys
    import traceback

    def _emit_error(error_code: str, message: str, *, script_hash: str = "", extra: dict | None = None) -> int:
        payload = {
            "status": "failed",
            "error": {"code": error_code, "message": message},
            "script_hash": script_hash,
        }
        if extra:
            payload.update(extra)
        print(json.dumps(payload, ensure_ascii=False, indent=2))
        return 1

    if os.environ.get("LUMERAI_SCRIPT_MODE", "0") != "1":
        return _emit_error(
            "feature_flag_disabled",
            "LUMERAI_SCRIPT_MODE=1 is required to run lumerai-script",
        )

    script_path = Path(args.script).expanduser()
    if not script_path.exists():
        return _emit_error("script_not_found", f"Script file does not exist: {script_path}")
    try:
        script_text = script_path.read_text(encoding="utf-8")
    except OSError as exc:
        return _emit_error("script_read_failed", f"Cannot read script: {exc}")

    use_id = bool(args.project_id)
    use_inline = bool(args.project_state)
    if use_id and use_inline:
        return _emit_error(
            "conflicting_project_inputs",
            "--project-id and --project-state are mutually exclusive",
        )
    if not use_id and not use_inline:
        return _emit_error(
            "missing_project_input",
            "one of --project-id or --project-state is required",
        )

    project_state: dict | None = None
    if use_inline:
        state_path = Path(args.project_state).expanduser()
        if not state_path.exists():
            return _emit_error("project_state_not_found", f"Project state file does not exist: {state_path}")
        try:
            project_state = json.loads(state_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            return _emit_error("project_state_invalid", f"Cannot parse project state JSON: {exc}")
        if not isinstance(project_state, dict):
            return _emit_error("project_state_invalid", "Project state JSON must be an object")

    orch = GemiaOrchestrator(root_dir=args.root) if getattr(args, "root", None) else GemiaOrchestrator()

    created = False
    if use_id:
        from .project_store import ProjectStoreError as _PSE

        try:
            if not orch.project_store.exists(args.project_id):
                seed: dict | None = None
                if args.project_init_from:
                    seed_path = Path(args.project_init_from).expanduser()
                    if not seed_path.exists():
                        return _emit_error(
                            "project_init_seed_not_found",
                            f"--project-init-from path does not exist: {seed_path}",
                        )
                    seed = json.loads(seed_path.read_text(encoding="utf-8"))
                    if not isinstance(seed, dict):
                        return _emit_error("project_init_seed_invalid", "seed JSON must be an object")
                orch.project_store.create(args.project_id, seed=seed)
                created = True
        except _PSE as exc:
            return _emit_error("invalid_project_id", str(exc))
        except (OSError, json.JSONDecodeError) as exc:
            return _emit_error("project_init_seed_invalid", f"Cannot read seed JSON: {exc}")

    try:
        task = orch.plan_from_script(
            script_text,
            project_state=project_state,
            project_id=args.project_id if use_id else None,
            session_id=args.session_id,
            ai_model=args.ai_model,
            timeout_sec=int(args.timeout_sec),
            dry_run=bool(args.dry_run),
        )
    except RuntimeError as exc:
        return _emit_error("feature_flag_disabled", str(exc))
    except Exception as exc:  # pragma: no cover - defensive
        return _emit_error(
            "orchestrator_failed",
            f"{type(exc).__name__}: {exc}",
            extra={"traceback_tail": traceback.format_exc().splitlines()[-3:]},
        )

    if args.dry_run:
        ok = task.get("status") == "succeeded"
        payload = {
            "status": task.get("status"),
            "dry_run": True,
            "script_hash": task.get("script_hash", ""),
        }
        if not ok:
            payload["error"] = {"code": "sandbox_violation", "message": task.get("error") or task.get("stderr") or "dry-run failed"}
        print(json.dumps(payload, ensure_ascii=False, indent=2))
        return 0 if ok else 1

    task_id = task.get("task_id", "")
    task_file = str((orch.tasks_dir / f"{task_id}.json").resolve()) if task_id else ""

    if task.get("status") != "succeeded":
        code = "script_execution_failed"
        if task.get("error_code") == "no_timeline_patches":
            code = "script_emitted_no_patches"
        return _emit_error(
            code,
            task.get("error") or "Script execution failed",
            script_hash=task.get("script_hash", ""),
            extra={"task_id": task_id, "task_path": task_file},
        )

    project_state_out = task.get("project_state") or {}
    timeline = project_state_out.get("timeline") if isinstance(project_state_out, dict) else None
    clips = (timeline or {}).get("clips") or []
    patches = task.get("timeline_patches") or []

    summary = {
        "status": "succeeded",
        "task_id": task_id,
        "task_path": task_file,
        "script_hash": task.get("script_hash", ""),
        "patch_count": len(patches),
        "timeline_clip_count": len(clips),
    }
    if use_id:
        summary["project_id"] = args.project_id
        summary["created"] = created
        summary["project_state_path"] = str(orch.project_store.state_path(args.project_id))
        summary["patch_seq_start"] = task.get("patch_seq_start", 0)
        summary["patch_seq_end"] = task.get("patch_seq_end", 0)
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return 0


def _cmd_lumerai_undo(args: argparse.Namespace) -> int:
    """Rewind a stored project to an earlier patch seq."""
    import os

    def _emit_error(code: str, message: str) -> int:
        print(json.dumps(
            {"status": "failed", "error": {"code": code, "message": message}},
            ensure_ascii=False,
            indent=2,
        ))
        return 1

    if os.environ.get("LUMERAI_SCRIPT_MODE", "0") != "1":
        return _emit_error(
            "feature_flag_disabled",
            "LUMERAI_SCRIPT_MODE=1 is required to run lumerai-undo",
        )

    from .project_store import ProjectStoreError as _PSE

    orch = GemiaOrchestrator(root_dir=args.root) if getattr(args, "root", None) else GemiaOrchestrator()
    try:
        if not orch.project_store.exists(args.project_id):
            return _emit_error("project_not_found", f"project not found: {args.project_id}")
        result = orch.project_store.undo_to_seq(args.project_id, int(args.to_seq))
    except _PSE as exc:
        code = "invalid_target_seq" if "target_seq" in str(exc) else "invalid_project_id"
        return _emit_error(code, str(exc))

    summary = {
        "status": "succeeded",
        "project_id": args.project_id,
        "from_seq": result["from_seq"],
        "to_seq": result["to_seq"],
        "discarded_count": len(result["discarded"]),
        "project_state_path": str(orch.project_store.state_path(args.project_id)),
    }
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return 0


def _cmd_lumerai_inspect(args: argparse.Namespace) -> int:
    """Read-only project summary. No feature flag; pure read."""
    from .project_inspect import inspect_project, render_text
    from .project_store import ProjectStoreError as _PSE

    def _emit_error(code: str, message: str) -> int:
        print(json.dumps(
            {"status": "failed", "error": {"code": code, "message": message}},
            ensure_ascii=False,
            indent=2,
        ))
        return 1

    orch = GemiaOrchestrator(root_dir=args.root) if getattr(args, "root", None) else GemiaOrchestrator()
    try:
        if not orch.project_store.exists(args.project_id):
            return _emit_error("project_not_found", f"project not found: {args.project_id}")
        summary = inspect_project(
            orch.project_store, args.project_id, history=max(int(args.history), 0)
        )
    except _PSE as exc:
        return _emit_error("invalid_project_id", str(exc))

    if args.format == "text":
        print(render_text(summary), end="")
    else:
        print(json.dumps(summary, ensure_ascii=False, indent=2))
    return 0


def _cmd_lumerai_render(args: argparse.Namespace) -> int:
    """Render a ProjectStore project into a low-res preview."""
    import os

    from .project_render import ProjectRenderError, render_project_preview
    from .project_store import ProjectStoreError as _PSE

    def _emit_error(code: str, message: str, *, detail: str = "") -> int:
        payload = {"status": "failed", "error": {"code": code, "message": message}}
        if detail:
            payload["error"]["detail"] = detail
        print(json.dumps(payload, ensure_ascii=False, indent=2))
        return 1

    if os.environ.get("LUMERAI_SCRIPT_MODE", "0") != "1":
        return _emit_error(
            "feature_flag_disabled",
            "LUMERAI_SCRIPT_MODE=1 is required to run lumerai-render",
        )

    orch = GemiaOrchestrator(root_dir=args.root) if getattr(args, "root", None) else GemiaOrchestrator()
    try:
        if not orch.project_store.exists(args.project_id):
            return _emit_error("project_not_found", f"project not found: {args.project_id}")
        manifest = render_project_preview(
            orch.project_store,
            args.project_id,
            output_root=orch.outputs_dir,
            max_long_edge=int(args.max_long_edge),
            label=str(args.label or "preview"),
            timeout_sec=int(args.timeout_sec),
        )
    except _PSE as exc:
        return _emit_error("invalid_project_id", str(exc))
    except ProjectRenderError as exc:
        return _emit_error(exc.code, str(exc), detail=exc.detail)
    except Exception as exc:  # pragma: no cover - defensive command boundary
        return _emit_error("render_failed", f"{type(exc).__name__}: {exc}")

    print(json.dumps(
        {
            "status": "succeeded",
            "project_id": args.project_id,
            "render_id": manifest.get("render_id"),
            "patch_seq": manifest.get("patch_seq"),
            "preview_path": manifest.get("preview_path"),
            "manifest_path": manifest.get("manifest_path"),
            "duration": manifest.get("duration"),
            "resolution": manifest.get("resolution"),
            "source_clip_count": len(manifest.get("source_clips") or []),
        },
        ensure_ascii=False,
        indent=2,
    ))
    return 0


def _cmd_render_layer_plan(args: argparse.Namespace) -> None:
    plan_path = Path(args.plan_path).expanduser().resolve()
    plan = json.loads(plan_path.read_text(encoding="utf-8"))
    output = render_layer_plan(plan, args.output, step=max(int(args.step), 1))
    print(output)


def _cmd_render_shadow_preview(args: argparse.Namespace) -> None:
    plan_path = Path(args.plan_path).expanduser().resolve()
    plan = json.loads(plan_path.read_text(encoding="utf-8"))
    result = render_shadow_preview(
        plan,
        args.output,
        frame_step=max(int(args.step), 1),
        max_long_edge=int(args.max_long_edge),
        proxy_resolution=int(args.proxy_resolution),
        proxy_root=args.proxy_root,
        backend=args.backend,
    )
    print(json.dumps(
        {
            "output_path": result.output_path,
            "manifest_path": result.manifest_path,
            "proxy_map": result.proxy_map,
            "render_backend": result.render_backend,
        },
        ensure_ascii=False,
        indent=2,
    ))


def _cmd_review_real_media(args: argparse.Namespace) -> None:
    result = review_real_media_artifact(
        args.source,
        args.output,
        report_path=args.report,
        preview_manifest_path=args.preview_manifest,
        layer_flow_manifest_path=args.layer_flow_manifest,
        stock_catalog_path=args.stock_catalog,
        min_output_frames=max(int(args.min_output_frames), 1),
        require_real_source=not bool(args.allow_unconfirmed_source),
    )
    print(json.dumps(
        {
            "report_path": result.report_path,
            "status": result.status,
            "artifact_paths": result.artifact_paths,
            "findings": result.findings,
        },
        ensure_ascii=False,
        indent=2,
    ))


def _cmd_intellisearch_index(args: argparse.Namespace) -> None:
    result = index_real_media(
        list(args.media or []),
        args.output,
        review_report_paths=list(args.review or []),
        stock_catalog_path=args.stock_catalog,
        extra_labels=list(args.label or []),
        max_samples=max(int(args.max_samples), 1),
    )
    print(json.dumps(
        {
            "index_path": result.index_path,
            "clip_count": result.clip_count,
            "label_count": result.label_count,
        },
        ensure_ascii=False,
        indent=2,
    ))


def _cmd_intellisearch_search(args: argparse.Namespace) -> None:
    result = search_media_index(
        args.index,
        args.query,
        output_path=args.output,
        limit=max(int(args.limit), 1),
    )
    print(json.dumps(
        {
            "query": result.query,
            "index_path": result.index_path,
            "output_path": result.output_path,
            "match_count": result.match_count,
            "matches": result.matches,
        },
        ensure_ascii=False,
        indent=2,
    ))


def _cmd_skill_stats(args: argparse.Namespace) -> None:
    from .ai.skill_router import load_skill_metadata
    from .ai.skill_telemetry import format_skill_stats, skill_stats

    stats = skill_stats(
        days=args.days,
        db_path=args.db,
        all_skill_ids=sorted(load_skill_metadata().keys()),
    )
    if args.json:
        print(json.dumps(stats, ensure_ascii=False, indent=2, sort_keys=True))
    else:
        print(format_skill_stats(stats), end="")


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


def _cmd_migrate_skills(args: argparse.Namespace) -> None:
    """One-shot migration of legacy distilled skill JSONs to .lus (spec §8)."""
    from .skill_store import migrate_distilled_to_lus

    report = migrate_distilled_to_lus(args.root, dry_run=args.dry_run)
    if not report:
        print("No legacy skill JSONs found; nothing to migrate.")
        return
    for entry in report:
        line = f"{entry['file']}: {entry['status']}"
        if entry.get("detail"):
            line += f" — {entry['detail']}"
        print(line)


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


def _bridge_paths(root: str) -> BridgePaths:
    return BridgePaths.from_root(root)


def _make_bridge_daemon(args: argparse.Namespace) -> BridgeDaemon:
    paths = _bridge_paths(args.root)
    claude_adapter = ClaudeCodeAdapter(
        claude_bin=args.claude_bin,
        timeout_sec=args.timeout_sec,
        default_cwd=args.cwd,
    )
    antigravity_root = args.antigravity_root or (paths.root / "agents" / "antigravity")
    antigravity_adapter = QueueBridgeAdapter("antigravity", antigravity_root)
    controller = MasterBridgeController(
        {
            "claude_code": claude_adapter,
            "antigravity": antigravity_adapter,
        },
        default_agent="claude_code",
    )
    adapter = ControllerAdapter(controller)
    heartbeat_interval_sec = int(getattr(args, "heartbeat_interval_sec", 7200))
    heartbeat_source = str(getattr(args, "heartbeat_source", "codex"))
    heartbeat_instructions = getattr(args, "heartbeat_instructions", None)
    if heartbeat_instructions:
        heartbeat_instructions = str(Path(heartbeat_instructions).expanduser().resolve())
    return BridgeDaemon(
        paths,
        adapter,
        auto_heartbeat_interval_sec=heartbeat_interval_sec,
        auto_heartbeat_source=heartbeat_source,
        auto_heartbeat_instructions_path=heartbeat_instructions,
    )


def _make_agent_queue_daemon(args: argparse.Namespace) -> BridgeDaemon:
    paths = BridgePaths.from_root(args.queue_root)
    claude_adapter = ClaudeCodeAdapter(
        claude_bin=args.claude_bin,
        timeout_sec=args.timeout_sec,
        default_cwd=args.cwd,
    )
    if args.adapter == "claude":
        adapter = claude_adapter
    elif args.adapter == "openclaw":
        from .bridge import OpenClawAgentAdapter

        openclaw_adapter = OpenClawAgentAdapter(
            openclaw_bin=args.openclaw_bin,
            agent=args.openclaw_agent,
            timeout_sec=args.timeout_sec,
            default_cwd=args.cwd,
        )
        adapter = openclaw_adapter
    else:
        from .bridge import FallbackBridgeAdapter, OpenClawAgentAdapter

        openclaw_adapter = OpenClawAgentAdapter(
            openclaw_bin=args.openclaw_bin,
            agent=args.openclaw_agent,
            timeout_sec=args.timeout_sec,
            default_cwd=args.cwd,
        )
        adapter = FallbackBridgeAdapter("antigravity_openclaw", openclaw_adapter, claude_adapter)
    return BridgeDaemon(paths, adapter, auto_heartbeat_interval_sec=0)


def _cmd_bridge_init(args: argparse.Namespace) -> None:
    paths = _bridge_paths(args.root)
    paths.ensure()
    print(json.dumps({
        "root": str(paths.root),
        "inbox": str(paths.inbox),
        "processing": str(paths.processing),
        "outbox": str(paths.outbox),
        "failed": str(paths.failed),
        "logs": str(paths.logs),
        "leases": str(paths.leases),
        "heartbeat_state": str(paths.heartbeat_state),
    }, ensure_ascii=False, indent=2))


def _read_json_arg(raw: str | None) -> dict:
    if not raw:
        return {}
    path = Path(raw).expanduser()
    if path.exists():
        return json.loads(path.read_text())
    return json.loads(raw)


def _cmd_bridge_submit(args: argparse.Namespace) -> None:
    daemon = BridgeDaemon(_bridge_paths(args.root), adapter=ClaudeCodeAdapter())
    metadata = {"submitted_via": "gemia_cli"}
    if args.task_class:
        metadata["task_class"] = args.task_class
    task = BridgeTask.new(
        source=args.source,
        intent=args.intent,
        prompt=args.prompt,
        assets=list(args.asset or []),
        context=_read_json_arg(args.context_json),
        permissions=_read_json_arg(args.permissions_json),
        metadata=metadata,
        cwd=args.cwd,
    )
    task.preferred_agent = args.agent
    task.allowed_agents = list(args.allow_agent or [])
    payload_path = daemon.submit_task(task)
    print(json.dumps({
        "task_id": task.task_id,
        "payload": str(payload_path),
    }, ensure_ascii=False, indent=2))


def _cmd_bridge_run_once(args: argparse.Namespace) -> None:
    daemon = _make_bridge_daemon(args)
    processed = daemon.process_once()
    print(json.dumps({
        "processed": processed,
        "root": str(daemon.paths.root),
    }, ensure_ascii=False, indent=2))


def _cmd_bridge_daemon(args: argparse.Namespace) -> None:
    daemon = _make_bridge_daemon(args)
    print(f"Bridge daemon watching {daemon.paths.inbox}")
    daemon.serve_forever(poll_interval=args.poll_interval)


def _cmd_bridge_heartbeat_once(args: argparse.Namespace) -> None:
    daemon = _make_bridge_daemon(args)
    metadata = {
        "task_class": "heartbeat",
        "heartbeat": True,
    }
    if args.instructions:
        metadata["instructions_path"] = str(Path(args.instructions).expanduser().resolve())
    task = BridgeTask.new(
        source=args.source,
        intent="heartbeat",
        prompt="Read HEARTBEAT.md if it exists. Follow it strictly. Reply HEARTBEAT_OK when nothing needs attention.",
        metadata=metadata,
        context={"heartbeat_action": "poll", "min_interval_sec": args.min_interval_sec},
        cwd=args.cwd,
    )
    daemon.submit_task(task)
    daemon.process_once()
    result_path = daemon.paths.outbox / f"{task.task_id}.json"
    if not result_path.exists():
        raise SystemExit(f"Heartbeat result missing: {result_path}")
    print(result_path.read_text().strip())


def _cmd_bridge_agent_run_once(args: argparse.Namespace) -> None:
    daemon = _make_agent_queue_daemon(args)
    processed = daemon.process_task(args.task_id) if args.task_id else daemon.process_once()
    print(json.dumps({
        "processed": processed,
        "queue_root": str(daemon.paths.root),
        "task_id": args.task_id,
    }, ensure_ascii=False, indent=2))


def _cmd_bridge_agent_daemon(args: argparse.Namespace) -> None:
    daemon = _make_agent_queue_daemon(args)
    print(f"Bridge agent daemon watching {daemon.paths.inbox}")
    daemon.serve_forever(poll_interval=args.poll_interval)


if __name__ == "__main__":
    main()
