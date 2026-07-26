"""Environment-driven configuration for Devin Automations."""

from __future__ import annotations

import os
from dataclasses import dataclass
from functools import lru_cache


def _env(name: str, default: str = "") -> str:
    return os.environ.get(name, default)


def _env_bool(name: str, default: bool) -> bool:
    raw = os.environ.get(name)
    if raw is None or raw == "":
        return default
    return raw.strip().lower() in ("1", "true", "yes", "on")


def _env_int(name: str, default: int) -> int:
    try:
        return int(os.environ[name])
    except (KeyError, ValueError):
        return default


def _env_float(name: str, default: float) -> float:
    try:
        return float(os.environ[name])
    except (KeyError, ValueError):
        return default


def _env_secret(name: str, default: str = "") -> str:
    """Read a secret from <NAME>_FILE when set, otherwise from <NAME>.

    Supports Docker/Kubernetes secret files without ever putting the value in
    the environment or the image.
    """
    path = os.environ.get(f"{name}_FILE", "").strip()
    if path:
        try:
            with open(path, "r", encoding="utf-8") as handle:
                return handle.read().strip()
        except OSError:
            return default
    return os.environ.get(name, default)


@dataclass(frozen=True)
class Settings:
    devin_api_base: str = "https://api.devin.ai/v3"
    devin_api_key: str = ""
    devin_org_id: str = ""
    github_token: str = ""
    github_webhook_secret: str = ""
    target_repo: str = "priyalwalpita/superset"
    base_branch: str = "master"
    trigger_label: str = "devin:remediate"
    max_acu_limit: int = 5
    max_concurrent_sessions: int = 2
    poll_interval_seconds: int = 20
    acu_cost_usd: float = 2.00
    enable_devin_review: bool = False
    bypass_approval: bool = True
    mock_devin: bool = False
    db_path: str = "data/automations.db"


def load_settings() -> Settings:
    return Settings(
        devin_api_base=_env("DEVIN_API_BASE", "https://api.devin.ai/v3").rstrip("/"),
        devin_api_key=_env_secret("DEVIN_API_KEY"),
        devin_org_id=_env("DEVIN_ORG_ID"),
        github_token=_env_secret("GITHUB_TOKEN"),
        github_webhook_secret=_env_secret("GITHUB_WEBHOOK_SECRET"),
        target_repo=_env("TARGET_REPO", "priyalwalpita/superset"),
        base_branch=_env("BASE_BRANCH", "master"),
        trigger_label=_env("TRIGGER_LABEL", "devin:remediate"),
        max_acu_limit=_env_int("MAX_ACU_LIMIT", 5),
        max_concurrent_sessions=_env_int("MAX_CONCURRENT_SESSIONS", 2),
        poll_interval_seconds=_env_int("POLL_INTERVAL_SECONDS", 20),
        acu_cost_usd=_env_float("ACU_COST_USD", 2.00),
        enable_devin_review=_env_bool("ENABLE_DEVIN_REVIEW", False),
        bypass_approval=_env_bool("BYPASS_APPROVAL", True),
        mock_devin=_env_bool("MOCK_DEVIN", False),
        db_path=_env("DB_PATH", "data/automations.db"),
    )


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return load_settings()


settings = get_settings()
