"""Sync-pattern archetypes — one word chooses how the edit locks to the music.

A rhythm **style** is a named *sync pattern*: the rule that decides where cuts
land relative to the beat grid. Choosing one re-tunes the whole cut plan, exactly
as picking a grade look re-tunes every pixel. Each style sets an axis baseline
*and* names the taste-floor pattern function (via the ``pattern`` hint) that
:mod:`lumenframe.rhythm.rhythm` will run.

The seven archetypes span the musical-editing vocabulary:

* ``on_beat``     — cut every N beats, locked to the pulse (the house default).
* ``on_downbeat`` — cut only on bar starts (beat 1); calm, architectural.
* ``on_phrase``   — cut only on 4/8-bar phrase starts; montage / storytelling.
* ``syncopated``  — cut on the off-beats ("&"); the one pattern allowed to leave
  the strict grid and cut mid-phrase.
* ``half_time``   — deliberately sparse, one cut every two bars; weighty.
* ``double_time`` — the fastest pattern; the only one allowed to cut on the
  half-beat subdivision.
* ``build_drop``  — accelerate cut density into a drop section, then sustain the
  fast rate (the EDM "build → drop" move).

Archetype names are generic; the ``edm`` alias (and a few convenience folds)
resolve to them for agents that reach for a genre word.
"""
from __future__ import annotations

from lumenframe.craft import StyleBook
from lumenframe.rhythm.params import SPACE

#: The rhythm style book. ``on_beat`` is the house pattern.
BOOK = StyleBook(space=SPACE, default="on_beat")

BOOK.add(
    "on_beat",
    "cut every N beats, locked to the pulse",
    {"energy": 0.5, "tightness": 0.75, "drive": 0.5, "build": 0.2},
    hints={"pattern": "on_beat"},
)
BOOK.add(
    "on_downbeat",
    "cut only on bar starts (beat 1) — architectural, uncrowded",
    {"energy": 0.4, "tightness": 0.85, "drive": 0.35, "build": 0.15},
    hints={"pattern": "on_downbeat"},
)
BOOK.add(
    "on_phrase",
    "cut only on 4/8-bar phrase starts — montage, storytelling",
    {"energy": 0.3, "tightness": 0.9, "drive": 0.2, "build": 0.1},
    hints={"pattern": "on_phrase"},
)
BOOK.add(
    "syncopated",
    "cut on the off-beats (&) — the only pattern that leaves the strict grid",
    {"energy": 0.6, "tightness": 0.35, "drive": 0.6, "build": 0.3},
    hints={"pattern": "syncopated"},
)
BOOK.add(
    "half_time",
    "deliberately sparse — one cut every two bars, weighty",
    {"energy": 0.3, "tightness": 0.8, "drive": 0.2, "build": 0.1},
    hints={"pattern": "half_time"},
)
BOOK.add(
    "double_time",
    "the fastest pattern — the only one allowed to cut on the half-beat",
    {"energy": 0.85, "tightness": 0.6, "drive": 0.85, "build": 0.4},
    hints={"pattern": "double_time"},
)
BOOK.add(
    "build_drop",
    "accelerate cut density into a drop, then sustain the fast rate",
    {"energy": 0.75, "tightness": 0.7, "drive": 0.55, "build": 0.9},
    hints={"pattern": "build_drop"},
)

# Genre / convenience aliases → archetypes.
BOOK.alias("edm", "build_drop")
BOOK.alias("drop", "build_drop")
BOOK.alias("montage", "on_phrase")
BOOK.alias("downbeat", "on_downbeat")
BOOK.alias("phrase", "on_phrase")
BOOK.alias("beatmatch", "on_beat")
BOOK.alias("onbeat", "on_beat")
BOOK.alias("halftime", "half_time")
BOOK.alias("doubletime", "double_time")
BOOK.alias("swing", "syncopated")
