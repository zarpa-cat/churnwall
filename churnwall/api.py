"""REST API routes for churnwall (Phase 2c).

Endpoints:
  GET  /subscribers                     — list all subscribers (with optional filters)
  GET  /subscribers/{customer_id}       — subscriber detail + latest risk score
  GET  /subscribers/{customer_id}/recommend — recommendations for a subscriber
  GET  /at-risk                         — subscribers above a risk threshold
  POST /score                           — trigger a full re-score pass
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel
from sqlalchemy.orm import Session

from churnwall.db import get_db as get_session
from churnwall.models import Subscriber, SubscriberState
from churnwall.recommender import RetentionRecommender
from churnwall.scorer import ChurnRiskScorer

router = APIRouter(prefix="/api", tags=["subscribers"])

_scorer = ChurnRiskScorer()
_recommender = RetentionRecommender()


# ── Pydantic response schemas ─────────────────────────────────────────────────


class SubscriberSummary(BaseModel):
    customer_id: str
    project_id: str
    state: str
    renewal_count: int
    billing_failure_count: int
    risk_score: float | None
    risk_band: str | None

    model_config = {"from_attributes": True}


class SubscriberDetail(SubscriberSummary):
    product_id: str | None = None
    store: str | None = None
    first_seen_at: str | None = None
    trial_started_at: str | None = None
    converted_at: str | None = None
    churned_at: str | None = None
    reactivated_at: str | None = None
    last_event_at: str | None = None
    risk_computed_at: str | None = None


class RecommendationOut(BaseModel):
    action: str
    urgency: str
    reason: str
    priority: int
    metadata: dict


class RecommendationResponse(BaseModel):
    customer_id: str
    state: str
    risk_score: float
    risk_band: str
    top_action: str | None
    recommendations: list[RecommendationOut]


class ScoreRunResult(BaseModel):
    subscribers_scored: int
    high_risk: int
    critical: int


# ── Helpers ───────────────────────────────────────────────────────────────────


def _band_from_score(score: float | None) -> str | None:
    if score is None:
        return None
    if score >= 90:
        return "critical"
    if score >= 60:
        return "high"
    if score >= 30:
        return "medium"
    return "low"


def _detail_from_sub(sub: Subscriber) -> SubscriberDetail:
    def _fmt(dt):
        return dt.isoformat() if dt else None

    return SubscriberDetail(
        customer_id=sub.customer_id,
        project_id=sub.project_id,
        state=sub.state.value,
        renewal_count=sub.renewal_count or 0,
        billing_failure_count=sub.billing_failure_count or 0,
        risk_score=sub.risk_score,
        risk_band=_band_from_score(sub.risk_score),
        product_id=sub.product_id,
        store=sub.store,
        first_seen_at=_fmt(sub.first_seen_at),
        trial_started_at=_fmt(sub.trial_started_at),
        converted_at=_fmt(sub.converted_at),
        churned_at=_fmt(sub.churned_at),
        reactivated_at=_fmt(sub.reactivated_at),
        last_event_at=_fmt(sub.last_event_at),
        risk_computed_at=_fmt(sub.risk_computed_at),
    )


# ── Routes ────────────────────────────────────────────────────────────────────


@router.get("/subscribers", response_model=list[SubscriberSummary])
def list_subscribers(
    project_id: str | None = Query(default=None, description="Filter by project"),
    state: str | None = Query(default=None, description="Filter by state (e.g. active, churned)"),
    risk_min: float | None = Query(default=None, ge=0, le=100, description="Minimum risk score"),
    limit: int = Query(default=100, ge=1, le=1000),
    offset: int = Query(default=0, ge=0),
    session: Session = Depends(get_session),
) -> list[SubscriberSummary]:
    """List subscribers with optional filters."""
    q = session.query(Subscriber)
    if project_id:
        q = q.filter(Subscriber.project_id == project_id)
    if state:
        try:
            state_enum = SubscriberState(state)
        except ValueError:
            raise HTTPException(status_code=400, detail=f"Unknown state: {state!r}")
        q = q.filter(Subscriber.state == state_enum)
    if risk_min is not None:
        q = q.filter(Subscriber.risk_score >= risk_min)

    subs = q.order_by(Subscriber.risk_score.desc().nulls_last()).offset(offset).limit(limit).all()
    return [
        SubscriberSummary(
            customer_id=s.customer_id,
            project_id=s.project_id,
            state=s.state.value,
            renewal_count=s.renewal_count or 0,
            billing_failure_count=s.billing_failure_count or 0,
            risk_score=s.risk_score,
            risk_band=_band_from_score(s.risk_score),
        )
        for s in subs
    ]


@router.get("/subscribers/{customer_id}", response_model=SubscriberDetail)
def get_subscriber(
    customer_id: str,
    session: Session = Depends(get_session),
) -> SubscriberDetail:
    """Get full subscriber detail including latest risk score."""
    sub = session.query(Subscriber).filter(Subscriber.customer_id == customer_id).first()
    if not sub:
        raise HTTPException(status_code=404, detail=f"Subscriber {customer_id!r} not found")
    return _detail_from_sub(sub)


@router.get("/subscribers/{customer_id}/recommend", response_model=RecommendationResponse)
def get_recommendations(
    customer_id: str,
    session: Session = Depends(get_session),
) -> RecommendationResponse:
    """Get retention recommendations for a subscriber.

    Recomputes risk score on the fly and returns prioritised recommendations.
    """
    sub = session.query(Subscriber).filter(Subscriber.customer_id == customer_id).first()
    if not sub:
        raise HTTPException(status_code=404, detail=f"Subscriber {customer_id!r} not found")

    score = _scorer.compute_and_persist(session, sub)
    result = _recommender.recommend(sub, score)

    return RecommendationResponse(
        customer_id=result.customer_id,
        state=result.state,
        risk_score=result.risk_score,
        risk_band=result.risk_band,
        top_action=result.top.action.value if result.top else None,
        recommendations=[
            RecommendationOut(
                action=r.action.value,
                urgency=r.urgency.value,
                reason=r.reason,
                priority=r.priority,
                metadata=r.metadata,
            )
            for r in result.recommendations
        ],
    )


@router.get("/at-risk", response_model=list[SubscriberSummary])
def at_risk_subscribers(
    threshold: float = Query(default=60.0, ge=0, le=100, description="Minimum risk score"),
    project_id: str | None = Query(default=None),
    limit: int = Query(default=100, ge=1, le=500),
    session: Session = Depends(get_session),
) -> list[SubscriberSummary]:
    """List subscribers with risk score >= threshold, ordered by highest risk first."""
    q = session.query(Subscriber).filter(Subscriber.risk_score >= threshold)
    if project_id:
        q = q.filter(Subscriber.project_id == project_id)

    subs = q.order_by(Subscriber.risk_score.desc()).limit(limit).all()
    return [
        SubscriberSummary(
            customer_id=s.customer_id,
            project_id=s.project_id,
            state=s.state.value,
            renewal_count=s.renewal_count or 0,
            billing_failure_count=s.billing_failure_count or 0,
            risk_score=s.risk_score,
            risk_band=_band_from_score(s.risk_score),
        )
        for s in subs
    ]


@router.post("/score", response_model=ScoreRunResult)
def run_score(
    project_id: str | None = Query(default=None, description="Restrict scoring to one project"),
    session: Session = Depends(get_session),
) -> ScoreRunResult:
    """Trigger a full re-score pass for all (or one project's) subscribers.

    Returns counts of subscribers scored and how many landed in high/critical bands.
    """
    results = _scorer.score_all(session, project_id=project_id)
    session.commit()

    high = sum(1 for _, r in results if r.score >= 60 and r.score < 90)
    critical = sum(1 for _, r in results if r.score >= 90)

    return ScoreRunResult(
        subscribers_scored=len(results),
        high_risk=high,
        critical=critical,
    )
