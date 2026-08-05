"""lumenframe.rhythm — cut to the beat, as a Lumeri creative point library.

Rhythm closes the *musical rhythm editing* domain: given a tempo (and optionally
an arrangement and a stack of clips), it emits an exact beat grid and a
beat-aligned cut plan the timeline layer executes. It rides the existing
``timeline`` — it never renders audio, never forks the editor, and cut times
always land on the musical grid (down to the half-beat for the fastest patterns).

The taste floor (enforced structurally in :mod:`lumenframe.rhythm.rhythm`, not
chosen per call): cuts land on the musical grid; 4/8-bar phrasing is respected;
accents land on strong beats; density follows energy/drive; ``build_drop``
accelerates into the drop and sustains; a minimum-shot floor makes seizure-fast
cutting impossible. Determinism: beat times are exact from ``bpm`` and the seed
only varies syncopation choices, so the same brief is byte-identical every time.

Public surface: :func:`build` / :func:`adjust` (the engine) and
:func:`rhythm_catalog` (the vocabulary). The single agent tool is
``lumenframe.rhythm.tool.dispatch`` (op: create | adjust | catalog).
"""
from __future__ import annotations

from lumenframe.craft import LibraryMeta, register_library
from lumenframe.rhythm.api import BriefError, adjust, build  # noqa: F401
from lumenframe.rhythm.catalog import describe_rhythm, rhythm_catalog  # noqa: F401

register_library(LibraryMeta(
    name="rhythm_edit",
    domain="musical rhythm editing (cut to the beat)",
    summary="turn a tempo + arrangement into a beat grid and beat-aligned cut plan",
    rides="timeline",
    output="beat grid + beat-aligned cut plan",
    catalog_fn=rhythm_catalog,
    ops=("create", "adjust", "catalog"),
    aliases=("edm", "beatmatch", "montage"),
))

__all__ = [
    "build",
    "adjust",
    "rhythm_catalog",
    "describe_rhythm",
    "BriefError",
]
