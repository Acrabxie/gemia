"""Agent-facing API — a movement brief in, a frame-safe transform track out.

This is the creative director for the camera library: it resolves style +
feelings + overrides into the four axes, derives a :class:`MotionProfile`,
asks the chosen move for its base keyframes, lays the seeded handheld layer on
top, and returns the track together with an explainable ``plan``.

Brief shape (everything optional except that it be a dict)::

    {"move": "push_in",                # any registered move (see catalog)
     "subject": {"x": 0.42, "y": 0.55, "scale": 1.0},   # focal point, 0..1
     "style": "cinematic",             # archetype or alias ("doc", "still")
     "feeling": ["epic", "steady"],
     "energy": 0.8,                     # shorthand override for the energy axis
     "duration": 6.0,                   # seconds, capped at 60 (transform layer)
     "canvas": {"width": 1920, "height": 1080},
     "params": {"drift": 0.7},          # explicit axis overrides (win)
     "seed": 7}

The whole track is deterministic in ``seed``: same brief → byte-identical
output. :func:`adjust_track` folds feedback ("more handheld", "更稳") into the
brief and re-derives with the *same* seed — adjustment is a re-derivation, never
keyframe surgery.
"""
from __future__ import annotations

from typing import Any

from lumenframe.craft import ResolvedAxes, new_rng

from lumenframe.camera import camera as cam
from lumenframe.camera.params import SPACE, feedback_vocab
from lumenframe.camera.styles import camera_styles

#: Transform layers ride the same clip cap as the html layer (≤ 60s).
MAX_DURATION = 60.0

DEFAULTS: dict[str, Any] = {
    "move": "push_in",
    "duration": 5.0,
    "canvas": {"width": 1920, "height": 1080},
    "seed": 7,
}


class BriefError(ValueError):
    """Raised for a structurally unusable brief (bad move, duration, canvas)."""


def _resolve_axes(brief: dict[str, Any]) -> ResolvedAxes:
    """Resolve the four axes from a brief (style → feelings → overrides).

    A top-level ``energy`` shorthand is folded in as an override so an agent can
    write ``{"energy": 0.8}`` without reaching for ``params``. Shared by build
    and by the feedback loop so accumulation starts from the true current axes.
    """
    overrides = dict(brief.get("params") or {})
    if "energy" in brief and "energy" not in overrides:
        overrides["energy"] = float(brief["energy"])
    return camera_styles().resolve_params(
        style=brief.get("style"),
        feelings=list(brief.get("feeling") or []),
        overrides=overrides,
    )


def _subject(brief: dict[str, Any]) -> dict[str, float]:
    """Focal point, clamped to the frame; missing → dead centre."""
    from lumenframe.craft import clamp01

    raw = brief.get("subject") if isinstance(brief.get("subject"), dict) else {}
    return {
        "x": clamp01(float(raw.get("x", 0.5))),
        "y": clamp01(float(raw.get("y", 0.5))),
        "scale": float(raw.get("scale", 1.0)),
    }


def build_track(brief: dict[str, Any]) -> dict[str, Any]:
    """Brief → ``{"track", "plan", "notes"}`` (deterministic per seed)."""
    if not isinstance(brief, dict):
        raise BriefError("brief must be a dict")

    move = str(brief.get("move") or DEFAULTS["move"]).strip()
    move_fn = cam.MOVES.get(move)
    if move_fn is None:
        raise BriefError(f"unknown move {move!r} (use {cam.move_names()})")

    raw_duration = brief.get("duration")
    duration = float(raw_duration) if raw_duration is not None else DEFAULTS["duration"]
    if duration <= 0:
        raise BriefError("duration must be > 0")
    duration = min(duration, MAX_DURATION)

    canvas = {**DEFAULTS["canvas"], **(brief.get("canvas") or {})}
    w, h = int(canvas["width"]), int(canvas["height"])
    if w <= 0 or h <= 0:
        raise BriefError("canvas width/height must be > 0")

    seed = int(brief.get("seed", DEFAULTS["seed"]))
    level = _resolve_axes(brief)
    subject = _subject(brief)

    profile = cam.MotionProfile.from_axes(
        energy=level["energy"], smoothness=level["smoothness"],
        drama=level["drama"], drift=level["drift"],
        w=w, h=h, duration=duration,
    )

    rng = new_rng(seed)
    base = move_fn(profile, subject, rng, dict(brief.get("params") or {}))
    channels = cam.handheld_channels(profile, rng)
    handheld = _handheld_spec(channels, profile, duration, level["energy"])

    used_eases = sorted({kf["ease"] for kf in base if kf["ease"]})
    track = {
        "duration": round(duration, 4),
        "canvas": {"width": w, "height": h},
        "move": move,
        "style": level.hints.get("style"),
        "focal": {"x": round(subject["x"], 4), "y": round(subject["y"], 4)},
        "keyframes": base,
        "handheld": handheld,
        "easings": {name: list(cam.EASINGS[name]) for name in used_eases},
    }

    plan = {
        "move": move,
        "style": level.hints.get("style"),
        "duration": round(duration, 4),
        "seed": seed,
        "focal": track["focal"],
        "axes": level.to_dict(),
        "scale_range": _scale_range(base),
        "push_delta": round(profile.push_delta, 4),
        "drift_amp_px": round(profile.drift_amp_px, 3),
        "eases": used_eases,
        "segments": [
            {"t0": base[i - 1]["t"], "t1": base[i]["t"], "ease": base[i]["ease"]}
            for i in range(1, len(base))
        ],
    }

    notes: list[str] = []
    if level.unknown_feelings:
        notes.append(f"unrecognised feelings ignored: {', '.join(level.unknown_feelings)}")
    if float(brief.get("duration") or duration) > MAX_DURATION:
        notes.append(f"duration capped at {MAX_DURATION}s (transform layer limit)")
    if profile.drift <= 0.001:
        notes.append("drift is ~0 — the frame is locked with no handheld layer")
    return {"track": track, "plan": plan, "notes": notes}


def adjust_track(brief: dict[str, Any], feedback: list[str]) -> dict[str, Any]:
    """Apply feedback phrases to a brief and rebuild with the SAME seed.

    Returns :func:`build_track`'s result plus ``brief`` (the adjusted brief to
    persist) and feedback notes. Unknown phrases are reported, never fatal; and
    if recognised feedback moved nothing (every targeted axis was already at its
    limit) we say so rather than silently no-op'ing.
    """
    before = build_track(brief)
    vocab = feedback_vocab()
    new_brief, unknown = vocab.apply(brief, list(feedback or []), _resolve_axes)
    result = build_track(new_brief)
    result["brief"] = new_brief
    if unknown:
        known = vocab.vocabulary()
        result["notes"].append(
            f"unrecognised feedback ignored: {', '.join(unknown)} "
            f"(known: {', '.join(known[:12])}, …)"
        )
    recognised = [p for p in (feedback or []) if p not in unknown]
    if recognised and _track_signature(before["track"]) == _track_signature(result["track"]):
        result["notes"].append(
            "feedback recognised but the track did not change — the targeted "
            "axes are already at their limit"
        )
    return result


def _handheld_spec(channels: dict[str, list[dict[str, float]]], profile: cam.MotionProfile,
                   duration: float, energy: float) -> dict[str, Any] | None:
    """Pre-sample the seeded sine stacks so consumers need no trig.

    The sample count is a deterministic function of duration and energy (a
    busier camera is sampled finer), never of the RNG, so the digest is stable.
    """
    if not channels:
        return None
    n = round(duration * (6.0 + 8.0 * energy))
    n = int(min(max(n, 16), 72))
    samples = []
    for i in range(n + 1):
        t = i / n
        samples.append({
            "t": round(t, 4),
            "tx": round(cam.sample_channel(channels["tx"], t), 3),
            "ty": round(cam.sample_channel(channels["ty"], t), 3),
            "rot": round(cam.sample_channel(channels["rot"], t), 4),
        })
    return {
        "amp_px": round(profile.drift_amp_px, 3),
        "rot_deg": round(profile.drift_rot_deg, 4),
        "channels": {k: [dict(s) for s in v] for k, v in channels.items()},
        "samples": samples,
    }


def _scale_range(base: list[dict[str, Any]]) -> list[float]:
    scales = [kf["scale"] for kf in base]
    return [round(min(scales), 5), round(max(scales), 5)]


def _track_signature(track: dict[str, Any]) -> str:
    from lumenframe.craft import stable_digest
    from lumenframe.craft.determinism import round_floats

    payload = {k: track[k] for k in ("keyframes", "handheld", "move", "style", "focal")}
    return stable_digest(round_floats(payload, 4))
