"""Tests for churnwall integrations: Resend (email) and Slack (alerts).

Strategy:
  - Mock httpx.AsyncClient with a simple FakeClient that records calls.
  - Verify payload structure, not just "send was called".
  - Test graceful no-op when keys are not configured.
  - Test dispatcher routing logic: action → channel + urgency → Slack.
"""

from __future__ import annotations

from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock

import pytest

from churnwall.integrations.dispatcher import DispatchResult, IntegrationDispatcher
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
from churnwall.models import Subscriber, SubscriberState
from churnwall.recommender import ActionType, Recommendation, Urgency

# ─── Helpers ──────────────────────────────────────────────────────────────────


def _make_http_client(status_code: int = 200, json_body: dict | None = None) -> MagicMock:
    """Return a mock httpx.AsyncClient that records calls and returns a canned response."""
    json_body = json_body or {"id": "email-id-123"}

    response = MagicMock()
    response.json = MagicMock(return_value=json_body)
    response.raise_for_status = MagicMock()
    response.status_code = status_code

    client = MagicMock()
    client.post = AsyncMock(return_value=response)
    # Support async context manager (not used when injected, but just in case)
    client.__aenter__ = AsyncMock(return_value=client)
    client.__aexit__ = AsyncMock(return_value=False)
    return client


def _make_subscriber(
    customer_id: str = "usr_test",
    state: SubscriberState = SubscriberState.ACTIVE,
    app_user_id: str | None = "user@example.com",
    billing_failure_count: int = 0,
    renewal_count: int = 0,
) -> Subscriber:
    sub = Subscriber()
    sub.customer_id = customer_id
    sub.project_id = "proj_test"
    sub.app_user_id = app_user_id
    sub.state = state
    sub.billing_failure_count = billing_failure_count
    sub.renewal_count = renewal_count
    sub.first_seen_at = datetime.now(UTC).replace(tzinfo=None)
    sub.last_event_at = datetime.now(UTC).replace(tzinfo=None)
    return sub


def _make_recommendation(
    action: ActionType = ActionType.SEND_BILLING_FAILURE_ALERT,
    urgency: Urgency = Urgency.IMMEDIATE,
    metadata: dict | None = None,
) -> Recommendation:
    return Recommendation(
        action=action,
        urgency=urgency,
        reason="Test reason",
        metadata=metadata or {},
        priority=10,
    )


# ─── ResendClient tests ────────────────────────────────────────────────────────


class TestResendClientConfigured:
    @pytest.mark.asyncio
    async def test_send_posts_to_resend_api(self):
        http = _make_http_client(json_body={"id": "resend-abc"})
        client = ResendClient(
            api_key="re_test_key", from_email="noreply@test.com", http_client=http
        )
        msg = EmailMessage(
            to="user@example.com",
            subject="Test subject",
            text="Hello",
            html="<p>Hello</p>",
        )
        result = await client.send(msg)

        assert result == {"id": "resend-abc"}
        http.post.assert_called_once()
        call_kwargs = http.post.call_args
        # Verify URL
        assert call_kwargs[0][0] == "https://api.resend.com/emails"
        # Verify payload structure
        payload = call_kwargs[1]["json"]
        assert payload["to"] == ["user@example.com"]
        assert payload["subject"] == "Test subject"
        assert payload["text"] == "Hello"
        assert payload["html"] == "<p>Hello</p>"
        assert payload["from"] == "noreply@test.com"

    @pytest.mark.asyncio
    async def test_send_includes_auth_header(self):
        http = _make_http_client()
        client = ResendClient(api_key="re_secret", from_email="a@b.com", http_client=http)
        await client.send(EmailMessage(to="x@y.com", subject="S", text="T"))
        headers = http.post.call_args[1]["headers"]
        assert headers["Authorization"] == "Bearer re_secret"

    @pytest.mark.asyncio
    async def test_send_without_html(self):
        http = _make_http_client()
        client = ResendClient(api_key="re_key", from_email="a@b.com", http_client=http)
        await client.send(EmailMessage(to="x@y.com", subject="S", text="T"))
        payload = http.post.call_args[1]["json"]
        assert "html" not in payload

    def test_configured_true_with_key(self):
        client = ResendClient(api_key="re_key", from_email="a@b.com")
        assert client.configured is True

    def test_configured_false_without_key(self):
        client = ResendClient(api_key=None, from_email="a@b.com")
        assert client.configured is False


class TestResendClientUnconfigured:
    @pytest.mark.asyncio
    async def test_send_returns_empty_dict_when_unconfigured(self):
        client = ResendClient(api_key=None, from_email="a@b.com")
        result = await client.send(EmailMessage(to="x@y.com", subject="S", text="T"))
        assert result == {}

    @pytest.mark.asyncio
    async def test_send_does_not_make_http_request_when_unconfigured(self):
        http = _make_http_client()
        client = ResendClient(api_key=None, from_email="a@b.com", http_client=http)
        await client.send(EmailMessage(to="x@y.com", subject="S", text="T"))
        http.post.assert_not_called()


# ─── Email template tests ──────────────────────────────────────────────────────


class TestEmailTemplates:
    def test_billing_failure_alert_structure(self):
        msg = billing_failure_alert(
            to="u@e.com", customer_id="usr_1", failure_count=2, app_name="TestApp"
        )
        assert msg.to == "u@e.com"
        assert "billing" in msg.subject.lower()
        assert "2" in msg.text  # failure_count present
        assert "usr_1" in msg.text
        assert "TestApp" in msg.text
        assert msg.html is not None

    def test_winback_offer_includes_discount(self):
        msg = winback_offer(to="u@e.com", customer_id="usr_1", discount_pct=40, app_name="App")
        assert "40%" in msg.text
        assert "40%" in (msg.html or "")

    def test_trial_conversion_nudge_includes_discount(self):
        msg = trial_conversion_nudge(to="u@e.com", customer_id="usr_1", discount_pct=15)
        assert "15%" in msg.text

    def test_loyalty_discount_shows_tenure(self):
        msg = loyalty_discount(to="u@e.com", customer_id="usr_1", discount_pct=20, renewal_count=12)
        assert "12 months" in msg.text

    def test_loyalty_discount_singular_month(self):
        msg = loyalty_discount(to="u@e.com", customer_id="usr_1", discount_pct=20, renewal_count=1)
        assert "1 month" in msg.text
        assert "months" not in msg.text

    def test_engagement_checkin_structure(self):
        msg = engagement_checkin(to="u@e.com", customer_id="usr_1", app_name="Foo")
        assert "Foo" in msg.text
        assert msg.html is not None

    def test_renewal_reminder_structure(self):
        msg = renewal_reminder(to="u@e.com", customer_id="usr_1", app_name="Bar")
        assert "renew" in msg.subject.lower()
        assert "Bar" in msg.text


# ─── SlackClient tests ─────────────────────────────────────────────────────────


class TestSlackClientConfigured:
    @pytest.mark.asyncio
    async def test_post_sends_to_webhook_url(self):
        http = _make_http_client(json_body={})
        http.post.return_value.text = "ok"
        client = SlackClient(webhook_url="https://hooks.slack.com/services/test", http_client=http)
        result = await client.post({"text": "hello"})
        assert result is True
        http.post.assert_called_once()
        assert http.post.call_args[0][0] == "https://hooks.slack.com/services/test"

    @pytest.mark.asyncio
    async def test_post_alert_builds_block_kit_payload(self):
        http = _make_http_client()
        client = SlackClient(webhook_url="https://hooks.slack.com/services/test", http_client=http)
        await client.post_alert(
            customer_id="usr_123",
            state="billing_issue",
            risk_score=88.0,
            action="send_billing_failure_alert",
            reason="Billing failed.",
            urgency="immediate",
            extra={"failure_count": 2},
        )
        payload = http.post.call_args[1]["json"]
        assert "blocks" in payload
        assert any("usr_123" in str(b) for b in payload["blocks"])
        assert any("88.0" in str(b) for b in payload["blocks"])

    @pytest.mark.asyncio
    async def test_post_alert_red_emoji_for_high_risk(self):
        http = _make_http_client()
        client = SlackClient(webhook_url="https://hooks.slack.com/services/test", http_client=http)
        await client.post_alert(
            customer_id="usr_x",
            state="active",
            risk_score=90.0,
            action="send_loyalty_discount",
            reason="Critical risk.",
        )
        payload = http.post.call_args[1]["json"]
        # 🔴 should appear for risk >= 80
        assert "🔴" in str(payload["blocks"])

    @pytest.mark.asyncio
    async def test_post_alert_yellow_emoji_for_medium_risk(self):
        http = _make_http_client()
        client = SlackClient(webhook_url="https://hooks.slack.com/services/test", http_client=http)
        await client.post_alert(
            customer_id="usr_x",
            state="active",
            risk_score=50.0,
            action="send_engagement_checkin",
            reason="Medium risk.",
        )
        payload = http.post.call_args[1]["json"]
        assert "🟡" in str(payload["blocks"])

    def test_configured_true_with_url(self):
        client = SlackClient(webhook_url="https://hooks.slack.com/services/x")
        assert client.configured is True

    def test_configured_false_without_url(self):
        client = SlackClient(webhook_url=None)
        assert client.configured is False


class TestSlackClientUnconfigured:
    @pytest.mark.asyncio
    async def test_post_returns_false_when_unconfigured(self):
        client = SlackClient(webhook_url=None)
        result = await client.post({"text": "hello"})
        assert result is False

    @pytest.mark.asyncio
    async def test_post_alert_returns_false_when_unconfigured(self):
        client = SlackClient(webhook_url=None)
        result = await client.post_alert(
            customer_id="usr_x",
            state="billing_issue",
            risk_score=85.0,
            action="send_billing_failure_alert",
            reason="Billing failed.",
        )
        assert result is False


# ─── IntegrationDispatcher tests ──────────────────────────────────────────────


def _make_dispatcher(
    resend_key: str | None = "re_key",
    from_email: str = "noreply@test.com",
    slack_url: str | None = "https://hooks.slack.com/x",
    resend_http: MagicMock | None = None,
    slack_http: MagicMock | None = None,
    app_name: str = "TestApp",
) -> IntegrationDispatcher:
    resend_http = resend_http or _make_http_client(json_body={"id": "email-123"})
    slack_http = slack_http or _make_http_client()

    resend = ResendClient(api_key=resend_key, from_email=from_email, http_client=resend_http)
    slack = SlackClient(webhook_url=slack_url, http_client=slack_http)
    return IntegrationDispatcher(resend=resend, slack=slack, app_name=app_name)


class TestDispatcherRouting:
    @pytest.mark.asyncio
    async def test_billing_failure_immediate_sends_email_and_slack(self):
        resend_http = _make_http_client(json_body={"id": "e1"})
        slack_http = _make_http_client()
        dispatcher = _make_dispatcher(resend_http=resend_http, slack_http=slack_http)

        sub = _make_subscriber(state=SubscriberState.BILLING_ISSUE, billing_failure_count=2)
        rec = _make_recommendation(
            action=ActionType.SEND_BILLING_FAILURE_ALERT,
            urgency=Urgency.IMMEDIATE,
            metadata={"failure_count": 2, "channel": "email"},
        )

        result = await dispatcher.dispatch(sub, rec, risk_score=75.0)

        assert isinstance(result, DispatchResult)
        assert result.email_sent is True
        assert result.slack_sent is True
        resend_http.post.assert_called_once()
        slack_http.post.assert_called_once()

    @pytest.mark.asyncio
    async def test_winback_soon_sends_email_not_slack(self):
        resend_http = _make_http_client(json_body={"id": "e2"})
        slack_http = _make_http_client()
        dispatcher = _make_dispatcher(resend_http=resend_http, slack_http=slack_http)

        sub = _make_subscriber(state=SubscriberState.CHURNED)
        rec = _make_recommendation(
            action=ActionType.SEND_WINBACK_OFFER,
            urgency=Urgency.SOON,
            metadata={"discount_pct": 30, "channel": "email"},
        )

        result = await dispatcher.dispatch(sub, rec, risk_score=60.0)

        assert result.email_sent is True
        assert result.slack_sent is False
        resend_http.post.assert_called_once()
        slack_http.post.assert_not_called()

    @pytest.mark.asyncio
    async def test_monitor_urgency_sends_nothing(self):
        resend_http = _make_http_client()
        slack_http = _make_http_client()
        dispatcher = _make_dispatcher(resend_http=resend_http, slack_http=slack_http)

        sub = _make_subscriber(state=SubscriberState.ACTIVE)
        rec = _make_recommendation(action=ActionType.MONITOR, urgency=Urgency.MONITOR)

        result = await dispatcher.dispatch(sub, rec, risk_score=20.0)

        assert result.email_sent is False
        assert result.slack_sent is False
        resend_http.post.assert_not_called()
        slack_http.post.assert_not_called()

    @pytest.mark.asyncio
    async def test_no_email_if_app_user_id_not_email(self):
        resend_http = _make_http_client()
        slack_http = _make_http_client()
        dispatcher = _make_dispatcher(resend_http=resend_http, slack_http=slack_http)

        sub = _make_subscriber(
            app_user_id="not-an-email",
            state=SubscriberState.BILLING_ISSUE,
        )
        rec = _make_recommendation(
            action=ActionType.SEND_BILLING_FAILURE_ALERT, urgency=Urgency.IMMEDIATE
        )

        result = await dispatcher.dispatch(sub, rec, risk_score=80.0)

        assert result.email_sent is False
        assert result.email_skipped_reason is not None
        # Slack still fires for immediate urgency even without email
        assert result.slack_sent is True

    @pytest.mark.asyncio
    async def test_no_email_if_app_user_id_is_none(self):
        resend_http = _make_http_client()
        slack_http = _make_http_client()
        dispatcher = _make_dispatcher(resend_http=resend_http, slack_http=slack_http)

        sub = _make_subscriber(app_user_id=None, state=SubscriberState.CHURNED)
        rec = _make_recommendation(action=ActionType.SEND_WINBACK_OFFER, urgency=Urgency.SOON)

        result = await dispatcher.dispatch(sub, rec, risk_score=60.0)

        assert result.email_sent is False
        resend_http.post.assert_not_called()

    @pytest.mark.asyncio
    async def test_loyalty_discount_soon_email_only(self):
        resend_http = _make_http_client(json_body={"id": "e3"})
        slack_http = _make_http_client()
        dispatcher = _make_dispatcher(resend_http=resend_http, slack_http=slack_http)

        sub = _make_subscriber(state=SubscriberState.ACTIVE, renewal_count=8)
        rec = _make_recommendation(
            action=ActionType.SEND_LOYALTY_DISCOUNT,
            urgency=Urgency.SOON,
            metadata={"discount_pct": 15},
        )

        result = await dispatcher.dispatch(sub, rec, risk_score=65.0)

        assert result.email_sent is True
        assert result.slack_sent is False

    @pytest.mark.asyncio
    async def test_trial_nudge_immediate_sends_email_and_slack(self):
        resend_http = _make_http_client(json_body={"id": "e4"})
        slack_http = _make_http_client()
        dispatcher = _make_dispatcher(resend_http=resend_http, slack_http=slack_http)

        sub = _make_subscriber(state=SubscriberState.TRIALING)
        rec = _make_recommendation(
            action=ActionType.SEND_TRIAL_CONVERSION_NUDGE,
            urgency=Urgency.IMMEDIATE,
            metadata={"discount_pct": 20},
        )

        result = await dispatcher.dispatch(sub, rec, risk_score=72.0)

        assert result.email_sent is True
        assert result.slack_sent is True

    @pytest.mark.asyncio
    async def test_engagement_checkin_soon_email_only(self):
        resend_http = _make_http_client(json_body={"id": "e5"})
        slack_http = _make_http_client()
        dispatcher = _make_dispatcher(resend_http=resend_http, slack_http=slack_http)

        sub = _make_subscriber(state=SubscriberState.ACTIVE)
        rec = _make_recommendation(action=ActionType.SEND_ENGAGEMENT_CHECKIN, urgency=Urgency.SOON)

        result = await dispatcher.dispatch(sub, rec, risk_score=55.0)

        assert result.email_sent is True
        assert result.slack_sent is False

    @pytest.mark.asyncio
    async def test_renewal_reminder_monitor_sends_nothing(self):
        resend_http = _make_http_client()
        slack_http = _make_http_client()
        dispatcher = _make_dispatcher(resend_http=resend_http, slack_http=slack_http)

        sub = _make_subscriber(state=SubscriberState.ACTIVE)
        rec = _make_recommendation(action=ActionType.SEND_RENEWAL_REMINDER, urgency=Urgency.MONITOR)

        result = await dispatcher.dispatch(sub, rec, risk_score=35.0)

        assert result.email_sent is False
        assert result.slack_sent is False


class TestDispatcherUnconfigured:
    @pytest.mark.asyncio
    async def test_no_email_when_resend_key_missing(self):
        resend_http = _make_http_client()
        slack_http = _make_http_client()
        dispatcher = _make_dispatcher(
            resend_key=None, resend_http=resend_http, slack_http=slack_http
        )

        sub = _make_subscriber(state=SubscriberState.BILLING_ISSUE, billing_failure_count=1)
        rec = _make_recommendation(
            action=ActionType.SEND_BILLING_FAILURE_ALERT, urgency=Urgency.IMMEDIATE
        )

        result = await dispatcher.dispatch(sub, rec, risk_score=80.0)

        assert result.email_sent is False
        assert result.slack_sent is True  # Slack still fires

    @pytest.mark.asyncio
    async def test_no_slack_when_webhook_missing(self):
        resend_http = _make_http_client(json_body={"id": "e6"})
        slack_http = _make_http_client()
        dispatcher = _make_dispatcher(
            slack_url=None, resend_http=resend_http, slack_http=slack_http
        )

        sub = _make_subscriber(state=SubscriberState.BILLING_ISSUE, billing_failure_count=1)
        rec = _make_recommendation(
            action=ActionType.SEND_BILLING_FAILURE_ALERT, urgency=Urgency.IMMEDIATE
        )

        result = await dispatcher.dispatch(sub, rec, risk_score=80.0)

        assert result.email_sent is True
        assert result.slack_sent is False


class TestDispatchTop:
    @pytest.mark.asyncio
    async def test_dispatch_top_picks_lowest_priority_number(self):
        resend_http = _make_http_client(json_body={"id": "e7"})
        slack_http = _make_http_client()
        dispatcher = _make_dispatcher(resend_http=resend_http, slack_http=slack_http)

        sub = _make_subscriber(state=SubscriberState.BILLING_ISSUE, billing_failure_count=2)
        recs = [
            Recommendation(
                action=ActionType.SEND_WINBACK_OFFER,
                urgency=Urgency.SOON,
                reason="winback",
                priority=30,
            ),
            Recommendation(
                action=ActionType.SEND_BILLING_FAILURE_ALERT,
                urgency=Urgency.IMMEDIATE,
                reason="billing failure",
                metadata={"failure_count": 2},
                priority=5,  # ← lowest number = highest priority
            ),
        ]

        result = await dispatcher.dispatch_top(sub, risk_score=85.0, recommendations=recs)

        assert result is not None
        assert result.action == ActionType.SEND_BILLING_FAILURE_ALERT.value
        # Billing alert (immediate) → email + Slack
        assert result.email_sent is True
        assert result.slack_sent is True

    @pytest.mark.asyncio
    async def test_dispatch_top_returns_none_for_empty_list(self):
        dispatcher = _make_dispatcher()
        sub = _make_subscriber()
        result = await dispatcher.dispatch_top(sub, risk_score=0.0, recommendations=[])
        assert result is None
