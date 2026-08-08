"""Validated, project-agnostic production contract primitives.

RealityContract describes *this* deliverable.  It deliberately contains no
storyboard, shot list or provider plan: those evolve incrementally in the
Creative IR and project graph.  Defaults describe Lumeri's currently supported
mastering format, while duration and creative structure remain unbound until a
real brief supplies them.
"""
from __future__ import annotations

import copy
import math
from typing import Any, Mapping


MAX_MEDIA_BUDGET_USD = 15.0


def _deep_merge(base: dict[str, Any], patch: Mapping[str, Any]) -> dict[str, Any]:
    result = copy.deepcopy(base)
    for key, value in patch.items():
        if isinstance(value, Mapping) and isinstance(result.get(key), dict):
            result[key] = _deep_merge(result[key], value)
        else:
            result[key] = copy.deepcopy(value)
    return result


def _finite_number(value: Any, *, field: str, allow_none: bool = False) -> float | None:
    if value is None and allow_none:
        return None
    if isinstance(value, bool):
        raise ValueError(f"{field} must be a finite number")
    try:
        number = float(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{field} must be a finite number") from exc
    if not math.isfinite(number):
        raise ValueError(f"{field} must be a finite number")
    return number


def default_reality_contract(
    *, brief: str = "", hard_cap_usd: float = MAX_MEDIA_BUDGET_USD
) -> dict[str, Any]:
    """Return a neutral contract, not a hidden 120-second example brief."""

    cap = _finite_number(hard_cap_usd, field="budget.hard_cap_usd")
    assert cap is not None
    if cap <= 0 or cap > MAX_MEDIA_BUDGET_USD:
        raise ValueError(
            f"budget.hard_cap_usd must be in (0, {MAX_MEDIA_BUDGET_USD}]"
        )
    return {
        "schema": "lumeri.reality-contract",
        "version": 2,
        "brief": str(brief or "").strip(),
        "deliverable": {
            # Duration is intentionally unbound.  A formal run cannot leave
            # preflight until the real brief fixes it.
            "duration_sec": None,
            "duration_tolerance_sec": 0.5,
            "width": 1920,
            "height": 1080,
            "fps": 30.0,
            "container": "mp4",
            "video_codec": "h264",
            "pixel_format": "yuv420p",
            "audio": {
                "required": True,
                "codec": "aac",
                "sample_rate": 48000,
                "channels": 2,
                "integrated_loudness_lufs": -16.0,
                "loudness_tolerance_lu": 1.0,
                "true_peak_max_dbtp": -1.0,
                "required_roles": [],
            },
        },
        "media_policy": {
            "licensed_or_owned_only": True,
            "provenance_required": True,
            "source_priority": [
                "owned_existing",
                "licensed_public_stock",
                "local_compositing",
                "generated_media_if_blocked",
            ],
            "full_ai_video_default": False,
            "generated_video_requires_recorded_blocker": True,
            "generated_video_default_calls": 0,
            "generated_video_attempt_cap": 3,
            "generated_video_duration_cap_sec": 24.0,
            "generated_image_default_cap": 0,
            "generated_image_blocker_cap": 5,
            "generated_image_blocker_types": ["character_continuity"],
        },
        "budget": {
            "hard_cap_usd": cap,
            "warning_usd": min(12.0, cap),
            "scope": "external_media_per_production_run",
        },
        "acceptance": {
            "full_decode_required": True,
            "dropped_fields_allowed": False,
            "provenance_complete_required": True,
            "forbid_temporary_references": True,
            # Structural and motion targets are brief-specific.  None/zero
            # means the contract does not invent that creative requirement.
            "edit_units": {"min": None, "max": None},
            "median_shot_duration_max_sec": None,
            "verified_motion_min_sec": 0.0,
            "licensed_public_motion_assets_min": 0,
            "static_shot_max_sec": None,
            "review_sample_frames_min": 6,
            "agent_review_checks": [
                "black_frames",
                "watermarks",
                "text_integrity",
                "character_continuity",
                "real_motion",
            ],
            "creative_dimensions": [
                "story",
                "pacing",
                "visual",
                "sound",
                "publishable",
            ],
            "human_approval_required": True,
        },
    }


def normalize_reality_contract(
    value: Mapping[str, Any] | None,
    *,
    hard_cap_usd: float = MAX_MEDIA_BUDGET_USD,
) -> dict[str, Any]:
    """Merge a partial contract onto neutral defaults and validate host limits."""

    if value is not None and not isinstance(value, Mapping):
        raise ValueError("reality_contract must be an object")
    requested_brief = str((value or {}).get("brief") or "").strip()
    result = _deep_merge(
        default_reality_contract(brief=requested_brief, hard_cap_usd=hard_cap_usd),
        value or {},
    )
    result["schema"] = "lumeri.reality-contract"
    result["version"] = 2
    result["brief"] = str(result.get("brief") or "").strip()

    deliverable = result.get("deliverable")
    if not isinstance(deliverable, dict):
        raise ValueError("deliverable must be an object")
    duration = _finite_number(
        deliverable.get("duration_sec"),
        field="deliverable.duration_sec",
        allow_none=True,
    )
    if duration is not None and duration <= 0:
        raise ValueError("deliverable.duration_sec must be > 0")
    deliverable["duration_sec"] = duration
    tolerance = _finite_number(
        deliverable.get("duration_tolerance_sec", 0.5),
        field="deliverable.duration_tolerance_sec",
    )
    assert tolerance is not None
    if tolerance < 0:
        raise ValueError("deliverable.duration_tolerance_sec must be >= 0")
    deliverable["duration_tolerance_sec"] = tolerance
    for field in ("width", "height"):
        number = _finite_number(deliverable.get(field), field=f"deliverable.{field}")
        assert number is not None
        integer = int(number)
        if integer < 2 or integer % 2:
            raise ValueError(f"deliverable.{field} must be an even integer >= 2")
        deliverable[field] = integer
    fps = _finite_number(deliverable.get("fps"), field="deliverable.fps")
    assert fps is not None
    if fps <= 0:
        raise ValueError("deliverable.fps must be > 0")
    deliverable["fps"] = fps
    # The canonical renderer is honest about its current delivery surface.
    supported = {
        "container": "mp4",
        "video_codec": "h264",
        "pixel_format": "yuv420p",
    }
    for field, expected in supported.items():
        actual = str(deliverable.get(field) or "").strip().lower()
        if actual != expected:
            raise ValueError(
                f"deliverable.{field}={actual!r} is unsupported; current renderer requires {expected!r}"
            )
        deliverable[field] = actual

    audio = deliverable.get("audio")
    if not isinstance(audio, dict):
        raise ValueError("deliverable.audio must be an object")
    if type(audio.get("required")) is not bool:
        raise ValueError("deliverable.audio.required must be boolean")
    if audio["required"]:
        if str(audio.get("codec") or "").lower() != "aac":
            raise ValueError("deliverable.audio.codec must be 'aac'")
        for field in ("sample_rate", "channels"):
            number = _finite_number(audio.get(field), field=f"deliverable.audio.{field}")
            assert number is not None
            if int(number) <= 0:
                raise ValueError(f"deliverable.audio.{field} must be > 0")
            audio[field] = int(number)
        for field in (
            "integrated_loudness_lufs",
            "loudness_tolerance_lu",
            "true_peak_max_dbtp",
        ):
            audio[field] = _finite_number(
                audio.get(field), field=f"deliverable.audio.{field}"
            )
        roles = audio.get("required_roles") or []
        if not isinstance(roles, list) or any(not str(role).strip() for role in roles):
            raise ValueError("deliverable.audio.required_roles must be a string array")
        audio["required_roles"] = list(dict.fromkeys(str(role).strip().lower() for role in roles))

    budget = result.get("budget")
    if not isinstance(budget, dict):
        raise ValueError("budget must be an object")
    cap = _finite_number(budget.get("hard_cap_usd"), field="budget.hard_cap_usd")
    ceiling = _finite_number(hard_cap_usd, field="hard_cap_usd")
    assert cap is not None and ceiling is not None
    if cap <= 0 or cap > ceiling or cap > MAX_MEDIA_BUDGET_USD:
        raise ValueError(
            f"budget.hard_cap_usd must be in (0, {min(ceiling, MAX_MEDIA_BUDGET_USD)}]"
        )
    budget["hard_cap_usd"] = cap
    warning = _finite_number(budget.get("warning_usd"), field="budget.warning_usd")
    assert warning is not None
    budget["warning_usd"] = min(max(0.0, warning), cap)

    media_policy = result.get("media_policy")
    if not isinstance(media_policy, dict):
        raise ValueError("media_policy must be an object")
    for field in (
        "licensed_or_owned_only",
        "provenance_required",
        "full_ai_video_default",
        "generated_video_requires_recorded_blocker",
    ):
        if type(media_policy.get(field)) is not bool:
            raise ValueError(f"media_policy.{field} must be boolean")
    priority = media_policy.get("source_priority") or []
    if not isinstance(priority, list) or any(not str(item).strip() for item in priority):
        raise ValueError("media_policy.source_priority must be a string array")
    media_policy["source_priority"] = list(
        dict.fromkeys(str(item).strip() for item in priority)
    )
    for field in (
        "generated_video_default_calls",
        "generated_video_attempt_cap",
        "generated_image_default_cap",
        "generated_image_blocker_cap",
    ):
        number = _finite_number(media_policy.get(field), field=f"media_policy.{field}")
        assert number is not None
        if number < 0 or int(number) != number:
            raise ValueError(f"media_policy.{field} must be a non-negative integer")
        media_policy[field] = int(number)
    if media_policy["generated_video_default_calls"] > media_policy["generated_video_attempt_cap"]:
        raise ValueError(
            "media_policy.generated_video_default_calls cannot exceed generated_video_attempt_cap"
        )
    if (
        media_policy["generated_video_requires_recorded_blocker"]
        and media_policy["generated_video_default_calls"] > 0
    ):
        raise ValueError(
            "media_policy.generated_video_default_calls must be 0 when a recorded blocker is required"
        )
    if media_policy["generated_image_default_cap"] > media_policy["generated_image_blocker_cap"]:
        raise ValueError(
            "media_policy.generated_image_default_cap cannot exceed generated_image_blocker_cap"
        )
    duration_cap = _finite_number(
        media_policy.get("generated_video_duration_cap_sec"),
        field="media_policy.generated_video_duration_cap_sec",
    )
    assert duration_cap is not None
    if duration_cap < 0:
        raise ValueError(
            "media_policy.generated_video_duration_cap_sec must be >= 0"
        )
    media_policy["generated_video_duration_cap_sec"] = duration_cap
    blocker_types = media_policy.get("generated_image_blocker_types") or []
    if not isinstance(blocker_types, list) or any(
        not str(item).strip() for item in blocker_types
    ):
        raise ValueError(
            "media_policy.generated_image_blocker_types must be a string array"
        )
    media_policy["generated_image_blocker_types"] = list(
        dict.fromkeys(str(item).strip().lower() for item in blocker_types)
    )

    acceptance = result.get("acceptance")
    if not isinstance(acceptance, dict):
        raise ValueError("acceptance must be an object")
    units = acceptance.get("edit_units")
    if not isinstance(units, dict):
        raise ValueError("acceptance.edit_units must be an object")
    for field in ("min", "max"):
        number = _finite_number(
            units.get(field), field=f"acceptance.edit_units.{field}", allow_none=True
        )
        units[field] = int(number) if number is not None else None
        if units[field] is not None and units[field] < 0:
            raise ValueError(f"acceptance.edit_units.{field} must be >= 0")
    if units["min"] is not None and units["max"] is not None and units["min"] > units["max"]:
        raise ValueError("acceptance.edit_units.min must be <= max")
    for field in ("median_shot_duration_max_sec", "static_shot_max_sec"):
        number = _finite_number(
            acceptance.get(field), field=f"acceptance.{field}", allow_none=True
        )
        if number is not None and number <= 0:
            raise ValueError(f"acceptance.{field} must be > 0")
        acceptance[field] = number
    motion = _finite_number(
        acceptance.get("verified_motion_min_sec", 0),
        field="acceptance.verified_motion_min_sec",
    )
    assert motion is not None
    acceptance["verified_motion_min_sec"] = max(0.0, motion)
    public_min = _finite_number(
        acceptance.get("licensed_public_motion_assets_min", 0),
        field="acceptance.licensed_public_motion_assets_min",
    )
    assert public_min is not None
    acceptance["licensed_public_motion_assets_min"] = max(0, int(public_min))
    samples = _finite_number(
        acceptance.get("review_sample_frames_min", 6),
        field="acceptance.review_sample_frames_min",
    )
    assert samples is not None
    acceptance["review_sample_frames_min"] = max(1, int(samples))
    for field in ("agent_review_checks", "creative_dimensions"):
        entries = acceptance.get(field) or []
        if not isinstance(entries, list) or any(not str(entry).strip() for entry in entries):
            raise ValueError(f"acceptance.{field} must be a string array")
        acceptance[field] = list(dict.fromkeys(str(entry).strip() for entry in entries))
    return result


def _generation_blocker(
    creative_ir: Mapping[str, Any], kind: str
) -> tuple[str, str]:
    strategy = (
        creative_ir.get("asset_strategy")
        if isinstance(creative_ir.get("asset_strategy"), Mapping)
        else {}
    )
    raw = strategy.get(kind)
    if not isinstance(raw, Mapping):
        return "", ""
    reason = str(raw.get("blocker") or raw.get("reason") or "").strip()
    blocker_type = str(raw.get("blocker_type") or raw.get("type") or "").strip().lower()
    return reason, blocker_type


def media_policy_decision(
    contract: Mapping[str, Any],
    creative_ir: Mapping[str, Any],
    budget_snapshot: Mapping[str, Any],
    tool_name: str,
) -> dict[str, Any]:
    """Evaluate the generation exception before any provider reservation."""

    name = str(tool_name or "").strip()
    if name not in {"generate_video", "generate_image"}:
        return {"allowed": True, "tool_name": name, "reason": "not generation-gated"}
    policy = contract.get("media_policy") if isinstance(contract.get("media_policy"), Mapping) else {}
    counts = (
        budget_snapshot.get("tool_reserved_calls")
        if isinstance(budget_snapshot.get("tool_reserved_calls"), Mapping)
        else {}
    )
    used = int(counts.get(name) or 0)

    if name == "generate_video":
        reason, _blocker_type = _generation_blocker(creative_ir, "generated_video")
        requires_blocker = bool(policy.get("generated_video_requires_recorded_blocker", True))
        default_calls = int(policy.get("generated_video_default_calls") or 0)
        attempt_cap = int(policy.get("generated_video_attempt_cap") or 0)
        allowed_cap = attempt_cap if reason else default_calls
        if requires_blocker and not reason:
            return {
                "allowed": False,
                "tool_name": name,
                "reason": (
                    "generated video is an exception: record why owned, licensed, and local "
                    "production cannot satisfy this shot in Creative IR "
                    "/asset_strategy/generated_video/blocker"
                ),
                "used_calls": used,
                "allowed_calls": allowed_cap,
            }
        if used >= allowed_cap:
            return {
                "allowed": False,
                "tool_name": name,
                "reason": f"generated-video call policy exhausted: {used} >= {allowed_cap}",
                "used_calls": used,
                "allowed_calls": allowed_cap,
            }
        return {
            "allowed": True,
            "tool_name": name,
            "reason": "recorded production blocker permits one bounded attempt",
            "used_calls": used,
            "allowed_calls": allowed_cap,
            "blocker": reason,
        }

    reason, blocker_type = _generation_blocker(creative_ir, "generated_image")
    default_cap = int(policy.get("generated_image_default_cap") or 0)
    blocker_cap = int(policy.get("generated_image_blocker_cap") or default_cap)
    allowed_types = {
        str(item).strip().lower()
        for item in (policy.get("generated_image_blocker_types") or [])
        if str(item).strip()
    }
    blocker_valid = bool(reason) and (not allowed_types or blocker_type in allowed_types)
    allowed_cap = blocker_cap if blocker_valid else default_cap
    if reason and not blocker_valid:
        return {
            "allowed": False,
            "tool_name": name,
            "reason": (
                f"generated-image blocker type {blocker_type or '(missing)'!r} is not allowed; "
                f"expected one of {sorted(allowed_types)}"
            ),
            "used_calls": used,
            "allowed_calls": allowed_cap,
        }
    if used >= allowed_cap:
        return {
            "allowed": False,
            "tool_name": name,
            "reason": (
                "generated images are disabled by default; record an allowed production "
                "blocker in Creative IR /asset_strategy/generated_image"
                if allowed_cap <= 0
                else f"generated-image call policy exhausted: {used} >= {allowed_cap}"
            ),
            "used_calls": used,
            "allowed_calls": allowed_cap,
        }
    return {
        "allowed": True,
        "tool_name": name,
        "reason": "recorded production blocker permits one bounded image attempt",
        "used_calls": used,
        "allowed_calls": allowed_cap,
        "blocker": reason,
        "blocker_type": blocker_type,
    }


def contract_gaps(contract: Mapping[str, Any]) -> list[str]:
    """Return facts still missing before a run has a real production target."""

    gaps: list[str] = []
    if not str(contract.get("brief") or "").strip():
        gaps.append("reality_contract.brief")
    deliverable = contract.get("deliverable")
    if not isinstance(deliverable, Mapping) or deliverable.get("duration_sec") is None:
        gaps.append("reality_contract.deliverable.duration_sec")
    return gaps


def required_acceptance_check_codes(contract: Mapping[str, Any]) -> frozenset[str]:
    """Compile the exact machine/review proof surface for this contract."""

    deliverable = contract.get("deliverable") if isinstance(contract.get("deliverable"), Mapping) else {}
    audio = deliverable.get("audio") if isinstance(deliverable.get("audio"), Mapping) else {}
    acceptance = contract.get("acceptance") if isinstance(contract.get("acceptance"), Mapping) else {}
    required = {
        "current_export_render_semantics",
        "contract_duration",
        "contract_width",
        "contract_height",
        "contract_fps",
        "contract_video_codec",
        "contract_pixel_format",
        "full_decode",
        "no_dropped_fields",
        "asset_provenance_complete",
        "no_tmp_references",
        "media_budget",
        "duplicate_billing",
        "current_preview_render_semantics",
        "preview_export_graph_parity",
    }
    if bool(audio.get("required", True)):
        required.update(
            {
                "contract_audio_codec",
                "contract_audio_sample_rate",
                "contract_audio_channels",
                "video_duration_matches_delivery",
                "audio_duration_matches_picture",
                "integrated_loudness",
                "true_peak",
            }
        )
        if audio.get("required_roles"):
            required.add("audio_roles_complete")
    units = acceptance.get("edit_units") if isinstance(acceptance.get("edit_units"), Mapping) else {}
    if units.get("min") is not None or units.get("max") is not None:
        required.add("edit_unit_count")
    if acceptance.get("median_shot_duration_max_sec") is not None:
        required.add("median_shot_duration")
    if float(acceptance.get("verified_motion_min_sec") or 0) > 0:
        required.add("verified_motion_coverage")
    if int(acceptance.get("licensed_public_motion_assets_min") or 0) > 0:
        required.add("public_motion_asset_count")
    if acceptance.get("static_shot_max_sec") is not None:
        required.add("static_shot_limit")
    required.update(
        f"review_{name}" for name in (acceptance.get("agent_review_checks") or [])
    )
    return frozenset(required)


def render_expectations(contract: Mapping[str, Any], *, timeline_duration: float) -> dict[str, Any]:
    """Compile contract fields consumed by the canonical RenderReceipt gate."""

    deliverable = contract.get("deliverable") if isinstance(contract.get("deliverable"), Mapping) else {}
    audio = deliverable.get("audio") if isinstance(deliverable.get("audio"), Mapping) else {}
    target_duration = float(deliverable.get("duration_sec") or timeline_duration)
    tolerance = float(deliverable.get("duration_tolerance_sec") or 0.5)
    required_audio = bool(audio.get("required", True))
    return {
        "duration": target_duration,
        "duration_min": max(0.0, target_duration - tolerance),
        "duration_max": target_duration + tolerance,
        "fps": float(deliverable.get("fps") or 0),
        "width": int(deliverable.get("width") or 0),
        "height": int(deliverable.get("height") or 0),
        "has_audio": required_audio,
        "require_h264_yuv420p": True,
        "video_duration": target_duration,
        "audio_duration": target_duration if required_audio else None,
        "audio_sample_rate": int(audio.get("sample_rate") or 0) if required_audio else None,
        "audio_channels": int(audio.get("channels") or 0) if required_audio else None,
        "integrated_loudness_lufs": audio.get("integrated_loudness_lufs") if required_audio else None,
        "loudness_tolerance_lu": float(audio.get("loudness_tolerance_lu") or 1.0),
        "true_peak_max_dbtp": audio.get("true_peak_max_dbtp") if required_audio else None,
    }


__all__ = [
    "MAX_MEDIA_BUDGET_USD",
    "contract_gaps",
    "default_reality_contract",
    "media_policy_decision",
    "normalize_reality_contract",
    "render_expectations",
    "required_acceptance_check_codes",
]
