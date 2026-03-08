"""Churn risk scorer for churnwall.

Computes a 0–100 risk score per subscriber based on:
  - Current subscriber state (base score)
  - Billing failure history
  - Renewal count (loyalty signal)
  - Recency of last event
  - Trial conversion speed (if applicable)

Score bands:
  0–29   → LOW      (healthy, retained)
  30–59  → MEDIUM   (watch)
  60–89  → HIGH     (at risk, intervention recommended)
  90–100 → CRITICAL (churned or imminent)
"""

from __future__ import annotations

import enum
from dataclasses import dataclass, field
from datetime import UTC, datetime

from sqlalchemy.orm import Session

from churnwall.models import Subscriber, SubscriberState


class RiskBand(str, enum.Enum):
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"
    CRITICAL = "critical"


@dataclass
class ScoreResult:
    score: float  # 0–100, higher = more likely to churn
    band: RiskBand
    breakdown: dict[str, float] = field(default_factory=dict)


# Base score per state — the prior before any modifiers
_STATE_BASE_SCORES: dict[SubscriberState, float] = {
    SubscriberState.ACTIVE: 20.0,
    SubscriberState.TRIALING: 40.0,
    SubscriberState.REACTIVATED: 35.0,
    SubscriberState.BILLING_ISSUE: 70.0,
    SubscriberState.CHURNED: 100.0,
    SubscriberState.UNKNOWN: 50.0,
}


def _risk_band(score: float) -> RiskBand:
    if score >= 90:
        return RiskBand.CRITICAL
    if score >= 60:
        return RiskBand.HIGH
    if score >= 30:
        return RiskBand.MEDIUM
    return RiskBand.LOW


class ChurnRiskScorer:
    """Scores subscriber churn risk on a 0–100 scale."""

    def score(self, subscriber: Subscriber) -> ScoreResult:
        """Compute a risk score without persisting it."""
        breakdown: dict[str, float] = {}

        # 1. Base score from state
        base = _STATE_BASE_SCORES.get(subscriber.state, 50.0)
        breakdown["state_base"] = base
        total = base

        # Churned is already max — no modifiers needed
        if subscriber.state == SubscriberState.CHURNED:
            return ScoreResult(score=100.0, band=RiskBand.CRITICAL, breakdown=breakdown)

        # 2. Billing failure penalty
        failures = subscriber.billing_failure_count or 0
        if failures == 0:
            billing_delta = -5.0
        elif failures == 1:
            billing_delta = 0.0
        elif failures == 2:
            billing_delta = 10.0
        else:
            billing_delta = min(20.0 + (failures - 3) * 3.0, 30.0)
        breakdown["billing_failures"] = billing_delta
        total += billing_delta

        # 3. Renewal count (loyalty discount)
        renewals = subscriber.renewal_count or 0
        if renewals == 0:
            renewal_delta = 10.0
        elif renewals <= 2:
            renewal_delta = 0.0
        elif renewals <= 5:
            renewal_delta = -5.0
        elif renewals <= 11:
            renewal_delta = -10.0
        else:
            renewal_delta = -15.0
        breakdown["renewals"] = renewal_delta
        total += renewal_delta

        # 4. Recency penalty
        now = datetime.now(UTC).replace(tzinfo=None)
        last_event = subscriber.last_event_at
        if last_event is not None:
            days_since = (now - last_event).days
            if days_since < 30:
                recency_delta = 0.0
            elif days_since < 60:
                recency_delta = 5.0
            elif days_since < 90:
                recency_delta = 10.0
            else:
                recency_delta = 15.0
        else:
            recency_delta = 10.0  # No event at all — moderate penalty
        breakdown["recency"] = recency_delta
        total += recency_delta

        # 5. Trial conversion speed bonus
        if subscriber.trial_started_at is not None and subscriber.converted_at is not None:
            trial_seconds = (subscriber.converted_at - subscriber.trial_started_at).total_seconds()
            trial_days = trial_seconds / 86400
            if trial_days < 1:
                conversion_delta = -5.0  # Fast converter → engaged
            elif trial_days < 3:
                conversion_delta = -3.0
            else:
                conversion_delta = 0.0
            breakdown["trial_conversion"] = conversion_delta
            total += conversion_delta

        # Clamp to [0, 100]
        final = max(0.0, min(100.0, total))
        return ScoreResult(score=final, band=_risk_band(final), breakdown=breakdown)

    def compute_and_persist(self, session: Session, subscriber: Subscriber) -> ScoreResult:
        """Compute score and write it back to the subscriber row."""
        result = self.score(subscriber)
        subscriber.risk_score = result.score
        subscriber.risk_computed_at = datetime.now(UTC).replace(tzinfo=None)
        session.flush()
        return result

    def score_all(
        self, session: Session, project_id: str | None = None
    ) -> list[tuple[Subscriber, ScoreResult]]:
        """Score all subscribers, optionally filtered by project.

        Persists scores as it goes. Returns (subscriber, result) pairs.
        """
        query = session.query(Subscriber)
        if project_id is not None:
            query = query.filter(Subscriber.project_id == project_id)

        results = []
        for sub in query.all():
            result = self.compute_and_persist(session, sub)
            results.append((sub, result))

        return results


# Module-level singleton
scorer = ChurnRiskScorer()
