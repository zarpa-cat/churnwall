"""Integration dispatcher for churnwall.

Routes retention recommendations to the right channel:
  - Email (Resend) for all actionable recommendations
  - Slack for immediate-urgency recommendations only

Routing logic:
  IMMEDIATE urgency  → email + Slack
  SOON urgency       → email only
  MONITOR urgency    → no send (observation only)

Email recipient is resolved from subscriber.app_user_id (treated as email)
if it looks like an email address. Otherwise the send is skipped with a warning.
In production you'd join to your user table here.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass

from churnwall.integrations.resend import (
    EmailMessage,
    ResendClient,
    billing_failure_alert,
    engagement_checkin,
    loyalty_discount,
    renewal_reminder,
    trial_conversion_nudge,
    winback_offer,
)
from churnwall.integrations.slack import SlackClient
from churnwall.models import Subscriber
from churnwall.recommender import ActionType, Recommendation, Urgency
from churnwall.settings import Settings

logger = logging.getLogger(__name__)

_EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


def _looks_like_email(value: str | None) -> bool:
    if not value:
        return False
    return bool(_EMAIL_RE.match(value))


@dataclass
class DispatchResult:
    customer_id: str
    action: str
    email_sent: bool
    slack_sent: bool
    email_skipped_reason: str | None = None


class IntegrationDispatcher:
    """Routes recommendations to Resend and/or Slack.

    Args:
        resend: Configured ResendClient instance.
        slack: Configured SlackClient instance.
        app_name: App name used in email copy.
    """

    def __init__(
        self,
        resend: ResendClient,
        slack: SlackClient,
        app_name: str = "your app",
    ) -> None:
        self._resend = resend
        self._slack = slack
        self._app_name = app_name

    @classmethod
    def from_settings(
        cls, settings: Settings, app_name: str = "your app"
    ) -> "IntegrationDispatcher":
        """Construct from application settings (convenience factory)."""
        resend = ResendClient(
            api_key=settings.resend_api_key,
            from_email=settings.resend_from_email,
        )
        slack = SlackClient(webhook_url=settings.slack_webhook_url)
        return cls(resend=resend, slack=slack, app_name=app_name)

    async def dispatch(
        self,
        subscriber: Subscriber,
        recommendation: Recommendation,
        risk_score: float = 0.0,
    ) -> DispatchResult:
        """Dispatch a single recommendation for a subscriber.

        Args:
            subscriber: The subscriber record.
            recommendation: The recommendation to act on.
            risk_score: Current risk score (for Slack alert context).

        Returns:
            DispatchResult describing what was sent.
        """
        customer_id = subscriber.customer_id
        action = recommendation.action
        urgency = recommendation.urgency
        meta = recommendation.metadata

        email_sent = False
        slack_sent = False
        email_skipped_reason: str | None = None

        # ── Resolve recipient email ──────────────────────────────────────────
        # app_user_id is the best candidate in our model.
        # In a real integration, join to your user table here.
        recipient = subscriber.app_user_id
        if not _looks_like_email(recipient):
            email_skipped_reason = (
                f"app_user_id '{recipient}' is not a valid email address; skipping email"
            )
            logger.warning("churnwall.dispatcher: %s — %s", customer_id, email_skipped_reason)
            recipient = None

        # ── MONITOR → no-op ──────────────────────────────────────────────────
        if urgency == Urgency.MONITOR:
            logger.debug("churnwall.dispatcher: %s — MONITOR urgency, no send", customer_id)
            return DispatchResult(
                customer_id=customer_id,
                action=action.value,
                email_sent=False,
                slack_sent=False,
                email_skipped_reason="MONITOR urgency — no action taken",
            )

        # ── Build email message ──────────────────────────────────────────────
        message: EmailMessage | None = None

        if action == ActionType.SEND_BILLING_FAILURE_ALERT and recipient:
            message = billing_failure_alert(
                to=recipient,
                customer_id=customer_id,
                failure_count=int(meta.get("failure_count", 1)),
                app_name=self._app_name,
            )
        elif action == ActionType.SEND_WINBACK_OFFER and recipient:
            message = winback_offer(
                to=recipient,
                customer_id=customer_id,
                discount_pct=int(meta.get("discount_pct", 30)),
                app_name=self._app_name,
            )
        elif action == ActionType.SEND_TRIAL_CONVERSION_NUDGE and recipient:
            message = trial_conversion_nudge(
                to=recipient,
                customer_id=customer_id,
                discount_pct=int(meta.get("discount_pct", 20)),
                app_name=self._app_name,
            )
        elif action == ActionType.SEND_TRIAL_FEATURE_HIGHLIGHT and recipient:
            # Reuse trial nudge copy without discount
            message = trial_conversion_nudge(
                to=recipient,
                customer_id=customer_id,
                discount_pct=0,
                app_name=self._app_name,
            )
        elif action == ActionType.SEND_LOYALTY_DISCOUNT and recipient:
            message = loyalty_discount(
                to=recipient,
                customer_id=customer_id,
                discount_pct=int(meta.get("discount_pct", 25)),
                renewal_count=subscriber.renewal_count or 0,
                app_name=self._app_name,
            )
        elif action == ActionType.SEND_ENGAGEMENT_CHECKIN and recipient:
            message = engagement_checkin(
                to=recipient,
                customer_id=customer_id,
                app_name=self._app_name,
            )
        elif action == ActionType.SEND_RENEWAL_REMINDER and recipient:
            message = renewal_reminder(
                to=recipient,
                customer_id=customer_id,
                app_name=self._app_name,
            )
        elif action == ActionType.PROMPT_PAYMENT_UPDATE and recipient:
            # Billing recovery — reuse billing failure alert
            message = billing_failure_alert(
                to=recipient,
                customer_id=customer_id,
                failure_count=int(meta.get("failure_count", 1)),
                app_name=self._app_name,
            )
        else:
            if recipient is None and email_skipped_reason is None:
                email_skipped_reason = f"No email template for action {action.value}"

        # ── Send email ────────────────────────────────────────────────────────
        if message:
            try:
                result = await self._resend.send(message)
                email_sent = bool(result)
            except Exception as exc:
                logger.error("churnwall.dispatcher: email send failed for %s: %s", customer_id, exc)

        # ── Send Slack alert (immediate only) ─────────────────────────────────
        if urgency == Urgency.IMMEDIATE:
            try:
                slack_sent = await self._slack.post_alert(
                    customer_id=customer_id,
                    state=subscriber.state.value,
                    risk_score=risk_score,
                    action=action.value,
                    reason=recommendation.reason,
                    urgency=urgency.value,
                    extra={k: v for k, v in meta.items() if k != "channel"},
                )
            except Exception as exc:
                logger.error(
                    "churnwall.dispatcher: Slack alert failed for %s: %s", customer_id, exc
                )

        return DispatchResult(
            customer_id=customer_id,
            action=action.value,
            email_sent=email_sent,
            slack_sent=slack_sent,
            email_skipped_reason=email_skipped_reason,
        )

    async def dispatch_top(
        self,
        subscriber: Subscriber,
        risk_score: float,
        recommendations: list[Recommendation],
    ) -> DispatchResult | None:
        """Dispatch only the top recommendation (highest priority).

        Returns None if recommendations list is empty.
        """
        if not recommendations:
            return None
        top = sorted(recommendations, key=lambda r: r.priority)[0]
        return await self.dispatch(subscriber, top, risk_score=risk_score)
