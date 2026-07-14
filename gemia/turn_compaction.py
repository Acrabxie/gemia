"""Protocol-safe, deterministic compaction for settled tool-call blocks."""
from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any, Iterable


@dataclass(frozen=True, slots=True)
class SettledBlock:
    start: int
    end: int
    call_ids: tuple[str, ...]
    tool_names: tuple[str, ...]
    has_provider_signature: bool = False


@dataclass(frozen=True, slots=True)
class CompactionResult:
    messages: list[dict[str, Any]]
    summaries: tuple[str, ...]
    removed_blocks: int
    estimated_tokens_before: int
    estimated_tokens_after: int


def estimate_message_tokens(messages: list[dict[str, Any]]) -> int:
    """Cheap deterministic estimate used only as a compaction threshold."""
    raw = json.dumps(messages, ensure_ascii=False, default=str, separators=(",", ":"))
    return max(1, (len(raw) + 3) // 4)


def settled_tool_blocks(messages: list[dict[str, Any]]) -> list[SettledBlock]:
    """Find complete assistant-call + result spans without splitting protocol pairs."""
    blocks: list[SettledBlock] = []
    for index, message in enumerate(messages):
        calls = message.get("tool_calls") if message.get("role") == "assistant" else None
        if not isinstance(calls, list) or not calls:
            continue
        call_ids = tuple(str(call.get("id") or "") for call in calls)
        if not all(call_ids):
            continue
        names = tuple(
            str((call.get("function") or {}).get("name") or "unknown")
            for call in calls
        )
        # A settled protocol block is only the assistant call plus its
        # contiguous matching tool results. Never scan through a later user or
        # system message looking for the next assistant: that can swallow the
        # next real request when an earlier turn hard-stopped after its tools.
        cursor = index + 1
        results: set[str] = set()
        while cursor < len(messages):
            item = messages[cursor]
            if item.get("role") != "tool":
                break
            result_id = str(item.get("tool_call_id") or "")
            if result_id not in call_ids:
                break
            results.add(result_id)
            cursor += 1
        end = cursor
        if not set(call_ids) <= results:
            continue
        has_signature = any(
            bool(call.get("extra_content") or call.get("thought_signature"))
            for call in calls
        )
        blocks.append(
            SettledBlock(
                start=index,
                end=end,
                call_ids=call_ids,
                tool_names=names,
                has_provider_signature=has_signature,
            )
        )
    return blocks


def compact_settled_tool_blocks(
    messages: list[dict[str, Any]],
    *,
    protected_call_ids: Iterable[str] = (),
    max_estimated_tokens: int = 8_000,
    max_settled_blocks: int = 10,
    keep_recent: int = 4,
) -> CompactionResult:
    """Collapse old complete blocks; fail-open callers can retain the input.

    Provider-signature blocks and caller-protected evidence are never removed.
    Recent blocks stay byte-for-byte intact. The returned summaries are intended
    for a host-owned TurnLedger/system-prompt slot, not a synthetic tool message.
    """
    before = estimate_message_tokens(messages)
    blocks = settled_tool_blocks(messages)
    if len(blocks) <= max_settled_blocks and before <= max_estimated_tokens:
        return CompactionResult(list(messages), (), 0, before, before)

    protected = {str(call_id) for call_id in protected_call_ids}
    old_blocks = blocks[:-max(0, keep_recent)] if keep_recent else blocks
    candidates = [
        block
        for block in old_blocks
        if not block.has_provider_signature and not (set(block.call_ids) & protected)
    ]
    if not candidates:
        return CompactionResult(list(messages), (), 0, before, before)

    remove_indices: set[int] = set()
    summaries: list[str] = []
    for ordinal, block in enumerate(candidates, 1):
        remove_indices.update(range(block.start, block.end))
        summaries.append(_summarize_block(messages, block, ordinal))

    compacted = [
        message for index, message in enumerate(messages) if index not in remove_indices
    ]
    after = estimate_message_tokens(compacted)
    return CompactionResult(
        compacted,
        tuple(summaries),
        len(candidates),
        before,
        after,
    )


def _summarize_block(
    messages: list[dict[str, Any]], block: SettledBlock, ordinal: int
) -> str:
    results: dict[str, Any] = {}
    for message in messages[block.start + 1 : block.end]:
        if message.get("role") != "tool":
            continue
        call_id = str(message.get("tool_call_id") or "")
        if call_id not in block.call_ids:
            continue
        content = message.get("content")
        try:
            results[call_id] = json.loads(content) if isinstance(content, str) else content
        except (TypeError, json.JSONDecodeError):
            results[call_id] = {"summary": str(content or "")[:120]}

    parts: list[str] = []
    for call_id, tool_name in zip(block.call_ids, block.tool_names):
        result = results.get(call_id)
        if isinstance(result, dict):
            code = result.get("error_code")
            status = result.get("status") or ("failure" if code or result.get("error") else "success")
            facts = []
            for key in ("asset_id", "asset_ids", "job_id", "duration_sec", "width", "height", "fps"):
                if key in result and result[key] not in (None, "", []):
                    facts.append(f"{key}={result[key]}")
            suffix = f" ({', '.join(facts)})" if facts else ""
            code_text = f"/{code}" if code else ""
            parts.append(f"{tool_name}:{status}{code_text}{suffix}")
        else:
            parts.append(f"{tool_name}:settled")
    text = f"#{ordinal} " + "; ".join(parts)
    return text[:500]


__all__ = [
    "CompactionResult",
    "SettledBlock",
    "compact_settled_tool_blocks",
    "estimate_message_tokens",
    "settled_tool_blocks",
]
