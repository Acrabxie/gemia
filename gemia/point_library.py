"""Installable Point Library packages for Lumeri.

The legacy ``.lus`` format is a small, human-readable Skill document.  A
Point Library needs more than prose, so v2 uses a deterministic ZIP container
whose required members are ``manifest.lus``, ``catalog.json`` and
``verification.json``.  The container is deliberately *descriptive*: the
``runtime/manifest.json`` member binds to an already installed Lumeri-native
dispatcher, while ``ops/manifest.json`` binds catalog operations to existing
safe tools.  Lumeri never executes uploaded package code.

This module owns package validation, local persistence, activation, and the
single host dispatch surface.  It imports ``gemia.tools`` lazily so the
legacy ``lus`` parser and package scanner remain usable in lean environments.
"""
from __future__ import annotations

import hashlib
import io
import json
import os
import re
import tempfile
import threading
import zipfile
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any, Mapping

from gemia import lus


POINT_LIBRARY_PACKAGE_VERSION = 2
MAX_BUNDLE_BYTES = 8 * 1024 * 1024
MAX_MEMBER_BYTES = 4 * 1024 * 1024
MAX_MEMBER_COUNT = 128
PACKAGE_ENTRIES = frozenset({"manifest.lus", "catalog.json", "verification.json"})
_ID_RE = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")
_TOOL_RE = re.compile(r"^[a-z][a-z0-9_]*$")
_SHA_RE = re.compile(r"^[a-f0-9]{64}$")
_SECRET_RE = re.compile(
    r"(?i)(?:api[_ -]?key|password|passwd|secret|access[_ -]?token|refresh[_ -]?token)"
    r"\s*[:=]\s*['\"]?[A-Za-z0-9._~+/@-]{12,}"
)
_ABS_PATH_RE = re.compile(r"(?:/Users/[^/\s]+/|/home/[^/\s]+/|[A-Za-z]:\\Users\\)")


class PointLibraryError(ValueError):
    """Base error for package and local-install failures."""


class PointLibraryValidationError(PointLibraryError):
    """The package is malformed, unsafe, or not supported by this runtime."""


class PointLibraryConflict(PointLibraryError):
    """The same library/version already exists with different bytes."""


class PointLibraryNotFound(PointLibraryError):
    """A requested local library/version is not installed."""


def _json_bytes(value: Any) -> bytes:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")


def _sha256(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _safe_member_name(name: str) -> bool:
    if not name or "\\" in name:
        return False
    path = PurePosixPath(name)
    return not path.is_absolute() and ".." not in path.parts and not name.startswith("./")


def _text_guard(value: str) -> None:
    if _SECRET_RE.search(value):
        raise PointLibraryValidationError("point library contains secret-looking content")
    if _ABS_PATH_RE.search(value):
        raise PointLibraryValidationError("point library contains a private absolute path")


def _mapping(value: Any, field: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise PointLibraryValidationError(f"{field} must be an object")
    return value


def _nonempty_string(value: Any, field: str, *, max_len: int = 256) -> str:
    text = str(value or "").strip()
    if not text or len(text) > max_len or "\n" in text:
        raise PointLibraryValidationError(f"{field} must be a non-empty single-line string")
    return text


def _validate_contract(meta: lus.LusMeta, catalog: dict[str, Any], verification: dict[str, Any]) -> dict[str, Any]:
    if meta.kind != "point_library" or not isinstance(meta.point_library, dict):
        raise PointLibraryValidationError("manifest.lus must declare kind: point_library")
    contract = dict(meta.point_library)
    shape = _nonempty_string(contract.get("shape"), "point_library.shape", max_len=16)
    if shape not in {"A", "B"}:
        raise PointLibraryValidationError("point_library.shape must be A or B")
    category = _nonempty_string(contract.get("category"), "point_library.category", max_len=32)
    if category not in {"SYNTHESIS", "TRANSFORM"}:
        raise PointLibraryValidationError(
            "point_library.category must be SYNTHESIS or TRANSFORM"
        )
    parameters = _mapping(meta.parameters, "manifest.parameters")
    if parameters.get("type") != "object":
        raise PointLibraryValidationError("manifest.parameters.type must be object")
    parameter_properties = _mapping(
        parameters.get("properties"), "manifest.parameters.properties"
    )
    if not parameter_properties:
        raise PointLibraryValidationError("manifest.parameters.properties must not be empty")
    if not any(
        isinstance(spec, dict) and "default" in spec
        for spec in parameter_properties.values()
    ):
        raise PointLibraryValidationError(
            "manifest.parameters.properties must declare at least one default"
        )
    output = _mapping(contract.get("output"), "point_library.output")
    for field in ("artifact", "next", "errors"):
        if field not in output:
            raise PointLibraryValidationError(f"point_library.output requires {field}")
    errors = _mapping(output.get("errors"), "point_library.output.errors")
    for code, detail in errors.items():
        if not re.fullmatch(r"E_[A-Z0-9_]+", str(code)):
            raise PointLibraryValidationError(f"invalid typed error code: {code!r}")
        detail_map = _mapping(detail, f"point_library.output.errors.{code}")
        if not isinstance(detail_map.get("recoverable"), bool):
            raise PointLibraryValidationError(
                f"point_library.output.errors.{code}.recoverable must be boolean"
            )
    implementation = _mapping(contract.get("implementation"), "point_library.implementation")
    mode = _nonempty_string(implementation.get("mode"), "implementation.mode", max_len=16)
    if mode not in {"builtin", "ops"}:
        raise PointLibraryValidationError("implementation.mode must be builtin or ops")
    if mode == "builtin":
        entrypoint = _nonempty_string(implementation.get("entrypoint"), "implementation.entrypoint")
        if not _TOOL_RE.fullmatch(entrypoint):
            raise PointLibraryValidationError("implementation.entrypoint must be a tool name")
        if entrypoint not in meta.tools_used:
            raise PointLibraryValidationError(
                "implementation.entrypoint must also appear in tools_used"
            )
    else:
        operations = _mapping(contract.get("operations"), "point_library.operations")
        required_ops = {"apply", "describe"} if shape == "B" else {"create", "adjust", "catalog"}
        missing = sorted(required_ops - set(operations))
        if missing:
            raise PointLibraryValidationError(
                f"point_library.operations is missing required operations: {missing}"
            )
        for operation, spec in operations.items():
            spec_map = _mapping(spec, f"point_library.operations.{operation}")
            target = _nonempty_string(spec_map.get("tool"), f"operation {operation}.tool")
            if not _TOOL_RE.fullmatch(target) or target in {
                "run_shell", "file_write", "file_delete", "write_file", "build",
            }:
                raise PointLibraryValidationError(
                    f"operation {operation}.tool is not an allowed Lumeri tool"
                )
            args = spec_map.get("args", {})
            if not isinstance(args, dict):
                raise PointLibraryValidationError(f"operation {operation}.args must be an object")

    entries = catalog.get("entries")
    if not isinstance(entries, list) or not entries:
        raise PointLibraryValidationError("catalog.json requires a non-empty entries list")
    seen: set[str] = set()
    for index, item in enumerate(entries):
        item_map = _mapping(item, f"catalog.entries[{index}]")
        item_id = _nonempty_string(item_map.get("id"), f"catalog.entries[{index}].id", max_len=64)
        if not _ID_RE.fullmatch(item_id) or item_id in seen:
            raise PointLibraryValidationError(
                f"catalog.entries[{index}].id must be unique kebab-case"
            )
        seen.add(item_id)
        _nonempty_string(item_map.get("title"), f"catalog.entries[{index}].title", max_len=120)

    if verification.get("deterministic") is not True:
        raise PointLibraryValidationError("verification.deterministic must be true")
    for key in ("taste_floor", "tests"):
        value = verification.get(key)
        if not isinstance(value, list) or not value:
            raise PointLibraryValidationError(f"verification.{key} must be a non-empty list")
    return contract


@dataclass(frozen=True)
class PointLibraryPackage:
    """Validated immutable Point Library bundle."""

    raw: bytes
    meta: lus.LusMeta
    body: str
    contract: dict[str, Any]
    catalog: dict[str, Any]
    verification: dict[str, Any]
    members: tuple[str, ...]
    content_sha256: str

    @classmethod
    def from_bytes(cls, raw: bytes | bytearray) -> "PointLibraryPackage":
        data = bytes(raw)
        if len(data) > MAX_BUNDLE_BYTES:
            raise PointLibraryValidationError(
                f"point library bundle exceeds {MAX_BUNDLE_BYTES} bytes"
            )
        if not data.startswith(b"PK"):
            raise PointLibraryValidationError("point library bundle must be a ZIP .lus file")
        try:
            archive = zipfile.ZipFile(io.BytesIO(data))
            infos = archive.infolist()
        except (OSError, zipfile.BadZipFile) as exc:
            raise PointLibraryValidationError("point library bundle is not a valid ZIP") from exc
        if not infos or len(infos) > MAX_MEMBER_COUNT:
            raise PointLibraryValidationError("point library bundle has an invalid member count")
        names: list[str] = []
        members: dict[str, bytes] = {}
        try:
            for info in infos:
                name = str(info.filename)
                if info.is_dir() or not _safe_member_name(name) or name in members:
                    raise PointLibraryValidationError(f"invalid or duplicate bundle member: {name!r}")
                if name not in PACKAGE_ENTRIES and not name.startswith(("runtime/", "ops/", "verification/")):
                    raise PointLibraryValidationError(f"unsupported bundle member: {name!r}")
                if info.file_size > MAX_MEMBER_BYTES:
                    raise PointLibraryValidationError(f"bundle member is too large: {name!r}")
                # ZIP symlinks are not files in the Point Library contract.
                if (info.external_attr >> 16) & 0o170000 == 0o120000:
                    raise PointLibraryValidationError(f"symlink bundle member is forbidden: {name!r}")
                value = archive.read(info)
                members[name] = value
                names.append(name)
        finally:
            archive.close()
        missing = PACKAGE_ENTRIES - set(names)
        if missing:
            raise PointLibraryValidationError(f"bundle is missing required members: {sorted(missing)}")

        manifest = members["manifest.lus"]
        try:
            manifest_text = manifest.decode("utf-8")
            catalog = json.loads(members["catalog.json"].decode("utf-8"))
            verification = json.loads(members["verification.json"].decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise PointLibraryValidationError("manifest/catalog/verification must be UTF-8 JSON/text") from exc
        _text_guard(manifest_text)
        _text_guard(json.dumps(catalog, ensure_ascii=False))
        _text_guard(json.dumps(verification, ensure_ascii=False))
        meta, body, _warnings = lus.validate_lus(manifest_text, strict=True)
        catalog_map = _mapping(catalog, "catalog.json")
        verification_map = _mapping(verification, "verification.json")
        contract = _validate_contract(meta, catalog_map, verification_map)
        implementation = _mapping(contract.get("implementation"), "implementation")
        mode = str(implementation.get("mode") or "")
        declaration_name = "runtime/manifest.json" if mode == "builtin" else "ops/manifest.json"
        if declaration_name not in members:
            raise PointLibraryValidationError(
                f"{mode} point library requires {declaration_name}"
            )
        try:
            declaration = json.loads(members[declaration_name].decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise PointLibraryValidationError(
                f"{declaration_name} must be a UTF-8 JSON object"
            ) from exc
        _mapping(declaration, declaration_name)
        return cls(
            raw=data,
            meta=meta,
            body=body,
            contract=contract,
            catalog=catalog_map,
            verification=verification_map,
            members=tuple(sorted(names)),
            content_sha256=_sha256(data),
        )

    @classmethod
    def from_file(cls, path: str | os.PathLike[str]) -> "PointLibraryPackage":
        candidate = Path(path).expanduser()
        if not candidate.is_file():
            raise PointLibraryValidationError(f"point library file not found: {candidate}")
        return cls.from_bytes(candidate.read_bytes())

    def to_base64(self) -> str:
        import base64

        return base64.b64encode(self.raw).decode("ascii")

    def summary(self, *, source: str = "installed", active: bool = False) -> dict[str, Any]:
        implementation = self.contract.get("implementation") or {}
        return {
            "kind": "point_library",
            "id": self.meta.name,
            "version": self.meta.version,
            "title": self.meta.title,
            "description": self.meta.description,
            "domain": self.meta.domain,
            "shape": self.contract.get("shape"),
            "category": self.contract.get("category"),
            "implementation": implementation.get("mode", ""),
            "entrypoint": implementation.get("entrypoint", ""),
            "catalog_count": len(self.catalog.get("entries") or []),
            "verified": bool(self.verification.get("deterministic") is True),
            "source": source,
            "active": active,
            "content_sha256": self.content_sha256,
        }

    async def dispatch(self, args: dict[str, Any], ctx: Any) -> dict[str, Any]:
        operation = str(args.get("op") or "catalog").strip().lower()
        shape = str(self.contract.get("shape") or "A")
        if operation in {"catalog", "describe"}:
            return {
                "applied": True,
                "point_library": self.meta.name,
                "version": self.meta.version,
                "shape": shape,
                "catalog": self.catalog,
                "verification": {
                    "deterministic": self.verification.get("deterministic"),
                    "taste_floor": self.verification.get("taste_floor"),
                    "tests": self.verification.get("tests"),
                },
                "next": (
                    "call op:'create' or op:'adjust' with semantic parameters"
                    if shape == "A"
                    else "call op:'apply' with a semantic payload, then verify the artifact"
                ),
            }
        allowed = {"create", "adjust"} if shape == "A" else {"apply"}
        if operation not in allowed:
            return {
                "applied": False,
                "error_code": "E_ARG",
                "error_message": f"{self.meta.name}: unknown op {operation!r}",
                "recoverable": True,
            }
        implementation = _mapping(self.contract.get("implementation"), "implementation")
        mode = str(implementation.get("mode") or "")
        if mode == "builtin":
            target = str(implementation.get("entrypoint") or "")
            from gemia.tools import DISPATCHER

            dispatcher = DISPATCHER.get(target)
            if dispatcher is None:
                raise PointLibraryValidationError(
                    f"installed native point library target is unavailable: {target}"
                )
            forwarded = dict(args)
            forwarded.pop("library_id", None)
            result = await dispatcher(forwarded, ctx)
            if isinstance(result, dict) and result.get("applied") is False:
                result.setdefault("recoverable", True)
            return result

        operations = _mapping(self.contract.get("operations"), "operations")
        spec = _mapping(operations.get(operation), f"operations.{operation}")
        target = str(spec.get("tool") or "")
        from gemia.tools import DISPATCHER

        dispatcher = DISPATCHER.get(target)
        if dispatcher is None:
            raise PointLibraryValidationError(f"ops target is unavailable: {target}")
        forwarded = dict(spec.get("args") or {})
        payload = args.get("payload")
        if isinstance(payload, dict):
            forwarded.update(payload)
        if spec.get("op") is not None:
            forwarded["op"] = spec["op"]
        result = await dispatcher(forwarded, ctx)
        if isinstance(result, dict) and result.get("applied") is False:
            result.setdefault("recoverable", True)
        return result


def _manifest_text(
    *,
    name: str,
    version: str,
    title: str,
    description: str,
    domain: str,
    triggers: list[str],
    tools_used: list[str],
    contract: dict[str, Any],
    author: str = "lumeri",
) -> str:
    contract = dict(contract)
    contract.setdefault(
        "parameters",
        {
            "type": "object",
            "properties": {
                "brief": {
                    "type": "object",
                    "description": "semantic creative intent",
                    "default": {},
                },
                "feedback": {
                    "type": "array",
                    "items": {"type": "string"},
                    "default": [],
                },
                "target_asset_id": {
                    "type": "string",
                    "description": "asset handle to transform",
                    "default": "",
                },
            },
        },
    )
    contract.setdefault(
        "output",
        {
            "artifact": "object",
            "next": "string",
            "errors": {
                "E_ARG": {"recoverable": True},
                "E_NOT_FOUND": {"recoverable": True},
                "E_RENDER": {"recoverable": True},
                "E_NOT_AVAILABLE": {"recoverable": True},
            },
        },
    )
    body = (
        f"\n## When to use\nUse the {title} point library for its declared creative domain.\n\n"
        "## Steps\n"
        "1. Call the point library with semantic creative intent and its named catalog.\n"
        "2. Inspect the returned artifact, plan, or recipe and follow its next verification step.\n\n"
        "## Pitfalls\nDo not bypass the library with raw craft recipes or coordinates.\n"
    )
    now = "2026-01-01T00:00:00+00:00"
    meta = lus.LusMeta(
        name=name,
        version=version,
        lus_version=1,
        title=title,
        description=description,
        triggers=tuple(triggers),
        domain=domain,
        tools_used=tuple(tools_used),
        parameters=contract["parameters"],
        author=author,
        created_at=now,
        updated_at=now,
        language="en",
        safety_requires_paid_generation=False,
        safety_mutates_project=contract.get("shape") == "A",
        checksum=None,
        kind="point_library",
        point_library=contract,
        extra={},
    )
    return lus.serialize_lus(meta, body)


def build_point_library_bundle(
    *,
    manifest_text: str,
    catalog: dict[str, Any],
    verification: dict[str, Any],
    runtime_manifest: dict[str, Any] | None = None,
    ops: dict[str, Any] | None = None,
    extra_files: Mapping[str, bytes | str] | None = None,
) -> bytes:
    """Build a deterministic ``.lus`` ZIP bundle and validate it."""
    members: dict[str, bytes] = {
        "manifest.lus": manifest_text.encode("utf-8"),
        "catalog.json": _json_bytes(catalog),
        "verification.json": _json_bytes(verification),
    }
    if runtime_manifest is not None:
        members["runtime/manifest.json"] = _json_bytes(runtime_manifest)
    if ops is not None:
        members["ops/manifest.json"] = _json_bytes(ops)
    for name, value in (extra_files or {}).items():
        if name in members or not _safe_member_name(name):
            raise PointLibraryValidationError(f"invalid extra bundle member: {name!r}")
        members[name] = value.encode("utf-8") if isinstance(value, str) else bytes(value)
    if len(members) > MAX_MEMBER_COUNT or any(len(value) > MAX_MEMBER_BYTES for value in members.values()):
        raise PointLibraryValidationError("point library bundle has an invalid member size")
    output = io.BytesIO()
    with zipfile.ZipFile(output, mode="w", compression=zipfile.ZIP_DEFLATED, compresslevel=9) as archive:
        for name in sorted(members):
            info = zipfile.ZipInfo(name, date_time=(1980, 1, 1, 0, 0, 0))
            info.compress_type = zipfile.ZIP_DEFLATED
            info.external_attr = 0o600 << 16
            archive.writestr(info, members[name])
    raw = output.getvalue()
    return PointLibraryPackage.from_bytes(raw).raw


def _builtin_package(name: str) -> PointLibraryPackage | None:
    if name == "vector-motion":
        from lumenframe.vector.catalog import vector_catalog

        contract = {
            "shape": "A",
            "category": "SYNTHESIS",
            "implementation": {"mode": "builtin", "entrypoint": "vector_motion"},
        }
        catalog = {
            "entries": [
                {"id": "vector-motion", "title": "Vector motion choreography"},
                {"id": "catalog", "title": "Vector vocabulary"},
            ],
            "vocabulary": vector_catalog(),
        }
        manifest = _manifest_text(
            name=name,
            version="1.0.0",
            title="Vector Motion",
            description="Human-validated vector motion choreography.",
            domain="video",
            triggers=["vector motion", "logo animation", "motion graphics"],
            tools_used=["vector_motion"],
            contract=contract,
        )
        return PointLibraryPackage.from_bytes(
            build_point_library_bundle(
                manifest_text=manifest,
                catalog=catalog,
                verification={
                    "deterministic": True,
                    "taste_floor": ["semantic brief", "phase choreography", "render-safe output"],
                    "tests": ["vector_determinism_subprocess", "vector_taste_floor_property"],
                },
                runtime_manifest={"api": "lumeri-point-library/v1", "entrypoint": "vector_motion"},
            )
        )
    if name == "grade":
        from lumenframe.grade.catalog import grade_catalog

        contract = {
            "shape": "A",
            "category": "TRANSFORM",
            "implementation": {"mode": "builtin", "entrypoint": "grade"},
        }
        catalog = {
            "entries": [
                {"id": "grade", "title": "Creative colour grading"},
                {"id": "catalog", "title": "Grade vocabulary"},
            ],
            "vocabulary": grade_catalog(),
        }
        manifest = _manifest_text(
            name=name,
            version="1.0.0",
            title="Creative Grade",
            description="Human-validated colour grading looks and recipes.",
            domain="video",
            triggers=["colour grade", "color grade", "film look"],
            tools_used=["grade"],
            contract=contract,
        )
        return PointLibraryPackage.from_bytes(
            build_point_library_bundle(
                manifest_text=manifest,
                catalog=catalog,
                verification={
                    "deterministic": True,
                    "taste_floor": ["protected tone curve", "skin-safe default", "validated SVG"],
                    "tests": ["grade_craft", "grade_determinism"],
                },
                runtime_manifest={"api": "lumeri-point-library/v1", "entrypoint": "grade"},
            )
        )
    return None


@dataclass(frozen=True)
class InstalledPointLibrary:
    package: PointLibraryPackage
    path: Path | None
    source: str
    active: bool

    def summary(self) -> dict[str, Any]:
        return self.package.summary(source=self.source, active=self.active)


class PointLibraryRegistry:
    """Transactional local registry for installed Point Library bundles."""

    def __init__(self, root_dir: str | os.PathLike[str] | None = None) -> None:
        self.root_dir = Path(root_dir).expanduser() if root_dir else Path.home() / ".gemia" / "point-libraries"
        self.root_dir.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()

    @property
    def _active_file(self) -> Path:
        return self.root_dir / "active.json"

    def _active(self) -> dict[str, str]:
        try:
            value = json.loads(self._active_file.read_text(encoding="utf-8"))
        except (FileNotFoundError, OSError, json.JSONDecodeError):
            return {}
        return {str(key): str(version) for key, version in value.items()} if isinstance(value, dict) else {}

    def _write_active(self, active: dict[str, str]) -> None:
        self.root_dir.mkdir(parents=True, exist_ok=True)
        fd, temp_name = tempfile.mkstemp(prefix=".active-", suffix=".json", dir=self.root_dir)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                json.dump(active, handle, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
                handle.write("\n")
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temp_name, self._active_file)
        finally:
            if os.path.exists(temp_name):
                os.unlink(temp_name)

    def _package_path(self, package: PointLibraryPackage) -> Path:
        return self.root_dir / package.meta.name / package.meta.version / f"{package.meta.name}.lus"

    def _load_path(self, path: Path) -> PointLibraryPackage:
        return PointLibraryPackage.from_file(path)

    def _installed(self, name: str) -> list[tuple[Path, PointLibraryPackage]]:
        base = self.root_dir / name
        if not base.is_dir():
            return []
        values: list[tuple[Path, PointLibraryPackage]] = []
        for path in sorted(base.glob("*/" + name + ".lus")):
            try:
                values.append((path, self._load_path(path)))
            except PointLibraryError:
                continue
        return values

    def install(self, source: PointLibraryPackage | bytes | bytearray | str | os.PathLike[str]) -> InstalledPointLibrary:
        package = source if isinstance(source, PointLibraryPackage) else (
            PointLibraryPackage.from_file(source) if isinstance(source, (str, os.PathLike))
            else PointLibraryPackage.from_bytes(source)
        )
        with self._lock:
            target = self._package_path(package)
            target.parent.mkdir(parents=True, exist_ok=True)
            if target.exists():
                existing = PointLibraryPackage.from_file(target)
                if existing.content_sha256 != package.content_sha256:
                    raise PointLibraryConflict(
                        f"{package.meta.name} {package.meta.version} already exists with different bytes"
                    )
            else:
                fd, temp_name = tempfile.mkstemp(prefix=".package-", suffix=".lus", dir=target.parent)
                created = False
                activated = False
                try:
                    with os.fdopen(fd, "wb") as handle:
                        handle.write(package.raw)
                        handle.flush()
                        os.fsync(handle.fileno())
                    os.replace(temp_name, target)
                    created = True
                    active = self._active()
                    active[package.meta.name] = package.meta.version
                    self._write_active(active)
                    activated = True
                finally:
                    if os.path.exists(temp_name):
                        os.unlink(temp_name)
                    if created and not activated:
                        # The activation write failed before the package became
                        # observable; remove only this exact newly-created file.
                        target.unlink(missing_ok=True)
            if target.exists() and self._active().get(package.meta.name) != package.meta.version:
                active = self._active()
                active[package.meta.name] = package.meta.version
                self._write_active(active)
            return InstalledPointLibrary(package, target, "installed", True)

    def activate(self, name: str, version: str) -> InstalledPointLibrary:
        with self._lock:
            for path, package in self._installed(name):
                if package.meta.version == version:
                    active = self._active()
                    active[name] = version
                    self._write_active(active)
                    return InstalledPointLibrary(package, path, "installed", True)
        raise PointLibraryNotFound(f"installed point library not found: {name} {version}")

    def resolve(self, name: str) -> InstalledPointLibrary:
        clean_name = str(name or "").strip()
        if not _ID_RE.fullmatch(clean_name):
            raise PointLibraryNotFound(f"invalid point library id: {clean_name!r}")
        with self._lock:
            active_version = self._active().get(clean_name)
            installed = self._installed(clean_name)
            if active_version:
                for path, package in installed:
                    if package.meta.version == active_version:
                        return InstalledPointLibrary(package, path, "installed", True)
            if installed:
                path, package = installed[-1]
                return InstalledPointLibrary(package, path, "installed", False)
        builtin = _builtin_package(clean_name)
        if builtin is not None:
            return InstalledPointLibrary(builtin, None, "builtin", True)
        raise PointLibraryNotFound(f"point library is not installed: {clean_name}")

    def list(self) -> list[dict[str, Any]]:
        with self._lock:
            active = self._active()
            values: dict[tuple[str, str], InstalledPointLibrary] = {}
            for name_dir in sorted(self.root_dir.iterdir() if self.root_dir.exists() else []):
                if not name_dir.is_dir() or name_dir.name.startswith("."):
                    continue
                for path, package in self._installed(name_dir.name):
                    values[(package.meta.name, package.meta.version)] = InstalledPointLibrary(
                        package, path, "installed", active.get(package.meta.name) == package.meta.version
                    )
        for name in ("vector-motion", "grade"):
            if not any(key[0] == name for key in values):
                builtin = _builtin_package(name)
                if builtin is not None:
                    values[(name, builtin.meta.version)] = InstalledPointLibrary(builtin, None, "builtin", True)
        return [item.summary() for item in sorted(values.values(), key=lambda item: (item.package.meta.name, item.package.meta.version))]

    async def dispatch(self, args: dict[str, Any], ctx: Any) -> dict[str, Any]:
        name = str(args.get("library_id") or args.get("library") or "").strip()
        if not name:
            return {"applied": False, "error_code": "E_ARG", "error_message": "point_library requires library_id"}
        item = self.resolve(name)
        result = await item.package.dispatch(args, ctx)
        if isinstance(result, dict):
            result.setdefault("point_library", item.package.meta.name)
            result.setdefault("point_library_version", item.package.meta.version)
        return result


_DEFAULT_REGISTRY: PointLibraryRegistry | None = None
_DEFAULT_LOCK = threading.Lock()


def default_point_library_registry() -> PointLibraryRegistry:
    global _DEFAULT_REGISTRY
    if _DEFAULT_REGISTRY is None:
        with _DEFAULT_LOCK:
            if _DEFAULT_REGISTRY is None:
                _DEFAULT_REGISTRY = PointLibraryRegistry()
    return _DEFAULT_REGISTRY


def install_point_library(source: PointLibraryPackage | bytes | bytearray | str | os.PathLike[str]) -> dict[str, Any]:
    return default_point_library_registry().install(source).summary()


__all__ = [
    "MAX_BUNDLE_BYTES",
    "MAX_MEMBER_BYTES",
    "PointLibraryConflict",
    "PointLibraryError",
    "PointLibraryNotFound",
    "PointLibraryPackage",
    "PointLibraryRegistry",
    "PointLibraryValidationError",
    "build_point_library_bundle",
    "default_point_library_registry",
    "install_point_library",
]
