"""Planner output contract for executable Lumeri plans.

Gemini is allowed to be creative, but the executor should only receive a
small, normalized JSON shape: known primitive, clean args, and media paths in
``input``/``output`` fields instead of duplicated function kwargs.
"""
from __future__ import annotations

import ast
import inspect
import re
from copy import deepcopy
from typing import Any

from gemia.errors import UserInputError
from gemia.registry import PrimitiveInfo, get_info


class PlanContractError(UserInputError):
    """A planner response is not executable as a Lumeri Plan."""

    code = "E_PLAN_CONTRACT"


_MEDIA_ARG_NAMES = {
    "img",
    "image",
    "frame",
    "frames",
    "audio",
    "audio_arr",
    "input_path",
    "input_paths",
    "input_video",
    "input_a",
    "input_b",
    "video_path",
    "path",
    "paths",
    "source_path",
    "output_path",
    "dest",
    "destination",
}
_OUTPUT_ARG_NAMES = {"output_path", "dest", "destination"}
_INPUT_ARG_NAMES = _MEDIA_ARG_NAMES - _OUTPUT_ARG_NAMES
_ARG_ALIASES = {
    "bg": "background_path",
    "background": "background_path",
    "threshold": "tolerance",
    "similarity": "tolerance",
    "blend": "feather",
    "method": "mode",
    "duration": "duration_sec",
    "start": "start_sec",
    "end": "end_sec",
}
_FUNCTION_ALIASES = {
    "gemia.video.effects.trim_clip": "gemia.video.timeline.cut",
    "gemia.video.effects.cut": "gemia.video.timeline.cut",
    "gemia.video.effects.color_grade": "gemia.picture.color.color_grade",
    "gemia.video.effects.adjust_exposure": "gemia.picture.color.adjust_exposure",
    "gemia.video.timeline.merge": "gemia.video.timeline.concat",
    "gemia.video.timeline.join": "gemia.video.timeline.concat",
    "gemia.video.transition.crossfade": "gemia.video.transitions.transition_dissolve",
    "gemia.video.transition.dissolve": "gemia.video.transitions.transition_dissolve",
    "gemia.video.transition.fade": "gemia.video.transitions.transition_dissolve",
    "gemia.video.transition.wipe": "gemia.video.transitions.transition_wipe",
    "gemia.video.transition.push": "gemia.video.transitions.transition_push",
    "gemia.video.transitions.crossfade": "gemia.video.transitions.transition_dissolve",
    "gemia.video.transitions.dissolve": "gemia.video.transitions.transition_dissolve",
    "gemia.video.subtitles.add_subtitle": "gemia.video.subtitles.add_text",
    "gemia.video.subtitles.text": "gemia.video.subtitles.add_text",
    "gemia.video.text.add_text": "gemia.video.subtitles.add_text",
    "gemia.video.html_graphics.render": "gemia.video.html_graphics.render_html_graphics_plan",
    "gemia.video.layer_flow.render": "gemia.video.layer_flow.render_layer_workflow",
    "gemia.video.effects.chroma_key": "gemia.video.masking.render_chroma_key_preview",
    "gemia.video.effects.video_luma_key": "gemia.video.masking.render_luma_key_preview",
    "gemia.video.compositing.background_replace": "gemia.video.masking.render_masked_composite",
}
_PLACEHOLDER_RE = re.compile(r"\{([A-Za-z_][\w.-]*)(?:\|([^{}]*))?\}")


def normalize_plan_for_execution(
    plan: dict[str, Any],
    *,
    active_specs: list[dict[str, Any]] | None = None,
    input_path: str | None = None,
    output_path: str | None = None,
    strict_functions: bool = True,
) -> dict[str, Any]:
    """Return a defensive, executor-ready copy of a Plan JSON dict.

    The normalizer fixes common LLM planning artifacts:
    - resolves template strings like ``"{duration|0.5}"`` before execution
    - moves media path kwargs into step ``input``/``output``
    - renames common aliases such as ``duration`` -> ``duration_sec``
    - drops top-level args unsupported by the primitive signature
    - rejects calls outside the active primitive set when specs are supplied
    - fails early on unknown primitives when ``strict_functions`` is true
    """
    if not isinstance(plan, dict):
        raise PlanContractError("规划结果不是可执行 JSON 对象。", detail=f"type={type(plan).__name__}")
    if plan.get("ask"):
        return deepcopy(plan)

    normalized = deepcopy(plan)
    steps = normalized.get("steps")
    if not isinstance(steps, list):
        raise PlanContractError("规划里没有可执行步骤。", detail="missing steps")
    if not steps:
        normalized.setdefault("version", "2.1")
        contract = normalized.setdefault("plan_contract", {})
        if isinstance(contract, dict):
            contract["normalized"] = True
            contract["warnings"] = [{"kind": "no_op_plan"}]
        return normalized

    spec_by_name = {
        str(spec.get("name")): spec
        for spec in active_specs or []
        if isinstance(spec, dict) and spec.get("name")
    }
    warnings: list[dict[str, Any]] = []
    for index, raw_step in enumerate(steps):
        if not isinstance(raw_step, dict):
            raise PlanContractError("规划步骤格式不正确。", detail=f"step[{index}] type={type(raw_step).__name__}")
        raw_step.setdefault("id", f"step_{index + 1}")
        fqn = str(raw_step.get("function") or "").strip()
        if not fqn:
            raise PlanContractError("规划步骤缺少 function。", detail=f"step={raw_step.get('id')}")
        original_fqn = fqn
        canonical_fqn = _canonical_function_name(fqn)
        if canonical_fqn != fqn:
            raw_step["function"] = canonical_fqn
            warnings.append(
                {
                    "step_id": raw_step.get("id"),
                    "kind": "renamed_function",
                    "from": fqn,
                    "to": canonical_fqn,
                }
                )
            fqn = canonical_fqn

        info = _get_info_or_none(fqn)
        if info is None:
            if strict_functions:
                raise PlanContractError(
                    "规划调用了 Lumeri 还没有注册的能力。",
                    detail=f"unknown primitive: {fqn}",
                )
            continue
        if active_specs is not None and spec_by_name and fqn not in spec_by_name:
            if canonical_fqn != original_fqn:
                warnings.append(
                    {
                        "step_id": raw_step.get("id"),
                        "kind": "inactive_primitive",
                        "primitive": fqn,
                    }
                )
            else:
                raise PlanContractError(
                    "规划调用了本轮没有激活的能力。",
                    detail=f"inactive primitive: {fqn}",
                )

        step_warnings = _normalize_step(
            raw_step,
            info=info,
            spec=spec_by_name.get(fqn),
            fallback_input=input_path,
            fallback_output=output_path,
        )
        warnings.extend({"step_id": raw_step.get("id"), **item} for item in step_warnings)

    normalized.setdefault("version", "2.1")
    if warnings:
        contract = normalized.setdefault("plan_contract", {})
        if isinstance(contract, dict):
            contract["normalized"] = True
            contract["warnings"] = warnings
    return normalized


def _normalize_step(
    step: dict[str, Any],
    *,
    info: PrimitiveInfo,
    spec: dict[str, Any] | None,
    fallback_input: str | None,
    fallback_output: str | None,
) -> list[dict[str, Any]]:
    args = step.get("args")
    if args is None:
        args = {}
    if not isinstance(args, dict):
        raise PlanContractError(
            "规划步骤的 args 必须是 JSON 对象。",
            detail=f"step={step.get('id')} args_type={type(args).__name__}",
        )

    warnings: list[dict[str, Any]] = []
    args = deepcopy(args)
    _move_media_args_to_step_fields(step, args, fallback_input=fallback_input, fallback_output=fallback_output, warnings=warnings)
    args = _apply_arg_aliases(args, info, warnings)
    defaults = _default_values(info, spec)
    args = {
        key: _normalize_value(value, arg_name=key, defaults=defaults)
        for key, value in args.items()
    }
    args = _drop_unsupported_args(args, info, warnings)
    step["args"] = args
    return warnings


def _move_media_args_to_step_fields(
    step: dict[str, Any],
    args: dict[str, Any],
    *,
    fallback_input: str | None,
    fallback_output: str | None,
    warnings: list[dict[str, Any]],
) -> None:
    input_a = args.pop("input_a", None)
    input_b = args.pop("input_b", None)
    if input_a is not None or input_b is not None:
        existing = step.get("input")
        values: list[Any] = []
        if input_a is not None:
            values.append(input_a)
        elif existing is not None:
            values.append(existing)
        elif fallback_input is not None:
            values.append(fallback_input)
        if input_b is not None:
            values.append(input_b)
        step["input"] = values if len(values) > 1 else (values[0] if values else step.get("input", "$input"))
        warnings.append({"kind": "moved_media_args", "args": [name for name, value in (("input_a", input_a), ("input_b", input_b)) if value is not None]})

    for name in list(args):
        if name in _OUTPUT_ARG_NAMES:
            value = args.pop(name)
            if step.get("output") is None:
                step["output"] = "$output" if fallback_output and str(value) == str(fallback_output) else value
            warnings.append({"kind": "moved_output_arg", "arg": name})
        elif name in _INPUT_ARG_NAMES:
            value = args.pop(name)
            if step.get("input") is None:
                step["input"] = value
            warnings.append({"kind": "moved_input_arg", "arg": name})


def _apply_arg_aliases(args: dict[str, Any], info: PrimitiveInfo, warnings: list[dict[str, Any]]) -> dict[str, Any]:
    params = set(info.params)
    out = dict(args)
    for source, target in _ARG_ALIASES.items():
        if source in out and target in params and target not in out:
            out[target] = out.pop(source)
            warnings.append({"kind": "renamed_arg", "from": source, "to": target})
    return out


def _default_values(info: PrimitiveInfo, spec: dict[str, Any] | None) -> dict[str, Any]:
    defaults: dict[str, Any] = {}
    for name, meta in info.params.items():
        if "default" in meta:
            defaults[name] = _literal_default(meta.get("default"))
    schema = spec.get("args_schema") if isinstance(spec, dict) and isinstance(spec.get("args_schema"), dict) else {}
    props = schema.get("properties") if isinstance(schema.get("properties"), dict) else {}
    for name, meta in props.items():
        if isinstance(meta, dict) and "default" in meta:
            defaults[str(name)] = meta.get("default")
    return defaults


def _normalize_value(value: Any, *, arg_name: str, defaults: dict[str, Any]) -> Any:
    if isinstance(value, str):
        return _normalize_string_value(value, arg_name=arg_name, defaults=defaults)
    if isinstance(value, list):
        return [_normalize_value(item, arg_name=arg_name, defaults=defaults) for item in value]
    if isinstance(value, dict):
        return {
            str(key): _normalize_value(item, arg_name=str(key), defaults=defaults)
            for key, item in value.items()
        }
    return value


def _normalize_string_value(value: str, *, arg_name: str, defaults: dict[str, Any]) -> Any:
    stripped = value.strip()
    matches = list(_PLACEHOLDER_RE.finditer(stripped))
    if not matches:
        return value
    if len(matches) == 1 and matches[0].span() == (0, len(stripped)):
        return _placeholder_default(matches[0], arg_name=arg_name, defaults=defaults)

    def replace(match: re.Match[str]) -> str:
        resolved = _placeholder_default(match, arg_name=arg_name, defaults=defaults)
        return str(resolved)

    return _PLACEHOLDER_RE.sub(replace, value)


def _placeholder_default(match: re.Match[str], *, arg_name: str, defaults: dict[str, Any]) -> Any:
    placeholder_name = match.group(1)
    inline_default = match.group(2)
    if inline_default is not None and inline_default != "":
        return _parse_inline_default(inline_default)
    for key in (arg_name, placeholder_name, _ARG_ALIASES.get(arg_name, "")):
        if key and key in defaults:
            return defaults[key]
    raise PlanContractError(
        "规划里还有未解析的模板占位符。",
        detail=f"placeholder={match.group(0)} arg={arg_name}",
    )


def _parse_inline_default(value: str) -> Any:
    text = value.strip()
    lowered = text.lower()
    if lowered == "true":
        return True
    if lowered == "false":
        return False
    if lowered in {"none", "null"}:
        return None
    try:
        if re.fullmatch(r"[-+]?\d+", text):
            return int(text)
        if re.fullmatch(r"[-+]?(?:\d+\.\d*|\d*\.\d+)", text):
            return float(text)
    except ValueError:
        pass
    return text.strip("\"'")


def _drop_unsupported_args(args: dict[str, Any], info: PrimitiveInfo, warnings: list[dict[str, Any]]) -> dict[str, Any]:
    sig = inspect.signature(info.func)
    if any(param.kind == inspect.Parameter.VAR_KEYWORD for param in sig.parameters.values()):
        return args
    allowed = {
        name
        for name, param in sig.parameters.items()
        if name not in _MEDIA_ARG_NAMES and name != "self" and param.kind in {
            inspect.Parameter.POSITIONAL_OR_KEYWORD,
            inspect.Parameter.KEYWORD_ONLY,
        }
    }
    out = {key: value for key, value in args.items() if key in allowed}
    dropped = sorted(set(args) - set(out))
    if dropped:
        warnings.append({"kind": "dropped_unsupported_args", "args": dropped})
    return out


def _literal_default(raw: Any) -> Any:
    if raw is None or not isinstance(raw, str):
        return raw
    try:
        return ast.literal_eval(raw)
    except (SyntaxError, ValueError):
        return raw


def _get_info_or_none(fqn: str) -> PrimitiveInfo | None:
    try:
        return get_info(fqn)
    except KeyError:
        return None


def _canonical_function_name(fqn: str) -> str:
    return _FUNCTION_ALIASES.get(fqn, fqn)
