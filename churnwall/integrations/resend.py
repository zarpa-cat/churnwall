"""Resend email integration for churnwall.

Sends transactional emails via the Resend API (https://resend.com).
All public methods are async. If RESEND_API_KEY is not configured, sends are
silently skipped and a warning is logged.

Email templates are intentionally plain-text-first with a minimal HTML wrapper.
No templating engine dependency — just f-strings. This keeps the package lean
and the content easy to audit.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

import httpx

logger = logging.getLogger(__name__)

RESEND_API_URL = "https://api.resend.com/emails"


@dataclass
class EmailMessage:
    to: str
    subject: str
    text: str          # Plain-text fallback (always required)
    html: str | None = None   # Optional HTML version


class ResendClient:
    """Async HTTP client wrapping the Resend emails API.

    Args:
        api_key: Resend API key. If None, all sends are no-ops.
        from_email: Sender address (must be verified in Resend dashboard).
        http_client: Optional injected httpx.AsyncClient for testing.
    """

    def __init__(
        self,
        api_key: str | None,
        from_email: str,
        http_client: httpx.AsyncClient | None = None,
    ) -> None:
        self._api_key = api_key
        self._from_email = from_email
        self._client = http_client  # injected; if None, created per-request

    @property
    def configured(self) -> bool:
        return bool(self._api_key)

    async def send(self, message: EmailMessage) -> dict:
        """Send a single email.

        Returns:
            Resend API response dict on success: {"id": "..."}
            Empty dict if not configured or send skipped.

        Raises:
            httpx.HTTPStatusError: If Resend returns a 4xx/5xx response.
        """
        if not self.configured:
            logger.warning(
                "RESEND_API_KEY not set — skipping email to %s (subject: %s)",
                message.to,
                message.subject,
            )
            return {}

        payload: dict = {
            "from": self._from_email,
            "to": [message.to],
            "subject": message.subject,
            "text": message.text,
        }
        if message.html:
            payload["html"] = message.html

        headers = {
            "Authorization": f"Bearer {self._api_key}",
            "Content-Type": "application/json",
        }

        if self._client is not None:
            response = await self._client.post(RESEND_API_URL, json=payload, headers=headers)
            response.raise_for_status()
            return response.json()

        async with httpx.AsyncClient(timeout=10.0) as client:
            response = await client.post(RESEND_API_URL, json=payload, headers=headers)
            response.raise_for_status()
            return response.json()


# ── Email templates ──────────────────────────────────────────────────────────
# Each template takes subscriber context and returns an EmailMessage.
# Keep them honest: no marketing fluff, just clear information.

def billing_failure_alert(
    to: str,
    customer_id: str,
    failure_count: int,
    app_name: str = "your app",
) -> EmailMessage:
    subject = f"Action required: billing issue with your {app_name} subscription"
    text = (
        f"Hi,\n\n"
        f"We hit a problem charging your payment method for your {app_name} subscription "
        f"(this is attempt {failure_count}).\n\n"
        f"To keep your subscription active, please update your payment details:\n"
        f"  → Open {app_name} → Settings → Billing → Update Payment Method\n\n"
        f"If you have questions, just reply to this email.\n\n"
        f"— {app_name} team\n\n"
        f"(Subscriber reference: {customer_id})"
    )
    html = f"""<p>Hi,</p>
<p>We hit a problem charging your payment method for your <strong>{app_name}</strong> subscription
(this is attempt {failure_count}).</p>
<p>To keep your subscription active, please update your payment details in the app under
<strong>Settings → Billing → Update Payment Method</strong>.</p>
<p>If you have questions, just reply to this email.</p>
<p>— {app_name} team</p>
<p style="color:#999;font-size:12px">Subscriber reference: {customer_id}</p>"""
    return EmailMessage(to=to, subject=subject, text=text, html=html)


def winback_offer(
    to: str,
    customer_id: str,
    discount_pct: int = 30,
    app_name: str = "your app",
) -> EmailMessage:
    subject = f"We miss you — here's {discount_pct}% off to come back"
    text = (
        f"Hi,\n\n"
        f"Your {app_name} subscription ended recently. We'd love to have you back.\n\n"
        f"For a limited time: {discount_pct}% off your first month when you resubscribe.\n\n"
        f"Just open {app_name} and resubscribe — the discount will apply automatically.\n\n"
        f"— {app_name} team\n\n"
        f"(Subscriber reference: {customer_id})"
    )
    html = f"""<p>Hi,</p>
<p>Your <strong>{app_name}</strong> subscription ended recently. We'd love to have you back.</p>
<p>For a limited time: <strong>{discount_pct}% off your first month</strong> when you
resubscribe. Just open the app — the discount will apply automatically.</p>
<p>— {app_name} team</p>
<p style="color:#999;font-size:12px">Subscriber reference: {customer_id}</p>"""
    return EmailMessage(to=to, subject=subject, text=text, html=html)


def trial_conversion_nudge(
    to: str,
    customer_id: str,
    discount_pct: int = 20,
    app_name: str = "your app",
) -> EmailMessage:
    subject = f"Your {app_name} trial — a quick note"
    text = (
        f"Hi,\n\n"
        f"You're currently on a free trial of {app_name}. Before it ends, "
        f"we wanted to check in.\n\n"
        f"If you're finding value in the app, now's a good time to subscribe. "
        f"We're offering {discount_pct}% off your first billing period as a trial user.\n\n"
        f"Open {app_name} → Settings → Subscription to convert.\n\n"
        f"Any questions? Just reply here.\n\n"
        f"— {app_name} team\n\n"
        f"(Subscriber reference: {customer_id})"
    )
    html = f"""<p>Hi,</p>
<p>You're currently on a free trial of <strong>{app_name}</strong>. Before it ends,
we wanted to check in.</p>
<p>If you're finding value in the app, we're offering <strong>{discount_pct}% off</strong>
your first billing period as a trial user.</p>
<p>Open the app → Settings → Subscription to convert.</p>
<p>Any questions? Just reply here.</p>
<p>— {app_name} team</p>
<p style="color:#999;font-size:12px">Subscriber reference: {customer_id}</p>"""
    return EmailMessage(to=to, subject=subject, text=text, html=html)


def loyalty_discount(
    to: str,
    customer_id: str,
    discount_pct: int = 25,
    renewal_count: int = 0,
    app_name: str = "your app",
) -> EmailMessage:
    months = renewal_count
    tenure = f"{months} month{'s' if months != 1 else ''}" if months else "a while"
    subject = f"A thank-you from {app_name}"
    text = (
        f"Hi,\n\n"
        f"You've been a {app_name} subscriber for {tenure} — thank you.\n\n"
        f"As a loyalty thank-you, we're giving you {discount_pct}% off your next renewal. "
        f"No action needed: it'll apply automatically.\n\n"
        f"— {app_name} team\n\n"
        f"(Subscriber reference: {customer_id})"
    )
    html = f"""<p>Hi,</p>
<p>You've been a <strong>{app_name}</strong> subscriber for {tenure} — thank you.</p>
<p>As a loyalty thank-you, we're giving you <strong>{discount_pct}% off</strong> your next
renewal. No action needed: it'll apply automatically.</p>
<p>— {app_name} team</p>
<p style="color:#999;font-size:12px">Subscriber reference: {customer_id}</p>"""
    return EmailMessage(to=to, subject=subject, text=text, html=html)


def engagement_checkin(
    to: str,
    customer_id: str,
    app_name: str = "your app",
) -> EmailMessage:
    subject = f"Quick check-in from {app_name}"
    text = (
        f"Hi,\n\n"
        f"We noticed you haven't been active in {app_name} recently. "
        f"Just wanted to check in — is there anything we can help with?\n\n"
        f"If you've hit a snag or have feedback, reply to this email. "
        f"Real person, real response.\n\n"
        f"— {app_name} team\n\n"
        f"(Subscriber reference: {customer_id})"
    )
    html = f"""<p>Hi,</p>
<p>We noticed you haven't been active in <strong>{app_name}</strong> recently.
Just wanted to check in — is there anything we can help with?</p>
<p>If you've hit a snag or have feedback, reply to this email. Real person, real response.</p>
<p>— {app_name} team</p>
<p style="color:#999;font-size:12px">Subscriber reference: {customer_id}</p>"""
    return EmailMessage(to=to, subject=subject, text=text, html=html)


def renewal_reminder(
    to: str,
    customer_id: str,
    app_name: str = "your app",
) -> EmailMessage:
    subject = f"Your {app_name} subscription renews soon"
    text = (
        f"Hi,\n\n"
        f"Just a heads-up: your {app_name} subscription is coming up for renewal soon.\n\n"
        f"No action needed — it'll renew automatically. "
        f"If you'd like to manage your subscription, "
        f"go to {app_name} → Settings → Subscription.\n\n"
        f"— {app_name} team\n\n"
        f"(Subscriber reference: {customer_id})"
    )
    html = f"""<p>Hi,</p>
<p>Just a heads-up: your <strong>{app_name}</strong> subscription is coming up for renewal
soon.</p>
<p>No action needed — it'll renew automatically. To manage your subscription, go to
Settings → Subscription in the app.</p>
<p>— {app_name} team</p>
<p style="color:#999;font-size:12px">Subscriber reference: {customer_id}</p>"""
    return EmailMessage(to=to, subject=subject, text=text, html=html)
