# Contributing to Lenny

This guide collects the recurring "how do I add X" recipes that are easier
to walk than to grep for. Architecture-level docs live in `docs/architecture.md`;
project-wide conventions live in `CLAUDE.md`. This file is for short, opinionated
recipes — the kind of thing that helps a contributor land their first PR
without re-deriving the design.

## How to add a connection kind

A "connection kind" is a typed FHIR endpoint Lenny manages: CDR (clinical data
repository), MCS (measure calculation server), and — eventually — TS (terminology
server), MR (measure repository), MRR (measure-results repository). Each kind
ships as a row type in its own table with the same connection-management
surface: list, create, get, update, delete, activate, test-connection.

Because everything beyond the URL field name and one or two kind-specific
columns is shared, adding a new kind is a five-step recipe. The seam is the
generic `make_connection_router(...)` factory at `backend/app/routes/connection_factory.py`.
The reference implementation is the MCS kind (PRs #283/#290/#292/#293/#294,
v0.0.17.1 → v0.0.17.5) — copy-paste from there when in doubt.

### Step 1: Add the SQLAlchemy model

Create `backend/app/models/{kind}_config.py` mirroring `mcs_config.py`. The
file declares a model that inherits both `Base` and `ConnectionConfigMixin`:

```python
from sqlalchemy import Boolean, Index, String
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base
from app.models.connection_base import ConnectionConfigMixin


class TSConfig(Base, ConnectionConfigMixin):  # example: terminology server
    __tablename__ = "ts_configs"
    __table_args__ = (
        # Partial unique index — at most one row may have is_active=True.
        # Both Postgres (`WHERE is_active = TRUE`) and SQLite (`is_active = 1`)
        # honor this; Base.metadata.create_all generates it on a fresh DB.
        Index(
            "idx_one_active_ts",
            "is_active",
            unique=True,
            sqlite_where="is_active = 1",
            postgresql_where="is_active = TRUE",
        ),
    )

    ts_url: Mapped[str] = mapped_column(String, nullable=False)
    # Add kind-specific columns here. CDR has `is_read_only`; MCS has none.
    # Resist adding fields that "feel useful" — every column is a forward-compat
    # liability. Add only what the kind genuinely needs.
```

`ConnectionConfigMixin` provides the shared columns: `id`, `name`, `auth_type`,
encrypted `auth_credentials`, `is_active`, `is_default`, `request_timeout_seconds`,
`created_at`, `updated_at`.

### Step 2: Wire the schema migration

In `backend/app/main.py`, find `_run_schema_migrations` and add an idempotent
`ALTER TABLE` block for the new table. Mirror the existing CDR/MCS blocks
(`ADD COLUMN IF NOT EXISTS` style on Postgres; SQLite just runs `Base.metadata.create_all`
which creates the new table outright). Then add the partial-unique-index entry
to the `_INDEX_DEFS` list near `seed_default_connections` so the lifespan
hook creates `idx_one_active_{kind}` on both dialects.

For a brand-new table (no existing rows to migrate), the `Base.metadata.create_all`
path takes care of column creation. The `_run_schema_migrations` block is for
when you later need to add columns to an established kind — keep it ready,
but you can leave it empty on day one.

### Step 3: Mount the routes via the factory

In `backend/app/routes/settings.py`:

1. Import the new model + define three Pydantic schemas: `{Kind}ConnectionResponse`,
   `{Kind}ConnectionCreate`, `{Kind}TestConnectionRequest`. Mirror the MCS
   schemas at lines ~71-92.
2. **Cap `request_timeout_seconds` at 1800.** The create schema MUST declare
   `request_timeout_seconds: int = Field(default=30, ge=1, le=_MAX_REQUEST_TIMEOUT_SECONDS)`.
   This is design-doc threat surface #3 (timeout-as-DoS-vector) — see CHANGELOG
   v0.0.17.8.
3. **Do not include `auth_credentials` in the response schema.** They round-trip
   to the DB encrypted and should never leave the backend in plaintext.
4. Add `router.include_router(make_connection_router(model=TSConfig, ...))`
   below the MCS instantiation. Pass `kind=ConnectionKind.ts`, `url_field="ts_url"`,
   `default_name="Local Terminology Server"`, and `job_fk_column=Job.ts_id`
   (or `None` if you haven't wired a Job FK for this kind yet — MCS shipped
   that way in PR #293 and added the FK in PR #294).

The factory handles list, create, get, update, delete, activate, and
`{prefix}/test-connection` routes uniformly. Custom kind-specific routes
(like the MCS `/probe` endpoint) live directly in `settings.py`, after
the factory `include_router` call.

### Step 4: Add the FastAPI dependency

In `backend/app/dependencies.py`, add a `get_active_{kind}` async function
mirroring `get_active_mcs`. Returns a `ConnectionContext` with `kind=ConnectionKind.{kind}`
and falls back to a sensible default if no row is active (defensive — the
seed should ensure a row, but the fallback prevents `Depends` failures during
startup races).

Add the kind's URL field to `ConnectionContext` (e.g., `ts_url: str | None = None`)
and update the `url` kind-agnostic property to dispatch on it.

### Step 5: Add the frontend surface

This is the one PR #5a/#5b laid the groundwork for, so it's three small edits:

1. **`frontend/src/api/client.js`** — add the 7 functions for the kind:
   `get{Kind}Connections`, `create{Kind}Connection`, `get{Kind}Connection`,
   `update{Kind}Connection`, `delete{Kind}Connection`, `activate{Kind}Connection`,
   `test{Kind}Connection`. Mirror the MCS block. Each test-connection function
   targets `/settings/{kind}-connections/test-connection` (per-kind path —
   the route-collision fix in PR #5a).
2. **`frontend/src/components/ConnectionModal.js`** — add a `KIND_SPECS`
   entry for the new kind. Set `urlField`, `urlLabel`, `urlPlaceholder`,
   `showReadOnly` (typically `false` for non-CDR kinds), and the API trio.
3. **`frontend/src/components/ConnectionSection.js`** — add a `KIND_API`
   entry mirroring the existing CDR/MCS rows. Then drop a
   `<ConnectionSection kind="{kind}" onChange={loadHealth} />` line into
   `frontend/src/pages/SettingsPage.js` next to the existing CDR + MCS cards.

For the topbar chip, add `{ kind: "{kind}", settingsHash: "#{kind}-connections" }`
to `HEALTH_KINDS` in `frontend/src/App.js` and extend the backend `/health`
endpoint to probe the active connection of the new kind. The
`HealthChipGroup` and 4-state machine are already kind-agnostic — no
changes needed there.

### What you should not need to touch

- The factory itself. If you find yourself editing
  `backend/app/routes/connection_factory.py` to add a kind, stop and ask
  why — almost every "this kind is different" instinct is parameterizable
  via the factory's existing kwargs. Open an issue first.
- The `EncryptedJSON` machinery. CDR and MCS share `CDR_FERNET_KEY` (env var
  or `/run/secrets/cdr_fernet_key`); new kinds inherit the same key and the
  same `auth_credentials` encrypted column. Per-kind key isolation is an
  intentional non-goal — operationally, the connectathon doesn't need it,
  and rotating one shared key is simpler than rotating four. The env var
  name is misleading post-MCS (`CDR_FERNET_KEY` for non-CDR kinds), but
  renaming is a breaking change that's been deferred. If your kind
  legitimately needs separate credentials encryption, raise it on a design
  issue first.
- `HealthIndicator`, `HealthChipGroup`, `ConnectionSection`. All three are
  already kind-agnostic — adding a kind is data-table changes, not
  component changes.

### Reference PRs

- **CDR factory extraction:** PR #292 (v0.0.17.3) — the seam this whole
  recipe relies on.
- **MCS as the canonical reference:** PRs #293, #294 (v0.0.17.4, v0.0.17.5)
  — backend; PR #298 (v0.0.17.6), PR #299 (v0.0.17.7) — frontend.

### Smoke checklist for a new kind

Before opening the PR:

- [ ] All three CRUD routes round-trip: `POST` → `GET` → `PUT` → `POST .../activate` → `DELETE`.
- [ ] `request_timeout_seconds=86400` is rejected with 422.
- [ ] The seeded default row exists after a fresh DB boot.
- [ ] Concurrent activation of two rows raises `IntegrityError` (regression test).
- [ ] Topbar chip renders with the new kind's name when active.
- [ ] CI-equivalent integration suite passes locally (per `CLAUDE.md` mandate).
