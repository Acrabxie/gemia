"""Local, persistent rough-cut preparation for media-library assets.

This module deliberately stops before timeline editing.  It turns imported
video/audio into creator-reviewable evidence: a word-timed transcript,
pause/filler cleanup suggestions, take ranking, and an optional low-resolution
proxy.  Every asset is checkpointed independently so an interrupted long batch
can be resumed without redoing completed work.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import subprocess
import threading
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from gemia.media_annotations import create_annotation, upsert_annotations
from gemia.media_library import asset_cache_root, get_asset, list_assets, media_root
from gemia.video.proxy import ProxyManager

ProgressCallback = Callable[[dict[str, Any]], None]

_MANIFEST_VERSION = 2
_ACTIVE_JOBS: dict[str, threading.Thread] = {}
_ACTIVE_JOBS_LOCK = threading.Lock()
_FILLERS = {
    "ah", "eh", "er", "erm", "hmm", "hm", "like", "uh", "uhh", "um", "umm",
    "呃", "嗯", "额", "啊", "这个", "那个", "就是", "然后",
}


class RoughcutError(RuntimeError):
    """Raised when local rough-cut preparation cannot complete."""


def prepare_roughcut(
    account_id: str,
    asset_ids: list[str] | None = None,
    *,
    all_assets: bool = False,
    language: str = "auto",
    create_proxies: bool = True,
    proxy_resolution: int = 540,
    resume: bool = True,
    max_assets: int = 100,
    progress: ProgressCallback | None = None,
) -> dict[str, Any]:
    """Prepare transcript/cleanup/take evidence without changing a timeline."""
    clean_ids = [str(item) for item in (asset_ids or []) if str(item).strip()]
    if not clean_ids and all_assets:
        clean_ids = [
            str(asset.get("asset_id") or "")
            for asset in list_assets(account_id, limit=max_assets)
            if asset.get("media_kind") in {"video", "audio"}
        ]
    clean_ids = clean_ids[: max(1, min(int(max_assets or 100), 100))]
    if not clean_ids:
        raise RoughcutError("prepare_roughcut requires video/audio asset_ids or all=true")

    results: list[dict[str, Any]] = []
    total = len(clean_ids)
    for index, asset_id in enumerate(clean_ids, start=1):
        _emit(progress, (index - 1) * 100 / total, f"preparing {asset_id}", asset_id)
        asset = get_asset(account_id, asset_id)
        if not asset:
            results.append({"asset_id": asset_id, "status": "error", "error": "media asset not found"})
            continue
        if asset.get("media_kind") not in {"video", "audio"}:
            results.append({"asset_id": asset_id, "status": "skipped", "error": "video or audio required"})
            continue
        try:
            results.append(
                _prepare_asset(
                    account_id,
                    asset,
                    language=language,
                    create_proxy=create_proxies,
                    proxy_resolution=proxy_resolution,
                    resume=resume,
                )
            )
        except Exception as exc:
            results.append({"asset_id": asset_id, "status": "error", "error": str(exc)})

    ready = [item for item in results if item.get("status") == "ready"]
    _rank_takes(account_id, ready)
    _emit(progress, 100.0, f"prepared {len(ready)} of {len(results)} asset(s)", None)
    return {
        "status": "ready" if len(ready) == len(results) else "partial",
        "asset_count": len(results),
        "ready_count": len(ready),
        "error_count": sum(item.get("status") == "error" for item in results),
        "results": results,
        "summary": f"prepared {len(ready)} of {len(results)} media-library asset(s)",
    }


def load_roughcut(account_id: str, asset_id: str) -> dict[str, Any]:
    path = _manifest_path(account_id, asset_id)
    if not path.exists():
        raise RoughcutError("rough-cut preparation not found")
    return _read_json(path)


def apply_roughcut_review(account_id: str, asset_id: str, payload: dict[str, Any]) -> dict[str, Any]:
    """Persist one human correction/decision and keep machine evidence intact."""
    manifest = load_roughcut(account_id, asset_id)
    target_type = str(payload.get("target_type") or "").strip()
    target_id = str(payload.get("target_id") or "").strip()
    action = str(payload.get("action") or "").strip().lower()
    note = ""
    start_sec: float | None = None
    end_sec: float | None = None

    if target_type == "cleanup":
        if action not in {"accept", "reject"}:
            raise RoughcutError("cleanup review action must be accept or reject")
        target = _find_by_id(manifest.get("cleanup_suggestions"), target_id)
        target["review_status"] = "accepted" if action == "accept" else "rejected"
        note = f"{target.get('kind', 'cleanup')} suggestion {target['review_status']}"
        start_sec, end_sec = float(target.get("start_sec") or 0), float(target.get("end_sec") or 0)
    elif target_type == "transcript":
        target = _find_by_id(manifest.get("transcript", {}).get("segments"), target_id)
        corrected = str(payload.get("text") or "").strip()
        if action != "correct" or not corrected:
            raise RoughcutError("transcript review requires action=correct and non-empty text")
        target["corrected_text"] = corrected[:5000]
        target["review_status"] = "corrected"
        note = f"transcript corrected to: {corrected[:240]}"
        start_sec, end_sec = float(target.get("start_sec") or 0), float(target.get("end_sec") or 0)
    elif target_type == "take":
        if action not in {"select", "alternative", "reject"}:
            raise RoughcutError("take review action must be select, alternative, or reject")
        manifest.setdefault("take", {})["user_decision"] = action
        manifest["take"]["selected"] = action == "select"
        note = f"take marked {action}"
    else:
        raise RoughcutError("target_type must be cleanup, transcript, or take")

    manifest["updated_at"] = _utc_now()
    _atomic_json(_manifest_path(account_id, asset_id), manifest)
    annotation_payload: dict[str, Any] = {
        "scope": "time_range" if start_sec is not None else "asset",
        "label": "Rough-cut review",
        "note": note,
        "tags": ["roughcut", "review", action],
        "category": "roughcut_review",
        "source": "user",
        "metadata": {"target_type": target_type, "target_id": target_id, "action": action},
    }
    if start_sec is not None:
        annotation_payload.update({"start_sec": start_sec, "end_sec": end_sec})
    annotation = create_annotation(account_id, asset_id, annotation_payload)
    return {"manifest": manifest, "annotation": annotation, "summary": note}


def start_prepare_job(account_id: str, payload: dict[str, Any]) -> dict[str, Any]:
    job_id = f"roughcut_{uuid.uuid4().hex[:12]}"
    args = _job_args(payload)
    record = {
        "job_id": job_id,
        "status": "queued",
        "progress": 0.0,
        "message": "queued",
        "recoverable": True,
        "args": args,
        "created_at": _utc_now(),
        "updated_at": _utc_now(),
    }
    _write_job(account_id, record)

    def _worker() -> None:
        record.update({"status": "running", "message": "starting", "updated_at": _utc_now()})
        _write_job(account_id, record)

        def _progress(update: dict[str, Any]) -> None:
            record.update(
                {
                    "progress": float(update.get("percent") or 0.0),
                    "message": str(update.get("message") or "working"),
                    "current_asset_id": update.get("asset_id"),
                    "updated_at": _utc_now(),
                }
            )
            _write_job(account_id, record)

        try:
            result = prepare_roughcut(account_id, progress=_progress, **args)
            record.update({"status": result["status"], "progress": 100.0, "result": result, "message": result["summary"]})
        except Exception as exc:
            record.update({"status": "error", "error": str(exc), "message": str(exc)})
        record["updated_at"] = _utc_now()
        _write_job(account_id, record)

    thread = threading.Thread(target=_worker, name=job_id, daemon=True)
    with _ACTIVE_JOBS_LOCK:
        _ACTIVE_JOBS[job_id] = thread
    thread.start()
    return record


def get_prepare_job(account_id: str, job_id: str) -> dict[str, Any]:
    record = _read_json(_job_path(account_id, job_id))
    if record.get("status") in {"queued", "running"}:
        with _ACTIVE_JOBS_LOCK:
            thread = _ACTIVE_JOBS.get(job_id)
        if thread is None or not thread.is_alive():
            record.update(
                {
                    "status": "interrupted",
                    "message": "server stopped before the batch finished; resume is available",
                    "recoverable": True,
                    "updated_at": _utc_now(),
                }
            )
            _write_job(account_id, record)
    return record


def resume_prepare_job(account_id: str, job_id: str) -> dict[str, Any]:
    previous = get_prepare_job(account_id, job_id)
    payload = dict(previous.get("args") or {})
    payload["resume"] = True
    resumed = start_prepare_job(account_id, payload)
    resumed["resumed_from"] = job_id
    _write_job(account_id, resumed)
    return resumed


def _prepare_asset(
    account_id: str,
    asset: dict[str, Any],
    *,
    language: str,
    create_proxy: bool,
    proxy_resolution: int,
    resume: bool,
) -> dict[str, Any]:
    asset_id = str(asset["asset_id"])
    cache_dir = asset_cache_root(account_id, asset_id)
    cache_dir.mkdir(parents=True, exist_ok=True)
    signature = _signature(asset, language, create_proxy, proxy_resolution)
    manifest_path = _manifest_path(account_id, asset_id)
    if resume and manifest_path.exists():
        previous = _read_json(manifest_path)
        if previous.get("status") == "ready" and previous.get("signature") == signature:
            return _result_summary(previous, reused=True)

    partial = {
        "version": _MANIFEST_VERSION,
        "asset_id": asset_id,
        "name": asset.get("name"),
        "status": "processing",
        "signature": signature,
        "created_at": _utc_now(),
        "updated_at": _utc_now(),
    }
    _atomic_json(manifest_path, partial)
    source = Path(str(asset.get("source_path") or asset.get("storage_path") or ""))
    if not source.exists():
        raise RoughcutError(f"media source missing: {source}")

    audio_path = cache_dir / "roughcut-audio.wav"
    if not audio_path.exists() or audio_path.stat().st_mtime < source.stat().st_mtime:
        _run([
            "ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-i", str(source),
            "-vn", "-ac", "1", "-ar", "16000", "-c:a", "pcm_s16le", str(audio_path),
        ], "audio extraction")

    whisper_json = cache_dir / "roughcut-whisper.json"
    if not whisper_json.exists() or whisper_json.stat().st_mtime < audio_path.stat().st_mtime:
        whisper = _whisper_binary()
        model = _whisper_model()
        output_base = cache_dir / "roughcut-whisper"
        cmd = [whisper, "-m", str(model), "-f", str(audio_path), "-ojf", "-of", str(output_base), "-np"]
        cmd.extend(["-l", language if language and language != "auto" else "auto"])
        _run(cmd, "speech transcription", timeout=int(os.environ.get("LUMERI_WHISPER_TIMEOUT_SEC") or 7200))
    whisper_payload = _read_json(whisper_json)
    transcript = _parse_whisper(whisper_payload)
    duration = max(float(asset.get("duration") or 0.0), transcript.get("duration_sec") or 0.0)
    silences = _detect_silences(source, duration)
    cleanup = _cleanup_suggestions(transcript, silences)

    proxy: dict[str, Any] | None = None
    if create_proxy and asset.get("media_kind") == "video":
        proxy_asset = ProxyManager(cache_dir).ensure_proxy(source, resolution=proxy_resolution)
        proxy = {
            "status": "ready",
            "resolution": proxy_asset.resolution,
            "path": proxy_asset.proxy_path,
            "src": f"/media-library/file/{asset_id}/cache/{Path(proxy_asset.proxy_path).name}",
        }

    manifest = {
        **partial,
        "status": "ready",
        "language": transcript.get("language") or language,
        "duration_sec": round(duration, 3),
        "transcript": transcript,
        "silences": silences,
        "cleanup_suggestions": cleanup,
        "proxy": proxy,
        "score": _take_score(asset, transcript, silences, cleanup, duration),
        "take": {"group": "", "rank": 1, "selected": True, "alternatives": []},
        "updated_at": _utc_now(),
    }
    _atomic_json(manifest_path, manifest)
    _write_roughcut_annotations(account_id, manifest)
    return _result_summary(manifest, reused=False)


def _parse_whisper(payload: dict[str, Any]) -> dict[str, Any]:
    language = str(payload.get("result", {}).get("language") or payload.get("params", {}).get("language") or "auto")
    segments: list[dict[str, Any]] = []
    words: list[dict[str, Any]] = []
    for index, raw in enumerate(payload.get("transcription") or [], start=1):
        offsets = raw.get("offsets") or {}
        start = max(float(offsets.get("from") or 0) / 1000.0, 0.0)
        end = max(float(offsets.get("to") or 0) / 1000.0, start)
        segment_words: list[dict[str, Any]] = []
        probabilities: list[float] = []
        for token in raw.get("tokens") or []:
            text = str(token.get("text") or "")
            if not text or text.startswith("[_"):
                continue
            token_offsets = token.get("offsets") or {}
            token_start = max(float(token_offsets.get("from") or 0) / 1000.0, 0.0)
            token_end = max(float(token_offsets.get("to") or 0) / 1000.0, token_start)
            probability = max(0.0, min(float(token.get("p") or 0.0), 1.0))
            word = {"text": text, "start_sec": round(token_start, 3), "end_sec": round(token_end, 3), "confidence": round(probability, 4)}
            segment_words.append(word)
            words.append(word)
            probabilities.append(probability)
        segment = {
            "id": f"segment-{index}",
            "start_sec": round(start, 3),
            "end_sec": round(end, 3),
            "text": str(raw.get("text") or "").strip(),
            "confidence": round(sum(probabilities) / len(probabilities), 4) if probabilities else None,
            "words": segment_words,
            "review_status": "pending",
        }
        segments.append(segment)
    full_text = " ".join(item["text"] for item in segments if item["text"]).strip()
    duration = max((item["end_sec"] for item in segments), default=0.0)
    confidence_values = [float(item["confidence"]) for item in words if item.get("confidence") is not None]
    return {
        "language": language,
        "text": full_text,
        "duration_sec": round(duration, 3),
        "confidence": round(sum(confidence_values) / len(confidence_values), 4) if confidence_values else None,
        "segments": segments,
        "words": words,
    }


def _detect_silences(source: Path, duration: float) -> list[dict[str, Any]]:
    proc = subprocess.run(
        ["ffmpeg", "-hide_banner", "-i", str(source), "-af", "silencedetect=noise=-35dB:d=0.45", "-f", "null", "-"],
        capture_output=True,
        text=True,
        timeout=int(os.environ.get("LUMERI_FFMPEG_TIMEOUT_SEC") or 1800),
        check=False,
    )
    if proc.returncode != 0:
        raise RoughcutError(f"silence detection failed: {(proc.stderr or proc.stdout)[-600:]}")
    starts = [float(value) for value in re.findall(r"silence_start:\s*([0-9.]+)", proc.stderr)]
    ends = [float(value) for value in re.findall(r"silence_end:\s*([0-9.]+)", proc.stderr)]
    out: list[dict[str, Any]] = []
    for index, start in enumerate(starts):
        end = ends[index] if index < len(ends) else duration
        if end > start:
            out.append({"start_sec": round(start, 3), "end_sec": round(end, 3), "duration_sec": round(end - start, 3)})
    return out


def _cleanup_suggestions(transcript: dict[str, Any], silences: list[dict[str, Any]]) -> list[dict[str, Any]]:
    suggestions: list[dict[str, Any]] = []
    for word in transcript.get("words") or []:
        normalized = re.sub(r"[^0-9a-z\u4e00-\u9fff]+", "", str(word.get("text") or "").lower())
        if normalized not in _FILLERS:
            continue
        start, end = float(word.get("start_sec") or 0), float(word.get("end_sec") or 0)
        suggestions.append(
            {
                "id": _suggestion_id("filler", start, end, normalized),
                "kind": "filler",
                "label": f"Remove filler: {normalized}",
                "start_sec": round(max(start - 0.04, 0.0), 3),
                "end_sec": round(end + 0.04, 3),
                "confidence": word.get("confidence"),
                "review_status": "pending",
            }
        )
    for silence in silences:
        if float(silence.get("duration_sec") or 0.0) < 0.65:
            continue
        start = float(silence["start_sec"]) + 0.12
        end = float(silence["end_sec"]) - 0.12
        if end - start < 0.2:
            continue
        suggestions.append(
            {
                "id": _suggestion_id("pause", start, end, ""),
                "kind": "pause",
                "label": f"Tighten {end - start:.1f}s pause",
                "start_sec": round(start, 3),
                "end_sec": round(end, 3),
                "confidence": 1.0,
                "review_status": "pending",
            }
        )
    return sorted(suggestions, key=lambda item: (item["start_sec"], item["end_sec"], item["kind"]))


def _rank_takes(account_id: str, results: list[dict[str, Any]]) -> None:
    groups: dict[str, list[dict[str, Any]]] = {}
    manifests: dict[str, dict[str, Any]] = {}
    for result in results:
        manifest = load_roughcut(account_id, str(result["asset_id"]))
        manifests[str(result["asset_id"])] = manifest
        key = _take_group_key(str(manifest.get("name") or ""), str(manifest.get("transcript", {}).get("text") or ""))
        groups.setdefault(key, []).append(manifest)
    for key, group in groups.items():
        ordered = sorted(group, key=lambda item: (-float(item.get("score") or 0.0), str(item.get("asset_id"))))
        alternatives = [str(item["asset_id"]) for item in ordered]
        for rank, manifest in enumerate(ordered, start=1):
            manifest["take"] = {
                "group": key,
                "rank": rank,
                "selected": rank == 1,
                "alternatives": [item for item in alternatives if item != manifest["asset_id"]],
                "user_decision": manifest.get("take", {}).get("user_decision"),
            }
            manifest["updated_at"] = _utc_now()
            _atomic_json(_manifest_path(account_id, str(manifest["asset_id"])), manifest)
            _write_roughcut_annotations(account_id, manifest)
            result = next(item for item in results if item.get("asset_id") == manifest["asset_id"])
            result.update({"take": manifest["take"], "score": manifest["score"]})


def _write_roughcut_annotations(account_id: str, manifest: dict[str, Any]) -> None:
    annotations: list[dict[str, Any]] = [
        {
            "scope": "asset",
            "label": "Recommended take" if manifest.get("take", {}).get("selected") else "Alternative take",
            "note": str(manifest.get("transcript", {}).get("text") or "")[:1000],
            "tags": ["roughcut", "transcript", "selected" if manifest.get("take", {}).get("selected") else "alternative"],
            "category": "roughcut_summary",
            "confidence": manifest.get("score"),
            "source": "roughcut",
            "language": manifest.get("language") or "auto",
            "metadata": {"take": manifest.get("take"), "proxy": manifest.get("proxy")},
        }
    ]
    for segment in manifest.get("transcript", {}).get("segments") or []:
        annotations.append(
            {
                "scope": "time_range",
                "start_sec": segment["start_sec"],
                "end_sec": segment["end_sec"],
                "label": segment.get("corrected_text") or segment.get("text") or "Transcript",
                "note": "Transcript segment",
                "tags": ["roughcut", "transcript"],
                "category": "transcript",
                "confidence": segment.get("confidence"),
                "source": "roughcut",
                "language": manifest.get("language") or "auto",
                "metadata": {"segment_id": segment["id"], "review_status": segment.get("review_status")},
            }
        )
    for suggestion in manifest.get("cleanup_suggestions") or []:
        annotations.append(
            {
                "scope": "time_range",
                "start_sec": suggestion["start_sec"],
                "end_sec": suggestion["end_sec"],
                "label": suggestion["label"],
                "note": "Review before removing; no timeline change has been made.",
                "tags": ["roughcut", "cleanup", suggestion["kind"]],
                "category": "cleanup_suggestion",
                "confidence": suggestion.get("confidence"),
                "source": "roughcut",
                "language": manifest.get("language") or "auto",
                "metadata": {"suggestion_id": suggestion["id"], "review_status": suggestion.get("review_status")},
            }
        )
    upsert_annotations(account_id, str(manifest["asset_id"]), annotations, replace_source="roughcut")


def _take_score(
    asset: dict[str, Any],
    transcript: dict[str, Any],
    silences: list[dict[str, Any]],
    cleanup: list[dict[str, Any]],
    duration: float,
) -> float:
    confidence = float(transcript.get("confidence") or 0.0)
    silent = sum(float(item.get("duration_sec") or 0.0) for item in silences)
    speech_ratio = max(0.0, min(1.0, (duration - silent) / duration)) if duration > 0 else 0.0
    technical = 1.0 if asset.get("status") == "ready" and asset.get("has_audio") else 0.5
    filler_count = sum(item.get("kind") == "filler" for item in cleanup)
    pause_count = sum(item.get("kind") == "pause" for item in cleanup)
    cleanup_penalty = min(0.30, filler_count * 0.10 + pause_count * 0.025)
    return round(max(0.0, 0.55 * confidence + 0.30 * speech_ratio + 0.15 * technical - cleanup_penalty), 4)


def _take_group_key(name: str, transcript: str) -> str:
    stem = Path(name).stem.lower()
    stem = re.sub(r"(?:^|[-_\s])(?:take|tk|t|v)?\d+(?:$|[-_\s])", " ", stem)
    stem = re.sub(r"第?\d+[条遍次]", " ", stem)
    stem = " ".join(re.findall(r"[0-9a-z\u4e00-\u9fff]+", stem))
    if stem:
        return stem[:120]
    words = re.findall(r"[0-9a-z\u4e00-\u9fff]+", transcript.lower())[:12]
    return " ".join(words) or "ungrouped"


def _result_summary(manifest: dict[str, Any], *, reused: bool) -> dict[str, Any]:
    return {
        "asset_id": manifest["asset_id"],
        "status": "ready",
        "reused": reused,
        "transcript_segments": len(manifest.get("transcript", {}).get("segments") or []),
        "cleanup_suggestions": len(manifest.get("cleanup_suggestions") or []),
        "proxy_status": (manifest.get("proxy") or {}).get("status") if manifest.get("proxy") else "not_requested",
        "score": manifest.get("score"),
        "take": manifest.get("take"),
    }


def _job_args(payload: dict[str, Any]) -> dict[str, Any]:
    raw_ids = payload.get("asset_ids") or payload.get("assets") or []
    if isinstance(raw_ids, str):
        raw_ids = [raw_ids]
    if not isinstance(raw_ids, list):
        raise RoughcutError("asset_ids must be a list")
    return {
        "asset_ids": [str(item) for item in raw_ids],
        "all_assets": bool(payload.get("all") or payload.get("all_assets")),
        "language": str(payload.get("language") or "auto"),
        "create_proxies": bool(payload.get("create_proxies", True)),
        "proxy_resolution": max(240, min(int(payload.get("proxy_resolution") or 540), 1080)),
        "resume": bool(payload.get("resume", True)),
        "max_assets": max(1, min(int(payload.get("max_assets") or 100), 100)),
    }


def _whisper_binary() -> str:
    configured = str(os.environ.get("LUMERI_WHISPER_CLI") or "").strip()
    found = configured or shutil.which("whisper-cli") or "/opt/homebrew/bin/whisper-cli"
    if not Path(found).exists():
        raise RoughcutError("whisper-cli not found; install whisper.cpp or set LUMERI_WHISPER_CLI")
    return found


def _whisper_model() -> Path:
    candidates = [
        Path(str(os.environ.get("LUMERI_WHISPER_MODEL") or "")).expanduser(),
        Path("~/.whisper/models/ggml-small.bin").expanduser(),
        Path("~/.cache/whisper/ggml-small.bin").expanduser(),
    ]
    for candidate in candidates:
        if str(candidate) and candidate.is_file():
            return candidate
    raise RoughcutError("local Whisper model not found; set LUMERI_WHISPER_MODEL")


def _run(command: list[str], label: str, *, timeout: int = 1800) -> None:
    proc = subprocess.run(command, capture_output=True, text=True, timeout=timeout, check=False)
    if proc.returncode != 0:
        detail = (proc.stderr or proc.stdout or "unknown error")[-1000:]
        raise RoughcutError(f"{label} failed: {detail}")


def _signature(asset: dict[str, Any], language: str, create_proxy: bool, resolution: int) -> str:
    raw = json.dumps(
        {
            "version": _MANIFEST_VERSION,
            "fingerprint": asset.get("fingerprint"),
            "language": language,
            "create_proxy": create_proxy,
            "proxy_resolution": resolution,
            "model": str(_whisper_model()),
        },
        sort_keys=True,
    )
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _suggestion_id(kind: str, start: float, end: float, text: str) -> str:
    digest = hashlib.sha1(f"{kind}|{start:.3f}|{end:.3f}|{text}".encode("utf-8")).hexdigest()[:10]
    return f"{kind}-{digest}"


def _find_by_id(items: Any, target_id: str) -> dict[str, Any]:
    for item in items or []:
        if str(item.get("id") or "") == target_id:
            return item
    raise RoughcutError(f"review target not found: {target_id}")


def _manifest_path(account_id: str, asset_id: str) -> Path:
    return asset_cache_root(account_id, asset_id) / "roughcut.json"


def _jobs_root(account_id: str) -> Path:
    return media_root(account_id) / "roughcut-jobs"


def _job_path(account_id: str, job_id: str) -> Path:
    if not re.fullmatch(r"roughcut_[0-9a-f]{12}", str(job_id or "")):
        raise RoughcutError("invalid rough-cut job id")
    return _jobs_root(account_id) / f"{job_id}.json"


def _write_job(account_id: str, record: dict[str, Any]) -> None:
    _atomic_json(_job_path(account_id, str(record["job_id"])), record)


def _read_json(path: Path) -> dict[str, Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise RoughcutError("rough-cut record not found") from exc
    except (OSError, json.JSONDecodeError) as exc:
        raise RoughcutError(f"invalid rough-cut record: {exc}") from exc
    if not isinstance(payload, dict):
        raise RoughcutError("invalid rough-cut record")
    return payload


def _atomic_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_suffix(path.suffix + f".{uuid.uuid4().hex[:8]}.tmp")
    temp.write_text(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True), encoding="utf-8")
    temp.replace(path)


def _emit(progress: ProgressCallback | None, percent: float, message: str, asset_id: str | None) -> None:
    if progress:
        progress({"percent": round(max(0.0, min(percent, 100.0)), 2), "message": message, "asset_id": asset_id})


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


__all__ = [
    "RoughcutError",
    "apply_roughcut_review",
    "get_prepare_job",
    "load_roughcut",
    "prepare_roughcut",
    "resume_prepare_job",
    "start_prepare_job",
]
