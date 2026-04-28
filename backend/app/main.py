"""FastAPI application entry point."""

import asyncio
import logging
import sys
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from slowapi.errors import RateLimitExceeded

from app.config import parse_allowed_origins
from app.config import settings as app_settings
from app.db import engine
from app.limiter import limiter
from app.models import Base
from app.routes import health, jobs, measures, results, settings, validation
from app.services.bundle_loader import load_connectathon_bundles
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
        _EXTRA_KEYS = (
            "job_id",
            "batch_id",
            "patient_id",
            "url",
            "count",
            "error",
            "cdr_url",
            "status_code",
            "latency_ms",
            "hint",
        )
        for key in _EXTRA_KEYS:
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
        # Check if tables already exist (skip migrations on a fresh DB — create_all handles those)
        result = await conn.execute(text("SELECT to_regtype('authtype')"))
        authtype_exists = result.scalar() is not None
        if not authtype_exists:
            return

        # AuthType enum: add 'smart' value if not present
        await conn.execute(text("ALTER TYPE authtype ADD VALUE IF NOT EXISTS 'smart'"))
        result = await conn.execute(text("SELECT to_regtype('validationstatus')"))
        validationstatus_exists = result.scalar() is not None
        if validationstatus_exists:
            await conn.execute(text("ALTER TYPE validationstatus ADD VALUE IF NOT EXISTS 'cancelled'"))

        # Add new columns to cdr_configs (idempotent)
        for stmt in [
            "ALTER TABLE cdr_configs ADD COLUMN IF NOT EXISTS name VARCHAR(512)",
            "ALTER TABLE cdr_configs ADD COLUMN IF NOT EXISTS is_default BOOLEAN NOT NULL DEFAULT FALSE",
            "ALTER TABLE cdr_configs ADD COLUMN IF NOT EXISTS is_read_only BOOLEAN NOT NULL DEFAULT FALSE",
        ]:
            await conn.execute(text(stmt))

        # Normalize legacy CDR rows before adding unique indexes. Production may
        # already contain duplicate names or multiple active/default rows created
        # before these constraints existed.
        await conn.execute(
            text("""
            WITH ranked AS (
                SELECT
                    id,
                    ROW_NUMBER() OVER (
                        ORDER BY is_active DESC, is_default DESC, id ASC
                    ) AS rn
                FROM cdr_configs
                WHERE cdr_url = 'http://hapi-fhir-cdr:8080/fhir'
                  AND (name IS NULL OR btrim(name) = '')
            )
            UPDATE cdr_configs AS c
            SET
                name = CASE
                    WHEN ranked.rn = 1 THEN 'Local CDR'
                    ELSE 'Local CDR (migrated ' || c.id::text || ')'
                END,
                is_default = CASE
                    WHEN ranked.rn = 1 THEN TRUE
                    ELSE FALSE
                END
            FROM ranked
            WHERE c.id = ranked.id
            """)
        )
        await conn.execute(
            text("""
            UPDATE cdr_configs
            SET name = 'CDR Connection ' || id::text
            WHERE name IS NULL OR btrim(name) = ''
            """)
        )
        await conn.execute(
            text("""
            WITH ranked AS (
                SELECT
                    id,
                    name,
                    ROW_NUMBER() OVER (
                        PARTITION BY name
                        ORDER BY is_default DESC, is_active DESC, id ASC
                    ) AS rn
                FROM cdr_configs
                WHERE name IS NOT NULL
            )
            UPDATE cdr_configs AS c
            SET name = left(c.name, 480) || ' (' || c.id::text || ')'
            FROM ranked
            WHERE c.id = ranked.id
              AND ranked.rn > 1
            """)
        )

        # Unique constraint on name (required for ON CONFLICT (name) seed upsert)
        await conn.execute(text("CREATE UNIQUE INDEX IF NOT EXISTS uq_cdr_configs_name ON cdr_configs (name)"))

        # Add new columns to jobs
        for stmt in [
            "ALTER TABLE jobs ADD COLUMN IF NOT EXISTS cdr_name VARCHAR(512)",
            "ALTER TABLE jobs ADD COLUMN IF NOT EXISTS cdr_read_only BOOLEAN NOT NULL DEFAULT FALSE",
            "ALTER TABLE jobs ADD COLUMN IF NOT EXISTS cdr_auth_type VARCHAR(32)",
            "ALTER TABLE jobs ADD COLUMN IF NOT EXISTS cdr_auth_credentials JSONB",
            "ALTER TABLE jobs ADD COLUMN IF NOT EXISTS delete_requested BOOLEAN NOT NULL DEFAULT FALSE",
            "ALTER TABLE validation_runs ADD COLUMN IF NOT EXISTS delete_requested BOOLEAN NOT NULL DEFAULT FALSE",
        ]:
            await conn.execute(text(stmt))

        # Add warning_message to bundle_uploads if the table exists
        await conn.execute(
            text("""
            DO $$
            BEGIN
                IF EXISTS (SELECT FROM information_schema.tables WHERE table_name = 'bundle_uploads') THEN
                    ALTER TABLE bundle_uploads ADD COLUMN IF NOT EXISTS warning_message TEXT;
                END IF;
            END
            $$;
            """)
        )

        # Keep only one active row before enforcing the partial unique index.
        await conn.execute(
            text("""
            WITH ranked AS (
                SELECT
                    id,
                    ROW_NUMBER() OVER (
                        ORDER BY is_default DESC, id ASC
                    ) AS rn
                FROM cdr_configs
                WHERE is_active = TRUE
            )
            UPDATE cdr_configs AS c
            SET is_active = FALSE
            FROM ranked
            WHERE c.id = ranked.id
              AND ranked.rn > 1
            """)
        )

        # Enforce at most one active CDR row (partial unique index — Postgres only)
        await conn.execute(
            text(
                "CREATE UNIQUE INDEX IF NOT EXISTS idx_one_active_cdr ON cdr_configs (is_active) WHERE is_active = TRUE"
            )
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

        # Dedup measure_results before adding the unique index — the batch-retry
        # loop can create duplicate rows. Keep the newest row per (job_id, patient_id).
        # Guard so the DELETE only runs once (before the index is created).
        index_exists = (
            await conn.execute(
                text("SELECT EXISTS (SELECT 1 FROM pg_indexes WHERE indexname = 'uq_measure_results_job_patient')")
            )
        ).scalar()
        if not index_exists:
            await conn.execute(
                text("""
                DELETE FROM measure_results a USING measure_results b
                WHERE a.id < b.id
                  AND a.job_id = b.job_id
                  AND a.patient_id = b.patient_id
                """)
            )
        await conn.execute(
            text(
                "CREATE UNIQUE INDEX IF NOT EXISTS uq_measure_results_job_patient"
                " ON measure_results (job_id, patient_id)"
            )
        )

        # Error context columns added for #74/#75/#76 observability
        for stmt in [
            "ALTER TABLE measure_results ADD COLUMN IF NOT EXISTS error_details JSONB",
            "ALTER TABLE measure_results ADD COLUMN IF NOT EXISTS error_phase VARCHAR(32)",
            "ALTER TABLE bundle_uploads ADD COLUMN IF NOT EXISTS error_details JSONB",
            "ALTER TABLE validation_results ADD COLUMN IF NOT EXISTS error_details JSONB",
        ]:
            await conn.execute(text(stmt))


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Startup: create DB tables and launch background worker.
    Shutdown: signal worker to stop.
    """
    from sqlalchemy import text

    # Run schema migrations outside a transaction (required for ALTER TYPE ADD VALUE in Postgres)
    async with engine.connect() as conn:
        await conn.execution_options(isolation_level="AUTOCOMMIT")
        await _run_schema_migrations(conn)
    # Create any missing tables
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    # Enforce at most one active CDR row on fresh DBs (partial unique index — Postgres only)
    async with engine.connect() as conn:
        await conn.execution_options(isolation_level="AUTOCOMMIT")
        if conn.dialect.name == "postgresql":
            await conn.execute(
                text(
                    "CREATE UNIQUE INDEX IF NOT EXISTS idx_one_active_cdr"
                    " ON cdr_configs (is_active) WHERE is_active = TRUE"
                )
            )
    # Seed built-in Local CDR row after tables exist (idempotent, separate connection)
    async with engine.connect() as conn:
        await conn.execution_options(isolation_level="AUTOCOMMIT")
        if conn.dialect.name == "postgresql":
            await conn.execute(
                text("""
                INSERT INTO cdr_configs (cdr_url, auth_type, is_active, name, is_default, is_read_only)
                VALUES ('http://hapi-fhir-cdr:8080/fhir', 'none', TRUE, 'Local CDR', TRUE, FALSE)
                ON CONFLICT (name) DO NOTHING
            """)
            )
    logger.info("Database tables created")

    # Load connectathon bundles at startup (no-op if directory missing)
    try:
        summary = await load_connectathon_bundles()
        logger.info("Startup bundle load complete", extra=summary)
    except Exception:
        logger.exception("Startup bundle load failed — continuing startup")

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


def _rate_limit_exceeded_handler(request: Request, exc: RateLimitExceeded) -> JSONResponse:
    return JSONResponse(
        status_code=429,
        content={
            "resourceType": "OperationOutcome",
            "issue": [
                {
                    "severity": "error",
                    "code": "throttled",
                    "diagnostics": f"Rate limit exceeded: {exc.detail}",
                }
            ],
        },
    )


app.state.limiter = limiter
app.add_exception_handler(RateLimitExceeded, _rate_limit_exceeded_handler)

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
