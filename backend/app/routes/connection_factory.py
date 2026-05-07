"""Generic CRUD router factory for connection-management resources.

Each connection kind (CDR today, MCS in PR #3, future TS/MR/MRR) shares a
7-route surface: list, create, get, update, delete, activate, test-connection.
This factory generates that router given a model, schemas, and a few kind-
specific parameters.

CDR is the first consumer (`app/routes/settings.py`); the pre-existing 33
unit tests in `tests/test_routes_settings.py` are the regression net for the
factory refactor.

Future kinds add a single `make_connection_router(...)` call with their own
model + schemas + URL field name.
"""

import logging
from typing import Any

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy import update as sa_update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm.attributes import InstrumentedAttribute

from app.db import get_session
from app.models.connection_base import AuthType, ConnectionKind
from app.models.job import Job, JobStatus
from app.services.fhir_client import _validate_ssrf_url, verify_fhir_connection
from app.services.fhir_errors import (
    HINT_BY_STATUS,
    FhirOperationError,
    build_error_envelope,
    hint_for_network_exception,
    sanitize_url,
)
from app.services.validation import sanitize_error

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Shared validation helpers
# ---------------------------------------------------------------------------


def _validate_auth_type(auth_type: str) -> AuthType:
    try:
        return AuthType(auth_type)
    except ValueError:
        valid_types = ", ".join(t.value for t in AuthType)
        raise HTTPException(
            status_code=400,
            detail={
                "resourceType": "OperationOutcome",
                "issue": [
                    {
                        "severity": "error",
                        "code": "invalid",
                        "diagnostics": f"Invalid auth_type: {auth_type}. Must be one of: {valid_types}",
                    }
                ],
            },
        )


def _validate_smart_credentials(auth_type: str, auth_credentials: dict | None) -> None:
    if auth_type != "smart":
        return
    required = {"client_id", "client_secret", "token_endpoint"}
    creds = auth_credentials or {}
    if not required.issubset(creds.keys()):
        raise HTTPException(
            status_code=400,
            detail={
                "resourceType": "OperationOutcome",
                "issue": [
                    {
                        "severity": "error",
                        "code": "invalid",
                        "diagnostics": (
                            "SMART on FHIR requires client_id, client_secret, and token_endpoint in auth_credentials."
                        ),
                    }
                ],
            },
        )


def _check_url(url: str, label: str) -> None:
    try:
        _validate_ssrf_url(url, label=label)
    except ValueError as exc:
        raise HTTPException(
            status_code=400,
            detail={
                "resourceType": "OperationOutcome",
                "issue": [{"severity": "error", "code": "security", "diagnostics": sanitize_error(exc)}],
            },
        )


# ---------------------------------------------------------------------------
# Factory
# ---------------------------------------------------------------------------


def make_connection_router(
    *,
    model: type[Any],
    response_schema: type[BaseModel],
    create_schema: type[BaseModel],
    test_request_schema: type[BaseModel],
    prefix: str,
    kind: ConnectionKind,
    url_field: str,
    default_name: str,
    job_fk_column: InstrumentedAttribute | None = None,
    audit_logger: logging.Logger | None = None,
) -> APIRouter:
    """Generate a 7-route APIRouter for a connection-management resource.

    Args:
        model: SQLAlchemy model class (e.g., `CDRConfig`).
        response_schema: Pydantic response schema. Its fields list drives the
            response dict keys, so kind-specific fields like `is_read_only`
            are surfaced automatically.
        create_schema: Pydantic schema for POST/PUT bodies.
        test_request_schema: Pydantic schema for the test-connection POST body.
        prefix: URL prefix relative to the parent router (e.g., `"/connections"`).
        kind: `ConnectionKind` value — used in log event names + audit fields.
        url_field: Name of the URL attribute on both the request schema and
            the model column (e.g., `"cdr_url"`).
        default_name: Display name for the seeded default row, used in delete-
            blocked error messages (e.g., `"Local CDR"`).
        job_fk_column: Optional `Job.<kind>_id` column for the delete-with-
            active-jobs check. Pass `None` if the kind has no Job FK yet.
    """

    router = APIRouter()
    # Use the caller's logger so audit-log captures (caplog, structured-log
    # consumers) work against the calling module's name, not the factory's.
    log = audit_logger if audit_logger is not None else logger
    log_event = f"{kind.value}_credentials_changed"
    log_id_field = f"{kind.value}_id"
    log_name_field = f"{kind.value}_name"

    def _cfg_to_response(cfg) -> dict:
        out: dict[str, Any] = {}
        for field_name in response_schema.model_fields:
            value = getattr(cfg, field_name, None)
            if isinstance(value, AuthType):
                value = value.value
            out[field_name] = value
        return out

    # -----------------------------------------------------------------------
    # List
    # -----------------------------------------------------------------------

    @router.get(prefix, response_model=list[response_schema])
    async def list_connections(session: AsyncSession = Depends(get_session)) -> list:
        result = await session.execute(select(model).order_by(model.id))
        configs = result.scalars().all()
        return [_cfg_to_response(c) for c in configs]

    # -----------------------------------------------------------------------
    # Create
    # -----------------------------------------------------------------------

    @router.post(prefix, response_model=response_schema, status_code=201)
    async def create_connection(
        body: create_schema,
        session: AsyncSession = Depends(get_session),
    ) -> dict:
        body_url = getattr(body, url_field)
        _check_url(body_url, label=url_field)
        _validate_auth_type(body.auth_type)
        _validate_smart_credentials(body.auth_type, body.auth_credentials)

        existing = await session.execute(select(model).where(model.name == body.name))
        if existing.scalar_one_or_none() is not None:
            raise HTTPException(
                status_code=409,
                detail={
                    "resourceType": "OperationOutcome",
                    "issue": [
                        {
                            "severity": "error",
                            "code": "conflict",
                            "diagnostics": f"A connection named '{body.name}' already exists.",
                        }
                    ],
                },
            )

        # body.model_dump() preserves all fields the schema declares (kind-specific
        # ones like is_read_only included). Override auth_type to the enum + force
        # is_active/is_default to False so a new row never silently steals active.
        cfg_kwargs = body.model_dump()
        cfg_kwargs["auth_type"] = AuthType(body.auth_type)
        cfg_kwargs["is_active"] = False
        cfg_kwargs["is_default"] = False
        cfg = model(**cfg_kwargs)
        session.add(cfg)
        await session.commit()
        await session.refresh(cfg)
        log.info(
            log_event,
            extra={
                "event": log_event,
                "action": "create",
                log_id_field: cfg.id,
                log_name_field: cfg.name,
            },
        )
        return _cfg_to_response(cfg)

    # -----------------------------------------------------------------------
    # Get
    # -----------------------------------------------------------------------

    @router.get(f"{prefix}/{{connection_id}}", response_model=response_schema)
    async def get_connection(
        connection_id: int,
        session: AsyncSession = Depends(get_session),
    ) -> dict:
        cfg = await session.get(model, connection_id)
        if cfg is None:
            raise HTTPException(
                status_code=404,
                detail={
                    "resourceType": "OperationOutcome",
                    "issue": [{"severity": "error", "code": "not-found", "diagnostics": "Connection not found"}],
                },
            )
        return _cfg_to_response(cfg)

    # -----------------------------------------------------------------------
    # Update
    # -----------------------------------------------------------------------

    @router.put(f"{prefix}/{{connection_id}}", response_model=response_schema)
    async def update_connection(
        connection_id: int,
        body: create_schema,
        session: AsyncSession = Depends(get_session),
    ) -> dict:
        cfg = await session.get(model, connection_id)
        if cfg is None:
            raise HTTPException(
                status_code=404,
                detail={
                    "resourceType": "OperationOutcome",
                    "issue": [{"severity": "error", "code": "not-found", "diagnostics": "Connection not found"}],
                },
            )

        body_url = getattr(body, url_field)
        _check_url(body_url, label=url_field)
        _validate_auth_type(body.auth_type)
        # Null auth_credentials in the body means "preserve existing" — protects
        # against accidental credential wipe on partial updates from the UI.
        effective_credentials = body.auth_credentials if body.auth_credentials is not None else cfg.auth_credentials
        _validate_smart_credentials(body.auth_type, effective_credentials)

        duplicate = await session.execute(select(model).where(model.name == body.name, model.id != connection_id))
        if duplicate.scalar_one_or_none() is not None:
            raise HTTPException(
                status_code=409,
                detail={
                    "resourceType": "OperationOutcome",
                    "issue": [
                        {
                            "severity": "error",
                            "code": "conflict",
                            "diagnostics": f"A connection named '{body.name}' already exists.",
                        }
                    ],
                },
            )

        # Apply the body's fields to the model. Auth type is converted to the
        # enum; auth_credentials respects the preserve-on-null rule above.
        for field_name in body.model_fields_set | set(body.model_dump().keys()):
            if field_name == "auth_type":
                cfg.auth_type = AuthType(body.auth_type)
            elif field_name == "auth_credentials":
                cfg.auth_credentials = effective_credentials
            else:
                setattr(cfg, field_name, getattr(body, field_name))
        await session.commit()
        await session.refresh(cfg)
        log.info(
            log_event,
            extra={
                "event": log_event,
                "action": "update",
                log_id_field: cfg.id,
                log_name_field: cfg.name,
            },
        )
        return _cfg_to_response(cfg)

    # -----------------------------------------------------------------------
    # Delete
    # -----------------------------------------------------------------------

    @router.delete(f"{prefix}/{{connection_id}}", status_code=204)
    async def delete_connection(
        connection_id: int,
        session: AsyncSession = Depends(get_session),
    ) -> None:
        cfg = await session.get(model, connection_id)
        if cfg is None:
            raise HTTPException(
                status_code=404,
                detail={
                    "resourceType": "OperationOutcome",
                    "issue": [{"severity": "error", "code": "not-found", "diagnostics": "Connection not found"}],
                },
            )

        if cfg.is_default:
            raise HTTPException(
                status_code=409,
                detail={
                    "resourceType": "OperationOutcome",
                    "issue": [
                        {
                            "severity": "error",
                            "code": "conflict",
                            "diagnostics": f"Cannot delete the built-in {default_name} connection.",
                        }
                    ],
                },
            )

        if cfg.is_active:
            raise HTTPException(
                status_code=409,
                detail={
                    "resourceType": "OperationOutcome",
                    "issue": [
                        {
                            "severity": "error",
                            "code": "conflict",
                            "diagnostics": (
                                "Cannot delete the active connection. Activate a different connection first."
                            ),
                        }
                    ],
                },
            )

        if job_fk_column is not None:
            active_jobs = await session.execute(
                select(Job.id).where(
                    job_fk_column == connection_id,
                    Job.status.in_([JobStatus.queued, JobStatus.running]),
                )
            )
            if active_jobs.first() is not None:
                raise HTTPException(
                    status_code=409,
                    detail={
                        "resourceType": "OperationOutcome",
                        "issue": [
                            {
                                "severity": "error",
                                "code": "conflict",
                                "diagnostics": "Cannot delete a connection with queued or running jobs. "
                                "Wait for jobs to complete or cancel them first.",
                            }
                        ],
                    },
                )

        cfg_name = cfg.name
        await session.delete(cfg)
        await session.commit()
        log.info(
            log_event,
            extra={
                "event": log_event,
                "action": "delete",
                log_id_field: connection_id,
                log_name_field: cfg_name,
            },
        )

    # -----------------------------------------------------------------------
    # Activate
    # -----------------------------------------------------------------------

    @router.post(f"{prefix}/{{connection_id}}/activate", response_model=response_schema)
    async def activate_connection(
        connection_id: int,
        session: AsyncSession = Depends(get_session),
    ) -> dict:
        cfg = await session.get(model, connection_id)
        if cfg is None:
            raise HTTPException(
                status_code=404,
                detail={
                    "resourceType": "OperationOutcome",
                    "issue": [{"severity": "error", "code": "not-found", "diagnostics": "Connection not found"}],
                },
            )

        # Already active — nothing to do
        if cfg.is_active:
            return _cfg_to_response(cfg)

        try:
            await session.execute(sa_update(model).where(model.is_active.is_(True)).values(is_active=False))
            cfg.is_active = True
            await session.commit()
        except IntegrityError:
            # The partial unique index `idx_one_active_<kind>` rejected the
            # commit because two activate requests raced. Caller retries.
            await session.rollback()
            raise HTTPException(
                status_code=409,
                detail={
                    "resourceType": "OperationOutcome",
                    "issue": [
                        {
                            "severity": "error",
                            "code": "conflict",
                            "diagnostics": "Another connection was activated simultaneously. Please retry.",
                        }
                    ],
                },
            )
        await session.refresh(cfg)
        return _cfg_to_response(cfg)

    # -----------------------------------------------------------------------
    # Test connection
    # -----------------------------------------------------------------------

    @router.post("/test-connection")
    async def test_connection(body: test_request_schema) -> dict:
        body_url = getattr(body, url_field)
        _validate_auth_type(body.auth_type)
        _validate_smart_credentials(body.auth_type, body.auth_credentials)
        try:
            return await verify_fhir_connection(
                fhir_url=body_url,
                auth_type=body.auth_type,
                auth_credentials=body.auth_credentials,
            )
        except FhirOperationError as exc:
            log.warning(
                f"{kind.value} connection test failed",
                extra={
                    url_field: sanitize_url(body_url),
                    "status_code": exc.status_code,
                    "error": sanitize_error(exc),
                },
            )
            if exc.status_code is not None:
                hint = HINT_BY_STATUS.get(exc.status_code)
            else:
                hint = hint_for_network_exception(exc.__cause__) if exc.__cause__ else None
            http_status = exc.status_code if exc.status_code and exc.status_code >= 400 else 502
            raise HTTPException(
                status_code=http_status,
                detail=build_error_envelope(
                    operation="test-connection",
                    url=body_url,
                    status_code=exc.status_code,
                    outcome=exc.outcome,
                    latency_ms=exc.latency_ms,
                    hint=hint,
                ),
            )
        except ValueError as exc:
            log.warning(
                f"{kind.value} connection test rejected",
                extra={url_field: sanitize_url(body_url), "error": sanitize_error(exc)},
            )
            raise HTTPException(
                status_code=400,
                detail={
                    "resourceType": "OperationOutcome",
                    "issue": [{"severity": "error", "code": "security", "diagnostics": sanitize_error(exc)}],
                },
            )
        except Exception as exc:
            log.warning(
                f"{kind.value} connection test failed",
                extra={url_field: sanitize_url(body_url), "error": sanitize_error(exc)},
            )
            raise HTTPException(
                status_code=502,
                detail={
                    "resourceType": "OperationOutcome",
                    "issue": [{"severity": "error", "code": "exception", "diagnostics": sanitize_error(exc)}],
                },
            )

    return router
