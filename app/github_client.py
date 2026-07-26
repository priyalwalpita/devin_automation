"""Minimal GitHub issue client. Never raises; no-ops when no token is configured."""

from __future__ import annotations

import logging

import httpx

from app.config import settings

log = logging.getLogger("automations")

API_ROOT = "https://api.github.com"
TIMEOUT = 30.0


class GitHubClient:
    def __init__(self) -> None:
        self.repo = settings.target_repo
        self.token = settings.github_token

    @property
    def _headers(self) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {self.token}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
        }

    async def comment(self, issue_number: int, body: str) -> None:
        if not self.token:
            log.info("[no GITHUB_TOKEN] would comment on %s#%s: %s", self.repo, issue_number, body.splitlines()[0])
            return
        url = f"{API_ROOT}/repos/{self.repo}/issues/{issue_number}/comments"
        try:
            async with httpx.AsyncClient(timeout=TIMEOUT) as client:
                response = await client.post(url, json={"body": body}, headers=self._headers)
            if response.status_code >= 400:
                log.warning("github comment failed (%s): %s", response.status_code, response.text[:200])
        except Exception as exc:  # noqa: BLE001 - GitHub must never break remediation
            log.warning("github comment error: %s", exc)

    async def set_label(self, issue_number: int, label: str) -> None:
        if not self.token:
            log.info("[no GITHUB_TOKEN] would label %s#%s with %s", self.repo, issue_number, label)
            return
        url = f"{API_ROOT}/repos/{self.repo}/issues/{issue_number}/labels"
        try:
            async with httpx.AsyncClient(timeout=TIMEOUT) as client:
                response = await client.post(url, json={"labels": [label]}, headers=self._headers)
            if response.status_code >= 400:
                log.warning("github label failed (%s): %s", response.status_code, response.text[:200])
        except Exception as exc:  # noqa: BLE001
            log.warning("github label error: %s", exc)
