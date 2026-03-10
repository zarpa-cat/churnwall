"""Churnwall CLI — query subscribers, scores, and recommendations from the terminal.

Usage examples:
  churnwall subscribers --risk-min 70
  churnwall subscribers --state billing_issue --limit 20
  churnwall recommend --customer-id usr_abc123
  churnwall cohort billing-failures --hours 48
  churnwall score
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Optional

import typer
from sqlalchemy.orm import Session

from churnwall.db import SessionLocal, init_db
from churnwall.models import RCEventType, Subscriber, SubscriberEvent, SubscriberState
from churnwall.recommender import RetentionRecommender
from churnwall.scorer import ChurnRiskScorer

app = typer.Typer(
    name="churnwall",
    help="Programmatic subscriber retention for RevenueCat-based apps.",
    no_args_is_help=True,
)

cohort_app = typer.Typer(help="Cohort queries (billing failures, churned, etc.)")
app.add_typer(cohort_app, name="cohort")

_scorer = ChurnRiskScorer()
_recommender = RetentionRecommender()


# ── Helpers ───────────────────────────────────────────────────────────────────


def _get_session() -> Session:
    init_db()
    return SessionLocal()


def _band_color(band: str | None) -> str:
    colors = {"critical": "red", "high": "yellow", "medium": "cyan", "low": "green"}
    return colors.get(band or "", "white")


def _risk_display(score: float | None, band: str | None) -> str:
    if score is None:
        return "—"
    color = _band_color(band)
    return typer.style(f"{score:.0f} ({band})", fg=color, bold=(band in ("critical", "high")))


def _fmt_dt(dt: datetime | None) -> str:
    if dt is None:
        return "—"
    return dt.strftime("%Y-%m-%d %H:%M")


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


# ── subscribers ───────────────────────────────────────────────────────────────


@app.command("subscribers")
def list_subscribers(
    state: Optional[str] = typer.Option(None, help="Filter by state (active, churned, …)"),
    risk_min: Optional[float] = typer.Option(None, "--risk-min", help="Minimum risk score"),
    project_id: Optional[str] = typer.Option(None, "--project", help="Filter by project ID"),
    limit: int = typer.Option(50, help="Max rows to return"),
) -> None:
    """List subscribers with optional filters, ordered by risk score (highest first)."""
    session = _get_session()
    try:
        q = session.query(Subscriber)
        if state:
            try:
                state_enum = SubscriberState(state)
            except ValueError:
                typer.echo(
                    typer.style(
                        f"Unknown state {state!r}. "
                        f"Valid: {', '.join(s.value for s in SubscriberState)}",
                        fg="red",
                    )
                )
                raise typer.Exit(1)
            q = q.filter(Subscriber.state == state_enum)
        if project_id:
            q = q.filter(Subscriber.project_id == project_id)
        if risk_min is not None:
            q = q.filter(Subscriber.risk_score >= risk_min)

        subs = q.order_by(Subscriber.risk_score.desc().nulls_last()).limit(limit).all()

        if not subs:
            typer.echo("No subscribers matched your filters.")
            return

        # Header
        typer.echo(
            f"{'CUSTOMER_ID':<30} {'STATE':<16} {'RISK':>14}  {'RENEWALS':>8}  "
            f"{'FAILURES':>8}  {'LAST_EVENT':<16}"
        )
        typer.echo("─" * 100)

        for s in subs:
            band = _band_from_score(s.risk_score)
            risk_str = _risk_display(s.risk_score, band)
            state_color = {
                "billing_issue": "yellow",
                "churned": "red",
                "active": "green",
                "trialing": "cyan",
                "reactivated": "magenta",
            }.get(s.state.value, "white")
            state_str = typer.style(f"{s.state.value:<16}", fg=state_color)
            typer.echo(
                f"{s.customer_id:<30} {state_str} {risk_str:>14}  "
                f"{s.renewal_count or 0:>8}  {s.billing_failure_count or 0:>8}  "
                f"{_fmt_dt(s.last_event_at):<16}"
            )

        typer.echo(f"\n{len(subs)} subscriber(s) shown.")
    finally:
        session.close()


# ── recommend ─────────────────────────────────────────────────────────────────


@app.command("recommend")
def recommend(
    customer_id: str = typer.Option(..., "--customer-id", help="Subscriber customer ID"),
    top_n: int = typer.Option(3, "--top", help="Number of recommendations to show"),
) -> None:
    """Show retention recommendations for a subscriber."""
    session = _get_session()
    try:
        sub = session.query(Subscriber).filter(Subscriber.customer_id == customer_id).first()
        if not sub:
            typer.echo(typer.style(f"Subscriber {customer_id!r} not found.", fg="red"))
            raise typer.Exit(1)

        score_result = _scorer.compute_and_persist(session, sub)
        result = _recommender.recommend(sub, score_result)
        session.commit()

        band_color = _band_color(result.risk_band)
        typer.echo(f"\n{'─' * 60}")
        typer.echo(f"  Subscriber : {sub.customer_id}")
        typer.echo(f"  State      : {sub.state.value}")
        typer.echo(
            "  Risk       : "
            + typer.style(
                f"{result.risk_score:.0f} / 100  [{result.risk_band}]",
                fg=band_color,
                bold=True,
            )
        )
        typer.echo(f"  Product    : {sub.product_id or '—'}")
        typer.echo(f"  Store      : {sub.store or '—'}")
        typer.echo(f"  Renewals   : {sub.renewal_count or 0}")
        typer.echo(f"  Failures   : {sub.billing_failure_count or 0}")
        typer.echo(f"{'─' * 60}")

        if not result.recommendations:
            typer.echo("  No recommendations. Subscriber looks healthy.")
        else:
            typer.echo(f"  Top {min(top_n, len(result.recommendations))} recommendation(s):\n")
            for i, rec in enumerate(result.recommendations[:top_n], 1):
                urgency_color = {
                    "immediate": "red",
                    "soon": "yellow",
                    "monitor": "cyan",
                }.get(rec.urgency.value, "white")
                urgency_label = typer.style(f"[{rec.urgency.value}]", fg=urgency_color, bold=True)
                typer.echo(f"  {i}. {rec.action.value}  {urgency_label}")
                typer.echo(f"     {rec.reason}")
                if rec.metadata:
                    for k, v in rec.metadata.items():
                        typer.echo(f"     • {k}: {v}")
                typer.echo()

        typer.echo(f"{'─' * 60}\n")
    finally:
        session.close()


# ── score ─────────────────────────────────────────────────────────────────────


@app.command("score")
def run_score(
    project_id: Optional[str] = typer.Option(None, "--project", help="Restrict to one project"),
) -> None:
    """Trigger a full re-score pass for all subscribers."""
    session = _get_session()
    try:
        results = _scorer.score_all(session, project_id=project_id)
        session.commit()

        high = sum(1 for _, r in results if 60 <= r.score < 90)
        critical = sum(1 for _, r in results if r.score >= 90)
        medium = sum(1 for _, r in results if 30 <= r.score < 60)
        low = sum(1 for _, r in results if r.score < 30)

        typer.echo(f"\nScored {len(results)} subscriber(s):")
        typer.echo(f"  {typer.style('critical', fg='red', bold=True):<20}  {critical}")
        typer.echo(f"  {typer.style('high', fg='yellow', bold=True):<20}  {high}")
        typer.echo(f"  {'medium':<12}  {medium}")
        typer.echo(f"  {'low':<12}  {low}\n")
    finally:
        session.close()


# ── cohort billing-failures ───────────────────────────────────────────────────


@cohort_app.command("billing-failures")
def billing_failures(
    hours: int = typer.Option(48, help="Look-back window in hours"),
    project_id: Optional[str] = typer.Option(None, "--project", help="Filter by project ID"),
    limit: int = typer.Option(50, help="Max rows to return"),
) -> None:
    """List subscribers who hit a billing failure in the last N hours."""
    session = _get_session()
    try:
        cutoff = datetime.now(UTC).replace(tzinfo=None) - timedelta(hours=hours)
        q = (
            session.query(Subscriber)
            .join(SubscriberEvent, Subscriber.id == SubscriberEvent.subscriber_id)
            .filter(
                SubscriberEvent.event_type == RCEventType.BILLING_ISSUE,
                SubscriberEvent.occurred_at >= cutoff,
            )
        )
        if project_id:
            q = q.filter(Subscriber.project_id == project_id)

        subs = q.distinct().order_by(Subscriber.last_billing_failure_at.desc()).limit(limit).all()

        if not subs:
            typer.echo(f"No billing failures in the last {hours}h.")
            return

        typer.echo(f"\nBilling failures in the last {hours}h — {len(subs)} subscriber(s):\n")
        typer.echo(
            f"{'CUSTOMER_ID':<30} {'STATE':<16} {'FAILURES':>8}  {'RISK':>14}  {'LAST_FAILURE':<16}"
        )
        typer.echo("─" * 95)

        for s in subs:
            band = _band_from_score(s.risk_score)
            risk_str = _risk_display(s.risk_score, band)
            typer.echo(
                f"{s.customer_id:<30} {s.state.value:<16} {s.billing_failure_count or 0:>8}  "
                f"{risk_str:>14}  {_fmt_dt(s.last_billing_failure_at):<16}"
            )

        typer.echo()
    finally:
        session.close()
