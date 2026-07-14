"""Single source of truth for seconds <-> frame conversion.

This module centralizes the time/frame quantization used throughout the
lumenframe compile/ops pipeline. Historically each caller open-coded
``int(round(seconds * fps))``; this module makes that one canonical
operation so rounding policy lives in exactly one place.

ADD-ONLY: callers in ``compile.py`` / ``ops.py`` are *not* changed in this
slice. ``to_frame(seconds, fps)`` with the default ``rounding="round"`` is a
drop-in replacement for the existing ``int(round(seconds * fps))`` pattern.

Functions
---------
to_frame(seconds, fps, rounding="round") -> int
    Convert a time in seconds to an integer frame index. ``rounding`` is one
    of ``{"round", "floor", "ceil"}``. The default ``"round"`` reproduces the
    current behavior exactly: ``int(round(seconds * fps))``.

to_seconds(frame, fps) -> float
    Convert an integer frame index back to seconds: ``frame / fps``.

snap_seconds(seconds, fps) -> float
    Quantize a time to the nearest frame boundary, returning seconds. Equal to
    ``to_seconds(to_frame(seconds, fps), fps)``. Idempotent under further
    snapping and stable under ``to_frame``.
"""

from __future__ import annotations

import math

__all__ = ["FRAME_EPS", "to_frame", "to_seconds", "snap_seconds"]

# Tolerance for floating-point comparisons of time/frame values (seconds).
# Times within FRAME_EPS of a frame boundary are treated as on the boundary.
FRAME_EPS: float = 1e-9

_ROUNDINGS = ("round", "floor", "ceil")


def to_frame(seconds: float, fps: float, rounding: str = "round") -> int:
    """Convert a time in ``seconds`` to an integer frame index at ``fps``.

    ``rounding`` selects the quantization policy:

    * ``"round"`` (default) -> ``int(round(seconds * fps))`` -- matches the
      current behavior in ``compile.py`` exactly, so this is a drop-in.
    * ``"floor"`` -> largest frame <= the exact position.
    * ``"ceil"``  -> smallest frame >= the exact position.

    A small ``FRAME_EPS`` cushion is applied for ``floor`` / ``ceil`` so that
    a value already sitting on a frame boundary (modulo float noise) is not
    pushed to the neighbouring frame. ``"round"`` is deliberately left as the
    bare ``int(round(...))`` so it stays bit-for-bit identical to the legacy
    callers.

    Raises:
        ValueError: if ``rounding`` is not one of {"round", "floor", "ceil"}.
    """
    if rounding not in _ROUNDINGS:
        raise ValueError(
            f"rounding must be one of {_ROUNDINGS!r}, got {rounding!r}"
        )

    exact = float(seconds) * float(fps)

    if rounding == "round":
        # Bit-for-bit identical to the legacy int(round(seconds * fps)) callers.
        return int(round(exact))
    if rounding == "floor":
        return int(math.floor(exact + FRAME_EPS))
    # ceil
    return int(math.ceil(exact - FRAME_EPS))


def to_seconds(frame: float, fps: float) -> float:
    """Convert an integer (or numeric) ``frame`` index back to seconds at ``fps``."""
    return float(frame) / float(fps)


def snap_seconds(seconds: float, fps: float) -> float:
    """Quantize ``seconds`` to the nearest frame boundary, returned in seconds.

    Equivalent to ``to_seconds(to_frame(seconds, fps), fps)``. Snapping an
    already-snapped time is stable (``to_frame`` of the result is unchanged).
    """
    return to_seconds(to_frame(seconds, fps), fps)
