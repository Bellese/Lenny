"""FastAPI application entry point."""

import asyncio
import logging
import sys
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.config import parse_allowed_origins
from app.config import settings as app_settings
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


async def _run_schema_migrations(conn) -> None:
    """Add new columns/enum values to existing tables. Idempotent (IF NOT EXISTS).

    Must run outside a transaction for ALTER TYPE ADD VALUE (Postgres < 12
    requires autocommit for that statement). SQLite (used in tests) skips all
    ALTER statements — create_all handles new columns on fresh in-memory DBs.
    """
    from sqlalchemy import text

    if conn.dialect.name == "postgresql":
        # AuthType enum: add 'smart' value if not present
        await conn.execute(text("ALTER TYPE authtype ADD VALUE IF NOT EXISTS 'smart'"))

        # Add new columns to cdr_configs (idempotent)
        for stmt in [
            "ALTER TABLE cdr_configs ADD COLUMN IF NOT EXISTS name VARCHAR(512)",
            "ALTER TABLE cdr_configs ADD COLUMN IF NOT EXISTS is_default BOOLEAN NOT NULL DEFAULT FALSE",
            "ALTER TABLE cdr_configs ADD COLUMN IF NOT EXISTS is_read_only BOOLEAN NOT NULL DEFAULT FALSE",
        ]:
            await conn.execute(text(stmt))

        # Add new columns to jobs
        for stmt in [
            "ALTER TABLE jobs ADD COLUMN IF NOT EXISTS cdr_name VARCHAR(512)",
            "ALTER TABLE jobs ADD COLUMN IF NOT EXISTS cdr_read_only BOOLEAN NOT NULL DEFAULT FALSE",
            "ALTER TABLE jobs ADD COLUMN IF NOT EXISTS cdr_auth_type VARCHAR(32)",
            "ALTER TABLE jobs ADD COLUMN IF NOT EXISTS cdr_auth_credentials JSONB",
        ]:
            await conn.execute(text(stmt))

        # Add warning_message to bundle_uploads
        await conn.execute(text("ALTER TABLE bundle_uploads ADD COLUMN IF NOT EXISTS warning_message TEXT"))

        # Seed the Local CDR row (idempotent via ON CONFLICT DO NOTHING)
        await conn.execute(
            text("""
            INSERT INTO cdr_configs (cdr_url, auth_type, is_active, name, is_default, is_read_only)
            VALUES ('http://hapi-fhir-cdr:8080/fhir', 'none', TRUE, 'Local CDR', TRUE, FALSE)
            ON CONFLICT (name) DO NOTHING
        """)
        )
        # Update existing row that matches the default URL but has no name yet
        await conn.execute(
            text("""
            UPDATE cdr_configs
            SET name = 'Local CDR', is_default = TRUE, is_read_only = FALSE
            WHERE cdr_url = 'http://hapi-fhir-cdr:8080/fhir'
              AND (name IS NULL OR name = '')
        """)
        )


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Startup: create DB tables and launch background worker.
    Shutdown: signal worker to stop.
    """
    # Run schema migrations outside a transaction (required for ALTER TYPE ADD VALUE in Postgres)
    async with engine.connect() as conn:
        await conn.execution_options(isolation_level="AUTOCOMMIT")
        await _run_schema_migrations(conn)
    # Create any missing tables
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
allow_credentials = bool(origins) and origins != ["*"]
if not allow_credentials:
    logger.warning(
        "CORS: ALLOWED_ORIGINS is wildcard — allow_credentials disabled. "
        "Set ALLOWED_ORIGINS to specific origins in production."
    )
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
