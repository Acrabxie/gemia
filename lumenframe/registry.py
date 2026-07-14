"""Extension registry — how lumenframe grows without forking the core.

The core registers its built-in ops and layer types here at import time. Third
parties add more in two ways:

1. **Programmatically** — call :func:`register_op` / :func:`register_layer_type`
   (e.g. from a plugin's ``__init__``), or use them as decorators.
2. **By package** — ship a Python distribution declaring an entry point in the
   ``lumenframe.extensions`` group; the referenced callable is invoked once on
   first use and registers whatever it likes::

       # in a third-party repo's pyproject.toml
       [project.entry-points."lumenframe.extensions"]
       my_pack = "my_pack.lumen:register"

   ``my_pack.lumen.register`` then calls ``lumenframe.register_op(...)`` /
   ``register_layer_type(...)``. Installing the GitHub repo (``pip install
   git+https://...``) is all it takes for its ops to light up.

An op handler has the signature ``handler(doc: dict, op: dict) -> None`` and
mutates ``doc`` in place; structural failures raise
:class:`lumenframe.ops.LayerPatchError`.
"""
from __future__ import annotations

import logging
from typing import Any, Callable

logger = logging.getLogger("lumenframe.registry")

OpHandler = Callable[[dict[str, Any], dict[str, Any]], None]

#: op name -> handler. Populated by the core (in :mod:`lumenframe.ops`) and by
#: extensions. ``source`` is tracked for diagnostics / catalogues.
_OPS: dict[str, OpHandler] = {}
_OP_SOURCE: dict[str, str] = {}
#: Immutable snapshot of the core vocabulary so :func:`reset_for_tests` can
#: restore it even after a test deliberately overrode a core op.
_CORE_OPS: dict[str, OpHandler] = {}

#: layer type name -> spec dict. A spec may carry ``defaults`` (props merged into
#: new layers), ``container`` (bool, may hold children), ``compile`` (callable
#: that turns the layer into render-backend content), and ``description``.
_LAYER_TYPES: dict[str, dict[str, Any]] = {}

_EXTENSIONS_LOADED = False
_ENTRY_POINT_GROUP = "lumenframe.extensions"


# ── op registration ─────────────────────────────────────────────────────


def register_op(
    name: str,
    handler: OpHandler | None = None,
    *,
    source: str = "extension",
    override: bool = False,
) -> Any:
    """Register an op handler. Usable directly or as a decorator.

    ``register_op("blur", fn)`` or::

        @register_op("blur")
        def _blur(doc, op): ...

    Re-registering an existing op raises unless ``override=True`` — this is how
    the core protects its vocabulary from being silently shadowed by a plugin.
    """
    def _apply(fn: OpHandler) -> OpHandler:
        key = str(name)
        if key in _OPS and not override and _OP_SOURCE.get(key) == "core" and source != "core":
            raise ValueError(
                f"op {key!r} is a core op; pass override=True to replace it"
            )
        _OPS[key] = fn
        _OP_SOURCE[key] = source
        if source == "core":
            _CORE_OPS[key] = fn
        return fn

    if handler is not None:
        return _apply(handler)
    return _apply


def op_handler(name: str) -> OpHandler | None:
    """Resolve an op name to its handler, loading extensions on first miss."""
    key = str(name)
    if key in _OPS:
        return _OPS[key]
    load_extensions()
    return _OPS.get(key)


def list_ops() -> list[str]:
    """All registered op names (core + loaded extensions), sorted."""
    load_extensions()
    return sorted(_OPS)


def op_source(name: str) -> str | None:
    return _OP_SOURCE.get(str(name))


# ── layer-type registration ─────────────────────────────────────────────


def register_layer_type(
    name: str,
    spec: dict[str, Any] | None = None,
    *,
    override: bool = False,
) -> dict[str, Any]:
    """Register a layer type and its spec (defaults / container / compile)."""
    key = str(name)
    if key in _LAYER_TYPES and not override:
        raise ValueError(f"layer type {key!r} already registered")
    resolved = dict(spec or {})
    resolved.setdefault("container", False)
    resolved.setdefault("defaults", {})
    _LAYER_TYPES[key] = resolved
    return resolved


def layer_type_spec(name: str) -> dict[str, Any] | None:
    key = str(name)
    if key in _LAYER_TYPES:
        return _LAYER_TYPES[key]
    load_extensions()
    return _LAYER_TYPES.get(key)


def list_layer_types() -> list[str]:
    load_extensions()
    return sorted(_LAYER_TYPES)


# ── extension discovery ─────────────────────────────────────────────────


def load_extensions(*, force: bool = False) -> None:
    """Discover and run ``lumenframe.extensions`` entry points exactly once.

    Each entry point resolves to a callable invoked with no arguments; it is
    expected to register ops / layer types. A failing extension is logged and
    skipped — one bad plugin never breaks the editor.
    """
    global _EXTENSIONS_LOADED
    if _EXTENSIONS_LOADED and not force:
        return
    _EXTENSIONS_LOADED = True
    try:
        from importlib.metadata import entry_points
    except Exception:  # pragma: no cover - importlib.metadata always present on 3.10+
        return
    try:
        eps = entry_points(group=_ENTRY_POINT_GROUP)
    except TypeError:  # pragma: no cover - very old API shape
        eps = entry_points().get(_ENTRY_POINT_GROUP, [])  # type: ignore[attr-defined]
    for ep in eps:
        try:
            register = ep.load()
            register()
            logger.info("lumenframe: loaded extension %s", getattr(ep, "name", ep))
        except Exception:  # noqa: BLE001 - isolate third-party failures
            logger.exception("lumenframe: extension %s failed to load", getattr(ep, "name", ep))


def reset_for_tests() -> None:
    """Drop every non-core registration (used by the test-suite for isolation)."""
    global _EXTENSIONS_LOADED
    _OPS.clear()
    _OP_SOURCE.clear()
    _OPS.update(_CORE_OPS)
    for key in _CORE_OPS:
        _OP_SOURCE[key] = "core"
    _LAYER_TYPES.clear()
    _EXTENSIONS_LOADED = False
    _register_builtin_layer_types()


def _register_builtin_layer_types() -> None:
    from lumenframe.model import CONTAINER_TYPES, LAYER_TYPES
    for name in sorted(LAYER_TYPES):
        _LAYER_TYPES.setdefault(name, {
            "container": name in CONTAINER_TYPES,
            "defaults": {},
            "source": "core",
        })
    # The ``html`` layer type ships in the core (lumenframe.resolve_html renders
    # it to a video via HyperFrames). Register it here so ``add_layer`` validation
    # accepts it and it survives ``reset_for_tests``. Importing resolve_html lazily
    # also runs its own ``register()`` for symmetry, but we register a minimal spec
    # directly to avoid pulling the renderer import into the registry hot path.
    _LAYER_TYPES.setdefault("html", {
        "container": False,
        "defaults": {},
        "source": "core",
        "description": (
            "HTML/CSS/JS motion-graphics layer rendered to a video via "
            "HyperFrames and composited like a video layer."
        ),
    })


_register_builtin_layer_types()
