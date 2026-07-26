"""In-process end-to-end test: webhook intake → session → PR → completion → telemetry."""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import os
import tempfile

import pytest

SECRET = "test-webhook-secret"

os.environ["MOCK_DEVIN"] = "true"
os.environ["GITHUB_WEBHOOK_SECRET"] = SECRET
os.environ["GITHUB_TOKEN"] = ""
os.environ["TARGET_REPO"] = "priyalwalpita/superset"
os.environ["POLL_INTERVAL_SECONDS"] = "1"
os.environ["DB_PATH"] = os.path.join(tempfile.mkdtemp(prefix="automations-test-"), "test.db")

from httpx import ASGITransport, AsyncClient  # noqa: E402

from app import db, observability  # noqa: E402
from app.devin_client import MockDevinClient  # noqa: E402
from app.main import _configure_logging, app, process_queue, sync_all  # noqa: E402

MockDevinClient.PR_AT = 2.0
MockDevinClient.DONE_AT = 4.0

ISSUE_NUMBER = 101
PAYLOAD = {
    "action": "labeled",
    "label": {"name": "devin:remediate"},
    "issue": {
        "number": ISSUE_NUMBER,
        "title": "Bandit B113: requests call without a timeout",
        "body": "Add an explicit timeout to the requests call in superset/utils/network.py.",
        "html_url": f"https://github.com/priyalwalpita/superset/issues/{ISSUE_NUMBER}",
    },
}


def _sign(raw: bytes) -> str:
    return "sha256=" + hmac.new(SECRET.encode(), raw, hashlib.sha256).hexdigest()


def _post_kwargs(payload: dict, event: str = "issues") -> dict:
    raw = json.dumps(payload).encode()
    return {
        "content": raw,
        "headers": {
            "X-GitHub-Event": event,
            "X-GitHub-Delivery": "test-delivery",
            "X-Hub-Signature-256": _sign(raw),
            "Content-Type": "application/json",
        },
    }


@pytest.fixture(scope="module")
def results() -> dict:
    async def run() -> dict:
        # ASGITransport does not run lifespan, so do the startup work explicitly.
        _configure_logging()
        db.init_db()
        collected: dict = {}
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            collected["queued"] = (await client.post("/webhook", **_post_kwargs(PAYLOAD))).json()
            collected["replay"] = (await client.post("/webhook", **_post_kwargs(PAYLOAD))).json()

            raw = json.dumps(PAYLOAD).encode()
            bad = await client.post(
                "/webhook",
                content=raw,
                headers={"X-GitHub-Event": "issues", "X-Hub-Signature-256": "sha256=deadbeef"},
            )
            collected["bad_signature_status"] = bad.status_code

            other = await client.post("/webhook", **_post_kwargs(PAYLOAD, event="push"))
            collected["other_event"] = other.json()

            # Drive the orchestration directly so the test never depends on poller timing.
            observability.mark_poll()
            await process_queue()
            collected["after_launch"] = db.rows(states=("running",))

            await asyncio.sleep(MockDevinClient.PR_AT + 0.3)
            await sync_all()
            collected["after_pr"] = db.rows()[0]

            await asyncio.sleep(MockDevinClient.DONE_AT - MockDevinClient.PR_AT + 0.3)
            await sync_all()
            collected["final_row"] = db.rows()[0]

            collected["metrics"] = (await client.get("/metrics")).json()
            collected["prometheus"] = (await client.get("/metrics?format=prometheus")).text
            collected["logs"] = (await client.get("/logs")).json()
            collected["healthz"] = (await client.get("/healthz")).json()
            collected["dashboard"] = (await client.get("/dashboard")).text
        return collected

    return asyncio.run(run())


def test_webhook_intake(results: dict) -> None:
    assert results["queued"] == {"status": "queued", "issue_number": ISSUE_NUMBER}
    assert results["replay"]["status"] == "duplicate_ignored"
    assert results["bad_signature_status"] == 401
    assert results["other_event"]["status"] == "ignored"


def test_session_launched(results: dict) -> None:
    running = results["after_launch"]
    assert len(running) == 1
    assert running[0]["session_id"].startswith("devin-mock-")
    assert running[0]["launched_ts"] is not None


def test_pull_request_captured(results: dict) -> None:
    row = results["after_pr"]
    assert row["pr_url"] == f"https://github.com/priyalwalpita/superset/pull/9{ISSUE_NUMBER}"
    assert row["pr_state"] == "open"
    assert row["pr_ts"] is not None


def test_completion_and_structured_output(results: dict) -> None:
    row = results["final_row"]
    assert row["state"] == "completed"
    assert row["completed_ts"] is not None
    structured = json.loads(row["structured_out"])
    assert structured["outcome"] == "fixed"
    assert structured["issue_number"] == ISSUE_NUMBER
    assert row["acus_consumed"] is not None


def test_metrics_coherent(results: dict) -> None:
    metrics = results["metrics"]
    assert metrics["total"] == 1
    assert metrics["by_state"]["completed"] == 1
    assert metrics["success_rate"] == 1.0
    assert metrics["prs_opened"] == 1
    assert metrics["median_seconds_to_pr"] is not None
    assert metrics["funnel"]["received"] >= 4
    assert metrics["funnel"]["duplicates"] == 1
    assert metrics["mode"] == "mock"


def test_observability_endpoints(results: dict) -> None:
    assert len(results["logs"]["logs"]) > 0
    assert "devin_automations_completed_total" in results["prometheus"]
    assert results["healthz"]["last_poll_seconds_ago"] is not None
    assert results["healthz"]["mock"] is True


def test_dashboard_render(results: dict) -> None:
    html = results["dashboard"]
    assert "DEVIN AUTOMATIONS" in html
    assert f"https://github.com/priyalwalpita/superset/pull/9{ISSUE_NUMBER}" in html
    assert "COMPLETED" in html
    assert "LIVE LOGS" in html
