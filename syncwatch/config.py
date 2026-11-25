"""Runtime configuration, read once from environment variables.

Everything has a safe default so the app runs with zero setup, but each
external dependency (CRM, Finance, alert webhook) can be pointed at a real
system by setting an env var - the same idea as a Power Platform
environment variable or connection reference.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def _float(name: str, default: float) -> float:
    try:
        return float(os.getenv(name, default))
    except ValueError:
        return default


@dataclass(frozen=True)
class Settings:
    db_path: str = field(default_factory=lambda: os.getenv("SYNCWATCH_DB", str(ROOT / "data" / "syncwatch.db")))

    # "internal" means: talk to the mock systems mounted inside this app.
    crm_base_url: str = field(default_factory=lambda: os.getenv("CRM_BASE_URL", "internal"))
    finance_base_url: str = field(default_factory=lambda: os.getenv("FINANCE_BASE_URL", "internal"))
    crm_client_id: str = field(default_factory=lambda: os.getenv("CRM_CLIENT_ID", "syncwatch-app"))
    crm_client_secret: str = field(default_factory=lambda: os.getenv("CRM_CLIENT_SECRET", "demo-secret"))
    finance_api_key: str = field(default_factory=lambda: os.getenv("FINANCE_API_KEY", "demo-finance-key"))

    # Retry policy for outbound HTTP calls.
    max_attempts: int = field(default_factory=lambda: int(os.getenv("RETRY_MAX_ATTEMPTS", "4")))
    retry_base_seconds: float = field(default_factory=lambda: _float("RETRY_BASE_SECONDS", 0.25))
    retry_max_wait_seconds: float = field(default_factory=lambda: _float("RETRY_MAX_WAIT_SECONDS", 2.0))
    http_timeout_seconds: float = field(default_factory=lambda: _float("HTTP_TIMEOUT_SECONDS", 10.0))

    # Optional Teams/Slack-style incoming webhook for failure alerts.
    alert_webhook_url: str | None = field(default_factory=lambda: os.getenv("ALERT_WEBHOOK_URL") or None)
    public_base_url: str = field(default_factory=lambda: os.getenv("PUBLIC_BASE_URL") or os.getenv("RENDER_EXTERNAL_URL") or "http://localhost:8000")

    # 0 disables the built-in scheduler.
    sync_interval_minutes: float = field(default_factory=lambda: _float("SYNC_INTERVAL_MINUTES", 0))


settings = Settings()
