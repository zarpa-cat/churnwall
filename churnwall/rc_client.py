"""RevenueCat API client for churnwall sync.

Wraps the RC REST API to fetch subscriber data for seeding / backfilling the
local DB without needing to replay the full webhook history.

API reference: https://www.revenuecat.com/docs/api-v1

Auth: Bearer token (secret API key, not the public/iOS key).
Set RC_API_KEY in your environment.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from typing import Any

import httpx

RC_BASE_URL = "https://api.revenuecat.com"


class RCApiError(Exception):
    """Raised for non-2xx responses from the RC API."""

    def __init__(self, status_code: int, message: str) -> None:
        super().__init__(f"RC API error {status_code}: {message}")
        self.status_code = status_code


class RCClient:
    """Async RevenueCat API client.

    Usage::

        async with RCClient(api_key="sk_...") as client:
            sub = await client.get_subscriber("user_123")
    """

    def __init__(self, api_key: str, timeout: float = 10.0) -> None:
        self._api_key = api_key
        self._timeout = timeout
        self._client: httpx.AsyncClient | None = None

    async def __aenter__(self) -> "RCClient":
        self._client = httpx.AsyncClient(
            base_url=RC_BASE_URL,
            headers={
                "Authorization": f"Bearer {self._api_key}",
                "Content-Type": "application/json",
                "X-Platform": "stripe",  # required for some endpoints
            },
            timeout=self._timeout,
        )
        return self

    async def __aexit__(self, *_: Any) -> None:
        if self._client:
            await self._client.aclose()

    @property
    def _http(self) -> httpx.AsyncClient:
        if self._client is None:
            raise RuntimeError("RCClient must be used as an async context manager")
        return self._client

    async def get_subscriber(self, app_user_id: str) -> dict[str, Any]:
        """Fetch a single subscriber by app_user_id.

        Returns the ``subscriber`` object from the RC v1 response::

            {
                "original_app_user_id": "...",
                "subscriptions": {...},
                "non_subscriptions": {...},
                "entitlements": {...},
                "first_seen": "...",
                "last_seen": "...",
            }

        Raises:
            RCApiError: if the RC API returns a non-2xx response.
        """
        resp = await self._http.get(f"/v1/subscribers/{app_user_id}")
        if resp.status_code == 404:
            raise RCApiError(404, f"Subscriber not found: {app_user_id}")
        if resp.status_code != 200:
            raise RCApiError(resp.status_code, resp.text[:200])
        return resp.json()["subscriber"]

    async def get_subscribers_batch(
        self,
        app_user_ids: list[str],
        concurrency: int = 5,
    ) -> dict[str, dict[str, Any]]:
        """Fetch multiple subscribers concurrently.

        Args:
            app_user_ids: List of app user IDs to fetch.
            concurrency: Max parallel requests (be polite to RC).

        Returns:
            Dict mapping app_user_id → subscriber dict (missing IDs omitted).
        """
        semaphore = asyncio.Semaphore(concurrency)
        results: dict[str, dict[str, Any]] = {}

        async def fetch_one(uid: str) -> None:
            async with semaphore:
                try:
                    results[uid] = await self.get_subscriber(uid)
                except RCApiError as exc:
                    if exc.status_code != 404:
                        raise

        await asyncio.gather(*[fetch_one(uid) for uid in app_user_ids])
        return results


# ── Subscription state inference ─────────────────────────────────────────────


def _parse_dt(value: str | None) -> datetime | None:
    """Parse an ISO-8601 string from RC to a naive UTC datetime."""
    if not value:
        return None
    dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    return dt.astimezone(UTC).replace(tzinfo=None)


def _now_utc() -> datetime:
    return datetime.now(UTC).replace(tzinfo=None)


class SubscriberSnapshot:
    """Derived view of a subscriber's current state from the RC API response.

    Translates RC subscription data into the churnwall state model so the sync
    engine can upsert Subscriber records without replaying events.
    """

    def __init__(self, app_user_id: str, rc_data: dict[str, Any]) -> None:
        self.app_user_id = app_user_id
        self._rc = rc_data
        self._subs: dict[str, Any] = rc_data.get("subscriptions", {})

    # ── Timestamps ───────────────────────────────────────────────────────────

    @property
    def first_seen(self) -> datetime | None:
        return _parse_dt(self._rc.get("first_seen"))

    @property
    def last_seen(self) -> datetime | None:
        return _parse_dt(self._rc.get("last_seen"))

    # ── Active subscription detection ─────────────────────────────────────────

    def _active_subs(self) -> list[dict[str, Any]]:
        """Return subscriptions that are not expired."""
        now = _now_utc()
        active = []
        for sub in self._subs.values():
            exp = _parse_dt(sub.get("expires_date"))
            if exp is None or exp > now:
                active.append(sub)
        return active

    def _all_subs_sorted(self) -> list[dict[str, Any]]:
        """All subscriptions sorted by purchase_date desc (most recent first)."""
        subs = list(self._subs.values())
        subs.sort(key=lambda s: _parse_dt(s.get("purchase_date")) or datetime.min, reverse=True)
        return subs

    # ── State derivation ──────────────────────────────────────────────────────

    @property
    def state(self) -> str:
        """Derive a churnwall SubscriberState from RC subscription data."""
        active = self._active_subs()
        if not active and not self._subs:
            return "unknown"

        if active:
            # Check for billing issue among active subs
            for sub in active:
                if sub.get("billing_issues_detected_at"):
                    return "billing_issue"

            # Check for trial
            for sub in active:
                if sub.get("period_type") == "TRIAL":
                    return "trialing"

            return "active"

        # All subs are expired — churned or reactivated?
        subs = self._all_subs_sorted()
        if len(subs) >= 2:
            return "reactivated"  # Had multiple purchase cycles
        return "churned"

    @property
    def product_id(self) -> str | None:
        active = self._active_subs()
        if active:
            # Return the most recently purchased active sub
            active.sort(
                key=lambda s: _parse_dt(s.get("purchase_date")) or datetime.min,
                reverse=True,
            )
            for product_id, sub in self._subs.items():
                if sub is active[0]:
                    return product_id
        subs = self._all_subs_sorted()
        if subs:
            for product_id, sub in self._subs.items():
                if sub is subs[0]:
                    return product_id
        return None

    @property
    def store(self) -> str | None:
        subs = self._all_subs_sorted()
        if subs:
            return subs[0].get("store")
        return None

    @property
    def renewal_count(self) -> int:
        total = 0
        for sub in self._subs.values():
            total += sub.get("billing_issues_count", 0)  # approximation
        return total

    @property
    def billing_failure_count(self) -> int:
        total = 0
        for sub in self._subs.values():
            if sub.get("billing_issues_detected_at"):
                total += 1
        return total

    @property
    def last_billing_failure_at(self) -> datetime | None:
        dates = []
        for sub in self._subs.values():
            dt = _parse_dt(sub.get("billing_issues_detected_at"))
            if dt:
                dates.append(dt)
        return max(dates) if dates else None

    @property
    def trial_started_at(self) -> datetime | None:
        for sub in self._subs.values():
            if sub.get("period_type") == "TRIAL":
                return _parse_dt(sub.get("purchase_date"))
        return None

    @property
    def converted_at(self) -> datetime | None:
        # First non-trial purchase date
        for sub in self._all_subs_sorted():
            if sub.get("period_type") != "TRIAL":
                return _parse_dt(sub.get("purchase_date"))
        return None

    @property
    def churned_at(self) -> datetime | None:
        if self.state not in ("churned",):
            return None
        subs = self._all_subs_sorted()
        if subs:
            return _parse_dt(subs[0].get("expires_date"))
        return None

    @property
    def reactivated_at(self) -> datetime | None:
        if self.state != "reactivated":
            return None
        subs = self._all_subs_sorted()
        if subs:
            return _parse_dt(subs[0].get("purchase_date"))
        return None
