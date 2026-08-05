"""Local Codex subscription adapter for Lumeri's existing v3 tool loop.

The adapter deliberately treats the Codex CLI as the credential boundary:
Lumeri never reads or copies ``~/.codex/auth.json``.  Every invocation runs in
an empty temporary directory with a read-only sandbox and returns one
OpenAI-compatible assistant/tool-call batch to :class:`AgentLoopV3`.
"""
from __future__ import annotations

import asyncio
import base64
import json
import os
import shutil
import subprocess
import tempfile
import uuid
from pathlib import Path
from typing import Any, AsyncIterator


_LOGIN_TIMEOUT_SECONDS = 8
_TURN_TIMEOUT_SECONDS = 240


def _codex_executable() -> str | None:
    return shutil.which("codex")


def _process_command(executable: str, args: list[str]) -> list[str]:
    """Build a cross-platform argv, including npm's ``codex.cmd`` shim."""
    if os.name == "nt" and Path(executable).suffix.lower() in {".cmd", ".bat"}:
        comspec = os.environ.get("COMSPEC") or "cmd.exe"
        return [comspec, "/d", "/s", "/c", subprocess.list2cmdline([executable, *args])]
    return [executable, *args]


def _run_codex(args: list[str], *, timeout: int = _LOGIN_TIMEOUT_SECONDS) -> subprocess.CompletedProcess[str]:
    executable = _codex_executable()
    if not executable:
        raise FileNotFoundError("Codex CLI is not installed or is not on PATH")
    creationflags = getattr(subprocess, "CREATE_NO_WINDOW", 0) if os.name == "nt" else 0
    return subprocess.run(
        _process_command(executable, args),
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=timeout,
        creationflags=creationflags,
        check=False,
    )


def subscription_status() -> dict[str, Any]:
    """Return a credential-free status summary from ``codex login status``."""
    executable = _codex_executable()
    if not executable:
        return {
            "installed": False,
            "authenticated": False,
            "auth_method": "none",
            "message": "未检测到 Codex CLI",
        }
    try:
        version_run = _run_codex(["--version"])
        login_run = _run_codex(["login", "status"])
    except (OSError, subprocess.SubprocessError) as exc:
        return {
            "installed": True,
            "authenticated": False,
            "auth_method": "unknown",
            "message": f"Codex 状态检测失败：{type(exc).__name__}",
        }

    version = (version_run.stdout or version_run.stderr or "").strip().splitlines()
    combined = f"{login_run.stdout}\n{login_run.stderr}".strip().lower()
    chatgpt = login_run.returncode == 0 and "chatgpt" in combined and "logged in" in combined
    api_key = login_run.returncode == 0 and "api key" in combined and "logged in" in combined
    if chatgpt:
        method = "chatgpt"
        message = "已登录 ChatGPT 订阅"
    elif api_key:
        method = "api_key"
        message = "当前 Codex 使用 API Key，不是 ChatGPT 订阅"
    else:
        method = "none"
        message = "尚未登录 ChatGPT 订阅"
    return {
        "installed": True,
        "authenticated": chatgpt,
        "auth_method": method,
        "version": version[0][:80] if version else "",
        "message": message,
    }


def launch_login() -> dict[str, Any]:
    """Open a user-visible Windows terminal running the fixed login command."""
    executable = _codex_executable()
    if not executable:
        return {"ok": False, "error": "未检测到 Codex CLI，请先按页面说明安装"}
    if os.name != "nt":
        return {"ok": False, "error": "请在终端运行 codex login"}

    comspec = os.environ.get("COMSPEC") or "cmd.exe"
    command_line = subprocess.list2cmdline([executable, "login"])
    creationflags = getattr(subprocess, "CREATE_NEW_CONSOLE", 0)
    try:
        subprocess.Popen(
            [comspec, "/d", "/s", "/k", command_line],
            close_fds=True,
            creationflags=creationflags,
        )
    except OSError as exc:
        return {"ok": False, "error": f"无法打开 Codex 登录窗口：{type(exc).__name__}"}
    return {"ok": True, "message": "已打开 Codex 登录窗口"}


def _output_schema() -> dict[str, Any]:
    return {
        "$schema": "http://json-schema.org/draft-07/schema#",
        "type": "object",
        "properties": {
            "text": {"type": "string"},
            "tool_calls": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "name": {"type": "string"},
                        "arguments_json": {"type": "string"},
                    },
                    "required": ["name", "arguments_json"],
                    "additionalProperties": False,
                },
            },
        },
        "required": ["text", "tool_calls"],
        "additionalProperties": False,
    }


def _materialize_messages(messages: list[dict[str, Any]], root: Path) -> tuple[list[dict[str, Any]], list[Path]]:
    """Replace inline image data with labels and Codex ``--image`` files."""
    clean = json.loads(json.dumps(messages))
    images: list[Path] = []
    for message in clean:
        content = message.get("content")
        if not isinstance(content, list):
            continue
        for part in content:
            if not isinstance(part, dict) or part.get("type") != "image_url":
                continue
            raw = part.get("image_url")
            url = raw.get("url") if isinstance(raw, dict) else raw
            if not isinstance(url, str) or not url.startswith("data:image/") or ";base64," not in url:
                part.clear()
                part.update({"type": "text", "text": "[图片无法作为本机附件读取]"})
                continue
            header, encoded = url.split(",", 1)
            ext = header.split("data:image/", 1)[1].split(";", 1)[0].lower()
            ext = "jpg" if ext == "jpeg" else ext
            if ext not in {"png", "jpg", "webp", "gif"}:
                ext = "png"
            try:
                payload = base64.b64decode(encoded, validate=True)
            except ValueError:
                payload = b""
            if not payload:
                part.clear()
                part.update({"type": "text", "text": "[图片附件解码失败]"})
                continue
            path = root / f"input-{len(images) + 1}.{ext}"
            path.write_bytes(payload)
            images.append(path)
            part.clear()
            part.update({"type": "text", "text": f"[已附加图片 {len(images)}]"})
    return clean, images


def _prompt(messages: list[dict[str, Any]], tools: list[dict[str, Any]]) -> str:
    return (
        "You are the model backend inside Lumeri Video's existing v3 agent loop. "
        "Do not run commands, inspect files, edit files, browse, or use any built-in Codex tools. "
        "Use only the Lumeri function definitions supplied below. Return exactly one JSON object "
        "matching the requested output schema. Put user-facing prose in text. Put zero or more "
        "requested Lumeri calls in tool_calls, encoding each call's arguments object as compact JSON "
        "inside arguments_json. Never invent a tool name. The host executes calls "
        "and will provide their results in a later transcript.\n\n"
        f"LUMERI_TOOLS={json.dumps(tools, ensure_ascii=False, separators=(',', ':'))}\n\n"
        f"TRANSCRIPT={json.dumps(messages, ensure_ascii=False, separators=(',', ':'))}"
    )


def _parse_result(raw: str, allowed_tools: set[str]) -> tuple[str, list[dict[str, Any]]]:
    text = raw.strip()
    if text.startswith("```"):
        lines = text.splitlines()
        if lines and lines[0].startswith("```"): lines = lines[1:]
        if lines and lines[-1].strip() == "```": lines = lines[:-1]
        text = "\n".join(lines).strip()
    data = json.loads(text)
    if not isinstance(data, dict):
        raise ValueError("Codex returned a non-object response")
    answer = str(data.get("text") or "")
    calls: list[dict[str, Any]] = []
    for item in data.get("tool_calls") or []:
        if not isinstance(item, dict):
            continue
        name = str(item.get("name") or "")
        arguments_raw = item.get("arguments_json")
        try:
            arguments = json.loads(arguments_raw) if isinstance(arguments_raw, str) else None
        except json.JSONDecodeError:
            arguments = None
        if name in allowed_tools and isinstance(arguments, dict):
            calls.append({"name": name, "arguments": arguments})
    return answer, calls


def _safe_process_error(stderr: bytes, returncode: int) -> str:
    """Map Codex diagnostics to creator-readable text without echoing prompts."""
    lowered = stderr.decode("utf-8", errors="replace").lower()
    if "usage limit" in lowered or "usagelimitexceeded" in lowered:
        return "Codex 订阅额度暂时用尽，请稍后重试或切换模型"
    if "unauthorized" in lowered or '"status": 401' in lowered:
        return "Codex 登录已失效，请重新运行 codex login"
    if "model" in lowered and ("not found" in lowered or "not available" in lowered):
        return "当前 ChatGPT 订阅不支持所选模型，请留空使用 Codex 默认模型"
    if "invalid_json_schema" in lowered:
        return "当前 Codex 版本不支持 Lumeri 所需的结构化响应，请升级 Codex CLI"
    return f"Codex 调用未完成（退出码 {returncode}）"


class CodexSubscriptionClient:
    """Translate one Codex subscription run into the v3 streaming protocol."""

    provider = "codex_subscription"

    def __init__(self, *, model: str = "", reasoning_effort: str = "medium", timeout: float = _TURN_TIMEOUT_SECONDS) -> None:
        self.model = model.strip()
        self.reasoning_effort = reasoning_effort.strip().lower() or "medium"
        self.timeout = float(timeout)

    async def stream_turn(
        self,
        messages: list[dict[str, Any]],
        *,
        tools: list[dict[str, Any]] | None = None,
        temperature: float | None = None,
    ) -> AsyncIterator[dict[str, Any]]:
        del temperature
        status = await asyncio.to_thread(subscription_status)
        if not status.get("authenticated"):
            yield {"kind": "error", "error": status.get("message") or "Codex subscription is not signed in"}
            return

        tool_defs = tools or []
        allowed = {
            str(item.get("function", {}).get("name") or "")
            for item in tool_defs
            if isinstance(item, dict)
        }
        try:
            with tempfile.TemporaryDirectory(prefix="lumeri-codex-") as tmp:
                root = Path(tmp)
                clean_messages, images = _materialize_messages(messages, root)
                schema_path = root / "output-schema.json"
                output_path = root / "last-message.json"
                schema_path.write_text(json.dumps(_output_schema()), encoding="utf-8")

                executable = _codex_executable()
                if not executable:
                    raise FileNotFoundError("Codex CLI is not installed or is not on PATH")
                args = [
                    "exec", "--ephemeral", "--ignore-user-config", "--skip-git-repo-check",
                    "--sandbox", "read-only", "--color", "never", "-C", str(root),
                    "--output-schema", str(schema_path), "--output-last-message", str(output_path),
                ]
                if self.model:
                    args.extend(["--model", self.model])
                if self.reasoning_effort in {"low", "medium", "high", "xhigh"}:
                    args.extend(["-c", f'model_reasoning_effort="{self.reasoning_effort}"'])
                for image in images:
                    args.extend(["--image", str(image)])
                args.append("-")

                creationflags = getattr(subprocess, "CREATE_NO_WINDOW", 0) if os.name == "nt" else 0
                proc = await asyncio.create_subprocess_exec(
                    *_process_command(executable, args),
                    stdin=asyncio.subprocess.PIPE,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                    creationflags=creationflags,
                )
                try:
                    _stdout, stderr = await asyncio.wait_for(
                        proc.communicate(_prompt(clean_messages, tool_defs).encode("utf-8")),
                        timeout=self.timeout,
                    )
                except asyncio.TimeoutError:
                    proc.kill()
                    await proc.wait()
                    raise TimeoutError("本机 Codex 响应超时")
                if proc.returncode != 0:
                    raise RuntimeError(_safe_process_error(stderr, proc.returncode))
                if not output_path.exists():
                    raise RuntimeError("Codex 没有返回可读取的结果")
                answer, calls = _parse_result(output_path.read_text(encoding="utf-8"), allowed)
        except Exception as exc:
            detail = str(exc).strip() or type(exc).__name__
            yield {"kind": "error", "error": f"本机 Codex 调用失败：{type(exc).__name__}: {detail}"}
            return

        if answer:
            yield {"kind": "text_delta", "text": answer}
        for index, call in enumerate(calls):
            call_id = f"codex_{uuid.uuid4().hex[:20]}"
            yield {"kind": "tool_call_start", "index": index, "id": call_id, "name": call["name"]}
            yield {
                "kind": "tool_call_args_delta",
                "index": index,
                "delta": json.dumps(call["arguments"], ensure_ascii=False, separators=(",", ":")),
            }
        yield {"kind": "finish", "reason": "tool_calls" if calls else "stop"}
