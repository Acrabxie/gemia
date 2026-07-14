"""Render / export a specified time range of a lumenframe document.

Often you only want a *slice* of a timeline — "give me seconds 1.0 through 2.5"
— not the whole thing. Compiling the document is cheap relative to compositing
every frame, and :func:`lumenframe.compile.compile_to_layer_stack` already
builds the full ``LayerStack`` once. This module mirrors the compile-once
pattern of :mod:`lumenframe.preview`: it compiles the document **a single time**
and then asks the stack to render only the frames inside the requested range.

Two coordinate flavours are offered:

* :func:`render_range` / :func:`export_range` take a range in **seconds**
  ``[t_in, t_out]`` and convert to frames with the document's single timebase
  (``canvas.fps``), using the same ``int(round(seconds * fps))`` policy as
  ``compile.py`` (via :func:`lumenframe.timebase.to_frame`).
* :func:`render_range_frames` takes the range directly in **frames**.

Range convention (matches ``LayerStack.render_frames`` exactly)
---------------------------------------------------------------
``LayerStack.render_frames(start_frame=a, end_frame=b, step=s)`` renders
``range(max(0, a), min(b, total_frames), s)`` — i.e. the **start frame is
inclusive and the end frame is exclusive** (a half-open ``[a, b)`` window).
These helpers reproduce that convention bit-for-bit: each returned frame is
identical to ``LayerStack.render_frame(idx)`` for the same ``idx``, because the
underlying call is literally the same ``render_frames`` invocation.

Robustness
----------
The frame bounds are **clamped** into ``[0, total_frames]`` before rendering, so
an out-of-range request (negative ``t_in``, a ``t_out`` past the end) yields a
sensible clamped slice instead of an ``IndexError``. A degenerate range where
``t_in >= t_out`` (equivalently ``frame_in >= frame_out`` after clamping)
produces an **empty list** — consistently, never an exception. ``export_range``,
which must write a real video file, raises ``ValueError`` for an empty range
because there is nothing to encode.

This module is **add-only**: it composes the existing compile + render path
(``compile_to_layer_stack`` -> ``LayerStack.render_frames`` /
``render_to_video``) and introduces no new rendering behaviour. ``compile.py``
is not touched.
"""
from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING, Any

from lumenframe import model, timebase
from lumenframe.compile import Resolver, compile_to_layer_stack

if TYPE_CHECKING:  # pragma: no cover - typing only
    import numpy as np


__all__ = ["render_range", "render_range_frames", "export_range"]


def _clamp_frame(frame: Any, total_frames: int) -> int:
    """Coerce ``frame`` to an int and clamp it into ``[0, total_frames]``.

    Note the **inclusive upper bound** of ``total_frames`` (not
    ``total_frames - 1``): these are range *bounds*, and the end bound is
    exclusive, so a value of ``total_frames`` is the legal "render to the very
    end" marker.
    """
    f = int(frame)
    if f < 0:
        return 0
    if f > total_frames:
        return total_frames
    return f


def _resolve_frame_bounds(
    frame_in: Any, frame_out: Any, total_frames: int
) -> tuple[int, int]:
    """Clamp a ``[frame_in, frame_out)`` request into ``[0, total_frames]``.

    Returns ``(start, stop)`` with ``0 <= start <= stop <= total_frames``. A
    degenerate request (``start >= stop`` after clamping) is normalised to
    ``(start, start)`` so callers can treat ``start == stop`` as "empty".
    """
    start = _clamp_frame(frame_in, total_frames)
    stop = _clamp_frame(frame_out, total_frames)
    if stop < start:
        stop = start
    return start, stop


def _resolve_seconds_bounds(
    doc: dict[str, Any] | None,
    t_in: float | None,
    t_out: float | None,
    *,
    stack: Any,
) -> tuple[float, float]:
    """Resolve the ``[t_in, t_out)`` seconds window, honouring the work area.

    Precedence (explicit args always win):

    1. Any explicitly-passed bound is used verbatim. The two bounds are resolved
       independently, so passing only one (e.g. ``t_in=0.5``, ``t_out=None``)
       overrides just that end and fills the other from the next source.
    2. For a bound left as ``None``: if the doc carries ``canvas.work_area``,
       its ``in`` / ``out`` supplies the missing end.
    3. Otherwise fall back to the full document: ``0.0`` for ``t_in`` and the
       compiled stack's full duration (``total_frames / fps``) for ``t_out`` —
       reproducing the historic "render the whole doc" behaviour.

    ``stack`` is the already-compiled ``LayerStack`` (so the full-doc duration is
    derived from the very same compile the caller renders from). Returns the two
    seconds bounds; frame conversion / clamping stays with the caller.
    """
    work = model.get_work_area(doc) if (t_in is None or t_out is None) else None
    if t_in is None:
        if work is not None:
            t_in = work[0]
        else:
            t_in = 0.0
    if t_out is None:
        if work is not None:
            t_out = work[1]
        else:
            t_out = float(stack.total_frames) / float(stack.fps)
    return float(t_in), float(t_out)


def render_range_frames(
    doc: dict[str, Any] | None,
    frame_in: Any,
    frame_out: Any,
    *,
    step: int = 1,
    resolver: "Resolver | None" = None,
    strict: bool = False,
) -> list["np.ndarray"]:
    """Render the half-open frame window ``[frame_in, frame_out)`` of ``doc``.

    The document is compiled into a ``LayerStack`` exactly once, then
    ``LayerStack.render_frames`` is called for the clamped window. The start
    frame is inclusive and the end frame is exclusive, matching
    ``render_frames`` exactly.

    Args:
        doc: A lumenframe document (same shape ``compile_to_layer_stack``
            accepts). May be ``None`` / partial — it is normalised on compile.
        frame_in: Inclusive start frame. Coerced to int and clamped to
            ``[0, total_frames]``.
        frame_out: Exclusive end frame. Coerced to int and clamped to
            ``[0, total_frames]``.
        step: Stride between rendered frames (``>= 1``). Forwarded to
            ``render_frames``.
        resolver: Optional content resolver forwarded to the compile step.
        strict: Forwarded to the compile step; raise on unresolved content.

    Returns:
        A list of canvas-sized ``(height, width, 4)`` float32 RGBA frames, one
        per ``frame_index`` in ``range(start, stop, step)`` of the clamped
        window. Empty if ``frame_in >= frame_out`` after clamping.

    Raises:
        ValueError: if ``step < 1`` (propagated from ``render_frames``).
    """
    stack = compile_to_layer_stack(doc, resolver=resolver, strict=strict)
    total = int(stack.total_frames)
    start, stop = _resolve_frame_bounds(frame_in, frame_out, total)
    if start >= stop:
        # Empty window. Still validate ``step`` for a consistent contract.
        if int(step) <= 0:
            raise ValueError("step must be >= 1")
        return []
    return stack.render_frames(start_frame=start, end_frame=stop, step=step)


def render_range(
    doc: dict[str, Any] | None,
    t_in: float | None = None,
    t_out: float | None = None,
    *,
    step: int = 1,
    resolver: "Resolver | None" = None,
    strict: bool = False,
) -> list["np.ndarray"]:
    """Render the half-open time window ``[t_in, t_out)`` seconds of ``doc``.

    The seconds bounds are converted to frames with the document's timebase
    (``canvas.fps``) using ``int(round(seconds * fps))`` — the same policy as
    ``compile.py`` — via :func:`lumenframe.timebase.to_frame`. The resulting
    frame window is rendered with :func:`render_range_frames`, so the start is
    inclusive and the end exclusive, clamped into ``[0, total_frames]``.

    Default range (optional behaviour, add-only)
    --------------------------------------------
    ``t_in`` / ``t_out`` are optional. When a bound is omitted (``None``):

    * if ``doc`` carries ``canvas.work_area``, that work area supplies the
      missing bound — so ``render_range(doc)`` renders the work area;
    * otherwise the full document is used (``t_in=0`` to the doc's duration) —
      the historic behaviour.

    **Explicit args always win**: passing ``t_in`` / ``t_out`` reproduces the
    exact prior behaviour regardless of any work area, bit-for-bit.

    Args:
        doc: A lumenframe document (see :func:`render_range_frames`).
        t_in: Inclusive start time in seconds, or ``None`` to default it.
        t_out: Exclusive end time in seconds, or ``None`` to default it.
        step: Stride between rendered frames (``>= 1``).
        resolver: Optional content resolver forwarded to the compile step.
        strict: Forwarded to the compile step.

    Returns:
        A list of float32 RGBA frames for the clamped frame window. Empty if
        ``t_in >= t_out`` (after frame conversion / clamping).
    """
    stack = compile_to_layer_stack(doc, resolver=resolver, strict=strict)
    fps = float(stack.fps)
    total = int(stack.total_frames)
    t_in, t_out = _resolve_seconds_bounds(doc, t_in, t_out, stack=stack)
    frame_in = timebase.to_frame(t_in, fps)
    frame_out = timebase.to_frame(t_out, fps)
    start, stop = _resolve_frame_bounds(frame_in, frame_out, total)
    if start >= stop:
        if int(step) <= 0:
            raise ValueError("step must be >= 1")
        return []
    return stack.render_frames(start_frame=start, end_frame=stop, step=step)


def export_range(
    doc: dict[str, Any] | None,
    t_in: float | None = None,
    t_out: float | None = None,
    out_path: str | Path | None = None,
    *,
    step: int = 1,
    resolver: "Resolver | None" = None,
    strict: bool = False,
    codec: str = "mp4v",
) -> str:
    """Export the time window ``[t_in, t_out)`` seconds of ``doc`` to a video.

    Compiles the document once, converts the seconds bounds to frames with the
    document timebase (``int(round(seconds * fps))``), clamps into
    ``[0, total_frames]``, then calls ``LayerStack.render_to_video`` for that
    half-open frame window. ``render_to_video`` derives the output fps itself
    (``self.fps / max(step, 1)``) and writes a real file on disk.

    Default range (optional behaviour, add-only)
    --------------------------------------------
    Like :func:`render_range`, ``t_in`` / ``t_out`` are optional: an omitted
    (``None``) bound is filled from ``canvas.work_area`` when the doc has one,
    else from the full document. **Explicit args always win**, reproducing the
    prior behaviour bit-for-bit. ``out_path`` remains required (passing only the
    doc still needs a destination).

    Args:
        doc: A lumenframe document.
        t_in: Inclusive start time in seconds, or ``None`` to default it.
        t_out: Exclusive end time in seconds, or ``None`` to default it.
        out_path: Destination video path. Parent directories are created by
            ``render_to_video``.
        step: Stride between encoded frames (``>= 1``).
        resolver: Optional content resolver forwarded to the compile step.
        strict: Forwarded to the compile step.
        codec: FourCC codec string forwarded to ``render_to_video``.

    Returns:
        The absolute path to the written video file (as a ``str``).

    Raises:
        ValueError: if the clamped range is empty (``t_in >= t_out``), since
            there are no frames to encode, or if ``out_path`` is omitted.
    """
    if out_path is None:
        raise ValueError("export_range requires an out_path destination")
    stack = compile_to_layer_stack(doc, resolver=resolver, strict=strict)
    fps = float(stack.fps)
    total = int(stack.total_frames)
    t_in, t_out = _resolve_seconds_bounds(doc, t_in, t_out, stack=stack)
    frame_in = timebase.to_frame(t_in, fps)
    frame_out = timebase.to_frame(t_out, fps)
    start, stop = _resolve_frame_bounds(frame_in, frame_out, total)
    if start >= stop:
        raise ValueError(
            f"empty time range: t_in={t_in} t_out={t_out} -> "
            f"frames [{start}, {stop}); nothing to export"
        )
    return stack.render_to_video(
        out_path, codec=codec, start_frame=start, end_frame=stop, step=step
    )
