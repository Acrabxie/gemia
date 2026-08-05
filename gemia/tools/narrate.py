"""narrate -- turn a line of script into spoken voiceover (human-voice TTS).

Fills the narration gap: ``generate_audio`` only makes Lyria *music*, so there
was no way to voice a script. This synthesizes speech from text and registers it
as an audio asset, returning its measured duration — the hook for pacing a cut
to the voiceover ("按旁白配节奏").

Backend: the local macOS ``say`` engine (offline, no API key, $0), via
``gemia.audio.effects.text_to_speech``. It is reliable and deterministic; a
higher-fidelity cloud voice can slot in behind the same tool interface later.
Pass ``voice`` to pick a specific system voice (e.g. English ``Ava``/``Samantha``,
Chinese ``Tingting``/``Meijia``); omit it to use the system default.
"""
from __future__ import annotations

from typing import Any

from gemia.tools._context import ProgressUpdate, ToolContext
from gemia.tools._ffmpeg import ffprobe_duration


def _short(text: str, n: int = 48) -> str:
    text = " ".join(text.split())
    return text if len(text) <= n else text[: n - 1] + "…"


async def dispatch(args: dict[str, Any], ctx: ToolContext) -> dict[str, Any]:
    text = str(args.get("text") or "").strip()
    if not text:
        raise ValueError("narrate requires non-empty 'text' to speak")
    voice = str(args.get("voice") or "auto").strip() or "auto"
    rate = args.get("rate")
    try:
        rate = int(rate) if rate not in (None, "") else 175
    except (TypeError, ValueError):
        raise ValueError(f"narrate rate must be an integer words-per-minute, got {rate!r}")
    rate = max(80, min(400, rate))

    from gemia.audio.effects import text_to_speech

    new_id = ctx.registry.allocate_id("audio")
    out_path = ctx.child_path(new_id, ".wav")
    ctx.emit_progress(ProgressUpdate(percent=10, message=f"synthesizing voiceover ({voice})", eta_sec=3))
    try:
        text_to_speech(text, str(out_path), voice=voice, rate=rate)
    except RuntimeError as exc:
        # say/espeak reported failure (e.g. unknown voice) — surface it, don't swallow.
        raise ValueError(f"narrate failed: {exc}") from exc
    if not out_path.exists() or out_path.stat().st_size == 0:
        raise ValueError("narrate produced no audio (no TTS backend available: need macOS 'say' or espeak)")

    duration = round(float(ffprobe_duration(out_path)), 3)
    ctx.emit_progress(ProgressUpdate(percent=100, message="voiceover ready", eta_sec=0))
    record = ctx.registry.register_output(
        new_id,
        kind="audio",
        path=out_path,
        summary=f"voiceover ({voice}, {duration:.1f}s): {_short(text)!r}",
        lineage=(),
    )
    return {
        "asset_id": new_id,
        "summary": record.summary,
        "metadata": {
            "provider": "local_tts",
            "voice": voice,
            "rate_wpm": rate,
            "duration_sec": duration,
            "word_count": len(text.split()),
        },
    }


__all__ = ["dispatch"]
