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

## REST API

Once running (`uvicorn churnwall.app:app`), the API is available at `http://localhost:8000/api`:

| Method | Endpoint | Description |
|--------|----------|-------------|
| GET | `/api/subscribers` | List all subscribers (filter by state, risk_min, project) |
| GET | `/api/subscribers/{customer_id}` | Full subscriber detail + risk score |
| GET | `/api/subscribers/{customer_id}/recommend` | Retention recommendations |
| GET | `/api/at-risk` | Subscribers above a risk threshold (default: 60) |
| POST | `/api/score` | Trigger a full re-score pass |

Interactive docs at `/docs`.

## Integrations (Phase 3)

Churnwall ships with pluggable integrations for email and Slack alerts. Configure via environment variables:

```bash
# Resend (email)
RESEND_API_KEY=re_your_key
RESEND_FROM_EMAIL=churnwall@yourapp.com

# Slack (alerts for immediate-urgency events)
SLACK_WEBHOOK_URL=https://hooks.slack.com/services/...
```

**Routing logic:**
- `immediate` urgency → email + Slack alert (billing failures, critical churn risk)
- `soon` urgency → email only (win-back offers, trial nudges, loyalty discounts)
- `monitor` urgency → no send (healthy subscribers, passive watch)

Both integrations degrade gracefully — if keys aren't set, sends are skipped with a log warning. Zero-config churnwall still works; you just won't get notifications.

```python
from churnwall.integrations.dispatcher import IntegrationDispatcher
from churnwall.settings import settings

dispatcher = IntegrationDispatcher.from_settings(settings, app_name="MyApp")
await dispatcher.dispatch(subscriber, recommendation, risk_score=87.5)
```

## RC API Sync (Phase 5)

Churnwall can seed its DB directly from the RevenueCat API — no webhook history replay needed.

```bash
# Sync a single subscriber
churnwall sync --customer-id usr_abc123

# Bulk sync from a file (one app_user_id per line)
churnwall sync --from-file subscriber_ids.txt --project proj_xyz
```

Configure with:
```bash
RC_API_KEY=sk_your_secret_key
RC_PROJECT_ID=proj_your_project_id   # optional default project
```

Sync derives state from live RC subscription data (active/trialing/billing_issue/churned/reactivated), computes a risk score, and upserts the record — preserving any richer history already tracked from webhooks.

**Python API:**
```python
from churnwall.rc_client import RCClient
from churnwall.sync import sync_subscriber

async with RCClient(api_key="sk_...") as client:
    subscriber, created = await sync_subscriber(client, "usr_abc", "proj_xyz", session)
```

## Status

- ✅ Phase 1: State machine + webhook receiver (28 tests)
- ✅ Phase 2a: Churn risk scorer (24 tests)
- ✅ Phase 2b: Recommendation engine (25 tests)
- ✅ Phase 2c: REST API (26 tests)
- ✅ Phase 3: Integrations — Resend + Slack (35 tests, 138 total)
- ✅ Phase 4: CLI — subscribers, recommend, score, cohort (153 tests)
- ✅ Phase 5: RC API Sync — backfill + seed from live RC data (180 tests)

See [GitHub Issues](https://github.com/zarpa-cat/churnwall/issues) for roadmap.

---

Built by [Zarpa](https://zarpa-cat.github.io) · [Purr in Prod](https://zarpa-cat.github.io)
