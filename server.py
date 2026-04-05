"""Minimal local HTTP server for Gemia MVP.

Endpoints:
  GET  /                        → web UI (static/index.html)
  GET  /file/<rel-path>         → serve output files (outputs/, frames/, styled/, demo/)
  GET  /config                  → {"has_key": bool}
  POST /config                  → save API keys to ~/.gemia/config.json
  POST /run-skill               body: {"skill_id": str, "inputs": {...}}
  GET  /task/<task_id>
  GET  /task/<task_id>/assets
  GET  /skills
"""

from __future__ import annotations

import json
import mimetypes
import os
import uuid
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

_CONFIG_PATH = Path.home() / ".gemia" / "config.json"


def _load_config_keys() -> None:
    """Load API keys from ~/.gemia/config.json into env vars (if not already set)."""
    if _CONFIG_PATH.exists():
        try:
            cfg = json.loads(_CONFIG_PATH.read_text())
            if key := cfg.get("openrouter_api_key"):
                os.environ.setdefault("OPENROUTER_API_KEY", key)
            if key := cfg.get("gemini_api_key"):
                os.environ.setdefault("GEMINI_API_KEY", key)
            if key := cfg.get("laozhang_api_key"):
                os.environ.setdefault("LAOZHANG_API_KEY", key)
        except Exception:
            pass


def _has_valid_key() -> bool:
    key = os.environ.get("OPENROUTER_API_KEY", "")
    return bool(key) and key not in ("test", "sk-or-...") and len(key) > 10

from gemia.orchestrator import GemiaOrchestrator, get_assets, get_task, run_skill, plan_from_primitives
from gemia.ai.sub_agents import SubAgentRegistry

# In-memory store for pending ask sessions
_pending_asks: dict[str, dict] = {}

_BASE_DIR = Path(__file__).resolve().parent
_SKILLS_DIR = _BASE_DIR / "skills"
_STATIC_DIR = _BASE_DIR / "static"
_INPUTS_DIR = _BASE_DIR / "inputs"
# Directories that may be served via /file/
_ALLOWED_ROOTS = {"outputs", "frames", "styled", "demo", "inputs"}
_TASKS_DIR = _BASE_DIR / "tasks"
_PLANS_DIR = _BASE_DIR / "plans"


def _json_response(handler: BaseHTTPRequestHandler, status: int, body: object) -> None:
    data = json.dumps(body, ensure_ascii=False).encode()
    handler.send_response(status)
    handler.send_header("Content-Type", "application/json")
    handler.send_header("Content-Length", str(len(data)))
    handler.end_headers()
    handler.wfile.write(data)


def _file_response(handler: BaseHTTPRequestHandler, path: Path, *, body: bool = True) -> None:
    if not path.exists() or not path.is_file():
        _json_response(handler, 404, {"error": "file not found"})
        return
    content_type = mimetypes.guess_type(str(path))[0] or "application/octet-stream"
    size = path.stat().st_size
    handler.send_response(200)
    handler.send_header("Content-Type", content_type)
    handler.send_header("Content-Length", str(size))
    handler.end_headers()
    if body:
        handler.wfile.write(path.read_bytes())


def _task_file(task_id: str) -> Path:
    return _TASKS_DIR / f"{task_id}.json"


def _plan_file_for_task(task_id: str) -> Path:
    return _PLANS_DIR / f"{task_id}_plan.json"


def _load_task_payload(task_id: str) -> dict:
    path = _task_file(task_id)
    if not path.exists():
        raise FileNotFoundError(f"task not found: {task_id}")
    return json.loads(path.read_text())


def _load_plan_payload(task_id: str) -> dict:
    path = _plan_file_for_task(task_id)
    if not path.exists():
        raise FileNotFoundError(f"plan not found for task: {task_id}")
    return json.loads(path.read_text())


def _goal_for_task(task_id: str) -> str | None:
    try:
        plan = _load_plan_payload(task_id)
        return plan.get("goal")
    except Exception:
        return None


def _style_from_goal(goal: str | None) -> str | None:
    if not goal:
        return None
    marker = "with style:"
    lower = goal.lower()
    idx = lower.find(marker)
    if idx == -1:
        return None
    style = goal[idx + len(marker):].strip()
    return style or None


def _append_revision(task_id: str, revision: dict) -> dict:
    path = _task_file(task_id)
    payload = _load_task_payload(task_id)
    revisions = payload.get("revisions", [])
    revisions.append(revision)
    payload["revisions"] = revisions
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n")
    return payload


class _Handler(BaseHTTPRequestHandler):
    def log_message(self, fmt: str, *args: object) -> None:  # quieter logs
        print(f"  {self.address_string()} {fmt % args}")

    def _handle_get_like(self, *, body: bool) -> None:
        path = self.path.rstrip("/") or "/"

        # Web UI
        if path == "/":
            _file_response(self, _STATIC_DIR / "index.html", body=body)
            return

        # Config status (for first-run key check)
        if path == "/config":
            _json_response(self, 200, {"has_key": _has_valid_key()})
            return

        # Safe file serving: /file/outputs/..., /file/demo/...
        if path.startswith("/file/"):
            rel = path[len("/file/"):]
            # Reject traversal attempts
            parts_rel = Path(rel).parts
            if not parts_rel or parts_rel[0] not in _ALLOWED_ROOTS or ".." in parts_rel:
                _json_response(self, 403, {"error": "forbidden"})
                return
            _file_response(self, (_BASE_DIR / rel).resolve(), body=body)
            return

        if path == "/agents":
            _json_response(self, 200, {"agents": SubAgentRegistry.list_agents()})
            return

        if path == "/skills":
            skills = sorted(p.stem for p in _SKILLS_DIR.glob("*.json"))
            inputs = sorted(
                {
                    p.name: str(p.resolve())
                    for p in _INPUTS_DIR.glob("**/*")
                    if p.is_file()
                }.items()
            )
            _json_response(self, 200, {
                "skills": skills,
                "inputs": [
                    {"name": name, "path": abs_path}
                    for name, abs_path in inputs
                ]
            })
            return

        if path == "/tasks":
            items = []
            for p in sorted(_TASKS_DIR.glob("*.json"), key=lambda x: x.stat().st_mtime, reverse=True):
                try:
                    payload = json.loads(p.read_text())
                    revisions = payload.get("revisions", [])
                    latest_revision = revisions[-1] if revisions else None
                    task_id = payload.get("task_id", p.stem)
                    goal = _goal_for_task(task_id)
                    latest_feedback = latest_revision.get("feedback") if latest_revision else None
                    items.append({
                        "task_id": task_id,
                        "status": payload.get("status", "unknown"),
                        "plan_id": payload.get("plan_id"),
                        "created_at": payload.get("created_at"),
                        "outputs": payload.get("outputs", []),
                        "revision_count": len(revisions),
                        "latest_feedback": latest_feedback,
                        "latest_style": latest_feedback or _style_from_goal(goal),
                        "latest_preview_task_id": latest_revision.get("revision_task_id") if latest_revision else task_id,
                        "goal": goal,
                    })
                except Exception:
                    continue
            _json_response(self, 200, {"tasks": items[:30]})
            return

        # /task/<task_id>  or  /task/<task_id>/assets
        parts = path.split("/")
        if len(parts) >= 3 and parts[1] == "task":
            task_id = parts[2]
            try:
                if len(parts) == 4 and parts[3] == "assets":
                    _json_response(self, 200, get_assets(task_id))
                else:
                    task = get_task(task_id)
                    revisions = task.get("revisions", [])
                    latest_revision = revisions[-1] if revisions else None
                    goal = _goal_for_task(task_id)
                    latest_feedback = latest_revision.get("feedback") if latest_revision else None
                    task["revision_count"] = len(revisions)
                    task["latest_preview_task_id"] = latest_revision.get("revision_task_id") if latest_revision else task_id
                    task["goal"] = goal
                    task["latest_style"] = latest_feedback or _style_from_goal(goal)
                    _json_response(self, 200, task)
            except FileNotFoundError:
                _json_response(self, 404, {"error": f"task not found: {task_id}"})
            except Exception as exc:
                _json_response(self, 500, {"error": str(exc)})
            return

        _json_response(self, 404, {"error": "not found"})

    def do_GET(self) -> None:  # noqa: N802
        self._handle_get_like(body=True)

    def do_HEAD(self) -> None:  # noqa: N802
        self._handle_get_like(body=False)

    def do_POST(self) -> None:  # noqa: N802
        route = self.path.rstrip("/")
        if route == "/upload-video":
            filename = (self.headers.get("X-Filename") or "upload.mp4").strip()
            safe_name = Path(filename).name.strip() or "upload.mp4"
            ext = Path(safe_name).suffix.lower() or ".mp4"
            if ext != ".mp4":
                safe_name = f"{Path(safe_name).stem}.mp4"
            dest = _INPUTS_DIR / safe_name
            if dest.exists():
                stem = Path(safe_name).stem
                suffix = Path(safe_name).suffix
                i = 1
                while dest.exists():
                    dest = _INPUTS_DIR / f"{stem}_{i}{suffix}"
                    i += 1
            length = int(self.headers.get("Content-Length", 0))
            if length <= 0:
                _json_response(self, 400, {"error": "empty upload"})
                return
            dest.parent.mkdir(parents=True, exist_ok=True)
            with dest.open("wb") as f:
                remaining = length
                while remaining > 0:
                    chunk = self.rfile.read(min(1024 * 1024, remaining))
                    if not chunk:
                        break
                    f.write(chunk)
                    remaining -= len(chunk)
            _json_response(self, 200, {"name": dest.name, "path": str(dest.resolve())})
            return

        if route == "/config":
            # Save API keys to ~/.gemia/config.json and reload into env
            try:
                length = int(self.headers.get("Content-Length", 0))
                raw = self.rfile.read(length) if length else b"{}"
                body = json.loads(raw)
                cfg_dir = _CONFIG_PATH.parent
                cfg_dir.mkdir(parents=True, exist_ok=True)
                existing = {}
                if _CONFIG_PATH.exists():
                    try:
                        existing = json.loads(_CONFIG_PATH.read_text())
                    except Exception:
                        pass
                if key := body.get("openrouter_api_key", "").strip():
                    existing["openrouter_api_key"] = key
                    os.environ["OPENROUTER_API_KEY"] = key
                if key := body.get("gemini_api_key", "").strip():
                    existing["gemini_api_key"] = key
                    os.environ["GEMINI_API_KEY"] = key
                _CONFIG_PATH.write_text(json.dumps(existing, indent=2, ensure_ascii=False))
                _json_response(self, 200, {"ok": True})
            except Exception as exc:
                _json_response(self, 500, {"error": str(exc)})
            return

        if route not in ("/run-skill", "/run-prompt") \
                and not route.startswith("/revise-task/") \
                and not route.startswith("/answer-ask/"):
            _json_response(self, 404, {"error": "not found"})
            return

        length = int(self.headers.get("Content-Length", 0))
        raw = self.rfile.read(length) if length else b"{}"
        try:
            payload = json.loads(raw)
        except json.JSONDecodeError as exc:
            _json_response(self, 400, {"error": f"invalid JSON: {exc}"})
            return

        if route == "/run-skill":
            skill_id = payload.get("skill_id")
            inputs = payload.get("inputs", {})
            if not skill_id:
                _json_response(self, 400, {"error": "skill_id is required"})
                return

            try:
                task_id = run_skill(skill_id, inputs)
                _json_response(self, 200, {"task_id": task_id})
            except FileNotFoundError as exc:
                _json_response(self, 404, {"error": str(exc)})
            except Exception as exc:
                _json_response(self, 500, {"error": str(exc)})
            return

        if route == "/run-prompt":
            prompt = str(payload.get("prompt", "")).strip()
            video = str(payload.get("video", "")).strip()
            agent = str(payload.get("agent", "")).strip() or None
            if not prompt:
                _json_response(self, 400, {"error": "prompt is required"})
                return
            if not video:
                _json_response(self, 400, {"error": "video is required"})
                return
            orch = GemiaOrchestrator()
            output_path = str((orch.outputs_dir / f"ai_{uuid.uuid4().hex[:8]}.mp4").resolve())
            try:
                result = orch.plan_from_primitives(prompt, input_path=video, output_path=output_path, agent=agent)
            except Exception as exc:
                _json_response(self, 500, {"error": str(exc)})
                return
            if result.get("ask"):
                ask_id = uuid.uuid4().hex[:12]
                _pending_asks[ask_id] = {
                    "prompt": prompt,
                    "video": video,
                    "output_path": output_path,
                }
                _json_response(self, 200, {
                    "ask": True,
                    "ask_id": ask_id,
                    "questions": result.get("questions", []),
                })
            else:
                try:
                    task_id = orch.run_plan_dict(result)
                    _json_response(self, 200, {"task_id": task_id})
                except Exception as exc:
                    _json_response(self, 500, {"error": str(exc)})
            return

        if route.startswith("/answer-ask/"):
            ask_id = route.split("/")[-1]
            session = _pending_asks.get(ask_id)
            if not session:
                _json_response(self, 404, {"error": f"ask session not found: {ask_id}"})
                return
            answers = payload.get("answers") or {}
            if isinstance(answers, str):
                answers = {"answer": answers}
            orch = GemiaOrchestrator()
            try:
                result = orch.plan_or_ask(
                    session["prompt"],
                    input_path=session["video"],
                    output_path=session["output_path"],
                    answers=answers,
                )
            except Exception as exc:
                _json_response(self, 500, {"error": str(exc)})
                return
            if result.get("ask"):
                # Still unclear — return new questions with same ask_id
                _pending_asks[ask_id] = session
                _json_response(self, 200, {
                    "ask": True,
                    "ask_id": ask_id,
                    "questions": result.get("questions", []),
                })
                return
            _pending_asks.pop(ask_id, None)
            try:
                task_id = orch.run_plan_dict(result)
                _json_response(self, 200, {"task_id": task_id})
            except Exception as exc:
                _json_response(self, 500, {"error": str(exc)})
            return

        task_id = route.split("/")[-1]
        feedback = str(payload.get("feedback", "")).strip()
        if not feedback:
            _json_response(self, 400, {"error": "feedback is required"})
            return

        try:
            plan = _load_plan_payload(task_id)
            skill_id = plan.get("skill_id")
            input_path = plan.get("input_path") or (plan.get("inputs") or {}).get("video")
            if not skill_id or not input_path:
                raise ValueError("original plan is missing skill_id or input_path")
            revision_task_id = run_skill(skill_id, {"video": input_path, "style": feedback})
            revision_task = get_task(revision_task_id)
            updated = _append_revision(task_id, {
                "revision_task_id": revision_task_id,
                "feedback": feedback,
                "created_at": revision_task.get("created_at"),
                "outputs": revision_task.get("outputs", []),
                "status": revision_task.get("status", "unknown")
            })
            _json_response(self, 200, {
                "task_id": task_id,
                "revision_task_id": revision_task_id,
                "revision_count": len(updated.get("revisions", [])),
                "status": "succeeded"
            })
        except FileNotFoundError as exc:
            _json_response(self, 404, {"error": str(exc)})
        except Exception as exc:
            _json_response(self, 500, {"error": str(exc)})


def main(host: str = "127.0.0.1", port: int = 8000) -> None:
    _load_config_keys()  # Load API keys from ~/.gemia/config.json on startup
    server = HTTPServer((host, port), _Handler)
    print(f"Gemia server listening on http://{host}:{port}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nStopped.")


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="Gemia MVP local HTTP server")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8000)
    args = parser.parse_args()
    main(args.host, args.port)
