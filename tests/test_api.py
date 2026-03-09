"""Tests for the churnwall REST API (Phase 2c)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from churnwall.app import create_app
from churnwall.db import get_db
from churnwall.models import Base, Subscriber, SubscriberState

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
def db_session(engine):
    Session = sessionmaker(bind=engine, autocommit=False, autoflush=False)
    session = Session()
    yield session
    session.close()


@pytest.fixture
def client(engine):
    app = create_app()
    Session = sessionmaker(bind=engine, autocommit=False, autoflush=False)

    def override_get_db():
        db = Session()
        try:
            yield db
            db.commit()
        except Exception:
            db.rollback()
            raise
        finally:
            db.close()

    app.dependency_overrides[get_db] = override_get_db
    with TestClient(app) as c:
        yield c


def _make_sub(
    session,
    customer_id: str = "usr_001",
    state: SubscriberState = SubscriberState.ACTIVE,
    renewal_count: int = 3,
    billing_failure_count: int = 0,
    risk_score: float | None = None,
    project_id: str = "proj_test",
) -> Subscriber:
    now = datetime.now(UTC).replace(tzinfo=None)
    sub = Subscriber(
        customer_id=customer_id,
        project_id=project_id,
        state=state,
        renewal_count=renewal_count,
        billing_failure_count=billing_failure_count,
        risk_score=risk_score,
        last_event_at=now - timedelta(days=3),
    )
    session.add(sub)
    session.commit()
    return sub


# ── GET /api/subscribers ──────────────────────────────────────────────────────


class TestListSubscribers:
    def test_empty_returns_list(self, client):
        r = client.get("/api/subscribers")
        assert r.status_code == 200
        assert r.json() == []

    def test_returns_all_subscribers(self, client, db_session):
        _make_sub(db_session, customer_id="a")
        _make_sub(db_session, customer_id="b")
        r = client.get("/api/subscribers")
        assert r.status_code == 200
        assert len(r.json()) == 2

    def test_filter_by_state(self, client, db_session):
        _make_sub(db_session, customer_id="active1", state=SubscriberState.ACTIVE)
        _make_sub(db_session, customer_id="churned1", state=SubscriberState.CHURNED)
        r = client.get("/api/subscribers?state=churned")
        assert r.status_code == 200
        data = r.json()
        assert len(data) == 1
        assert data[0]["customer_id"] == "churned1"

    def test_filter_by_invalid_state(self, client):
        r = client.get("/api/subscribers?state=flying")
        assert r.status_code == 400

    def test_filter_by_risk_min(self, client, db_session):
        _make_sub(db_session, customer_id="low", risk_score=20.0)
        _make_sub(db_session, customer_id="high", risk_score=75.0)
        r = client.get("/api/subscribers?risk_min=60")
        assert r.status_code == 200
        data = r.json()
        assert len(data) == 1
        assert data[0]["customer_id"] == "high"

    def test_response_includes_risk_band(self, client, db_session):
        _make_sub(db_session, customer_id="x", risk_score=72.0)
        r = client.get("/api/subscribers")
        data = r.json()
        assert data[0]["risk_band"] == "high"

    def test_pagination_limit(self, client, db_session):
        for i in range(5):
            _make_sub(db_session, customer_id=f"usr_{i}")
        r = client.get("/api/subscribers?limit=3")
        assert r.status_code == 200
        assert len(r.json()) == 3

    def test_pagination_offset(self, client, db_session):
        for i in range(5):
            _make_sub(db_session, customer_id=f"usr_{i}", risk_score=float(i * 10))
        r = client.get("/api/subscribers?limit=3&offset=3")
        assert r.status_code == 200
        assert len(r.json()) == 2


# ── GET /api/subscribers/{customer_id} ────────────────────────────────────────


class TestGetSubscriber:
    def test_existing_subscriber(self, client, db_session):
        _make_sub(db_session, customer_id="usr_abc")
        r = client.get("/api/subscribers/usr_abc")
        assert r.status_code == 200
        assert r.json()["customer_id"] == "usr_abc"

    def test_missing_subscriber_404(self, client):
        r = client.get("/api/subscribers/nonexistent")
        assert r.status_code == 404

    def test_detail_includes_timestamps(self, client, db_session):
        _make_sub(db_session, customer_id="usr_ts")
        r = client.get("/api/subscribers/usr_ts")
        data = r.json()
        assert "last_event_at" in data

    def test_detail_includes_store_fields(self, client, db_session):
        sub = _make_sub(db_session, customer_id="usr_store")
        sub.store = "app_store"
        sub.product_id = "premium_monthly"
        db_session.commit()
        r = client.get("/api/subscribers/usr_store")
        data = r.json()
        assert data["store"] == "app_store"
        assert data["product_id"] == "premium_monthly"


# ── GET /api/subscribers/{customer_id}/recommend ──────────────────────────────


class TestGetRecommendations:
    def test_recommend_existing(self, client, db_session):
        _make_sub(db_session, customer_id="usr_rec", state=SubscriberState.ACTIVE)
        r = client.get("/api/subscribers/usr_rec/recommend")
        assert r.status_code == 200
        data = r.json()
        assert "recommendations" in data
        assert "top_action" in data
        assert len(data["recommendations"]) > 0

    def test_recommend_missing_404(self, client):
        r = client.get("/api/subscribers/ghost/recommend")
        assert r.status_code == 404

    def test_recommend_churned_winback(self, client, db_session):
        _make_sub(db_session, customer_id="usr_churned", state=SubscriberState.CHURNED)
        r = client.get("/api/subscribers/usr_churned/recommend")
        data = r.json()
        assert data["top_action"] == "send_winback_offer"

    def test_recommend_billing_issue_immediate(self, client, db_session):
        _make_sub(
            db_session,
            customer_id="usr_billing",
            state=SubscriberState.BILLING_ISSUE,
            billing_failure_count=2,
        )
        r = client.get("/api/subscribers/usr_billing/recommend")
        data = r.json()
        urgencies = [rec["urgency"] for rec in data["recommendations"]]
        assert "immediate" in urgencies

    def test_recommend_includes_risk_score(self, client, db_session):
        _make_sub(db_session, customer_id="usr_score", state=SubscriberState.ACTIVE)
        r = client.get("/api/subscribers/usr_score/recommend")
        data = r.json()
        assert isinstance(data["risk_score"], float)
        assert 0 <= data["risk_score"] <= 100

    def test_recommend_updates_risk_score_in_db(self, client, db_session):
        sub = _make_sub(db_session, customer_id="usr_persist")
        assert sub.risk_score is None
        client.get("/api/subscribers/usr_persist/recommend")
        db_session.refresh(sub)
        assert sub.risk_score is not None


# ── GET /api/at-risk ──────────────────────────────────────────────────────────


class TestAtRisk:
    def test_returns_high_risk_only(self, client, db_session):
        _make_sub(db_session, customer_id="safe", risk_score=25.0)
        _make_sub(db_session, customer_id="risky", risk_score=75.0)
        r = client.get("/api/at-risk")
        assert r.status_code == 200
        data = r.json()
        assert len(data) == 1
        assert data[0]["customer_id"] == "risky"

    def test_custom_threshold(self, client, db_session):
        _make_sub(db_session, customer_id="med", risk_score=45.0)
        _make_sub(db_session, customer_id="high", risk_score=80.0)
        r = client.get("/api/at-risk?threshold=40")
        data = r.json()
        assert len(data) == 2

    def test_sorted_highest_first(self, client, db_session):
        _make_sub(db_session, customer_id="a", risk_score=65.0)
        _make_sub(db_session, customer_id="b", risk_score=90.0)
        _make_sub(db_session, customer_id="c", risk_score=72.0)
        r = client.get("/api/at-risk?threshold=60")
        data = r.json()
        scores = [d["risk_score"] for d in data]
        assert scores == sorted(scores, reverse=True)

    def test_no_scored_subscribers_empty(self, client, db_session):
        # Subscribers with no risk_score don't appear
        _make_sub(db_session, customer_id="unscored", risk_score=None)
        r = client.get("/api/at-risk")
        data = r.json()
        assert len(data) == 0


# ── POST /api/score ───────────────────────────────────────────────────────────


class TestRunScore:
    def test_score_all_returns_counts(self, client, db_session):
        _make_sub(db_session, customer_id="a", state=SubscriberState.ACTIVE)
        _make_sub(
            db_session,
            customer_id="b",
            state=SubscriberState.BILLING_ISSUE,
            billing_failure_count=2,
        )
        r = client.post("/api/score")
        assert r.status_code == 200
        data = r.json()
        assert data["subscribers_scored"] == 2
        assert "high_risk" in data
        assert "critical" in data

    def test_score_empty_db(self, client):
        r = client.post("/api/score")
        assert r.status_code == 200
        assert r.json()["subscribers_scored"] == 0

    def test_score_project_filter(self, client, db_session):
        _make_sub(db_session, customer_id="p1", project_id="proj_a")
        _make_sub(db_session, customer_id="p2", project_id="proj_b")
        r = client.post("/api/score?project_id=proj_a")
        assert r.status_code == 200
        assert r.json()["subscribers_scored"] == 1

    def test_score_updates_db(self, client, db_session):
        sub = _make_sub(db_session, customer_id="usr_update")
        assert sub.risk_score is None
        client.post("/api/score")
        db_session.refresh(sub)
        assert sub.risk_score is not None
