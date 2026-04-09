"""FastAPI application entry point."""

import asyncio
import logging
import sys
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.config import parse_allowed_origins, settings as app_settings
from app.db import engine
from app.models import Base
from app.routes import health, jobs, measures, results, settings, validation
from app.services.worker import request_shutdown, worker_loop

# ---------------------------------------------------------------------------
# Structured JSON logging
# ---------------------------------------------------------------------------


class JSONFormatter(logging.Formatter):
    """Minimal structured JSON log formatter."""

    def format(self, record: logging.LogRecord) -> str:
        import json as _json
        from datetime import datetime, timezone

        log_entry: dict = {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        }
        # Merge structlog-style extra keys
        for key in ("job_id", "batch_id", "patient_id", "url", "count", "error", "cdr_url"):
            val = getattr(record, key, None)
            if val is not None:
                log_entry[key] = val
        if record.exc_info and record.exc_info[1]:
            log_entry["exception"] = self.formatException(record.exc_info)
        return _json.dumps(log_entry)


handler = logging.StreamHandler(sys.stdout)
handler.setFormatter(JSONFormatter())
logging.basicConfig(level=logging.INFO, handlers=[handler])
logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Lifespan: create tables + start worker
# ---------------------------------------------------------------------------


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Startup: create DB tables and launch background worker.
    Shutdown: signal worker to stop.
    """
    # Create tables
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    logger.info("Database tables created")

    # Start background worker
    worker_task = asyncio.create_task(worker_loop())
    logger.info("Background worker started")

    yield

    # Shutdown
    request_shutdown()
    worker_task.cancel()
    try:
        await worker_task
    except asyncio.CancelledError:
        pass
    logger.info("Background worker stopped")


# ---------------------------------------------------------------------------
# App
# ---------------------------------------------------------------------------

app = FastAPI(
    title="MCT2 — Measure Calculation Tool v2",
    description="Healthcare quality measure calculation orchestrator",
    version="0.1.0",
    lifespan=lifespan,
)

# CORS — restricted in production via ALLOWED_ORIGINS env var; defaults to wildcard for local dev
origins = parse_allowed_origins(app_settings.ALLOWED_ORIGINS)
# allow_credentials requires an explicit origin list; wildcard + credentials is invalid per spec
allow_credentials = origins != ["*"]
if not allow_credentials:
    logger.warning("CORS: ALLOWED_ORIGINS is wildcard — allow_credentials disabled. "
                   "Set ALLOWED_ORIGINS to specific origins in production.")
app.add_middleware(
    CORSMiddleware,
    allow_origins=origins,
    allow_credentials=allow_credentials,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Register routers
app.include_router(health.router)
app.include_router(jobs.router)
app.include_router(measures.router)
app.include_router(results.router)
app.include_router(settings.router)
app.include_router(validation.router)
