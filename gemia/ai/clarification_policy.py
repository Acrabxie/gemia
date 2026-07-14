from __future__ import annotations

from typing import Any


DEFAULTABLE_ASK_KEYWORDS = (
    "参数",
    "强度",
    "程度",
    "风格",
    "样式",
    "类型",
    "时长",
    "多久",
    "时间段",
    "范围",
    "哪一段",
    "哪个片段",
    "哪张脸",
    "哪一个人",
    "坐标",
    "点位",
    "位置",
    "目标",
    "overlay",
    "style",
    "type",
    "strength",
    "duration",
    "range",
    "target",
    "point",
    "coordinate",
)

HIGH_RISK_ASK_KEYWORDS = (
    "删除",
    "清空",
    "覆盖",
    "不可逆",
    "付费",
    "购买",
    "外部",
    "上传",
    "公开",
    "隐私",
    "账号",
    "登录",
    "api",
    "key",
    "secret",
    "版权",
    "客户",
    "delete",
    "overwrite",
    "paid",
    "purchase",
    "external",
    "upload",
    "privacy",
    "account",
    "login",
    "copyright",
)

EXTERNAL_SIDE_EFFECTS = {
    "external_model",
    "external_download",
    "blender_optional",
    "source_patch_proposal",
}


def build_clarification_policy(
    selected_skills: list[str],
    active_specs: list[dict[str, Any]],
    *,
    answers: dict[str, str] | None = None,
) -> dict[str, Any]:
    """Build a default-first ask policy for the model payload."""
    defaultable = _defaultable_specs(active_specs)
    high_risk = _high_risk_specs(active_specs)
    return {
        "mode": "default_first",
        "max_rounds": 1,
        "current_round": 1 if answers else 0,
        "no_reask_after_clarifications": bool(answers),
        "default_missing_slots_after_first_ask": True,
        "ask_cost": "high",
        "ask_allowed_only_for": [
            "missing source media or no usable target material",
            "irreversible delete/overwrite or destructive edit",
            "external paid/model/download action that the user did not explicitly request",
            "identity/privacy/copyright-sensitive choice",
            "multi-target ambiguity where a default would clearly change creative intent",
        ],
        "do_not_ask_for": [
            "style, intensity, duration, point, face id, overlay style, or range when a safe default exists",
            "low-level API parameters that have defaults in active_primitive_specs",
            "ordinary creative taste requests such as make it better, cleaner, warmer, cooler, cinematic",
            "face/object tracking point coordinates or target id when a prominent subject default exists",
        ],
        "defaultable_primitives": [str(spec.get("name")) for spec in defaultable],
        "high_risk_primitives": [str(spec.get("name")) for spec in high_risk],
        "has_defaultable_executable_primitive": bool(defaultable),
        "face_tracking_defaults": {
            "target": "most_prominent_face",
            "time_scope": "selected_range_or_full_clip",
            "output": "tracking_overlay_plus_metadata",
        },
    }


def maybe_default_plan_for_ask(
    ask_plan: dict[str, Any],
    *,
    effective_request: str,
    selected_skills: list[str],
    active_specs: list[dict[str, Any]],
    project_state: dict[str, Any] | None = None,
    answers: dict[str, str] | None = None,
) -> dict[str, Any] | None:
    """Convert an over-eager ask into a conservative executable default plan."""
    if not isinstance(ask_plan, dict) or not ask_plan.get("ask"):
        return None
    questions = ask_plan.get("questions") if isinstance(ask_plan.get("questions"), list) else []
    if not _ask_is_defaultable(questions, answers=answers):
        return None

    if "face-tracking" in selected_skills:
        return _single_step_plan(
            effective_request,
            "gemia.video.face_tracking.render_face_tracking_plan",
            {
                "target": "most_prominent_face",
                "time_scope": "selected_range_or_full_clip",
                "overlay": True,
                "trail": True,
            },
            "我会按默认规则做人脸跟踪：锁定最明显的人脸，输出跟踪预览和轨迹数据。",
        )

    if "transition" in selected_skills:
        transition_plan = _default_transition_plan(effective_request, project_state)
        if transition_plan is not None:
            return transition_plan

    if "timeline-ops" in selected_skills and _looks_like_concat_request(effective_request):
        concat_plan = _default_concat_plan(effective_request, project_state)
        if concat_plan is not None:
            return concat_plan

    spec = _first_defaultable_executable_spec(active_specs)
    if spec is None:
        return None
    args = _default_args(spec)
    return _single_step_plan(
        effective_request,
        str(spec["name"]),
        args,
        "我不再追问参数，先按 Lumeri 的安全默认值执行；如果方向不对，你可以直接让我改。",
    )


def _ask_is_defaultable(questions: list[Any], *, answers: dict[str, str] | None) -> bool:
    """Default-first: an ask is convertible to a default plan UNLESS the
    question text mentions a high-risk topic (delete/overwrite, paid/external
    side effect, identity/privacy, copyright). The previous version required
    the question to also hit a curated allowlist of "defaultable" keywords,
    which let a lot of vague Chinese asks ("您想要哪种效果?") slip through and
    bounce back to the user. Now we trust the high-risk filter alone.
    """
    if answers:
        return True
    text = " ".join(_question_text(item) for item in questions).lower()
    if not text:
        return False
    return not any(keyword in text for keyword in HIGH_RISK_ASK_KEYWORDS)


def _question_text(item: Any) -> str:
    if isinstance(item, dict):
        return " ".join(str(item.get(key) or "") for key in ("id", "text", "label", "description"))
    return str(item or "")


def _defaultable_specs(active_specs: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [spec for spec in active_specs if _is_defaultable_executable_spec(spec)]


def _high_risk_specs(active_specs: list[dict[str, Any]]) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for spec in active_specs:
        side_effects = set(str(item) for item in spec.get("side_effects") or [])
        if side_effects & EXTERNAL_SIDE_EFFECTS:
            out.append(spec)
    return out


def _first_defaultable_executable_spec(active_specs: list[dict[str, Any]]) -> dict[str, Any] | None:
    for spec in active_specs:
        if _is_defaultable_executable_spec(spec):
            return spec
    return None


def _is_defaultable_executable_spec(spec: dict[str, Any]) -> bool:
    name = str(spec.get("name") or "")
    if not name or spec.get("ask_if_missing"):
        return False
    output_media = str(spec.get("output_media") or "")
    if not output_media or output_media == "metadata":
        return False
    input_media = spec.get("input_media") if isinstance(spec.get("input_media"), list) else []
    logical_single_input = set(str(item) for item in input_media) == {"image", "video_frames"}
    if len(input_media) > 1 and not logical_single_input:
        return False
    side_effects = set(str(item) for item in spec.get("side_effects") or [])
    if side_effects & EXTERNAL_SIDE_EFFECTS:
        return False
    return True


def _default_args(spec: dict[str, Any]) -> dict[str, Any]:
    schema = spec.get("args_schema") if isinstance(spec.get("args_schema"), dict) else {}
    props = schema.get("properties") if isinstance(schema.get("properties"), dict) else {}
    args: dict[str, Any] = {}
    for key, meta in props.items():
        if not isinstance(meta, dict) or "default" not in meta:
            continue
        value = meta.get("default")
        if value is not None:
            args[str(key)] = value
    return args


def _default_transition_plan(effective_request: str, project_state: dict[str, Any] | None) -> dict[str, Any] | None:
    sources = _project_sources(project_state)
    if len(sources) < 2:
        return None
    return {
        "version": "2.1",
        "goal": effective_request or "transition",
        "assistant_message": "我会用默认短溶解把相邻两段素材接起来，不再单独追问转场类型和时长。",
        "steps": [
            {
                "id": "step_1",
                "function": "gemia.video.transitions.transition_dissolve",
                "args": {"duration_sec": 0.5},
                "input": sources[:2],
                "output": "$output",
                "assistant_message": "我会在两段素材之间加入 0.5 秒溶解转场。",
                "depends_on": [],
                "parallel_group": None,
                "artifact_type": "video",
            }
        ],
    }


def _default_concat_plan(effective_request: str, project_state: dict[str, Any] | None) -> dict[str, Any] | None:
    sources = _project_sources(project_state)
    if len(sources) < 2:
        return None
    return {
        "version": "2.1",
        "goal": effective_request or "concat",
        "assistant_message": "我会按当前时间轴/素材顺序先合并成片，不再追问每一段的选择。",
        "steps": [
            {
                "id": "step_1",
                "function": "gemia.video.timeline.concat",
                "args": {},
                "input": sources,
                "output": "$output",
                "assistant_message": "我会按顺序拼接这些素材。",
                "depends_on": [],
                "parallel_group": None,
                "artifact_type": "video",
            }
        ],
    }


def _single_step_plan(effective_request: str, function_name: str, args: dict[str, Any], message: str) -> dict[str, Any]:
    return {
        "version": "2.1",
        "goal": effective_request or function_name.rsplit(".", 1)[-1],
        "assistant_message": message,
        "steps": [
            {
                "id": "step_1",
                "function": function_name,
                "args": args,
                "input": "$input",
                "output": "$output",
                "assistant_message": message,
                "depends_on": [],
                "parallel_group": None,
                "artifact_type": "video",
            }
        ],
    }


def _looks_like_concat_request(text: str) -> bool:
    lowered = (text or "").lower()
    return any(word in lowered for word in ("合并", "拼接", "连接", "concat", "join"))


def _project_sources(project_state: dict[str, Any] | None) -> list[str]:
    if not isinstance(project_state, dict):
        return []
    clips = project_state.get("clips") if isinstance(project_state.get("clips"), list) else []
    sources: list[str] = []
    for clip in clips:
        if not isinstance(clip, dict):
            continue
        value = (
            clip.get("source_path")
            or clip.get("sourcePath")
            or clip.get("serverPath")
            or clip.get("path")
            or clip.get("original_path")
            or clip.get("originalPath")
        )
        if isinstance(value, str) and value.strip() and value not in sources:
            sources.append(value)
    return sources
