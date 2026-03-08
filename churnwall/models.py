"""SQLAlchemy ORM models for churnwall.

Subscriber state machine:
  trialing → active
  trialing → churned  (trial cancelled before conversion)
  active → billing_issue
  active → churned  (voluntary cancellation + expiration)
  billing_issue → active  (payment recovered)
  billing_issue → churned  (grace period expired)
  churned → reactivated  (win-back purchase)
  reactivated → active
  reactivated → billing_issue
  reactivated → churned
"""

from __future__ import annotations

import enum
from datetime import UTC, datetime

from sqlalchemy import DateTime, Enum, Float, ForeignKey, Integer, String, Text
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


class Base(DeclarativeBase):
    pass


class SubscriberState(str, enum.Enum):
    TRIALING = "trialing"
    ACTIVE = "active"
    BILLING_ISSUE = "billing_issue"
    CHURNED = "churned"
    REACTIVATED = "reactivated"
    UNKNOWN = "unknown"


class RCEventType(str, enum.Enum):
    INITIAL_PURCHASE = "INITIAL_PURCHASE"
    RENEWAL = "RENEWAL"
    CANCELLATION = "CANCELLATION"
    UNCANCELLATION = "UNCANCELLATION"
    BILLING_ISSUE = "BILLING_ISSUE"
    EXPIRATION = "EXPIRATION"
    PRODUCT_CHANGE = "PRODUCT_CHANGE"
    TRANSFER = "TRANSFER"
    SUBSCRIBER_ALIAS = "SUBSCRIBER_ALIAS"
    # Trial events
    TRIAL_STARTED = "TRIAL_STARTED"
    TRIAL_CONVERTED = "TRIAL_CONVERTED"
    TRIAL_CANCELLED = "TRIAL_CANCELLED"
    TRIAL_EXPIRED = "TRIAL_EXPIRED"


# Valid state transitions: (from_state, event_type) → to_state
STATE_TRANSITIONS: dict[tuple[SubscriberState, RCEventType], SubscriberState] = {
    # New subscriber
    (SubscriberState.UNKNOWN, RCEventType.INITIAL_PURCHASE): SubscriberState.ACTIVE,
    (SubscriberState.UNKNOWN, RCEventType.TRIAL_STARTED): SubscriberState.TRIALING,
    # Trial paths
    (SubscriberState.TRIALING, RCEventType.TRIAL_CONVERTED): SubscriberState.ACTIVE,
    (SubscriberState.TRIALING, RCEventType.INITIAL_PURCHASE): SubscriberState.ACTIVE,
    (SubscriberState.TRIALING, RCEventType.TRIAL_CANCELLED): SubscriberState.CHURNED,
    (SubscriberState.TRIALING, RCEventType.TRIAL_EXPIRED): SubscriberState.CHURNED,
    (SubscriberState.TRIALING, RCEventType.CANCELLATION): SubscriberState.CHURNED,
    # Active paths
    (SubscriberState.ACTIVE, RCEventType.RENEWAL): SubscriberState.ACTIVE,
    (SubscriberState.ACTIVE, RCEventType.CANCELLATION): SubscriberState.CHURNED,
    (SubscriberState.ACTIVE, RCEventType.EXPIRATION): SubscriberState.CHURNED,
    (SubscriberState.ACTIVE, RCEventType.BILLING_ISSUE): SubscriberState.BILLING_ISSUE,
    (SubscriberState.ACTIVE, RCEventType.PRODUCT_CHANGE): SubscriberState.ACTIVE,
    (SubscriberState.ACTIVE, RCEventType.UNCANCELLATION): SubscriberState.ACTIVE,
    # Billing issue paths
    (SubscriberState.BILLING_ISSUE, RCEventType.RENEWAL): SubscriberState.ACTIVE,
    (SubscriberState.BILLING_ISSUE, RCEventType.INITIAL_PURCHASE): SubscriberState.ACTIVE,
    (SubscriberState.BILLING_ISSUE, RCEventType.EXPIRATION): SubscriberState.CHURNED,
    (SubscriberState.BILLING_ISSUE, RCEventType.CANCELLATION): SubscriberState.CHURNED,
    (SubscriberState.BILLING_ISSUE, RCEventType.BILLING_ISSUE): SubscriberState.BILLING_ISSUE,
    # Churned paths (win-back)
    (SubscriberState.CHURNED, RCEventType.INITIAL_PURCHASE): SubscriberState.REACTIVATED,
    (SubscriberState.CHURNED, RCEventType.TRIAL_STARTED): SubscriberState.TRIALING,
    # Reactivated paths
    (SubscriberState.REACTIVATED, RCEventType.RENEWAL): SubscriberState.ACTIVE,
    (SubscriberState.REACTIVATED, RCEventType.CANCELLATION): SubscriberState.CHURNED,
    (SubscriberState.REACTIVATED, RCEventType.BILLING_ISSUE): SubscriberState.BILLING_ISSUE,
    (SubscriberState.REACTIVATED, RCEventType.EXPIRATION): SubscriberState.CHURNED,
}


class Subscriber(Base):
    __tablename__ = "subscribers"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    customer_id: Mapped[str] = mapped_column(String(255), unique=True, index=True, nullable=False)
    project_id: Mapped[str] = mapped_column(String(255), nullable=False, index=True)
    app_user_id: Mapped[str | None] = mapped_column(String(255), nullable=True)

    state: Mapped[SubscriberState] = mapped_column(
        Enum(SubscriberState), default=SubscriberState.UNKNOWN, nullable=False
    )
    product_id: Mapped[str | None] = mapped_column(String(255), nullable=True)
    # app_store, play_store, stripe, rc_billing, etc.
    store: Mapped[str | None] = mapped_column(String(64), nullable=True)

    # Key timestamps
    first_seen_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    trial_started_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    converted_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    churned_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    reactivated_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    last_event_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)

    # Billing history counters
    renewal_count: Mapped[int] = mapped_column(Integer, default=0)
    billing_failure_count: Mapped[int] = mapped_column(Integer, default=0)
    last_billing_failure_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)

    # Risk
    risk_score: Mapped[float | None] = mapped_column(Float, nullable=True)
    risk_computed_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)

    created_at: Mapped[datetime] = mapped_column(
        DateTime, default=lambda: datetime.now(UTC).replace(tzinfo=None), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, default=datetime.utcnow, onupdate=datetime.utcnow, nullable=False
    )

    events: Mapped[list[SubscriberEvent]] = relationship(
        "SubscriberEvent", back_populates="subscriber", order_by="SubscriberEvent.occurred_at"
    )


class SubscriberEvent(Base):
    __tablename__ = "subscriber_events"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    subscriber_id: Mapped[int] = mapped_column(
        ForeignKey("subscribers.id"), nullable=False, index=True
    )

    event_type: Mapped[RCEventType] = mapped_column(Enum(RCEventType), nullable=False)
    occurred_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, index=True)

    from_state: Mapped[SubscriberState] = mapped_column(Enum(SubscriberState), nullable=False)
    to_state: Mapped[SubscriberState] = mapped_column(Enum(SubscriberState), nullable=False)

    product_id: Mapped[str | None] = mapped_column(String(255), nullable=True)
    raw_payload: Mapped[str | None] = mapped_column(Text, nullable=True)  # JSON

    created_at: Mapped[datetime] = mapped_column(
        DateTime, default=lambda: datetime.now(UTC).replace(tzinfo=None), nullable=False
    )

    subscriber: Mapped[Subscriber] = relationship("Subscriber", back_populates="events")
