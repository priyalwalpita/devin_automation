"""Devin API client plus an in-memory mock used for keyless demos and tests."""

from __future__ import annotations

import logging
import re
import secrets
import time
from typing import Any, Optional

import httpx

from app.config import settings
from app.prompts import STRUCTURED_OUTPUT_SCHEMA

log = logging.getLogger("automations")

TIMEOUT = 30.0


class DevinClient:
    """Thin async wrapper over the Devin sessions API."""

    def __init__(self) -> None:
        self.base = settings.devin_api_base.rstrip("/")
        self.org_id = settings.devin_org_id
        self.headers = {
            "Authorization": f"Bearer {settings.devin_api_key}",
            "Content-Type": "application/json",
        }

    @property
    def _sessions_url(self) -> str:
        if not self.org_id:
            raise RuntimeError("DEVIN_ORG_ID is not configured")
        return f"{self.base}/organizations/{self.org_id}/sessions"

    async def create_session(
        self, prompt: str, title: str, tags: Optional[list[str]] = None
    ) -> dict[str, Any]:
        payload = {
            "prompt": prompt,
            "title": title,
            "tags": tags or [],
            "max_acu_limit": settings.max_acu_limit,
            "structured_output_schema": STRUCTURED_OUTPUT_SCHEMA,
            "structured_output_required": True,
            "bypass_approval": settings.bypass_approval,
        }
        async with httpx.AsyncClient(timeout=TIMEOUT) as client:
            response = await client.post(self._sessions_url, json=payload, headers=self.headers)
        if response.status_code >= 400:
            raise RuntimeError(
                f"create_session failed ({response.status_code}): {response.text}"
            )
        return response.json()

    async def get_session(self, session_id: str) -> dict[str, Any]:
        async with httpx.AsyncClient(timeout=TIMEOUT) as client:
            response = await client.get(
                f"{self._sessions_url}/{session_id}", headers=self.headers
            )
        if response.status_code >= 400:
            raise RuntimeError(
                f"get_session failed ({response.status_code}): {response.text}"
            )
        return response.json()

    async def trigger_devin_review(self, pr_url: str) -> None:
        """Best-effort Devin Review request; failures are swallowed by design."""
        try:
            async with httpx.AsyncClient(timeout=TIMEOUT) as client:
                await client.post(
                    f"{self.base}/organizations/{self.org_id}/pr-reviews",
                    json={"pr_url": pr_url},
                    headers=self.headers,
                )
        except Exception as exc:  # noqa: BLE001 - non-fatal, feature-flagged
            log.warning("devin review request failed for %s: %s", pr_url, exc)


class MockDevinClient:
    """Deterministic in-memory stand-in: no network, no key, time-driven state machine."""

    PR_AT = 15.0
    DONE_AT = 30.0

    def __init__(self) -> None:
        self._sessions: dict[str, dict[str, Any]] = {}

    async def create_session(
        self, prompt: str, title: str, tags: Optional[list[str]] = None
    ) -> dict[str, Any]:
        session_id = f"devin-mock-{secrets.token_hex(4)}"
        match = re.search(r"#(\d+)", title)
        issue_number = int(match.group(1)) if match else 0
        self._sessions[session_id] = {
            "session_id": session_id,
            "url": f"https://app.devin.ai/sessions/{session_id}",
            "created_at": time.time(),
            "issue_number": issue_number,
            "title": title,
        }
        log.info("mock session created: %s", session_id)
        return {
            "session_id": session_id,
            "url": self._sessions[session_id]["url"],
            "status": "running",
        }

    async def get_session(self, session_id: str) -> dict[str, Any]:
        session = self._sessions.get(session_id)
        if session is None:
            raise RuntimeError(f"unknown mock session {session_id}")

        elapsed = time.time() - session["created_at"]
        issue_number = session["issue_number"]
        payload: dict[str, Any] = {
            "session_id": session_id,
            "url": session["url"],
            "status": "running",
            "status_detail": "working",
            "pull_requests": [],
            "structured_output": None,
            "acus_consumed": round(min(2.4, 0.2 + elapsed * 0.08), 2),
        }

        if elapsed >= self.PR_AT:
            payload["pull_requests"] = [
                {
                    "url": f"https://github.com/{settings.target_repo}/pull/9{issue_number}",
                    "state": "open",
                }
            ]

        if elapsed >= self.DONE_AT:
            payload["status_detail"] = "finished"
            payload["acus_consumed"] = 2.4
            payload["structured_output"] = {
                "issue_number": issue_number,
                "outcome": "fixed",
                "pr_url": f"https://github.com/{settings.target_repo}/pull/9{issue_number}",
                "summary": (
                    f"Applied the minimal fix described in issue #{issue_number} and opened a "
                    "pull request with the change plus verification evidence."
                ),
                "files_changed": ["superset/utils/network.py", "tests/unit_tests/utils/network_test.py"],
                "risk_notes": "Timeout value chosen conservatively; no behavioural change on the happy path.",
                "verification": "ruff check on touched files; pytest for the touched module — both pass.",
            }

        return payload

    async def trigger_devin_review(self, pr_url: str) -> None:
        log.info("mock devin review requested for %s", pr_url)


def make_client() -> DevinClient | MockDevinClient:
    return MockDevinClient() if settings.mock_devin else DevinClient()
