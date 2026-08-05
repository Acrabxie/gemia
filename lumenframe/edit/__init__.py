"""lumenframe.edit — the edit-grammar / cut-craft point library.

One creative domain, closed with a structural taste floor: *how clips join*. An
agent describes a sequence of clips and a cut *style* in words; the library
returns a **cut plan** — one reasoned entry per join (transition, duration, J/L
audio split, action trims, cutaway notes) — that an editor or the timeline layer
can apply. The whole point is the floor in :mod:`lumenframe.edit.grammar`:
straight cuts are the default, transitions are budget-capped seasoning, jump cuts
are structurally avoided, and montage cadence accelerates on its own.

Built entirely on the :mod:`lumenframe.craft` spine (axes, styles, feedback,
determinism, registry, single-tool dispatch); it rides the existing ``timeline``
layer and never forks a renderer. Same brief + seed → byte-identical plan.

Surface:

* :func:`~lumenframe.edit.api.build_cut_plan` / :func:`~lumenframe.edit.api.adjust_cut_plan`
* :func:`~lumenframe.edit.catalog.edit_catalog` / :func:`~lumenframe.edit.catalog.describe_edit`
* :func:`~lumenframe.edit.tool.dispatch` — the ``edit_grammar`` agent tool.
"""
from __future__ import annotations

from lumenframe.craft import LibraryMeta, register_library
from lumenframe.edit.api import (  # noqa: F401
    EditBriefError,
    adjust_cut_plan,
    build_cut_plan,
)
from lumenframe.edit.catalog import describe_edit, edit_catalog  # noqa: F401

register_library(LibraryMeta(
    name="edit_grammar",
    domain="edit grammar / cut craft",
    summary="how clips join — a reasoned cut plan of transitions, trims and J/L splits, "
            "with straight cuts as the default and transitions as capped seasoning",
    rides="timeline",
    output="cut plan",
    catalog_fn=edit_catalog,
    aliases=("edit", "cut", "mtv", "film", "music_video"),
))

__all__ = [
    "build_cut_plan",
    "adjust_cut_plan",
    "edit_catalog",
    "describe_edit",
    "EditBriefError",
]
