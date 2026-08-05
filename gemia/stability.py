"""Stability gate helpers for Lumeri task/runtime boundaries."""
from __future__ import annotations

import json
import os
import re
import traceback
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from gemia.errors import GemiaError, TaskCancelledError

TASK_STATUSES = {
    "planning",
    "running",
    "preview_ready",
    "artifact_ready",
    "needs_input",
    "failed",
    "succeeded",
    "cancelled",
}

TERMINAL_TASK_STATUSES = {"preview_ready", "artifact_ready", "failed", "succeeded", "cancelled"}
_ERROR_LOG_PATH = Path.home() / ".gemia" / "logs" / "error_firewall.jsonl"
_SECRET_RE = re.compile(
    r"(?i)(authorization|api[_-]?key|token|secret|password)(['\"\s:=]+)([A-Za-z0-9._\-+/=]{8,})"
)


def stability_gate_enabled() -> bool:
    return str(os.environ.get("LUMERI_STABILITY_GATE") or os.environ.get("GEMIA_STABILITY_GATE") or "1").lower() not in {
        "0",
        "false",
        "off",
        "no",
    }


def normalize_task_status(value: Any) -> str:
    status = str(value or "").strip().lower()
    aliases = {
        "asking": "needs_input",
        "ask": "needs_input",
        "queued": "planning",
        "executing": "running",
        "done": "succeeded",
        "success": "succeeded",
        "error": "failed",
        "warning": "running",
    }
    status = aliases.get(status, status)
    return status if status in TASK_STATUSES else "failed"


def error_envelope(
    exc: BaseException | str,
    *,
    context: str = "",
    recoverable: bool | None = None,
    next_action: str | None = None,
) -> dict[str, Any]:
    """Return a UI-safe error envelope and write technical detail locally."""
    text = str(exc).strip() or exc.__class__.__name__
    lower = text.lower()
    debug_id = f"dbg_{uuid.uuid4().hex[:10]}"
    code = getattr(exc, "code", "") if not isinstance(exc, str) else ""
    user_message = getattr(exc, "user_message", "") if not isinstance(exc, str) else ""

    if not code:
        if isinstance(exc, TaskCancelledError):
            code = "E_CANCELLED"
        elif "cannot open video" in lower or "could not read source video" in lower:
            code = "E_NOT_PLAYABLE_MEDIA"
        elif "no such file or directory" in lower and ("http://" in lower or "https://" in lower):
            code = "E_REMOTE_AS_LOCAL_FILE"
        elif "got multiple values for argument" in lower:
            code = "E_DUPLICATE_ARGUMENT"
        elif "veo" in lower or "openrouter" in lower:
            code = "E_PROVIDER_UNAVAILABLE"
        elif "timeout" in lower or "timed out" in lower or "超时" in text:
            code = "E_TIMEOUT"
        elif "ask" in lower and ("repeat" in lower or "反复" in text):
            code = "E_REPEATED_ASK"
        elif "not found" in lower or isinstance(exc, FileNotFoundError):
            code = "E_NOT_FOUND"
        elif isinstance(exc, GemiaError):
            code = exc.code
        else:
            code = "E_RUNTIME"

    if code == "E_PLAN_CONTRACT":
        user_message = ""
    if not user_message:
        user_message = _user_message_for(code, text)

    if recoverable is None:
        recoverable = code not in {"E_CANCELLED", "E_NOT_FOUND"}
    next_action = next_action or _next_action_for(code)
    detail_text = getattr(exc, "detail", "") if not isinstance(exc, str) else ""
    technical_detail = _redact(str(detail_text or text))
    envelope = {
        "error": user_message,
        "error_code": code,
        "user_message": user_message,
        "technical_detail": technical_detail,
        "recoverable": bool(recoverable),
        "next_action": next_action,
        "debug_id": debug_id,
    }
    _write_error_log(
        {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "debug_id": debug_id,
            "context": context,
            "error_code": code,
            "user_message": user_message,
            "technical_detail": technical_detail,
            "traceback": _redact("".join(traceback.format_exception(exc)) if isinstance(exc, BaseException) else ""),
        }
    )
    return envelope


def error_event(exc: BaseException | str, *, label: str = "执行没有完成", context: str = "") -> dict[str, Any]:
    envelope = error_envelope(exc, context=context)
    return {
        "id": f"evt_{uuid.uuid4().hex[:10]}",
        "phase": "error",
        "label": label,
        "detail": envelope["debug_id"],
        "status": "failed",
        "body": envelope["user_message"],
        "voice": "gemini",
        "created_at": datetime.now(timezone.utc).isoformat(),
        **envelope,
    }


def _user_message_for(code: str, detail: str) -> str:
    if code == "E_PLAN_CONTRACT":
        return "这次规划没有通过 Lumeri 的执行契约，所以我没有继续硬跑。通常是模型调用了未激活能力、未知能力，或把素材路径/模板参数写进了错误位置。"
    if code == "E_NOT_PLAYABLE_MEDIA":
        return "这次产物不是可播放视频，所以我没有把它塞进播放器。我会把它当成文档或计划保留，需要视频时改走本地图层小样。"
    if code == "E_REMOTE_AS_LOCAL_FILE":
        return "规划里把网络地址当成本地文件用了。我已经拦住这类路径；下一步会先下载素材，或改用本地占位小样。"
    if code == "E_DUPLICATE_ARGUMENT":
        return "这一步的参数被重复传入了。我先把任务停在安全状态，避免继续生成错误产物。"
    if code == "E_PROVIDER_UNAVAILABLE":
        return "外部生成服务这次没有返回可用结果。我会保留已有素材和小样；你也可以明确要求重试外部生成。"
    if code == "E_TIMEOUT":
        return "这次模型或外部服务响应超时了。本轮没有收到可用结果，但任务不会卡死。"
    if code == "E_REPEATED_ASK":
        return "我没有继续反复追问。这类请求最多确认一轮，剩下的参数会按默认值推进或给出清晰失败原因。"
    if code == "E_CANCELLED":
        return "任务已取消。已有素材和历史记录不会被删除。"
    if code == "E_NOT_FOUND":
        return "需要的文件或任务已经不存在。我没有把这个缺失路径当作可播放结果。"
    if code == "E_CONFIG":
        return str(detail or "配置还不完整，请先补齐模型或输出目录配置。")
    return "这一步没有完成。我已经把底层错误收进本地调试日志，没有把它当成可播放结果。"


def _next_action_for(code: str) -> str:
    if code == "E_PLAN_CONTRACT":
        return "按当前已激活能力重新规划，或明确开启需要的能力后再执行。"
    if code == "E_NOT_PLAYABLE_MEDIA":
        return "查看文档产物，或让 Lumeri 重新生成本地图层预览。"
    if code == "E_REMOTE_AS_LOCAL_FILE":
        return "先把远程素材下载/导入媒体库，再继续执行。"
    if code == "E_DUPLICATE_ARGUMENT":
        return "按默认参数重试，或把目标素材/参数写得更明确。"
    if code == "E_PROVIDER_UNAVAILABLE":
        return "先用本地预览推进；需要时再明确重试 Veo/OpenRouter。"
    if code == "E_REPEATED_ASK":
        return "把剩余关键约束写进下一条，或让系统按默认值继续。"
    if code == "E_CANCELLED":
        return "可以修改 prompt 后重新运行。"
    return "查看本地 debug_id 对应日志，或用更小的本地预览路径重试。"


def _redact(text: str) -> str:
    if not text:
        return ""
    return _SECRET_RE.sub(lambda m: f"{m.group(1)}{m.group(2)}[redacted]", text)


def _write_error_log(payload: dict[str, Any]) -> None:
    try:
        _ERROR_LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
        with _ERROR_LOG_PATH.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(payload, ensure_ascii=False) + "\n")
    except Exception:
        pass
