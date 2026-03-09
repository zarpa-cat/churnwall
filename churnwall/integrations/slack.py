"""Slack integration for churnwall.

Posts alert messages to a Slack channel via an incoming webhook URL.
Only fires for `immediate` urgency recommendations — high-urgency signals
that need human attention: billing failures and critical churn risk.

Monitoring-only and 'soon' urgency recommendations are handled by email.

Usage:
    client = SlackClient(webhook_url="https://hooks.slack.com/services/...")
    await client.post_alert(
        customer_id="usr_abc123",
        state="billing_issue",
        risk_score=87.5,
        action="send_billing_failure_alert",
        reason="Billing has failed. Alert the subscriber immediately.",
        failure_count=2,
    )
"""

from __future__ import annotations

import logging

import httpx

logger = logging.getLogger(__name__)


class SlackClient:
    """Async Slack incoming webhook client.

    Args:
        webhook_url: Slack incoming webhook URL.
                     If None, all posts are no-ops.
        http_client: Optional injected httpx.AsyncClient for testing.
    """

    def __init__(
        self,
        webhook_url: str | None,
        http_client: httpx.AsyncClient | None = None,
    ) -> None:
        self._webhook_url = webhook_url
        self._client = http_client

    @property
    def configured(self) -> bool:
        return bool(self._webhook_url)

    async def post(self, payload: dict) -> bool:
        """Post a raw payload dict to the Slack webhook.

        Returns:
            True on success, False if not configured or send skipped.

        Raises:
            httpx.HTTPStatusError: If Slack returns a non-2xx response.
        """
        if not self.configured:
            logger.warning("SLACK_WEBHOOK_URL not set — skipping Slack alert")
            return False

        if self._client is not None:
            response = await self._client.post(
                self._webhook_url,  # type: ignore[arg-type]
                json=payload,
                headers={"Content-Type": "application/json"},
            )
            response.raise_for_status()
            return True

        async with httpx.AsyncClient(timeout=10.0) as client:
            response = await client.post(
                self._webhook_url,  # type: ignore[arg-type]
                json=payload,
                headers={"Content-Type": "application/json"},
            )
            response.raise_for_status()
            return True

    async def post_alert(
        self,
        *,
        customer_id: str,
        state: str,
        risk_score: float,
        action: str,
        reason: str,
        urgency: str = "immediate",
        extra: dict | None = None,
    ) -> bool:
        """Post a structured churnwall alert to Slack.

        Formats a rich Block Kit message with subscriber context and the
        recommended action. Only call this for `immediate` urgency.
        """
        extra = extra or {}

        # Risk score → emoji
        if risk_score >= 80:
            risk_emoji = "🔴"
        elif risk_score >= 60:
            risk_emoji = "🟠"
        elif risk_score >= 40:
            risk_emoji = "🟡"
        else:
            risk_emoji = "🟢"

        action_display = action.replace("_", " ").title()
        state_display = state.replace("_", " ").title()

        # Extra context lines (billing failures, discount %, etc.)
        extra_lines = ""
        if extra:
            extra_lines = "\n".join(
                f"• *{k.replace('_', ' ').title()}:* {v}" for k, v in extra.items()
            )

        blocks = [
            {
                "type": "header",
                "text": {
                    "type": "plain_text",
                    "text": f"⚠️ Churnwall Alert — {action_display}",
                    "emoji": True,
                },
            },
            {
                "type": "section",
                "fields": [
                    {
                        "type": "mrkdwn",
                        "text": f"*Customer:*\n`{customer_id}`",
                    },
                    {
                        "type": "mrkdwn",
                        "text": f"*State:*\n{state_display}",
                    },
                    {
                        "type": "mrkdwn",
                        "text": f"*Risk Score:*\n{risk_emoji} {risk_score:.1f}/100",
                    },
                    {
                        "type": "mrkdwn",
                        "text": f"*Urgency:*\n{urgency.upper()}",
                    },
                ],
            },
            {
                "type": "section",
                "text": {
                    "type": "mrkdwn",
                    "text": f"*Reason:* {reason}",
                },
            },
        ]

        if extra_lines:
            blocks.append(
                {
                    "type": "section",
                    "text": {
                        "type": "mrkdwn",
                        "text": f"*Context:*\n{extra_lines}",
                    },
                }
            )

        blocks.append({"type": "divider"})

        payload = {
            "text": f"Churnwall alert: {action_display} for {customer_id}",
            "blocks": blocks,
        }

        return await self.post(payload)
