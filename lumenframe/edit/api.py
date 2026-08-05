"""Agent-facing API — a sequence of clips in, a reasoned cut plan out.

This is the **editor**: the only layer that sees the whole sequence at once. It
validates the brief, resolves the cut style + feelings + overrides into axes,
then walks the joins deciding — for each one — whether it stays a straight cut
(the default), becomes a match/action cut, earns a seasoning transition, needs a
J/L audio split, or needs a cutaway to dodge a jump. Every non-default choice
records a ``reason`` so the plan is auditable, and every number comes from the
taste-floor maths in :mod:`lumenframe.edit.grammar`, never from the caller.

Brief shape (everything optional except ``clips``)::

    {"clips": [{"id": "a", "duration": 3.0, "has_action": false,
                "tags": ["street"], "scene": "market"}, ...],   # ≥ 2 required
     "style": "documentary",          # archetype or alias ("mtv"/"film")
     "feeling": ["seamless", "slow"],
     "params": {"pace": 0.3},         # explicit axis overrides (win)
     "seed": 7}

The output is a **cut plan**: one entry per join, plus a ``plan`` summary and
human ``notes``. Feedback ("more seamless", "更快") folds into the brief and the
plan re-derives with the *same seed* — adjustment is re-editing, never patching
the emitted plan.
"""
from __future__ import annotations

from typing import Any

from lumenframe.craft import new_rng, stable_digest
from lumenframe.craft.determinism import round_floats
from lumenframe.edit import grammar
from lumenframe.edit.params import SPACE, edit_feedback
from lumenframe.edit.styles import STYLES

DEFAULT_SEED = 7


class EditBriefError(ValueError):
    """Raised for a structurally unusable edit brief."""


# ── brief validation ───────────────────────────────────────────────────────

def _normalise_clips(raw: Any) -> list[dict[str, Any]]:
    """Validate + normalise the clip list; raise :class:`EditBriefError`.

    Requires at least two clips (you cannot join fewer), each a dict with a
    unique ``id`` and a positive ``duration``. Optional facts (``has_action``,
    ``tags``, ``scene``) are coerced to their canonical types so the grammar can
    trust them without re-checking.
    """
    if not isinstance(raw, list) or len(raw) < 2:
        raise EditBriefError("brief needs a 'clips' list of at least 2 clips")
    seen: set[str] = set()
    clips: list[dict[str, Any]] = []
    for i, c in enumerate(raw):
        if not isinstance(c, dict):
            raise EditBriefError(f"clip #{i} must be an object")
        cid = str(c.get("id") or "").strip()
        if not cid:
            raise EditBriefError(f"clip #{i} needs a non-empty 'id'")
        if cid in seen:
            raise EditBriefError(f"duplicate clip id {cid!r}")
        seen.add(cid)
        try:
            duration = float(c.get("duration"))
        except (TypeError, ValueError):
            raise EditBriefError(f"clip {cid!r} needs a numeric 'duration' (seconds)")
        if duration <= 0:
            raise EditBriefError(f"clip {cid!r} duration must be > 0")
        clips.append({
            "id": cid,
            "duration": duration,
            "duration_ms": int(round(duration * 1000)),
            "has_action": bool(c.get("has_action")),
            "tags": [str(t) for t in (c.get("tags") or [])],
            "scene": (str(c["scene"]) if c.get("scene") is not None else None),
        })
    return clips


def _normalise_params(raw: Any) -> dict[str, float]:
    """Validate the ``params`` overrides; raise :class:`EditBriefError` on junk.

    ``params`` must be an object of axis → number. Coercing here (instead of deep
    inside the shared axis maths) keeps every bad-input path funnelling through
    :class:`EditBriefError`, so the tool boundary can return a uniform E_ARG
    rather than leaking a bare ``ValueError``/``TypeError`` from ``float()``.
    """
    if raw is None:
        return {}
    if not isinstance(raw, dict):
        raise EditBriefError("'params' must be an object of axis overrides")
    overrides: dict[str, float] = {}
    for key, value in raw.items():
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise EditBriefError(f"param {str(key)!r} must be a number")
        overrides[str(key)] = float(value)
    return overrides


def _resolve_axes(brief: dict[str, Any]):
    """Resolve the brief's style + feelings + overrides into axes (+ hints)."""
    return STYLES.resolve_params(
        style=brief.get("style"),
        feelings=list(brief.get("feeling") or []),
        overrides=_normalise_params(brief.get("params")),
    )


# ── the build ──────────────────────────────────────────────────────────────

def build_cut_plan(brief: dict[str, Any]) -> dict[str, Any]:
    """Brief → ``{"cut_plan", "plan", "notes"}`` (deterministic per seed)."""
    if not isinstance(brief, dict):
        raise EditBriefError("brief must be an object")
    clips = _normalise_clips(brief.get("clips"))
    try:
        seed = int(brief.get("seed", DEFAULT_SEED))
    except (TypeError, ValueError):
        raise EditBriefError("'seed' must be an integer")
    rng = new_rng(seed)

    level = _resolve_axes(brief)
    style_name = level.hints["style"]
    hints = STYLES.spec(style_name).hints
    pace = level["pace"]
    invisibility = level["invisibility"]
    drama = level["drama"]
    variety = level["variety"]

    n_joins = len(clips) - 1
    floor_ms = grammar.min_shot_ms(pace)
    budget = grammar.transition_budget(n_joins, hints["cut_frac"], drama)

    # Rank joins by how much they *want* a transition; spend the scarce budget on
    # the worthiest. Ties break on the seeded RNG so equal briefs stay stable per
    # seed yet can differ across seeds.
    joins = list(range(n_joins))
    ranked = sorted(
        joins,
        key=lambda i: (-grammar.join_worth(clips[i], clips[i + 1]), rng.random(), i),
    )
    transition_slots = set(ranked[:budget])

    notes: list[str] = []
    cut_plan: list[dict[str, Any]] = []
    # slot_index counts only the joins that actually get a seasoning transition,
    # so palette rotation is dense and deterministic.
    slot_index = 0
    # committed_in[k] is the incoming trim magnitude already spent on clip k (as
    # the to_clip of the *preceding* join). Threading it into each clip's
    # outgoing trim keeps a shared interior clip's two independent edge-trims
    # from stacking below the min-shot floor — the floor is a single per-clip
    # slack budget, not a per-edge one.
    committed_in = [0] * len(clips)

    for i in range(n_joins):
        a, b = clips[i], clips[i + 1]
        entry = _plan_join(
            a, b, i, n_joins,
            in_slot=i in transition_slots,
            slot_index=slot_index if i in transition_slots else 0,
            level=level, hints=hints, pace=pace, invisibility=invisibility,
            drama=drama, variety=variety, floor_ms=floor_ms, rng=rng, notes=notes,
            a_reserved=committed_in[i],
        )
        # Record the incoming trim this join spends on clip i+1 so its own
        # outgoing trim (decided at the next join) draws from the remaining slack.
        committed_in[i + 1] = abs(entry["trim_in_adjust"])
        if not grammar.is_straight(entry["transition"]):
            slot_index += 1
        cut_plan.append(entry)

    _floor_notes(clips, floor_ms, notes)
    if level.unknown_feelings:
        notes.append("unrecognised feelings ignored: " + ", ".join(level.unknown_feelings))

    plan = {
        "style": style_name,
        "seed": seed,
        "axes": level.to_dict()["axes"],
        "clips": len(clips),
        "joins": n_joins,
        "min_shot_ms": floor_ms,
        "transition_budget": budget,
        "transitions_used": sum(1 for e in cut_plan if not grammar.is_straight(e["transition"])),
        "digest": stable_digest(round_floats(cut_plan)),
    }
    return {"cut_plan": cut_plan, "plan": plan, "notes": notes}


def _plan_join(
    a: dict[str, Any], b: dict[str, Any], i: int, n_joins: int, *,
    in_slot: bool, slot_index: int, level, hints: dict[str, Any],
    pace: float, invisibility: float, drama: float, variety: float,
    floor_ms: int, rng, notes: list[str], a_reserved: int = 0,
) -> dict[str, Any]:
    """Decide the single join between ``a`` and ``b`` — the taste table.

    Order of preference encodes the floor: a straight/match cut is the default;
    a transition is applied only if this join won a budget slot; jump risk is
    always mitigated (cutaway or cover); action cuts and J/L splits are layered
    on straight joins. Every departure from a plain cut is explained in
    ``reason``.
    """
    reasons: list[str] = []
    transition = "cut"
    duration_ms = 0

    if in_slot:
        transition = grammar.choose_transition(
            hints["palette"], hints["primary"], variety, drama, slot_index, a, b, rng)
        meta = grammar.transition_meta(transition)
        duration_ms = grammar.dissolve_ms(meta["base_ms"], pace, hints["dissolve_scale"])
        reasons.append(meta["when"])
    elif hints["match_cuts"] and a.get("has_action") and b.get("has_action") \
            and a.get("scene") == b.get("scene"):
        # Continuous motion across the join → an invisible action/match cut.
        transition = "match_cut"
        reasons.append("continuous action across the join — cut on the movement")

    entry: dict[str, Any] = {
        "from_clip": a["id"],
        "to_clip": b["id"],
        "transition": transition,
        "duration_ms": duration_ms,
        "j_cut_ms": 0,
        "l_cut_ms": 0,
        "trim_in_adjust": 0,
        "trim_out_adjust": 0,
        "cutaway": False,
        "reason": "",
    }

    # Cut on action: nudge the trims so the cut lands on the movement, never
    # after it settles. Clamped so a nudge never shoves a shot under the floor.
    if grammar.is_straight(transition):
        nudge = grammar.action_trim_ms(pace)
        if a.get("has_action"):
            # a's outgoing edge draws from the slack left after a's incoming trim.
            entry["trim_out_adjust"] = -_bounded_trim(
                nudge, a["duration_ms"], floor_ms, a_reserved)
            reasons.append("trim outgoing to cut on its action")
        if b.get("has_action"):
            entry["trim_in_adjust"] = _bounded_trim(nudge, b["duration_ms"], floor_ms)
            reasons.append("ease into incoming action")

    # J/L audio split: on straight joins, let sound lead or trail so the picture
    # cut disappears. Alternate J and L across joins for a natural weave.
    if grammar.is_straight(transition):
        split = grammar.audio_split_ms(invisibility, hints["audio"], kind="j" if i % 2 == 0 else "l")
        if split:
            if i % 2 == 0:
                entry["j_cut_ms"] = split
                reasons.append(f"J-cut: {b['id']} audio leads by {split}ms")
            else:
                entry["l_cut_ms"] = split
                reasons.append(f"L-cut: {a['id']} audio trails by {split}ms")

    # Jump-cut guard: same-scene, similar, static shots must not cut straight.
    # Mitigate with a cutaway — a straight join that adds NO seasoning transition,
    # so a run of static coverage cannot silently salt every join with a showy
    # whip pan and blow past the transition budget. A covering transition would
    # have to be charged against that budget, which the budget maths already
    # spend on the joins that most earn one; the leftover jump risks hold as
    # cutaways so ``transitions_used <= transition_budget`` always holds.
    if grammar.is_straight(transition) and grammar.is_jump_risk(a, b):
        entry["cutaway"] = True
        reasons.append(f"jump-cut risk: insert a cutaway between {a['id']} and {b['id']}")
        notes.append(f"jump-cut risk at {a['id']}→{b['id']} — insert a cutaway shot")

    # Montage accelerates: the target trim rises the closer to the end, so the
    # cadence tightens toward a climax. Each trim is still bounded by the clip's
    # own min-shot floor (and by any incoming trim already spent on it), so an
    # already-short clip is trimmed less than an earlier long one — the target
    # is monotonic, the *applied* trim is not, because the floor wins. (The
    # floor is the library's #1 guarantee; a montage may not machine-gun a short
    # shot below it just to keep the magnitudes strictly increasing.)
    if hints["accelerate"] and n_joins > 1 and grammar.is_straight(entry["transition"]):
        frac = i / (n_joins - 1)
        accel = _bounded_trim(int(round(frac * 400)), a["duration_ms"], floor_ms, a_reserved)
        if accel:
            entry["trim_out_adjust"] = -max(-entry["trim_out_adjust"], accel)
            reasons.append("accelerating montage cadence")

    entry["reason"] = "; ".join(reasons) if reasons else "straight cut (default)"
    return entry


def _bounded_trim(magnitude: int, clip_ms: int, floor_ms: int, reserved: int = 0) -> int:
    """Clamp a trim magnitude so the shot never falls below the floor.

    A trim may shave at most the slack between the clip's length and the minimum
    shot length, and never more than ~30% of the clip — nudges are nudges.
    ``reserved`` is any trim already committed to this same clip (its other
    edge); the returned trim draws only from the *remaining* slack, so the two
    edge-trims a shared interior clip receives can never stack it below the
    floor. This makes the docstring guarantee ("the shot never falls below the
    floor") true per-clip, not merely per-edge.
    """
    reserved = max(0, reserved)
    slack = max(0, clip_ms - floor_ms - reserved)
    soft = max(0, int(round(clip_ms * 0.3)) - reserved)
    return int(max(0, min(magnitude, slack, soft)))


def _floor_notes(clips: list[dict[str, Any]], floor_ms: int, notes: list[str]) -> None:
    """Warn about clips already shorter than the minimum shot length."""
    short = [c["id"] for c in clips if c["duration_ms"] < floor_ms]
    if short:
        notes.append(
            f"clips below the {floor_ms}ms minimum-shot floor: {', '.join(short)} "
            "— consider merging or holding them longer")


# ── feedback / adjust ──────────────────────────────────────────────────────

def adjust_cut_plan(brief: dict[str, Any], feedback: list[str]) -> dict[str, Any]:
    """Fold feedback into the brief and re-derive with the same seed.

    Returns :func:`build_cut_plan`'s result plus ``brief`` (the adjusted brief to
    persist). Recognised phrases move axes and change the plan; unrecognised
    phrases are reported, never fatal. If recognised feedback moved nothing (the
    targeted axes were already at their limit) that is stated honestly.
    """
    before = build_cut_plan(brief)
    vocab = edit_feedback()
    new_brief, unknown = vocab.apply(
        brief, feedback or [], resolve_axes=_resolve_axes)
    result = build_cut_plan(new_brief)
    result["brief"] = new_brief

    recognised = [p for p in (feedback or []) if p not in unknown]
    if recognised and result["plan"]["digest"] == before["plan"]["digest"]:
        result["notes"].append(
            "feedback recognised but the plan did not change — the targeted "
            "parameters are already at their limit")
    if unknown:
        result["notes"].append(
            "unrecognised feedback ignored: " + ", ".join(unknown)
            + " (known: " + ", ".join(vocab.vocabulary()[:12]) + ", …)")
    return result
