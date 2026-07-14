"""lumenframe — the layered editing core for Lumeri.

Everything the user edits is a *layer*. The project is a tree of layers
(``composition`` nodes hold children); time is a property of every layer
(``start`` / ``duration`` / ``source_in`` / ``source_out`` / ``speed``), and the
classic NLE "timeline" is just a *view* over the root composition's children.

Two surfaces make up the core:

* :mod:`lumenframe.model` — the canonical, JSON-serialisable document model
  (``LumenDoc`` dicts + ``LayerNode`` dicts) plus tree helpers.
* :mod:`lumenframe.ops` — the operation vocabulary (``LayerPatch``): an atomic,
  validated edit language covering layer management (new / select / move /
  duplicate / merge / group), inter-layer ops (masks, clipping, adjustment
  layers) and intra-layer ops (transform, colour grade, effects, keyframes).

Third-party GitHub repositories extend both surfaces through
:mod:`lumenframe.registry` (register new layer types, ops and effects), so the
editing language grows without forking the core.
"""
from __future__ import annotations

from lumenframe.model import (
    BLEND_MODES,
    CONTAINER_TYPES,
    DEFAULT_CANVAS,
    LAYER_TYPES,
    doc_duration,
    empty_doc,
    find_layer,
    find_parent,
    locate,
    new_layer,
    normalize_doc,
    walk,
)
from lumenframe.catalog import describe_ops, op_catalog
from lumenframe.ops import (
    LayerPatchError,
    apply_layer_patch,
    validate_doc,
)
from lumenframe.templates import (
    describe_templates,
    expand_template,
    template_catalog,
    template_names,
)
from lumenframe.elements import (
    describe_elements,
    element_catalog,
    element_names,
    expand_element,
)
from lumenframe.registry import (
    layer_type_spec,
    list_layer_types,
    list_ops,
    op_handler,
    register_layer_type,
    register_op,
)

__all__ = [
    "BLEND_MODES",
    "CONTAINER_TYPES",
    "DEFAULT_CANVAS",
    "LAYER_TYPES",
    "LayerPatchError",
    "apply_layer_patch",
    "describe_ops",
    "describe_templates",
    "expand_template",
    "template_catalog",
    "template_names",
    "describe_elements",
    "expand_element",
    "element_catalog",
    "element_names",
    "doc_duration",
    "empty_doc",
    "op_catalog",
    "find_layer",
    "find_parent",
    "layer_type_spec",
    "list_layer_types",
    "list_ops",
    "locate",
    "new_layer",
    "normalize_doc",
    "op_handler",
    "register_layer_type",
    "register_op",
    "validate_doc",
    "walk",
]

__version__ = "0.1.0"
