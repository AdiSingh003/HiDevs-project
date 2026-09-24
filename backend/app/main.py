"""FastAPI application: REST + SSE API for the negotiation platform, and the built arena UI.

Run: ``uvicorn backend.app.main:app --reload`` from the repository root.
"""

from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from pathlib import Path
from typing import AsyncIterator

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from agents import __version__
from agents.lyzr.settings import LyzrSettings
from agents.platform import NegotiationPlatform

from .bus import EventBus
from .routers import audit, contracts, meta, runs, telemetry
from .services import RunService
from .settings import AppSettings, get_settings
from .store import Store


def create_app(settings: AppSettings | None = None, lyzr_settings: LyzrSettings | None = None) -> FastAPI:
    settings = settings or get_settings()
    logging.basicConfig(level=settings.log_level.upper(), format="%(asctime)s %(levelname)s %(name)s: %(message)s")

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        settings.data_dir.mkdir(parents=True, exist_ok=True)
        platform = NegotiationPlatform(settings.data_dir, lyzr_settings)
        app.state.service = RunService(platform, Store(settings.data_dir), EventBus(), settings)
        yield
        await app.state.service.shutdown()

    app = FastAPI(
        title="Autonomous B2B Supply Chain & SLA Contract Negotiator",
        version=__version__,
        description="Bounded buyer/supplier agents (Lyzr Agent API + Lyzr Automata) negotiate under a Legal Arbiter "
                    "(Lyzr Safe AI) and emit signed contracts with a hash-chained audit log (Lyzr AIMS).",
        lifespan=lifespan,
    )
    app.add_middleware(CORSMiddleware, allow_origins=settings.cors_list, allow_credentials=False,
                       allow_methods=["*"], allow_headers=["*"])
    for module in (meta, runs, contracts, audit, telemetry):
        app.include_router(module.router)

    dist = settings.frontend_dist.resolve()
    if (dist / "index.html").exists():
        if (dist / "assets").is_dir():
            app.mount("/assets", StaticFiles(directory=dist / "assets"), name="assets")

        @app.get("/{full_path:path}", include_in_schema=False)
        async def spa(full_path: str) -> FileResponse:
            if full_path.startswith("api/"):
                raise HTTPException(404, "not found")
            candidate = (dist / full_path).resolve()
            if full_path and candidate.is_file() and candidate.is_relative_to(dist):
                return FileResponse(candidate)
            return FileResponse(dist / "index.html")

    return app


app = create_app()
