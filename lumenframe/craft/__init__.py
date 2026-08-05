"""lumenframe.craft — the shared spine of Lumeri's creative "point libraries".

Lumeri's second architectural layer is a set of point libraries, each closing
one creative domain (color grading, kinetic type, edit grammar, camera moves,
composition, musical rhythm) with a *structural taste floor*: the agent speaks
creative language and the library makes amateur output structurally hard.
:mod:`lumenframe.vector` was the first such library; this package factors out
the machinery all of them share so they stay coherent rather than diverging.

The spine (bottom → top):

* :mod:`~lumenframe.craft.params`       — semantic axes (0..1) → resolved values;
  ``AxisSpace`` / ``ResolvedAxes`` / ``clamp01``.
* :mod:`~lumenframe.craft.styles`       — ``StyleBook``: named archetypes a single
  word reshapes everything with; alias resolution.
* :mod:`~lumenframe.craft.feedback`     — ``FeedbackVocab``: "more/less X" (中/英)
  → axis deltas → a re-derived brief.
* :mod:`~lumenframe.craft.determinism`  — seeded rng, thread-local id counter,
  stable content digests.
* :mod:`~lumenframe.craft.registry`     — ``Registry``: a catalogued vocabulary
  that cannot drift from its implementations.
* :mod:`~lumenframe.craft.library`      — the point-library contract + registry;
  ``craft_catalog()`` returns the whole second layer.
* :mod:`~lumenframe.craft.tool`         — the single-tool, op-discriminated
  (create|adjust|catalog) surface shape.

Each sibling package (``lumenframe.grade`` etc.) is a thin domain built on this
spine; none re-implements resolution, feedback parsing, determinism, or catalog
discipline.
"""
from __future__ import annotations

from lumenframe.craft.determinism import IdSeq, new_rng, stable_digest  # noqa: F401
from lumenframe.craft.feedback import FeedbackVocab  # noqa: F401
from lumenframe.craft.library import (  # noqa: F401
    LibraryMeta,
    craft_catalog,
    describe_craft,
    library_names,
    register_library,
)
from lumenframe.craft.params import (  # noqa: F401
    AxisSpace,
    ResolvedAxes,
    axis_space,
    clamp01,
    lerp,
    remap,
)
from lumenframe.craft.registry import Registry  # noqa: F401
from lumenframe.craft.styles import Style, StyleBook, StyleError  # noqa: F401
from lumenframe.craft.tool import dispatch as tool_dispatch  # noqa: F401
from lumenframe.craft.tool import err, ok  # noqa: F401

__all__ = [
    "AxisSpace", "ResolvedAxes", "axis_space", "clamp01", "lerp", "remap",
    "StyleBook", "Style", "StyleError",
    "FeedbackVocab",
    "IdSeq", "new_rng", "stable_digest",
    "Registry",
    "LibraryMeta", "register_library", "library_names", "craft_catalog", "describe_craft",
    "tool_dispatch", "err", "ok",
]
