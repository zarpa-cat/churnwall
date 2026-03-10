"""RC → churnwall sync engine.

Pulls subscriber data from the RevenueCat REST API and upserts it into the
local churnwall DB. Useful for:

- Seeding churnwall from an existing RC project (no webhook history replay needed)
- Backfilling subscribers that arrived before churnwall was running
- Refreshing stale records on demand

Usage (CLI):
    churnwall sync --customer-id user_abc123
    churnwall sync --from-file customer_ids.txt  # one ID per line

Usage (Python):
    from sqlalchemy.orm import Session
    from churnwall.sync import sync_subscriber, sync_from_ids

    async with RCClient(api_key="sk_...") as client:
        result = await sync_subscriber(client, "user_abc123", "proj_xyz", session)
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from datetime import UTC, datetime

from sqlalchemy.orm import Session

from churnwall.db import get_session
from churnwall.models import Subscriber, SubscriberState
from churnwall.rc_client import RCClient, SubscriberSnapshot
from churnwall.scorer import ChurnRiskScorer as ChurnScorer
from churnwall.settings import settings


@dataclass
class SyncResult:
    """Summary returned by sync operations."""

    created: int = 0
    updated: int = 0
    skipped: int = 0
    errors: list[str] = field(default_factory=list)

    @property
    def total(self) -> int:
        return self.created + self.updated + self.skipped

    def __str__(self) -> str:
        return (
            f"SyncResult(created={self.created}, updated={self.updated}, "
            f"skipped={self.skipped}, errors={len(self.errors)})"
        )


def _state_enum(state_str: str) -> SubscriberState:
    try:
        return SubscriberState(state_str)
    except ValueError:
        return SubscriberState.UNKNOWN


def _upsert_subscriber(
    snapshot: SubscriberSnapshot,
    project_id: str,
    session: Session,
) -> tuple[Subscriber, bool]:
    """Create or update a Subscriber record from a SubscriberSnapshot.

    Returns:
        (subscriber, created): created=True if this was a new record.
    """
    existing = session.query(Subscriber).filter_by(customer_id=snapshot.app_user_id).first()

    now = datetime.now(UTC).replace(tzinfo=None)

    if existing is None:
        sub = Subscriber(
            customer_id=snapshot.app_user_id,
            app_user_id=snapshot.app_user_id,
            project_id=project_id,
            state=_state_enum(snapshot.state),
            product_id=snapshot.product_id,
            store=snapshot.store,
            first_seen_at=snapshot.first_seen,
            trial_started_at=snapshot.trial_started_at,
            converted_at=snapshot.converted_at,
            churned_at=snapshot.churned_at,
            reactivated_at=snapshot.reactivated_at,
            last_event_at=snapshot.last_seen,
            renewal_count=snapshot.renewal_count,
            billing_failure_count=snapshot.billing_failure_count,
            last_billing_failure_at=snapshot.last_billing_failure_at,
            created_at=now,
            updated_at=now,
        )
        session.add(sub)
        return sub, True
    else:
        # Update mutable fields; preserve churnwall-computed fields where RC lacks data
        existing.state = _state_enum(snapshot.state)
        existing.product_id = snapshot.product_id or existing.product_id
        existing.store = snapshot.store or existing.store
        existing.first_seen_at = snapshot.first_seen or existing.first_seen_at
        existing.trial_started_at = snapshot.trial_started_at or existing.trial_started_at
        existing.converted_at = snapshot.converted_at or existing.converted_at
        if snapshot.churned_at:
            existing.churned_at = snapshot.churned_at
        if snapshot.reactivated_at:
            existing.reactivated_at = snapshot.reactivated_at
        existing.last_event_at = snapshot.last_seen or existing.last_event_at
        existing.billing_failure_count = max(
            existing.billing_failure_count, snapshot.billing_failure_count
        )
        if snapshot.last_billing_failure_at:
            existing.last_billing_failure_at = snapshot.last_billing_failure_at
        existing.updated_at = now
        return existing, False


async def sync_subscriber(
    client: RCClient,
    app_user_id: str,
    project_id: str,
    session: Session,
    *,
    score: bool = True,
) -> tuple[Subscriber, bool]:
    """Fetch a single subscriber from RC and upsert into the local DB.

    Args:
        client: Authenticated RCClient (open context manager).
        app_user_id: RC app user ID.
        project_id: churnwall project ID to tag the record with.
        session: SQLAlchemy session.
        score: If True, compute a churn risk score after syncing.

    Returns:
        (subscriber, created)
    """
    rc_data = await client.get_subscriber(app_user_id)
    snapshot = SubscriberSnapshot(app_user_id, rc_data)
    sub, created = _upsert_subscriber(snapshot, project_id, session)
    session.flush()

    if score:
        scorer = ChurnScorer()
        score_result = scorer.score(sub)
        sub.risk_score = score_result.score
        sub.risk_computed_at = datetime.now(UTC).replace(tzinfo=None)

    session.commit()
    return sub, created


async def sync_from_ids(
    app_user_ids: list[str],
    project_id: str,
    session: Session,
    *,
    api_key: str | None = None,
    concurrency: int = 5,
    score: bool = True,
) -> SyncResult:
    """Sync a batch of subscribers from RC.

    Args:
        app_user_ids: List of app user IDs to sync.
        project_id: churnwall project ID.
        session: SQLAlchemy session.
        api_key: RC secret API key. Falls back to settings.rc_api_key.
        concurrency: Max parallel RC API requests.
        score: If True, compute risk scores after syncing.

    Returns:
        SyncResult with counts of created/updated/skipped/errored.
    """
    key = api_key or settings.rc_api_key
    if not key:
        raise ValueError("No RC API key configured. Set RC_API_KEY environment variable.")

    result = SyncResult()
    scorer = ChurnScorer() if score else None

    async with RCClient(api_key=key, timeout=15.0) as client:
        rc_data = await client.get_subscribers_batch(app_user_ids, concurrency=concurrency)

    for uid, data in rc_data.items():
        try:
            snapshot = SubscriberSnapshot(uid, data)
            sub, created = _upsert_subscriber(snapshot, project_id, session)
            session.flush()

            if scorer:
                score_result = scorer.score(sub)
                sub.risk_score = score_result.score
                sub.risk_computed_at = datetime.now(UTC).replace(tzinfo=None)

            if created:
                result.created += 1
            else:
                result.updated += 1
        except Exception as exc:
            result.errors.append(f"{uid}: {exc}")

    missing = set(app_user_ids) - set(rc_data.keys())
    result.skipped = len(missing)

    session.commit()
    return result


def sync_from_file(
    filepath: str,
    project_id: str,
    *,
    api_key: str | None = None,
    concurrency: int = 5,
) -> SyncResult:
    """Sync subscribers from a text file (one app_user_id per line).

    Convenience wrapper for CLI use — runs the async sync synchronously.
    """
    with open(filepath) as f:
        ids = [line.strip() for line in f if line.strip() and not line.startswith("#")]

    if not ids:
        return SyncResult()

    session = next(get_session())
    try:
        return asyncio.run(
            sync_from_ids(ids, project_id, session, api_key=api_key, concurrency=concurrency)
        )
    finally:
        session.close()
