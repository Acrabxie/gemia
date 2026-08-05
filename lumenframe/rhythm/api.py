"""Agent-facing API — a rhythm brief in, a beat grid + beat-aligned cut plan out.

This is the **creative director**: the one layer that sees the whole brief. It
validates the tempo/metre, resolves the sync-pattern style + feelings + overrides
into the four axes, builds the exact beat grid, runs the style's taste-floor
pattern to get candidate cut positions, enforces the minimum-shot floor, assigns
clips to the resulting segments, and returns the ``score`` (grid + cut plan)
alongside an explainable ``plan``.

Brief shape (only ``bpm`` is required)::

    {"bpm": 128,
     "time_signature": [4, 4],           # default [4, 4]
     "sections": [{"name": "intro", "bars": 4},
                  {"name": "drop",  "bars": 8}],
     "clips": [{"id": "c1", "duration": 3.0}, ...],
     "style": "build_drop",              # sync pattern archetype or alias ("edm")
     "feeling": ["driving", "tight"],
     "energy": 0.8,                       # shortcut → energy axis override
     "sync": 0.9,                         # shortcut → tightness axis override
     "offset_ms": 20,                     # nudge the whole grid (audio latency)
     "total_duration": 30.0,             # cap the grid to the audio length
     "params": {"drive": 0.7},           # explicit axis overrides (win)
     "seed": 7}

Feedback (``adjust``) folds "more driving" / "更紧凑" into the brief and rebuilds
with the **same seed** — the cut plan is re-derived, never hand-patched.
"""
from __future__ import annotations

import math
from typing import Any

from lumenframe.craft import new_rng, stable_digest
from lumenframe.craft.determinism import round_floats
from lumenframe.rhythm import rhythm as R
from lumenframe.rhythm.params import SPACE, rhythm_vocab
from lumenframe.rhythm.styles import BOOK

#: Sane tempo envelope. Outside this a "bpm" is almost certainly a mistake
#: (a duration in seconds, a sample rate, …) and we refuse rather than emit a
#: nonsense grid.
MIN_BPM, MAX_BPM = 20.0, 400.0

DEFAULTS: dict[str, Any] = {
    "time_signature": [4, 4],
    "seed": 7,
}


class BriefError(ValueError):
    """Raised for a structurally unusable rhythm brief."""


def _time_signature(raw: Any) -> tuple[int, int]:
    ts = raw or DEFAULTS["time_signature"]
    if not isinstance(ts, (list, tuple)) or len(ts) != 2:
        raise BriefError("time_signature must be [numerator, denominator]")
    num, den = int(ts[0]), int(ts[1])
    if num <= 0 or den <= 0:
        raise BriefError(f"time_signature values must be positive, got {ts}")
    if den not in (1, 2, 4, 8, 16):
        raise BriefError(f"time_signature denominator {den} is not a note value (1/2/4/8/16)")
    return num, den


def _overrides(brief: dict[str, Any]) -> dict[str, float]:
    """Collect axis overrides from the ``params`` map + top-level shortcuts.

    ``energy`` and ``sync`` are the documented ergonomic shortcuts (``sync`` maps
    to the ``tightness`` axis); ``drive`` and ``build`` are accepted top-level too
    since they name domain axes directly. An explicit ``params`` entry always
    wins over a shortcut.
    """
    over: dict[str, float] = {}
    if isinstance(brief.get("energy"), (int, float)):
        over["energy"] = float(brief["energy"])
    if isinstance(brief.get("sync"), (int, float)):
        over["tightness"] = float(brief["sync"])
    for axis in ("drive", "build", "tightness"):
        if isinstance(brief.get(axis), (int, float)):
            over[axis] = float(brief[axis])
    params = brief.get("params")
    if isinstance(params, dict):
        for axis, value in params.items():
            if isinstance(value, (int, float)):
                over[axis] = float(value)
    return over


def _resolve_level(brief: dict[str, Any]):
    """Resolve the four axes for a brief (style baseline → feelings → overrides)."""
    return BOOK.resolve_params(
        style=brief.get("style"),
        feelings=list(brief.get("feeling") or []),
        overrides=_overrides(brief),
    )


def _cut_plan(positions: list[float], ctx: R.GridContext, clips: Any) -> list[dict[str, Any]]:
    """Turn beat positions into cut entries, enforcing the minimum-shot floor.

    A position is dropped if it would fall within :data:`~lumenframe.rhythm.rhythm.MIN_SHOT_SECONDS`
    of the previously kept cut (the seizure floor) or past a pinned total
    duration. Clips, if given, are assigned to the resulting segments in order
    (cycling if there are more segments than clips).
    """
    bpb = ctx.beats_per_bar
    entries: list[dict[str, Any]] = []
    last_t: float | None = None
    for pos in positions:
        t = round(ctx.offset_s + pos * ctx.spb, 6)
        if ctx.total_duration is not None and t > ctx.total_duration + 1e-9:
            break
        if last_t is not None and (t - last_t) < ctx.min_shot_s - 1e-9:
            continue  # minimum-shot floor: never seizure-fast
        last_t = t
        ibeat = int(math.floor(pos + 1e-9))
        on_grid = abs(pos - round(pos)) < 1e-9
        beat_in_bar = ibeat % bpb
        entries.append({
            "t": t,
            "bar": ibeat // bpb,
            "beat": beat_in_bar + 1,
            "phrase": (ibeat // bpb) // ctx.phrase_bars,
            "accent": on_grid and beat_in_bar == 0,
            "downbeat": on_grid and beat_in_bar == 0,
            "offbeat": not on_grid,
            "beat_index": round(pos, 4),
        })
    clip_list = [c for c in (clips or []) if isinstance(c, dict) and c.get("id") is not None]
    if clip_list:
        for i, entry in enumerate(entries):
            entry["clip"] = clip_list[i % len(clip_list)]["id"]
    return entries


def build(brief: dict[str, Any]) -> dict[str, Any]:
    """Brief → ``{"score", "plan", "notes"}`` (deterministic per seed).

    ``score`` is the deliverable: ``beat_grid``, ``cut_plan``, ``bars`` and
    ``seconds_per_beat`` (plus metre helpers). ``plan`` is the human-readable
    explanation of what was decided and why.
    """
    if not isinstance(brief, dict):
        raise BriefError("brief must be an object")
    bpm = brief.get("bpm")
    if not isinstance(bpm, (int, float)):
        raise BriefError("brief needs a numeric 'bpm'")
    bpm = float(bpm)
    if not (MIN_BPM <= bpm <= MAX_BPM):
        raise BriefError(f"bpm {bpm} is out of the musical range [{MIN_BPM}, {MAX_BPM}]")

    time_signature = _time_signature(brief.get("time_signature"))
    total_duration = brief.get("total_duration")
    if total_duration is not None:
        if not isinstance(total_duration, (int, float)) or total_duration <= 0:
            raise BriefError("total_duration, if given, must be a positive number")
    offset_ms = brief.get("offset_ms") or 0
    if not isinstance(offset_ms, (int, float)):
        raise BriefError("offset_ms must be a number of milliseconds")

    # Resolve the style + axes up front. An unknown style raises StyleError and
    # an unknown override axis raises ValueError — both are structurally-unusable
    # briefs, so map them to BriefError (the same convention line 177-178 uses for
    # malformed sections) rather than letting them escape the tool's err() contract.
    try:
        level = _resolve_level(brief)
        style_name = BOOK.resolve_name(brief.get("style"))
        pattern = str(BOOK.spec(style_name).hints.get("pattern") or style_name)
    except ValueError as exc:  # StyleError (unknown style) or unknown-axes ValueError
        raise BriefError(str(exc)) from exc

    try:
        ctx = R.build_grid_context(
            bpm=bpm,
            time_signature=time_signature,
            sections_raw=brief.get("sections"),
            clips=brief.get("clips"),
            total_duration=total_duration,
            offset_ms=float(offset_ms),
            level=level,
        )
    except ValueError as exc:  # malformed sections
        raise BriefError(str(exc)) from exc

    seed = int(brief.get("seed", DEFAULTS["seed"]))
    rng = new_rng(seed)  # only syncopated consumes it
    positions = R.run_pattern(pattern, ctx, level, rng)
    grid = R.beat_grid(ctx)
    cut_plan = _cut_plan(positions, ctx, brief.get("clips"))

    score = {
        "bpm": ctx.bpm,
        "seconds_per_beat": round(ctx.spb, 6),
        "seconds_per_bar": round(ctx.seconds_per_bar, 6),
        "beats_per_bar": ctx.beats_per_bar,
        "time_signature": list(time_signature),
        "bars": ctx.bars,
        "total_beats": ctx.total_beats,
        "beat_grid": grid,
        "cut_plan": cut_plan,
    }

    plan = _plan(style_name, pattern, ctx, level, cut_plan, seed)
    notes = _notes(level, ctx, cut_plan, total_duration)
    return {"score": score, "plan": plan, "notes": notes}


def adjust(brief: dict[str, Any], feedback: list[str]) -> dict[str, Any]:
    """Fold feedback phrases into the brief and rebuild with the SAME seed.

    Returns :func:`build`'s result plus ``brief`` (the adjusted brief to persist).
    Unknown phrases are reported in ``notes``; recognised feedback that moved
    nothing (an axis already at its limit) is reported honestly rather than
    silently no-op'ing.
    """
    vocab = rhythm_vocab()
    before = build(brief)
    new_brief, unknown = vocab.apply(brief, feedback or [], _resolve_level)
    result = build(new_brief)
    result["brief"] = new_brief

    if unknown:
        vocabulary = ", ".join(vocab.vocabulary()[:14])
        result["notes"].append(
            f"unrecognised feedback ignored: {', '.join(unknown)} (known: {vocabulary}, …)"
        )
    recognised = [p for p in (feedback or []) if p not in unknown]
    if recognised and _score_signature(before) == _score_signature(result):
        result["notes"].append(
            "feedback recognised but the cut plan did not change — the targeted "
            "axes are already at their limit"
        )
    return result


# ── plan / notes / signatures ───────────────────────────────────────────────


def _plan(style_name, pattern, ctx, level, cut_plan, seed) -> dict[str, Any]:
    """A compact, explainable summary of the decisions behind the cut plan."""
    accents = sum(1 for c in cut_plan if c.get("accent"))
    gaps = [b["t"] - a["t"] for a, b in zip(cut_plan, cut_plan[1:])]
    avg_shot = round(sum(gaps) / len(gaps), 4) if gaps else None
    return {
        "style": style_name,
        "pattern": pattern,
        "bpm": ctx.bpm,
        "bars": ctx.bars,
        "time_signature": [ctx.beats_per_bar, 4],
        "phrase_bars": ctx.phrase_bars,
        "seed": seed,
        "params": level.to_dict(),
        "cuts": len(cut_plan),
        "accents": accents,
        "avg_shot_seconds": avg_shot,
        "sections": [
            {"name": s.name, "start_bar": s.start_bar, "bars": s.end_bar - s.start_bar,
             "is_drop": s.is_drop}
            for s in ctx.sections
        ],
        "drop_start_beat": ctx.drop_start_beat() if ctx.sections else None,
    }


def _notes(level, ctx, cut_plan, total_duration) -> list[str]:
    notes: list[str] = []
    if level.unknown_feelings:
        notes.append(f"unrecognised feelings ignored: {', '.join(level.unknown_feelings)}")
    if not cut_plan:
        notes.append("no cuts fit the grid — check bpm / duration / sections")
    if total_duration:
        notes.append(f"grid capped to {total_duration}s of audio ({ctx.total_beats} beats)")
    return notes


def _score_signature(result: dict[str, Any]) -> str:
    """Content digest of the cut plan (order + times + accents) for change tests."""
    return stable_digest(round_floats(result["score"]["cut_plan"], 4))
