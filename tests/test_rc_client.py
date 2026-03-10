"""Tests for the RC API client and SubscriberSnapshot state inference."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, MagicMock

import pytest

from churnwall.rc_client import RCApiError, RCClient, SubscriberSnapshot, _parse_dt

# ── _parse_dt ─────────────────────────────────────────────────────────────────


def test_parse_dt_none():
    assert _parse_dt(None) is None


def test_parse_dt_iso_z():
    dt = _parse_dt("2024-01-15T12:00:00Z")
    assert dt == datetime(2024, 1, 15, 12, 0, 0)
    assert dt.tzinfo is None  # stored as naive UTC


def test_parse_dt_with_offset():
    dt = _parse_dt("2024-01-15T14:00:00+02:00")
    assert dt == datetime(2024, 1, 15, 12, 0, 0)


# ── SubscriberSnapshot — state inference ─────────────────────────────────────


def _future(days: int = 30) -> str:
    return (datetime.now(UTC) + timedelta(days=days)).strftime("%Y-%m-%dT%H:%M:%SZ")


def _past(days: int = 30) -> str:
    return (datetime.now(UTC) - timedelta(days=days)).strftime("%Y-%m-%dT%H:%M:%SZ")


def _active_sub(product_id: str = "monthly_pro", store: str = "app_store") -> dict:
    return {
        "purchase_date": _past(15),
        "expires_date": _future(15),
        "period_type": "NORMAL",
        "store": store,
        "billing_issues_detected_at": None,
        "unsubscribe_detected_at": None,
    }


def _trial_sub() -> dict:
    return {
        "purchase_date": _past(3),
        "expires_date": _future(11),
        "period_type": "TRIAL",
        "store": "app_store",
        "billing_issues_detected_at": None,
        "unsubscribe_detected_at": None,
    }


def _billing_issue_sub() -> dict:
    return {
        "purchase_date": _past(20),
        "expires_date": _future(10),
        "period_type": "NORMAL",
        "store": "play_store",
        "billing_issues_detected_at": _past(2),
        "unsubscribe_detected_at": None,
    }


def _expired_sub() -> dict:
    return {
        "purchase_date": _past(60),
        "expires_date": _past(30),
        "period_type": "NORMAL",
        "store": "app_store",
        "billing_issues_detected_at": None,
        "unsubscribe_detected_at": _past(32),
    }


def _make_rc_data(subscriptions: dict, first_seen: str | None = None) -> dict:
    return {
        "original_app_user_id": "usr_test",
        "subscriptions": subscriptions,
        "first_seen": first_seen or _past(60),
        "last_seen": _past(1),
    }


def test_state_unknown_no_subs():
    snap = SubscriberSnapshot("usr_1", _make_rc_data({}))
    assert snap.state == "unknown"


def test_state_active():
    snap = SubscriberSnapshot("usr_1", _make_rc_data({"monthly_pro": _active_sub()}))
    assert snap.state == "active"


def test_state_trialing():
    snap = SubscriberSnapshot("usr_1", _make_rc_data({"annual_pro": _trial_sub()}))
    assert snap.state == "trialing"


def test_state_billing_issue():
    snap = SubscriberSnapshot("usr_1", _make_rc_data({"monthly_pro": _billing_issue_sub()}))
    assert snap.state == "billing_issue"


def test_state_churned_single_expired():
    snap = SubscriberSnapshot("usr_1", _make_rc_data({"monthly_pro": _expired_sub()}))
    assert snap.state == "churned"


def test_state_reactivated_multiple_subs():
    subs = {
        "monthly_pro": _expired_sub(),
        "annual_pro": _active_sub(),
    }
    snap = SubscriberSnapshot("usr_1", _make_rc_data(subs))
    # Has an active sub → should be active, not reactivated
    assert snap.state == "active"


def test_state_reactivated_all_expired_multiple():
    subs = {
        "monthly_pro_old": _expired_sub(),
        "monthly_pro_new": {
            **_expired_sub(),
            "purchase_date": _past(5),
            "expires_date": _past(1),
        },
    }
    snap = SubscriberSnapshot("usr_1", _make_rc_data(subs))
    assert snap.state == "reactivated"


def test_store_extracted():
    snap = SubscriberSnapshot("usr_1", _make_rc_data({"p": _active_sub(store="play_store")}))
    assert snap.store == "play_store"


def test_first_seen():
    snap = SubscriberSnapshot("usr_1", _make_rc_data({}, first_seen="2024-01-01T00:00:00Z"))
    assert snap.first_seen == datetime(2024, 1, 1, 0, 0, 0)


def test_billing_failure_count():
    snap = SubscriberSnapshot("usr_1", _make_rc_data({"p": _billing_issue_sub()}))
    assert snap.billing_failure_count == 1


def test_last_billing_failure_at():
    snap = SubscriberSnapshot("usr_1", _make_rc_data({"p": _billing_issue_sub()}))
    assert snap.last_billing_failure_at is not None


# ── RCClient — HTTP interaction ───────────────────────────────────────────────


def _make_mock_http(status_code: int, body: dict | None = None, text: str = "") -> MagicMock:
    """Build a fake httpx.AsyncClient with a stubbed .get() response."""
    mock_resp = MagicMock()
    mock_resp.status_code = status_code
    mock_resp.text = text
    if body is not None:
        mock_resp.json = MagicMock(return_value=body)
    mock_http = MagicMock()
    mock_http.get = AsyncMock(return_value=mock_resp)
    mock_http.aclose = AsyncMock()
    return mock_http


@pytest.mark.asyncio
async def test_get_subscriber_success():
    rc_response = {
        "subscriber": {
            "original_app_user_id": "usr_123",
            "subscriptions": {"monthly_pro": _active_sub()},
            "first_seen": _past(30),
            "last_seen": _past(1),
        }
    }
    client = RCClient(api_key="sk_test")
    client._client = _make_mock_http(200, rc_response)
    sub = await client.get_subscriber("usr_123")
    assert sub["original_app_user_id"] == "usr_123"


@pytest.mark.asyncio
async def test_get_subscriber_404_raises():
    client = RCClient(api_key="sk_test")
    client._client = _make_mock_http(404, text="Not found")
    with pytest.raises(RCApiError) as exc_info:
        await client.get_subscriber("usr_missing")
    assert exc_info.value.status_code == 404


@pytest.mark.asyncio
async def test_get_subscriber_500_raises():
    client = RCClient(api_key="sk_test")
    client._client = _make_mock_http(500, text="Internal error")
    with pytest.raises(RCApiError) as exc_info:
        await client.get_subscriber("usr_x")
    assert exc_info.value.status_code == 500
