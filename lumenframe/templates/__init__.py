"""Composable scene templates — the Lumeri component library.

A *template* is a pure function ``(**params) -> list[op_dict]``: it expands into
the exact same op vocabulary an agent (or the editor) would emit, so the
``apply_template`` op just feeds the result through the normal dispatch. No
template touches the document directly and none introduces a new op — they are
sugar that stamps out a common, professionally-styled multi-layer arrangement (a
title card, a bullet list, a stat card, …) in one call. This is what lets the
agent *compose* a scene from a vetted building block instead of hand-authoring a
dozen layers from scratch — faster, and on a consistent design floor.

Two audiences, one source of truth:

* the **agent** — :func:`describe_templates` renders a compact prompt block so a
  natural-language request maps onto ``apply_template``;
* **authors** — :func:`template_catalog` returns structured metadata (name,
  category, params, example) they can introspect and extend.

A test asserts every registered template has a catalogue entry and that every
documented param exists on the function, so the library and its docs can never
silently drift.

Adding a template
-----------------
1. Write ``my_scene(**params) -> list[op_dict]`` in a module here (build from
   :mod:`lumenframe.templates.theme` for palette / type / layout).
2. Register it in :data:`TEMPLATES` and add a :data:`TEMPLATE_CATALOG` entry.
   ``apply_template``, the op catalogue and the agent prompt pick it up
   automatically.

Conventions
-----------
* Templates return ops only — never raw layers — so every layer is validated by
  the standard ``add_layer`` path.
* Layer ids are made unique per call via a ``prefix`` (default derived from the
  template name) so the same template can be applied twice without id clashes.
* Every styled template shares the :data:`SHARED_PARAMS` (palette / canvas size /
  timing / prefix / animate); only its *content* params are listed per entry.
* Times are in seconds (the doc is seconds-canonical).
"""
from __future__ import annotations

from typing import Any, Callable

# theme (the element / design-token layer) must import first — the templates
# below build on it, and importing it here makes it an attribute of the package
# before any template module runs ``from lumenframe.templates import theme``.
from lumenframe.templates import theme
from lumenframe.templates.lower_third import lower_third
from lumenframe.templates.intro import intro
from lumenframe.templates.title_card import title_card
from lumenframe.templates.section_divider import section_divider
from lumenframe.templates.bullet_list import bullet_list
from lumenframe.templates.stat_card import stat_card
from lumenframe.templates.quote_card import quote_card
from lumenframe.templates.end_card import end_card
from lumenframe.templates.caption import caption
from lumenframe.templates.callout import callout

Template = Callable[..., list[dict[str, Any]]]

#: name -> template function. The single source of truth consumed by
#: ``apply_template`` (lumenframe.ops) and the catalogue.
TEMPLATES: dict[str, Template] = {
    "intro": intro,
    "title_card": title_card,
    "section_divider": section_divider,
    "lower_third": lower_third,
    "caption": caption,
    "bullet_list": bullet_list,
    "quote_card": quote_card,
    "stat_card": stat_card,
    "callout": callout,
    "end_card": end_card,
}

#: Styling / timing params every styled template accepts (documented once here
#: instead of on every catalogue entry). ``intro`` / ``lower_third`` are the two
#: legacy templates and take a subset (their own colour args, no ``palette``).
SHARED_PARAMS: tuple[str, ...] = (
    "palette", "width", "height", "start", "duration", "prefix", "animate",
)

#: Structured metadata — one entry per template. ``params`` lists only the
#: *content* params (the shared styling params live in :data:`SHARED_PARAMS`);
#: ``*`` is informational. A test pins these names to the real signatures.
TEMPLATE_CATALOG: list[dict[str, Any]] = [
    {"name": "intro", "category": "opener",
     "summary": "Simple centred title card with an optional subtitle (legacy).",
     "params": ["title*", "subtitle", "background", "title_color", "font_size"],
     "example": {"template": "intro", "params": {"title": "Lumeri", "subtitle": "made from light"}}},
    {"name": "title_card", "category": "opener",
     "summary": "Hero opener: eyebrow kicker · big title · accent rule · subtitle.",
     "params": ["title*", "kicker", "subtitle"],
     "example": {"template": "title_card", "params": {"title": "Q3 Review", "kicker": "STRATEGY", "subtitle": "What moved the numbers", "palette": "ink"}}},
    {"name": "section_divider", "category": "opener",
     "summary": "Chapter break: oversized index number · accent rule · section label.",
     "params": ["label*", "index"],
     "example": {"template": "section_divider", "params": {"label": "The Approach", "index": 2}}},
    {"name": "lower_third", "category": "lower-third",
     "summary": "Left-aligned name/title lockup on a bar, pinned to the lower third (legacy).",
     "params": ["text*", "subtitle", "color", "text_color", "font_size"],
     "example": {"template": "lower_third", "params": {"text": "Jane Doe", "subtitle": "Director of Design"}}},
    {"name": "caption", "category": "lower-third",
     "summary": "Centred subtitle line in the lower-third safe band, over a translucent strip.",
     "params": ["text*", "band"],
     "example": {"template": "caption", "params": {"text": "…and that changed everything."}}},
    {"name": "bullet_list", "category": "content",
     "summary": "Headed bullet list that builds in line by line on a stagger.",
     "params": ["heading*", "items* (list[str])", "stagger", "bullet"],
     "example": {"template": "bullet_list", "params": {"heading": "Agenda", "items": ["Why now", "The plan", "Next steps"]}}},
    {"name": "quote_card", "category": "content",
     "summary": "Pull-quote: big accent quote-mark · centred quote · attribution.",
     "params": ["quote*", "author", "role"],
     "example": {"template": "quote_card", "params": {"quote": "Invent the future.", "author": "Alan Kay", "palette": "noir"}}},
    {"name": "stat_card", "category": "data",
     "summary": "Metric highlight: hero accent number · label · optional caption.",
     "params": ["value*", "label*", "caption"],
     "example": {"template": "stat_card", "params": {"value": "3.2×", "label": "faster render", "caption": "vs. last quarter"}}},
    {"name": "callout", "category": "overlay",
     "summary": "Accent pill that pops on to spotlight one word/number; overlays the shot.",
     "params": ["text*", "position (top|center|bottom)"],
     "example": {"template": "callout", "params": {"text": "NEW", "position": "top"}}},
    {"name": "end_card", "category": "closing",
     "summary": "Sign-off: closing title · call-to-action · handle at the bottom.",
     "params": ["title*", "cta", "handle"],
     "example": {"template": "end_card", "params": {"title": "Lumeri", "cta": "Start creating", "handle": "@lumeri"}}},
]


def template_names() -> list[str]:
    """Sorted list of registered template names."""
    return sorted(TEMPLATES)


def template_catalog() -> list[dict[str, Any]]:
    """Structured metadata for every registered template (a fresh copy).

    Ordered to match :data:`TEMPLATE_CATALOG` (roughly opener → content → close),
    which reads better than alphabetical for authors and the prompt.
    """
    return [dict(entry) for entry in TEMPLATE_CATALOG]


def expand_template(name: str, params: dict[str, Any] | None = None) -> list[dict[str, Any]]:
    """Expand template ``name`` with ``params`` into a list of op dicts.

    Raises:
        KeyError: if ``name`` is not a registered template.
    """
    fn = TEMPLATES[name]
    return fn(**(params or {}))


def describe_templates() -> str:
    """Render the component library as a compact agent-facing prompt block.

    One line per template (``name [category] — summary``) plus its content
    params, with the shared styling params and palette names stated once at the
    top. Appended to :func:`lumenframe.describe_ops` so the agent sees the whole
    library exactly where it sees the op vocabulary.
    """
    lines = [
        "Scene templates — `apply_template template=<name> params={…}` stamps a",
        "styled, animated multi-layer scene in one op (compose from these instead",
        "of hand-building layers). Shared params on every template: "
        + ", ".join(SHARED_PARAMS) + ".",
        "Palettes: " + ", ".join(theme.palette_names()) + " (default: "
        + theme.DEFAULT_PALETTE + ", the brand ice-blue).",
        "",
    ]
    for entry in TEMPLATE_CATALOG:
        params = ", ".join(entry.get("params", [])) or "—"
        lines.append(f"• {entry['name']} [{entry['category']}] — {entry['summary']}")
        lines.append(f"    params: {params}")
    return "\n".join(lines)


__all__ = [
    "TEMPLATES",
    "TEMPLATE_CATALOG",
    "SHARED_PARAMS",
    "Template",
    "template_names",
    "template_catalog",
    "describe_templates",
    "expand_template",
    "theme",
    # template functions
    "intro",
    "title_card",
    "section_divider",
    "lower_third",
    "caption",
    "bullet_list",
    "quote_card",
    "stat_card",
    "callout",
    "end_card",
]
