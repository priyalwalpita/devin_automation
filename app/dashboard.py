"""Server-rendered operations dashboard (inline CSS, 10s meta refresh)."""

from __future__ import annotations

import html
import time
from typing import Any, Optional

from app.config import settings

BG = "#101623"
PANEL = "#171F2E"
LINE = "#232E42"
TEXT = "#E7ECF5"
MUTED = "#8A96AB"
AMBER = "#EFB341"
RED = "#E0574C"
GREEN = "#5BB98C"

STATE_COLORS = {
    "queued": MUTED,
    "launching": "#6FA8DC",
    "running": "#6FA8DC",
    "blocked": AMBER,
    "completed": GREEN,
    "failed": RED,
}


def _esc(value: Any) -> str:
    return html.escape("" if value is None else str(value))


def _dur(seconds: Optional[float]) -> str:
    if seconds is None:
        return "n/a"
    seconds = float(seconds)
    if seconds < 60:
        return f"{seconds:.0f}s"
    if seconds < 3600:
        return f"{seconds / 60:.1f}m"
    return f"{seconds / 3600:.1f}h"


def _pct(value: Optional[float]) -> str:
    return "n/a" if value is None else f"{value * 100:.0f}%"


def _num(value: Optional[float], suffix: str = "") -> str:
    return "n/a" if value is None else f"{value:,.2f}{suffix}"


def _card(label: str, value: str, sub: str = "") -> str:
    sub_html = f'<div style="color:{MUTED};font-size:11px;margin-top:4px">{_esc(sub)}</div>' if sub else ""
    return (
        f'<div style="background:{PANEL};border:1px solid {LINE};border-radius:6px;padding:14px 16px">'
        f'<div style="color:{MUTED};font-size:11px;letter-spacing:.12em;text-transform:uppercase">{_esc(label)}</div>'
        f'<div style="color:{TEXT};font-size:26px;margin-top:6px">{_esc(value)}</div>'
        f"{sub_html}</div>"
    )


def _funnel_stage(label: str, value: Any, annotation: str = "") -> str:
    annotation_html = (
        f'<div style="color:{MUTED};font-size:10px;margin-top:4px">{_esc(annotation)}</div>'
        if annotation
        else ""
    )
    return (
        '<div style="text-align:center;min-width:120px">'
        f'<div style="color:{TEXT};font-size:24px">{_esc(value)}</div>'
        f'<div style="color:{MUTED};font-size:10px;letter-spacing:.14em;margin-top:2px">{_esc(label)}</div>'
        f"{annotation_html}</div>"
    )


def _state_badge(state: str) -> str:
    color = STATE_COLORS.get(state, MUTED)
    return (
        f'<span style="border:1px solid {color};color:{color};border-radius:3px;'
        f'padding:1px 7px;font-size:11px;letter-spacing:.08em">{_esc(state.upper())}</span>'
    )


def _link(url: Optional[str], text: str) -> str:
    if not url:
        return f'<span style="color:{MUTED}">—</span>'
    return f'<a href="{_esc(url)}" style="color:{AMBER};text-decoration:none">{_esc(text)}</a>'


def render(
    metrics: dict[str, Any],
    remediations: list[dict[str, Any]],
    events: list[dict[str, Any]],
    logs: list[dict[str, Any]],
    lag: Optional[float],
) -> str:
    now = time.time()
    funnel = metrics.get("funnel", {})
    by_state = metrics.get("by_state", {})

    lag_limit = 3 * settings.poll_interval_seconds
    if lag is None:
        heartbeat_text, heartbeat_color = "poller not started", RED
    elif lag > lag_limit:
        heartbeat_text, heartbeat_color = f"poll {lag:.0f}s ago", RED
    else:
        heartbeat_text, heartbeat_color = f"poll {lag:.0f}s ago", GREEN

    mode_label = "MOCK" if settings.mock_devin else "LIVE"
    mode_color = AMBER if settings.mock_devin else GREEN

    header = f"""
    <div style="display:flex;align-items:center;gap:14px;flex-wrap:wrap;padding-bottom:14px;border-bottom:1px solid {LINE}">
      <div style="font-size:18px;letter-spacing:.28em;color:{TEXT}">DEVIN AUTOMATIONS</div>
      <span style="border:1px solid {mode_color};color:{mode_color};border-radius:3px;padding:1px 8px;font-size:11px;letter-spacing:.16em">{mode_label}</span>
      <span style="color:{MUTED};font-size:12px">target {_esc(settings.target_repo)} @ {_esc(settings.base_branch)}</span>
      <span style="margin-left:auto;border:1px solid {heartbeat_color};color:{heartbeat_color};border-radius:3px;padding:1px 8px;font-size:11px">{_esc(heartbeat_text)}</span>
    </div>
    """

    arrow = f'<div style="color:{AMBER};font-size:18px">&#8594;</div>'
    ignored_note = f"{funnel.get('ignored', 0)} ignored · {funnel.get('duplicates', 0)} dupes"
    pipeline = f"""
    <div style="display:flex;align-items:center;justify-content:space-between;gap:8px;background:{PANEL};
                border:1px solid {LINE};border-radius:6px;padding:16px;margin:18px 0">
      {_funnel_stage("RECEIVED", funnel.get("received", 0), ignored_note)}
      {arrow}
      {_funnel_stage("ACCEPTED", funnel.get("accepted", 0))}
      {arrow}
      {_funnel_stage("SESSIONS ACTIVE", metrics.get("active", 0))}
      {arrow}
      {_funnel_stage("PRS OPEN", metrics.get("prs_opened", 0))}
      {arrow}
      {_funnel_stage("COMPLETED", by_state.get("completed", 0))}
    </div>
    """

    total_acus = metrics.get("total_acus")
    est_cost = metrics.get("est_cost_usd")
    cards = "".join(
        [
            _card("success rate", _pct(metrics.get("success_rate")), f"{by_state.get('completed', 0)} completed"),
            _card("median time to pr", _dur(metrics.get("median_seconds_to_pr"))),
            _card("p95 time to pr", _dur(metrics.get("p95_seconds_to_pr"))),
            _card("avg queue wait", _dur(metrics.get("avg_queue_wait_seconds"))),
            _card("avg session duration", _dur(metrics.get("avg_session_seconds"))),
            _card("completed 24h", str(metrics.get("completed_last_24h", 0))),
            _card("blocked", str(by_state.get("blocked", 0))),
            _card("failed", str(by_state.get("failed", 0))),
            _card("total acus", "n/a" if total_acus is None else f"{total_acus:.2f}"),
            _card("est. cost", "n/a" if est_cost is None else f"${est_cost:,.2f}", f"@ ${settings.acu_cost_usd:.2f}/ACU"),
        ]
    )
    cards_grid = (
        '<div style="display:grid;grid-template-columns:repeat(auto-fit,minmax(170px,1fr));gap:10px">'
        f"{cards}</div>"
    )

    if remediations:
        body_rows = []
        for row in remediations:
            structured = row.get("structured_out") or ""
            outcome = "—"
            for candidate in ("fixed", "partial", "blocked"):
                if f'"outcome": "{candidate}"' in structured or f'"outcome":"{candidate}"' in structured:
                    outcome = candidate
                    break
            queue_wait = (
                row["launched_ts"] - row["created_ts"]
                if row.get("launched_ts") and row.get("created_ts")
                else None
            )
            end_ts = row.get("completed_ts") or now
            elapsed = end_ts - row["launched_ts"] if row.get("launched_ts") else None
            acus = row.get("acus_consumed")
            body_rows.append(
                "<tr>"
                f'<td style="padding:8px 10px;border-top:1px solid {LINE}">#{_esc(row["issue_number"])}</td>'
                f'<td style="padding:8px 10px;border-top:1px solid {LINE};max-width:380px">{_esc(row.get("issue_title"))}</td>'
                f'<td style="padding:8px 10px;border-top:1px solid {LINE}">{_state_badge(row["state"])}</td>'
                f'<td style="padding:8px 10px;border-top:1px solid {LINE}">{_link(row.get("session_url"), "session")}</td>'
                f'<td style="padding:8px 10px;border-top:1px solid {LINE}">{_link(row.get("pr_url"), "pull request")}</td>'
                f'<td style="padding:8px 10px;border-top:1px solid {LINE}">{"n/a" if acus is None else f"{acus:.2f}"}</td>'
                f'<td style="padding:8px 10px;border-top:1px solid {LINE}">{_dur(queue_wait)}</td>'
                f'<td style="padding:8px 10px;border-top:1px solid {LINE}">{_dur(elapsed)}</td>'
                f'<td style="padding:8px 10px;border-top:1px solid {LINE}">{_esc(outcome)}</td>'
                "</tr>"
            )
        headers = "".join(
            f'<th style="text-align:left;padding:8px 10px;color:{MUTED};font-size:10px;letter-spacing:.14em">{label}</th>'
            for label in (
                "ISSUE",
                "FINDING",
                "STATE",
                "SESSION",
                "PR",
                "ACUS",
                "QUEUE WAIT",
                "ELAPSED",
                "OUTCOME",
            )
        )
        table = (
            f'<table style="width:100%;border-collapse:collapse;font-size:12px">'
            f"<thead><tr>{headers}</tr></thead><tbody>{''.join(body_rows)}</tbody></table>"
        )
    else:
        table = (
            f'<div style="color:{MUTED};font-size:13px;line-height:1.7">'
            "No remediations yet. Trigger one by labelling an issue "
            f'<span style="color:{AMBER}">{_esc(settings.trigger_label)}</span> in '
            f'<span style="color:{AMBER}">{_esc(settings.target_repo)}</span>, '
            f'or run <span style="color:{AMBER}">./simulate.sh</span> to replay a sample payload.'
            "</div>"
        )

    audit_rows = "".join(
        f'<div style="padding:5px 0;border-top:1px solid {LINE};font-size:11px;color:{TEXT}">'
        f'<span style="color:{MUTED}">{time.strftime("%H:%M:%S", time.localtime(event["ts"]))}</span> '
        f'<span style="color:{AMBER}">{_esc(event["kind"])}</span> '
        f'<span style="color:{MUTED}">{_esc(("#" + str(event["issue_number"])) if event["issue_number"] else "")}</span> '
        f'{_esc(event.get("detail"))}</div>'
        for event in events
    ) or f'<div style="color:{MUTED};font-size:12px">no events yet</div>'

    log_rows = "".join(
        f'<div style="padding:3px 0;font-size:11px;color:{RED if entry["level"] == "ERROR" else (AMBER if entry["level"] == "WARNING" else TEXT)};'
        'white-space:pre-wrap;word-break:break-word">'
        f'{_esc(entry["message"])}</div>'
        for entry in logs
    ) or f'<div style="color:{MUTED};font-size:12px">no log lines yet</div>'

    def panel(title: str, content: str) -> str:
        return (
            f'<div style="background:{PANEL};border:1px solid {LINE};border-radius:6px;padding:14px 16px;overflow:auto;max-height:420px">'
            f'<div style="color:{MUTED};font-size:10px;letter-spacing:.18em;margin-bottom:8px">{title}</div>'
            f"{content}</div>"
        )

    return f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta http-equiv="refresh" content="10">
<title>Devin Automations</title>
</head>
<body style="margin:0;background:{BG};color:{TEXT};font-family:ui-monospace,SFMono-Regular,Menlo,Consolas,monospace">
<div style="max-width:1280px;margin:0 auto;padding:22px 26px 48px">
  {header}
  {pipeline}
  {cards_grid}
  <div style="background:{PANEL};border:1px solid {LINE};border-radius:6px;padding:14px 16px;margin:18px 0">
    <div style="color:{MUTED};font-size:10px;letter-spacing:.18em;margin-bottom:8px">REMEDIATIONS</div>
    {table}
  </div>
  <div style="display:grid;grid-template-columns:1fr 1fr;gap:14px">
    {panel("AUDIT TRAIL", audit_rows)}
    {panel("LIVE LOGS", log_rows)}
  </div>
</div>
</body>
</html>
"""
