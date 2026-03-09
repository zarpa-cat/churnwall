"""Recommendation engine for churnwall.

Takes a subscriber + their ScoreResult and produces prioritised, actionable
retention recommendations. Each recommendation tells the caller *what* to do,
*why*, and with what *urgency*.

Urgency bands:
  immediate — act within hours (billing failure, expired trial)
  soon      — act within 24–72h (high risk active subscriber)
  monitor   — nothing to do right now, keep watching
"""

from __future__ import annotations

import enum
from dataclasses import dataclass, field

from churnwall.models import Subscriber, SubscriberState
from churnwall.scorer import RiskBand, ScoreResult


class Urgency(str, enum.Enum):
    IMMEDIATE = "immediate"
    SOON = "soon"
    MONITOR = "monitor"


class ActionType(str, enum.Enum):
    # Win-back
    SEND_WINBACK_OFFER = "send_winback_offer"
    # Billing recovery
    PROMPT_PAYMENT_UPDATE = "prompt_payment_update"
    SEND_BILLING_FAILURE_ALERT = "send_billing_failure_alert"
    # Trial nudges
    SEND_TRIAL_CONVERSION_NUDGE = "send_trial_conversion_nudge"
    SEND_TRIAL_FEATURE_HIGHLIGHT = "send_trial_feature_highlight"
    # Retention
    SEND_LOYALTY_DISCOUNT = "send_loyalty_discount"
    SEND_ENGAGEMENT_CHECKIN = "send_engagement_checkin"
    # Proactive
    SEND_RENEWAL_REMINDER = "send_renewal_reminder"
    # Passive
    MONITOR = "monitor"


@dataclass
class Recommendation:
    action: ActionType
    urgency: Urgency
    reason: str
    # Optional metadata for downstream systems (e.g. discount %, channel)
    metadata: dict[str, str | int | float] = field(default_factory=dict)

    # Lower number = higher priority
    priority: int = 50


@dataclass
class RecommendationResult:
    customer_id: str
    state: str
    risk_score: float
    risk_band: str
    recommendations: list[Recommendation]

    @property
    def top(self) -> Recommendation | None:
        """Highest-priority recommendation (lowest priority number)."""
        if not self.recommendations:
            return None
        return sorted(self.recommendations, key=lambda r: r.priority)[0]


class RetentionRecommender:
    """Produces retention recommendations for a subscriber given their risk score."""

    def recommend(self, subscriber: Subscriber, score: ScoreResult) -> RecommendationResult:
        recs: list[Recommendation] = []

        state = subscriber.state
        band = score.band
        failures = subscriber.billing_failure_count or 0
        renewals = subscriber.renewal_count or 0

        # ── CHURNED ─────────────────────────────────────────────────────────
        if state == SubscriberState.CHURNED:
            recs.append(
                Recommendation(
                    action=ActionType.SEND_WINBACK_OFFER,
                    urgency=Urgency.SOON,
                    reason="Subscriber has churned. A win-back offer improves reactivation odds.",
                    metadata={"discount_pct": 30, "channel": "email"},
                    priority=10,
                )
            )

        # ── BILLING_ISSUE ────────────────────────────────────────────────────
        elif state == SubscriberState.BILLING_ISSUE:
            recs.append(
                Recommendation(
                    action=ActionType.SEND_BILLING_FAILURE_ALERT,
                    urgency=Urgency.IMMEDIATE,
                    reason="Billing has failed. Alert the subscriber immediately to prevent churn.",
                    metadata={"channel": "email", "failure_count": failures},
                    priority=5,
                )
            )
            recs.append(
                Recommendation(
                    action=ActionType.PROMPT_PAYMENT_UPDATE,
                    urgency=Urgency.IMMEDIATE,
                    reason=(
                        "Prompt subscriber to update their payment method"
                        " before the grace period expires."
                    ),
                    metadata={"channel": "push"},
                    priority=6,
                )
            )

        # ── TRIALING ─────────────────────────────────────────────────────────
        elif state == SubscriberState.TRIALING:
            recs.append(
                Recommendation(
                    action=ActionType.SEND_TRIAL_FEATURE_HIGHLIGHT,
                    urgency=Urgency.SOON,
                    reason=(
                        "Trial subscriber hasn't converted yet."
                        " Highlighting key features improves conversion."
                    ),
                    metadata={"channel": "email"},
                    priority=20,
                )
            )
            if band in (RiskBand.HIGH, RiskBand.CRITICAL):
                recs.append(
                    Recommendation(
                        action=ActionType.SEND_TRIAL_CONVERSION_NUDGE,
                        urgency=Urgency.IMMEDIATE,
                        reason=(
                            "Trial subscriber shows high churn risk."
                            " A targeted nudge (e.g. discount) may convert them."
                        ),
                        metadata={"discount_pct": 20, "channel": "email"},
                        priority=15,
                    )
                )

        # ── ACTIVE / REACTIVATED ──────────────────────────────────────────────
        elif state in (SubscriberState.ACTIVE, SubscriberState.REACTIVATED):
            if band == RiskBand.CRITICAL:
                recs.append(
                    Recommendation(
                        action=ActionType.SEND_LOYALTY_DISCOUNT,
                        urgency=Urgency.IMMEDIATE,
                        reason=(
                            "Active subscriber is at critical churn risk."
                            " An immediate loyalty offer may prevent cancellation."
                        ),
                        metadata={"discount_pct": 25, "channel": "email"},
                        priority=10,
                    )
                )
            elif band == RiskBand.HIGH:
                if renewals >= 6:
                    # Long-term subscriber — respect the relationship
                    recs.append(
                        Recommendation(
                            action=ActionType.SEND_LOYALTY_DISCOUNT,
                            urgency=Urgency.SOON,
                            reason=(
                            f"Long-term subscriber ({renewals} renewals) shows elevated churn risk."
                            " A loyalty discount retains them without devaluing."
                        ),
                            metadata={"discount_pct": 15, "channel": "email"},
                            priority=20,
                        )
                    )
                else:
                    recs.append(
                        Recommendation(
                            action=ActionType.SEND_ENGAGEMENT_CHECKIN,
                            urgency=Urgency.SOON,
                            reason=(
                        "Subscriber shows high churn risk."
                        " An engagement check-in can surface friction or missed value."
                    ),
                            metadata={"channel": "email"},
                            priority=25,
                        )
                    )
            elif band == RiskBand.MEDIUM:
                recs.append(
                    Recommendation(
                        action=ActionType.SEND_RENEWAL_REMINDER,
                        urgency=Urgency.MONITOR,
                        reason=(
                            "Subscriber is in the medium-risk band."
                            " A soft renewal reminder keeps them engaged."
                        ),
                        metadata={"channel": "email"},
                        priority=40,
                    )
                )
            else:
                # LOW risk — healthy
                recs.append(
                    Recommendation(
                        action=ActionType.MONITOR,
                        urgency=Urgency.MONITOR,
                        reason="Subscriber is healthy. No action required.",
                        metadata={},
                        priority=100,
                    )
                )

        # ── UNKNOWN / fallback ────────────────────────────────────────────────
        else:
            recs.append(
                Recommendation(
                    action=ActionType.MONITOR,
                    urgency=Urgency.MONITOR,
                    reason="Subscriber state is unknown. Await more events before acting.",
                    metadata={},
                    priority=100,
                )
            )

        return RecommendationResult(
            customer_id=subscriber.customer_id,
            state=subscriber.state.value,
            risk_score=round(score.score, 1),
            risk_band=score.band.value,
            recommendations=sorted(recs, key=lambda r: r.priority),
        )

    def recommend_batch(
        self, pairs: list[tuple[Subscriber, ScoreResult]]
    ) -> list[RecommendationResult]:
        """Produce recommendations for a batch of (subscriber, score) pairs."""
        return [self.recommend(sub, score) for sub, score in pairs]


# Module-level singleton
recommender = RetentionRecommender()
