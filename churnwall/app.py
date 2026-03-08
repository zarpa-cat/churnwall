"""FastAPI application factory."""

from __future__ import annotations

from contextlib import asynccontextmanager

from fastapi import FastAPI

from churnwall.db import init_db
from churnwall.webhook import router as webhook_router


@asynccontextmanager
async def lifespan(app: FastAPI):  # type: ignore[type-arg]
    init_db()
    yield


def create_app() -> FastAPI:
    app = FastAPI(
        title="churnwall",
        description="Programmatic subscriber retention for RevenueCat-based apps",
        version="0.1.0",
        lifespan=lifespan,
    )
    app.include_router(webhook_router)
    return app


app = create_app()
