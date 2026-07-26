"""Hand-rolled observability: log ring buffer, poller heartbeat, Prometheus text."""

from __future__ import annotations

import logging
import time
from collections import deque
from typing import Any, Optional

RING_CAPACITY = 300
_ring: deque[dict[str, Any]] = deque(maxlen=RING_CAPACITY)

last_poll_ts: float = 0.0


class RingBufferHandler(logging.Handler):
    """Keeps the most recent formatted log records in memory for /logs and the dashboard."""

    def emit(self, record: logging.LogRecord) -> None:
        try:
            message = self.format(record)
        except Exception:  # pragma: no cover - formatting must never break logging
            message = record.getMessage()
        _ring.append(
            {"ts": record.created, "level": record.levelname, "message": message}
        )


def recent_logs(n: int = 50) -> list[dict[str, Any]]:
    if n <= 0:
        return []
    return list(_ring)[-n:][::-1]


def mark_poll(ts: Optional[float] = None) -> None:
    global last_poll_ts
    last_poll_ts = ts if ts is not None else time.time()


def poller_lag_seconds() -> Optional[float]:
    if not last_poll_ts:
        return None
    return time.time() - last_poll_ts


def _num(value: Any) -> str:
    return f"{float(value):.6g}"


def prometheus_text(metrics_dict: dict[str, Any], lag: Optional[float]) -> str:
    """Render metrics as Prometheus exposition text without a client library."""
    prefix = "devin_automations"
    lines: list[str] = []

    def add(name: str, kind: str, help_text: str, value: Any) -> None:
        if value is None:
            return
        lines.append(f"# HELP {prefix}_{name} {help_text}")
        lines.append(f"# TYPE {prefix}_{name} {kind}")
        lines.append(f"{prefix}_{name} {_num(value)}")

    by_state = metrics_dict.get("by_state", {})
    funnel = metrics_dict.get("funnel", {})

    add("remediations_total", "gauge", "Remediations tracked.", metrics_dict.get("total"))
    add("active_sessions", "gauge", "Sessions currently launching/running/blocked.", metrics_dict.get("active"))
    add("completed_total", "counter", "Remediations completed.", by_state.get("completed", 0))
    add("failed_total", "counter", "Remediations failed.", by_state.get("failed", 0))
    add("blocked_total", "gauge", "Remediations blocked on human input.", by_state.get("blocked", 0))
    add("queued_total", "gauge", "Remediations waiting for a session slot.", by_state.get("queued", 0))
    add("prs_opened_total", "counter", "Pull requests opened by sessions.", metrics_dict.get("prs_opened"))
    add("success_rate", "gauge", "completed / (completed + failed).", metrics_dict.get("success_rate"))
    add("median_seconds_to_pr", "gauge", "Median seconds from launch to PR.", metrics_dict.get("median_seconds_to_pr"))
    add("p95_seconds_to_pr", "gauge", "p95 seconds from launch to PR.", metrics_dict.get("p95_seconds_to_pr"))
    add("avg_queue_wait_seconds", "gauge", "Average seconds spent queued.", metrics_dict.get("avg_queue_wait_seconds"))
    add("avg_session_seconds", "gauge", "Average seconds from launch to completion.", metrics_dict.get("avg_session_seconds"))
    add("completed_last_hour", "gauge", "Remediations completed in the last hour.", metrics_dict.get("completed_last_hour"))
    add("completed_last_24h", "gauge", "Remediations completed in the last 24 hours.", metrics_dict.get("completed_last_24h"))
    add("acus_total", "counter", "ACUs reported by the Devin API.", metrics_dict.get("total_acus"))
    add("avg_acus_per_fix", "gauge", "Average ACUs per completed remediation.", metrics_dict.get("avg_acus_per_fix"))
    add("est_cost_usd", "gauge", "Estimated spend in USD.", metrics_dict.get("est_cost_usd"))
    add("poller_lag_seconds", "gauge", "Seconds since the poller last ran.", lag)

    for name in ("received", "ignored", "duplicates", "accepted", "launched", "prs", "completed", "failed"):
        add(
            f"funnel_{name}_total",
            "counter",
            f"Intake funnel counter: {name}.",
            funnel.get(name, 0),
        )

    return "\n".join(lines) + "\n"
