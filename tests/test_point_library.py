from __future__ import annotations

import asyncio

import pytest

from gemia.point_library import (
    PointLibraryConflict,
    PointLibraryPackage,
    PointLibraryRegistry,
    _builtin_package,
    _manifest_text,
    build_point_library_bundle,
)


def _demo_bundle(version: str, *, catalog_title: str = "Neutral") -> bytes:
    contract = {
        "shape": "A",
        "category": "TRANSFORM",
        "implementation": {"mode": "builtin", "entrypoint": "grade"},
    }
    manifest = _manifest_text(
        name="demo-library",
        version=version,
        title="Demo Library",
        description="A deterministic test point library.",
        domain="video",
        triggers=["demo grade"],
        tools_used=["grade"],
        contract=contract,
    )
    return build_point_library_bundle(
        manifest_text=manifest,
        catalog={"entries": [{"id": "neutral", "title": catalog_title}]},
        verification={
            "deterministic": True,
            "taste_floor": ["protected output"],
            "tests": ["demo_determinism"],
        },
        runtime_manifest={"api": "lumeri-point-library/v1", "entrypoint": "grade"},
    )


def _shape_b_bundle() -> bytes:
    contract = {
        "shape": "B",
        "category": "TRANSFORM",
        "implementation": {"mode": "ops"},
        "operations": {
            "apply": {"tool": "grade", "op": "create"},
            "describe": {"tool": "grade", "op": "catalog"},
        },
    }
    manifest = _manifest_text(
        name="grade-recipe",
        version="1.0.0",
        title="Grade Recipe",
        description="A declarative grading recipe binding.",
        domain="video",
        triggers=["grade recipe"],
        tools_used=["grade"],
        contract=contract,
    )
    return build_point_library_bundle(
        manifest_text=manifest,
        catalog={"entries": [{"id": "neutral", "title": "Neutral"}]},
        verification={
            "deterministic": True,
            "taste_floor": ["skin-safe default"],
            "tests": ["grade_determinism"],
        },
        ops={"api": "lumeri-point-library/v1", "operations": ["apply", "describe"]},
    )


def test_builtin_point_library_packages_are_deterministic_and_verified() -> None:
    first = _builtin_package("vector-motion")
    second = _builtin_package("vector-motion")
    assert first is not None and second is not None
    assert first.raw == second.raw
    assert first.content_sha256 == second.content_sha256
    assert first.meta.kind == "point_library"
    assert first.contract["shape"] == "A"
    assert first.verification["deterministic"] is True
    assert first.summary(source="builtin")["verified"] is True


def test_registry_install_conflict_and_exact_version_rollback(tmp_path) -> None:
    registry = PointLibraryRegistry(tmp_path / "point-libraries")
    first = PointLibraryPackage.from_bytes(_demo_bundle("1.0.0"))
    second = PointLibraryPackage.from_bytes(_demo_bundle("1.1.0"))

    registry.install(first)
    registry.install(second)
    assert registry.resolve("demo-library").package.meta.version == "1.1.0"

    registry.activate("demo-library", "1.0.0")
    assert registry.resolve("demo-library").package.meta.version == "1.0.0"
    versions = {
        (item["id"], item["version"]): item["active"]
        for item in registry.list()
        if item["id"] == "demo-library"
    }
    assert versions == {("demo-library", "1.0.0"): True, ("demo-library", "1.1.0"): False}

    changed = _demo_bundle("1.0.0", catalog_title="Changed")
    with pytest.raises(PointLibraryConflict):
        registry.install(changed)


def test_shape_a_catalog_and_shape_b_apply_return_contracts() -> None:
    shape_a = _builtin_package("grade")
    assert shape_a is not None
    catalog = asyncio.run(shape_a.dispatch({"op": "catalog"}, None))
    assert catalog["applied"] is True
    assert catalog["verification"]["deterministic"] is True
    assert catalog["catalog"]["entries"]

    shape_b = PointLibraryPackage.from_bytes(_shape_b_bundle())
    description = asyncio.run(shape_b.dispatch({"op": "describe"}, None))
    assert description["shape"] == "B"
    applied = asyncio.run(
        shape_b.dispatch(
            {
                "op": "apply",
                "payload": {"brief": {"look": "neutral", "intensity": 0.5, "seed": 7}},
            },
            None,
        )
    )
    assert applied["applied"] is True
    assert applied["next"]
