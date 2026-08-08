"""Model-stream and tool-protocol support for gemia.agent_loop_v3.

This module is intentionally stateless. The public loop module re-exports the
compatibility symbols that existing tests and integrations import directly.
"""

from __future__ import annotations

import base64
import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

_PROMPT_PATH = Path(__file__).resolve().parent / "prompts" / "system_v3.md"


def _strip_gate_images(msg: dict[str, Any]) -> None:
    """Replace image parts of any already-consumed one-shot message with a
    compact text placeholder, IN PLACE.

    The model must see each thumbnail exactly once. Left as-is, the base64
    payload would ride the rolling window
    into every subsequent model call — bandwidth and tokens spent re-sending
    stale pixels. Text sections are preserved verbatim."""
    content = msg.get("content")
    if not isinstance(content, list):
        return
    n_images = sum(1 for p in content if p.get("type") == "image_url")
    if n_images == 0:
        return
    texts = [p.get("text", "") for p in content if p.get("type") == "text"]
    texts.append(f"[{n_images} 张预览图已发送一次并从上下文回收]")
    msg["content"] = "\n\n".join(t for t in texts if t)


# Failure-direction nudge: every failed tool call immediately asks the model to
# decide whether retrying the current direction is justified by the structured
# error. This is guidance, never a host-side stop condition. The legacy
# threshold name remains for compatibility, but intentionally means "first
# failure".
_REPEATED_FAILURE_NUDGE_THRESHOLD = 1
# Backward-compatible constant name for older imports/tests. It is no longer a
# host-side maximum.
_MAX_CONSECUTIVE_TOOL_FAILURES = _REPEATED_FAILURE_NUDGE_THRESHOLD
# Transient failures receive the same immediate direction check. The prompt
# still permits a retry when the error itself provides concrete transient
# evidence.
_TRANSIENT_RETRY_NUDGE_THRESHOLD = 1

# A dropped model stream is a transport failure, not a completed turn. Retry
# the whole stream six times after the initial attempt, with exponential
# backoff. Each retry starts from the same settled conversation state; partial
# text/tool-call frames from the failed attempt are reset before reconnecting.
_MODEL_STREAM_RECONNECT_RETRIES = 6
_MODEL_STREAM_RECONNECT_BASE_DELAY_SEC = 2.0
_MODEL_STREAM_RECONNECT_MAX_DELAY_SEC = 64.0

_RETRYABLE_MODEL_STREAM_MARKERS = (
    "broken pipe",
    "connection",
    "eof",
    "mid-frame",
    "network",
    "read operation timed out",
    "reset",
    "response_failed",
    "service unavailable",
    "sslerror",
    "stream ended",
    "stream failure",
    "temporarily unavailable",
    "timeout",
    "timed out",
    "urlerror",
)


def _model_stream_retry_delay(retry_number: int) -> float:
    """Return the increasing delay before retry ``retry_number`` (1-based)."""
    return min(
        _MODEL_STREAM_RECONNECT_BASE_DELAY_SEC * (2 ** max(0, retry_number - 1)),
        _MODEL_STREAM_RECONNECT_MAX_DELAY_SEC,
    )


def _is_retryable_model_stream_error(error: str) -> bool:
    """True only for connection/rate-limit/server failures worth reconnecting."""
    normalized = str(error or "").strip().lower()
    if not normalized:
        return True
    if re.search(r"\bhttp\s+(?:429|500|502|503|504)\b", normalized):
        return True
    return any(marker in normalized for marker in _RETRYABLE_MODEL_STREAM_MARKERS)


def _model_stream_error_class(error: str) -> str:
    """Return a bounded, non-sensitive category for retry telemetry."""
    normalized = str(error or "").strip().lower()
    if re.search(r"\bhttp\s+429\b", normalized):
        return "rate_limit"
    if re.search(r"\bhttp\s+(?:500|502|503|504)\b", normalized):
        return "server"
    if "timeout" in normalized or "timed out" in normalized:
        return "timeout"
    if "reset" in normalized or "broken pipe" in normalized:
        return "connection_reset"
    if "eof" in normalized or "stream ended" in normalized or "mid-frame" in normalized:
        return "stream_ended"
    if "stream failure" in normalized:
        return "stream_failure"
    if "response_failed" in normalized or "response failed" in normalized:
        return "response_failed"
    if "connection" in normalized or "network" in normalized or "sslerror" in normalized:
        return "connection"
    return "other"


# Success-BLIND doom-loop guard (ported from opencode processor.ts,
# DOOM_LOOP_THRESHOLD=3). The per-(tool, error_code) nudge above only tracks
# FAILURES. But a loop can also get stuck repeating a call that keeps
# "succeeding" — or whose result the model ignores — and re-issuing the exact
# same tool with byte-identical arguments forever. That is not progress, it is a
# stuck model echoing itself. Independent of success/failure: if the last
# ``_DOOM_LOOP_THRESHOLD`` tool calls in a turn are the SAME tool name with the
# SAME (byte-identical) raw args JSON, the turn is looping on itself — emit a
# structured turn_error and stop. Distinct args (real progress / different work)
# never trip it.
_DOOM_LOOP_THRESHOLD = 3

# Job-polling verbs are exempt from the doom-loop guard: three identical
# check_job(job_id=X) calls in a row are legitimate polling of a background
# job, not a stuck model. Their spam ceiling is handled by prompt guidance
# ("don't busy-poll; completion is announced") + the budget guard instead.
_DOOM_LOOP_EXEMPT_TOOLS = frozenset({"check_job", "wait_for_job", "kill_job"})

# Post-edit self-correction (ported from opencode pattern #2: append LSP
# diagnostics to a tool_result right after an edit). After a SUCCESSFUL
# *mutating* lumenframe verb, we append a compact POST-STATE digest (the
# resulting layer-tree summary + any lumenframe validate_doc warnings) to that
# tool's tool_result text the model reads next. This grounds the model in the
# new layer state at the exact moment it edits, so it self-corrects instead of
# editing blind. A "mutating" lumen verb is any tool whose name starts with
# "lumen_" EXCEPT the read-only ones below.
_LUMEN_TOOL_PREFIX = "lumen_"
_LUMEN_READONLY_TOOLS = frozenset({"lumen_get", "lumen_render"})


def _is_mutating_lumen_tool(name: str) -> bool:
    """True for a lumenframe verb that changes the layer document.

    Mutating == tool name starts with ``lumen_`` and is NOT one of the
    read-only verbs (``lumen_get``, ``lumen_render``). The read-only get verb
    is actually registered as ``get_lumenframe`` (no ``lumen_`` prefix) so it is
    excluded automatically; ``lumen_render`` is excluded explicitly because it
    only rasterises and does not edit the tree.
    """
    return name.startswith(_LUMEN_TOOL_PREFIX) and name not in _LUMEN_READONLY_TOOLS


# Tools whose children ALREADY settle their own seconds via BudgetGuard
# reservation/settlement (gemia/subtasks.py). Committing the batch wall-clock on
# top of that would double-count, so the loop commits 0.0 seconds for them and
# lets the children's settlements be the truth (docs/multi-agent-plan.md §5.3).
# The ~1 s orchestration overhead is covered by the tool's _TOOL_COSTS eta row.
_SELF_SETTLING_TOOLS = frozenset({"spawn_subtasks"})


def _commit_seconds(tool_name: str, elapsed: float) -> float:
    """Wall-elapsed seconds to commit for ``tool_name``. Zero for self-settling
    tools (their children already committed the real seconds)."""
    return 0.0 if tool_name in _SELF_SETTLING_TOOLS else elapsed


# ──────────────────────────────────────────────────────────────────────
# Stream accumulators (one stream = one model call)
# ──────────────────────────────────────────────────────────────────────


@dataclass
class _ToolCallAccumulator:
    """One tool call accumulated across many stream chunks."""

    index: int
    id: str
    name: str
    extra_content: Any | None = None
    args_buf: list[str] = field(default_factory=list)

    @property
    def args(self) -> str:
        return "".join(self.args_buf)


@dataclass
class _StreamAccumulator:
    """Everything collected from one model stream until it ends."""

    text_buf: list[str] = field(default_factory=list)
    raw_text_buf: list[str] = field(default_factory=list)
    tool_calls_by_index: dict[int, _ToolCallAccumulator] = field(default_factory=dict)
    finish_reason: str | None = None

    @property
    def text(self) -> str:
        return "".join(self.text_buf)

    @property
    def raw_text(self) -> str:
        return "".join(self.raw_text_buf)

    @property
    def tool_calls(self) -> list[_ToolCallAccumulator]:
        return [self.tool_calls_by_index[k] for k in sorted(self.tool_calls_by_index)]


@dataclass
class _DisplayStreamGate:
    """Release real model deltas unless they form a tool UI preamble.

    The first few chunks stay buffered only long enough to distinguish normal
    user-facing prose from the structured ``<report>`` / ``<activity>`` prefix.
    Once normal prose is established, every subsequent chunk passes through
    unchanged for the SSE client to render immediately.
    """

    pending: list[str] = field(default_factory=list)
    streamable: bool = False
    withheld: bool = False
    emitted: bool = False

    def feed(self, delta: str) -> list[str]:
        if not delta or self.withheld:
            return []
        if self.streamable:
            self.emitted = True
            return [delta]

        self.pending.append(delta)
        candidate = "".join(self.pending)
        folded = candidate.lstrip().casefold()
        if not folded:
            return []

        openers = ("<report>", "<activity>")
        if any(opener.startswith(folded) for opener in openers):
            return []
        if any(folded.startswith(opener) for opener in openers):
            self.withheld = True
            self.pending.clear()
            return []

        self.streamable = True
        self.emitted = True
        self.pending.clear()
        return [candidate]


# ──────────────────────────────────────────────────────────────────────
# Pure helpers
# ──────────────────────────────────────────────────────────────────────


# Model-authored mid-turn UI copy is deliberately opt-in and narrow. A tool
# preamble may contain one occasional descriptive report followed by the short
# activity label for the next batch; no unstructured prose is accepted.
_UI_PREAMBLE_RE = re.compile(
    r"^\s*(?:<report>(?P<report>[^<>\r\n]+)</report>\s*)?"
    r"<activity>(?P<activity>[^<>\r\n]+)</activity>\s*$",
    re.IGNORECASE,
)
_UI_COPY_BLOCK_RE = re.compile(
    r"<(?:activity|report)\b[^>]*>[\s\S]*?</(?:activity|report)\s*>",
    re.IGNORECASE,
)
_ACTIVITY_UNSAFE_RE = re.compile(
    r"(?:[`{}\[\]<>\\]|[=;]|(?:https?|file)://|"
    r"(?:^|\s)(?:/|~/|[A-Za-z]:[\\/])|"
    r"\b[\w.-]+\.(?:py|js|jsx|ts|tsx|json|md|yaml|yml|sh|bash|zsh|html|css|sql)\b|"
    r"\b[a-z][a-z0-9]*_[a-z0-9_]+\b|"
    r"\b(?:api[_-]?key|token|password|secret|system[_ -]?prompt|"
    r"reasoning|thought[_ -]?signature|asset[_ -]?id)\b|"
    r"(?:代码|路径|工具名?|参数|命令|思维链|推理|内部))",
    re.IGNORECASE,
)
_ACTIVITY_TEXT_MAX_CHARS = 72
_PROGRESS_REPORT_MAX_CHARS = 240
_MANUAL_MEDIA_IMPORT_RE = re.compile(
    r"(?:手动.{0,12}(?:导入|拖入|拖进)|"
    r"(?:拖入|拖进).{0,12}素材库|"
    r"(?:没有|未|不).{0,16}(?:提供|具备|支持).{0,16}(?:导入能力|导入接口|素材导入)|"
    r"(?:导入能力|导入接口|素材导入).{0,16}(?:不可用|缺失|不可访问)|"
    r"(?:cannot|can't|unable to).{0,20}import|"
    r"import.{0,20}(?:unavailable|not available)|"
    r"please.{0,20}(?:import|drag).{0,20}(?:media|library))",
    re.IGNORECASE,
)
_LOCAL_MEDIA_PATH_RE = re.compile(
    r"(?P<path>/[^\r\n`\"'<>]*?\.(?:mp4|mov|m4v|webm|mkv|avi|png|jpe?g|webp|gif|wav|mp3|m4a|aac|flac))"
    r"(?=$|[\s`\"'，。；;、)])",
    re.IGNORECASE,
)


def _safe_model_ui_text(
    value: str,
    *,
    max_chars: int,
    tool_names: list[str] | tuple[str, ...] = (),
) -> str | None:
    candidate = " ".join(str(value or "").split())
    if not candidate or len(candidate) > max_chars:
        return None
    if _ACTIVITY_UNSAFE_RE.search(candidate):
        return None
    folded = candidate.casefold()
    if any(name and str(name).casefold() in folded for name in tool_names):
        return None
    return candidate


def _activity_text_from_model_preamble(
    text: str, *, tool_names: list[str] | tuple[str, ...] = ()
) -> str | None:
    """Return one safe, model-authored activity label or ``None``.

    The activity tag is protocol text written by the model, never a host
    summary.  Reject rather than repair anything technical so no code, path,
    argument, ID, or hidden reasoning can reach the display layer.
    """
    match = _UI_PREAMBLE_RE.fullmatch(str(text or ""))
    if match is None:
        return None
    return _safe_model_ui_text(
        match.group("activity"),
        max_chars=_ACTIVITY_TEXT_MAX_CHARS,
        tool_names=tool_names,
    )


def _progress_report_from_model_preamble(
    text: str, *, tool_names: list[str] | tuple[str, ...] = ()
) -> str | None:
    """Return one safe, model-authored mid-turn progress report or ``None``."""
    match = _UI_PREAMBLE_RE.fullmatch(str(text or ""))
    if match is None or not match.group("report"):
        return None
    return _safe_model_ui_text(
        match.group("report"),
        max_chars=_PROGRESS_REPORT_MAX_CHARS,
        tool_names=tool_names,
    )


def _strip_activity_markup(text: str) -> str:
    """Keep mid-turn UI protocol text out of history and final prose."""
    without_blocks = _UI_COPY_BLOCK_RE.sub("", str(text or ""))
    return "\n".join(
        line
        for line in without_blocks.splitlines()
        if not re.search(r"</?(?:activity|report)\b", line, re.IGNORECASE)
    ).strip()


def _manual_media_import_path(
    response_text: str,
    *,
    request_text: str = "",
) -> Path | None:
    """Return a validated local media path from a manual-import deflection.

    This is intentionally narrow: ordinary advice remains a natural model
    stop. Recovery applies only when the model asks the local user to perform
    an import that Lumeri's active ``copy_in`` tool can do itself.
    """
    response = str(response_text or "")
    if not _MANUAL_MEDIA_IMPORT_RE.search(response):
        return None
    # The model may omit the path from its refusal even though the current
    # user request supplied it. Only inspect these two turn-local texts: do
    # not guess from older session history or import an unrelated file.
    for value in (response, str(request_text or "")):
        match = _LOCAL_MEDIA_PATH_RE.search(value)
        if match is None:
            continue
        path = Path(match.group("path")).expanduser()
        try:
            resolved = path.resolve(strict=True)
        except (OSError, RuntimeError):
            continue
        if resolved.is_file():
            return resolved
    return None


def _load_system_template() -> str:
    return _PROMPT_PATH.read_text(encoding="utf-8")


def _looks_like_model_identity_question(text: str) -> bool:
    """Heuristic guard for direct questions about the assistant/model itself.

    These turns are explanatory conversation, not media-creation briefs. The
    prompt already tells the model this, but a compact host-side nudge in the
    recency slot helps when long history or synthetic user notices would
    otherwise pull attention back toward an old creation task.
    """
    s = str(text or "").strip().lower()
    if not s:
        return False
    keys = [
        "你叫什么",
        "你叫啥",
        "你是谁",
        "你能做什么",
        "你可以帮我做什么",
        "what can you do",
        "who are you",
        "your name",
        "什么模型",
        "什么ai",
        "什么引擎",
        "哪个模型",
        "哪个ai",
        "哪个引擎",
        "what model",
        "which model",
        "what engine",
        "which engine",
        "what ai",
        "which ai",
        "runtime engine",
    ]
    return any(k in s for k in keys)


def _parse_args(raw: str) -> tuple[dict[str, Any] | None, str | None]:
    """Parse JSON tool-call args. Returns (parsed, None) or (None, error_message)."""
    text = (raw or "").strip()
    if not text:
        return {}, None
    try:
        value = json.loads(text)
    except json.JSONDecodeError as exc:
        return None, f"JSONDecodeError: {exc}"
    if not isinstance(value, dict):
        return None, f"tool args must be a JSON object, got {type(value).__name__}"
    return value, None


def _tool_call_message(tc: _ToolCallAccumulator) -> dict[str, Any]:
    message = {
        "id": tc.id,
        "type": "function",
        "function": {"name": tc.name, "arguments": tc.args},
    }
    if tc.extra_content is not None:
        # Some providers' tool calls carry metadata such as thought_signature
        # (e.g. Gemini via OpenRouter). The follow-up request must echo it on
        # the assistant tool_call part, or the provider rejects the next call.
        message["extra_content"] = tc.extra_content
    return message


def _thumbnail_user_content(paths: list[Path]) -> list[dict[str, Any]]:
    parts: list[dict[str, Any]] = [
        {
            "type": "text",
            "text": "Thumbnails for the analyze_media call(s) you just made are attached below.",
        }
    ]
    for p in paths:
        data = Path(p).read_bytes()
        b64 = base64.b64encode(data).decode("ascii")
        parts.append(
            {
                "type": "image_url",
                "image_url": {"url": f"data:image/png;base64,{b64}"},
            }
        )
    return parts
