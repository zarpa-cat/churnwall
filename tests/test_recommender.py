"""Tests for churnwall recommender (Phase 2b)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from churnwall.models import Base, Subscriber, SubscriberState
from churnwall.recommender import ActionType, RetentionRecommender, Urgency
from churnwall.scorer import ScoreResult, _risk_band

# ─── Fixtures ────────────────────────────────────────────────────────────────


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
def rec():
    return RetentionRecommender()


def _sub(
    session,
    customer_id: str = "usr_test",
    state: SubscriberState = SubscriberState.ACTIVE,
    renewal_count: int = 3,
    billing_failure_count: int = 0,
    last_event_at: datetime | None = None,
    trial_started_at: datetime | None = None,
    converted_at: datetime | None = None,
) -> Subscriber:
    now = datetime.now(UTC).replace(tzinfo=None)
    sub = Subscriber(
        customer_id=customer_id,
        project_id="proj_test",
        state=state,
        renewal_count=renewal_count,
        billing_failure_count=billing_failure_count,
        last_event_at=last_event_at or (now - timedelta(days=5)),
        trial_started_at=trial_started_at,
        converted_at=converted_at,
    )
    session.add(sub)
    session.flush()
    return sub


def _score(score: float) -> ScoreResult:
    return ScoreResult(score=score, band=_risk_band(score))


# ─── Churned ─────────────────────────────────────────────────────────────────


class TestChurnedSubscriber:
    def test_churned_gets_winback(self, session, rec):
        sub = _sub(session, state=SubscriberState.CHURNED)
        result = rec.recommend(sub, _score(100.0))
        assert ActionType.SEND_WINBACK_OFFER in [r.action for r in result.recommendations]

    def test_churned_winback_urgency_soon(self, session, rec):
        sub = _sub(session, state=SubscriberState.CHURNED)
        result = rec.recommend(sub, _score(100.0))
        winback = next(
            r for r in result.recommendations if r.action == ActionType.SEND_WINBACK_OFFER
        )
        assert winback.urgency == Urgency.SOON

    def test_churned_winback_has_discount(self, session, rec):
        sub = _sub(session, state=SubscriberState.CHURNED)
        result = rec.recommend(sub, _score(100.0))
        winback = next(
            r for r in result.recommendations if r.action == ActionType.SEND_WINBACK_OFFER
        )
        assert "discount_pct" in winback.metadata
        assert winback.metadata["discount_pct"] > 0

    def test_churned_result_metadata(self, session, rec):
        sub = _sub(session, state=SubscriberState.CHURNED)
        result = rec.recommend(sub, _score(100.0))
        assert result.risk_band == "critical"
        assert result.state == "churned"


# ─── Billing Issue ────────────────────────────────────────────────────────────


class TestBillingIssueSubscriber:
    def test_billing_issue_immediate_urgency(self, session, rec):
        sub = _sub(session, state=SubscriberState.BILLING_ISSUE, billing_failure_count=2)
        result = rec.recommend(sub, _score(75.0))
        urgencies = [r.urgency for r in result.recommendations]
        assert Urgency.IMMEDIATE in urgencies

    def test_billing_issue_alert_action(self, session, rec):
        sub = _sub(session, state=SubscriberState.BILLING_ISSUE, billing_failure_count=1)
        result = rec.recommend(sub, _score(75.0))
        assert ActionType.SEND_BILLING_FAILURE_ALERT in [r.action for r in result.recommendations]

    def test_billing_issue_payment_update_action(self, session, rec):
        sub = _sub(session, state=SubscriberState.BILLING_ISSUE, billing_failure_count=1)
        result = rec.recommend(sub, _score(75.0))
        assert ActionType.PROMPT_PAYMENT_UPDATE in [r.action for r in result.recommendations]

    def test_billing_issue_two_recs(self, session, rec):
        sub = _sub(session, state=SubscriberState.BILLING_ISSUE, billing_failure_count=1)
        result = rec.recommend(sub, _score(75.0))
        assert len(result.recommendations) == 2

    def test_billing_alert_includes_failure_count(self, session, rec):
        sub = _sub(session, state=SubscriberState.BILLING_ISSUE, billing_failure_count=3)
        result = rec.recommend(sub, _score(80.0))
        alert = next(
            r for r in result.recommendations if r.action == ActionType.SEND_BILLING_FAILURE_ALERT
        )
        assert alert.metadata.get("failure_count") == 3


# ─── Trialing ────────────────────────────────────────────────────────────────


class TestTrialingSubscriber:
    def test_trialing_low_risk_feature_highlight(self, session, rec):
        sub = _sub(session, state=SubscriberState.TRIALING)
        result = rec.recommend(sub, _score(35.0))
        assert ActionType.SEND_TRIAL_FEATURE_HIGHLIGHT in [r.action for r in result.recommendations]

    def test_trialing_high_risk_gets_nudge(self, session, rec):
        sub = _sub(session, state=SubscriberState.TRIALING, billing_failure_count=2)
        result = rec.recommend(sub, _score(70.0))
        assert ActionType.SEND_TRIAL_CONVERSION_NUDGE in [r.action for r in result.recommendations]

    def test_trialing_high_risk_nudge_immediate(self, session, rec):
        sub = _sub(session, state=SubscriberState.TRIALING)
        result = rec.recommend(sub, _score(65.0))
        nudge = next(
            (
                r
                for r in result.recommendations
                if r.action == ActionType.SEND_TRIAL_CONVERSION_NUDGE
            ),
            None,
        )
        assert nudge is not None
        assert nudge.urgency == Urgency.IMMEDIATE

    def test_trialing_low_risk_no_nudge(self, session, rec):
        sub = _sub(session, state=SubscriberState.TRIALING)
        result = rec.recommend(sub, _score(25.0))
        actions = [r.action for r in result.recommendations]
        assert ActionType.SEND_TRIAL_CONVERSION_NUDGE not in actions


# ─── Active / Reactivated ─────────────────────────────────────────────────────


class TestActiveSubscriber:
    def test_active_low_risk_monitor(self, session, rec):
        sub = _sub(session, state=SubscriberState.ACTIVE, renewal_count=5)
        result = rec.recommend(sub, _score(15.0))
        assert result.top is not None
        assert result.top.action == ActionType.MONITOR

    def test_active_medium_risk_renewal_reminder(self, session, rec):
        sub = _sub(session, state=SubscriberState.ACTIVE, renewal_count=2)
        result = rec.recommend(sub, _score(45.0))
        assert ActionType.SEND_RENEWAL_REMINDER in [r.action for r in result.recommendations]

    def test_active_high_risk_new_subscriber_engagement(self, session, rec):
        sub = _sub(session, state=SubscriberState.ACTIVE, renewal_count=1)
        result = rec.recommend(sub, _score(65.0))
        assert ActionType.SEND_ENGAGEMENT_CHECKIN in [r.action for r in result.recommendations]

    def test_active_high_risk_loyal_subscriber_discount(self, session, rec):
        sub = _sub(session, state=SubscriberState.ACTIVE, renewal_count=10)
        result = rec.recommend(sub, _score(65.0))
        assert ActionType.SEND_LOYALTY_DISCOUNT in [r.action for r in result.recommendations]

    def test_active_critical_risk_immediate_discount(self, session, rec):
        sub = _sub(session, state=SubscriberState.ACTIVE, renewal_count=3, billing_failure_count=0)
        result = rec.recommend(sub, _score(92.0))
        top = result.top
        assert top is not None
        assert top.action == ActionType.SEND_LOYALTY_DISCOUNT
        assert top.urgency == Urgency.IMMEDIATE

    def test_reactivated_critical_risk_loyalty_discount(self, session, rec):
        sub = _sub(session, state=SubscriberState.REACTIVATED, renewal_count=2)
        result = rec.recommend(sub, _score(91.0))
        assert ActionType.SEND_LOYALTY_DISCOUNT in [r.action for r in result.recommendations]


# ─── Result shape ─────────────────────────────────────────────────────────────


class TestResultShape:
    def test_result_has_customer_id(self, session, rec):
        sub = _sub(session, customer_id="usr_abc123")
        result = rec.recommend(sub, _score(20.0))
        assert result.customer_id == "usr_abc123"

    def test_result_score_rounded(self, session, rec):
        sub = _sub(session)
        result = rec.recommend(sub, _score(67.3456))
        assert result.risk_score == 67.3

    def test_recs_sorted_by_priority(self, session, rec):
        sub = _sub(session, state=SubscriberState.BILLING_ISSUE, billing_failure_count=1)
        result = rec.recommend(sub, _score(75.0))
        priorities = [r.priority for r in result.recommendations]
        assert priorities == sorted(priorities)

    def test_top_rec_is_lowest_priority_number(self, session, rec):
        sub = _sub(session, state=SubscriberState.BILLING_ISSUE, billing_failure_count=1)
        result = rec.recommend(sub, _score(75.0))
        assert result.top == result.recommendations[0]

    def test_unknown_state_returns_monitor(self, session, rec):
        sub = _sub(session, state=SubscriberState.UNKNOWN)
        result = rec.recommend(sub, _score(50.0))
        assert result.recommendations[0].action == ActionType.MONITOR


# ─── Batch ────────────────────────────────────────────────────────────────────


class TestBatch:
    def test_batch_returns_all(self, session, rec):
        pairs = [
            (_sub(session, customer_id="a", state=SubscriberState.CHURNED), _score(100.0)),
            (
                _sub(session, customer_id="b", state=SubscriberState.ACTIVE, renewal_count=8),
                _score(15.0),
            ),
            (
                _sub(
                    session,
                    customer_id="c",
                    state=SubscriberState.BILLING_ISSUE,
                    billing_failure_count=1,
                ),
                _score(75.0),
            ),
        ]
        results = rec.recommend_batch(pairs)
        assert len(results) == 3
        assert {r.customer_id for r in results} == {"a", "b", "c"}
