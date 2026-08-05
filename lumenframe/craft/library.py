"""Point-library contract + registry — what makes a library a *point library*.

Lumeri's second architectural layer is a set of "point libraries", each closing
one creative domain with a structural taste floor (see the vector-motion memo).
This module records the contract as data and lets every library announce itself,
so the agent surface can enumerate them and a single ``craft_catalog()`` returns
the whole second layer's vocabulary.

The contract (each library MUST honour it — checked per library by tests):

1. Speak creative/semantic language; never expose raw numbers as the surface.
2. A structural taste floor — craft (choreography, curves, grids, phrasing) is
   enforced by the library, not chosen per call. Bad output must be hard.
3. Style archetypes: one word reshapes the whole result.
4. Determinism: same brief + seed → identical output.
5. A single consolidated agent tool with an ``op`` discriminator
   (``create`` | ``adjust`` | ``catalog``) — no flat-tool proliferation.
6. Ride the existing render / primitive / timeline layer; never fork it.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable


@dataclass(frozen=True)
class LibraryMeta:
    """Announcement record for one point library."""

    name: str                      # the agent tool name, e.g. "grade"
    domain: str                    # human label, e.g. "color grading"
    summary: str                   # one line
    rides: str                     # which layer it rides (effect/html/timeline/transform)
    output: str                    # what create produces (recipe/scene/plan/track)
    catalog_fn: Callable[[], dict[str, Any]]  # the library's own catalog()
    ops: tuple[str, ...] = ("create", "adjust", "catalog")
    aliases: tuple[str, ...] = ()


#: name → LibraryMeta, in registration order of import.
LIBRARIES: dict[str, LibraryMeta] = {}


def register_library(meta: LibraryMeta) -> LibraryMeta:
    if meta.name in LIBRARIES:
        raise ValueError(f"point library {meta.name!r} already registered")
    LIBRARIES[meta.name] = meta
    return meta


def library_names() -> list[str]:
    return sorted(LIBRARIES)


def craft_catalog() -> dict[str, Any]:
    """The whole second layer: every registered library's vocabulary at once.

    Lazily imports the sibling library packages so registration side-effects
    fire, mirroring ``vector.behaviors._load``.
    """
    _load_all()
    return {
        "libraries": {
            m.name: {
                "domain": m.domain, "summary": m.summary,
                "rides": m.rides, "output": m.output,
                "ops": list(m.ops), "aliases": list(m.aliases),
                "catalog": m.catalog_fn(),
            }
            for m in (LIBRARIES[n] for n in library_names())
        }
    }


def describe_craft() -> str:
    _load_all()
    lines = ["Lumeri creative libraries (each = one agent tool, op: create|adjust|catalog):"]
    for name in library_names():
        m = LIBRARIES[name]
        lines.append(f"- {m.name} — {m.domain}: {m.summary} (rides {m.rides}, makes {m.output})")
    return "\n".join(lines)


_LOADED = False
#: sibling library packages that self-register on import.
_LIBRARY_MODULES = (
    "lumenframe.grade", "lumenframe.kinetic", "lumenframe.edit",
    "lumenframe.camera", "lumenframe.compose", "lumenframe.rhythm",
)


def _load_all() -> None:
    global _LOADED
    if _LOADED:
        return
    _LOADED = True
    import importlib
    for mod in _LIBRARY_MODULES:
        try:
            importlib.import_module(mod)
        except ImportError:
            pass  # a library not yet built is simply absent from the catalog
