"""Application settings loaded from environment variables.

All integration keys are optional. If not set, integrations degrade gracefully
(log a warning, skip the send). This means churnwall works without any external
services configured — you just won't get email/Slack notifications.
"""

from __future__ import annotations

import os


class Settings:
    """Thin wrapper around environment variables. No pydantic-settings required."""

    # ── Database ────────────────────────────────────────────────────────────
    @property
    def database_url(self) -> str:
        return os.environ.get("DATABASE_URL", "sqlite:///churnwall.db")

    # ── Resend (email) ───────────────────────────────────────────────────────
    @property
    def resend_api_key(self) -> str | None:
        return os.environ.get("RESEND_API_KEY")

    @property
    def resend_from_email(self) -> str:
        return os.environ.get("RESEND_FROM_EMAIL", "churnwall@example.com")

    # ── Slack ────────────────────────────────────────────────────────────────
    @property
    def slack_webhook_url(self) -> str | None:
        return os.environ.get("SLACK_WEBHOOK_URL")

    @property
    def slack_alerts_enabled(self) -> bool:
        val = os.environ.get("SLACK_ALERTS_ENABLED", "true").lower()
        return val in ("1", "true", "yes")

    # ── Churnwall ─────────────────────────────────────────────────────────────
    @property
    def churnwall_base_url(self) -> str:
        return os.environ.get("CHURNWALL_BASE_URL", "http://localhost:8000")

    @property
    def revenuecat_webhook_auth_key(self) -> str | None:
        return os.environ.get("RC_WEBHOOK_AUTH_KEY")


settings = Settings()
