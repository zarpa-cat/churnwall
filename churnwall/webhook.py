"""RC webhook receiver.

Parses RevenueCat webhook payloads and applies them to the subscriber state machine.
Reference: https://www.revenuecat.com/docs/integrations/webhooks/event-types-and-fields
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, field_validator
from sqlalchemy.orm import Session

from churnwall.db import get_db
from churnwall.models import RCEventType
from churnwall.state_machine import state_machine

logger = logging.getLogger(__name__)
router = APIRouter()


class RCWebhookEvent(BaseModel):
    """Parsed RC webhook event. Only the fields we care about."""

    event_type: str
    id: str
    app_id: str | None = None
    app_user_id: str | None = None
    original_app_user_id: str | None = None
    product_id: str | None = None
    store: str | None = None
    environment: str | None = None
    purchased_at_ms: int | None = None
    expiration_at_ms: int | None = None

    @field_validator("event_type")
    @classmethod
    def validate_event_type(cls, v: str) -> str:
        # Allow unknown event types — we handle them gracefully in the state machine
        return v.upper()


class RCWebhookPayload(BaseModel):
    """Top-level RC webhook payload."""

    event: RCWebhookEvent
    api_version: str | None = None


def _parse_event_type(raw: str) -> RCEventType | None:
    """Map RC event type string to our enum. Returns None for unknown types."""
    try:
        return RCEventType(raw.upper())
    except ValueError:
        return None


def _ms_to_datetime(ms: int | None) -> datetime | None:
    if ms is None:
        return None
    return datetime.fromtimestamp(ms / 1000, tz=timezone.utc).replace(tzinfo=None)


@router.post("/webhook")
async def receive_webhook(
    request: Request,
    db: Session = Depends(get_db),
) -> dict:
    """Receive and process a RevenueCat webhook event."""
    try:
        body = await request.json()
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid JSON payload")

    try:
        payload = RCWebhookPayload.model_validate(body)
    except Exception as exc:
        raise HTTPException(status_code=422, detail=f"Invalid payload structure: {exc}")

    event = payload.event
    event_type = _parse_event_type(event.event_type)

    if event_type is None:
        # Unknown event type — acknowledge but don't process
        logger.info("Unknown RC event type %r — skipping", event.event_type)
        return {"status": "skipped", "reason": f"unknown event type: {event.event_type}"}

    # Use original_app_user_id as the stable customer identifier
    customer_id = event.original_app_user_id or event.app_user_id
    if not customer_id:
        raise HTTPException(status_code=422, detail="No customer identifier in payload")

    occurred_at = _ms_to_datetime(event.purchased_at_ms) or datetime.utcnow()

    # Derive project_id from app_id (simplification — in production, map app_id → project_id)
    project_id = event.app_id or "unknown"

    subscriber, sub_event = state_machine.apply(
        session=db,
        customer_id=customer_id,
        project_id=project_id,
        event_type=event_type,
        occurred_at=occurred_at,
        product_id=event.product_id,
        app_user_id=event.app_user_id,
        store=event.store,
        raw_payload=body,
    )

    return {
        "status": "ok",
        "customer_id": customer_id,
        "from_state": sub_event.from_state,
        "to_state": sub_event.to_state,
        "event_type": event_type,
    }
