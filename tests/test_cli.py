"""Tests for the churnwall CLI (Phase 4 — CLI)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from unittest.mock import patch

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool
from typer.testing import CliRunner

from churnwall.cli import app
from churnwall.models import Base, RCEventType, Subscriber, SubscriberEvent, SubscriberState

runner = CliRunner()


# ── Fixtures ──────────────────────────────────────────────────────────────────


@pytest.fixture
def engine():
    eng = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(eng)
    yield eng
    Base.metadata.drop_all(eng)


@pytest.fixture
def session_factory(engine):
    return sessionmaker(bind=engine, autocommit=False, autoflush=False)


@pytest.fixture
def session(session_factory):
    sess = session_factory()
    yield sess
    sess.close()


def _make_subscriber(
    session,
    customer_id: str = "usr_test",
    state: SubscriberState = SubscriberState.ACTIVE,
    risk_score: float | None = 75.0,
    billing_failure_count: int = 0,
    renewal_count: int = 3,
    project_id: str = "proj_test",
) -> Subscriber:
    now = datetime.now(UTC).replace(tzinfo=None)
    sub = Subscriber(
        customer_id=customer_id,
        project_id=project_id,
        state=state,
        risk_score=risk_score,
        billing_failure_count=billing_failure_count,
        renewal_count=renewal_count,
        product_id="monthly_pro",
        store="app_store",
        first_seen_at=now - timedelta(days=30),
        last_event_at=now - timedelta(hours=2),
        last_billing_failure_at=now - timedelta(hours=1) if billing_failure_count else None,
    )
    session.add(sub)
    session.commit()
    return sub


def _make_billing_event(session, subscriber: Subscriber, hours_ago: float = 1.0) -> SubscriberEvent:
    occurred = datetime.now(UTC).replace(tzinfo=None) - timedelta(hours=hours_ago)
    event = SubscriberEvent(
        subscriber_id=subscriber.id,
        event_type=RCEventType.BILLING_ISSUE,
        occurred_at=occurred,
        from_state=SubscriberState.ACTIVE,
        to_state=SubscriberState.BILLING_ISSUE,
    )
    session.add(event)
    session.commit()
    return event


# ── subscribers command ───────────────────────────────────────────────────────


def test_subscribers_empty(engine, session_factory):
    with patch("churnwall.cli.SessionLocal", session_factory), \
         patch("churnwall.cli.init_db"):
        result = runner.invoke(app, ["subscribers"])
    assert result.exit_code == 0
    assert "No subscribers matched" in result.output


def test_subscribers_lists_results(engine, session_factory, session):
    _make_subscriber(session, customer_id="usr_alpha", risk_score=80.0)
    _make_subscriber(
        session, customer_id="usr_beta", risk_score=40.0, state=SubscriberState.TRIALING
    )

    with patch("churnwall.cli.SessionLocal", session_factory), \
         patch("churnwall.cli.init_db"):
        result = runner.invoke(app, ["subscribers"])

    assert result.exit_code == 0
    assert "usr_alpha" in result.output
    assert "usr_beta" in result.output
    assert "2 subscriber(s) shown" in result.output


def test_subscribers_filter_state(engine, session_factory, session):
    _make_subscriber(session, customer_id="usr_active", state=SubscriberState.ACTIVE)
    _make_subscriber(
        session, customer_id="usr_churned", state=SubscriberState.CHURNED, risk_score=95.0
    )

    with patch("churnwall.cli.SessionLocal", session_factory), \
         patch("churnwall.cli.init_db"):
        result = runner.invoke(app, ["subscribers", "--state", "churned"])

    assert result.exit_code == 0
    assert "usr_churned" in result.output
    assert "usr_active" not in result.output


def test_subscribers_filter_risk_min(engine, session_factory, session):
    _make_subscriber(session, customer_id="usr_high", risk_score=85.0)
    _make_subscriber(session, customer_id="usr_low", risk_score=20.0)

    with patch("churnwall.cli.SessionLocal", session_factory), \
         patch("churnwall.cli.init_db"):
        result = runner.invoke(app, ["subscribers", "--risk-min", "70"])

    assert result.exit_code == 0
    assert "usr_high" in result.output
    assert "usr_low" not in result.output


def test_subscribers_invalid_state(engine, session_factory):
    with patch("churnwall.cli.SessionLocal", session_factory), \
         patch("churnwall.cli.init_db"):
        result = runner.invoke(app, ["subscribers", "--state", "nonsense"])

    assert result.exit_code == 1
    assert "Unknown state" in result.output


def test_subscribers_filter_project(engine, session_factory, session):
    _make_subscriber(session, customer_id="usr_proj_a", project_id="proj_a")
    _make_subscriber(session, customer_id="usr_proj_b", project_id="proj_b")

    with patch("churnwall.cli.SessionLocal", session_factory), \
         patch("churnwall.cli.init_db"):
        result = runner.invoke(app, ["subscribers", "--project", "proj_a"])

    assert result.exit_code == 0
    assert "usr_proj_a" in result.output
    assert "usr_proj_b" not in result.output


# ── recommend command ─────────────────────────────────────────────────────────


def test_recommend_not_found(engine, session_factory):
    with patch("churnwall.cli.SessionLocal", session_factory), \
         patch("churnwall.cli.init_db"):
        result = runner.invoke(app, ["recommend", "--customer-id", "usr_ghost"])

    assert result.exit_code == 1
    assert "not found" in result.output


def test_recommend_billing_issue(engine, session_factory, session):
    _make_subscriber(
        session,
        customer_id="usr_billing",
        state=SubscriberState.BILLING_ISSUE,
        risk_score=88.0,
        billing_failure_count=2,
    )

    with patch("churnwall.cli.SessionLocal", session_factory), \
         patch("churnwall.cli.init_db"):
        result = runner.invoke(app, ["recommend", "--customer-id", "usr_billing"])

    assert result.exit_code == 0
    assert "usr_billing" in result.output
    assert "billing_issue" in result.output
    # Should have at least one recommendation (billing failure alert)
    assert "billing_failure_alert" in result.output or "payment_update" in result.output


def test_recommend_healthy_subscriber(engine, session_factory, session):
    _make_subscriber(
        session,
        customer_id="usr_healthy",
        state=SubscriberState.ACTIVE,
        risk_score=15.0,
        renewal_count=12,
        billing_failure_count=0,
    )

    with patch("churnwall.cli.SessionLocal", session_factory), \
         patch("churnwall.cli.init_db"):
        result = runner.invoke(app, ["recommend", "--customer-id", "usr_healthy"])

    assert result.exit_code == 0
    assert "usr_healthy" in result.output


# ── score command ─────────────────────────────────────────────────────────────


def test_score_empty_db(engine, session_factory):
    with patch("churnwall.cli.SessionLocal", session_factory), \
         patch("churnwall.cli.init_db"):
        result = runner.invoke(app, ["score"])

    assert result.exit_code == 0
    assert "Scored 0 subscriber(s)" in result.output


def test_score_runs_and_reports(engine, session_factory, session):
    _make_subscriber(session, customer_id="usr_s1", risk_score=None, state=SubscriberState.ACTIVE)
    _make_subscriber(
        session,
        customer_id="usr_s2",
        risk_score=None,
        state=SubscriberState.BILLING_ISSUE,
        billing_failure_count=3,
    )

    with patch("churnwall.cli.SessionLocal", session_factory), \
         patch("churnwall.cli.init_db"):
        result = runner.invoke(app, ["score"])

    assert result.exit_code == 0
    assert "Scored 2 subscriber(s)" in result.output


# ── cohort billing-failures command ───────────────────────────────────────────


def test_cohort_billing_failures_empty(engine, session_factory):
    with patch("churnwall.cli.SessionLocal", session_factory), \
         patch("churnwall.cli.init_db"):
        result = runner.invoke(app, ["cohort", "billing-failures", "--hours", "24"])

    assert result.exit_code == 0
    assert "No billing failures" in result.output


def test_cohort_billing_failures_shows_recent(engine, session_factory, session):
    sub = _make_subscriber(
        session,
        customer_id="usr_bf",
        state=SubscriberState.BILLING_ISSUE,
        billing_failure_count=2,
    )
    _make_billing_event(session, sub, hours_ago=5)  # within 48h window

    with patch("churnwall.cli.SessionLocal", session_factory), \
         patch("churnwall.cli.init_db"):
        result = runner.invoke(app, ["cohort", "billing-failures", "--hours", "48"])

    assert result.exit_code == 0
    assert "usr_bf" in result.output
    assert "1 subscriber(s)" in result.output


def test_cohort_billing_failures_outside_window(engine, session_factory, session):
    sub = _make_subscriber(
        session,
        customer_id="usr_old_bf",
        state=SubscriberState.BILLING_ISSUE,
        billing_failure_count=1,
    )
    _make_billing_event(session, sub, hours_ago=72)  # outside 48h window

    with patch("churnwall.cli.SessionLocal", session_factory), \
         patch("churnwall.cli.init_db"):
        result = runner.invoke(app, ["cohort", "billing-failures", "--hours", "48"])

    assert result.exit_code == 0
    assert "No billing failures" in result.output


def test_cohort_billing_failures_project_filter(engine, session_factory, session):
    sub_a = _make_subscriber(
        session, customer_id="usr_proj_a_bf", project_id="proj_a",
        state=SubscriberState.BILLING_ISSUE, billing_failure_count=1
    )
    sub_b = _make_subscriber(
        session, customer_id="usr_proj_b_bf", project_id="proj_b",
        state=SubscriberState.BILLING_ISSUE, billing_failure_count=1
    )
    _make_billing_event(session, sub_a, hours_ago=2)
    _make_billing_event(session, sub_b, hours_ago=2)

    with patch("churnwall.cli.SessionLocal", session_factory), \
         patch("churnwall.cli.init_db"):
        result = runner.invoke(app, ["cohort", "billing-failures", "--project", "proj_a"])

    assert result.exit_code == 0
    assert "usr_proj_a_bf" in result.output
    assert "usr_proj_b_bf" not in result.output
