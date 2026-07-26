"""FastAPI orchestrator: webhook intake, session launcher, poller, dashboard, metrics."""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import hmac
import json
import logging
import sys
import time
from contextlib import asynccontextmanager
from typing import Any, Optional

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse, PlainTextResponse

from app import dashboard, db, observability
from app.config import settings
from app.devin_client import make_client
from app.github_client import GitHubClient
from app.prompts import build_prompt

log = logging.getLogger("automations")

devin = make_client()
github = GitHubClient()

_LIMIT_MARKERS = ("acu", "limit", "quota", "credit", "budget")
_launch_lock = asyncio.Lock()

# A session whose polls keep failing is given up on rather than holding a slot forever.
MAX_CONSECUTIVE_POLL_FAILURES = 5
_poll_failures: dict[int, int] = {}


def _configure_logging() -> None:
    log.setLevel(logging.INFO)
    log.propagate = False
    formatter = logging.Formatter("%(asctime)s %(levelname)s %(message)s", "%H:%M:%S")
    if not any(isinstance(h, logging.StreamHandler) for h in log.handlers):
        stream = logging.StreamHandler(sys.stdout)
        stream.setFormatter(formatter)
        log.addHandler(stream)
    if not any(isinstance(h, observability.RingBufferHandler) for h in log.handlers):
        ring = observability.RingBufferHandler()
        ring.setFormatter(formatter)
        log.addHandler(ring)


@asynccontextmanager
async def lifespan(app: FastAPI):
    _configure_logging()
    db.init_db()
    stranded = db.requeue_stranded_launches()
    if stranded:
        log.warning("requeued %s remediation(s) stranded in launching", stranded)
    log.info(
        "devin automations starting | mode=%s repo=%s label=%s concurrency=%s acu_cap=%s",
        "mock" if settings.mock_devin else "live",
        settings.target_repo,
        settings.trigger_label,
        settings.max_concurrent_sessions,
        settings.max_acu_limit,
    )
    task = asyncio.create_task(poller())
    try:
        yield
    finally:
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task


app = FastAPI(title="Devin Automations", lifespan=lifespan)


def _verify_signature(raw: bytes, signature: Optional[str]) -> bool:
    if not settings.github_webhook_secret:
        log.warning("GITHUB_WEBHOOK_SECRET unset — accepting unsigned webhook (dev mode)")
        return True
    if not signature:
        return False
    expected = "sha256=" + hmac.new(
        settings.github_webhook_secret.encode(), raw, hashlib.sha256
    ).hexdigest()
    return hmac.compare_digest(expected, signature)


@app.post("/webhook")
async def webhook(request: Request) -> JSONResponse:
    raw = await request.body()
    delivery = request.headers.get("X-GitHub-Delivery", "unknown")
    event = request.headers.get("X-GitHub-Event", "")
    db.log_event(None, "webhook_received", f"delivery={delivery} event={event or 'none'}")

    if not _verify_signature(raw, request.headers.get("X-Hub-Signature-256")):
        db.log_event(None, "webhook_ignored", "invalid signature")
        log.warning("rejected webhook %s: invalid signature", delivery)
        return JSONResponse({"status": "invalid_signature"}, status_code=401)

    if event != "issues":
        db.log_event(None, "webhook_ignored", f"event={event or 'none'}")
        return JSONResponse({"status": "ignored", "reason": "not an issues event"})

    try:
        payload: dict[str, Any] = json.loads(raw or b"{}")
    except json.JSONDecodeError:
        db.log_event(None, "webhook_ignored", "malformed json")
        return JSONResponse({"status": "ignored", "reason": "malformed json"})

    action = payload.get("action")
    label = (payload.get("label") or {}).get("name")
    if action != "labeled" or label != settings.trigger_label:
        db.log_event(None, "webhook_ignored", f"action={action} label={label}")
        return JSONResponse({"status": "ignored", "reason": f"action={action} label={label}"})

    issue = payload.get("issue") or {}
    issue_number = issue.get("number")
    if not isinstance(issue_number, int):
        db.log_event(None, "webhook_ignored", "missing issue number")
        return JSONResponse({"status": "ignored", "reason": "missing issue number"})

    title = issue.get("title") or f"Issue #{issue_number}"
    created = db.enqueue(issue_number, title, issue.get("body") or "", issue.get("html_url") or "")
    if not created:
        db.log_event(issue_number, "webhook_duplicate", f"delivery={delivery}")
        log.info("issue #%s already tracked — ignoring duplicate", issue_number)
        return JSONResponse({"status": "duplicate_ignored", "issue_number": issue_number})

    db.log_event(issue_number, "queued", title[:200])
    log.info("queued issue #%s: %s", issue_number, title)
    asyncio.create_task(process_queue())
    return JSONResponse({"status": "queued", "issue_number": issue_number})


async def process_queue() -> None:
    """Launch queued remediations while under the concurrency limit.

    The lock plus the conditional claim keep the webhook-triggered and
    poller-triggered invocations from launching the same row twice.
    """
    async with _launch_lock:
        active = len(db.rows(states=db.ACTIVE_STATES))
        for row in db.rows(states=("queued",)):
            if active >= settings.max_concurrent_sessions:
                log.info(
                    "concurrency gate: %s active sessions, issue #%s stays queued",
                    active,
                    row["issue_number"],
                )
                break
            if not db.claim_queued(row["issue_number"]):
                continue
            active += 1
            await _launch(row)


async def _launch(row: dict[str, Any]) -> None:
    """Start a session for a row already claimed into the launching state."""
    issue_number = row["issue_number"]
    prompt = build_prompt(issue_number, row["issue_title"] or "", row["issue_body"] or "")
    title = f"Remediate {settings.target_repo}#{issue_number}: {(row['issue_title'] or '')[:80]}"
    try:
        session = await devin.create_session(
            prompt=prompt,
            title=title,
            tags=["devin-automations", f"issue-{issue_number}"],
        )
    except Exception as exc:  # noqa: BLE001 - surfaced as an honest failure state
        message = str(exc)[:500]
        db.update(issue_number, state="failed", error=message, completed_ts=time.time())
        db.log_event(issue_number, "failed", f"launch failed: {message}")
        log.error("failed to launch session for #%s: %s", issue_number, message)
        await github.comment(
            issue_number,
            f"⚠️ Devin Automations could not start a remediation session.\n\n```\n{message}\n```",
        )
        return

    session_id = session.get("session_id") or session.get("id") or ""
    session_url = session.get("url") or f"https://app.devin.ai/sessions/{session_id}"
    db.update(
        issue_number,
        state="running",
        session_id=session_id,
        session_url=session_url,
        devin_status=session.get("status") or "running",
        launched_ts=time.time(),
    )
    db.log_event(issue_number, "session_started", session_url)
    log.info("session %s started for issue #%s", session_id, issue_number)
    await github.comment(
        issue_number,
        (
            f"🤖 **Devin Automations** started a remediation session for this finding.\n\n"
            f"- Session: {session_url}\n"
            f"- ACU cap: {settings.max_acu_limit}\n"
            f"- Target: `{settings.target_repo}` @ `{settings.base_branch}`\n\n"
            "A pull request will be linked here when it is opened."
        ),
    )


def _is_limit_failure(status: str, detail: str) -> bool:
    if status != "suspended":
        return False
    lowered = (detail or "").lower()
    return any(marker in lowered for marker in _LIMIT_MARKERS)


async def sync_session(row: dict[str, Any]) -> None:
    """Poll one session and reconcile its state, PR and completion."""
    issue_number = row["issue_number"]
    session_id = row["session_id"]
    if not session_id:
        return
    try:
        session = await devin.get_session(session_id)
    except Exception as exc:  # noqa: BLE001 - transient API errors must not kill the poller
        failures = _poll_failures.get(issue_number, 0) + 1
        _poll_failures[issue_number] = failures
        log.warning(
            "poll failed for #%s (%s), attempt %s/%s: %s",
            issue_number,
            session_id,
            failures,
            MAX_CONSECUTIVE_POLL_FAILURES,
            exc,
        )
        if failures >= MAX_CONSECUTIVE_POLL_FAILURES:
            await _give_up(issue_number, f"session unreachable after {failures} polls: {exc}")
        return

    _poll_failures.pop(issue_number, None)

    status = (session.get("status") or "").lower()
    detail = (session.get("status_detail") or "").lower()
    acus = session.get("acus_consumed")

    updates: dict[str, Any] = {"devin_status": status, "devin_detail": detail}
    if acus is not None:
        updates["acus_consumed"] = float(acus)

    pull_requests = session.get("pull_requests") or []
    if pull_requests and not row.get("pr_url"):
        first = pull_requests[0]
        if isinstance(first, dict):
            pr_url = first.get("pr_url") or first.get("url")
            pr_state = first.get("pr_state") or first.get("state")
        else:
            pr_url, pr_state = str(first), None
        updates.update({"pr_url": pr_url, "pr_state": pr_state, "pr_ts": time.time()})
        if not db.has_event(issue_number, "pr_opened"):
            db.log_event(issue_number, "pr_opened", pr_url or "")
            log.info("PR opened for issue #%s: %s", issue_number, pr_url)
            await github.comment(issue_number, f"🔧 Pull request opened for this finding: {pr_url}")
            if settings.enable_devin_review and pr_url:
                await devin.trigger_devin_review(pr_url)
                db.log_event(issue_number, "devin_review_triggered", pr_url)

    if status == "error" or _is_limit_failure(status, detail):
        reason = session.get("status_detail") or session.get("error") or status or "unknown error"
        updates.update({"state": "failed", "error": str(reason)[:500], "completed_ts": time.time()})
        db.update(issue_number, **updates)
        if not db.has_event(issue_number, "failed"):
            db.log_event(issue_number, "failed", str(reason)[:200])
            log.error("session failed for issue #%s: %s", issue_number, reason)
            await github.comment(
                issue_number,
                f"❌ **Devin Automations** session ended without a fix.\n\nReason: `{reason}`",
            )
        return

    finished = detail == "finished" or status in ("exit", "exited", "finished", "completed")
    if finished:
        structured = session.get("structured_output")
        updates.update(
            {
                "state": "completed",
                "completed_ts": time.time(),
                "structured_out": json.dumps(structured) if structured is not None else None,
            }
        )
        db.update(issue_number, **updates)
        if not db.has_event(issue_number, "completed"):
            outcome = (structured or {}).get("outcome", "unknown")
            db.log_event(issue_number, "completed", f"outcome={outcome}")
            log.info("issue #%s completed with outcome=%s", issue_number, outcome)
            structured = structured or {}
            await github.comment(
                issue_number,
                (
                    f"✅ **Devin Automations** finished this remediation.\n\n"
                    f"- Outcome: `{outcome}`\n"
                    f"- Summary: {structured.get('summary', 'n/a')}\n"
                    f"- Verification: {structured.get('verification', 'n/a')}\n"
                    f"- Risk notes: {structured.get('risk_notes', 'n/a')}\n"
                    f"- ACUs: {updates.get('acus_consumed', row.get('acus_consumed')) or 'n/a'}"
                ),
            )
        return

    if detail in ("waiting_for_user", "waiting_for_approval") or status in (
        "waiting_for_user",
        "waiting_for_approval",
    ):
        updates["state"] = "blocked"
        db.update(issue_number, **updates)
        if not db.has_event(issue_number, "blocked"):
            db.log_event(issue_number, "blocked", detail or status)
            log.warning("issue #%s blocked: %s", issue_number, detail or status)
            await github.comment(
                issue_number,
                f"⏸️ **Devin Automations** session needs human input (`{detail or status}`): {row.get('session_url')}",
            )
        return

    updates["state"] = "running"
    db.update(issue_number, **updates)


async def _give_up(issue_number: int, reason: str) -> None:
    """Fail a row whose session can no longer be reached so it releases its slot."""
    _poll_failures.pop(issue_number, None)
    db.update(issue_number, state="failed", error=reason[:500], completed_ts=time.time())
    if not db.has_event(issue_number, "failed"):
        db.log_event(issue_number, "failed", reason[:200])
        log.error("giving up on issue #%s: %s", issue_number, reason)
        await github.comment(
            issue_number,
            f"❌ **Devin Automations** lost track of this remediation session.\n\nReason: `{reason}`",
        )


async def sync_all() -> None:
    for row in db.rows(states=("running", "blocked")):
        await sync_session(row)


async def poller() -> None:
    while True:
        observability.mark_poll()
        try:
            await process_queue()
            await sync_all()
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 - the poller must survive everything
            log.error("poller cycle error: %s", exc)
        await asyncio.sleep(settings.poll_interval_seconds)


@app.get("/dashboard", response_class=HTMLResponse)
async def dashboard_view() -> HTMLResponse:
    return HTMLResponse(
        dashboard.render(
            metrics=db.metrics(),
            remediations=db.rows(),
            events=db.recent_events(40),
            logs=observability.recent_logs(40),
            lag=observability.poller_lag_seconds(),
        )
    )


@app.get("/metrics")
async def metrics_view(format: str = "json"):
    data = db.metrics()
    lag = observability.poller_lag_seconds()
    if format == "prometheus":
        return PlainTextResponse(
            observability.prometheus_text(data, lag),
            media_type="text/plain; version=0.0.4; charset=utf-8",
        )
    return JSONResponse(
        {**data, "poller_lag_seconds": lag, "mode": "mock" if settings.mock_devin else "live"}
    )


@app.get("/logs")
async def logs_view() -> JSONResponse:
    return JSONResponse({"logs": observability.recent_logs(100)})


@app.get("/healthz")
async def healthz() -> JSONResponse:
    return JSONResponse(
        {
            "ok": True,
            "mock": settings.mock_devin,
            "repo": settings.target_repo,
            "last_poll_seconds_ago": observability.poller_lag_seconds(),
        }
    )


@app.get("/")
async def root() -> JSONResponse:
    return JSONResponse(
        {
            "service": "devin-automations",
            "dashboard": "/dashboard",
            "metrics": "/metrics",
            "logs": "/logs",
            "health": "/healthz",
        }
    )
