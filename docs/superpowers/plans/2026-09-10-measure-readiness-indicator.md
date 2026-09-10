# Measure Readiness Indicator Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Show on the Measures page whether each measure is completely defined on the active MCS, so a user learns a measure cannot be evaluated before starting a job rather than after every patient fails.

**Architecture:** A per-`(mcs_id, measure_id, measure_version)` verdict cached in a new `measure_readiness` table. A background sweep, kicked by `asyncio.create_task` (the pattern `routes/settings.py` already uses for factory-reset and reseed), calls `$data-requirements` on each measure and then checks that every ValueSet canonical the server names is present. `GET /measures` left-joins the cache and never blocks on a check.

**Tech Stack:** FastAPI, SQLAlchemy 2.0 async, httpx, PostgreSQL (SQLite in tests), React 18 (plain JS), CSS Modules, pytest / pytest-asyncio, React Testing Library.

**Spec:** `docs/superpowers/specs/2026-09-10-measure-readiness-design.md`

## Global Constraints

- Python 3.10+; `X | None` union syntax, never `Optional[X]`. Type hints required.
- React is plain JavaScript, not TypeScript. PascalCase components, co-located CSS Modules.
- All configurable values go through `backend/app/config.py` as environment variables — never hardcoded.
- Conventional commits (`feat:`, `fix:`, `chore:`, `docs:`, `test:`).
- Lint must pass: `cd backend && ruff check app/ tests/ && ruff format --check app/ tests/`.
- This feature is **read-only against the MCS**. No task may add a write, upload, or repair path — that is an explicit non-goal of the spec.
- Never render a check that failed to complete as "not ready". Only an answer from the server turns a row red.

## Deviations from the spec

Three, found while mapping the spec onto the code. Each is a simplification; none changes observable behaviour.

1. **No `_run_schema_migrations` entry is needed.** The spec says to add a `CREATE TABLE IF NOT EXISTS`. Reading `main.py:469-475`, `_run_schema_migrations(conn)` runs *before* `Base.metadata.create_all`, and `create_all` issues `CREATE TABLE` for any table not already present — including on an existing database. `_run_schema_migrations` exists for `ALTER`s to tables `create_all` will not touch. A brand-new table needs nothing there.
2. **MCS activation does not invalidate anything.** Verdicts are keyed by `mcs_id`, so activating a different MCS selects a different set of rows rather than staling the current ones. Rows missing for the newly active MCS are created on the next `GET /measures`, which kicks the sweep. The spec listed activation as a trigger; it is at most a cache-warming optimisation, and it is dropped.
3. **Measure upload invalidates every row for that MCS, not just the uploaded measure.** An uploaded bundle can carry a Library (`Status`, say) that several *other* measures were missing. Invalidating only the uploaded measure would leave those stale and red.

What remains as a real invalidation trigger: **MCS URL change**, **measure upload**, **measure delete**. All three are "same `mcs_id`, different content".

## File Structure

**Created**
- `backend/app/models/measure_readiness.py` — `ReadinessState` enum + `MeasureReadiness` ORM model. Storage shape only.
- `backend/app/services/measure_readiness.py` — the check (pure logic + HTTP), the sweep (persistence + concurrency), and invalidation. No FastAPI imports.
- `backend/tests/test_services_measure_readiness.py` — unit tests for the service.
- `backend/tests/integration/test_measure_readiness.py` — against the local prebaked stack.

**Modified**
- `backend/app/config.py` — two new settings.
- `backend/app/models/__init__.py` — export the new model so `create_all` sees it.
- `backend/app/main.py` — startup reclaim of rows stranded in `checking`.
- `backend/app/routes/measures.py` — decorate `GET /measures`; add `POST /measures/readiness/refresh`; invalidate on upload and delete.
- `backend/app/routes/connection_factory.py` — optional `on_url_change` hook.
- `backend/app/routes/settings.py` — pass the hook for the MCS router only.
- `backend/tests/test_routes_measures.py` — route-level tests.
- `frontend/src/pages/MeasuresPage.js` + `MeasuresPage.module.css` — Readiness column, detail row, Re-check, polling.
- `frontend/src/pages/MeasuresPage.test.js` — one case per state plus detail and polling.

---

### Task 1: Storage — model, config, startup reclaim

**Files:**
- Create: `backend/app/models/measure_readiness.py`
- Modify: `backend/app/models/__init__.py`
- Modify: `backend/app/config.py`
- Modify: `backend/app/main.py` (in the startup block, beside the existing `admin_operations` reconcile at ~line 540)
- Test: `backend/tests/test_services_measure_readiness.py`

**Interfaces:**
- Consumes: nothing.
- Produces: `ReadinessState` (enum with members `ready`, `not_ready`, `checking`, `unknown`, all `str`-valued); `MeasureReadiness` ORM model with columns `id`, `mcs_id`, `measure_id`, `measure_version`, `state`, `missing_libraries`, `missing_valuesets`, `error`, `duration_ms`, `checked_at`; `settings.READINESS_TIMEOUT_SECONDS: int` and `settings.READINESS_CONCURRENCY: int`.

- [ ] **Step 1: Write the failing test**

Create `backend/tests/test_services_measure_readiness.py`:

```python
"""Tests for the measure readiness check, sweep, and storage model."""

import pytest
import pytest_asyncio

pytestmark = pytest.mark.asyncio


@pytest_asyncio.fixture
async def mcs_row(test_session):
    """A writable MCS row to hang readiness rows off."""
    from sqlalchemy import update as sa_update

    from app.models.connection_base import AuthType
    from app.models.mcs_config import MCSConfig

    await test_session.execute(sa_update(MCSConfig).values(is_active=False))
    cfg = MCSConfig(
        name="Readiness MCS",
        mcs_url="https://readiness-mcs.example.com/fhir",
        auth_type=AuthType.bearer,
        auth_credentials={"token": "tok"},
        is_active=True,
        is_default=False,
        is_read_only=False,
        request_timeout_seconds=30,
    )
    test_session.add(cfg)
    await test_session.commit()
    await test_session.refresh(cfg)
    return cfg


async def test_readiness_row_round_trips(test_session, mcs_row):
    from sqlalchemy import select

    from app.models.measure_readiness import MeasureReadiness, ReadinessState

    row = MeasureReadiness(
        mcs_id=mcs_row.id,
        measure_id="CMS122FHIRDiabetesAssessGreaterThan9Percent",
        measure_version="0.5.000",
        state=ReadinessState.not_ready,
        missing_libraries=["Status 1.15.000"],
        missing_valuesets=["http://cts.nlm.nih.gov/fhir/ValueSet/2.16.840.1.113883.3.464.1003.1003"],
        error="Could not load source for library Status, version 1.15.000, namespace uri null.",
        duration_ms=1424,
    )
    test_session.add(row)
    await test_session.commit()

    found = (await test_session.execute(select(MeasureReadiness))).scalars().all()
    assert len(found) == 1
    assert found[0].state is ReadinessState.not_ready
    assert found[0].missing_libraries == ["Status 1.15.000"]
    assert found[0].measure_version == "0.5.000"


async def test_readiness_is_unique_per_mcs_measure_version(test_session, mcs_row):
    """A second row for the same (mcs, measure, version) must be rejected.

    Without this, a sweep that races itself silently doubles every verdict and
    GET /measures picks an arbitrary one.
    """
    from sqlalchemy.exc import IntegrityError

    from app.models.measure_readiness import MeasureReadiness, ReadinessState

    for _ in range(2):
        test_session.add(
            MeasureReadiness(
                mcs_id=mcs_row.id,
                measure_id="CMS122",
                measure_version="0.5.000",
                state=ReadinessState.checking,
            )
        )
    with pytest.raises(IntegrityError):
        await test_session.commit()
    await test_session.rollback()


async def test_unversioned_measures_do_not_collide(test_session, mcs_row):
    """A missing FHIR version stores as "" not NULL.

    SQL treats NULL as distinct from NULL in a unique constraint, so a NULL
    version would let unlimited duplicate rows through for exactly the measures
    least likely to be well-formed.
    """
    from sqlalchemy.exc import IntegrityError

    from app.models.measure_readiness import MeasureReadiness, ReadinessState

    for _ in range(2):
        test_session.add(
            MeasureReadiness(mcs_id=mcs_row.id, measure_id="NoVersion", state=ReadinessState.unknown)
        )
    with pytest.raises(IntegrityError):
        await test_session.commit()
    await test_session.rollback()


def test_readiness_settings_have_defaults():
    from app.config import settings

    assert settings.READINESS_TIMEOUT_SECONDS == 60
    assert settings.READINESS_CONCURRENCY == 2
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `cd backend && python3 -m pytest tests/test_services_measure_readiness.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'app.models.measure_readiness'`, and the settings test fails with `AttributeError`.

- [ ] **Step 3: Write the model**

Create `backend/app/models/measure_readiness.py`:

```python
"""Per-measure readiness verdicts, cached against the MCS that was checked.

A verdict answers one question: can the active MCS actually evaluate this
measure? It is keyed by `(mcs_id, measure_id, measure_version)` so that
re-publishing a measure under a new version re-checks it rather than inheriting
the old verdict.

`measure_version` is `""` rather than NULL when the FHIR resource carries no
version. SQL treats NULL as distinct from NULL inside a unique constraint, so a
nullable column would silently permit duplicate rows for exactly the measures
least likely to be well-formed.
"""

import enum
from datetime import datetime

from sqlalchemy import JSON, DateTime, Enum, ForeignKey, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base

__all__ = ["MeasureReadiness", "ReadinessState"]


class ReadinessState(str, enum.Enum):
    """Four states, rendered as three icons.

    `unknown` is deliberately distinct from `not_ready`: it means the check could
    not reach a verdict (never run, timed out, refused authentication), not that
    the server said no. Rendering a timeout as red would let one slow server mark
    every measure broken.
    """

    ready = "ready"
    not_ready = "not_ready"
    checking = "checking"
    unknown = "unknown"


class MeasureReadiness(Base):
    __tablename__ = "measure_readiness"
    __table_args__ = (
        UniqueConstraint("mcs_id", "measure_id", "measure_version", name="uq_measure_readiness_key"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    mcs_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("mcs_configs.id", ondelete="CASCADE"), nullable=False, index=True
    )
    measure_id: Mapped[str] = mapped_column(String(256), nullable=False)
    measure_version: Mapped[str] = mapped_column(String(64), nullable=False, default="", server_default="")
    state: Mapped[ReadinessState] = mapped_column(Enum(ReadinessState), nullable=False)
    missing_libraries: Mapped[list | None] = mapped_column(JSON, nullable=True)
    missing_valuesets: Mapped[list | None] = mapped_column(JSON, nullable=True)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    duration_ms: Mapped[int | None] = mapped_column(Integer, nullable=True)
    checked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
```

- [ ] **Step 4: Export the model so `create_all` sees it**

`Base.metadata` only knows about models that have been imported. Add to `backend/app/models/__init__.py`, following whatever import/`__all__` style is already there:

```python
from app.models.measure_readiness import MeasureReadiness, ReadinessState
```

and add `"MeasureReadiness"` and `"ReadinessState"` to `__all__` if that file defines one.

**Note:** no entry in `main.py::_run_schema_migrations` is required. That function runs before `Base.metadata.create_all` and exists for `ALTER`s; `create_all` creates tables that do not yet exist, including on an existing database.

- [ ] **Step 5: Add the two settings**

In `backend/app/config.py`, inside `class Settings`, after `MAX_RETRIES`:

```python
    # Readiness check (#434). $data-requirements measured at 6-11s per measure
    # against the local engine, so this needs its own ceiling rather than
    # borrowing MCSConfig.request_timeout_seconds (default 30).
    READINESS_TIMEOUT_SECONDS: int = 60
    # Concurrency cap for the sweep. fhir_client.py:371-375 records
    # $data-requirements OOM-killing the measure engine when called per patient;
    # a shared server deserves the same restraint.
    READINESS_CONCURRENCY: int = 2
```

- [ ] **Step 6: Run the tests to verify they pass**

Run: `cd backend && python3 -m pytest tests/test_services_measure_readiness.py -v`
Expected: PASS — 4 passed.

- [ ] **Step 7: Add the startup reclaim test**

Append to `backend/tests/test_services_measure_readiness.py`:

```python
async def test_startup_reclaims_stranded_checking_rows(test_session, mcs_row):
    """`asyncio.create_task` does not survive a restart.

    A container that dies mid-sweep leaves rows in `checking` forever, which
    renders as a spinner that never resolves. Startup must convert them.
    """
    from sqlalchemy import select

    from app.models.measure_readiness import MeasureReadiness, ReadinessState
    from app.services.measure_readiness import reclaim_stranded_checks

    test_session.add(
        MeasureReadiness(
            mcs_id=mcs_row.id, measure_id="CMS122", measure_version="0.5.000", state=ReadinessState.checking
        )
    )
    test_session.add(
        MeasureReadiness(
            mcs_id=mcs_row.id, measure_id="CMS124", measure_version="1.0.000", state=ReadinessState.ready
        )
    )
    await test_session.commit()

    await reclaim_stranded_checks(test_session)

    rows = {r.measure_id: r for r in (await test_session.execute(select(MeasureReadiness))).scalars().all()}
    assert rows["CMS122"].state is ReadinessState.unknown
    assert rows["CMS122"].error == "Interrupted by backend restart"
    assert rows["CMS124"].state is ReadinessState.ready  # untouched
```

- [ ] **Step 8: Run it to verify it fails**

Run: `cd backend && python3 -m pytest tests/test_services_measure_readiness.py::test_startup_reclaims_stranded_checking_rows -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'app.services.measure_readiness'`.

- [ ] **Step 9: Create the service module with the reclaim function**

Create `backend/app/services/measure_readiness.py`:

```python
"""Measure readiness: can the active MCS actually evaluate this measure?

Two questions, in order:
  1. Does `$data-requirements` succeed? That is the compile check — it is what
     fails when a Library the CQL includes is absent.
  2. Is every ValueSet canonical the server named actually present?

Lenny parses no CQL. The server computes the dependency closure and returns it.
"""

import logging

from sqlalchemy import update as sa_update
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.measure_readiness import MeasureReadiness, ReadinessState

logger = logging.getLogger(__name__)


async def reclaim_stranded_checks(session: AsyncSession) -> int:
    """Convert rows left in `checking` by a crashed sweep into `unknown`.

    Called at startup. `asyncio.create_task` does not survive a restart, so
    without this a killed container leaves a spinner that never resolves.
    Returns the number of rows reclaimed.
    """
    result = await session.execute(
        sa_update(MeasureReadiness)
        .where(MeasureReadiness.state == ReadinessState.checking)
        .values(state=ReadinessState.unknown, error="Interrupted by backend restart")
    )
    await session.commit()
    return result.rowcount or 0
```

- [ ] **Step 10: Run the test to verify it passes**

Run: `cd backend && python3 -m pytest tests/test_services_measure_readiness.py -v`
Expected: PASS — 5 passed.

- [ ] **Step 11: Wire the reclaim into startup**

In `backend/app/main.py`, in the startup block, directly after the existing `Admin operations reconciled` try/except (~line 560), add:

```python
    # Reclaim readiness rows stranded in `checking` by a restart (#434).
    try:
        from app.db import async_session as _async_session
        from app.services.measure_readiness import reclaim_stranded_checks

        async with _async_session() as _session:
            reclaimed = await reclaim_stranded_checks(_session)
        logger.info("Measure readiness reclaimed", extra={"rows": reclaimed})
    except Exception:
        logger.exception("Measure readiness reclaim failed — continuing startup")
```

The bare `except` mirrors every other startup step here: a reclaim failure must not stop the app from booting.

- [ ] **Step 12: Run lint and the full unit suite**

Run: `cd backend && ruff check app/ tests/ && ruff format --check app/ tests/ && python3 -m pytest tests/ --ignore=tests/integration -q`
Expected: lint clean; all tests pass.

- [ ] **Step 13: Commit**

```bash
git add backend/app/models/measure_readiness.py backend/app/models/__init__.py \
        backend/app/config.py backend/app/main.py \
        backend/app/services/measure_readiness.py \
        backend/tests/test_services_measure_readiness.py
git commit -m "feat: add measure readiness storage and startup reclaim (#434)"
```

---

### Task 2: Parsing helpers — ValueSet canonicals and missing libraries

**Files:**
- Modify: `backend/app/services/measure_readiness.py`
- Test: `backend/tests/test_services_measure_readiness.py`

**Interfaces:**
- Consumes: nothing from Task 1 beyond the module existing.
- Produces:
  - `extract_valueset_canonicals(library: dict) -> list[str]` — sorted, de-duplicated, version suffix stripped at `|`.
  - `extract_missing_libraries(diagnostic: str | None) -> list[str]` — e.g. `["Status 1.15.000"]`; `[]` when nothing matches.

- [ ] **Step 1: Write the failing tests**

Append to `backend/tests/test_services_measure_readiness.py`:

```python
def test_extract_valueset_canonicals_from_both_locations():
    """$data-requirements names valuesets in two places; both count."""
    from app.services.measure_readiness import extract_valueset_canonicals

    library = {
        "resourceType": "Library",
        "dataRequirement": [
            {"type": "Condition", "codeFilter": [{"path": "code", "valueSet": "http://vs/one"}]},
            {"type": "Observation", "codeFilter": [{"path": "code", "valueSet": "http://vs/two|20210101"}]},
        ],
        "relatedArtifact": [
            {"type": "depends-on", "resource": "http://vs/three"},
            {"type": "depends-on", "resource": "https://madie.cms.gov/Library/FHIRHelpers|4.4.000"},
        ],
    }
    assert extract_valueset_canonicals(library) == ["http://vs/one", "http://vs/three", "http://vs/two"]


def test_extract_valueset_canonicals_ignores_libraries_and_dedupes():
    """A Library dependency is not a ValueSet, and the same VS appears repeatedly."""
    from app.services.measure_readiness import extract_valueset_canonicals

    library = {
        "dataRequirement": [
            {"codeFilter": [{"valueSet": "http://vs/dupe"}]},
            {"codeFilter": [{"valueSet": "http://vs/dupe|1.0.0"}]},
        ],
        "relatedArtifact": [{"type": "depends-on", "resource": "Library/Status"}],
    }
    assert extract_valueset_canonicals(library) == ["http://vs/dupe"]


def test_extract_valueset_canonicals_tolerates_empty_library():
    from app.services.measure_readiness import extract_valueset_canonicals

    assert extract_valueset_canonicals({}) == []
    assert extract_valueset_canonicals({"dataRequirement": [], "relatedArtifact": []}) == []


def test_extract_missing_libraries_parses_the_engine_diagnostic():
    """The real string, verbatim from the 2026-09-10 connectathon failure."""
    from app.services.measure_readiness import extract_missing_libraries

    diagnostic = (
        "Exception for library: CMS122FHIRDiabetesAssessGreaterThan9Percent, "
        "Message: Could not load source for library Status, version 1.15.000, namespace uri null."
    )
    assert extract_missing_libraries(diagnostic) == ["Status 1.15.000"]


def test_extract_missing_libraries_returns_empty_when_unrecognised():
    """An unparsed diagnostic is not evidence of a missing library.

    The raw text is still stored in `error`; this list stays empty rather than
    inventing a name.
    """
    from app.services.measure_readiness import extract_missing_libraries

    assert extract_missing_libraries("HTTP 500 Internal Server Error") == []
    assert extract_missing_libraries(None) == []
```

- [ ] **Step 2: Run them to verify they fail**

Run: `cd backend && python3 -m pytest tests/test_services_measure_readiness.py -k "extract" -v`
Expected: FAIL — `ImportError: cannot import name 'extract_valueset_canonicals'`.

- [ ] **Step 3: Implement both helpers**

Add to `backend/app/services/measure_readiness.py`, below the imports (add `import re` to the import block):

```python
# HAPI's CQL engine reports an unresolvable include as:
#   "Could not load source for library Status, version 1.15.000, namespace uri null."
# It names only the FIRST one it cannot load, so a parsed result is a starting
# point, never a complete inventory of what is missing.
_MISSING_LIBRARY_RE = re.compile(r"Could not load source for library ([\w.\-]+), version ([\w.\-]+)")


def extract_valueset_canonicals(library: dict) -> list[str]:
    """Collect every ValueSet canonical named by a `$data-requirements` response.

    Two locations carry them: `dataRequirement[].codeFilter[].valueSet` and
    `relatedArtifact[]` entries of type `depends-on`. Version suffixes are
    stripped at `|` so presence can be checked by URL. `relatedArtifact` also
    carries Library dependencies, which are excluded by URL shape.
    """
    found: set[str] = set()

    for requirement in library.get("dataRequirement") or []:
        for code_filter in requirement.get("codeFilter") or []:
            canonical = code_filter.get("valueSet")
            if canonical:
                found.add(canonical.split("|")[0])

    for artifact in library.get("relatedArtifact") or []:
        if artifact.get("type") != "depends-on":
            continue
        resource = artifact.get("resource") or ""
        if not resource or "/Library/" in resource or resource.startswith("Library/"):
            continue
        found.add(resource.split("|")[0])

    return sorted(found)


def extract_missing_libraries(diagnostic: str | None) -> list[str]:
    """Pull library names out of the engine's compile diagnostic.

    Returns `[]` when nothing matches — an unrecognised message is not evidence
    of a missing library, and the raw text is preserved in the verdict's `error`
    either way.
    """
    if not diagnostic:
        return []
    return [f"{name} {version}" for name, version in _MISSING_LIBRARY_RE.findall(diagnostic)]
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `cd backend && python3 -m pytest tests/test_services_measure_readiness.py -k "extract" -v`
Expected: PASS — 5 passed.

- [ ] **Step 5: Commit**

```bash
git add backend/app/services/measure_readiness.py backend/tests/test_services_measure_readiness.py
git commit -m "feat: parse valueset canonicals and missing libraries for readiness (#434)"
```

---

### Task 3: The per-measure check — four states

**Files:**
- Modify: `backend/app/services/measure_readiness.py`
- Test: `backend/tests/test_services_measure_readiness.py`

**Interfaces:**
- Consumes: `extract_valueset_canonicals`, `extract_missing_libraries` (Task 2); `ReadinessState` (Task 1).
- Produces:
  - `@dataclass ReadinessVerdict` with fields `state: ReadinessState`, `missing_libraries: list[str]`, `missing_valuesets: list[str]`, `error: str | None`, `duration_ms: int`.
  - `async def find_missing_valuesets(mcs_url: str, canonicals: list[str], *, auth_headers: dict[str, str], timeout: float, chunk_size: int = 10) -> list[str]` — raises on transport failure; the caller maps that to `unknown`.
  - `async def check_measure_readiness(mcs_url: str, measure_id: str, *, auth_headers: dict[str, str], timeout: float) -> ReadinessVerdict`.

- [ ] **Step 1: Write the failing tests**

Append to `backend/tests/test_services_measure_readiness.py`:

```python
def _dr_library(valuesets: list[str]) -> dict:
    return {
        "resourceType": "Library",
        "dataRequirement": [{"codeFilter": [{"valueSet": vs}]} for vs in valuesets],
    }


def _vs_bundle(urls: list[str]) -> dict:
    return {
        "resourceType": "Bundle",
        "type": "searchset",
        "total": len(urls),
        "entry": [{"resource": {"resourceType": "ValueSet", "url": u}} for u in urls],
    }


async def test_check_returns_ready_when_compiled_and_valuesets_present():
    import httpx

    from app.models.measure_readiness import ReadinessState
    from app.services.measure_readiness import check_measure_readiness

    def handler(request: httpx.Request) -> httpx.Response:
        if "$data-requirements" in str(request.url):
            return httpx.Response(200, json=_dr_library(["http://vs/a", "http://vs/b"]))
        return httpx.Response(200, json=_vs_bundle(["http://vs/a", "http://vs/b"]))

    transport = httpx.MockTransport(handler)
    verdict = await check_measure_readiness(
        "https://mcs.example.com/fhir", "CMS122", auth_headers={}, timeout=5.0, transport=transport
    )
    assert verdict.state is ReadinessState.ready
    assert verdict.missing_valuesets == []
    assert verdict.error is None


async def test_check_returns_not_ready_on_500_naming_a_missing_library():
    """The motivating failure: HTTP 500 + HAPI-0389 from the connectathon server."""
    import httpx

    from app.models.measure_readiness import ReadinessState
    from app.services.measure_readiness import check_measure_readiness

    outcome = {
        "resourceType": "OperationOutcome",
        "issue": [
            {
                "severity": "error",
                "code": "processing",
                "diagnostics": (
                    "HAPI-0389: Failed to call access method: java.lang.RuntimeException: "
                    "Could not load source for library Status, version 1.15.000, namespace uri null."
                ),
            }
        ],
    }
    transport = httpx.MockTransport(lambda request: httpx.Response(500, json=outcome))
    verdict = await check_measure_readiness(
        "https://mcs.example.com/fhir", "CMS122", auth_headers={}, timeout=5.0, transport=transport
    )
    assert verdict.state is ReadinessState.not_ready
    assert verdict.missing_libraries == ["Status 1.15.000"]
    assert "Could not load source for library Status" in verdict.error


async def test_check_returns_not_ready_on_200_carrying_an_error_outcome():
    """A 2xx is not proof of success — same shape #415 fixed for submit_data."""
    import httpx

    from app.models.measure_readiness import ReadinessState
    from app.services.measure_readiness import check_measure_readiness

    outcome = {
        "resourceType": "OperationOutcome",
        "issue": [{"severity": "error", "code": "exception", "diagnostics": "Measure is not valid"}],
    }
    transport = httpx.MockTransport(lambda request: httpx.Response(200, json=outcome))
    verdict = await check_measure_readiness(
        "https://mcs.example.com/fhir", "CMS122", auth_headers={}, timeout=5.0, transport=transport
    )
    assert verdict.state is ReadinessState.not_ready
    assert verdict.error == "Measure is not valid"


async def test_check_ignores_a_warning_only_outcome():
    """Warning/information outcomes are advisory and accompany real responses."""
    import httpx

    from app.models.measure_readiness import ReadinessState
    from app.services.measure_readiness import check_measure_readiness

    library = _dr_library(["http://vs/a"])
    library["contained"] = [
        {
            "resourceType": "OperationOutcome",
            "issue": [{"severity": "warning", "code": "informational", "diagnostics": "heads up"}],
        }
    ]

    def handler(request: httpx.Request) -> httpx.Response:
        if "$data-requirements" in str(request.url):
            return httpx.Response(200, json=library)
        return httpx.Response(200, json=_vs_bundle(["http://vs/a"]))

    verdict = await check_measure_readiness(
        "https://mcs.example.com/fhir",
        "CMS122",
        auth_headers={},
        timeout=5.0,
        transport=httpx.MockTransport(handler),
    )
    assert verdict.state is ReadinessState.ready


async def test_check_returns_not_ready_when_valuesets_are_absent():
    import httpx

    from app.models.measure_readiness import ReadinessState
    from app.services.measure_readiness import check_measure_readiness

    def handler(request: httpx.Request) -> httpx.Response:
        if "$data-requirements" in str(request.url):
            return httpx.Response(200, json=_dr_library(["http://vs/a", "http://vs/b", "http://vs/c"]))
        return httpx.Response(200, json=_vs_bundle(["http://vs/b"]))

    verdict = await check_measure_readiness(
        "https://mcs.example.com/fhir",
        "CMS122",
        auth_headers={},
        timeout=5.0,
        transport=httpx.MockTransport(handler),
    )
    assert verdict.state is ReadinessState.not_ready
    assert verdict.missing_valuesets == ["http://vs/a", "http://vs/c"]


async def test_check_returns_unknown_on_timeout_not_not_ready():
    """THE load-bearing test.

    $data-requirements measured at 6-11s. If a timeout rendered red, one slow
    server would mark every measure broken and the indicator would be noise.
    """
    import httpx

    from app.models.measure_readiness import ReadinessState
    from app.services.measure_readiness import check_measure_readiness

    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("timed out", request=request)

    verdict = await check_measure_readiness(
        "https://mcs.example.com/fhir",
        "CMS122",
        auth_headers={},
        timeout=5.0,
        transport=httpx.MockTransport(handler),
    )
    assert verdict.state is ReadinessState.unknown
    assert verdict.error is not None


@pytest.mark.parametrize("status", [401, 403])
async def test_check_returns_unknown_when_authentication_is_refused(status):
    """A credential problem is ours, not the measure's."""
    import httpx

    from app.models.measure_readiness import ReadinessState
    from app.services.measure_readiness import check_measure_readiness

    transport = httpx.MockTransport(lambda request: httpx.Response(status, json={}))
    verdict = await check_measure_readiness(
        "https://mcs.example.com/fhir", "CMS122", auth_headers={}, timeout=5.0, transport=transport
    )
    assert verdict.state is ReadinessState.unknown


async def test_check_returns_unknown_when_the_valueset_query_itself_fails():
    import httpx

    from app.models.measure_readiness import ReadinessState
    from app.services.measure_readiness import check_measure_readiness

    def handler(request: httpx.Request) -> httpx.Response:
        if "$data-requirements" in str(request.url):
            return httpx.Response(200, json=_dr_library(["http://vs/a"]))
        raise httpx.ConnectError("connection refused", request=request)

    verdict = await check_measure_readiness(
        "https://mcs.example.com/fhir",
        "CMS122",
        auth_headers={},
        timeout=5.0,
        transport=httpx.MockTransport(handler),
    )
    assert verdict.state is ReadinessState.unknown


async def test_check_sends_no_period_parameters():
    """Library resolution does not depend on a measurement period.

    The DEQM path (fhir_client.py:434) calls it bare; probe_mcs_data_requirements
    hardcodes 2024 dates. This spec follows the DEQM path.
    """
    import httpx

    from app.services.measure_readiness import check_measure_readiness

    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        if "$data-requirements" in str(request.url):
            seen["url"] = str(request.url)
            return httpx.Response(200, json=_dr_library([]))
        return httpx.Response(200, json=_vs_bundle([]))

    await check_measure_readiness(
        "https://mcs.example.com/fhir",
        "CMS122",
        auth_headers={},
        timeout=5.0,
        transport=httpx.MockTransport(handler),
    )
    assert "periodStart" not in seen["url"]
    assert "periodEnd" not in seen["url"]


async def test_find_missing_valuesets_chunks_long_lists():
    """URL length is finite; 23 canonicals must not become one query."""
    import httpx

    from app.services.measure_readiness import find_missing_valuesets

    present = [f"http://vs/{i}" for i in range(25)]
    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(str(request.url))
        return httpx.Response(200, json=_vs_bundle(present))

    missing = await find_missing_valuesets(
        "https://mcs.example.com/fhir",
        present,
        auth_headers={},
        timeout=5.0,
        chunk_size=10,
        transport=httpx.MockTransport(handler),
    )
    assert missing == []
    assert len(calls) == 3  # 25 canonicals at 10 per chunk
```

- [ ] **Step 2: Run them to verify they fail**

Run: `cd backend && python3 -m pytest tests/test_services_measure_readiness.py -k "check_ or find_missing" -v`
Expected: FAIL — `ImportError: cannot import name 'check_measure_readiness'`.

- [ ] **Step 3: Implement the verdict types and the check**

Add to `backend/app/services/measure_readiness.py`. Extend the import block with `import time`, `from dataclasses import dataclass, field`, `from datetime import datetime, timezone`, `import httpx`, and `from app.services.fhir_errors import FhirOperationOutcome, hint_for_network_exception`.

```python
@dataclass
class ReadinessVerdict:
    """The outcome of one measure's check. Maps 1:1 onto a `MeasureReadiness` row."""

    state: ReadinessState
    missing_libraries: list[str] = field(default_factory=list)
    missing_valuesets: list[str] = field(default_factory=list)
    error: str | None = None
    duration_ms: int = 0


def _error_diagnostic(outcome: FhirOperationOutcome | None) -> str | None:
    """The first error/fatal diagnostic, or None if the outcome is only advisory.

    Warning and information issues accompany successful responses; treating them
    as failures is the naive mistake #415 documented on the submit_data path.
    """
    if outcome is None:
        return None
    for issue in outcome.issues:
        if issue.severity in ("error", "fatal"):
            return issue.diagnostics or "Server reported an error with no diagnostic."
    return None


async def find_missing_valuesets(
    mcs_url: str,
    canonicals: list[str],
    *,
    auth_headers: dict[str, str],
    timeout: float,
    chunk_size: int = 10,
    transport: httpx.AsyncBaseTransport | None = None,
) -> list[str]:
    """Return the subset of `canonicals` the MCS does not hold.

    Chunked because a measure's closure runs to two dozen URLs and a single
    comma-joined query would outgrow practical URL limits. Raises on transport
    failure; the caller maps that to `unknown` rather than `not_ready`.
    """
    if not canonicals:
        return []

    present: set[str] = set()
    async with httpx.AsyncClient(timeout=timeout, transport=transport) as client:
        for start in range(0, len(canonicals), chunk_size):
            chunk = canonicals[start : start + chunk_size]
            resp = await client.get(
                f"{mcs_url}/ValueSet",
                params={"url": ",".join(chunk), "_elements": "url", "_count": str(len(chunk))},
                headers=auth_headers,
            )
            resp.raise_for_status()
            for entry in resp.json().get("entry") or []:
                url = (entry.get("resource") or {}).get("url")
                if url:
                    present.add(url.split("|")[0])

    return [c for c in canonicals if c not in present]


async def check_measure_readiness(
    mcs_url: str,
    measure_id: str,
    *,
    auth_headers: dict[str, str],
    timeout: float,
    transport: httpx.AsyncBaseTransport | None = None,
) -> ReadinessVerdict:
    """Decide whether `measure_id` can be evaluated on the MCS at `mcs_url`.

    Read-only. Never raises: every failure mode becomes a verdict, because the
    caller's job is to write a row, not to propagate an exception.

    No period parameters are sent. Whether the Library graph resolves does not
    depend on a measurement period, and the DEQM path already calls the
    operation bare.
    """
    started = time.monotonic()

    def elapsed() -> int:
        return round((time.monotonic() - started) * 1000)

    dr_url = f"{mcs_url}/Measure/{measure_id}/$data-requirements"
    try:
        async with httpx.AsyncClient(timeout=timeout, transport=transport) as client:
            resp = await client.get(dr_url, headers=auth_headers)
    except Exception as exc:
        return ReadinessVerdict(
            state=ReadinessState.unknown, error=hint_for_network_exception(exc), duration_ms=elapsed()
        )

    # Authentication failures are OUR problem, not the measure's. Checked before
    # the general non-2xx branch, which would otherwise call them not_ready.
    if resp.status_code in (401, 403):
        return ReadinessVerdict(
            state=ReadinessState.unknown,
            error=f"HTTP {resp.status_code}: the measure server refused the request. Check this connection's credentials.",
            duration_ms=elapsed(),
        )

    outcome = FhirOperationOutcome.from_response(resp)
    diagnostic = _error_diagnostic(outcome)

    if not resp.is_success:
        message = diagnostic or f"HTTP {resp.status_code} from $data-requirements."
        return ReadinessVerdict(
            state=ReadinessState.not_ready,
            missing_libraries=extract_missing_libraries(message),
            error=message,
            duration_ms=elapsed(),
        )

    # A 2xx can still carry an error OperationOutcome instead of the Library.
    if diagnostic:
        return ReadinessVerdict(
            state=ReadinessState.not_ready,
            missing_libraries=extract_missing_libraries(diagnostic),
            error=diagnostic,
            duration_ms=elapsed(),
        )

    try:
        library = resp.json()
    except Exception:
        return ReadinessVerdict(
            state=ReadinessState.unknown,
            error="$data-requirements returned a body that is not JSON.",
            duration_ms=elapsed(),
        )

    canonicals = extract_valueset_canonicals(library)
    try:
        missing = await find_missing_valuesets(
            mcs_url, canonicals, auth_headers=auth_headers, timeout=timeout, transport=transport
        )
    except Exception as exc:
        return ReadinessVerdict(
            state=ReadinessState.unknown,
            error=f"Could not verify value sets: {hint_for_network_exception(exc)}",
            duration_ms=elapsed(),
        )

    if missing:
        noun = "value set" if len(missing) == 1 else "value sets"
        return ReadinessVerdict(
            state=ReadinessState.not_ready,
            missing_valuesets=missing,
            error=f"{len(missing)} {noun} referenced by this measure are not on this server.",
            duration_ms=elapsed(),
        )

    return ReadinessVerdict(state=ReadinessState.ready, duration_ms=elapsed())
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `cd backend && python3 -m pytest tests/test_services_measure_readiness.py -v`
Expected: PASS — all tests in the file.

- [ ] **Step 5: Commit**

```bash
git add backend/app/services/measure_readiness.py backend/tests/test_services_measure_readiness.py
git commit -m "feat: four-state readiness check per measure (#434)"
```

---

### Task 4: The sweep — persistence, concurrency cap, invalidation

**Files:**
- Modify: `backend/app/services/measure_readiness.py`
- Test: `backend/tests/test_services_measure_readiness.py`

**Interfaces:**
- Consumes: `check_measure_readiness`, `ReadinessVerdict` (Task 3); `MeasureReadiness`, `ReadinessState` (Task 1).
- Produces:
  - `async def claim_unchecked(session, mcs_id: int, measures: list[tuple[str, str]]) -> list[tuple[str, str]]` — inserts `checking` rows for `(measure_id, version)` pairs with no row yet, returns the pairs it claimed.
  - `async def mark_all_checking(session, mcs_id: int, measures: list[tuple[str, str]]) -> None` — for the manual re-check path.
  - `async def run_sweep(mcs_id: int, measures: list[tuple[str, str]]) -> None` — opens its own session; safe as an `asyncio.create_task` target.
  - `async def invalidate_mcs(session, mcs_id: int) -> int` — deletes every row for that MCS, returns the count.

- [ ] **Step 1: Write the failing tests**

Append to `backend/tests/test_services_measure_readiness.py`:

```python
async def test_claim_unchecked_only_claims_measures_with_no_row(test_session, mcs_row):
    from app.models.measure_readiness import MeasureReadiness, ReadinessState
    from app.services.measure_readiness import claim_unchecked

    test_session.add(
        MeasureReadiness(
            mcs_id=mcs_row.id, measure_id="CMS122", measure_version="0.5.000", state=ReadinessState.ready
        )
    )
    await test_session.commit()

    claimed = await claim_unchecked(
        test_session, mcs_row.id, [("CMS122", "0.5.000"), ("CMS124", "1.0.000"), ("CMS125", "")]
    )
    assert sorted(claimed) == [("CMS124", "1.0.000"), ("CMS125", "")]


async def test_claim_unchecked_writes_checking_rows_so_repeat_loads_do_not_requeue(test_session, mcs_row):
    """The claim IS the de-duplication guard.

    GET /measures kicks a sweep for unchecked measures. Without a synchronous
    `checking` row, every page refresh during a 60s sweep queues another one.
    """
    from app.services.measure_readiness import claim_unchecked

    first = await claim_unchecked(test_session, mcs_row.id, [("CMS124", "1.0.000")])
    second = await claim_unchecked(test_session, mcs_row.id, [("CMS124", "1.0.000")])
    assert first == [("CMS124", "1.0.000")]
    assert second == []


async def test_invalidate_mcs_removes_only_that_connections_rows(test_session, mcs_row):
    from sqlalchemy import select

    from app.models.measure_readiness import MeasureReadiness, ReadinessState
    from app.services.measure_readiness import invalidate_mcs

    test_session.add(
        MeasureReadiness(mcs_id=mcs_row.id, measure_id="CMS122", measure_version="1", state=ReadinessState.ready)
    )
    test_session.add(
        MeasureReadiness(mcs_id=mcs_row.id + 999, measure_id="CMS122", measure_version="1", state=ReadinessState.ready)
    )
    await test_session.commit()

    removed = await invalidate_mcs(test_session, mcs_row.id)
    assert removed == 1
    remaining = (await test_session.execute(select(MeasureReadiness))).scalars().all()
    assert [r.mcs_id for r in remaining] == [mcs_row.id + 999]


async def test_run_sweep_writes_a_verdict_per_measure(test_session, mcs_row, monkeypatch):
    from sqlalchemy import select

    import app.services.measure_readiness as svc
    from app.models.measure_readiness import MeasureReadiness, ReadinessState

    async def fake_check(mcs_url, measure_id, **kwargs):
        if measure_id == "CMS122":
            return svc.ReadinessVerdict(
                state=ReadinessState.not_ready,
                missing_libraries=["Status 1.15.000"],
                error="Could not load source for library Status, version 1.15.000, namespace uri null.",
                duration_ms=11442,
            )
        return svc.ReadinessVerdict(state=ReadinessState.ready, duration_ms=6167)

    monkeypatch.setattr(svc, "check_measure_readiness", fake_check)
    monkeypatch.setattr(svc, "_session_factory", lambda: _SessionCtx(test_session))

    await svc.run_sweep(mcs_row.id, [("CMS122", "0.5.000"), ("CMS124", "1.0.000")])

    rows = {r.measure_id: r for r in (await test_session.execute(select(MeasureReadiness))).scalars().all()}
    assert rows["CMS122"].state is ReadinessState.not_ready
    assert rows["CMS122"].missing_libraries == ["Status 1.15.000"]
    assert rows["CMS122"].checked_at is not None
    assert rows["CMS124"].state is ReadinessState.ready


async def test_run_sweep_respects_the_concurrency_cap(test_session, mcs_row, monkeypatch):
    """fhir_client.py:371-375 records this operation OOM-killing the engine."""
    import asyncio

    import app.services.measure_readiness as svc
    from app.models.measure_readiness import ReadinessState

    in_flight = 0
    peak = 0

    async def fake_check(mcs_url, measure_id, **kwargs):
        nonlocal in_flight, peak
        in_flight += 1
        peak = max(peak, in_flight)
        await asyncio.sleep(0.01)
        in_flight -= 1
        return svc.ReadinessVerdict(state=ReadinessState.ready)

    monkeypatch.setattr(svc, "check_measure_readiness", fake_check)
    monkeypatch.setattr(svc, "_session_factory", lambda: _SessionCtx(test_session))

    await svc.run_sweep(mcs_row.id, [(f"CMS{i}", "1.0.0") for i in range(8)])
    assert peak <= 2


async def test_run_sweep_leaves_no_row_stuck_in_checking_when_a_check_explodes(test_session, mcs_row, monkeypatch):
    """A raising check must not leave a spinner that only a restart clears."""
    from sqlalchemy import select

    import app.services.measure_readiness as svc
    from app.models.measure_readiness import MeasureReadiness, ReadinessState

    async def fake_check(mcs_url, measure_id, **kwargs):
        raise RuntimeError("boom")

    monkeypatch.setattr(svc, "check_measure_readiness", fake_check)
    monkeypatch.setattr(svc, "_session_factory", lambda: _SessionCtx(test_session))

    await svc.run_sweep(mcs_row.id, [("CMS122", "0.5.000")])

    rows = (await test_session.execute(select(MeasureReadiness))).scalars().all()
    assert rows[0].state is ReadinessState.unknown
    assert "boom" in (rows[0].error or "")
```

Also add this helper near the top of the test file, under the imports — the sweep opens its own session in production, and the tests need it to reuse the test session without closing it:

```python
class _SessionCtx:
    """Async-context wrapper that yields the test session without closing it."""

    def __init__(self, session):
        self._session = session

    async def __aenter__(self):
        return self._session

    async def __aexit__(self, *exc_info):
        return False
```

- [ ] **Step 2: Run them to verify they fail**

Run: `cd backend && python3 -m pytest tests/test_services_measure_readiness.py -k "sweep or claim or invalidate" -v`
Expected: FAIL — `ImportError: cannot import name 'claim_unchecked'`.

- [ ] **Step 3: Implement the sweep**

Add to `backend/app/services/measure_readiness.py`. Extend the import block with `import asyncio`, `from sqlalchemy import delete as sa_delete, select`, and `from app.config import settings`.

```python
def _session_factory():
    """Indirection so tests can substitute the session without patching `app.db`.

    The sweep runs detached from any request, so it cannot take a `Depends`
    session — it must open its own.
    """
    from app.db import async_session

    return async_session()


async def claim_unchecked(
    session: AsyncSession, mcs_id: int, measures: list[tuple[str, str]]
) -> list[tuple[str, str]]:
    """Insert `checking` rows for measures with no verdict yet; return what was claimed.

    Writing the row synchronously, before the sweep starts, is what stops a page
    refresh during a 60s sweep from queueing a second one.
    """
    existing = set(
        (
            await session.execute(
                select(MeasureReadiness.measure_id, MeasureReadiness.measure_version).where(
                    MeasureReadiness.mcs_id == mcs_id
                )
            )
        ).all()
    )
    claimed = [(mid, ver) for mid, ver in measures if (mid, ver) not in existing]
    for measure_id, version in claimed:
        session.add(
            MeasureReadiness(
                mcs_id=mcs_id, measure_id=measure_id, measure_version=version, state=ReadinessState.checking
            )
        )
    if claimed:
        await session.commit()
    return claimed


async def mark_all_checking(session: AsyncSession, mcs_id: int, measures: list[tuple[str, str]]) -> None:
    """Force every listed measure into `checking`, inserting rows that are absent.

    Used by the manual re-check, where the point is to discard current verdicts.
    """
    await invalidate_mcs(session, mcs_id)
    for measure_id, version in measures:
        session.add(
            MeasureReadiness(
                mcs_id=mcs_id, measure_id=measure_id, measure_version=version, state=ReadinessState.checking
            )
        )
    await session.commit()


async def invalidate_mcs(session: AsyncSession, mcs_id: int) -> int:
    """Drop every cached verdict for one MCS. Returns the number removed.

    Deliberately whole-connection rather than per-measure: an uploaded bundle can
    carry a Library that several OTHER measures were missing, so invalidating
    only the uploaded measure would leave those stale and red.
    """
    result = await session.execute(sa_delete(MeasureReadiness).where(MeasureReadiness.mcs_id == mcs_id))
    await session.commit()
    return result.rowcount or 0


async def _store_verdict(
    session: AsyncSession, mcs_id: int, measure_id: str, version: str, verdict: ReadinessVerdict
) -> None:
    row = (
        await session.execute(
            select(MeasureReadiness).where(
                MeasureReadiness.mcs_id == mcs_id,
                MeasureReadiness.measure_id == measure_id,
                MeasureReadiness.measure_version == version,
            )
        )
    ).scalar_one_or_none()
    if row is None:
        row = MeasureReadiness(mcs_id=mcs_id, measure_id=measure_id, measure_version=version)
        session.add(row)
    row.state = verdict.state
    row.missing_libraries = verdict.missing_libraries or None
    row.missing_valuesets = verdict.missing_valuesets or None
    row.error = verdict.error
    row.duration_ms = verdict.duration_ms
    row.checked_at = datetime.now(timezone.utc)
    await session.commit()


async def run_sweep(mcs_id: int, measures: list[tuple[str, str]]) -> None:
    """Check every listed measure against one MCS and store the verdicts.

    Safe as an `asyncio.create_task` target: opens its own session and never
    raises. Concurrency is capped because `$data-requirements` is expensive
    enough to have OOM-killed the engine once already
    (`fhir_client.py:371-375`).
    """
    from app.dependencies import resolve_mcs_auth_headers
    from app.models.mcs_config import MCSConfig

    if not measures:
        return

    semaphore = asyncio.Semaphore(max(1, settings.READINESS_CONCURRENCY))

    async with _session_factory() as session:
        cfg = await session.get(MCSConfig, mcs_id)
        if cfg is None:
            logger.warning("Readiness sweep skipped: MCS %s no longer exists", mcs_id)
            return
        mcs_url = cfg.mcs_url
        try:
            auth_headers = await resolve_mcs_auth_headers(
                session, mcs_id=mcs_id, mcs_url=mcs_url, mcs_auth_type=cfg.auth_type.value, owner_label=f"MCS {mcs_id}"
            )
        except Exception as exc:
            logger.warning("Readiness sweep could not authenticate to MCS %s: %s", mcs_id, exc)
            auth_headers = {}

        async def one(measure_id: str, version: str) -> None:
            async with semaphore:
                try:
                    verdict = await check_measure_readiness(
                        mcs_url,
                        measure_id,
                        auth_headers=auth_headers,
                        timeout=float(settings.READINESS_TIMEOUT_SECONDS),
                    )
                except Exception as exc:
                    # A raising check must not leave the row spinning until restart.
                    logger.exception("Readiness check raised for %s", measure_id)
                    verdict = ReadinessVerdict(state=ReadinessState.unknown, error=f"Check failed: {exc}")
                await _store_verdict(session, mcs_id, measure_id, version, verdict)

        await asyncio.gather(*(one(mid, ver) for mid, ver in measures))

    logger.info("Readiness sweep complete", extra={"mcs_id": mcs_id, "measures": len(measures)})
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `cd backend && python3 -m pytest tests/test_services_measure_readiness.py -v`
Expected: PASS.

- [ ] **Step 5: Run lint and the full unit suite**

Run: `cd backend && ruff check app/ tests/ && ruff format --check app/ tests/ && python3 -m pytest tests/ --ignore=tests/integration -q`
Expected: clean.

- [ ] **Step 6: Commit**

```bash
git add backend/app/services/measure_readiness.py backend/tests/test_services_measure_readiness.py
git commit -m "feat: background readiness sweep with capped concurrency (#434)"
```

---

### Task 5: API — decorate `GET /measures`, add the refresh endpoint

**Files:**
- Modify: `backend/app/routes/measures.py:80-135` (the `get_measures` handler) and the router tail
- Test: `backend/tests/test_routes_measures.py`

**Interfaces:**
- Consumes: `claim_unchecked`, `mark_all_checking`, `run_sweep` (Task 4); `MeasureReadiness`, `ReadinessState` (Task 1).
- Produces:
  - `GET /measures` — each item in `measures[]` gains `readiness: {state, checked_at, missing_libraries, missing_valuesets, error}`.
  - `POST /measures/readiness/refresh` — `202 {"status": "accepted", "measures": <int>}`.

- [ ] **Step 1: Write the failing tests**

Append to `backend/tests/test_routes_measures.py`:

```python
async def test_get_measures_reports_unknown_and_kicks_a_sweep(client, active_mcs):
    """First view: no cached rows, so every measure reads `checking`."""
    bundle = {
        "resourceType": "Bundle",
        "entry": [
            {"resource": {"resourceType": "Measure", "id": "CMS122", "version": "0.5.000", "status": "active"}}
        ],
    }
    with patch.object(measures_module, "list_measures", AsyncMock(return_value=bundle)):
        with patch.object(measures_module.asyncio, "create_task", lambda coro: coro.close()):
            resp = await client.get("/measures")

    assert resp.status_code == 200
    measure = resp.json()["measures"][0]
    assert measure["readiness"]["state"] == "checking"


async def test_get_measures_serves_a_cached_verdict(client, active_mcs, test_session):
    from app.models.measure_readiness import MeasureReadiness, ReadinessState

    test_session.add(
        MeasureReadiness(
            mcs_id=active_mcs.id,
            measure_id="CMS122",
            measure_version="0.5.000",
            state=ReadinessState.not_ready,
            missing_libraries=["Status 1.15.000"],
            missing_valuesets=["http://vs/a"],
            error="Could not load source for library Status, version 1.15.000, namespace uri null.",
        )
    )
    await test_session.commit()

    bundle = {
        "resourceType": "Bundle",
        "entry": [
            {"resource": {"resourceType": "Measure", "id": "CMS122", "version": "0.5.000", "status": "active"}}
        ],
    }
    with patch.object(measures_module, "list_measures", AsyncMock(return_value=bundle)):
        resp = await client.get("/measures")

    readiness = resp.json()["measures"][0]["readiness"]
    assert readiness["state"] == "not_ready"
    assert readiness["missing_libraries"] == ["Status 1.15.000"]
    assert "Could not load source for library Status" in readiness["error"]


async def test_get_measures_does_not_serve_another_connections_verdict(client, active_mcs, test_session):
    """Verdicts are per-MCS. A row for a different connection must not leak."""
    from app.models.measure_readiness import MeasureReadiness, ReadinessState

    test_session.add(
        MeasureReadiness(
            mcs_id=active_mcs.id + 999,
            measure_id="CMS122",
            measure_version="0.5.000",
            state=ReadinessState.ready,
        )
    )
    await test_session.commit()

    bundle = {
        "resourceType": "Bundle",
        "entry": [
            {"resource": {"resourceType": "Measure", "id": "CMS122", "version": "0.5.000", "status": "active"}}
        ],
    }
    with patch.object(measures_module, "list_measures", AsyncMock(return_value=bundle)):
        with patch.object(measures_module.asyncio, "create_task", lambda coro: coro.close()):
            resp = await client.get("/measures")

    assert resp.json()["measures"][0]["readiness"]["state"] != "ready"


async def test_refresh_accepts_and_marks_everything_checking(client, active_mcs, test_session):
    from sqlalchemy import select

    from app.models.measure_readiness import MeasureReadiness, ReadinessState

    bundle = {
        "resourceType": "Bundle",
        "entry": [
            {"resource": {"resourceType": "Measure", "id": "CMS122", "version": "0.5.000", "status": "active"}},
            {"resource": {"resourceType": "Measure", "id": "CMS124", "version": "1.0.000", "status": "active"}},
        ],
    }
    with patch.object(measures_module, "list_measures", AsyncMock(return_value=bundle)):
        with patch.object(measures_module.asyncio, "create_task", lambda coro: coro.close()):
            resp = await client.post("/measures/readiness/refresh")

    assert resp.status_code == 202
    assert resp.json()["measures"] == 2
    rows = (await test_session.execute(select(MeasureReadiness))).scalars().all()
    assert {r.state for r in rows} == {ReadinessState.checking}


async def test_measure_with_no_version_gets_a_readiness_object(client, active_mcs):
    """A Measure without `version` must still render, not 500."""
    bundle = {
        "resourceType": "Bundle",
        "entry": [{"resource": {"resourceType": "Measure", "id": "NoVersion", "status": "active"}}],
    }
    with patch.object(measures_module, "list_measures", AsyncMock(return_value=bundle)):
        with patch.object(measures_module.asyncio, "create_task", lambda coro: coro.close()):
            resp = await client.get("/measures")

    assert resp.status_code == 200
    assert resp.json()["measures"][0]["readiness"]["state"] == "checking"
```

- [ ] **Step 2: Run them to verify they fail**

Run: `cd backend && python3 -m pytest tests/test_routes_measures.py -k "readiness or refresh or no_version" -v`
Expected: FAIL — `KeyError: 'readiness'` on the GET tests, `404` on the refresh test.

- [ ] **Step 3: Decorate `GET /measures`**

In `backend/app/routes/measures.py`, add to the imports:

```python
import asyncio

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db import async_session
from app.dependencies import get_session
from app.models.measure_readiness import MeasureReadiness, ReadinessState
from app.services.measure_readiness import claim_unchecked, mark_all_checking, run_sweep
```

Add this helper above `get_measures`:

```python
def _readiness_payload(row: MeasureReadiness | None) -> dict:
    """Render one verdict for the API. A missing row is `unknown`, never absent.

    Always emitting the object keeps the frontend from having to distinguish
    "not checked" from "field not implemented".
    """
    if row is None:
        return {
            "state": ReadinessState.unknown.value,
            "checked_at": None,
            "missing_libraries": [],
            "missing_valuesets": [],
            "error": None,
        }
    return {
        "state": row.state.value,
        "checked_at": row.checked_at.isoformat() if row.checked_at else None,
        "missing_libraries": row.missing_libraries or [],
        "missing_valuesets": row.missing_valuesets or [],
        "error": row.error,
    }
```

Change the `get_measures` signature to take a session, and replace the `return {...}` block. The measure-collecting loop is unchanged except that each appended dict now also records its version for the lookup:

```python
@router.get("")
async def get_measures(
    mcs: ConnectionContext = Depends(get_active_mcs),
    session: AsyncSession = Depends(get_session),
) -> dict:
```

After the existing `for entry in bundle.get("entry", []):` loop finishes and before the `return`, insert:

```python
        # Readiness is a left join from the cache — never a blocking call. A
        # measure with no cached verdict is claimed as `checking` here,
        # synchronously, so a page refresh during a sweep cannot queue a second.
        keys = [(m["id"], m.get("version") or "") for m in measures if m.get("id")]
        rows = (
            await session.execute(select(MeasureReadiness).where(MeasureReadiness.mcs_id == mcs.id))
        ).scalars().all()
        by_key = {(r.measure_id, r.measure_version): r for r in rows}

        claimed = await claim_unchecked(session, mcs.id, keys)
        if claimed:
            asyncio.create_task(run_sweep(mcs.id, claimed))

        for measure in measures:
            key = (measure.get("id"), measure.get("version") or "")
            row = by_key.get(key)
            if row is None and key in set(claimed):
                measure["readiness"] = {
                    "state": ReadinessState.checking.value,
                    "checked_at": None,
                    "missing_libraries": [],
                    "missing_valuesets": [],
                    "error": None,
                }
            else:
                measure["readiness"] = _readiness_payload(row)
```

- [ ] **Step 4: Add the refresh endpoint**

Append to `backend/app/routes/measures.py`, after `delete_measure_route`:

```python
@router.post("/readiness/refresh", status_code=202)
async def refresh_readiness(
    mcs: ConnectionContext = Depends(get_active_mcs),
    session: AsyncSession = Depends(get_session),
) -> dict:
    """Discard cached verdicts for the active MCS and re-check every measure.

    Returns 202 immediately — the sweep runs detached, and the caller learns the
    outcome by polling `GET /measures`. Read-only against the MCS, so it is
    allowed even when the connection is marked read-only.
    """
    auth_headers = await _resolve_auth(mcs)
    try:
        bundle = await list_measures(
            mcs.mcs_url,
            auth_headers=auth_headers,
            timeout=float(mcs.request_timeout_seconds),
        )
    except Exception as exc:
        logger.exception("Cannot list measures for readiness refresh", extra={"mcs_id": mcs.id})
        raise HTTPException(
            status_code=502,
            detail={
                "resourceType": "OperationOutcome",
                "issue": [
                    {
                        "severity": "error",
                        "code": "exception",
                        "diagnostics": f"Cannot reach measure engine '{mcs.name}': {sanitize_error(exc)}",
                    }
                ],
            },
        ) from exc

    keys = [
        (r.get("id"), r.get("version") or "")
        for entry in bundle.get("entry", [])
        if (r := entry.get("resource", {})).get("resourceType") == "Measure" and r.get("id")
    ]
    await mark_all_checking(session, mcs.id, keys)
    asyncio.create_task(run_sweep(mcs.id, keys))
    return {"status": "accepted", "measures": len(keys)}
```

- [ ] **Step 5: Run the tests to verify they pass**

Run: `cd backend && python3 -m pytest tests/test_routes_measures.py -v`
Expected: PASS — including the pre-existing tests in that file.

- [ ] **Step 6: Commit**

```bash
git add backend/app/routes/measures.py backend/tests/test_routes_measures.py
git commit -m "feat: expose measure readiness on GET /measures and add refresh (#434)"
```

---

### Task 6: Invalidation triggers — upload, delete, MCS URL change

**Files:**
- Modify: `backend/app/routes/measures.py` (the upload and delete handlers)
- Modify: `backend/app/routes/connection_factory.py:109-125` (signature) and `:293` (after `await session.commit()` in the update handler)
- Modify: `backend/app/routes/settings.py:150-164` (the MCS `make_connection_router` call)
- Test: `backend/tests/test_routes_measures.py`, `backend/tests/test_routes_mcs_settings.py`

**Interfaces:**
- Consumes: `invalidate_mcs` (Task 4).
- Produces: `make_connection_router(..., on_url_change: Callable[[AsyncSession, int], Awaitable[None]] | None = None)`.

- [ ] **Step 1: Write the failing tests**

Append to `backend/tests/test_routes_measures.py`:

```python
async def test_uploading_a_bundle_invalidates_every_verdict_for_that_mcs(client, active_mcs, test_session):
    """An uploaded Library can fix measures other than the one uploaded.

    Invalidating only the uploaded measure would leave those stale and red.
    """
    from sqlalchemy import select

    from app.models.measure_readiness import MeasureReadiness, ReadinessState

    for measure_id in ("CMS122", "CMS124"):
        test_session.add(
            MeasureReadiness(
                mcs_id=active_mcs.id, measure_id=measure_id, measure_version="1", state=ReadinessState.not_ready
            )
        )
    await test_session.commit()

    bundle = json.dumps({"resourceType": "Bundle", "type": "transaction", "entry": []}).encode()
    with patch.object(measures_module, "upload_measure_bundle", AsyncMock(return_value={"created": 1})):
        resp = await client.post(
            "/measures/upload", files={"file": ("bundle.json", bundle, "application/json")}
        )

    assert resp.status_code in (200, 201)
    assert (await test_session.execute(select(MeasureReadiness))).scalars().all() == []


async def test_deleting_a_measure_invalidates_verdicts_for_that_mcs(client, active_mcs, test_session):
    from sqlalchemy import select

    from app.models.measure_readiness import MeasureReadiness, ReadinessState

    test_session.add(
        MeasureReadiness(
            mcs_id=active_mcs.id, measure_id="CMS122", measure_version="1", state=ReadinessState.ready
        )
    )
    await test_session.commit()

    with patch.object(measures_module, "delete_measure", AsyncMock(return_value=None)):
        resp = await client.delete("/measures/CMS122")

    assert resp.status_code == 204
    assert (await test_session.execute(select(MeasureReadiness))).scalars().all() == []
```

Append to `backend/tests/test_routes_mcs_settings.py`:

```python
async def test_changing_the_mcs_url_invalidates_its_readiness_verdicts(client, test_session):
    """Same connection id, different server — every cached verdict is now about
    the wrong machine."""
    from sqlalchemy import select

    from app.models.connection_base import AuthType
    from app.models.mcs_config import MCSConfig
    from app.models.measure_readiness import MeasureReadiness, ReadinessState

    cfg = MCSConfig(
        name="Repointed MCS",
        mcs_url="https://before.example.com/fhir",
        auth_type=AuthType.none,
        auth_credentials=None,
        is_active=False,
        is_default=False,
    )
    test_session.add(cfg)
    await test_session.commit()
    await test_session.refresh(cfg)

    test_session.add(
        MeasureReadiness(mcs_id=cfg.id, measure_id="CMS122", measure_version="1", state=ReadinessState.ready)
    )
    await test_session.commit()

    resp = await client.put(
        f"/settings/mcs-connections/{cfg.id}",
        json={"name": "Repointed MCS", "mcs_url": "https://after.example.com/fhir", "auth_type": "none"},
    )
    assert resp.status_code == 200

    rows = (await test_session.execute(select(MeasureReadiness))).scalars().all()
    assert rows == []
```

- [ ] **Step 2: Run them to verify they fail**

Run: `cd backend && python3 -m pytest tests/test_routes_measures.py -k "invalidat" tests/test_routes_mcs_settings.py -k "invalidat" -v`
Expected: FAIL — rows are still present after each operation.

- [ ] **Step 3: Invalidate on upload and delete**

In `backend/app/routes/measures.py`, add `invalidate_mcs` to the import from `app.services.measure_readiness`. Then give both handlers the session dependency and call it after the MCS operation succeeds.

In `upload_measure_bundle_route`, add `session: AsyncSession = Depends(get_session)` to the signature, and immediately before its success `return`:

```python
    # An uploaded bundle can carry a Library that OTHER measures were missing,
    # so the whole connection's verdicts are stale, not just this measure's.
    await invalidate_mcs(session, mcs.id)
```

In `delete_measure_route`, add the same dependency, and after the `await delete_measure(...)` call succeeds:

```python
    await invalidate_mcs(session, mcs.id)
```

- [ ] **Step 4: Add the `on_url_change` hook to the factory**

In `backend/app/routes/connection_factory.py`, add to the imports:

```python
from collections.abc import Awaitable, Callable
```

Add the parameter to `make_connection_router`'s signature (after `audit_logger`):

```python
    on_url_change: Callable[[AsyncSession, int], Awaitable[None]] | None = None,
```

and document it in that function's docstring `Args:` block:

```
        on_url_change: Optional coroutine invoked after an update that changes
            `url_field`. Lets a kind attach cache invalidation without this
            factory knowing what a measure is. CDR passes nothing.
```

In the update handler, capture the URL before the field loop. Immediately before `for field_name in body.model_fields_set | ...`:

```python
        url_before = getattr(cfg, url_field, None)
```

and after `await session.refresh(cfg)`:

```python
        # Same connection id, different server: anything cached about the old
        # host is now about the wrong machine.
        if on_url_change is not None and getattr(cfg, url_field, None) != url_before:
            await on_url_change(session, cfg.id)
```

- [ ] **Step 5: Pass the hook for the MCS router only**

In `backend/app/routes/settings.py`, add the import:

```python
from app.services.measure_readiness import invalidate_mcs
```

and add to the MCS `make_connection_router(...)` call (the one with `prefix="/mcs-connections"`), after `audit_logger=logger`:

```python
        # Repointing an MCS at a different server invalidates every readiness
        # verdict cached for it (#434). The CDR router passes nothing.
        on_url_change=invalidate_mcs,
```

- [ ] **Step 6: Run the tests to verify they pass**

Run: `cd backend && python3 -m pytest tests/test_routes_measures.py tests/test_routes_mcs_settings.py -v`
Expected: PASS.

- [ ] **Step 7: Run lint and the full unit suite**

Run: `cd backend && ruff check app/ tests/ && ruff format --check app/ tests/ && python3 -m pytest tests/ --ignore=tests/integration -q`
Expected: clean. Pay attention to `test_routes_settings.py` and `test_dependencies.py` — the factory signature changed.

- [ ] **Step 8: Commit**

```bash
git add backend/app/routes/measures.py backend/app/routes/connection_factory.py \
        backend/app/routes/settings.py backend/tests/test_routes_measures.py \
        backend/tests/test_routes_mcs_settings.py
git commit -m "feat: invalidate readiness on upload, delete, and MCS repoint (#434)"
```

---

### Task 7: Frontend — the readiness column and its detail row

**Files:**
- Modify: `frontend/src/pages/MeasuresPage.js`
- Modify: `frontend/src/pages/MeasuresPage.module.css`
- Test: `frontend/src/pages/MeasuresPage.test.js`

**Interfaces:**
- Consumes: the `readiness` object on each measure from `GET /measures` (Task 5).
- Produces: a `ReadinessBadge` component and an `expandedId` piece of page state.

**Naming caution:** the table already has a **Status** column rendering the FHIR `Measure.status` (`active`/`draft`/`retired`) via the existing `StatusBadge`. The new column is **Readiness** and must not reuse that name or that component — they answer completely different questions.

- [ ] **Step 1: Write the failing tests**

Append to `frontend/src/pages/MeasuresPage.test.js`, following the existing render/mock helpers in that file:

```javascript
const measureWith = (readiness, overrides = {}) => ({
  id: 'CMS122FHIRDiabetesAssessGreaterThan9Percent',
  name: 'DiabetesAssess',
  title: 'Diabetes: Hemoglobin A1c Poor Control',
  version: '0.5.000',
  status: 'active',
  readiness,
  ...overrides,
});

const READY = { state: 'ready', checked_at: '2026-09-10T18:00:00Z', missing_libraries: [], missing_valuesets: [], error: null };
const NOT_READY = {
  state: 'not_ready',
  checked_at: '2026-09-10T18:00:00Z',
  missing_libraries: ['Status 1.15.000'],
  missing_valuesets: ['http://cts.nlm.nih.gov/fhir/ValueSet/2.16.840.1.113883.3.464.1003.1003'],
  error: 'Could not load source for library Status, version 1.15.000, namespace uri null.',
};
const UNKNOWN = { state: 'unknown', checked_at: null, missing_libraries: [], missing_valuesets: [], error: null };
const CHECKING = { state: 'checking', checked_at: null, missing_libraries: [], missing_valuesets: [], error: null };

test('a ready measure shows the ready badge', async () => {
  renderMeasuresPage([measureWith(READY)]);
  expect(await screen.findByText(/^Ready$/)).toBeInTheDocument();
});

test('a not-ready measure shows the not-ready badge', async () => {
  renderMeasuresPage([measureWith(NOT_READY)]);
  expect(await screen.findByText(/Not ready/i)).toBeInTheDocument();
});

test('an unchecked measure shows Not checked, not a failure', async () => {
  renderMeasuresPage([measureWith(UNKNOWN)]);
  expect(await screen.findByText(/Not checked/i)).toBeInTheDocument();
  expect(screen.queryByText(/Not ready/i)).not.toBeInTheDocument();
});

test('a measure being checked shows Checking', async () => {
  renderMeasuresPage([measureWith(CHECKING)]);
  expect(await screen.findByText(/Checking/i)).toBeInTheDocument();
});

test('expanding a not-ready measure lists what is missing', async () => {
  renderMeasuresPage([measureWith(NOT_READY)]);
  const badge = await screen.findByRole('button', { name: /readiness details/i });
  await userEvent.click(badge);

  expect(await screen.findByText(/Status 1.15.000/)).toBeInTheDocument();
  expect(screen.getByText(/2\.16\.840\.1\.113883\.3\.464\.1003\.1003/)).toBeInTheDocument();
  expect(screen.getByText(/Could not load source for library Status/)).toBeInTheDocument();
});

test('the detail names only the first unresolvable library', async () => {
  /* The CQL engine reports one include at a time; the copy must not imply the
     list is exhaustive, or a user fixes one library and is surprised twice. */
  renderMeasuresPage([measureWith(NOT_READY)]);
  await userEvent.click(await screen.findByRole('button', { name: /readiness details/i }));
  expect(screen.getByText(/may reveal another/i)).toBeInTheDocument();
});

test('a ready measure exposes no details toggle', async () => {
  renderMeasuresPage([measureWith(READY)]);
  await screen.findByText(/^Ready$/);
  expect(screen.queryByRole('button', { name: /readiness details/i })).not.toBeInTheDocument();
});
```

If `renderMeasuresPage(measures)` does not already exist in that file, add it next to the existing setup, mirroring however the file currently mocks `GET /measures`:

```javascript
function renderMeasuresPage(measures) {
  global.fetch = jest.fn((url) => {
    if (String(url).includes('/measures')) {
      return Promise.resolve({
        ok: true,
        status: 200,
        json: () => Promise.resolve({ measures, total: measures.length, mcs: { id: 1, name: 'Local Measure Engine' } }),
      });
    }
    return Promise.resolve({ ok: true, status: 200, json: () => Promise.resolve({}) });
  });
  return render(<MeasuresPage />, { wrapper: TestProviders });
}
```

- [ ] **Step 2: Run them to verify they fail**

Run: `cd frontend && npx jest src/pages/MeasuresPage.test.js -t readiness`
Expected: FAIL — no "Ready"/"Not ready" text is rendered.

- [ ] **Step 3: Add the badge component**

In `frontend/src/pages/MeasuresPage.js`, add below the existing `StatusBadge`:

```javascript
// Readiness answers "can the active MCS actually evaluate this measure?".
// Deliberately separate from StatusBadge above, which renders the FHIR
// Measure.status (active/draft/retired) — a different question entirely.
const READINESS_LABELS = {
  ready: 'Ready',
  not_ready: 'Not ready',
  checking: 'Checking…',
  unknown: 'Not checked',
};

function ReadinessBadge({ readiness, expanded, onToggle }) {
  const state = readiness?.state || 'unknown';
  const label = READINESS_LABELS[state] || READINESS_LABELS.unknown;

  if (state !== 'not_ready') {
    const cls =
      state === 'ready' ? styles.badgeOk : state === 'checking' ? styles.badgeDraft : styles.badge;
    return <span className={`${styles.badge} ${cls}`}>{label}</span>;
  }

  return (
    <button
      type="button"
      className={`${styles.badge} ${styles.badgeBad} ${styles.readinessToggle}`}
      aria-expanded={expanded}
      aria-label={`Readiness details for this measure`}
      onClick={onToggle}
    >
      {label}
    </button>
  );
}

function ReadinessDetail({ readiness }) {
  return (
    <div className={styles.readinessDetail}>
      {readiness.error && <p className={styles.readinessError}>{readiness.error}</p>}
      {readiness.missing_libraries?.length > 0 && (
        <>
          <h4>Missing libraries</h4>
          <ul>
            {readiness.missing_libraries.map(lib => <li key={lib} className={styles.mono}>{lib}</li>)}
          </ul>
          <p className={styles.readinessNote}>
            The measure server reports only the first library it cannot load, so
            loading this one may reveal another.
          </p>
        </>
      )}
      {readiness.missing_valuesets?.length > 0 && (
        <>
          <h4>Missing value sets ({readiness.missing_valuesets.length})</h4>
          <ul>
            {readiness.missing_valuesets.map(vs => <li key={vs} className={styles.mono}>{vs}</li>)}
          </ul>
        </>
      )}
    </div>
  );
}
```

- [ ] **Step 4: Render the column**

Add page state next to the existing `useState` calls:

```javascript
  const [expandedId, setExpandedId] = useState(null);
```

Add a header cell after the existing `Status` header, in **both** the loading skeleton table and the real table:

```javascript
                <th style={{ width: 120 }}>Readiness</th>
```

In the skeleton's `{[90, 200, 60, 80, 100].map(...)}`, add one more width so the column counts match: `{[90, 200, 60, 80, 110, 100].map(...)}`.

In the real table body, replace the single `<tr>` per measure with a fragment carrying an optional detail row, and add the readiness cell after the Status cell:

```javascript
                visible.map((measure, i) => {
                  const key = measure.id || i;
                  const readiness = measure.readiness;
                  const isExpanded = expandedId === key;
                  return (
                    <React.Fragment key={key}>
                      <tr className={styles.row}>
                        <td data-label="ID"><span className={styles.mono}>{extractCmsId(measure.id) || measure.id || '--'}</span></td>
                        <td data-label="Measure" className={`${styles.measureName} ${styles.measureCell}`}>{getMeasureDisplayName(measure)}</td>
                        <td data-label="Version" className={styles.mono} style={{ color: 'var(--text-muted)' }}>{getMeasureVersion(measure)}</td>
                        <td data-label="Status"><StatusBadge status={getMeasureStatus(measure)} /></td>
                        <td data-label="Readiness">
                          <ReadinessBadge
                            readiness={readiness}
                            expanded={isExpanded}
                            onToggle={() => setExpandedId(isExpanded ? null : key)}
                          />
                        </td>
                        <td data-label="Actions">
                          <div className={styles.actionGroup}>
                            <Link to={`/jobs?newCalc=${encodeURIComponent(measure.id || '')}`} className={styles.calcBtn}>Calculate</Link>
                            <KebabMenu items={[
                              { divider: true },
                              {
                                label: 'Delete permanently',
                                icon: <TrashIcon />,
                                tone: 'destructive',
                                disabled: !measure.id || mcs.isReadOnly,
                                title: mcs.isReadOnly ? `${mcs.name || 'This connection'} is read-only` : undefined,
                                onClick: () => confirmDelete(measure),
                              },
                            ]} />
                          </div>
                        </td>
                      </tr>
                      {isExpanded && readiness && (
                        <tr className={styles.detailRow}>
                          <td colSpan={6}><ReadinessDetail readiness={readiness} /></td>
                        </tr>
                      )}
                    </React.Fragment>
                  );
                })
```

Update the empty-state row's `colSpan={5}` to `colSpan={6}`.

- [ ] **Step 5: Add the styles**

Append to `frontend/src/pages/MeasuresPage.module.css`, matching the existing `.badge` conventions in that file:

```css
.badgeBad {
  background: var(--danger-bg, #fdeced);
  color: var(--danger-fg, #a31515);
}

.readinessToggle {
  border: none;
  cursor: pointer;
  font: inherit;
}

.detailRow > td {
  background: var(--surface-subtle, #f7f7f8);
}

.readinessDetail h4 {
  margin: 0.75rem 0 0.25rem;
  font-size: 0.8125rem;
}

.readinessDetail ul {
  margin: 0;
  padding-left: 1.25rem;
  font-size: 0.8125rem;
}

.readinessError {
  margin: 0;
  color: var(--danger-fg, #a31515);
  font-size: 0.8125rem;
}

.readinessNote {
  margin: 0.375rem 0 0;
  color: var(--text-muted);
  font-size: 0.75rem;
}
```

- [ ] **Step 6: Run the tests to verify they pass**

Run: `cd frontend && npx jest src/pages/MeasuresPage.test.js`
Expected: PASS — new tests plus every pre-existing test in the file.

- [ ] **Step 7: Commit**

```bash
git add frontend/src/pages/MeasuresPage.js frontend/src/pages/MeasuresPage.module.css \
        frontend/src/pages/MeasuresPage.test.js
git commit -m "feat: readiness column and detail row on the Measures page (#434)"
```

---

### Task 8: Frontend — Re-check button and polling while checking

**Files:**
- Modify: `frontend/src/pages/MeasuresPage.js`
- Test: `frontend/src/pages/MeasuresPage.test.js`

**Interfaces:**
- Consumes: `POST /measures/readiness/refresh` (Task 5); `ReadinessBadge` (Task 7).
- Produces: nothing later tasks depend on.

- [ ] **Step 1: Write the failing tests**

Append to `frontend/src/pages/MeasuresPage.test.js`:

```javascript
test('the re-check button posts to the refresh endpoint', async () => {
  renderMeasuresPage([measureWith(NOT_READY)]);
  await screen.findByText(/Not ready/i);

  await userEvent.click(screen.getByRole('button', { name: /re-check/i }));

  expect(global.fetch).toHaveBeenCalledWith(
    expect.stringContaining('/measures/readiness/refresh'),
    expect.objectContaining({ method: 'POST' }),
  );
});

test('the page polls while any measure is still checking', async () => {
  jest.useFakeTimers();
  try {
    renderMeasuresPage([measureWith(CHECKING)]);
    await screen.findByText(/Checking/i);
    const callsAfterLoad = global.fetch.mock.calls.length;

    await act(async () => {
      jest.advanceTimersByTime(5000);
    });

    expect(global.fetch.mock.calls.length).toBeGreaterThan(callsAfterLoad);
  } finally {
    jest.useRealTimers();
  }
});

test('the page stops polling once nothing is checking', async () => {
  jest.useFakeTimers();
  try {
    renderMeasuresPage([measureWith(READY)]);
    await screen.findByText(/^Ready$/);
    const callsAfterLoad = global.fetch.mock.calls.length;

    await act(async () => {
      jest.advanceTimersByTime(15000);
    });

    expect(global.fetch.mock.calls.length).toBe(callsAfterLoad);
  } finally {
    jest.useRealTimers();
  }
});
```

Ensure `act` is imported from `@testing-library/react` at the top of the file if it is not already.

- [ ] **Step 2: Run them to verify they fail**

Run: `cd frontend && npx jest src/pages/MeasuresPage.test.js -t "re-check\|polls\|stops polling"`
Expected: FAIL — no Re-check button; no polling.

- [ ] **Step 3: Add the polling effect and the re-check handler**

In `frontend/src/pages/MeasuresPage.js`, add state next to the others:

```javascript
  const [rechecking, setRechecking] = useState(false);
```

Add below the existing `useEffect(() => { loadMeasures(); }, [loadMeasures, mcs.id]);`:

```javascript
  // Poll only while a sweep is actually running. A verdict is cached and
  // event-invalidated, so there is nothing to poll for once every row has
  // settled — an unconditional interval would be steady load for no news.
  const anyChecking = measures.some(m => m.readiness?.state === 'checking');
  useEffect(() => {
    if (!anyChecking) return undefined;
    const timer = setInterval(() => { loadMeasures({ quiet: true }); }, 5000);
    return () => clearInterval(timer);
  }, [anyChecking, loadMeasures]);
```

`loadMeasures` must accept `{ quiet }` and skip `setLoading(true)` when it is set — otherwise every poll flashes the skeleton table over content the user is reading. Add the parameter to its existing definition:

```javascript
  const loadMeasures = useCallback(async ({ quiet = false } = {}) => {
    if (!quiet) setLoading(true);
    // ...existing body unchanged...
  }, [/* existing deps */]);
```

Add the handler beside `handleUploadClick`:

```javascript
  const handleRecheck = async () => {
    setRechecking(true);
    try {
      const resp = await fetch(`${API_BASE}/measures/readiness/refresh`, { method: 'POST' });
      if (!resp.ok) throw new Error('Refresh failed');
      await loadMeasures({ quiet: true });
    } catch (err) {
      toast.error(`Could not start readiness check: ${err.message}`);
    } finally {
      setRechecking(false);
    }
  };
```

Use whatever the file already uses to build API URLs in place of `API_BASE` — match the existing `loadMeasures` call, do not introduce a second convention.

Add the button in `styles.headerActions`, before the Upload button:

```javascript
          <button
            className={styles.btnSecondary}
            onClick={handleRecheck}
            disabled={rechecking}
            aria-busy={rechecking}
          >
            {rechecking ? 'Checking…' : 'Re-check readiness'}
          </button>
```

If `styles.btnSecondary` does not exist in `MeasuresPage.module.css`, use the class the page already uses for secondary actions (e.g. `styles.retryBtn`) rather than adding a new one.

- [ ] **Step 4: Run the tests to verify they pass**

Run: `cd frontend && npx jest src/pages/MeasuresPage.test.js`
Expected: PASS.

- [ ] **Step 5: Verify the production build**

Run: `cd frontend && CI=true npm test -- --watchAll=false && npm run build`
Expected: all frontend tests pass; build succeeds.

- [ ] **Step 6: Commit**

```bash
git add frontend/src/pages/MeasuresPage.js frontend/src/pages/MeasuresPage.test.js \
        frontend/src/pages/MeasuresPage.module.css
git commit -m "feat: poll while readiness checks run and add a re-check control (#434)"
```

---

### Task 9: Integration test against the local prebaked stack

**Files:**
- Create: `backend/tests/integration/test_measure_readiness.py`

**Interfaces:**
- Consumes: everything above, end to end.
- Produces: nothing.

**Why this task exists:** every test so far mocks the MCS. Two things can only be confirmed against a real HAPI: that `ValueSet?url=a,b,c` actually performs an OR match (the whole presence check rests on it), and that all 9 seeded measures genuinely resolve to `ready`.

- [ ] **Step 1: Write the test**

Create `backend/tests/integration/test_measure_readiness.py`:

```python
"""Measure readiness against a real HAPI measure server.

Everything else about this feature is tested against mocks. These two facts
cannot be: that HAPI treats a comma-separated `url` search as an OR match, and
that the seeded measures actually pass the check end to end.
"""

import os

import httpx
import pytest

pytestmark = [pytest.mark.asyncio, pytest.mark.integration]

MCS_URL = os.getenv("MEASURE_ENGINE_URL", "http://localhost:8080/fhir")


async def test_comma_separated_valueset_url_search_is_an_or_match():
    """The presence check batches canonicals into one `url=a,b,c` query.

    If HAPI ever treated that as a literal string match, every batched valueset
    would read as missing and every measure would go red. Assert the behaviour
    rather than trusting it.
    """
    async with httpx.AsyncClient(timeout=60) as client:
        listing = await client.get(f"{MCS_URL}/ValueSet?_elements=url&_count=3")
        listing.raise_for_status()
        urls = [e["resource"]["url"] for e in listing.json().get("entry", []) if e["resource"].get("url")]

    if len(urls) < 2:
        pytest.skip("Measure server has fewer than 2 ValueSets to test an OR match with")

    from app.services.measure_readiness import find_missing_valuesets

    missing = await find_missing_valuesets(MCS_URL, urls, auth_headers={}, timeout=60.0, chunk_size=10)
    assert missing == [], f"Expected all {len(urls)} known valuesets to be found, missing: {missing}"


async def test_a_canonical_that_does_not_exist_is_reported_missing():
    """The negative control. Without it, a check that always returns [] passes."""
    from app.services.measure_readiness import find_missing_valuesets

    bogus = "http://example.invalid/ValueSet/definitely-not-here"
    missing = await find_missing_valuesets(MCS_URL, [bogus], auth_headers={}, timeout=60.0)
    assert missing == [bogus]


async def test_every_seeded_measure_is_ready():
    """The local prebaked stack ships complete content; all of it must pass.

    Slow by design — $data-requirements measured at 6-11s per measure, and
    hapi-fhir-measure runs emulated on arm64 hosts.
    """
    from app.models.measure_readiness import ReadinessState
    from app.services.measure_readiness import check_measure_readiness

    async with httpx.AsyncClient(timeout=60) as client:
        listing = await client.get(f"{MCS_URL}/Measure?_elements=id&_count=50")
        listing.raise_for_status()
        measure_ids = [e["resource"]["id"] for e in listing.json().get("entry", [])]

    assert measure_ids, "No measures on the measure server — is the prebaked stack up?"

    failures = []
    for measure_id in measure_ids:
        verdict = await check_measure_readiness(MCS_URL, measure_id, auth_headers={}, timeout=120.0)
        if verdict.state is not ReadinessState.ready:
            failures.append((measure_id, verdict.state.value, verdict.error))

    assert not failures, f"Measures not ready on a complete server: {failures}"
```

- [ ] **Step 2: Run it against the local stack**

The CI-equivalent suite uses `--ignore` flags and will **silently skip** this new file, so it must be run explicitly (CLAUDE.md pre-push checklist, step 5):

Run: `USE_PREBAKED=1 ./scripts/run-integration-tests.sh tests/integration/test_measure_readiness.py`
Expected: PASS. Budget several minutes — 9 measures at 6–11s each, slower still on an emulated `hapi-fhir-measure`.

If `test_comma_separated_valueset_url_search_is_an_or_match` fails, **stop**: the batching strategy in `find_missing_valuesets` is wrong, and Task 3 needs one request per canonical instead of chunked OR queries. That is a real design change, not a test tweak — raise it rather than working around it.

- [ ] **Step 3: Commit**

```bash
git add backend/tests/integration/test_measure_readiness.py
git commit -m "test: verify readiness against a real HAPI measure server (#434)"
```

---

## Final verification before pushing

CLAUDE.md's pre-push checklist is mandatory and admits no exceptions for "small" changes. Run every step and do not push until all pass.

- [ ] **Lint:** `cd backend && ruff check app/ tests/ && ruff format --check app/ tests/`
- [ ] **Unit:** `cd backend && python3 -m pytest tests/ --ignore=tests/integration -v`
- [ ] **Coverage floor (≥70%):** `cd backend && python3 -m pytest tests/ --ignore=tests/integration --cov=app --cov-report=term-missing`
- [ ] **Frontend:** `cd frontend && CI=true npm test -- --watchAll=false && npm run build`
- [ ] **CI-equivalent integration** — the `USE_PREBAKED=1 REQUIRE_PREBAKED=1` prefix is not optional:

```bash
USE_PREBAKED=1 REQUIRE_PREBAKED=1 ./scripts/run-integration-tests.sh \
  --ignore=tests/integration/test_golden_measures.py \
  --ignore=tests/integration/test_connectathon_measures.py \
  --ignore=tests/integration/test_full_workflow.py \
  --ignore=tests/integration/test_groups_dropdown.py \
  --ignore=tests/integration/test_full_jobs_pipeline.py \
  --ignore=tests/integration/test_factory_reset.py
```

- [ ] **The new integration file, explicitly** (the flags above skip it):

```bash
USE_PREBAKED=1 ./scripts/run-integration-tests.sh tests/integration/test_measure_readiness.py
```

- [ ] **End-to-end smoke on a local stack.** This change does not touch `fhir_client.py`, `validation.py`, or `orchestrator.py`, so the full smoke recipe is not triggered — but the feature is a UI change driven by live MCS calls, so confirm it by eye: bring the stack up, open `http://localhost:3001/measures`, watch every row go from `Checking…` to `Ready`, then click **Re-check readiness** and watch them cycle again. **Do not run `cp .env.example .env`** — it blanks a working `CDR_FERNET_KEY`.

- [ ] **Confirm the not-ready path against a real incomplete server.** The whole feature exists for this case, and no local test covers it: point an MCS connection at a server missing content (the connectathon server that produced job 7 is the known instance) and confirm CMS122 renders **Not ready**, naming `Status 1.15.000` in the expanded detail.

## Self-review notes

Checked against the spec:

- Every spec section maps to a task. The four states → Task 3; storage → Task 1; sweep, concurrency cap, dedicated timeout → Task 4; API shape → Task 5; triggers → Task 6; UI → Tasks 7 and 8; the testing section → distributed, with the integration items in Task 9.
- **Three deviations, all recorded** in the section above: no `_run_schema_migrations` entry needed, activation is not an invalidation trigger, and upload invalidates the whole connection rather than one measure.
- Naming is consistent across tasks: `check_measure_readiness`, `find_missing_valuesets`, `extract_valueset_canonicals`, `extract_missing_libraries`, `claim_unchecked`, `mark_all_checking`, `invalidate_mcs`, `run_sweep`, `reclaim_stranded_checks`, `ReadinessVerdict`, `ReadinessState`, `MeasureReadiness`.
- **One assumption carries real risk and is tested rather than assumed:** that `ValueSet?url=a,b,c` is an OR match in HAPI. Task 9 asserts it with a negative control, and says explicitly what to do if it fails.
