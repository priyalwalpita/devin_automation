"""SQLite persistence: remediation rows plus an append-only event audit log."""

from __future__ import annotations

import os
import sqlite3
import time
from typing import Any, Iterable, Optional

from app.config import settings

REMEDIATION_FIELDS = (
    "issue_number",
    "issue_title",
    "issue_body",
    "issue_url",
    "state",
    "session_id",
    "session_url",
    "devin_status",
    "devin_detail",
    "pr_url",
    "pr_state",
    "acus_consumed",
    "structured_out",
    "error",
    "created_ts",
    "launched_ts",
    "pr_ts",
    "completed_ts",
    "updated_ts",
)

ACTIVE_STATES = ("launching", "running", "blocked")


def _connect() -> sqlite3.Connection:
    directory = os.path.dirname(settings.db_path)
    if directory:
        os.makedirs(directory, exist_ok=True)
    conn = sqlite3.connect(settings.db_path, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    return conn


def init_db() -> None:
    with _connect() as conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS remediations (
                issue_number   INTEGER PRIMARY KEY,
                issue_title    TEXT,
                issue_body     TEXT,
                issue_url      TEXT,
                state          TEXT NOT NULL,
                session_id     TEXT,
                session_url    TEXT,
                devin_status   TEXT,
                devin_detail   TEXT,
                pr_url         TEXT,
                pr_state       TEXT,
                acus_consumed  REAL,
                structured_out TEXT,
                error          TEXT,
                created_ts     REAL,
                launched_ts    REAL,
                pr_ts          REAL,
                completed_ts   REAL,
                updated_ts     REAL
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS events (
                id           INTEGER PRIMARY KEY AUTOINCREMENT,
                issue_number INTEGER,
                ts           REAL NOT NULL,
                kind         TEXT NOT NULL,
                detail       TEXT
            )
            """
        )
        conn.execute("CREATE INDEX IF NOT EXISTS idx_events_kind ON events(kind)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_events_issue ON events(issue_number)")


def enqueue(
    issue_number: int,
    issue_title: str,
    issue_body: str,
    issue_url: str,
) -> bool:
    """Insert a queued remediation. Returns False when the issue is already tracked."""
    now = time.time()
    with _connect() as conn:
        cur = conn.execute(
            """
            INSERT OR IGNORE INTO remediations
                (issue_number, issue_title, issue_body, issue_url, state, created_ts, updated_ts)
            VALUES (?, ?, ?, ?, 'queued', ?, ?)
            """,
            (issue_number, issue_title, issue_body, issue_url, now, now),
        )
        return cur.rowcount == 1


def claim_queued(issue_number: int) -> bool:
    """Atomically move a row from queued to launching. False when someone else won it."""
    with _connect() as conn:
        cur = conn.execute(
            "UPDATE remediations SET state = 'launching', updated_ts = ? "
            "WHERE issue_number = ? AND state = 'queued'",
            (time.time(), issue_number),
        )
        return cur.rowcount == 1


def update(issue_number: int, **fields: Any) -> None:
    unknown = set(fields) - set(REMEDIATION_FIELDS)
    if unknown:
        raise ValueError(f"unknown remediation fields: {sorted(unknown)}")
    fields["updated_ts"] = time.time()
    assignments = ", ".join(f"{name} = ?" for name in fields)
    with _connect() as conn:
        conn.execute(
            f"UPDATE remediations SET {assignments} WHERE issue_number = ?",
            (*fields.values(), issue_number),
        )


def rows(states: Optional[Iterable[str]] = None) -> list[dict[str, Any]]:
    query = "SELECT * FROM remediations"
    params: tuple[Any, ...] = ()
    if states is not None:
        states = tuple(states)
        if not states:
            return []
        placeholders = ", ".join("?" for _ in states)
        query += f" WHERE state IN ({placeholders})"
        params = states
    query += " ORDER BY created_ts ASC"
    with _connect() as conn:
        return [dict(row) for row in conn.execute(query, params)]


def log_event(issue_number: Optional[int], kind: str, detail: str = "") -> None:
    with _connect() as conn:
        conn.execute(
            "INSERT INTO events (issue_number, ts, kind, detail) VALUES (?, ?, ?, ?)",
            (issue_number, time.time(), kind, detail),
        )


def recent_events(limit: int = 40) -> list[dict[str, Any]]:
    with _connect() as conn:
        return [
            dict(row)
            for row in conn.execute(
                "SELECT * FROM events ORDER BY id DESC LIMIT ?", (limit,)
            )
        ]


def has_event(issue_number: int, kind: str) -> bool:
    with _connect() as conn:
        row = conn.execute(
            "SELECT 1 FROM events WHERE issue_number = ? AND kind = ? LIMIT 1",
            (issue_number, kind),
        ).fetchone()
    return row is not None


def count_events(kind: str, since_ts: Optional[float] = None) -> int:
    query = "SELECT COUNT(*) FROM events WHERE kind = ?"
    params: list[Any] = [kind]
    if since_ts is not None:
        query += " AND ts >= ?"
        params.append(since_ts)
    with _connect() as conn:
        return int(conn.execute(query, params).fetchone()[0])


def _percentile(values: list[float], fraction: float) -> Optional[float]:
    if not values:
        return None
    ordered = sorted(values)
    if len(ordered) == 1:
        return ordered[0]
    position = fraction * (len(ordered) - 1)
    low = int(position)
    high = min(low + 1, len(ordered) - 1)
    weight = position - low
    return ordered[low] * (1 - weight) + ordered[high] * weight


def _mean(values: list[float]) -> Optional[float]:
    return sum(values) / len(values) if values else None


def metrics() -> dict[str, Any]:
    all_rows = rows()
    by_state: dict[str, int] = {}
    for row in all_rows:
        by_state[row["state"]] = by_state.get(row["state"], 0) + 1

    completed = by_state.get("completed", 0)
    failed = by_state.get("failed", 0)
    finished = completed + failed

    to_pr = [
        row["pr_ts"] - row["launched_ts"]
        for row in all_rows
        if row["pr_ts"] and row["launched_ts"]
    ]
    queue_waits = [
        row["launched_ts"] - row["created_ts"]
        for row in all_rows
        if row["launched_ts"] and row["created_ts"]
    ]
    session_durations = [
        row["completed_ts"] - row["launched_ts"]
        for row in all_rows
        if row["completed_ts"] and row["launched_ts"]
    ]

    now = time.time()
    completed_last_hour = sum(
        1 for row in all_rows if row["completed_ts"] and row["completed_ts"] >= now - 3600
    )
    completed_last_24h = sum(
        1 for row in all_rows if row["completed_ts"] and row["completed_ts"] >= now - 86400
    )

    reported_acus = [
        row["acus_consumed"] for row in all_rows if row["acus_consumed"] is not None
    ]
    total_acus = sum(reported_acus) if reported_acus else None
    prs_opened = sum(1 for row in all_rows if row["pr_url"])

    funnel = {
        "received": count_events("webhook_received"),
        "ignored": count_events("webhook_ignored"),
        "duplicates": count_events("webhook_duplicate"),
        "accepted": count_events("queued"),
        "launched": count_events("session_started"),
        "prs": count_events("pr_opened"),
        "completed": count_events("completed"),
        "failed": count_events("failed"),
    }

    return {
        "total": len(all_rows),
        "by_state": by_state,
        "active": sum(by_state.get(state, 0) for state in ACTIVE_STATES),
        "success_rate": (completed / finished) if finished else None,
        "median_seconds_to_pr": _percentile(to_pr, 0.5),
        "p95_seconds_to_pr": _percentile(to_pr, 0.95),
        "avg_queue_wait_seconds": _mean(queue_waits),
        "avg_session_seconds": _mean(session_durations),
        "completed_last_hour": completed_last_hour,
        "completed_last_24h": completed_last_24h,
        "funnel": funnel,
        "total_acus": total_acus,
        "avg_acus_per_fix": (total_acus / completed) if total_acus and completed else None,
        "est_cost_usd": (total_acus * settings.acu_cost_usd) if total_acus is not None else None,
        "prs_opened": prs_opened,
    }
