"""Verb/preset registry — a named, catalogued vocabulary that cannot drift.

Every point library exposes a vocabulary the agent composes from: grade *looks*,
kinetic *layouts* and *reveals*, edit *transitions*, camera *moves*, composition
*framings*, rhythm *sync patterns*. This is the shared machinery for all of
them, generalising :mod:`lumenframe.vector.behaviors`' registry:

* :meth:`Registry.verb` — a decorator that registers a callable with metadata.
* :attr:`Registry.catalog` — the agent-facing metadata list, appended in
  registration order.
* :meth:`Registry.check_catalog` — an anti-drift assertion (call it in a test):
  the catalog must exactly mirror the registered implementations, so prompt
  docs can never silently diverge from what actually runs.

``families`` (optional) constrains names to ``"family.verb"`` and validates the
family, exactly as vector's behaviour families do.
"""
from __future__ import annotations

from typing import Any, Callable, Iterable


class Registry:
    def __init__(self, label: str, families: Iterable[str] | None = None) -> None:
        self.label = label
        self.families = tuple(families) if families else None
        self._impls: dict[str, Callable[..., Any]] = {}
        self._catalog: list[dict[str, Any]] = []

    def verb(self, name: str, *, summary: str, family: str | None = None,
             **meta: Any) -> Callable[[Callable[..., Any]], Callable[..., Any]]:
        """Register ``name`` with a one-line ``summary`` and any extra metadata."""
        if self.families is not None:
            if family is None:
                raise ValueError(f"{self.label}: {name!r} needs a family (one of {self.families})")
            if family not in self.families:
                raise ValueError(f"{self.label}: unknown family {family!r} (use {self.families})")
            if not name.startswith(family + "."):
                raise ValueError(f"{self.label}: {name!r} must be '{family}.<verb>'")

        def _register(fn: Callable[..., Any]) -> Callable[..., Any]:
            if name in self._impls:
                raise ValueError(f"{self.label}: {name!r} already registered")
            self._impls[name] = fn
            entry = {"name": name, "summary": summary}
            if family is not None:
                entry["family"] = family
            entry.update(meta)
            self._catalog.append(entry)
            return fn

        return _register

    def get(self, name: str) -> Callable[..., Any] | None:
        return self._impls.get(str(name))

    def require(self, name: str) -> Callable[..., Any]:
        fn = self._impls.get(str(name))
        if fn is None:
            raise KeyError(f"{self.label}: unknown {name!r} (use {self.names()})")
        return fn

    def names(self) -> list[str]:
        return sorted(self._impls)

    def catalog(self) -> list[dict[str, Any]]:
        return [dict(e) for e in self._catalog]

    def check_catalog(self) -> None:
        """Anti-drift: catalog names must equal registered impl names exactly."""
        cat = {e["name"] for e in self._catalog}
        impl = set(self._impls)
        if cat != impl:
            missing, extra = impl - cat, cat - impl
            raise AssertionError(
                f"{self.label} catalog drift — uncatalogued: {sorted(missing)}, "
                f"phantom: {sorted(extra)}")

    def describe(self, header: str) -> str:
        lines = [header]
        if self.families:
            for fam in self.families:
                verbs = [e for e in self._catalog if e.get("family") == fam]
                if verbs:
                    lines.append(f"- {fam}: " + "; ".join(
                        f"{e['name'].split('.', 1)[-1]} ({e['summary']})" for e in verbs))
        else:
            for e in self._catalog:
                lines.append(f"- {e['name']}: {e['summary']}")
        return "\n".join(lines)
