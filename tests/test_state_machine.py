"""Tests for the subscriber state machine.

Covers all major state transitions derived from RC event types.
"""

from __future__ import annotations

from datetime import datetime, timedelta

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from churnwall.models import Base, RCEventType, SubscriberState
from churnwall.state_machine import StateMachine

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def engine():
    eng = create_engine("sqlite:///:memory:", connect_args={"check_same_thread": False})
    Base.metadata.create_all(eng)
    yield eng
    Base.metadata.drop_all(eng)


@pytest.fixture
def session(engine):
    Session = sessionmaker(bind=engine)
    s = Session()
    yield s
    s.close()


@pytest.fixture
def sm():
    return StateMachine()


def _apply(sm, session, event_type, customer_id="cust_001", minutes_offset=0):
    return sm.apply(
        session=session,
        customer_id=customer_id,
        project_id="proj_test",
        event_type=event_type,
        occurred_at=datetime(2026, 3, 1, 12, 0) + timedelta(minutes=minutes_offset),
    )


# ---------------------------------------------------------------------------
# New subscriber: UNKNOWN → active
# ---------------------------------------------------------------------------


def test_initial_purchase_creates_active_subscriber(sm, session):
    sub, evt = _apply(sm, session, RCEventType.INITIAL_PURCHASE)
    assert sub.state == SubscriberState.ACTIVE
    assert evt.from_state == SubscriberState.UNKNOWN
    assert evt.to_state == SubscriberState.ACTIVE


def test_initial_purchase_sets_converted_at(sm, session):
    sub, _ = _apply(sm, session, RCEventType.INITIAL_PURCHASE)
    assert sub.converted_at is not None


def test_idempotent_customer_id(sm, session):
    """Applying two events to the same customer_id uses the same subscriber row."""
    sub1, _ = _apply(sm, session, RCEventType.INITIAL_PURCHASE)
    sub2, _ = _apply(sm, session, RCEventType.RENEWAL)
    assert sub1.id == sub2.id


# ---------------------------------------------------------------------------
# Trial path
# ---------------------------------------------------------------------------


def test_trial_start_creates_trialing_subscriber(sm, session):
    sub, evt = _apply(sm, session, RCEventType.TRIAL_STARTED)
    assert sub.state == SubscriberState.TRIALING


def test_trial_converted_moves_to_active(sm, session):
    _apply(sm, session, RCEventType.TRIAL_STARTED)
    sub, _ = _apply(sm, session, RCEventType.TRIAL_CONVERTED)
    assert sub.state == SubscriberState.ACTIVE
    assert sub.converted_at is not None


def test_trial_cancelled_moves_to_churned(sm, session):
    _apply(sm, session, RCEventType.TRIAL_STARTED)
    sub, _ = _apply(sm, session, RCEventType.TRIAL_CANCELLED)
    assert sub.state == SubscriberState.CHURNED


def test_trial_expired_moves_to_churned(sm, session):
    _apply(sm, session, RCEventType.TRIAL_STARTED)
    sub, _ = _apply(sm, session, RCEventType.TRIAL_EXPIRED)
    assert sub.state == SubscriberState.CHURNED


# ---------------------------------------------------------------------------
# Active subscriber paths
# ---------------------------------------------------------------------------


def test_renewal_keeps_active(sm, session):
    _apply(sm, session, RCEventType.INITIAL_PURCHASE)
    sub, _ = _apply(sm, session, RCEventType.RENEWAL)
    assert sub.state == SubscriberState.ACTIVE
    assert sub.renewal_count == 1


def test_multiple_renewals_counted(sm, session):
    _apply(sm, session, RCEventType.INITIAL_PURCHASE)
    for _ in range(5):
        _apply(sm, session, RCEventType.RENEWAL)
    from churnwall.models import Subscriber

    sub = session.query(Subscriber).filter_by(customer_id="cust_001").first()
    assert sub.renewal_count == 5


def test_cancellation_from_active_churns(sm, session):
    _apply(sm, session, RCEventType.INITIAL_PURCHASE)
    sub, _ = _apply(sm, session, RCEventType.CANCELLATION)
    assert sub.state == SubscriberState.CHURNED
    assert sub.churned_at is not None


def test_expiration_from_active_churns(sm, session):
    _apply(sm, session, RCEventType.INITIAL_PURCHASE)
    sub, _ = _apply(sm, session, RCEventType.EXPIRATION)
    assert sub.state == SubscriberState.CHURNED


def test_billing_issue_from_active(sm, session):
    _apply(sm, session, RCEventType.INITIAL_PURCHASE)
    sub, _ = _apply(sm, session, RCEventType.BILLING_ISSUE)
    assert sub.state == SubscriberState.BILLING_ISSUE
    assert sub.billing_failure_count == 1
    assert sub.last_billing_failure_at is not None


# ---------------------------------------------------------------------------
# Billing issue paths
# ---------------------------------------------------------------------------


def test_renewal_recovers_from_billing_issue(sm, session):
    _apply(sm, session, RCEventType.INITIAL_PURCHASE)
    _apply(sm, session, RCEventType.BILLING_ISSUE)
    sub, _ = _apply(sm, session, RCEventType.RENEWAL)
    assert sub.state == SubscriberState.ACTIVE


def test_expiration_from_billing_issue_churns(sm, session):
    _apply(sm, session, RCEventType.INITIAL_PURCHASE)
    _apply(sm, session, RCEventType.BILLING_ISSUE)
    sub, _ = _apply(sm, session, RCEventType.EXPIRATION)
    assert sub.state == SubscriberState.CHURNED


def test_multiple_billing_failures_counted(sm, session):
    _apply(sm, session, RCEventType.INITIAL_PURCHASE)
    _apply(sm, session, RCEventType.BILLING_ISSUE)
    _apply(sm, session, RCEventType.BILLING_ISSUE)
    from churnwall.models import Subscriber

    sub = session.query(Subscriber).filter_by(customer_id="cust_001").first()
    assert sub.billing_failure_count == 2


# ---------------------------------------------------------------------------
# Win-back / reactivation
# ---------------------------------------------------------------------------


def test_purchase_after_churn_reactivates(sm, session):
    _apply(sm, session, RCEventType.INITIAL_PURCHASE)
    _apply(sm, session, RCEventType.CANCELLATION)
    sub, _ = _apply(sm, session, RCEventType.INITIAL_PURCHASE)
    assert sub.state == SubscriberState.REACTIVATED
    assert sub.reactivated_at is not None


def test_reactivated_renewal_moves_to_active(sm, session):
    _apply(sm, session, RCEventType.INITIAL_PURCHASE)
    _apply(sm, session, RCEventType.CANCELLATION)
    _apply(sm, session, RCEventType.INITIAL_PURCHASE)
    sub, _ = _apply(sm, session, RCEventType.RENEWAL)
    assert sub.state == SubscriberState.ACTIVE


# ---------------------------------------------------------------------------
# Event log
# ---------------------------------------------------------------------------


def test_events_are_recorded(sm, session):
    from churnwall.models import SubscriberEvent

    _apply(sm, session, RCEventType.INITIAL_PURCHASE)
    _apply(sm, session, RCEventType.RENEWAL)
    _apply(sm, session, RCEventType.CANCELLATION)
    events = session.query(SubscriberEvent).all()
    assert len(events) == 3
    assert events[0].event_type == RCEventType.INITIAL_PURCHASE
    assert events[-1].event_type == RCEventType.CANCELLATION


# ---------------------------------------------------------------------------
# Unknown transition (graceful fallback)
# ---------------------------------------------------------------------------


def test_unknown_transition_keeps_state(sm, session):
    """An event with no defined transition should not crash or corrupt state."""
    # RENEWAL from UNKNOWN has no defined transition
    sub, evt = _apply(sm, session, RCEventType.RENEWAL)
    assert sub.state == SubscriberState.UNKNOWN
    assert evt.from_state == evt.to_state
