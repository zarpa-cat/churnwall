"""Tests for the webhook receiver endpoint."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from churnwall.app import create_app
from churnwall.db import get_db
from churnwall.models import Base, SubscriberState

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def test_engine():
    eng = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(eng)
    yield eng
    Base.metadata.drop_all(eng)


@pytest.fixture
def client(test_engine):
    TestSession = sessionmaker(bind=test_engine)

    def override_get_db():
        db = TestSession()
        try:
            yield db
            db.commit()
        except Exception:
            db.rollback()
            raise
        finally:
            db.close()

    app = create_app()
    app.dependency_overrides[get_db] = override_get_db
    return TestClient(app)


def _purchase_payload(customer_id: str = "cust_001", app_id: str = "app_test") -> dict:
    return {
        "api_version": "1.0",
        "event": {
            "event_type": "INITIAL_PURCHASE",
            "id": "evt_001",
            "app_id": app_id,
            "app_user_id": customer_id,
            "original_app_user_id": customer_id,
            "product_id": "premium_monthly",
            "store": "app_store",
            "environment": "SANDBOX",
            "purchased_at_ms": 1741305600000,
        },
    }


def _event_payload(event_type: str, customer_id: str = "cust_001") -> dict:
    p = _purchase_payload(customer_id)
    p["event"]["event_type"] = event_type
    return p


# ---------------------------------------------------------------------------
# Happy path
# ---------------------------------------------------------------------------


def test_initial_purchase_returns_ok(client):
    resp = client.post("/webhook", json=_purchase_payload())
    assert resp.status_code == 200
    data = resp.json()
    assert data["status"] == "ok"
    assert data["to_state"] == SubscriberState.ACTIVE


def test_renewal_after_purchase(client):
    client.post("/webhook", json=_purchase_payload())
    resp = client.post("/webhook", json=_event_payload("RENEWAL"))
    assert resp.status_code == 200
    assert resp.json()["to_state"] == SubscriberState.ACTIVE


def test_cancellation_after_purchase(client):
    client.post("/webhook", json=_purchase_payload())
    resp = client.post("/webhook", json=_event_payload("CANCELLATION"))
    assert resp.status_code == 200
    assert resp.json()["to_state"] == SubscriberState.CHURNED


def test_billing_issue_after_purchase(client):
    client.post("/webhook", json=_purchase_payload())
    resp = client.post("/webhook", json=_event_payload("BILLING_ISSUE"))
    assert resp.status_code == 200
    assert resp.json()["to_state"] == SubscriberState.BILLING_ISSUE


def test_winback_after_churn(client):
    client.post("/webhook", json=_purchase_payload())
    client.post("/webhook", json=_event_payload("CANCELLATION"))
    resp = client.post("/webhook", json=_event_payload("INITIAL_PURCHASE"))
    assert resp.status_code == 200
    assert resp.json()["to_state"] == SubscriberState.REACTIVATED


# ---------------------------------------------------------------------------
# Unknown event types
# ---------------------------------------------------------------------------


def test_unknown_event_type_skipped(client):
    payload = _purchase_payload()
    payload["event"]["event_type"] = "SOME_FUTURE_EVENT"
    resp = client.post("/webhook", json=payload)
    assert resp.status_code == 200
    assert resp.json()["status"] == "skipped"


# ---------------------------------------------------------------------------
# Malformed payloads
# ---------------------------------------------------------------------------


def test_invalid_json_returns_400(client):
    resp = client.post(
        "/webhook", content=b"not json", headers={"Content-Type": "application/json"}
    )
    assert resp.status_code == 400


def test_missing_event_field_returns_422(client):
    resp = client.post("/webhook", json={"api_version": "1.0"})
    assert resp.status_code == 422


def test_missing_customer_id_returns_422(client):
    payload = _purchase_payload()
    del payload["event"]["app_user_id"]
    del payload["event"]["original_app_user_id"]
    resp = client.post("/webhook", json=payload)
    assert resp.status_code == 422


# ---------------------------------------------------------------------------
# Webhook authorization
# ---------------------------------------------------------------------------


def test_auth_skipped_when_key_not_configured(client, monkeypatch):
    """When RC_WEBHOOK_AUTH_KEY is unset, any (or no) auth header is accepted."""
    monkeypatch.delenv("RC_WEBHOOK_AUTH_KEY", raising=False)
    resp = client.post("/webhook", json=_purchase_payload())
    assert resp.status_code == 200


def test_auth_accepted_with_correct_key(client, monkeypatch):
    """Correct Authorization header is accepted."""
    monkeypatch.setenv("RC_WEBHOOK_AUTH_KEY", "supersecret")
    resp = client.post(
        "/webhook",
        json=_purchase_payload(),
        headers={"Authorization": "supersecret"},
    )
    assert resp.status_code == 200


def test_auth_rejected_with_wrong_key(client, monkeypatch):
    """Wrong Authorization header returns 401."""
    monkeypatch.setenv("RC_WEBHOOK_AUTH_KEY", "supersecret")
    resp = client.post(
        "/webhook",
        json=_purchase_payload(),
        headers={"Authorization": "wrongsecret"},
    )
    assert resp.status_code == 401


def test_auth_rejected_when_header_missing(client, monkeypatch):
    """Missing Authorization header returns 401 when key is configured."""
    monkeypatch.setenv("RC_WEBHOOK_AUTH_KEY", "supersecret")
    resp = client.post("/webhook", json=_purchase_payload())
    assert resp.status_code == 401
