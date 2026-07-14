"""Standalone colour/effect kernels for lumenframe.

Each module here exposes a pure, NumPy-only kernel that operates on RGBA
frames (``float32``, shape ``(H, W, 4)``, channels in ``[0, 1]``) and preserves
the alpha channel. Kernels are deliberately decoupled from the renderer's
dispatch so they can be unit-tested in isolation and reused.
"""
from __future__ import annotations

from .curves import apply_curves, build_curve_lut

__all__ = ["apply_curves", "build_curve_lut"]
