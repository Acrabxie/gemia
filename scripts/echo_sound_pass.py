#!/usr/bin/env python3
"""Apply the deterministic, zero-dollar sound pass for Echo Protocol V1.

The operator only consumes the persisted rough cut and local ffmpeg outputs.
It never calls a provider.  Four reusable SFX assets are registered before a
single atomic timeline patch adds the sound-design track, places eleven cues,
and applies the music/voice ducking relationship.  Stable clip ids make a
partial timeline write a hard failure while an already-complete write can be
reconciled after a process restart.
"""
from __future__ import annotations

import argparse
from dataclasses import dataclass
import hashlib
import json
import mimetypes
from pathlib import Path
import sys
from typing import Any, Callable, Mapping


# A direct script invocation must not resolve ``gemia`` from another editable
# checkout on this machine.
REPO_ROOT = Path(__file__).resolve().parents[1]
try:
    sys.path.remove(str(REPO_ROOT))
except ValueError:
    pass
sys.path.insert(0, str(REPO_ROOT))

from gemia.echo_local_media import AUDIO_SAMPLE_RATE, probe_media, synthesize_sfx
from gemia.session_manager import SessionManager
from lumerai.export_support import clip_dropped_fields
from lumerai.patches import apply_timeline_patches
from scripts import build_echo_protocol_v1 as rough_cut_builder


SESSION_ID = rough_cut_builder.SESSION_ID
RUN_ID = rough_cut_builder.RUN_ID
SOUND_PASS_VERSION = "echo-protocol-v1-sound-pass-1"
SFX_TRACK_ID = "A3"
SFX_CUES = ("impact", "alarm_glitch", "riser", "collapse")
EPSILON = 1e-3


class EchoSoundPassError(RuntimeError):
    """The persisted sound pass is incomplete, ambiguous, or unsafe to edit."""


@dataclass(frozen=True)
class CuePlacement:
    index: int
    start_sec: float
    cue: str

    @property
    def clip_id(self) -> str:
        return f"echo_v1_sfx_{self.index:02d}"


@dataclass(frozen=True)
class SfxAssetFact:
    cue: str
    asset_id: str
    path: Path
    sha256: str
    duration_sec: float
    fingerprint: str
    source: dict[str, Any]
    license: dict[str, Any]


PLACEMENTS: tuple[CuePlacement, ...] = (
    CuePlacement(1, 0.0, "impact"),
    CuePlacement(2, 20.0, "alarm_glitch"),
    CuePlacement(3, 25.0, "impact"),
    CuePlacement(4, 48.0, "alarm_glitch"),
    CuePlacement(5, 78.0, "impact"),
    CuePlacement(6, 86.0, "alarm_glitch"),
    CuePlacement(7, 94.5, "riser"),
    CuePlacement(8, 98.0, "impact"),
    CuePlacement(9, 102.5, "riser"),
    CuePlacement(10, 106.0, "collapse"),
    CuePlacement(11, 115.0, "impact"),
)
STABLE_CLIP_IDS = frozenset(item.clip_id for item in PLACEMENTS)


def _stable_digest(value: Any) -> str:
    encoded = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _budget(run: Mapping[str, Any]) -> dict[str, Any]:
    value = run.get("budget")
    if not isinstance(value, Mapping):
        raise EchoSoundPassError("production run has no canonical budget view")
    return dict(value)


def _assert_no_veo(budget: Mapping[str, Any]) -> None:
    calls = int(budget.get("veo_reserved_calls") or 0)
    duration = float(budget.get("veo_reserved_duration_sec") or 0.0)
    if calls != 0 or abs(duration) > EPSILON:
        raise EchoSoundPassError(
            f"Echo V1 sound pass forbids Veo: calls={calls}, duration={duration}"
        )


def _assert_budget_unchanged(before: Mapping[str, Any], after: Mapping[str, Any]) -> None:
    _assert_no_veo(after)
    if _stable_digest(dict(before)) != _stable_digest(dict(after)):
        raise EchoSoundPassError("zero-cost sound pass changed the media budget")


def _project_handle(runner: Any) -> Any:
    project = getattr(getattr(runner, "agent", None), "project", None)
    if project is None:
        project = getattr(runner, "project", None)
    if project is None or not callable(getattr(project, "load", None)):
        raise EchoSoundPassError("runner does not expose its canonical project handle")
    return project


def _project_state(runner: Any) -> dict[str, Any]:
    state = _project_handle(runner).load()
    if not isinstance(state, dict):
        raise EchoSoundPassError("canonical project state is unreadable")
    return state


def _identity_timecode_snapshot(state: Mapping[str, Any]) -> list[dict[str, Any]]:
    """Capture immutable A1/A2 identity and timing, intentionally excluding effects."""

    result: list[dict[str, Any]] = []
    for clip in (state.get("timeline") or {}).get("clips") or []:
        if str(clip.get("track_id") or "") not in {"A1", "A2"}:
            continue
        result.append(
            {
                "id": str(clip.get("id") or ""),
                "asset_id": str(clip.get("asset_id") or ""),
                "track_id": str(clip.get("track_id") or ""),
                "start": float(clip.get("start") or 0.0),
                "duration": float(clip.get("duration") or 0.0),
                "source_in": float(clip.get("source_in") or 0.0),
                "source_out": float(clip.get("source_out") or 0.0),
                "enabled": bool(clip.get("enabled", True)),
            }
        )
    result.sort(key=lambda item: item["id"])
    music = [item for item in result if item["asset_id"] == rough_cut_builder.EXPECTED_MUSIC_ASSET]
    narration = [
        item
        for item in result
        if item["asset_id"] in rough_cut_builder.EXPECTED_NARRATION_ASSETS
    ]
    if len(result) != 14 or len(music) != 1 or len(narration) != 13:
        raise EchoSoundPassError("sound pass requires the original 1 music + 13 narration clips")
    if music[0]["track_id"] != "A1" or any(item["track_id"] != "A2" for item in narration):
        raise EchoSoundPassError("music/narration track identity changed before sound pass")
    if {item["asset_id"] for item in narration} != set(
        rough_cut_builder.EXPECTED_NARRATION_ASSETS
    ):
        raise EchoSoundPassError("the original narration asset set is incomplete")
    return result


def _find_music_clip(state: Mapping[str, Any]) -> dict[str, Any]:
    matches = [
        clip
        for clip in (state.get("timeline") or {}).get("clips") or []
        if str(clip.get("track_id") or "") == "A1"
        and str(clip.get("asset_id") or "") == rough_cut_builder.EXPECTED_MUSIC_ASSET
    ]
    if len(matches) != 1:
        raise EchoSoundPassError("sound pass cannot identify exactly one A1 music clip")
    return dict(matches[0])


def _stable_clip_presence(state: Mapping[str, Any]) -> set[str]:
    return {
        str(clip.get("id") or "")
        for clip in (state.get("timeline") or {}).get("clips") or []
        if str(clip.get("id") or "") in STABLE_CLIP_IDS
    }


def _assert_a3_precondition(state: Mapping[str, Any], *, completed: bool) -> None:
    tracks = {
        str(track.get("id") or ""): track
        for track in (state.get("timeline") or {}).get("tracks") or []
        if isinstance(track, Mapping)
    }
    a3 = tracks.get(SFX_TRACK_ID)
    if a3 is not None and str(a3.get("kind") or "") != "audio":
        raise EchoSoundPassError("A3 exists but is not an audio track")
    a3_clips = [
        clip
        for clip in (state.get("timeline") or {}).get("clips") or []
        if str(clip.get("track_id") or "") == SFX_TRACK_ID
    ]
    ids = {str(clip.get("id") or "") for clip in a3_clips}
    if completed:
        if ids != STABLE_CLIP_IDS or len(a3_clips) != len(PLACEMENTS):
            raise EchoSoundPassError("completed sound pass must own exactly eleven A3 clips")
    elif a3_clips:
        raise EchoSoundPassError("A3 contains non-sound-pass content; refusing to overwrite it")


def _assert_non_tmp(path: Path) -> None:
    resolved = path.expanduser().resolve()
    for root in (Path("/tmp"), Path("/private/tmp")):
        try:
            resolved.relative_to(root)
        except ValueError:
            continue
        raise EchoSoundPassError(f"production SFX cannot be stored under {root}: {resolved}")


def _sfx_output_dir(runner: Any) -> Path:
    raw_root = getattr(runner, "output_dir", None)
    if raw_root is None or not str(raw_root).strip():
        raise EchoSoundPassError("runner has no absolute project workdir")
    root = Path(raw_root).expanduser().resolve()
    if not root.is_absolute():
        raise EchoSoundPassError("runner has no absolute project workdir")
    _assert_non_tmp(root)
    destination = (root / "production-assets" / RUN_ID / "sound-pass-v1").resolve()
    try:
        destination.relative_to(root)
    except ValueError as exc:
        raise EchoSoundPassError("sound-pass output escaped the project workdir") from exc
    return destination


def _duration_from_probe(probe: Mapping[str, Any]) -> float:
    raw = (probe.get("format") or {}).get("duration")
    if raw is None:
        streams = probe.get("streams") or []
        raw = next(
            (
                stream.get("duration")
                for stream in streams
                if isinstance(stream, Mapping) and stream.get("codec_type") == "audio"
            ),
            None,
        )
    try:
        duration = float(raw or 0.0)
    except (TypeError, ValueError):
        duration = 0.0
    if duration <= 0.0:
        raise EchoSoundPassError("SFX probe has no positive duration")
    return duration


def _assert_audio_probe(probe: Mapping[str, Any], *, cue: str) -> float:
    streams = [item for item in (probe.get("streams") or []) if isinstance(item, Mapping)]
    audio = [item for item in streams if str(item.get("codec_type") or "") == "audio"]
    video = [item for item in streams if str(item.get("codec_type") or "") == "video"]
    if len(audio) != 1 or video:
        raise EchoSoundPassError(f"{cue} must contain one audio stream and no video")
    if str(audio[0].get("codec_name") or "") != "pcm_s16le":
        raise EchoSoundPassError(f"{cue} is not PCM s16le audio")
    if int(audio[0].get("sample_rate") or 0) != AUDIO_SAMPLE_RATE:
        raise EchoSoundPassError(f"{cue} is not {AUDIO_SAMPLE_RATE}Hz")
    if int(audio[0].get("channels") or 0) != 2:
        raise EchoSoundPassError(f"{cue} is not stereo")
    return _duration_from_probe(probe)


def _expected_source(cue: str, fingerprint: str) -> dict[str, Any]:
    return {
        "kind": "owned_audio",
        "provider": "local",
        "role": "sfx",
        "cue": cue,
        "cost_usd": 0.0,
        "generator": "echo_local_media",
        "production_run_id": RUN_ID,
        "production_stage": "sound_pass",
        "fingerprint": fingerprint,
    }


def _expected_license() -> dict[str, Any]:
    return {"basis": "project_created_programmatic_audio"}


def _record_matches_cue(record: Any, cue: str) -> bool:
    source = dict(getattr(record, "source", {}) or {})
    return (
        str(getattr(record, "kind", "")) == "audio"
        and str(source.get("kind") or "") == "owned_audio"
        and str(source.get("role") or "") == "sfx"
        and str(source.get("cue") or "") == cue
        and str(source.get("generator") or "") == "echo_local_media"
        and str(source.get("production_run_id") or "") == RUN_ID
        and str(source.get("production_stage") or "") == "sound_pass"
    )


def _fact_from_record(
    record: Any,
    *,
    cue: str,
    probe_fn: Callable[[str | Path], dict[str, Any]],
) -> SfxAssetFact:
    path = Path(getattr(record, "path", "")).expanduser().resolve()
    _assert_non_tmp(path)
    if not path.is_file() or path.stat().st_size <= 0:
        raise EchoSoundPassError(f"registered SFX is missing or empty: {cue}")
    actual_hash = _sha256_file(path)
    registered_hash = str(getattr(record, "sha256", "") or "")
    if not registered_hash or registered_hash != actual_hash:
        raise EchoSoundPassError(f"registered SFX hash mismatch: {cue}")
    source = dict(getattr(record, "source", {}) or {})
    license_data = dict(getattr(record, "license", {}) or {})
    fingerprint = str(source.get("fingerprint") or "")
    if source != _expected_source(cue, fingerprint) or not fingerprint:
        raise EchoSoundPassError(f"registered SFX source provenance is incomplete: {cue}")
    if license_data != _expected_license():
        raise EchoSoundPassError(f"registered SFX license is incomplete: {cue}")
    duration = _assert_audio_probe(probe_fn(path), cue=cue)
    return SfxAssetFact(
        cue=cue,
        asset_id=str(getattr(record, "asset_id", "") or ""),
        path=path,
        sha256=actual_hash,
        duration_sec=duration,
        fingerprint=fingerprint,
        source=source,
        license=license_data,
    )


def _existing_sfx_facts(
    registry: Any,
    *,
    probe_fn: Callable[[str | Path], dict[str, Any]],
    require_all: bool,
) -> dict[str, SfxAssetFact]:
    records = list(registry.list_records())
    result: dict[str, SfxAssetFact] = {}
    for cue in SFX_CUES:
        matches = [record for record in records if _record_matches_cue(record, cue)]
        if len(matches) > 1:
            raise EchoSoundPassError(f"multiple registered sound-pass assets found for {cue}")
        if matches:
            result[cue] = _fact_from_record(matches[0], cue=cue, probe_fn=probe_fn)
        elif require_all:
            raise EchoSoundPassError(f"completed sound pass is missing registered asset: {cue}")
    return result


def _assert_facts_in_output_dir(runner: Any, facts: Mapping[str, SfxAssetFact]) -> None:
    expected = _sfx_output_dir(runner)
    for cue, fact in facts.items():
        try:
            fact.path.relative_to(expected)
        except ValueError as exc:
            raise EchoSoundPassError(
                f"registered SFX is outside this project's sound-pass directory: {cue}"
            ) from exc


def ensure_sfx_assets(
    runner: Any,
    *,
    synthesize_fn: Callable[[str | Path], dict[str, dict[str, Any]]] = synthesize_sfx,
    probe_fn: Callable[[str | Path], dict[str, Any]] = probe_media,
) -> dict[str, SfxAssetFact]:
    """Render/cache four cues and resolve their real registry-assigned ids."""

    registry = runner.agent.registry
    existing = _existing_sfx_facts(registry, probe_fn=probe_fn, require_all=False)
    _assert_facts_in_output_dir(runner, existing)
    if len(existing) == len(SFX_CUES):
        return existing

    output_dir = _sfx_output_dir(runner)
    outputs = synthesize_fn(output_dir)
    if set(outputs) != set(SFX_CUES):
        raise EchoSoundPassError("local SFX synthesis must return exactly four named cues")

    for cue in SFX_CUES:
        if cue in existing:
            continue
        output = outputs[cue]
        if not isinstance(output, Mapping):
            raise EchoSoundPassError(f"local SFX output is invalid: {cue}")
        path = Path(str(output.get("path") or "")).expanduser().resolve()
        try:
            path.relative_to(output_dir.resolve())
        except ValueError as exc:
            raise EchoSoundPassError(f"local SFX escaped its project directory: {cue}") from exc
        _assert_non_tmp(path)
        if not path.is_file() or path.stat().st_size <= 0:
            raise EchoSoundPassError(f"local SFX bytes are missing: {cue}")
        fingerprint = str(output.get("fingerprint") or "")
        if not fingerprint:
            raise EchoSoundPassError(f"local SFX has no deterministic fingerprint: {cue}")
        duration = _assert_audio_probe(dict(output.get("probe") or probe_fn(path)), cue=cue)
        source = _expected_source(cue, fingerprint)
        license_data = _expected_license()
        asset_id = registry.allocate_id("audio")
        record = registry.register_output(
            asset_id,
            kind="audio",
            path=path,
            summary=f"Echo Protocol local SFX: {cue}",
            lineage=(),
            source=source,
            license=license_data,
        )
        # Re-read from the canonical registry record; no caller may infer an id
        # from a counter or filename.
        fact = _fact_from_record(record, cue=cue, probe_fn=probe_fn)
        if abs(fact.duration_sec - duration) > 0.04:
            raise EchoSoundPassError(f"registered SFX duration changed: {cue}")
        existing[cue] = fact

    if set(existing) != set(SFX_CUES):
        raise EchoSoundPassError("could not resolve all four registered SFX assets")
    _assert_facts_in_output_dir(runner, existing)
    return existing


def _project_asset(fact: SfxAssetFact) -> dict[str, Any]:
    return {
        "id": fact.asset_id,
        "asset_id": fact.asset_id,
        "name": fact.path.name,
        "media_kind": "audio",
        "mime_type": mimetypes.guess_type(fact.path.name)[0] or "audio/wav",
        "source_path": str(fact.path),
        "duration": fact.duration_sec,
        "metadata": {
            "sha256": fact.sha256,
            "duration": fact.duration_sec,
            "sample_rate": AUDIO_SAMPLE_RATE,
            "channels": 2,
            "role": "sfx",
            "cue": fact.cue,
            "source": dict(fact.source),
            "license": dict(fact.license),
            "production_run_id": RUN_ID,
        },
    }


def build_sound_pass_ops(
    current_state: Mapping[str, Any], facts: Mapping[str, SfxAssetFact]
) -> tuple[list[dict[str, Any]], str, str]:
    if set(facts) != set(SFX_CUES):
        raise EchoSoundPassError("sound-pass patch requires all four SFX assets")
    digest = _stable_digest(
        {
            "version": SOUND_PASS_VERSION,
            "assets": {
                cue: {
                    "asset_id": facts[cue].asset_id,
                    "sha256": facts[cue].sha256,
                    "duration_sec": facts[cue].duration_sec,
                    "fingerprint": facts[cue].fingerprint,
                }
                for cue in SFX_CUES
            },
            "placements": [item.__dict__ for item in PLACEMENTS],
        }
    )
    trace_id = f"trace-echo-sound-pass-{digest[:20]}"
    tracks = {
        str(track.get("id") or ""): track
        for track in (current_state.get("timeline") or {}).get("tracks") or []
        if isinstance(track, Mapping)
    }
    ops: list[dict[str, Any]] = []
    if SFX_TRACK_ID not in tracks:
        ops.append(
            {
                "op": "add_track",
                "kind": "audio",
                "track_id": SFX_TRACK_ID,
                "name": "Sound Design",
            }
        )
    for cue in SFX_CUES:
        ops.append({"op": "upsert_asset", "asset": _project_asset(facts[cue])})
    ops.append({"op": "set_track", "track_id": "A1", "duck_under": "A2"})
    music = _find_music_clip(current_state)
    ops.append(
        {
            "op": "set_clip_effects",
            "clip_id": str(music.get("id") or ""),
            "effects": {"gain_db": -8.0, "fade_in": 1.0, "fade_out": 2.5},
            "provenance": {
                "source": "echo_protocol_v1_sound_pass",
                "run_id": RUN_ID,
                "trace_id": trace_id,
            },
        }
    )
    for placement in PLACEMENTS:
        fact = facts[placement.cue]
        if placement.start_sec + fact.duration_sec > 120.0 + EPSILON:
            raise EchoSoundPassError(f"SFX placement exceeds delivery duration: {placement.clip_id}")
        clip = {
            "id": placement.clip_id,
            "asset_id": fact.asset_id,
            "track_id": SFX_TRACK_ID,
            "name": fact.path.name,
            "media_kind": "audio",
            "duration": fact.duration_sec,
            "source_in": 0.0,
            "source_out": fact.duration_sec,
            "enabled": True,
        }
        ops.append(
            {
                "op": "insert_clip",
                "track_id": SFX_TRACK_ID,
                "at": {"time": placement.start_sec},
                "ripple": False,
                "data": {"clip": clip},
                "provenance": {
                    "source": "echo_protocol_v1_sound_pass",
                    "run_id": RUN_ID,
                    "trace_id": trace_id,
                    "sound_pass_version": SOUND_PASS_VERSION,
                    "cue": placement.cue,
                    "placement_index": placement.index,
                    "fingerprint": fact.fingerprint,
                },
            }
        )
    return ops, trace_id, f"{trace_id}:{SOUND_PASS_VERSION}"


def validate_sound_pass_state(
    state: Mapping[str, Any],
    facts: Mapping[str, SfxAssetFact],
    *,
    original_audio: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    if original_audio is not None and _identity_timecode_snapshot(state) != original_audio:
        raise EchoSoundPassError("sound pass changed original A1/A2 identity or timecodes")
    else:
        _identity_timecode_snapshot(state)

    tracks = {
        str(track.get("id") or ""): track
        for track in (state.get("timeline") or {}).get("tracks") or []
        if isinstance(track, Mapping)
    }
    if str((tracks.get(SFX_TRACK_ID) or {}).get("kind") or "") != "audio":
        raise EchoSoundPassError("sound pass did not persist an A3 audio track")
    if str((tracks.get("A1") or {}).get("duck_under") or "") != "A2":
        raise EchoSoundPassError("music track is not configured to duck under narration")

    music = _find_music_clip(state)
    music_effects = dict(music.get("effects") or {})
    expected_music = {"gain_db": -8.0, "fade_in": 1.0, "fade_out": 2.5}
    if any(abs(float(music_effects.get(key) or 0.0) - value) > EPSILON for key, value in expected_music.items()):
        raise EchoSoundPassError("music gain/fades do not match the sound-pass mix")

    clips = list((state.get("timeline") or {}).get("clips") or [])
    sfx = [clip for clip in clips if str(clip.get("id") or "") in STABLE_CLIP_IDS]
    if len(sfx) != len(PLACEMENTS):
        raise EchoSoundPassError("sound pass does not contain all eleven stable SFX clips")
    by_id = {str(clip.get("id") or ""): clip for clip in sfx}
    previous_end = 0.0
    for placement in PLACEMENTS:
        clip = by_id.get(placement.clip_id)
        fact = facts[placement.cue]
        if clip is None:
            raise EchoSoundPassError(f"missing SFX clip: {placement.clip_id}")
        if str(clip.get("track_id") or "") != SFX_TRACK_ID:
            raise EchoSoundPassError(f"SFX clip is not on A3: {placement.clip_id}")
        if str(clip.get("asset_id") or "") != fact.asset_id:
            raise EchoSoundPassError(f"SFX asset identity changed: {placement.clip_id}")
        start = float(clip.get("start") or 0.0)
        duration = float(clip.get("duration") or 0.0)
        source_in = float(clip.get("source_in") or 0.0)
        source_out = float(clip.get("source_out") or 0.0)
        if abs(start - placement.start_sec) > EPSILON:
            raise EchoSoundPassError(f"SFX timecode changed: {placement.clip_id}")
        if abs(duration - fact.duration_sec) > EPSILON or abs(source_in) > EPSILON:
            raise EchoSoundPassError(f"SFX source range changed: {placement.clip_id}")
        if abs(source_out - fact.duration_sec) > EPSILON:
            raise EchoSoundPassError(f"SFX source-out changed: {placement.clip_id}")
        if start + EPSILON < previous_end:
            raise EchoSoundPassError(f"SFX clips overlap before {placement.clip_id}")
        previous_end = start + duration
        provenance = dict(clip.get("provenance") or {})
        if (
            str(provenance.get("run_id") or "") != RUN_ID
            or str(provenance.get("cue") or "") != placement.cue
            or str(provenance.get("fingerprint") or "") != fact.fingerprint
            or not str(provenance.get("trace_id") or "")
        ):
            raise EchoSoundPassError(f"SFX provenance is incomplete: {placement.clip_id}")

    project_assets = {
        str(asset.get("id") or asset.get("asset_id") or ""): asset
        for asset in state.get("assets") or []
        if isinstance(asset, Mapping)
    }
    for cue, fact in facts.items():
        asset = project_assets.get(fact.asset_id)
        metadata = dict((asset or {}).get("metadata") or {})
        source = dict(metadata.get("source") or {})
        if (
            str((asset or {}).get("media_kind") or "") != "audio"
            or str(metadata.get("role") or "") != "sfx"
            or str(source.get("kind") or "") != "owned_audio"
            or str(source.get("provider") or "") != "local"
            or str(source.get("cue") or "") != cue
        ):
            raise EchoSoundPassError(f"project SFX source role is incomplete: {cue}")

    dropped = [
        {"clip_id": str(clip.get("id") or ""), **item}
        for clip in clips
        for item in clip_dropped_fields(clip)
    ]
    if dropped:
        raise EchoSoundPassError(f"sound-pass dry run has dropped fields: {dropped}")
    duration = float((state.get("timeline") or {}).get("duration") or 0.0)
    if abs(duration - 120.0) > EPSILON:
        raise EchoSoundPassError(f"sound pass changed the 120-second timeline: {duration}")
    return {
        "sfx_asset_count": len(facts),
        "sfx_clip_count": len(sfx),
        "sfx_track_id": SFX_TRACK_ID,
        "original_audio_clip_ids": [item["id"] for item in _identity_timecode_snapshot(state)],
        "dropped_fields": [],
        "duration_sec": duration,
    }


def dry_run_sound_pass_patch(
    current_state: Mapping[str, Any],
    ops: list[dict[str, Any]],
    facts: Mapping[str, SfxAssetFact],
) -> dict[str, Any]:
    original = _identity_timecode_snapshot(current_state)
    dry_state = apply_timeline_patches(dict(current_state), [{"version": 1, "ops": ops}])
    validate_sound_pass_state(dry_state, facts, original_audio=original)
    return dry_state


def _facts_for_persisted_clips(
    runner: Any,
    state: Mapping[str, Any],
    *,
    probe_fn: Callable[[str | Path], dict[str, Any]],
) -> dict[str, SfxAssetFact]:
    facts = _existing_sfx_facts(
        runner.agent.registry,
        probe_fn=probe_fn,
        require_all=True,
    )
    _assert_facts_in_output_dir(runner, facts)
    by_id = {
        str(clip.get("id") or ""): clip
        for clip in (state.get("timeline") or {}).get("clips") or []
        if str(clip.get("id") or "") in STABLE_CLIP_IDS
    }
    for placement in PLACEMENTS:
        clip = by_id.get(placement.clip_id)
        if clip is None or str(clip.get("asset_id") or "") != facts[placement.cue].asset_id:
            raise EchoSoundPassError(f"persisted SFX registry binding changed: {placement.clip_id}")
    return facts


def execute_sound_pass(
    manager: Any,
    runner: Any,
    board: tuple[rough_cut_builder.BoardUnit, ...],
    *,
    synthesize_fn: Callable[[str | Path], dict[str, dict[str, Any]]] = synthesize_sfx,
    probe_fn: Callable[[str | Path], dict[str, Any]] = probe_media,
) -> dict[str, Any]:
    """Execute or reconcile the formal sound-pass stage."""

    run = manager.get_run(runner.project_id, runner.run_id)
    state_name = str(run.get("production_state") or run.get("state") or "")
    if state_name not in {"sound_pass", "visual_pass"}:
        raise EchoSoundPassError(
            f"sound-pass operator requires sound_pass/visual_pass state, got {state_name}"
        )
    budget_before = _budget(run)
    _assert_no_veo(budget_before)

    # The sound operator cannot repair or reinterpret a different rough cut.
    rough_cut_builder.validate_persisted_board(runner, board)
    project = _project_handle(runner)
    current_state = project.load()
    original_audio = _identity_timecode_snapshot(current_state)
    present = _stable_clip_presence(current_state)
    if present and present != STABLE_CLIP_IDS:
        raise EchoSoundPassError(
            "partial deterministic sound pass detected; refusing blind rebuild: "
            f"{len(present)}/{len(STABLE_CLIP_IDS)} stable clips"
        )
    _assert_a3_precondition(current_state, completed=present == STABLE_CLIP_IDS)

    if state_name == "visual_pass":
        if present != STABLE_CLIP_IDS:
            raise EchoSoundPassError("visual_pass state has no complete persisted sound pass")
        facts = _facts_for_persisted_clips(runner, current_state, probe_fn=probe_fn)
        checks = validate_sound_pass_state(current_state, facts, original_audio=original_audio)
        budget_after = _budget(manager.get_run(runner.project_id, runner.run_id))
        _assert_budget_unchanged(budget_before, budget_after)
        return {
            "ok": True,
            "replayed": True,
            "production_state": "visual_pass",
            "project_revision": int(runner.project_revision),
            "patch_applied": False,
            **checks,
            "asset_ids": {cue: facts[cue].asset_id for cue in SFX_CUES},
            "budget": budget_after,
            "veo_calls": 0,
        }

    meta_before = project.store.load_meta(project.project_id)
    patch_seq_before = int(meta_before.get("patch_seq") or 0)
    project_revision_before = int(runner.project_revision)
    patch_applied = False
    if present == STABLE_CLIP_IDS:
        # Recovery window: project patch committed, evidence/state transition did not.
        facts = _facts_for_persisted_clips(runner, current_state, probe_fn=probe_fn)
        checks = validate_sound_pass_state(current_state, facts, original_audio=original_audio)
        patch_seq_after = patch_seq_before
        project_revision_after = project_revision_before
        patch_trace = str(
            next(
                clip.get("provenance", {}).get("trace_id")
                for clip in (current_state.get("timeline") or {}).get("clips") or []
                if str(clip.get("id") or "") == PLACEMENTS[0].clip_id
            )
        )
    else:
        facts = ensure_sfx_assets(
            runner,
            synthesize_fn=synthesize_fn,
            probe_fn=probe_fn,
        )
        # Registration is durable but zero-dollar.  Refuse before the project
        # patch if anything touched the run budget or project revision.
        prepatch_run = manager.get_run(runner.project_id, runner.run_id)
        _assert_budget_unchanged(budget_before, _budget(prepatch_run))
        if int(runner.project_revision) != project_revision_before:
            raise EchoSoundPassError("SFX registration unexpectedly changed project revision")
        if int(project.store.load_meta(project.project_id).get("patch_seq") or 0) != patch_seq_before:
            raise EchoSoundPassError("SFX registration unexpectedly changed project patch sequence")

        ops, patch_trace, patch_label = build_sound_pass_ops(current_state, facts)
        dry_run_sound_pass_patch(current_state, ops, facts)
        runner.run_project_edit(
            lambda: project.apply_ops(ops, label=patch_label),
            expected_project_revision=project_revision_before,
            timeout=300,
        )
        patch_applied = True
        patch_seq_after = int(project.store.load_meta(project.project_id).get("patch_seq") or 0)
        project_revision_after = int(runner.project_revision)
        if patch_seq_after != patch_seq_before + 1:
            raise EchoSoundPassError(
                f"sound pass must commit one patch, seq {patch_seq_before} -> {patch_seq_after}"
            )
        if project_revision_after != project_revision_before + 1:
            raise EchoSoundPassError(
                "sound pass must advance project revision exactly once: "
                f"{project_revision_before} -> {project_revision_after}"
            )
        persisted_state = project.load()
        checks = validate_sound_pass_state(
            persisted_state,
            facts,
            original_audio=original_audio,
        )
        rough_cut_builder.validate_persisted_board(runner, board)

    budget_after_patch = _budget(manager.get_run(runner.project_id, runner.run_id))
    _assert_budget_unchanged(budget_before, budget_after_patch)
    asset_checks = [
        {
            "cue": cue,
            "asset_id": facts[cue].asset_id,
            "path": str(facts[cue].path),
            "sha256": facts[cue].sha256,
            "duration_sec": facts[cue].duration_sec,
            "fingerprint": facts[cue].fingerprint,
            "source": dict(facts[cue].source),
            "license": dict(facts[cue].license),
        }
        for cue in SFX_CUES
    ]
    placement_receipt = [
        {
            "clip_id": placement.clip_id,
            "cue": placement.cue,
            "asset_id": facts[placement.cue].asset_id,
            "start_sec": placement.start_sec,
            "duration_sec": facts[placement.cue].duration_sec,
        }
        for placement in PLACEMENTS
    ]
    evidence_id = "ev-echo-sound-pass-v1"
    evidence_trace = "trace-ev-echo-sound-pass-v1"
    manager.record_evidence(
        runner.project_id,
        runner.run_id,
        evidence_id=evidence_id,
        kind="sound_pass",
        project_revision=int(runner.project_revision),
        trace_id=evidence_trace,
        payload={
            "sound_pass_version": SOUND_PASS_VERSION,
            "patch_trace_id": patch_trace,
            "patch_seq": patch_seq_after,
            "assets": asset_checks,
            "placements": placement_receipt,
            "original_audio_identity_timecodes": original_audio,
            "checks": {
                **checks,
                "single_atomic_patch": True,
                "budget_unchanged": True,
                "ai_video_generation_calls": 0,
                "music_ducks_under_narration": True,
                "source_role_sfx_complete": True,
            },
            "budget_before": budget_before,
            "budget_after": budget_after_patch,
        },
    )
    latest = manager.get_run(runner.project_id, runner.run_id)
    transition = manager.transition_run(
        runner.project_id,
        runner.run_id,
        "visual_pass",
        expected_revision=int(latest.get("production_revision") or latest.get("revision") or 0),
        trace_id="trace-echo-sound-pass-v1-complete",
    )
    final_budget = _budget(manager.get_run(runner.project_id, runner.run_id))
    _assert_budget_unchanged(budget_before, final_budget)
    return {
        "ok": True,
        "replayed": False,
        "production_state": str(
            transition.get("production_state") or transition.get("state") or "visual_pass"
        ),
        "project_revision": int(runner.project_revision),
        "patch_applied": patch_applied,
        "patch_seq": patch_seq_after,
        "patch_trace_id": patch_trace,
        "evidence_id": evidence_id,
        **checks,
        "asset_ids": {cue: facts[cue].asset_id for cue in SFX_CUES},
        "budget": final_budget,
        "veo_calls": 0,
    }


def sound_pass(output_root: Path) -> dict[str, Any]:
    manager = SessionManager(
        output_root=output_root,
        max_sessions=2,
        idle_timeout_sec=0,
        sweep_interval_sec=0,
    )
    runner = manager.resume_session(SESSION_ID)
    try:
        if str(runner.run_id) != RUN_ID:
            raise EchoSoundPassError(f"unexpected production run: {runner.run_id}")
        source_assets, _source_review = rough_cut_builder._load_reviewed_sources(runner)
        board = rough_cut_builder.build_board(source_assets)
        return execute_sound_pass(manager, runner, board)
    finally:
        manager.close_all()


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--output-root",
        type=Path,
        default=Path.home() / ".gemia" / "v3",
    )
    args = parser.parse_args()
    result = sound_pass(args.output_root.expanduser().resolve())
    print(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
