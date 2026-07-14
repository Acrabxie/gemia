"""End-to-end self-correction arc on the zero-key FFmpeg path.

This is the "advanced agent" moment, made deterministic: a scripted model
asks color_grade for an unavailable look, the host fails honestly with a
typed error + valid_options, and the model reads it and self-corrects to the
appropriate zero-saturation tool — which actually runs through ffmpeg and
produces a file. Only the
model's token output is scripted; the error typing, the feedback, the real
grading, and the SSE event sequence the frontend groups into an arc are all
real. No API keys, usd=0.
"""
from __future__ import annotations

import asyncio
import json
import shutil
import subprocess
from pathlib import Path
from typing import Any, AsyncIterator

import pytest

from gemia.agent_loop_v3 import AgentLoopV3

pytestmark = pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="ffmpeg not available")


class _ScriptedColorFix:
    """Asks for an unavailable look, then — having seen the typed error fed
    back as a tool_result — diagnoses and switches to a real monochrome
    operation."""

    model = "fake"

    def __init__(self) -> None:
        self.calls = 0
        self.asset_id = "v_001"

    async def stream_turn(
        self, messages: list[dict[str, Any]], *, tools=None, temperature: float = 0.7
    ) -> AsyncIterator[dict[str, Any]]:
        del messages, tools, temperature
        self.calls += 1
        if self.calls == 1:
            yield {"kind": "tool_call_start", "index": 0, "id": "c1", "name": "color_grade"}
            yield {"kind": "tool_call_args_delta", "index": 0,
                   "delta": json.dumps({"asset_id": self.asset_id, "look": "black and white"})}
            yield {"kind": "finish", "reason": "tool_calls"}
            return
        if self.calls == 2:
            # The diagnosis line — streamed right before the corrective call, so
            # the frontend attaches it to the retry card.
            yield {"kind": "text_delta",
                   "text": "That look is unsupported; applying true zero saturation instead."}
            yield {"kind": "tool_call_start", "index": 0, "id": "c2", "name": "adjust_media"}
            yield {"kind": "tool_call_args_delta", "index": 0,
                   "delta": json.dumps({"asset_id": self.asset_id, "saturation": 0.0})}
            yield {"kind": "finish", "reason": "tool_calls"}
            return
        yield {"kind": "text_delta", "text": "Converted to black and white."}
        yield {"kind": "finish", "reason": "stop"}


def test_self_correction_arc_end_to_end_ffmpeg(tmp_path: Path) -> None:
    src = tmp_path / "src.mp4"
    subprocess.run(
        ["ffmpeg", "-y", "-f", "lavfi",
         "-i", "testsrc=duration=1:size=160x120:rate=10",
         "-pix_fmt", "yuv420p", str(src)],
        check=True, capture_output=True,
    )

    client = _ScriptedColorFix()
    events: list[dict[str, Any]] = []
    loop = AgentLoopV3(
        session_id="e2e_arc",
        output_dir=tmp_path,
        gemini_client=client,  # type: ignore[arg-type]
        emit_event=events.append,
    )
    client.asset_id = loop.add_external_asset(src, summary="test clip")

    asyncio.run(loop.run_turn("make it black and white"))

    # 1) The first attempt failed honestly with a typed, fixable error.
    errs = [e for e in events if e.get("kind") == "tool_exec_error"]
    assert len(errs) == 1
    assert errs[0]["tool_name"] == "color_grade"
    assert errs[0]["error_code"] == "E_UNSUPPORTED"
    assert "cool" in errs[0]["valid_options"]

    # 2) The corrective call really ran ffmpeg and produced a monochrome asset.
    results = [e for e in events if e.get("kind") == "tool_exec_result"
               and e.get("tool_name") == "adjust_media"]
    assert len(results) == 1
    produced = results[0]["result"]["asset_id"]
    assert Path(loop.registry.get(produced).path).exists()

    # 3) The correction is failure → semantically appropriate success.
    seq = [(e["kind"], e.get("tool_name")) for e in events
           if e["kind"] in ("tool_exec_error", "tool_exec_result")
           and e.get("tool_name") in {"color_grade", "adjust_media"}]
    assert seq == [
        ("tool_exec_error", "color_grade"),
        ("tool_exec_result", "adjust_media"),
    ]

    # 4) The turn completed cleanly — no breaker trip, no fabricated success.
    assert [e for e in events if e.get("kind") == "turn_complete"]
    assert not [e for e in events if e.get("kind") == "turn_error"]
