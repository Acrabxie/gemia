"""Session adapter for Quanta state-frame PNG materialization.

``assemble_quanta`` reuses this adapter before placing the registered images
on the clip timeline, and ``refine_quantum`` uses it to rematerialize one
content scope. The state tree is the source of truth; this adapter projects
it through ``gemia.quanta.traverse.flat_view`` so the placed-blocks → PNG
chain (layout/raster) stays rewrite-free. Hidden subtrees do not materialize
in v1 (they are outside the default walk and the mp4 flatten).
"""
from __future__ import annotations

from pathlib import Path
from typing import Any, Mapping

from gemia.quanta import (
    QuantaMaterializeError,
    build_quanta_pager_url,
    build_quanta_pager_url_from_manifest,
    render_quanta_frames,
)
from gemia.quanta.traverse import flat_view, lift_flat_quanta
from gemia.tools._context import ToolContext


def _image_asset_ids(blocks: Any) -> list[str]:
    result: list[str] = []
    for block in blocks if isinstance(blocks, list) else []:
        if not isinstance(block, Mapping):
            continue
        if block.get("kind") == "group":
            result.extend(_image_asset_ids(block.get("children")))
        elif block.get("kind") == "image" and block.get("asset_id"):
            asset_id = str(block["asset_id"])
            if asset_id not in result:
                result.append(asset_id)
    return result


def _render_view(quanta: Mapping[str, Any]) -> dict[str, Any]:
    """Canonical flat projection of the state tree (visible walk only)."""
    if not isinstance(quanta, Mapping):
        raise QuantaMaterializeError("quanta must be a mapping")
    return flat_view(lift_flat_quanta(quanta))


def materialize_quanta_frame_assets(
    quanta: Mapping[str, Any],
    ctx: ToolContext,
    *,
    scale: int = 1,
    fail_on_overflow: bool = False,
) -> dict[str, Any]:
    """Render, write, and register all state frames for one session.

    Rendering completes in memory before any output asset is allocated, so a
    layout/font/source failure cannot leave a partial registered quanta.
    """
    view = _render_view(quanta)
    source_ids: list[str] = []
    for scope in view.get("slides") or []:
        if isinstance(scope, Mapping):
            for asset_id in _image_asset_ids(scope.get("blocks")):
                if asset_id not in source_ids:
                    source_ids.append(asset_id)
    image_sources: dict[str, bytes] = {}
    for asset_id in source_ids:
        if not ctx.registry.contains(asset_id):
            raise QuantaMaterializeError(
                f"quanta image asset {asset_id!r} is not in this session registry"
            )
        record = ctx.registry.get(asset_id)
        if record.kind != "image":
            raise QuantaMaterializeError(
                f"quanta image asset {asset_id!r} is {record.kind!r}, expected 'image'"
            )
        try:
            image_sources[asset_id] = Path(record.path).read_bytes()
        except OSError as exc:
            raise QuantaMaterializeError(
                f"quanta image asset {asset_id!r} cannot be read"
            ) from exc

    frames = render_quanta_frames(
        view,
        image_sources=image_sources,
        scale=scale,
        fail_on_overflow=fail_on_overflow,
    )
    output_dir = Path(ctx.output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    asset_ids: list[str] = []
    manifest: list[dict[str, Any]] = []
    for frame in frames:
        asset_id = ctx.registry.allocate_id("image")
        path = ctx.child_path(asset_id, ".png")
        path.write_bytes(frame.png_bytes)
        ctx.registry.register_output(
            asset_id,
            kind="image",
            path=path,
            summary=(
                f"quanta frame {frame.scope_id}/{frame.state_id} "
                f"({frame.scope_index + 1}:{frame.state_index + 1})"
            ),
            lineage=frame.source_asset_ids,
        )
        asset_ids.append(asset_id)
        manifest.append(frame.manifest_entry(asset_id))

    pager_url = build_quanta_pager_url(ctx.session_id, frames, asset_ids)
    first_state_pager_url = build_quanta_pager_url(
        ctx.session_id, frames, asset_ids, first_build_only=True
    )
    overflow = [
        {
            "scope_id": frame.scope_id,
            "state_id": frame.state_id,
            "items": [dict(item) for item in frame.overflow],
        }
        for frame in frames
        if frame.overflow
    ]
    return {
        "kind": "quanta",
        "asset_id": asset_ids[0] if asset_ids else None,
        "frame_asset_ids": asset_ids,
        "frames": manifest,
        "pager_url": pager_url,
        "first_state_pager_url": first_state_pager_url,
        "scope_count": len({frame.scope_id for frame in frames}),
        "frame_count": len(frames),
        "overflow": overflow,
        "summary": (
            f"rendered {len({frame.scope_id for frame in frames})} scope(s) / "
            f"{len(frames)} state(s)"
        ),
        "rematerialization_scope": "quanta",
    }


def _expected_states(scope: Mapping[str, Any]) -> list[tuple[str, float]]:
    states = [
        state for state in scope.get("builds") or [] if isinstance(state, Mapping)
    ]
    if not states:
        return [("b1", 3.0)]
    return [
        (str(state.get("id") or "b1"), float(state.get("dwell_sec") or 0.0))
        for state in states
    ]


def _reusable_frames(
    previous: Mapping[str, Any] | None,
    ctx: ToolContext,
    scopes: list[Mapping[str, Any]],
    changed_scope_id: str,
) -> dict[str, list[dict[str, Any]]] | None:
    if not isinstance(previous, Mapping):
        return None
    raw_frames = previous.get("frames")
    if not isinstance(raw_frames, list):
        return None
    grouped: dict[str, list[dict[str, Any]]] = {}
    for raw in raw_frames:
        if not isinstance(raw, Mapping):
            return None
        frame = dict(raw)
        scope_id = str(frame.get("scope_id") or "")
        asset_id = str(frame.get("asset_id") or "")
        if not scope_id or not asset_id or not ctx.registry.contains(asset_id):
            return None
        if not ctx.registry.get(asset_id).path.is_file():
            return None
        grouped.setdefault(scope_id, []).append(frame)
    for scope in scopes:
        scope_id = str(scope.get("id") or "")
        if scope_id == changed_scope_id:
            continue
        actual = sorted(
            grouped.get(scope_id) or [],
            key=lambda frame: int(frame.get("state_index", -1)),
        )
        expected = _expected_states(scope)
        if len(actual) != len(expected):
            return None
        for index, (frame, (state_id, dwell)) in enumerate(zip(actual, expected)):
            if int(frame.get("state_index", -1)) != index:
                return None
            if str(frame.get("state_id") or "") != state_id:
                return None
            if abs(float(frame.get("dwell_sec") or 0.0) - dwell) > 1e-6:
                return None
        grouped[scope_id] = actual
    return grouped


def rematerialize_quanta_scope_assets(
    quanta: Mapping[str, Any],
    ctx: ToolContext,
    *,
    scope_id: str,
    previous: Mapping[str, Any] | None,
    scale: int = 1,
    fail_on_overflow: bool = False,
) -> dict[str, Any]:
    """Render one changed content scope and reuse every unchanged registered
    frame — the edit tree's subtree granularity applied to materialization.

    If the prior frame manifest is missing or stale, this safely falls back to
    whole-quanta materialization; callers can surface the returned scope.
    """
    view = _render_view(quanta)
    scopes = [s for s in view.get("slides") or [] if isinstance(s, Mapping)]
    target = next(
        (scope for scope in scopes if str(scope.get("id") or "") == scope_id),
        None,
    )
    if target is None:
        raise QuantaMaterializeError(f"content scope not found: {scope_id!r}")
    reusable = _reusable_frames(previous, ctx, scopes, scope_id)
    if reusable is None:
        return materialize_quanta_frame_assets(
            quanta, ctx, scale=scale, fail_on_overflow=fail_on_overflow
        )
    if fail_on_overflow:
        for other_id, frames in reusable.items():
            if other_id != scope_id and any(frame.get("overflow") for frame in frames):
                raise QuantaMaterializeError(
                    f"cached scope {other_id} contains overflow; refine that copy first"
                )

    subview = dict(view)
    subview["slides"] = [target]
    subview["default_path"] = [scope_id]
    changed = materialize_quanta_frame_assets(
        subview, ctx, scale=scale, fail_on_overflow=fail_on_overflow
    )
    changed_frames = [
        dict(frame) for frame in changed.get("frames") or []
        if isinstance(frame, Mapping)
    ]
    manifest: list[dict[str, Any]] = []
    for scope_index, scope in enumerate(scopes):
        current_id = str(scope.get("id") or "")
        source = changed_frames if current_id == scope_id else reusable[current_id]
        for state_index, source_frame in enumerate(source):
            frame = dict(source_frame)
            frame["scope_index"] = scope_index
            frame["state_index"] = state_index
            manifest.append(frame)

    asset_ids = [str(frame.get("asset_id") or "") for frame in manifest]
    overflow = [
        {
            "scope_id": str(frame.get("scope_id") or ""),
            "state_id": str(frame.get("state_id") or ""),
            "items": [dict(item) for item in frame.get("overflow") or []],
        }
        for frame in manifest
        if frame.get("overflow")
    ]
    return {
        "kind": "quanta",
        "asset_id": asset_ids[0] if asset_ids else None,
        "frame_asset_ids": asset_ids,
        "frames": manifest,
        "pager_url": build_quanta_pager_url_from_manifest(ctx.session_id, manifest),
        "first_state_pager_url": build_quanta_pager_url_from_manifest(
            ctx.session_id, manifest, first_build_only=True
        ),
        "scope_count": len(scopes),
        "frame_count": len(manifest),
        "overflow": overflow,
        "summary": (
            f"rematerialized scope {scope_id} and reused "
            f"{len(scopes) - 1} unchanged scope(s)"
        ),
        "rematerialization_scope": "scope",
        "rematerialized_scope_id": scope_id,
    }


__all__ = ["materialize_quanta_frame_assets", "rematerialize_quanta_scope_assets"]
