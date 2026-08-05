"""The beat grid + the sync-pattern taste floor — where musical craft is enforced.

This is the heart of the library. It builds an exact beat grid from the tempo
and then, per style, decides *where cuts are allowed to land*. The invariants a
human editor would never violate are structural here, not optional:

* **Cuts land on the musical grid.** Grid-locked patterns (``on_beat``,
  ``on_downbeat``, ``on_phrase``, ``half_time``) place cuts on integer beats;
  ``on_downbeat`` only on bar starts, ``on_phrase`` only on 4/8-bar phrase
  starts. ``syncopated``, ``double_time`` and a high-build ``build_drop`` may
  land on the half-beat but never finer — you cannot ask for a cut at 0.37 of a
  beat.
* **Phrasing is respected.** ``syncopated`` is the pattern that deliberately cuts
  mid-phrase; ``double_time`` and a sustaining ``build_drop`` may sit on the
  half-beat, always bounded by the minimum-shot floor below.
* **Accents land on strong beats.** A cut on beat 1 of a bar is flagged an
  accent; patterns aim their strongest cuts there.
* **Density follows energy/drive**, and ``build_drop`` *accelerates* that density
  into the drop and then sustains it.
* **A minimum-shot floor** (in beats *and* seconds) makes seizure-fast cutting
  structurally impossible. ``double_time`` and a high-build ``build_drop`` are
  the patterns licensed to cut on the half-beat (the fastest subdivision allowed),
  and even they are bounded by the seconds floor.

Determinism: beat times are computed exactly from ``bpm`` and never involve the
RNG. The seed only chooses *which* off-beats a ``syncopated`` edit displaces, so
two builds with the same seed are byte-identical while different seeds vary only
the syncopation.
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Any

from lumenframe.craft import Registry, clamp01

#: Absolute seizure floor: no two cuts may fall closer than this in wall time,
#: regardless of tempo or pattern (~4 frames at 24fps). Enforced after a pattern
#: proposes positions, so even ``double_time`` at 180bpm cannot strobe.
MIN_SHOT_SECONDS: float = 0.18

#: Bars a "phrase" spans when the material is busy vs. sparse. Real songs phrase
#: in fours and eights; we pick one, never an odd number.
PHRASE_BARS_BUSY: int = 4
PHRASE_BARS_SPARSE: int = 8

#: Default song length (bars) when the brief pins neither sections, a total
#: duration, nor enough clips to imply one.
DEFAULT_BARS: int = 8

#: The registry of sync patterns. A pattern is ``fn(ctx, level, rng) -> list of
#: cut positions in beats (floats)``. Kept flat (no families): the style name and
#: the pattern name are one and the same word.
SYNC = Registry("rhythm.sync")

#: Section name fragments that mark the "drop" a build accelerates toward.
_DROP_WORDS = ("drop", "chorus", "hook", "hard", "climax")


# ── grid context ────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class Section:
    """A named span of the song measured in bars (e.g. intro=4, drop=8)."""

    name: str
    start_bar: int
    end_bar: int  # exclusive

    @property
    def is_drop(self) -> bool:
        low = self.name.lower()
        return any(word in low for word in _DROP_WORDS)


@dataclass(frozen=True)
class GridContext:
    """Everything the tempo/metre implies, resolved once per build.

    All timing derives from :attr:`spb` (seconds per beat) which is exact from
    ``bpm``; the pattern functions never see wall-clock time, only beats.
    """

    bpm: float
    spb: float               # seconds per beat (exact: 60 / bpm)
    beats_per_bar: int
    bars: int
    total_beats: int
    phrase_bars: int
    offset_s: float
    total_duration: float | None
    sections: tuple[Section, ...] = ()
    min_shot_s: float = MIN_SHOT_SECONDS

    @property
    def seconds_per_bar(self) -> float:
        return self.spb * self.beats_per_bar

    def drop_start_beat(self) -> int:
        """First beat of the first drop-like section, else the song's midpoint.

        ``build_drop`` accelerates toward this beat. Falling back to the midpoint
        means a section-less brief still gets a coherent build → sustain shape.
        """
        for sec in self.sections:
            if sec.is_drop:
                return sec.start_bar * self.beats_per_bar
        return (self.bars // 2) * self.beats_per_bar


# ── density helpers (axes → grid decisions) ─────────────────────────────────


def _pick(score: float, options: list[int]) -> int:
    """Pick from ``options`` (ascending intervals) by a 0..1 density ``score``.

    A higher score picks a *smaller* interval (denser). The mapping is a plain
    quantised remap so it is deterministic and unit-testable as a table.
    """
    score = clamp01(score)
    idx = round((1.0 - score) * (len(options) - 1))
    return options[int(idx)]


def _density_score(level: Any) -> float:
    """Blend drive (dominant) with energy into a single 0..1 density score."""
    return clamp01(0.6 * level.axis("drive", 0.5) + 0.4 * level.axis("energy", 0.5))


def beat_interval(level: Any) -> int:
    """Beats between cuts for pulse-locked patterns: one of 1 / 2 / 4."""
    return _pick(_density_score(level), [1, 2, 4])


def bar_interval(level: Any) -> int:
    """Bars between cuts for bar-locked patterns: one of 1 / 2 / 4."""
    return _pick(_density_score(level), [1, 2, 4])


def phrase_bars_for(level: Any) -> int:
    """4-bar phrasing when busy, 8-bar when sparse."""
    return PHRASE_BARS_BUSY if _density_score(level) >= 0.4 else PHRASE_BARS_SPARSE


# ── sync patterns (the taste floor, one per style) ──────────────────────────


@SYNC.verb("on_beat", summary="cut every N beats, locked to the pulse")
def _on_beat(ctx: GridContext, level: Any, rng: Any) -> list[float]:
    step = beat_interval(level)
    return [float(b) for b in range(0, ctx.total_beats, step)]


@SYNC.verb("on_downbeat", summary="cut only on bar starts (beat 1)")
def _on_downbeat(ctx: GridContext, level: Any, rng: Any) -> list[float]:
    step = bar_interval(level) * ctx.beats_per_bar
    return [float(b) for b in range(0, ctx.total_beats, step)]


@SYNC.verb("on_phrase", summary="cut only on 4/8-bar phrase starts")
def _on_phrase(ctx: GridContext, level: Any, rng: Any) -> list[float]:
    step = ctx.phrase_bars * ctx.beats_per_bar
    return [float(b) for b in range(0, ctx.total_beats, step)]


@SYNC.verb("syncopated", summary="cut on the off-beats (&) — leaves the strict grid")
def _syncopated(ctx: GridContext, level: Any, rng: Any) -> list[float]:
    """Base cuts every 2 or 4 beats, then displace non-downbeat cuts to the "&".

    Candidates sit on a half-bar pulse (every 2 beats). Downbeats (bar starts)
    are always kept on the grid so accents still land on beat 1; every other
    candidate has a ``1 - tightness`` chance of sliding a half-beat late onto the
    off-beat. The 2-beat base spacing means even a displaced cut never lands
    closer than one beat to its neighbour — the "no faster than a beat" floor
    holds without help from the seconds clamp. The RNG (seeded) is the *only*
    source of variation between builds, so a different seed re-rolls only which
    off-beats are hit.
    """
    step = 2  # half-bar pulse → displacing mid-bar cuts keeps >=1 beat gaps
    prob = clamp01(0.85 - level.axis("tightness", 0.5))
    out: list[float] = []
    for b in range(0, ctx.total_beats, step):
        on_downbeat = (b % ctx.beats_per_bar) == 0
        if not on_downbeat and rng.random() < prob:
            out.append(b + 0.5)
        else:
            out.append(float(b))
    return out


@SYNC.verb("half_time", summary="one cut every two bars — deliberately sparse")
def _half_time(ctx: GridContext, level: Any, rng: Any) -> list[float]:
    # Half-time is intentionally slow: 2 bars normally, 4 when energy is low.
    step_bars = 2 if _density_score(level) >= 0.45 else 4
    step = step_bars * ctx.beats_per_bar
    return [float(b) for b in range(0, ctx.total_beats, step)]


@SYNC.verb("double_time", summary="the fastest pattern — cuts on the half-beat")
def _double_time(ctx: GridContext, level: Any, rng: Any) -> list[float]:
    # The only pattern allowed sub-beat cuts. 1 beat by default, 0.5 when driven
    # hard; the seconds floor still guards against strobing at high tempo.
    subdiv = 0.5 if _density_score(level) >= 0.6 else 1.0
    out: list[float] = []
    pos = 0.0
    while pos < ctx.total_beats:
        out.append(round(pos, 4))
        pos += subdiv
    return out


@SYNC.verb("build_drop", summary="accelerate density into a drop, then sustain")
def _build_drop(ctx: GridContext, level: Any, rng: Any) -> list[float]:
    """Ramp the cut interval 4 → 2 → 1 beats approaching the drop, then sustain.

    The ``build`` axis compresses the ramp so a high-build brief reaches the
    dense rate sooner. After the drop beat the interval is *pinned* to the
    sustain rate (1 beat, or the half-beat when both build and drive are high),
    guaranteeing post-drop density is at least as high as any build-region
    interval — the acceleration invariant the tests assert.
    """
    drop_beat = ctx.drop_start_beat()
    build = level.axis("build", 0.5)
    # High build/drive earns a half-beat sustain; otherwise a solid 1-beat pulse.
    sustain = 0.5 if (build >= 0.75 and _density_score(level) >= 0.6) else 1.0
    # Thresholds (as a fraction of the way to the drop) at which the ramp steps
    # from 4→2 and 2→1 beats. Higher build pulls both earlier.
    t0 = 0.5 - 0.3 * build
    t1 = 0.85 - 0.25 * build

    out: list[float] = []
    pos = 0.0
    while pos < ctx.total_beats:
        out.append(round(pos, 4))
        if pos >= drop_beat or drop_beat <= 0:
            step = sustain
        else:
            frac = pos / drop_beat
            step = 4.0 if frac < t0 else (2.0 if frac < t1 else 1.0)
        pos += step
    return out


# ── grid construction ───────────────────────────────────────────────────────


def resolve_sections(raw: Any, beats_per_bar: int) -> tuple[list[Section], int]:
    """Turn ``brief["sections"]`` into ``(Section list, total bars)``.

    Sections are laid end to end; bars accumulate. A malformed section (missing
    or non-positive ``bars``) raises so a structurally broken arrangement is
    caught early rather than silently swallowing bars.
    """
    sections: list[Section] = []
    cursor = 0
    for item in raw or []:
        if not isinstance(item, dict):
            raise ValueError(f"section must be an object, got {item!r}")
        bars = int(item.get("bars", 0))
        if bars <= 0:
            raise ValueError(f"section {item.get('name')!r} needs bars > 0")
        name = str(item.get("name") or f"section_{len(sections) + 1}")
        sections.append(Section(name=name, start_bar=cursor, end_bar=cursor + bars))
        cursor += bars
    return sections, cursor


def build_grid_context(
    *,
    bpm: float,
    time_signature: tuple[int, int],
    sections_raw: Any,
    clips: Any,
    total_duration: float | None,
    offset_ms: float,
    level: Any,
) -> GridContext:
    """Resolve tempo/metre/arrangement into a single :class:`GridContext`.

    Bar count precedence: explicit sections → a total duration → enough bars to
    give the clips room → :data:`DEFAULT_BARS`. A total duration additionally
    *caps* the beat count so the grid never runs past the audio.
    """
    beats_per_bar, _beat_unit = time_signature
    spb = 60.0 / float(bpm)
    offset_s = float(offset_ms) / 1000.0

    sections, section_bars = resolve_sections(sections_raw, beats_per_bar)
    if section_bars > 0:
        bars = section_bars
    elif total_duration:
        usable = max(0.0, float(total_duration) - offset_s)
        bars = max(1, int(usable // (spb * beats_per_bar)))
    elif clips:
        bars = max(DEFAULT_BARS, 2 * len(clips))
    else:
        bars = DEFAULT_BARS

    total_beats = bars * beats_per_bar
    if total_duration:
        usable = max(0.0, float(total_duration) - offset_s)
        cap = int(math.floor(usable / spb + 1e-9))
        total_beats = max(1, min(total_beats, cap))

    return GridContext(
        bpm=float(bpm),
        spb=spb,
        beats_per_bar=beats_per_bar,
        bars=bars,
        total_beats=total_beats,
        phrase_bars=phrase_bars_for(level),
        offset_s=offset_s,
        total_duration=float(total_duration) if total_duration else None,
        sections=tuple(sections),
    )


def beat_grid(ctx: GridContext) -> list[dict[str, Any]]:
    """The exact beat grid: one entry per beat, downbeats flagged.

    Each entry carries its wall-clock ``t`` (seconds, offset applied), its
    ``bar``/``beat`` position (beat is 1-indexed, as musicians count), the
    ``phrase`` it belongs to, and whether it is a ``downbeat`` (bar start).
    """
    grid: list[dict[str, Any]] = []
    for i in range(ctx.total_beats):
        t = round(ctx.offset_s + i * ctx.spb, 6)
        if ctx.total_duration is not None and t > ctx.total_duration + 1e-9:
            break
        bar = i // ctx.beats_per_bar
        beat = i % ctx.beats_per_bar
        grid.append({
            "t": t,
            "beat_index": i,
            "bar": bar,
            "beat": beat + 1,
            "downbeat": beat == 0,
            "phrase": bar // ctx.phrase_bars,
        })
    return grid


def run_pattern(pattern: str, ctx: GridContext, level: Any, rng: Any) -> list[float]:
    """Run a registered sync pattern, returning sorted, de-duplicated positions."""
    fn = SYNC.require(pattern)
    positions = fn(ctx, level, rng)
    seen: set[float] = set()
    out: list[float] = []
    for p in sorted(positions):
        key = round(float(p), 4)
        if key < 0 or key >= ctx.total_beats:
            continue
        if key in seen:
            continue
        seen.add(key)
        out.append(key)
    return out
