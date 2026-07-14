from __future__ import annotations

import argparse
import json
import os
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Literal


RouteSource = Literal["keyword", "llm", "fallback"]


@dataclass
class RouteEvent:
    timestamp: datetime
    raw_request: str
    effective_request: str
    clarifications: dict[str, Any]
    hit_skills: list[str]
    route_source: RouteSource
    confidence: float
    final_plan_steps: list[str] | None = None
    user_satisfied: bool | None = None


def default_db_path() -> Path:
    override = os.environ.get("GEMIA_SKILL_TELEMETRY_DB", "").strip()
    if override:
        return Path(override).expanduser()
    return Path.home() / ".gemia" / "planner" / "skill_telemetry.sqlite3"


def record_route_event(event: RouteEvent, *, db_path: str | Path | None = None) -> int:
    path = _db_path(db_path)
    _init_db(path)
    with sqlite3.connect(path) as conn:
        cur = conn.execute(
            """
            INSERT INTO route_events (
                timestamp, raw_request, effective_request, clarifications_json,
                hit_skills_json, route_source, confidence, final_plan_steps_json,
                user_satisfied
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                event.timestamp.astimezone(timezone.utc).isoformat(),
                event.raw_request,
                event.effective_request,
                json.dumps(event.clarifications or {}, ensure_ascii=False, sort_keys=True),
                json.dumps(event.hit_skills or [], ensure_ascii=False),
                event.route_source,
                float(event.confidence),
                json.dumps(event.final_plan_steps or [], ensure_ascii=False),
                _bool_to_db(event.user_satisfied),
            ),
        )
        conn.commit()
        return int(cur.lastrowid)


def update_final_plan_steps(event_id: int | None, steps: list[str], *, db_path: str | Path | None = None) -> None:
    if not event_id:
        return
    path = _db_path(db_path)
    _init_db(path)
    with sqlite3.connect(path) as conn:
        conn.execute(
            "UPDATE route_events SET final_plan_steps_json = ? WHERE id = ?",
            (json.dumps(steps or [], ensure_ascii=False), int(event_id)),
        )
        conn.commit()


def skill_stats(*, days: int = 7, db_path: str | Path | None = None, all_skill_ids: list[str] | None = None) -> dict[str, Any]:
    path = _db_path(db_path)
    _init_db(path)
    since = datetime.now(timezone.utc) - timedelta(days=max(int(days), 1))
    rows = _read_rows(path, since)
    skill_counts: dict[str, int] = {skill_id: 0 for skill_id in all_skill_ids or []}
    pair_counts: dict[tuple[str, str], int] = {}
    source_counts: dict[str, int] = {"keyword": 0, "llm": 0, "fallback": 0}
    fallback_requests: list[str] = []
    for row in rows:
        skills = _loads(row["hit_skills_json"], [])
        source = str(row["route_source"])
        source_counts[source] = source_counts.get(source, 0) + 1
        if source == "fallback":
            fallback_requests.append(str(row["effective_request"]))
        unique = sorted({str(skill) for skill in skills})
        for skill in unique:
            skill_counts[skill] = skill_counts.get(skill, 0) + 1
        for i, a in enumerate(unique):
            for b in unique[i + 1 :]:
                pair_counts[(a, b)] = pair_counts.get((a, b), 0) + 1
    low_activation = [
        {"skill": skill, "count": count}
        for skill, count in sorted(skill_counts.items())
        if count < 5
    ]
    cooccurrence: list[dict[str, Any]] = []
    for (a, b), pair_count in sorted(pair_counts.items()):
        base = max(1, min(skill_counts.get(a, 0), skill_counts.get(b, 0)))
        rate = pair_count / base
        if rate > 0.6:
            cooccurrence.append({"skills": [a, b], "count": pair_count, "rate": round(rate, 3)})
    total = max(len(rows), 1)
    source_distribution = {
        source: {"count": count, "pct": round(count / total * 100, 1)}
        for source, count in sorted(source_counts.items())
    }
    return {
        "db_path": str(path),
        "days": days,
        "event_count": len(rows),
        "low_activation": low_activation,
        "cooccurrence_gt_60pct": cooccurrence,
        "fallback_requests": fallback_requests,
        "source_distribution": source_distribution,
    }


def format_skill_stats(stats: dict[str, Any]) -> str:
    lines = [
        f"Lumeri skill stats ({stats.get('days', 7)}d)",
        f"DB: {stats.get('db_path')}",
        f"Events: {stats.get('event_count', 0)}",
        "",
        "Route source distribution:",
    ]
    for source, data in (stats.get("source_distribution") or {}).items():
        lines.append(f"- {source}: {data.get('count', 0)} ({data.get('pct', 0)}%)")
    lines.append("")
    lines.append("Low activation skills (<5):")
    for item in stats.get("low_activation") or []:
        lines.append(f"- {item['skill']}: {item['count']}")
    lines.append("")
    lines.append("Co-occurrence >60%:")
    for item in stats.get("cooccurrence_gt_60pct") or []:
        lines.append(f"- {', '.join(item['skills'])}: {item['rate']} ({item['count']})")
    lines.append("")
    lines.append("Fallback requests:")
    for request in stats.get("fallback_requests") or []:
        lines.append(f"- {request}")
    return "\n".join(lines).rstrip() + "\n"


def main(argv: list[str] | None = None) -> None:
    from .skill_router import load_skill_metadata

    parser = argparse.ArgumentParser(prog="lumeri-skill-stats")
    parser.add_argument("--days", type=int, default=7)
    parser.add_argument("--db", default=None)
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)
    stats = skill_stats(
        days=args.days,
        db_path=args.db,
        all_skill_ids=sorted(load_skill_metadata().keys()),
    )
    if args.json:
        print(json.dumps(stats, ensure_ascii=False, indent=2, sort_keys=True))
    else:
        print(format_skill_stats(stats), end="")


def _db_path(db_path: str | Path | None) -> Path:
    return Path(db_path).expanduser() if db_path else default_db_path()


def _init_db(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(path) as conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS route_events (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                timestamp TEXT NOT NULL,
                raw_request TEXT NOT NULL,
                effective_request TEXT NOT NULL,
                clarifications_json TEXT NOT NULL,
                hit_skills_json TEXT NOT NULL,
                route_source TEXT NOT NULL,
                confidence REAL NOT NULL,
                final_plan_steps_json TEXT NOT NULL DEFAULT '[]',
                user_satisfied INTEGER NULL
            )
            """
        )
        conn.execute("CREATE INDEX IF NOT EXISTS idx_route_events_timestamp ON route_events(timestamp)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_route_events_source ON route_events(route_source)")
        conn.commit()


def _read_rows(path: Path, since: datetime) -> list[sqlite3.Row]:
    with sqlite3.connect(path) as conn:
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            "SELECT * FROM route_events WHERE timestamp >= ? ORDER BY timestamp DESC",
            (since.astimezone(timezone.utc).isoformat(),),
        ).fetchall()
    return rows


def _loads(text: str, fallback: Any) -> Any:
    try:
        return json.loads(text)
    except Exception:
        return fallback


def _bool_to_db(value: bool | None) -> int | None:
    if value is None:
        return None
    return 1 if value else 0


if __name__ == "__main__":
    main()
