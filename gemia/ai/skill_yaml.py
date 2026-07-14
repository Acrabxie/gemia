from __future__ import annotations

import json
from typing import Any

try:  # pragma: no cover - exercised only when PyYAML is installed.
    import yaml as _pyyaml  # type: ignore
except Exception:  # pragma: no cover - default in lean test envs.
    _pyyaml = None


def safe_load(text: str) -> Any:
    if _pyyaml is not None:
        return _pyyaml.safe_load(text)
    return _manual_load(text)


def safe_dump(data: Any, *, allow_unicode: bool = True, sort_keys: bool = False) -> str:
    if _pyyaml is not None:
        return _pyyaml.safe_dump(data, allow_unicode=allow_unicode, sort_keys=sort_keys)
    return json.dumps(data, ensure_ascii=not allow_unicode, indent=2, sort_keys=sort_keys)


def _manual_load(text: str) -> Any:
    raw_lines = text.splitlines()
    lines: list[tuple[int, str]] = []
    for raw in raw_lines:
        if not raw.strip() or raw.lstrip().startswith("#"):
            continue
        indent = len(raw) - len(raw.lstrip(" "))
        lines.append((indent, raw.strip()))
    if not lines:
        return None
    value, _next = _parse_block(lines, 0, lines[0][0])
    return value


def _parse_block(lines: list[tuple[int, str]], index: int, indent: int) -> tuple[Any, int]:
    if index >= len(lines):
        return {}, index
    if lines[index][0] < indent:
        return {}, index
    if lines[index][1].startswith("- "):
        return _parse_list(lines, index, indent)
    return _parse_dict(lines, index, indent)


def _parse_dict(lines: list[tuple[int, str]], index: int, indent: int) -> tuple[dict[str, Any], int]:
    out: dict[str, Any] = {}
    while index < len(lines):
        line_indent, content = lines[index]
        if line_indent < indent:
            break
        if line_indent > indent:
            break
        if content.startswith("- "):
            break
        if ":" not in content:
            index += 1
            continue
        key, rest = content.split(":", 1)
        key = key.strip()
        rest = rest.strip()
        if rest == "|":
            value, index = _parse_literal(lines, index + 1, indent)
            out[key] = value
            continue
        if rest:
            out[key] = _parse_scalar(rest)
            index += 1
            continue
        if index + 1 < len(lines) and lines[index + 1][0] > indent:
            value, index = _parse_block(lines, index + 1, lines[index + 1][0])
            out[key] = value
        else:
            out[key] = None
            index += 1
    return out, index


def _parse_list(lines: list[tuple[int, str]], index: int, indent: int) -> tuple[list[Any], int]:
    out: list[Any] = []
    while index < len(lines):
        line_indent, content = lines[index]
        if line_indent != indent or not content.startswith("- "):
            break
        item_text = content[2:].strip()
        if not item_text:
            if index + 1 < len(lines) and lines[index + 1][0] > indent:
                value, index = _parse_block(lines, index + 1, lines[index + 1][0])
                out.append(value)
            else:
                out.append(None)
                index += 1
            continue
        if ":" in item_text and not item_text.startswith(("'", '"')):
            key, rest = item_text.split(":", 1)
            item: dict[str, Any] = {}
            if rest.strip():
                item[key.strip()] = _parse_scalar(rest.strip())
                index += 1
            else:
                if index + 1 < len(lines) and lines[index + 1][0] > indent:
                    value, index = _parse_block(lines, index + 1, lines[index + 1][0])
                    item[key.strip()] = value
                else:
                    item[key.strip()] = None
                    index += 1
            if index < len(lines) and lines[index][0] > indent:
                extra, index = _parse_dict(lines, index, lines[index][0])
                if isinstance(extra, dict):
                    item.update(extra)
            out.append(item)
        else:
            out.append(_parse_scalar(item_text))
            index += 1
    return out, index


def _parse_literal(lines: list[tuple[int, str]], index: int, parent_indent: int) -> tuple[str, int]:
    literal: list[str] = []
    min_indent: int | None = None
    start = index
    while index < len(lines) and lines[index][0] > parent_indent:
        min_indent = lines[index][0] if min_indent is None else min(min_indent, lines[index][0])
        index += 1
    for line_indent, content in lines[start:index]:
        strip_count = min_indent or line_indent
        literal.append(" " * max(line_indent - strip_count, 0) + content)
    return "\n".join(literal).strip(), index


def _parse_scalar(value: str) -> Any:
    value = value.strip()
    if value.startswith("[") and value.endswith("]"):
        inner = value[1:-1].strip()
        if not inner:
            return []
        return [_parse_scalar(part.strip()) for part in inner.split(",")]
    if value.startswith(('"', "'")) and value.endswith(('"', "'")):
        return value[1:-1]
    lowered = value.lower()
    if lowered == "true":
        return True
    if lowered == "false":
        return False
    if lowered in {"null", "none"}:
        return None
    try:
        return int(value)
    except ValueError:
        pass
    try:
        return float(value)
    except ValueError:
        return value
