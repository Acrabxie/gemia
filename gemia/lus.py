""".lus — Lumeri Skill File format v1: parse, validate, serialize.

Implements ``docs/lus-skill-format.md`` (the single normative reference):

* :func:`validate_lus` — full validation with the spec's exact check order,
  raising :class:`LusValidationError` (15 typed ``E_LUS_*`` codes) on the
  FIRST failing check and returning ``(meta, body, warnings)`` where
  ``warnings`` carries the 5 ``W_LUS_*`` codes (spec D7).
* :func:`parse_lus` — validate and discard warnings.
* :func:`serialize_lus` — the canonical hand-rolled emitter (spec D5): fixed
  field order, block style, 2-space indent, LF only, fresh checksum. Output
  is byte-stable and round-trip stable against both §9 reference examples.
* :func:`scan_lus_meta` — bounded metadata-only parse of a file prefix (≤8
  KiB, spec D6) for the cheap recall scan (§7.2).
* :func:`derive_name` / :func:`detect_language` / :func:`extract_tools_used`
  — the §7.1 derivation helpers shared by the store and the migrator.

This module deliberately imports nothing from ``gemia.tools`` (spec §6): the
store and tool dispatchers import *it*.  YAML parsing goes through
``gemia.ai.skill_yaml.safe_load`` so lean environments without PyYAML keep
working (both §9 examples parse under the fallback shim).
"""
from __future__ import annotations

import hashlib
import json
import re
import unicodedata
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from gemia.ai.skill_yaml import safe_load

# ── Constants (spec §2) ──────────────────────────────────────────────────

SUPPORTED_LUS_MAJORS = frozenset({1})
MAX_FILE_BYTES = 65_536   # §2.2 — whole file, checked before any parsing
META_MAX_BYTES = 8_192    # D6 — meta close fence must occur within this

DOMAINS = ("video", "deck", "cad", "general")
LANGUAGES = ("zh", "en", "mixed")

# Paid generation verbs (per gemia/budget_guard.py pricing — §3.2 `safety`).
PAID_GENERATION_TOOLS = frozenset({
    "generate_image", "generate_video", "generate_audio", "narrate",
})

# Canonical writer field order == §3.2 table order.
FIELD_ORDER = (
    "name", "version", "lus_version", "title", "description", "triggers",
    "domain", "tools_used", "parameters", "author", "created_at",
    "updated_at", "language", "safety", "checksum",
)

_MAGIC_RE = re.compile(r"^#!lus/[1-9][0-9]*$")
_NAME_RE = re.compile(r"^[a-z0-9]+(-[a-z0-9]+)*$")
_SEMVER_RE = re.compile(r"^(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)$")
_CHECKSUM_RE = re.compile(r"^sha256:[0-9a-f]{64}$")
_TOOL_NAME_RE = re.compile(r"^[a-z][a-z0-9_]*$")
_STEP_ITEM_RE = re.compile(r"^[0-9]+\. ")
_FENCE_RE = re.compile(r"^```")

KNOWN_HEADINGS = ("## When to use", "## Steps", "## Pitfalls", "## Examples")

# §4.2.1 — secret battery (same shapes as the gemia/memory.py guard).
# (class label, pattern); the label goes into the error message, the matched
# text NEVER does.
_SECRET_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("provider API key (sk-…)", re.compile(r"sk-[A-Za-z0-9]{16,}")),
    ("AWS access key id (AKIA…)", re.compile(r"AKIA[0-9A-Z]{16}")),
    ("bearer token", re.compile(r"(?i)bearer\s+[a-z0-9._-]{16,}")),
    ("private key block", re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----")),
    (
        "credential assignment",
        re.compile(r"(?i)(api[_-]?key|token|secret|passwd|password)\s*[:=]\s*\S{8,}"),
    ),
)

# §4.2.2 — absolute user paths (multiline).
_ABS_PATH_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"""(^|[\s"'`(=])/(Users|home|Volumes)/""", re.MULTILINE),
    re.compile(r"[A-Za-z]:\\Users\\"),
    re.compile(r"""(^|[\s"'`(=])~/""", re.MULTILINE),
)

# §3.2 `parameters` — JSON-Schema subset.
_SCHEMA_ALLOWED_KEYS = frozenset({
    "type", "properties", "items", "required", "description", "enum",
    "default", "minimum", "maximum", "minItems", "maxItems",
})
_SCHEMA_ALLOWED_TYPES = frozenset({
    "object", "string", "number", "integer", "boolean", "array",
})
_SCHEMA_MAX_DEPTH = 4
_SCHEMA_MAX_ROOT_PROPS = 16

_DEFAULT_PARAMETERS: dict[str, Any] = {"type": "object", "properties": {}}


# ── Result / error types (spec §6) ───────────────────────────────────────


@dataclass(frozen=True)
class LusMeta:
    name: str
    version: str
    lus_version: int
    title: str
    description: str
    triggers: tuple[str, ...]
    domain: str                      # "video" | "deck" | "cad" | "general"
    tools_used: tuple[str, ...]
    parameters: dict                 # JSON-Schema subset, §3.2
    author: str
    created_at: str                  # ISO 8601, tz-aware
    updated_at: str
    language: str                    # "zh" | "en" | "mixed"
    safety_requires_paid_generation: bool
    safety_mutates_project: bool
    checksum: str | None
    extra: dict = field(default_factory=dict)  # unknown fields, round-trip


@dataclass(frozen=True)
class LusWarning:
    code: str                        # W_LUS_*
    message: str
    field: str | None = None


class LusValidationError(ValueError):
    """Typed .lus validation failure (E_LUS_* codes, spec §6.1)."""

    def __init__(self, code: str, message: str, *,
                 field: str | None = None, line: int | None = None) -> None:
        super().__init__(f"{code}: {message}")
        self.code = code
        self.message = message
        self.field = field
        self.line = line


def _err(code: str, message: str, *, field: str | None = None,
         line: int | None = None) -> LusValidationError:
    return LusValidationError(code, message, field=field, line=line)


# ── Derivation helpers (spec §7.1, shared with store + migrator) ─────────


def derive_name(title: str) -> str:
    """Derive the ASCII kebab-case machine ``name`` from a display title.

    §7.1: NFKD → lowercase → non-``[a-z0-9]`` runs → ``-`` → collapse/trim;
    if the result is empty (pure-CJK input), ``skill-<sha256(title)[:8]>``.
    Deterministic so re-saving the same title hits the same file.
    """
    text = str(title or "")
    normalized = unicodedata.normalize("NFKD", text).lower()
    slug = re.sub(r"[^a-z0-9]+", "-", normalized).strip("-")
    slug = re.sub(r"-+", "-", slug)
    slug = slug[:64].strip("-")
    if not slug:
        digest = hashlib.sha256(text.encode("utf-8")).hexdigest()[:8]
        return f"skill-{digest}"
    return slug


_CJK_RE = re.compile(r"[぀-ヿ㐀-䶿一-鿿豈-﫿]")
_CODE_SPAN_RE = re.compile(r"`[^`\n]*`")


def detect_language(text: str) -> str:
    """§7.1 language heuristic: ``zh`` / ``en`` / ``mixed``.

    Backticked code spans and fenced blocks are ignored so exact tool names
    inside a Chinese playbook do not flip it to ``mixed``.
    """
    prose_lines: list[str] = []
    in_fence = False
    for line in str(text or "").split("\n"):
        if _FENCE_RE.match(line):
            in_fence = not in_fence
            continue
        if not in_fence:
            prose_lines.append(_CODE_SPAN_RE.sub(" ", line))
    prose = "\n".join(prose_lines)
    cjk = len(_CJK_RE.findall(prose))
    ascii_letters = len(re.findall(r"[A-Za-z]", prose))
    if cjk == 0:
        return "en"
    if ascii_letters > cjk:
        return "mixed"
    return "zh"


def extract_tools_used(text: str, known_tools: frozenset[str] | set[str]) -> list[str]:
    """§7.1 ``tools_used`` auto-extraction from steps text.

    Every token matching ``^[a-z][a-z0-9_]*$`` that is a known tool name, in
    first-appearance order, deduplicated. Unknown tokens simply are not
    extracted.
    """
    if not known_tools:
        return []
    seen: set[str] = set()
    out: list[str] = []
    for token in re.findall(r"\b[a-z][a-z0-9_]*\b", str(text or "")):
        if token in known_tools and token not in seen:
            seen.add(token)
            out.append(token)
    return out


def body_checksum(body: str) -> str:
    """§5: ``sha256:`` + lowercase hex SHA-256 of the raw body bytes."""
    return "sha256:" + hashlib.sha256(body.encode("utf-8")).hexdigest()


# ── Internal: framing ────────────────────────────────────────────────────


def _split_frames(text: str) -> tuple[int, str, str, int]:
    """Return ``(major, meta_text, body, close_line)`` or raise E_LUS_*.

    Assumes encoding/size checks already ran. ``close_line`` is the 1-based
    line number of the close fence.
    """
    lines = text.split("\n")
    if not lines or not _MAGIC_RE.match(lines[0]):
        raise _err("E_LUS_MAGIC",
                   "line 1 must be exactly '#!lus/<major>' (regex ^#!lus/[1-9][0-9]*$)",
                   line=1)
    major = int(lines[0][len("#!lus/"):])
    if major not in SUPPORTED_LUS_MAJORS:
        raise _err(
            "E_LUS_VERSION",
            f".lus major version {major} is not supported "
            f"(supported: {sorted(SUPPORTED_LUS_MAJORS)}); upgrade Lumeri to read this skill",
            line=1,
        )
    if len(lines) < 2 or lines[1] != "---":
        raise _err("E_LUS_META_OPEN", "line 2 must be exactly '---'", line=2)

    close_idx: int | None = None
    for i in range(2, len(lines)):
        if lines[i] == "---":
            close_idx = i
            break
    total_bytes = len(text.encode("utf-8"))
    if close_idx is None:
        if total_bytes > META_MAX_BYTES:
            raise _err("E_LUS_META_TOO_LARGE",
                       f"metadata close fence not found within the first {META_MAX_BYTES} bytes")
        raise _err("E_LUS_META_UNTERMINATED",
                   "EOF reached without a metadata close fence ('---')")
    fence_end = len("\n".join(lines[: close_idx + 1]).encode("utf-8"))
    if fence_end > META_MAX_BYTES:
        raise _err("E_LUS_META_TOO_LARGE",
                   f"metadata close fence at byte {fence_end} exceeds the "
                   f"{META_MAX_BYTES}-byte bound", line=close_idx + 1)

    meta_text = "\n".join(lines[2:close_idx])
    body = "\n".join(lines[close_idx + 1:])
    return major, meta_text, body, close_idx + 1


def _scan_forbidden_yaml(meta_text: str, first_meta_line: int) -> None:
    """Reject anchors/aliases/tags (§3.1) with E_LUS_META_PARSE."""
    for offset, raw in enumerate(meta_text.split("\n")):
        stripped = raw.strip()
        if not stripped or stripped.startswith("#"):
            continue
        line_no = first_meta_line + offset
        candidate = stripped
        while candidate.startswith("- "):
            candidate = candidate[2:].lstrip()
        if candidate.startswith(("&", "*", "!")):
            raise _err("E_LUS_META_PARSE",
                       "forbidden YAML feature (anchor/alias/tag) in metadata",
                       line=line_no)
        m = re.match(r"""^[^:'"\n]+:(?:\s+(.*))?$""", candidate)
        if m:
            value = (m.group(1) or "").strip()
            if value.startswith(("&", "*", "!")):
                raise _err("E_LUS_META_PARSE",
                           "forbidden YAML feature (anchor/alias/tag) in metadata",
                           line=line_no)


def _check_string_keys(node: Any) -> None:
    if isinstance(node, dict):
        for key, value in node.items():
            if not isinstance(key, str):
                raise _err("E_LUS_META_PARSE",
                           f"non-string mapping key of type {type(key).__name__} in metadata")
            _check_string_keys(value)
    elif isinstance(node, list):
        for item in node:
            _check_string_keys(item)


def _normalize_flow_empties(value: Any) -> Any:
    """Map exact ``"{}"`` strings to ``{}`` (fallback-shim flow-empty gap)."""
    if value == "{}":
        return {}
    if isinstance(value, dict):
        return {k: _normalize_flow_empties(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_normalize_flow_empties(v) for v in value]
    return value


def _parse_meta_mapping(meta_text: str, first_meta_line: int) -> dict[str, Any]:
    _scan_forbidden_yaml(meta_text, first_meta_line)
    try:
        parsed = safe_load(meta_text)
    except Exception as exc:
        raise _err("E_LUS_META_PARSE", f"metadata block failed to parse: {exc}") from exc
    if not isinstance(parsed, dict):
        raise _err("E_LUS_META_PARSE",
                   f"metadata block must parse to a mapping, got {type(parsed).__name__}")
    _check_string_keys(parsed)
    return parsed


# ── Internal: field validation (§3.2, E_LUS_META_FIELD) ──────────────────


def _field_err(field_name: str, message: str) -> LusValidationError:
    return _err("E_LUS_META_FIELD", f"{field_name}: {message}", field=field_name)


def _req_str(mapping: dict[str, Any], field_name: str, *, max_len: int,
             single_line: bool = True) -> str:
    if field_name not in mapping:
        raise _field_err(field_name, "required field missing")
    value = mapping[field_name]
    if not isinstance(value, str):
        raise _field_err(field_name, f"must be a string, got {type(value).__name__}")
    if single_line and "\n" in value:
        raise _field_err(field_name, "must be a single line")
    if not value.strip():
        raise _field_err(field_name, "must be non-empty")
    if len(value) > max_len:
        raise _field_err(field_name, f"exceeds max length {max_len}")
    return value


def _timestamp(mapping: dict[str, Any], field_name: str) -> tuple[str, datetime]:
    if field_name not in mapping:
        raise _field_err(field_name, "required field missing")
    value = mapping[field_name]
    if isinstance(value, datetime):        # PyYAML resolves ISO timestamps
        dt = value
        text = dt.isoformat()
    elif isinstance(value, str):
        text = value.strip()
        if not text:
            raise _field_err(field_name, "must be non-empty")
        if len(text) > 40:
            raise _field_err(field_name, "exceeds max length 40")
        try:
            dt = datetime.fromisoformat(text)
        except ValueError as exc:
            raise _field_err(field_name, f"invalid ISO 8601 timestamp: {exc}") from exc
    else:
        raise _field_err(field_name, f"must be an ISO 8601 string, got {type(value).__name__}")
    if dt.tzinfo is None:
        raise _field_err(field_name, "timestamp must carry an explicit UTC offset (tz-aware)")
    return text, dt


def _validate_schema_node(node: Any, path: str, depth: int) -> None:
    if depth > _SCHEMA_MAX_DEPTH:
        raise _field_err(path, f"nesting depth exceeds {_SCHEMA_MAX_DEPTH}")
    if not isinstance(node, dict):
        raise _field_err(path, f"must be a mapping, got {type(node).__name__}")
    for key in node:
        if key not in _SCHEMA_ALLOWED_KEYS:
            raise _field_err(f"{path}.{key}", "key outside the JSON-Schema subset")
    type_value = node.get("type")
    if type_value is not None and (
        not isinstance(type_value, str) or type_value not in _SCHEMA_ALLOWED_TYPES
    ):
        raise _field_err(f"{path}.type",
                         f"must be one of {sorted(_SCHEMA_ALLOWED_TYPES)}")
    properties = node.get("properties")
    if properties is not None:
        if not isinstance(properties, dict):
            raise _field_err(f"{path}.properties", "must be a mapping")
        for prop_name, prop_node in properties.items():
            _validate_schema_node(prop_node, f"{path}.properties.{prop_name}", depth + 1)
    items = node.get("items")
    if items is not None:
        _validate_schema_node(items, f"{path}.items", depth + 1)
    required = node.get("required")
    if required is not None:
        if not isinstance(required, list) or any(not isinstance(r, str) for r in required):
            raise _field_err(f"{path}.required", "must be a list of strings")
        prop_keys = set(properties.keys()) if isinstance(properties, dict) else set()
        missing = [r for r in required if r not in prop_keys]
        if missing:
            raise _field_err(f"{path}.required",
                             f"entries not in properties: {missing}")


def _validate_meta_fields(
    mapping: dict[str, Any],
    major: int,
    warnings: list[LusWarning],
) -> LusMeta:
    # name
    name = _req_str(mapping, "name", max_len=64)
    if not _NAME_RE.match(name):
        raise _field_err("name", "must be ASCII kebab-case (^[a-z0-9]+(-[a-z0-9]+)*$)")
    # version
    version = _req_str(mapping, "version", max_len=32)
    if not _SEMVER_RE.match(version):
        raise _field_err("version", "must be strict semver core X.Y.Z")
    # lus_version
    if "lus_version" not in mapping:
        raise _field_err("lus_version", "required field missing")
    lus_version = mapping["lus_version"]
    if isinstance(lus_version, bool) or not isinstance(lus_version, int):
        raise _field_err("lus_version", "must be an integer")
    if lus_version != major:
        raise _field_err("lus_version",
                         f"must equal the magic-line major ({major}), got {lus_version}")
    # title / description
    title = _req_str(mapping, "title", max_len=80)
    description = _req_str(mapping, "description", max_len=500)
    # triggers
    if "triggers" not in mapping:
        raise _field_err("triggers", "required field missing")
    triggers_raw = mapping["triggers"]
    if not isinstance(triggers_raw, list):
        raise _field_err("triggers", "must be a list of strings")
    if not 1 <= len(triggers_raw) <= 16:
        raise _field_err("triggers", f"must have 1-16 items, got {len(triggers_raw)}")
    seen_triggers: set[str] = set()
    triggers: list[str] = []
    for idx, item in enumerate(triggers_raw):
        if not isinstance(item, str) or not item.strip() or "\n" in item:
            raise _field_err("triggers", f"item {idx} must be a non-empty single-line string")
        if len(item) > 64:
            raise _field_err("triggers", f"item {idx} exceeds max length 64")
        lowered = item.lower()
        if lowered in seen_triggers:
            raise _field_err("triggers", f"duplicate item (case-insensitive): {item!r}")
        seen_triggers.add(lowered)
        triggers.append(item)
    # domain
    domain = _req_str(mapping, "domain", max_len=16)
    if domain not in DOMAINS:
        raise _field_err("domain", f"must be one of {list(DOMAINS)}")
    # tools_used (optional)
    tools_used_raw = mapping.get("tools_used", [])
    if tools_used_raw is None:
        tools_used_raw = []
    if not isinstance(tools_used_raw, list):
        raise _field_err("tools_used", "must be a list of strings")
    if len(tools_used_raw) > 32:
        raise _field_err("tools_used", f"must have at most 32 items, got {len(tools_used_raw)}")
    tools_used: list[str] = []
    for idx, item in enumerate(tools_used_raw):
        if not isinstance(item, str) or not _TOOL_NAME_RE.match(item) or len(item) > 64:
            raise _field_err("tools_used",
                             f"item {idx} must match ^[a-z][a-z0-9_]*$ and be <=64 chars")
        tools_used.append(item)
    # parameters (optional)
    parameters = mapping.get("parameters", None)
    if parameters is None:
        parameters = dict(_DEFAULT_PARAMETERS)
        parameters["properties"] = {}
    parameters = _normalize_flow_empties(parameters)
    if not isinstance(parameters, dict):
        raise _field_err("parameters", "must be a mapping")
    if parameters.get("type") != "object":
        raise _field_err("parameters.type", "root schema must have type: object")
    root_props = parameters.get("properties")
    if isinstance(root_props, dict) and len(root_props) > _SCHEMA_MAX_ROOT_PROPS:
        raise _field_err("parameters.properties",
                         f"must have at most {_SCHEMA_MAX_ROOT_PROPS} properties at root")
    _validate_schema_node(parameters, "parameters", 1)
    # author (optional)
    if "author" in mapping:
        author = _req_str(mapping, "author", max_len=64)
    else:
        author = "lumeri-agent"
    # created_at / updated_at
    created_at, created_dt = _timestamp(mapping, "created_at")
    updated_at, updated_dt = _timestamp(mapping, "updated_at")
    if updated_dt < created_dt:
        raise _field_err("updated_at", "must be >= created_at")
    # language
    language = _req_str(mapping, "language", max_len=8)
    if language not in LANGUAGES:
        raise _field_err("language", f"must be one of {list(LANGUAGES)}")
    # safety
    if "safety" not in mapping:
        raise _field_err("safety", "required field missing")
    safety = mapping["safety"]
    if not isinstance(safety, dict):
        raise _field_err("safety", "must be a mapping")
    safety_values: dict[str, bool] = {}
    for key in ("requires_paid_generation", "mutates_project"):
        if key not in safety:
            raise _field_err(f"safety.{key}", "required key missing")
        value = safety[key]
        if not isinstance(value, bool):
            raise _field_err(f"safety.{key}", "must be a boolean")
        safety_values[key] = value
    for key in safety:
        if key not in ("requires_paid_generation", "mutates_project"):
            warnings.append(LusWarning(
                "W_LUS_UNKNOWN_FIELD", f"unknown safety key: {key}", field=f"safety.{key}"))
    # checksum (optional on read; malformed is a hard field error)
    checksum: str | None = None
    if "checksum" in mapping and mapping["checksum"] is not None:
        checksum_raw = mapping["checksum"]
        if not isinstance(checksum_raw, str) or not _CHECKSUM_RE.match(checksum_raw):
            raise _field_err("checksum", "must match ^sha256:[0-9a-f]{64}$")
        checksum = checksum_raw
    # unknown extra fields — preserved, warned (§3.2)
    extra: dict[str, Any] = {}
    for key, value in mapping.items():
        if key not in FIELD_ORDER:
            extra[key] = value
            warnings.append(LusWarning(
                "W_LUS_UNKNOWN_FIELD", f"unknown metadata field: {key}", field=key))

    return LusMeta(
        name=name,
        version=version,
        lus_version=lus_version,
        title=title,
        description=description,
        triggers=tuple(triggers),
        domain=domain,
        tools_used=tuple(tools_used),
        parameters=parameters,
        author=author,
        created_at=created_at,
        updated_at=updated_at,
        language=language,
        safety_requires_paid_generation=safety_values["requires_paid_generation"],
        safety_mutates_project=safety_values["mutates_project"],
        checksum=checksum,
        extra=extra,
    )


# ── Internal: body validation (§4, E_LUS_BODY_*) ─────────────────────────


def _body_headings(body: str) -> list[tuple[int, str]]:
    """All ``## `` heading lines outside fenced code blocks: (line_idx, text)."""
    headings: list[tuple[int, str]] = []
    in_fence = False
    for idx, line in enumerate(body.split("\n")):
        if _FENCE_RE.match(line):
            in_fence = not in_fence
            continue
        if not in_fence and line.startswith("## "):
            headings.append((idx, line))
    return headings


def _validate_body(body: str) -> None:
    if not body.strip():
        raise _err("E_LUS_BODY_EMPTY", "body is empty or whitespace-only")

    lines = body.split("\n")
    headings = _body_headings(body)

    counts: dict[str, int] = {}
    for _, text in headings:
        if text in KNOWN_HEADINGS:
            counts[text] = counts.get(text, 0) + 1
    for heading, count in counts.items():
        if count > 1:
            raise _err("E_LUS_BODY_SECTION", f"duplicate section heading: '{heading}'")

    if counts.get("## When to use", 0) == 0:
        raise _err("E_LUS_BODY_SECTION", "missing required section '## When to use'")
    if counts.get("## Steps", 0) == 0:
        raise _err("E_LUS_BODY_SECTION", "missing required section '## Steps'")

    if headings[0][1] != "## When to use":
        raise _err("E_LUS_BODY_SECTION",
                   "'## When to use' must be the first section",
                   line=headings[0][0] + 1)
    for idx in range(headings[0][0]):
        if lines[idx].strip():
            raise _err("E_LUS_BODY_SECTION",
                       "no content may appear before '## When to use'", line=idx + 1)
    if len(headings) < 2 or headings[1][1] != "## Steps":
        raise _err("E_LUS_BODY_SECTION", "'## Steps' must be the second section")

    positions = {text: idx for idx, text in headings if text in KNOWN_HEADINGS}
    if "## Pitfalls" in positions and "## Examples" in positions:
        if positions["## Examples"] < positions["## Pitfalls"]:
            raise _err("E_LUS_BODY_SECTION",
                       "'## Pitfalls' must precede '## Examples'")

    steps_start = positions["## Steps"]
    next_after_steps = [idx for idx, _ in headings if idx > steps_start]
    steps_end = min(next_after_steps) if next_after_steps else len(lines)
    if not any(_STEP_ITEM_RE.match(line) for line in lines[steps_start + 1: steps_end]):
        raise _err("E_LUS_BODY_SECTION",
                   "'## Steps' must contain at least one numbered list item")


# ── Public API (spec §6) ─────────────────────────────────────────────────


def validate_lus(
    text: str | bytes,
    *,
    known_tools: frozenset[str] | set[str] | None = None,
    strict: bool = False,
    filename: str | None = None,
) -> tuple[LusMeta, str, list[LusWarning]]:
    """Validate a .lus document. Spec §6: raises :class:`LusValidationError`
    on the FIRST failing check in the §6.1 order; returns
    ``(meta, body, warnings)``.
    """
    # 1. E_LUS_ENCODING
    if isinstance(text, (bytes, bytearray)):
        try:
            text = bytes(text).decode("utf-8")
        except UnicodeDecodeError as exc:
            raise _err("E_LUS_ENCODING", "input is not valid UTF-8") from exc
    if text.startswith("\ufeff"):
        raise _err("E_LUS_ENCODING", "file must not begin with a BOM", line=1)
    if "\r" in text:
        raise _err("E_LUS_ENCODING", "CR bytes are forbidden (LF line endings only)",
                   line=text[: text.index("\r")].count("\n") + 1)
    # 2. E_LUS_TOO_LARGE
    if len(text.encode("utf-8")) > MAX_FILE_BYTES:
        raise _err("E_LUS_TOO_LARGE",
                   f"file exceeds {MAX_FILE_BYTES} bytes")
    # 3-7. magic / version / fences
    major, meta_text, body, close_line = _split_frames(text)
    # 8. E_LUS_META_PARSE
    mapping = _parse_meta_mapping(meta_text, first_meta_line=3)
    # 9. E_LUS_META_FIELD (+ W_LUS_UNKNOWN_FIELD)
    warnings: list[LusWarning] = []
    meta = _validate_meta_fields(mapping, major, warnings)
    # 10-12. body checks
    _validate_body(body)
    fence_count = sum(1 for line in body.split("\n") if _FENCE_RE.match(line))
    if fence_count % 2 != 0:
        raise _err("E_LUS_BODY_FENCE",
                   f"unbalanced code fences ({fence_count} fence lines)")
    # 13. E_LUS_SECRET — whole file, never echo the match
    for label, pattern in _SECRET_PATTERNS:
        if pattern.search(text):
            raise _err("E_LUS_SECRET",
                       f"secret-looking content ({label}) — skills must never contain credentials")
    # 14. E_LUS_ABS_PATH — whole file
    for pattern in _ABS_PATH_PATTERNS:
        match = pattern.search(text)
        if match:
            raise _err("E_LUS_ABS_PATH",
                       "absolute user path — reference assets by asset_id and files "
                       "by workspace-relative paths",
                       line=text[: match.start()].count("\n") + 1)
    # 15. checksum (E_LUS_CHECKSUM strict / W_LUS_* otherwise)
    if known_tools is not None:
        for tool in meta.tools_used:
            if tool not in known_tools:
                warnings.append(LusWarning(
                    "W_LUS_UNKNOWN_TOOL", f"unknown tool name: {tool}", field="tools_used"))
    actual = body_checksum(body)
    if meta.checksum is None:
        warnings.append(LusWarning(
            "W_LUS_CHECKSUM_MISSING", "checksum field is absent", field="checksum"))
    elif meta.checksum != actual:
        if strict:
            raise _err("E_LUS_CHECKSUM",
                       "checksum does not match the body bytes (strict mode)",
                       field="checksum")
        warnings.append(LusWarning(
            "W_LUS_CHECKSUM_STALE",
            "checksum does not match the body bytes (stale after a hand edit; "
            "the next save auto-heals it)",
            field="checksum"))
    if filename is not None:
        stem = filename.rsplit("/", 1)[-1]
        if stem.endswith(".lus"):
            stem = stem[: -len(".lus")]
        if stem != meta.name:
            warnings.append(LusWarning(
                "W_LUS_NAME_MISMATCH",
                f"filename stem {stem!r} != metadata name {meta.name!r}", field="name"))
    return meta, body, warnings


def parse_lus(text: str | bytes) -> tuple[LusMeta, str]:
    """Validate and return ``(meta, body)``, discarding warnings (§6)."""
    meta, body, _warnings = validate_lus(text)
    return meta, body


def scan_lus_meta(prefix: bytes | str) -> LusMeta:
    """Metadata-only parse of a file prefix (≤8 KiB read), for the cheap
    recall scan (§7.2). No body/section/checksum/secret checks — callers get
    the metadata or a :class:`LusValidationError`.
    """
    if isinstance(prefix, (bytes, bytearray)):
        head = bytes(prefix)[: META_MAX_BYTES]
        text = head.decode("utf-8", errors="ignore")
    else:
        text = prefix
        if len(text.encode("utf-8")) > META_MAX_BYTES:
            text = text.encode("utf-8")[:META_MAX_BYTES].decode("utf-8", errors="ignore")
    if text.startswith("\ufeff"):
        raise _err("E_LUS_ENCODING", "file must not begin with a BOM", line=1)
    if "\r" in text:
        raise _err("E_LUS_ENCODING", "CR bytes are forbidden (LF line endings only)")
    lines = text.split("\n")
    if not lines or not _MAGIC_RE.match(lines[0]):
        raise _err("E_LUS_MAGIC", "line 1 must be exactly '#!lus/<major>'", line=1)
    major = int(lines[0][len("#!lus/"):])
    if major not in SUPPORTED_LUS_MAJORS:
        raise _err("E_LUS_VERSION",
                   f".lus major version {major} is not supported "
                   f"(supported: {sorted(SUPPORTED_LUS_MAJORS)})", line=1)
    if len(lines) < 2 or lines[1] != "---":
        raise _err("E_LUS_META_OPEN", "line 2 must be exactly '---'", line=2)
    close_idx: int | None = None
    for i in range(2, len(lines)):
        if lines[i] == "---":
            close_idx = i
            break
    if close_idx is None:
        raise _err("E_LUS_META_TOO_LARGE",
                   f"metadata close fence not found within the first {META_MAX_BYTES} bytes")
    mapping = _parse_meta_mapping("\n".join(lines[2:close_idx]), first_meta_line=3)
    warnings: list[LusWarning] = []
    return _validate_meta_fields(mapping, major, warnings)


# ── Canonical writer (spec D5, §6) ───────────────────────────────────────

_PLAIN_UNSAFE_FIRST = set("-?:[]{}#&*!|>'\"%@` \t")
_YAML_WORDS = {"true", "false", "null", "yes", "no", "on", "off", "~", ""}


def _plain_safe(s: str) -> bool:
    if not s or s != s.strip():
        return False
    if s.lower() in _YAML_WORDS:
        return False
    if s[0] in _PLAIN_UNSAFE_FIRST:
        return False
    if any(ord(c) < 0x20 or c == "\x7f" for c in s):
        return False
    if ": " in s or s.endswith(":") or " #" in s:
        return False
    # Values YAML would re-type (numbers) must be quoted to stay strings.
    try:
        float(s)
        return False
    except ValueError:
        pass
    return True


def _emit_scalar(value: Any) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if value is None:
        return "null"
    if isinstance(value, int):
        return str(value)
    if isinstance(value, float):
        return json.dumps(value)
    s = str(value)
    if _plain_safe(s):
        return s
    return json.dumps(s, ensure_ascii=False)


def _emit_entry(key: str, value: Any, indent: int, out: list[str]) -> None:
    pad = "  " * indent
    if isinstance(value, dict):
        if not value:
            out.append(f"{pad}{key}: {{}}")
            return
        out.append(f"{pad}{key}:")
        for sub_key, sub_value in value.items():
            _emit_entry(str(sub_key), sub_value, indent + 1, out)
    elif isinstance(value, (list, tuple)):
        if not value:
            out.append(f"{pad}{key}: []")
            return
        out.append(f"{pad}{key}:")
        for item in value:
            if isinstance(item, (dict, list, tuple)):
                out.append(f"{pad}  - {json.dumps(item, ensure_ascii=False)}")
            else:
                out.append(f"{pad}  - {_emit_scalar(item)}")
    else:
        out.append(f"{pad}{key}: {_emit_scalar(value)}")


def serialize_lus(meta: LusMeta, body: str) -> str:
    """Canonical emitter (D5): fixed §3.2 field order, block style, 2-space
    indent, LF only, exactly one leading blank line and one trailing newline
    in the body, and a freshly recomputed checksum (§5). Byte-stable:
    ``serialize_lus(*parse_lus(text)) == text`` for canonical input.
    """
    if not str(body or "").strip():
        raise _err("E_LUS_BODY_EMPTY", "cannot serialize an empty body")
    body_norm = "\n" + str(body).strip("\n") + "\n"
    checksum = body_checksum(body_norm)

    out: list[str] = [f"#!lus/{int(meta.lus_version)}", "---"]
    _emit_entry("name", meta.name, 0, out)
    _emit_entry("version", meta.version, 0, out)
    _emit_entry("lus_version", int(meta.lus_version), 0, out)
    _emit_entry("title", meta.title, 0, out)
    _emit_entry("description", meta.description, 0, out)
    _emit_entry("triggers", list(meta.triggers), 0, out)
    _emit_entry("domain", meta.domain, 0, out)
    _emit_entry("tools_used", list(meta.tools_used), 0, out)
    _emit_entry("parameters", meta.parameters, 0, out)
    _emit_entry("author", meta.author, 0, out)
    _emit_entry("created_at", meta.created_at, 0, out)
    _emit_entry("updated_at", meta.updated_at, 0, out)
    _emit_entry("language", meta.language, 0, out)
    _emit_entry("safety", {
        "requires_paid_generation": meta.safety_requires_paid_generation,
        "mutates_project": meta.safety_mutates_project,
    }, 0, out)
    _emit_entry("checksum", checksum, 0, out)
    for key, value in (meta.extra or {}).items():
        _emit_entry(str(key), value, 0, out)
    out.append("---")

    # The close-fence line ends with its own LF; the checksum-covered body
    # bytes (body_norm, starting with the conventional blank line) follow it.
    text = "\n".join(out) + "\n" + body_norm
    # §2 self-enforcement on output.
    if "\r" in text:
        raise _err("E_LUS_ENCODING", "serializer produced a CR byte")
    if len(text.encode("utf-8")) > MAX_FILE_BYTES:
        raise _err("E_LUS_TOO_LARGE",
                   f"serialized skill exceeds {MAX_FILE_BYTES} bytes")
    return text


__all__ = [
    "SUPPORTED_LUS_MAJORS",
    "MAX_FILE_BYTES",
    "META_MAX_BYTES",
    "DOMAINS",
    "LANGUAGES",
    "PAID_GENERATION_TOOLS",
    "FIELD_ORDER",
    "KNOWN_HEADINGS",
    "LusMeta",
    "LusWarning",
    "LusValidationError",
    "validate_lus",
    "parse_lus",
    "scan_lus_meta",
    "serialize_lus",
    "derive_name",
    "detect_language",
    "extract_tools_used",
    "body_checksum",
]
