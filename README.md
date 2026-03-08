# churnwall

**Programmatic subscriber retention for RevenueCat-based apps.**

Churnwall sits in your RC webhook stream, maintains a real-time subscriber state machine, scores churn risk, and generates actionable retention recommendations — via API, CLI, or agent.

Not a dashboard you stare at. A decision engine you query.

---

## What it does

1. **Receives RC webhook events** — `INITIAL_PURCHASE`, `RENEWAL`, `CANCELLATION`, `BILLING_ISSUE`, `EXPIRATION`, `PRODUCT_CHANGE`, and more
2. **Maintains subscriber state** — accurate state machine across trial → active → billing_issue → churned → reactivated
3. **Scores churn risk** — per-subscriber risk score (0–100) based on plan type, billing history, conversion speed, recency
4. **Generates recommendations** — what to do, when to do it, and why

```bash
# Query at-risk subscribers
churnwall subscribers --risk-min 70

# Get recommendations for a subscriber
churnwall recommend --customer-id "usr_abc123"

# Check recent billing failures
churnwall cohort billing-failures --hours 48
```

---

## Architecture

```
RC Webhooks → /webhook endpoint → Event log → State machine → Risk scorer → Recommendations
                                                    ↑
                               RC API pull (backfill / sync)
```

**Stack:** FastAPI · SQLAlchemy · SQLite (dev) / Postgres (prod) · Typer · httpx · pytest

---

## Status

🚧 Active development — see [GitHub Issues](https://github.com/zarpa-cat/churnwall/issues) for roadmap

---

Built by [Zarpa](https://zarpa-cat.github.io) · [Purr in Prod](https://zarpa-cat.github.io)
