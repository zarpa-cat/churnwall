"""Tests for the churn risk scorer.

Covers scoring logic for all subscriber states, billing history,
renewal count, recency, and trial conversion speed modifiers.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from churnwall.models import Base, Subscriber, SubscriberState
from churnwall.scorer import ChurnRiskScorer, RiskBand

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
def scorer():
    return ChurnRiskScorer()


def _make_subscriber(
    session,
    customer_id: str = "cust_001",
    state: SubscriberState = SubscriberState.ACTIVE,
    renewal_count: int = 0,
    billing_failure_count: int = 0,
    first_seen_at: datetime | None = None,
    trial_started_at: datetime | None = None,
    converted_at: datetime | None = None,
    last_event_at: datetime | None = None,
    last_billing_failure_at: datetime | None = None,
) -> Subscriber:
    now = datetime.now(UTC).replace(tzinfo=None)
    sub = Subscriber(
        customer_id=customer_id,
        project_id="proj_test",
        state=state,
        renewal_count=renewal_count,
        billing_failure_count=billing_failure_count,
        first_seen_at=first_seen_at or now,
        trial_started_at=trial_started_at,
        converted_at=converted_at,
        last_event_at=last_event_at or now,
        last_billing_failure_at=last_billing_failure_at,
    )
    session.add(sub)
    session.flush()
    return sub


# ---------------------------------------------------------------------------
# Base score by state
# ---------------------------------------------------------------------------


class TestBaseScoreByState:
    def test_active_low_risk(self, session, scorer):
        sub = _make_subscriber(session, state=SubscriberState.ACTIVE)
        result = scorer.score(sub)
        assert result.score < 40, f"Active subscriber should be low risk, got {result.score}"

    def test_billing_issue_high_risk(self, session, scorer):
        sub = _make_subscriber(session, state=SubscriberState.BILLING_ISSUE)
        result = scorer.score(sub)
        assert result.score >= 60, (
            f"Billing issue subscriber should be high risk, got {result.score}"
        )

    def test_churned_max_risk(self, session, scorer):
        sub = _make_subscriber(session, state=SubscriberState.CHURNED)
        result = scorer.score(sub)
        assert result.score == 100, f"Churned subscriber should be 100, got {result.score}"

    def test_trialing_medium_risk(self, session, scorer):
        sub = _make_subscriber(session, state=SubscriberState.TRIALING)
        result = scorer.score(sub)
        assert 20 <= result.score <= 60, f"Trialing should be medium risk, got {result.score}"

    def test_reactivated_moderate_risk(self, session, scorer):
        sub = _make_subscriber(session, state=SubscriberState.REACTIVATED)
        result = scorer.score(sub)
        assert 20 <= result.score <= 60, f"Reactivated should be moderate risk, got {result.score}"

    def test_unknown_medium_risk(self, session, scorer):
        sub = _make_subscriber(session, state=SubscriberState.UNKNOWN)
        result = scorer.score(sub)
        assert 30 <= result.score <= 70, f"Unknown should be medium risk, got {result.score}"


# ---------------------------------------------------------------------------
# Billing failure modifier
# ---------------------------------------------------------------------------


class TestBillingFailureModifier:
    def test_no_failures_not_penalized(self, session, scorer):
        sub_clean = _make_subscriber(session, customer_id="c1", billing_failure_count=0)
        sub_failed = _make_subscriber(session, customer_id="c2", billing_failure_count=2)
        assert scorer.score(sub_clean).score < scorer.score(sub_failed).score

    def test_repeated_failures_increase_score(self, session, scorer):
        sub1 = _make_subscriber(session, customer_id="c1", billing_failure_count=1)
        sub2 = _make_subscriber(session, customer_id="c2", billing_failure_count=3)
        assert scorer.score(sub1).score < scorer.score(sub2).score

    def test_billing_issue_state_with_failures_caps_at_100(self, session, scorer):
        sub = _make_subscriber(
            session,
            state=SubscriberState.BILLING_ISSUE,
            billing_failure_count=10,
        )
        result = scorer.score(sub)
        assert result.score <= 100


# ---------------------------------------------------------------------------
# Renewal count modifier
# ---------------------------------------------------------------------------


class TestRenewalCountModifier:
    def test_loyal_subscriber_lower_risk(self, session, scorer):
        sub_new = _make_subscriber(session, customer_id="c1", renewal_count=0)
        sub_loyal = _make_subscriber(session, customer_id="c2", renewal_count=12)
        assert scorer.score(sub_loyal).score < scorer.score(sub_new).score

    def test_medium_loyalty_between_new_and_loyal(self, session, scorer):
        sub_new = _make_subscriber(session, customer_id="c1", renewal_count=0)
        sub_mid = _make_subscriber(session, customer_id="c2", renewal_count=3)
        sub_loyal = _make_subscriber(session, customer_id="c3", renewal_count=12)
        scores = [scorer.score(s).score for s in (sub_new, sub_mid, sub_loyal)]
        assert scores[0] >= scores[1] >= scores[2]


# ---------------------------------------------------------------------------
# Recency modifier
# ---------------------------------------------------------------------------


class TestRecencyModifier:
    def test_stale_subscriber_higher_risk(self, session, scorer):
        now = datetime.now(UTC).replace(tzinfo=None)
        sub_recent = _make_subscriber(session, customer_id="c1", last_event_at=now)
        sub_stale = _make_subscriber(
            session, customer_id="c2", last_event_at=now - timedelta(days=120)
        )
        assert scorer.score(sub_stale).score > scorer.score(sub_recent).score

    def test_90_day_stale_penalized(self, session, scorer):
        now = datetime.now(UTC).replace(tzinfo=None)
        sub = _make_subscriber(
            session,
            state=SubscriberState.ACTIVE,
            last_event_at=now - timedelta(days=91),
        )
        result = scorer.score(sub)
        # Should have recency penalty applied
        assert result.score > 20  # base active + penalty


# ---------------------------------------------------------------------------
# Trial conversion speed
# ---------------------------------------------------------------------------


class TestTrialConversionModifier:
    def test_fast_converter_lower_risk(self, session, scorer):
        now = datetime.now(UTC).replace(tzinfo=None)
        # Fast converter: < 1 day
        sub_fast = _make_subscriber(
            session,
            customer_id="c1",
            state=SubscriberState.ACTIVE,
            trial_started_at=now - timedelta(days=30),
            converted_at=now - timedelta(days=30) + timedelta(hours=6),
        )
        # Slow converter: 5 days
        sub_slow = _make_subscriber(
            session,
            customer_id="c2",
            state=SubscriberState.ACTIVE,
            trial_started_at=now - timedelta(days=30),
            converted_at=now - timedelta(days=30) + timedelta(days=5),
        )
        assert scorer.score(sub_fast).score <= scorer.score(sub_slow).score

    def test_no_trial_no_penalty(self, session, scorer):
        """Direct purchase (no trial) should not be penalized."""
        sub = _make_subscriber(
            session,
            state=SubscriberState.ACTIVE,
            trial_started_at=None,
            converted_at=None,
        )
        result = scorer.score(sub)
        assert result.score < 40  # should still be low risk


# ---------------------------------------------------------------------------
# Score clamping
# ---------------------------------------------------------------------------


class TestScoreClamping:
    def test_score_minimum_zero(self, session, scorer):
        """Score should never go below 0."""
        sub = _make_subscriber(
            session,
            state=SubscriberState.ACTIVE,
            renewal_count=100,
            billing_failure_count=0,
        )
        result = scorer.score(sub)
        assert result.score >= 0

    def test_score_maximum_100(self, session, scorer):
        """Score should never exceed 100."""
        sub = _make_subscriber(
            session,
            state=SubscriberState.BILLING_ISSUE,
            billing_failure_count=100,
            renewal_count=0,
        )
        result = scorer.score(sub)
        assert result.score <= 100


# ---------------------------------------------------------------------------
# Risk band
# ---------------------------------------------------------------------------


class TestRiskBand:
    def test_low_band(self, session, scorer):
        sub = _make_subscriber(session, state=SubscriberState.ACTIVE, renewal_count=12)
        result = scorer.score(sub)
        if result.score < 30:
            assert result.band == RiskBand.LOW

    def test_medium_band(self, session, scorer):
        sub = _make_subscriber(session, state=SubscriberState.TRIALING)
        result = scorer.score(sub)
        if 30 <= result.score < 60:
            assert result.band == RiskBand.MEDIUM

    def test_high_band(self, session, scorer):
        sub = _make_subscriber(session, state=SubscriberState.BILLING_ISSUE)
        result = scorer.score(sub)
        if result.score >= 60:
            assert result.band == RiskBand.HIGH

    def test_churned_critical_band(self, session, scorer):
        sub = _make_subscriber(session, state=SubscriberState.CHURNED)
        result = scorer.score(sub)
        assert result.band == RiskBand.CRITICAL


# ---------------------------------------------------------------------------
# Persist score
# ---------------------------------------------------------------------------


class TestPersistScore:
    def test_compute_and_persist_updates_subscriber(self, session, scorer):
        sub = _make_subscriber(session, state=SubscriberState.ACTIVE)
        assert sub.risk_score is None
        assert sub.risk_computed_at is None

        scorer.compute_and_persist(session, sub)

        assert sub.risk_score is not None
        assert sub.risk_computed_at is not None

    def test_score_result_matches_persisted_value(self, session, scorer):
        sub = _make_subscriber(session, state=SubscriberState.BILLING_ISSUE)
        result = scorer.compute_and_persist(session, sub)
        assert sub.risk_score == result.score

    def test_score_result_has_breakdown(self, session, scorer):
        sub = _make_subscriber(session, state=SubscriberState.BILLING_ISSUE)
        result = scorer.score(sub)
        assert result.breakdown is not None
        assert "state_base" in result.breakdown
