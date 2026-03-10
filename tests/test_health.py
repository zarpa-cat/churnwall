"""Tests for the health check endpoint."""

from __future__ import annotations

from fastapi.testclient import TestClient

from churnwall.app import create_app


def test_health_returns_ok():
    app = create_app()
    client = TestClient(app)
    resp = client.get("/health")
    assert resp.status_code == 200
    data = resp.json()
    assert data["status"] == "ok"
    assert data["service"] == "churnwall"
