"""Subscriber state machine.

Applies RC webhook events to subscriber state, recording the full transition history.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime

from sqlalchemy.orm import Session

from churnwall.models import (
    STATE_TRANSITIONS,
    RCEventType,
    Subscriber,
    SubscriberEvent,
    SubscriberState,
)

logger = logging.getLogger(__name__)


class UnknownEventTypeError(ValueError):
    pass


class StateMachine:
    """Applies events to subscriber state."""

    def apply(
        self,
        session: Session,
        customer_id: str,
        project_id: str,
        event_type: RCEventType,
        occurred_at: datetime,
        product_id: str | None = None,
        app_user_id: str | None = None,
        store: str | None = None,
        raw_payload: dict | None = None,
    ) -> tuple[Subscriber, SubscriberEvent]:
        """Apply an RC event to a subscriber, creating them if needed.

        Returns (subscriber, event) after applying the transition.
        """
        # Get or create subscriber
        subscriber = session.query(Subscriber).filter_by(customer_id=customer_id).first()
        if subscriber is None:
            subscriber = Subscriber(
                customer_id=customer_id,
                project_id=project_id,
                state=SubscriberState.UNKNOWN,
                first_seen_at=occurred_at,
            )
            session.add(subscriber)
            session.flush()

        from_state = subscriber.state

        # Determine new state
        transition_key = (from_state, event_type)
        if transition_key not in STATE_TRANSITIONS:
            # Graceful fallback: log and keep current state
            logger.warning(
                "No transition defined for (%s, %s) — subscriber %s stays in %s",
                from_state,
                event_type,
                customer_id,
                from_state,
            )
            to_state = from_state
        else:
            to_state = STATE_TRANSITIONS[transition_key]

        # Update subscriber fields
        subscriber.state = to_state
        subscriber.last_event_at = occurred_at
        if app_user_id:
            subscriber.app_user_id = app_user_id
        if store:
            subscriber.store = store
        if product_id:
            subscriber.product_id = product_id

        # Update timestamps based on event type
        self._update_timestamps(subscriber, event_type, occurred_at)

        # Update counters
        if event_type == RCEventType.RENEWAL:
            subscriber.renewal_count += 1
        if event_type == RCEventType.BILLING_ISSUE:
            subscriber.billing_failure_count += 1
            subscriber.last_billing_failure_at = occurred_at

        # Record event
        event = SubscriberEvent(
            subscriber_id=subscriber.id,
            event_type=event_type,
            occurred_at=occurred_at,
            from_state=from_state,
            to_state=to_state,
            product_id=product_id,
            raw_payload=json.dumps(raw_payload) if raw_payload else None,
        )
        session.add(event)

        logger.debug(
            "Subscriber %s: %s → %s via %s",
            customer_id,
            from_state,
            to_state,
            event_type,
        )

        return subscriber, event

    def _update_timestamps(
        self,
        subscriber: Subscriber,
        event_type: RCEventType,
        occurred_at: datetime,
    ) -> None:
        """Update relevant timestamps based on the event type."""
        if event_type in (RCEventType.TRIAL_STARTED,):
            subscriber.trial_started_at = subscriber.trial_started_at or occurred_at
        elif event_type in (
            RCEventType.INITIAL_PURCHASE,
            RCEventType.TRIAL_CONVERTED,
        ):
            if subscriber.converted_at is None:
                subscriber.converted_at = occurred_at
        elif event_type in (
            RCEventType.CANCELLATION,
            RCEventType.EXPIRATION,
            RCEventType.TRIAL_CANCELLED,
            RCEventType.TRIAL_EXPIRED,
        ):
            # Only set churned_at if we're actually moving to churned
            # (cancellation may precede expiration — record first occurrence)
            if subscriber.churned_at is None:
                subscriber.churned_at = occurred_at
        elif event_type == RCEventType.RENEWAL and subscriber.state == SubscriberState.ACTIVE:
            # Clear churned_at on recovery (shouldn't happen, but be safe)
            pass

        if event_type == RCEventType.INITIAL_PURCHASE and subscriber.churned_at is not None:
            # Win-back
            subscriber.reactivated_at = occurred_at
            subscriber.churned_at = None  # Reset for next potential churn


state_machine = StateMachine()
