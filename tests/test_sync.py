"""Tests for the RC → churnwall sync engine."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from churnwall.models import Base, Subscriber, SubscriberState
from churnwall.rc_client import SubscriberSnapshot
from churnwall.sync import SyncResult, _upsert_subscriber, sync_from_ids

# ── Fixtures ──────────────────────────────────────────────────────────────────


@pytest.fixture()
def session() -> Session:
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    SessionLocal = sessionmaker(bind=engine, autoflush=False)
    sess = SessionLocal()
    yield sess
    sess.close()
    engine.dispose()


def _future(days: int = 30) -> str:
    return (datetime.now(UTC) + timedelta(days=days)).strftime("%Y-%m-%dT%H:%M:%SZ")


def _past(days: int = 30) -> str:
    return (datetime.now(UTC) - timedelta(days=days)).strftime("%Y-%m-%dT%H:%M:%SZ")


def _make_snapshot(
    uid: str = "usr_test",
    state: str = "active",
    product_id: str = "monthly_pro",
) -> SubscriberSnapshot:
    """Build a minimal SubscriberSnapshot for testing _upsert_subscriber."""
    snap = MagicMock(spec=SubscriberSnapshot)
    snap.app_user_id = uid
    snap.state = state
    snap.product_id = product_id
    snap.store = "app_store"
    snap.first_seen = datetime.now(UTC).replace(tzinfo=None) - timedelta(days=60)
    snap.last_seen = datetime.now(UTC).replace(tzinfo=None) - timedelta(days=1)
    snap.trial_started_at = None
    snap.converted_at = datetime.now(UTC).replace(tzinfo=None) - timedelta(days=55)
    snap.churned_at = None
    snap.reactivated_at = None
    snap.renewal_count = 3
    snap.billing_failure_count = 0
    snap.last_billing_failure_at = None
    return snap


# ── _upsert_subscriber ────────────────────────────────────────────────────────


def test_upsert_creates_new(session: Session):
    snap = _make_snapshot("usr_new", state="active")
    sub, created = _upsert_subscriber(snap, "proj_1", session)
    session.flush()

    assert created is True
    assert sub.customer_id == "usr_new"
    assert sub.state == SubscriberState.ACTIVE
    assert sub.project_id == "proj_1"
    assert sub.product_id == "monthly_pro"
    assert sub.renewal_count == 3


def test_upsert_updates_existing(session: Session):
    # Create first
    snap1 = _make_snapshot("usr_existing", state="active")
    sub, created = _upsert_subscriber(snap1, "proj_1", session)
    session.flush()
    assert created is True

    # Update with new state
    snap2 = _make_snapshot("usr_existing", state="billing_issue")
    sub2, created2 = _upsert_subscriber(snap2, "proj_1", session)
    session.flush()

    assert created2 is False
    assert sub2.state == SubscriberState.BILLING_ISSUE
    assert sub2.customer_id == "usr_existing"


def test_upsert_churned_state(session: Session):
    snap = _make_snapshot("usr_churned", state="churned")
    snap.churned_at = datetime.now(UTC).replace(tzinfo=None) - timedelta(days=5)
    sub, created = _upsert_subscriber(snap, "proj_1", session)
    session.flush()

    assert sub.state == SubscriberState.CHURNED
    assert sub.churned_at is not None


def test_upsert_unknown_state_maps_to_unknown(session: Session):
    snap = _make_snapshot("usr_x", state="not_a_real_state")
    sub, created = _upsert_subscriber(snap, "proj_1", session)
    session.flush()
    assert sub.state == SubscriberState.UNKNOWN


def test_upsert_preserves_higher_billing_failure_count(session: Session):
    # Existing record has 3 failures from webhook history
    snap1 = _make_snapshot("usr_billing")
    sub, _ = _upsert_subscriber(snap1, "proj_1", session)
    session.flush()
    sub.billing_failure_count = 5  # set from webhook history
    session.flush()

    # RC API only shows 1 (no history)
    snap2 = _make_snapshot("usr_billing")
    snap2.billing_failure_count = 1
    sub2, created = _upsert_subscriber(snap2, "proj_1", session)
    session.flush()

    assert not created
    assert sub2.billing_failure_count == 5  # preserves the higher count


# ── sync_from_ids ─────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_sync_from_ids_creates_subscribers(session: Session):
    rc_responses = {
        "usr_a": {
            "original_app_user_id": "usr_a",
            "subscriptions": {
                "monthly_pro": {
                    "purchase_date": _past(15),
                    "expires_date": _future(15),
                    "period_type": "NORMAL",
                    "store": "app_store",
                    "billing_issues_detected_at": None,
                }
            },
            "first_seen": _past(30),
            "last_seen": _past(1),
        },
        "usr_b": {
            "original_app_user_id": "usr_b",
            "subscriptions": {},
            "first_seen": _past(60),
            "last_seen": _past(10),
        },
    }

    with patch("churnwall.sync.RCClient") as MockRCClient:
        mock_client = AsyncMock()
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=None)
        mock_client.get_subscribers_batch = AsyncMock(return_value=rc_responses)
        MockRCClient.return_value = mock_client

        result = await sync_from_ids(
            ["usr_a", "usr_b"],
            "proj_1",
            session,
            api_key="sk_test",
        )

    assert result.created == 2
    assert result.updated == 0
    assert result.skipped == 0
    assert len(result.errors) == 0

    subs = session.query(Subscriber).all()
    assert len(subs) == 2
    ids = {s.customer_id for s in subs}
    assert ids == {"usr_a", "usr_b"}


@pytest.mark.asyncio
async def test_sync_from_ids_handles_404(session: Session):
    """IDs not returned by RC should count as skipped."""
    rc_responses = {
        "usr_a": {
            "original_app_user_id": "usr_a",
            "subscriptions": {},
            "first_seen": _past(10),
            "last_seen": _past(1),
        }
    }  # noqa: E501

    with patch("churnwall.sync.RCClient") as MockRCClient:
        mock_client = AsyncMock()
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=None)
        mock_client.get_subscribers_batch = AsyncMock(return_value=rc_responses)
        MockRCClient.return_value = mock_client

        result = await sync_from_ids(["usr_a", "usr_missing"], "proj_1", session, api_key="sk_t")

    assert result.created == 1
    assert result.skipped == 1


@pytest.mark.asyncio
async def test_sync_from_ids_no_api_key_raises(session: Session, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.delenv("RC_API_KEY", raising=False)
    with pytest.raises(ValueError, match="No RC API key"):
        await sync_from_ids(["usr_x"], "proj_1", session, api_key=None)


# ── SyncResult ────────────────────────────────────────────────────────────────


def test_sync_result_total():
    r = SyncResult(created=3, updated=2, skipped=1)
    assert r.total == 6


def test_sync_result_str():
    r = SyncResult(created=1, updated=2, skipped=0, errors=["err"])
    assert "1" in str(r)
    assert "errors=1" in str(r)
