# Groups `$evaluate` Page Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Ship an admin-gated, architecturally-independent Groups page that lists CQL-evaluatable Groups on the connected CDR and invokes `Group/<id>/$evaluate`, displaying resolved members in an accordion.

**Architecture:** New FastAPI router `/api/groups` + new React page `/groups` with no imports from the Measure pipeline. Two new helpers in `fhir_client.py` (list + evaluate-and-resolve). New admin toggle `groups_enabled` follows the existing `validation_enabled` / `comparison_enabled` pattern. Disabled-state redirect to `/measures`. Ephemeral results, synchronous evaluation.

**Tech Stack:** Python 3.10+, FastAPI, SQLAlchemy async, httpx; React 18, plain JS (no TS), CSS Modules, react-router-dom v6, pytest, Jest + RTL.

**Spec:** `docs/superpowers/specs/2026-05-17-groups-evaluate-design.md`
**Issue:** [Bellese/Lenny#322](https://github.com/Bellese/Lenny/issues/322)

---

## File Structure

### Backend — create
- `backend/app/routes/groups.py` — new router, prefix `/api/groups`
- `backend/tests/test_routes_groups.py` — route unit tests
- `backend/tests/test_fhir_client_groups.py` — service helper tests
- `backend/tests/test_groups_independence.py` — AST-based isolation test

### Backend — modify
- `backend/app/services/fhir_client.py` — add `list_groups_with_expression`, `evaluate_group_and_resolve_members`
- `backend/app/routes/settings.py` — add `groups_enabled` to admin defaults, GET, PUT, and PATCH model
- `backend/app/main.py` — register the new router

### Frontend — create
- `frontend/src/pages/GroupsPage.js`
- `frontend/src/pages/GroupsPage.module.css`
- `frontend/src/pages/GroupsPage.test.js`

### Frontend — modify
- `frontend/src/api/client.js` — add `getEvaluatableGroups`, `evaluateGroup`
- `frontend/src/App.js` — add nav item, route, feature flag, keyboard shortcut
- `frontend/src/pages/SettingsPage.js` — add Groups toggle row + handler

---

## Task 1: Backend — Add `groups_enabled` admin setting

**Files:**
- Modify: `backend/app/routes/settings.py:221-269`
- Test: `backend/tests/test_settings_admin.py` (existing file; add to it if present, otherwise create)

- [ ] **Step 1: Locate the existing admin settings tests**

Run: `find /Users/bill/dev/bellese/lenny-feature-cdr-chunked-push/backend/tests -name "*settings*"`
If `test_settings_admin.py` exists, add tests there. Otherwise create it.

- [ ] **Step 2: Write a failing test for `groups_enabled` default + roundtrip**

Add to `backend/tests/test_settings_admin.py` (create the file if needed; mirror the existing settings-test patterns):

```python
import pytest
from httpx import AsyncClient

@pytest.mark.asyncio
async def test_groups_enabled_default_false(client: AsyncClient):
    resp = await client.get("/settings/admin")
    assert resp.status_code == 200
    body = resp.json()
    assert body["groups_enabled"] is False

@pytest.mark.asyncio
async def test_groups_enabled_toggle_persists(client: AsyncClient):
    resp = await client.put("/settings/admin", json={"groups_enabled": True})
    assert resp.status_code == 200
    assert resp.json()["groups_enabled"] is True

    resp2 = await client.get("/settings/admin")
    assert resp2.json()["groups_enabled"] is True
```

- [ ] **Step 3: Run the test, confirm failure**

Run: `cd backend && python3 -m pytest tests/test_settings_admin.py -v`
Expected: FAIL (`groups_enabled` not in response)

- [ ] **Step 4: Implement the setting**

In `backend/app/routes/settings.py`, update `_ADMIN_DEFAULTS`, `get_admin_settings`, `AdminSettingsUpdate`, and `update_admin_settings`:

```python
_ADMIN_DEFAULTS: dict[str, str] = {
    "validation_enabled": "false",
    "comparison_enabled": "false",
    "groups_enabled": "false",
}


@router.get("/admin")
async def get_admin_settings(session: AsyncSession = Depends(get_session)) -> dict:
    return {
        "validation_enabled": (await _get_setting(session, "validation_enabled")) == "true",
        "comparison_enabled": (await _get_setting(session, "comparison_enabled")) == "true",
        "groups_enabled": (await _get_setting(session, "groups_enabled")) == "true",
    }


class AdminSettingsUpdate(BaseModel):
    validation_enabled: bool | None = None
    comparison_enabled: bool | None = None
    groups_enabled: bool | None = None


@router.put("/admin")
async def update_admin_settings(
    body: AdminSettingsUpdate,
    session: AsyncSession = Depends(get_session),
) -> dict:
    updates: dict[str, str] = {}
    if body.validation_enabled is not None:
        updates["validation_enabled"] = "true" if body.validation_enabled else "false"
    if body.comparison_enabled is not None:
        updates["comparison_enabled"] = "true" if body.comparison_enabled else "false"
    if body.groups_enabled is not None:
        updates["groups_enabled"] = "true" if body.groups_enabled else "false"

    for key, value in updates.items():
        row = await session.get(AppSetting, key)
        if row is None:
            session.add(AppSetting(key=key, value=value))
        else:
            row.value = value
    await session.commit()

    return {
        "validation_enabled": (await _get_setting(session, "validation_enabled")) == "true",
        "comparison_enabled": (await _get_setting(session, "comparison_enabled")) == "true",
        "groups_enabled": (await _get_setting(session, "groups_enabled")) == "true",
    }
```

- [ ] **Step 5: Run tests, confirm pass**

Run: `cd backend && python3 -m pytest tests/test_settings_admin.py -v`
Expected: PASS

- [ ] **Step 6: Lint**

Run: `cd backend && ruff check app/routes/settings.py tests/test_settings_admin.py && ruff format --check app/routes/settings.py tests/test_settings_admin.py`
Expected: clean. Run `ruff format app/routes/settings.py tests/test_settings_admin.py` if format fails.

- [ ] **Step 7: Commit**

```bash
git add backend/app/routes/settings.py backend/tests/test_settings_admin.py
git commit -m "feat(settings): add groups_enabled admin toggle (#322)"
```

---

## Task 2: Backend — `list_groups_with_expression` helper

**Files:**
- Modify: `backend/app/services/fhir_client.py` (add new function near existing `list_groups`)
- Test: `backend/tests/test_fhir_client_groups.py` (create)

- [ ] **Step 1: Write failing tests**

Create `backend/tests/test_fhir_client_groups.py`:

```python
"""Unit tests for groups-feature fhir_client helpers (issue #322)."""
import pytest
from unittest.mock import AsyncMock, patch

from app.services.fhir_client import list_groups_with_expression


CQL_EXTENSION_URL = "http://hl7.org/fhir/StructureDefinition/characteristicExpression"


def _bundle(entries: list[dict]) -> dict:
    return {"resourceType": "Bundle", "entry": [{"resource": r} for r in entries], "link": []}


@pytest.mark.asyncio
async def test_list_filters_to_cql_evaluatable_groups():
    cql_group = {
        "resourceType": "Group",
        "id": "g1",
        "name": "CQL Group",
        "type": "person",
        "extension": [{
            "url": CQL_EXTENSION_URL,
            "valueExpression": {"language": "text/cql-expression", "expression": "Patient.active"},
        }],
    }
    plain_group = {"resourceType": "Group", "id": "g2", "name": "Plain", "type": "person"}
    wrong_lang = {
        "resourceType": "Group", "id": "g3", "name": "Wrong Lang", "type": "person",
        "extension": [{
            "url": CQL_EXTENSION_URL,
            "valueExpression": {"language": "text/fhirpath", "expression": "Patient.active"},
        }],
    }
    other_extension = {
        "resourceType": "Group", "id": "g4", "name": "Other Ext", "type": "person",
        "extension": [{"url": "http://example.org/other", "valueString": "noop"}],
    }

    with patch("app.services.fhir_client.httpx.AsyncClient") as mock_client_cls:
        mock_client = AsyncMock()
        mock_client_cls.return_value.__aenter__.return_value = mock_client
        mock_client.get.return_value.json.return_value = _bundle(
            [cql_group, plain_group, wrong_lang, other_extension]
        )
        mock_client.get.return_value.raise_for_status = AsyncMock()

        out = await list_groups_with_expression("http://cdr.example", {})

    assert len(out) == 1
    g = out[0]
    assert g["id"] == "g1"
    assert g["name"] == "CQL Group"
    assert g["type"] == "person"
    assert g["expression_language"] == "text/cql-expression"
    assert g["expression_preview"].startswith("Patient.active")


@pytest.mark.asyncio
async def test_list_truncates_long_expressions():
    long_expr = "Patient." + ("x" * 500)
    cql_group = {
        "resourceType": "Group", "id": "g1", "name": "Long",
        "extension": [{
            "url": CQL_EXTENSION_URL,
            "valueExpression": {"language": "text/cql-expression", "expression": long_expr},
        }],
    }
    with patch("app.services.fhir_client.httpx.AsyncClient") as mock_client_cls:
        mock_client = AsyncMock()
        mock_client_cls.return_value.__aenter__.return_value = mock_client
        mock_client.get.return_value.json.return_value = _bundle([cql_group])
        mock_client.get.return_value.raise_for_status = AsyncMock()

        out = await list_groups_with_expression("http://cdr.example", {})

    assert len(out[0]["expression_preview"]) <= 123  # 120 chars + ellipsis "..."
    assert out[0]["expression_preview"].endswith("...")


@pytest.mark.asyncio
async def test_list_accepts_text_cql_identifier_language():
    cql_group = {
        "resourceType": "Group", "id": "g1", "name": "Ident",
        "extension": [{
            "url": CQL_EXTENSION_URL,
            "valueExpression": {"language": "text/cql-identifier", "expression": "InEligible"},
        }],
    }
    with patch("app.services.fhir_client.httpx.AsyncClient") as mock_client_cls:
        mock_client = AsyncMock()
        mock_client_cls.return_value.__aenter__.return_value = mock_client
        mock_client.get.return_value.json.return_value = _bundle([cql_group])
        mock_client.get.return_value.raise_for_status = AsyncMock()

        out = await list_groups_with_expression("http://cdr.example", {})

    assert len(out) == 1
    assert out[0]["expression_language"] == "text/cql-identifier"
```

- [ ] **Step 2: Run tests, confirm failure**

Run: `cd backend && python3 -m pytest tests/test_fhir_client_groups.py -v`
Expected: FAIL with `ImportError: cannot import name 'list_groups_with_expression'`

- [ ] **Step 3: Implement helper**

Append to `backend/app/services/fhir_client.py` after the existing `list_groups` function (around line 1108):

```python
_CQL_CHARACTERISTIC_EXTENSION_URL = (
    "http://hl7.org/fhir/StructureDefinition/characteristicExpression"
)
_EXPRESSION_PREVIEW_MAX = 120


def _extract_cql_expression(resource: dict[str, Any]) -> Optional[dict[str, str]]:
    """Return {'language', 'expression'} if resource carries a CQL valueExpression,
    else None. Filters by extension URL and `text/cql*` language prefix."""
    for ext in resource.get("extension", []) or []:
        if ext.get("url") != _CQL_CHARACTERISTIC_EXTENSION_URL:
            continue
        ve = ext.get("valueExpression") or {}
        language = ve.get("language") or ""
        expression = ve.get("expression") or ""
        if language.startswith("text/cql") and expression:
            return {"language": language, "expression": expression}
    return None


async def list_groups_with_expression(
    cdr_url: str,
    auth_headers: dict[str, str],
) -> list[dict[str, Any]]:
    """List CDR Group resources filtered to those carrying a CQL valueExpression.

    Used by the experimental /api/groups endpoint (issue #322). Distinct from
    `list_groups` which serves the measure-job flow and is intentionally not
    modified."""
    groups: list[dict[str, Any]] = []
    url: Optional[str] = f"{cdr_url}/Group?_count=100"
    async with httpx.AsyncClient(timeout=30.0) as client:
        while url:
            resp = await client.get(url, headers=auth_headers)
            resp.raise_for_status()
            bundle = resp.json()
            for entry in bundle.get("entry", []):
                resource = entry.get("resource", {})
                if resource.get("resourceType") != "Group":
                    continue
                cql = _extract_cql_expression(resource)
                if cql is None:
                    continue
                expression = cql["expression"]
                preview = (
                    expression
                    if len(expression) <= _EXPRESSION_PREVIEW_MAX
                    else expression[:_EXPRESSION_PREVIEW_MAX] + "..."
                )
                groups.append({
                    "id": resource.get("id"),
                    "name": resource.get("name"),
                    "type": resource.get("type"),
                    "expression_language": cql["language"],
                    "expression_preview": preview,
                })
            url = None
            for link in bundle.get("link", []):
                if link.get("relation") == "next":
                    next_url = link.get("url")
                    if next_url and _same_origin(cdr_url, next_url):
                        url = next_url
                    elif next_url:
                        logger.warning(
                            "SSRF: pagination next link rejected (origin mismatch)",
                            extra={"url": sanitize_url(next_url)},
                        )
                    break
    return groups
```

- [ ] **Step 4: Run tests, confirm pass**

Run: `cd backend && python3 -m pytest tests/test_fhir_client_groups.py -v`
Expected: PASS (3 tests)

- [ ] **Step 5: Lint**

Run: `cd backend && ruff check app/services/fhir_client.py tests/test_fhir_client_groups.py && ruff format --check app/services/fhir_client.py tests/test_fhir_client_groups.py`

- [ ] **Step 6: Commit**

```bash
git add backend/app/services/fhir_client.py backend/tests/test_fhir_client_groups.py
git commit -m "feat(fhir_client): list_groups_with_expression helper (#322)"
```

---

## Task 3: Backend — `evaluate_group_and_resolve_members` helper

**Files:**
- Modify: `backend/app/services/fhir_client.py`
- Test: `backend/tests/test_fhir_client_groups.py` (extend)

- [ ] **Step 1: Write failing tests**

Append to `backend/tests/test_fhir_client_groups.py`:

```python
from app.services.fhir_client import (
    evaluate_group_and_resolve_members,
    GroupEvaluateError,
)


@pytest.mark.asyncio
async def test_evaluate_returns_enriched_members():
    eval_resp = {
        "resourceType": "Group",
        "id": "g1",
        "member": [
            {"entity": {"reference": "Patient/p1"}},
            {"entity": {"reference": "Patient/p2"}},
        ],
    }
    p1 = {
        "resourceType": "Patient", "id": "p1",
        "name": [{"family": "Smith", "given": ["John"]}],
        "gender": "male", "birthDate": "1980-04-12",
    }
    p2 = {
        "resourceType": "Patient", "id": "p2",
        "name": [{"family": "Doe", "given": ["Jane"]}],
        "gender": "female", "birthDate": "1992-08-30",
    }

    with patch("app.services.fhir_client.httpx.AsyncClient") as mock_client_cls:
        mock_client = AsyncMock()
        mock_client_cls.return_value.__aenter__.return_value = mock_client

        async def fake_get(url, headers=None):
            class R:
                status_code = 200
                def json(self_inner):
                    if url.endswith("/$evaluate"):
                        return eval_resp
                    if url.endswith("/Patient/p1"):
                        return p1
                    if url.endswith("/Patient/p2"):
                        return p2
                    raise AssertionError(f"unexpected url {url}")
                def raise_for_status(self_inner):
                    pass
            return R()

        async def fake_post(url, headers=None, json=None):
            class R:
                status_code = 200
                is_success = True
                def json(self_inner):
                    return eval_resp
                def raise_for_status(self_inner):
                    pass
            return R()

        mock_client.get = AsyncMock(side_effect=fake_get)
        mock_client.post = AsyncMock(side_effect=fake_post)

        result = await evaluate_group_and_resolve_members("http://cdr.example", "g1", {})

    assert result["group_id"] == "g1"
    assert result["member_count"] == 2
    by_id = {m["id"]: m for m in result["members"]}
    assert by_id["p1"]["name"] == "Smith, John"
    assert by_id["p1"]["gender"] == "male"
    assert by_id["p1"]["birth_date"] == "1980-04-12"
    assert "lookup_error" not in by_id["p1"]
    assert by_id["p2"]["name"] == "Doe, Jane"


@pytest.mark.asyncio
async def test_evaluate_partial_failure_records_lookup_error():
    eval_resp = {
        "resourceType": "Group", "id": "g1",
        "member": [
            {"entity": {"reference": "Patient/p1"}},
            {"entity": {"reference": "Patient/missing"}},
        ],
    }
    p1 = {"resourceType": "Patient", "id": "p1", "name": [{"family": "Smith", "given": ["John"]}]}

    with patch("app.services.fhir_client.httpx.AsyncClient") as mock_client_cls:
        mock_client = AsyncMock()
        mock_client_cls.return_value.__aenter__.return_value = mock_client

        async def fake_get(url, headers=None):
            class R:
                def __init__(self_inner, code, body):
                    self_inner.status_code = code
                    self_inner._body = body
                def json(self_inner):
                    return self_inner._body
                def raise_for_status(self_inner):
                    pass
            if url.endswith("/Patient/p1"):
                return R(200, p1)
            if url.endswith("/Patient/missing"):
                return R(404, {"resourceType": "OperationOutcome"})
            raise AssertionError(f"unexpected url {url}")

        async def fake_post(url, headers=None, json=None):
            class R:
                status_code = 200
                is_success = True
                def json(self_inner): return eval_resp
                def raise_for_status(self_inner): pass
            return R()

        mock_client.get = AsyncMock(side_effect=fake_get)
        mock_client.post = AsyncMock(side_effect=fake_post)

        result = await evaluate_group_and_resolve_members("http://cdr.example", "g1", {})

    by_id = {m["id"]: m for m in result["members"]}
    assert by_id["p1"]["name"] == "Smith, John"
    assert by_id["missing"]["name"] is None
    assert "lookup_error" in by_id["missing"]


@pytest.mark.asyncio
async def test_evaluate_raises_on_operation_outcome():
    outcome = {
        "resourceType": "OperationOutcome",
        "issue": [{"severity": "error", "code": "not-supported", "diagnostics": "no $evaluate"}],
    }
    with patch("app.services.fhir_client.httpx.AsyncClient") as mock_client_cls:
        mock_client = AsyncMock()
        mock_client_cls.return_value.__aenter__.return_value = mock_client

        async def fake_post(url, headers=None, json=None):
            class R:
                status_code = 400
                is_success = False
                def json(self_inner): return outcome
                def raise_for_status(self_inner): pass
            return R()
        mock_client.post = AsyncMock(side_effect=fake_post)

        with pytest.raises(GroupEvaluateError) as ei:
            await evaluate_group_and_resolve_members("http://cdr.example", "g1", {})

    assert ei.value.status_code == 400
    assert ei.value.operation_outcome == outcome


@pytest.mark.asyncio
async def test_evaluate_zero_members_returns_empty_list():
    eval_resp = {"resourceType": "Group", "id": "g1", "member": []}
    with patch("app.services.fhir_client.httpx.AsyncClient") as mock_client_cls:
        mock_client = AsyncMock()
        mock_client_cls.return_value.__aenter__.return_value = mock_client

        async def fake_post(url, headers=None, json=None):
            class R:
                status_code = 200
                is_success = True
                def json(self_inner): return eval_resp
                def raise_for_status(self_inner): pass
            return R()
        mock_client.post = AsyncMock(side_effect=fake_post)
        mock_client.get = AsyncMock()

        result = await evaluate_group_and_resolve_members("http://cdr.example", "g1", {})

    assert result["member_count"] == 0
    assert result["members"] == []
```

- [ ] **Step 2: Run tests, confirm failure**

Run: `cd backend && python3 -m pytest tests/test_fhir_client_groups.py -v`
Expected: FAIL (`GroupEvaluateError` and `evaluate_group_and_resolve_members` not yet defined)

- [ ] **Step 3: Implement helper + error class**

Append to `backend/app/services/fhir_client.py`:

```python
class GroupEvaluateError(Exception):
    """Raised when Group/<id>/$evaluate returns a non-success response.

    Carries the upstream HTTP status code and (if parseable) the FHIR
    OperationOutcome body so callers can pass it through to clients."""

    def __init__(
        self,
        message: str,
        status_code: int,
        operation_outcome: Optional[dict] = None,
    ):
        super().__init__(message)
        self.status_code = status_code
        self.operation_outcome = operation_outcome


def _format_patient_name(patient: dict[str, Any]) -> Optional[str]:
    """Render the patient's first HumanName as 'family, given1 given2', or None."""
    names = patient.get("name") or []
    if not names:
        return None
    n = names[0]
    family = n.get("family")
    given = " ".join(n.get("given") or [])
    if family and given:
        return f"{family}, {given}"
    return family or given or None


async def evaluate_group_and_resolve_members(
    cdr_url: str,
    group_id: str,
    auth_headers: dict[str, str],
) -> dict[str, Any]:
    """Invoke Group/<id>/$evaluate on the CDR and resolve members to patient summaries.

    Returns: {"group_id", "evaluated_at" (ISO Z), "member_count", "members":
    [{"id", "name", "gender", "birth_date", "lookup_error"?}, ...]}.

    Raises GroupEvaluateError if $evaluate returns non-success."""
    evaluated_at = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")

    async with httpx.AsyncClient(timeout=60.0) as client:
        resp = await client.post(
            f"{cdr_url}/Group/{group_id}/$evaluate",
            headers=auth_headers,
        )
        if not resp.is_success:
            try:
                outcome = resp.json()
            except Exception:
                outcome = None
            if not isinstance(outcome, dict) or outcome.get("resourceType") != "OperationOutcome":
                outcome = None
            raise GroupEvaluateError(
                f"Group/$evaluate failed with status {resp.status_code}",
                status_code=resp.status_code,
                operation_outcome=outcome,
            )

        evaluated_group = resp.json()
        refs: list[tuple[str, str]] = []
        for m in evaluated_group.get("member", []):
            ref = m.get("entity", {}).get("reference", "")
            if ref.startswith("Patient/"):
                refs.append((ref.split("/", 1)[1], ref))

        semaphore = asyncio.Semaphore(10)

        async def fetch(patient_id: str, ref: str) -> dict[str, Any]:
            async with semaphore:
                try:
                    pr = await client.get(
                        f"{cdr_url}/Patient/{patient_id}", headers=auth_headers,
                    )
                except Exception as exc:
                    return {
                        "id": patient_id,
                        "name": None,
                        "gender": None,
                        "birth_date": None,
                        "lookup_error": f"{type(exc).__name__}: {exc}",
                    }
                if pr.status_code != 200:
                    return {
                        "id": patient_id,
                        "name": None,
                        "gender": None,
                        "birth_date": None,
                        "lookup_error": f"HTTP {pr.status_code}",
                    }
                p = pr.json()
                return {
                    "id": patient_id,
                    "name": _format_patient_name(p),
                    "gender": p.get("gender"),
                    "birth_date": p.get("birthDate"),
                }

        members = await asyncio.gather(*[fetch(pid, ref) for pid, ref in refs])

    return {
        "group_id": group_id,
        "evaluated_at": evaluated_at,
        "member_count": len(members),
        "members": members,
    }
```

Confirm `datetime` and `timezone` are already imported at the top of `fhir_client.py`; if not, add:
```python
from datetime import datetime, timezone
```

- [ ] **Step 4: Run tests, confirm pass**

Run: `cd backend && python3 -m pytest tests/test_fhir_client_groups.py -v`
Expected: PASS (7 tests total)

- [ ] **Step 5: Lint**

Run: `cd backend && ruff check app/services/fhir_client.py tests/test_fhir_client_groups.py && ruff format --check app/services/fhir_client.py tests/test_fhir_client_groups.py`

- [ ] **Step 6: Commit**

```bash
git add backend/app/services/fhir_client.py backend/tests/test_fhir_client_groups.py
git commit -m "feat(fhir_client): evaluate_group_and_resolve_members helper (#322)"
```

---

## Task 4: Backend — `routes/groups.py` (both endpoints)

**Files:**
- Create: `backend/app/routes/groups.py`
- Test: `backend/tests/test_routes_groups.py` (create)

- [ ] **Step 1: Write failing tests**

Create `backend/tests/test_routes_groups.py`:

```python
"""Unit tests for /api/groups endpoints (issue #322)."""
import pytest
from unittest.mock import AsyncMock, patch
from httpx import AsyncClient


async def _enable_groups(client: AsyncClient) -> None:
    await client.put("/settings/admin", json={"groups_enabled": True})


@pytest.mark.asyncio
async def test_list_groups_404_when_feature_disabled(client: AsyncClient):
    # Default is disabled.
    resp = await client.get("/api/groups")
    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_list_groups_happy(client: AsyncClient):
    await _enable_groups(client)
    fake_groups = [{
        "id": "g1",
        "name": "Active Adults",
        "type": "person",
        "expression_language": "text/cql-expression",
        "expression_preview": "Patient.active",
    }]
    with patch(
        "app.routes.groups.list_groups_with_expression",
        new=AsyncMock(return_value=fake_groups),
    ):
        resp = await client.get("/api/groups")
    assert resp.status_code == 200
    assert resp.json() == {"groups": fake_groups}


@pytest.mark.asyncio
async def test_list_groups_502_when_cdr_unreachable(client: AsyncClient):
    await _enable_groups(client)
    with patch(
        "app.routes.groups.list_groups_with_expression",
        new=AsyncMock(side_effect=Exception("connection refused")),
    ):
        resp = await client.get("/api/groups")
    assert resp.status_code == 502


@pytest.mark.asyncio
async def test_evaluate_404_when_feature_disabled(client: AsyncClient):
    resp = await client.post("/api/groups/g1/evaluate")
    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_evaluate_happy(client: AsyncClient):
    await _enable_groups(client)
    fake_result = {
        "group_id": "g1",
        "evaluated_at": "2026-05-17T14:32:01Z",
        "member_count": 1,
        "members": [{
            "id": "p1", "name": "Smith, John", "gender": "male", "birth_date": "1980-04-12",
        }],
    }
    with patch(
        "app.routes.groups.evaluate_group_and_resolve_members",
        new=AsyncMock(return_value=fake_result),
    ):
        resp = await client.post("/api/groups/g1/evaluate")
    assert resp.status_code == 200
    assert resp.json() == fake_result


@pytest.mark.asyncio
async def test_evaluate_passes_operation_outcome_through(client: AsyncClient):
    from app.services.fhir_client import GroupEvaluateError

    await _enable_groups(client)
    outcome = {
        "resourceType": "OperationOutcome",
        "issue": [{"severity": "error", "code": "not-supported", "diagnostics": "no $evaluate"}],
    }
    err = GroupEvaluateError("nope", status_code=400, operation_outcome=outcome)
    with patch(
        "app.routes.groups.evaluate_group_and_resolve_members",
        new=AsyncMock(side_effect=err),
    ):
        resp = await client.post("/api/groups/g1/evaluate")
    assert resp.status_code == 502
    assert resp.json()["operation_outcome"] == outcome


@pytest.mark.asyncio
async def test_evaluate_timeout_returns_504(client: AsyncClient):
    import httpx
    await _enable_groups(client)
    with patch(
        "app.routes.groups.evaluate_group_and_resolve_members",
        new=AsyncMock(side_effect=httpx.TimeoutException("slow")),
    ):
        resp = await client.post("/api/groups/g1/evaluate")
    assert resp.status_code == 504


@pytest.mark.asyncio
async def test_group_id_must_be_safe(client: AsyncClient):
    await _enable_groups(client)
    resp = await client.post("/api/groups/..%2Fevil/evaluate")
    assert resp.status_code in (400, 422)
```

- [ ] **Step 2: Run tests, confirm failure**

Run: `cd backend && python3 -m pytest tests/test_routes_groups.py -v`
Expected: FAIL (no router yet → 404 for all paths, but for unrelated reasons; some asserts pass by accident).

- [ ] **Step 3: Implement the router**

Create `backend/app/routes/groups.py`:

```python
"""Experimental Groups endpoints (issue #322).

Lists CQL-evaluatable Groups on the active CDR and invokes
`Group/<id>/$evaluate`. Architecturally independent from the Measure pipeline:
this module imports nothing from `app.orchestrator`, `app.routes.jobs`,
`app.routes.measures`, `app.routes.results`, or measure/job state modules.
"""

from __future__ import annotations

import logging
import re

import httpx
from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import JSONResponse
from sqlalchemy.ext.asyncio import AsyncSession

from app.db import get_session
from app.dependencies import ConnectionContext, get_active_cdr
from app.services.fhir_client import (
    GroupEvaluateError,
    _build_auth_headers,
    evaluate_group_and_resolve_members,
    list_groups_with_expression,
)

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/groups", tags=["groups"])

_GROUP_ID_RE = re.compile(r"^[A-Za-z0-9_\-\.]{1,256}$")


async def _require_feature_enabled(session: AsyncSession) -> None:
    """Return 404 unless the `groups_enabled` admin setting is true."""
    from app.models.app_setting import AppSetting  # local import: keep top-level imports lean

    row = await session.get(AppSetting, "groups_enabled")
    enabled = (row.value if row is not None else "false") == "true"
    if not enabled:
        raise HTTPException(status_code=404, detail="Not Found")


@router.get("")
async def list_groups_endpoint(
    session: AsyncSession = Depends(get_session),
    cdr: ConnectionContext = Depends(get_active_cdr),
) -> dict:
    await _require_feature_enabled(session)
    auth_headers = await _build_auth_headers(cdr.auth_type, cdr.auth_credentials)
    try:
        groups = await list_groups_with_expression(cdr.cdr_url, auth_headers)
    except Exception:
        logger.exception("Failed to list groups for $evaluate page")
        raise HTTPException(
            status_code=502,
            detail="Cannot reach CDR to list groups. Check CDR connectivity in Settings.",
        )
    return {"groups": groups}


@router.post("/{group_id}/evaluate")
async def evaluate_group_endpoint(
    group_id: str,
    session: AsyncSession = Depends(get_session),
    cdr: ConnectionContext = Depends(get_active_cdr),
) -> JSONResponse:
    await _require_feature_enabled(session)

    if not _GROUP_ID_RE.match(group_id):
        raise HTTPException(
            status_code=400,
            detail="group_id must be alphanumeric with hyphens, underscores, or dots only",
        )

    auth_headers = await _build_auth_headers(cdr.auth_type, cdr.auth_credentials)
    try:
        result = await evaluate_group_and_resolve_members(cdr.cdr_url, group_id, auth_headers)
    except GroupEvaluateError as exc:
        body = {"error": str(exc), "status": exc.status_code}
        if exc.operation_outcome is not None:
            body["operation_outcome"] = exc.operation_outcome
        return JSONResponse(status_code=502, content=body)
    except httpx.TimeoutException as exc:
        logger.warning("Group $evaluate timed out", extra={"group_id": group_id})
        return JSONResponse(status_code=504, content={"error": str(exc)})
    except Exception:
        logger.exception("Group $evaluate unexpected failure", extra={"group_id": group_id})
        return JSONResponse(status_code=502, content={"error": "Group $evaluate failed"})

    return JSONResponse(status_code=200, content=result)
```

- [ ] **Step 4: Register router in `main.py`**

Modify `backend/app/main.py` around line 18 and line 549:

```python
# In imports
from app.routes import groups, health, jobs, measures, results, settings, validation
```

```python
# In router registration (after the existing block, alphabetical placement)
app.include_router(groups.router)
```

- [ ] **Step 5: Run tests, confirm pass**

Run: `cd backend && python3 -m pytest tests/test_routes_groups.py -v`
Expected: PASS (8 tests)

- [ ] **Step 6: Lint**

Run: `cd backend && ruff check app/routes/groups.py app/main.py tests/test_routes_groups.py && ruff format --check app/routes/groups.py app/main.py tests/test_routes_groups.py`

- [ ] **Step 7: Commit**

```bash
git add backend/app/routes/groups.py backend/app/main.py backend/tests/test_routes_groups.py
git commit -m "feat(api): /api/groups list + evaluate endpoints (#322)"
```

---

## Task 5: Backend — Architecture independence test

**Files:**
- Create: `backend/tests/test_groups_independence.py`

- [ ] **Step 1: Write the independence test**

Create `backend/tests/test_groups_independence.py`:

```python
"""Static check (issue #322 acceptance criterion):

`app/routes/groups.py` and `frontend/src/pages/GroupsPage.js` must not import
anything from the Measure pipeline. The new feature is architecturally
independent — shared infra (fhir_client, dependencies, db) is allowed; job/
measure/result modules are not.
"""
from __future__ import annotations

import ast
from pathlib import Path

import pytest

BACKEND_ROOT = Path(__file__).resolve().parents[1] / "app"
GROUPS_PY = BACKEND_ROOT / "routes" / "groups.py"

FORBIDDEN_MODULE_PREFIXES = (
    "app.orchestrator",
    "app.routes.jobs",
    "app.routes.measures",
    "app.routes.results",
    "app.routes.validation",
    "app.models.job",
    "app.models.validation",
    "app.services.orchestrator",
    "app.services.validation",
)


def _imported_modules(source: str) -> set[str]:
    tree = ast.parse(source)
    mods: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                mods.add(alias.name)
        elif isinstance(node, ast.ImportFrom) and node.module:
            mods.add(node.module)
    return mods


def test_routes_groups_has_no_measure_pipeline_imports():
    source = GROUPS_PY.read_text()
    mods = _imported_modules(source)
    offenders = [
        m for m in mods
        if any(m == p or m.startswith(p + ".") for p in FORBIDDEN_MODULE_PREFIXES)
    ]
    assert not offenders, (
        f"app/routes/groups.py imports forbidden measure-pipeline modules: "
        f"{offenders}. The Groups feature must remain architecturally independent."
    )
```

- [ ] **Step 2: Run the test**

Run: `cd backend && python3 -m pytest tests/test_groups_independence.py -v`
Expected: PASS

- [ ] **Step 3: Commit**

```bash
git add backend/tests/test_groups_independence.py
git commit -m "test: assert /api/groups is independent of measure pipeline (#322)"
```

---

## Task 6: Frontend — API client functions

**Files:**
- Modify: `frontend/src/api/client.js`

- [ ] **Step 1: Add functions**

Append after the existing exports in `frontend/src/api/client.js`:

```javascript
// Groups (experimental, issue #322)
export function getEvaluatableGroups() {
  return request('/api/groups');
}

export function evaluateGroup(groupId) {
  return request(`/api/groups/${encodeURIComponent(groupId)}/evaluate`, {
    method: 'POST',
    _timeout: 60000,
  });
}
```

- [ ] **Step 2: Commit (no tests yet — exercised by page tests in Task 8)**

```bash
git add frontend/src/api/client.js
git commit -m "feat(frontend): api client for /api/groups (#322)"
```

---

## Task 7: Frontend — Settings toggle + features wiring

**Files:**
- Modify: `frontend/src/pages/SettingsPage.js`
- Modify: `frontend/src/App.js`

- [ ] **Step 1: Add the `Groups` toggle row in SettingsPage.js**

In `frontend/src/pages/SettingsPage.js`:

(a) Add a new handler near `handleToggleComparison` (around line 87):

```javascript
const handleToggleGroups = async (enabled) => {
  setAdminSaving(true);
  try {
    const updated = await updateAdminSettings({ groups_enabled: enabled });
    setAdminSettings(updated);
    window.dispatchEvent(new CustomEvent('admin-settings-changed', { detail: updated }));
    toast.success(enabled ? 'Groups enabled' : 'Groups disabled');
  } catch (err) {
    toast.error(err.message || 'Failed to update setting');
  } finally {
    setAdminSaving(false);
  }
};
```

(b) Add a new `adminRow` block immediately after the Comparison row (around line 252, before the closing `</div>` of the Developer Tools card):

```jsx
<div className={styles.adminRow}>
  <div className={styles.adminRowInfo}>
    <div className={styles.adminRowLabel}>Groups</div>
    <div className={styles.adminRowDesc}>
      Adds a Groups tab where you can list CQL-evaluatable Groups from the current CDR
      and invoke <code>Group/&lt;id&gt;/$evaluate</code> to resolve members.
      Requires a CDR that supports the operation (the bundled HAPI image does not).
      Experimental.
    </div>
  </div>
  <Toggle
    checked={adminSettings?.groups_enabled ?? false}
    onChange={handleToggleGroups}
    disabled={adminSaving}
  />
</div>
```

(c) Update the `loadAdminSettings` catch-fallback (around line 50) so the missing-call default includes `groups_enabled`:

```javascript
setAdminSettings({ validation_enabled: false, comparison_enabled: false, groups_enabled: false });
```

- [ ] **Step 2: Wire `groups` into App.js features**

In `frontend/src/App.js`:

(a) Update the `getAdminSettings` `.then` (around line 155) and the `admin-settings-changed` handler (around line 157):

```javascript
useEffect(() => {
  getAdminSettings()
    .then(s => setFeatures({
      validation: s.validation_enabled ?? false,
      groups: s.groups_enabled ?? false,
    }))
    .catch(() => {});
  const h = (e) => setFeatures({
    validation: e.detail.validation_enabled ?? false,
    groups: e.detail.groups_enabled ?? false,
  });
  window.addEventListener('admin-settings-changed', h);
  return () => window.removeEventListener('admin-settings-changed', h);
}, []);
```

(b) Add a `'g'` / `'G'` keyboard shortcut in the keyboard handler (after the validation case, around line 185):

```javascript
else if ((e.key === 'g' || e.key === 'G') && features.groups) navigate('/groups');
```

- [ ] **Step 3: Sanity check (manual)**

Start the dev server: `cd frontend && npm start` (port 3001).
Open Settings → Admin tab. Toggle Groups on. Confirm: page survives without errors; toast appears. Toggle back off.

- [ ] **Step 4: Commit**

```bash
git add frontend/src/App.js frontend/src/pages/SettingsPage.js
git commit -m "feat(settings): Groups admin toggle + features wiring (#322)"
```

---

## Task 8: Frontend — Create `GroupsPage` with disabled-state redirect

**Files:**
- Create: `frontend/src/pages/GroupsPage.js`
- Create: `frontend/src/pages/GroupsPage.module.css`
- Create: `frontend/src/pages/GroupsPage.test.js`

- [ ] **Step 1: Write failing test for disabled-state redirect**

Create `frontend/src/pages/GroupsPage.test.js`:

```javascript
import React from 'react';
import { render, screen, waitFor } from '@testing-library/react';
import { MemoryRouter, Routes, Route } from 'react-router-dom';
import GroupsPage from './GroupsPage';
import * as api from '../api/client';

jest.mock('../api/client');

function renderAt(path = '/groups') {
  return render(
    <MemoryRouter initialEntries={[path]}>
      <Routes>
        <Route path="/groups" element={<GroupsPage />} />
        <Route path="/measures" element={<div data-testid="measures-page">Measures</div>} />
      </Routes>
    </MemoryRouter>,
  );
}

describe('GroupsPage — feature disabled', () => {
  test('redirects to /measures when groups_enabled is false', async () => {
    api.getAdminSettings = jest.fn().mockResolvedValue({ groups_enabled: false });
    renderAt();
    await waitFor(() => expect(screen.getByTestId('measures-page')).toBeInTheDocument());
  });
});
```

- [ ] **Step 2: Run test, confirm failure**

Run: `cd frontend && CI=true npm test -- --watchAll=false GroupsPage.test.js`
Expected: FAIL (`GroupsPage` does not yet exist).

- [ ] **Step 3: Create minimal `GroupsPage.js` to pass redirect test**

Create `frontend/src/pages/GroupsPage.js`:

```javascript
import React, { useEffect, useState } from 'react';
import { Navigate } from 'react-router-dom';
import { getAdminSettings } from '../api/client';
import styles from './GroupsPage.module.css';

export default function GroupsPage() {
  const [enabled, setEnabled] = useState(null); // null = loading, true/false = known

  useEffect(() => {
    let cancelled = false;
    getAdminSettings()
      .then(s => { if (!cancelled) setEnabled(!!s.groups_enabled); })
      .catch(() => { if (!cancelled) setEnabled(false); });
    return () => { cancelled = true; };
  }, []);

  if (enabled === null) return <div className={styles.page} />;
  if (!enabled) return <Navigate to="/measures" replace />;

  return (
    <div className={styles.page}>
      <h1>Groups</h1>
    </div>
  );
}
```

Create `frontend/src/pages/GroupsPage.module.css`:

```css
.page {
  padding: 24px;
  max-width: 1100px;
}
```

- [ ] **Step 4: Run test, confirm pass**

Run: `cd frontend && CI=true npm test -- --watchAll=false GroupsPage.test.js`
Expected: PASS (1 test)

- [ ] **Step 5: Add the frontend independence test**

Append to `frontend/src/pages/GroupsPage.test.js`:

```javascript
import fs from 'fs';
import path from 'path';

describe('GroupsPage — architecture independence (#322)', () => {
  const FORBIDDEN_IMPORT_FRAGMENTS = [
    '/pages/JobsPage',
    '/pages/MeasuresPage',
    '/pages/ResultsPage',
    '/pages/ValidationPage',
    '/utils/jobStatus',
    '/utils/measureFormat',
  ];

  test('GroupsPage.js does not import measure-pipeline modules', () => {
    const source = fs.readFileSync(
      path.join(__dirname, 'GroupsPage.js'),
      'utf8',
    );
    const offenders = FORBIDDEN_IMPORT_FRAGMENTS.filter(f => source.includes(f));
    expect(offenders).toEqual([]);
  });
});
```

Run: `cd frontend && CI=true npm test -- --watchAll=false GroupsPage.test.js`
Expected: PASS (2 tests total at this stage).

- [ ] **Step 6: Commit**

```bash
git add frontend/src/pages/GroupsPage.js frontend/src/pages/GroupsPage.module.css frontend/src/pages/GroupsPage.test.js
git commit -m "feat(groups): GroupsPage shell with disabled-state redirect (#322)"
```

---

## Task 9: Frontend — List rendering on `GroupsPage`

**Files:**
- Modify: `frontend/src/pages/GroupsPage.js`
- Modify: `frontend/src/pages/GroupsPage.module.css`
- Modify: `frontend/src/pages/GroupsPage.test.js`

- [ ] **Step 1: Add failing tests for list rendering**

Append to `frontend/src/pages/GroupsPage.test.js`:

```javascript
describe('GroupsPage — list', () => {
  test('renders rows from getEvaluatableGroups', async () => {
    api.getAdminSettings = jest.fn().mockResolvedValue({ groups_enabled: true });
    api.getEvaluatableGroups = jest.fn().mockResolvedValue({
      groups: [
        {
          id: 'g1',
          name: 'Active Adults',
          type: 'person',
          expression_language: 'text/cql-expression',
          expression_preview: 'Patient.active and Patient.age >= 18',
        },
      ],
    });
    renderAt();
    expect(await screen.findByText('Active Adults')).toBeInTheDocument();
    expect(screen.getByText(/Patient\.active and Patient\.age >= 18/)).toBeInTheDocument();
    expect(screen.getByText(/text\/cql-expression/)).toBeInTheDocument();
  });

  test('renders empty state when no groups returned', async () => {
    api.getAdminSettings = jest.fn().mockResolvedValue({ groups_enabled: true });
    api.getEvaluatableGroups = jest.fn().mockResolvedValue({ groups: [] });
    renderAt();
    expect(await screen.findByText(/No CQL-evaluatable Groups/i)).toBeInTheDocument();
  });

  test('renders error banner when list call fails', async () => {
    api.getAdminSettings = jest.fn().mockResolvedValue({ groups_enabled: true });
    api.getEvaluatableGroups = jest.fn().mockRejectedValue(new Error('CDR unreachable'));
    renderAt();
    expect(await screen.findByText(/CDR unreachable/i)).toBeInTheDocument();
  });
});
```

- [ ] **Step 2: Run, confirm failure**

Run: `cd frontend && CI=true npm test -- --watchAll=false GroupsPage.test.js`
Expected: 3 new tests fail.

- [ ] **Step 3: Implement list rendering**

Replace `frontend/src/pages/GroupsPage.js` with:

```javascript
import React, { useEffect, useState, useCallback } from 'react';
import { Navigate } from 'react-router-dom';
import { getAdminSettings, getEvaluatableGroups } from '../api/client';
import ErrorBanner from '../components/ErrorBanner';
import styles from './GroupsPage.module.css';

export default function GroupsPage() {
  const [enabled, setEnabled] = useState(null);
  const [groups, setGroups] = useState([]);
  const [loading, setLoading] = useState(true);
  const [listError, setListError] = useState(null);

  useEffect(() => {
    let cancelled = false;
    getAdminSettings()
      .then(s => { if (!cancelled) setEnabled(!!s.groups_enabled); })
      .catch(() => { if (!cancelled) setEnabled(false); });
    return () => { cancelled = true; };
  }, []);

  const loadGroups = useCallback(async () => {
    setLoading(true);
    setListError(null);
    try {
      const data = await getEvaluatableGroups();
      setGroups(data.groups || []);
    } catch (err) {
      setListError(err.message || 'Failed to load groups');
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    if (enabled === true) loadGroups();
  }, [enabled, loadGroups]);

  if (enabled === null) return <div className={styles.page} />;
  if (!enabled) return <Navigate to="/measures" replace />;

  return (
    <div className={styles.page}>
      <div className={styles.header}>
        <h1 className={styles.title}>Groups</h1>
        <button
          type="button"
          className={styles.refreshBtn}
          onClick={loadGroups}
          disabled={loading}
        >
          Refresh
        </button>
      </div>
      <p className={styles.subtitle}>
        Showing only Groups with a CQL <code>valueExpression</code>. Other Groups on the CDR are hidden.
      </p>

      {listError && <ErrorBanner message={listError} />}

      {!listError && !loading && groups.length === 0 && (
        <div className={styles.empty}>No CQL-evaluatable Groups found on this CDR.</div>
      )}

      <div className={styles.rows}>
        {groups.map(g => (
          <div key={g.id} className={styles.row} data-testid={`group-row-${g.id}`}>
            <div className={styles.rowMain}>
              <div className={styles.groupHeader}>
                <span className={styles.groupName}>{g.name || g.id}</span>
                <span className={styles.idChip}>{g.id}</span>
                <span className={styles.langChip}>{g.expression_language}</span>
              </div>
              <code className={styles.expression}>{g.expression_preview}</code>
            </div>
            {/* $evaluate button arrives in the next task */}
          </div>
        ))}
      </div>
    </div>
  );
}
```

Append to `frontend/src/pages/GroupsPage.module.css`:

```css
.header { display: flex; align-items: center; justify-content: space-between; margin-bottom: 8px; }
.title { font-size: 22px; margin: 0; }
.refreshBtn { padding: 6px 12px; border-radius: 6px; border: 1px solid #d0d7de; background: #fff; cursor: pointer; }
.refreshBtn:disabled { opacity: 0.5; cursor: not-allowed; }
.subtitle { color: #57606a; font-size: 13px; margin: 0 0 16px; }
.empty { padding: 32px; text-align: center; color: #57606a; border: 1px dashed #d0d7de; border-radius: 8px; }
.rows { display: flex; flex-direction: column; gap: 8px; }
.row { padding: 12px 16px; border: 1px solid #d0d7de; border-radius: 8px; background: #fff; display: flex; align-items: center; justify-content: space-between; gap: 16px; }
.rowMain { flex: 1; min-width: 0; }
.groupHeader { display: flex; align-items: center; gap: 8px; flex-wrap: wrap; }
.groupName { font-weight: 600; }
.idChip, .langChip { font-family: ui-monospace, SFMono-Regular, Menlo, monospace; font-size: 11px; padding: 2px 6px; border-radius: 4px; background: #f6f8fa; color: #57606a; }
.expression { display: block; font-family: ui-monospace, SFMono-Regular, Menlo, monospace; font-size: 12px; color: #24292f; margin-top: 6px; overflow-wrap: anywhere; }
```

- [ ] **Step 4: Run tests, confirm pass**

Run: `cd frontend && CI=true npm test -- --watchAll=false GroupsPage.test.js`
Expected: 4 tests pass.

- [ ] **Step 5: Commit**

```bash
git add frontend/src/pages/GroupsPage.js frontend/src/pages/GroupsPage.module.css frontend/src/pages/GroupsPage.test.js
git commit -m "feat(groups): list CQL-evaluatable Groups with refresh + empty state (#322)"
```

---

## Task 10: Frontend — `$evaluate` button and accordion

**Files:**
- Modify: `frontend/src/pages/GroupsPage.js`
- Modify: `frontend/src/pages/GroupsPage.module.css`
- Modify: `frontend/src/pages/GroupsPage.test.js`

- [ ] **Step 1: Write failing tests for the $evaluate flow**

Append to `frontend/src/pages/GroupsPage.test.js`:

```javascript
import userEvent from '@testing-library/user-event';

describe('GroupsPage — $evaluate', () => {
  const enableAndOneGroup = () => {
    api.getAdminSettings = jest.fn().mockResolvedValue({ groups_enabled: true });
    api.getEvaluatableGroups = jest.fn().mockResolvedValue({
      groups: [{
        id: 'g1', name: 'g1', type: 'person',
        expression_language: 'text/cql-expression',
        expression_preview: 'Patient.active',
      }],
    });
  };

  test('clicking $evaluate expands the row with members', async () => {
    enableAndOneGroup();
    api.evaluateGroup = jest.fn().mockResolvedValue({
      group_id: 'g1',
      evaluated_at: '2026-05-17T14:32:01Z',
      member_count: 1,
      members: [{ id: 'p1', name: 'Smith, John', gender: 'male', birth_date: '1980-04-12' }],
    });
    renderAt();
    const btn = await screen.findByRole('button', { name: /\$evaluate/i });
    await userEvent.click(btn);
    expect(await screen.findByText('Smith, John')).toBeInTheDocument();
    expect(screen.getByText('1980-04-12')).toBeInTheDocument();
  });

  test('disables button while evaluating', async () => {
    enableAndOneGroup();
    let resolve;
    api.evaluateGroup = jest.fn().mockReturnValue(new Promise(r => { resolve = r; }));
    renderAt();
    const btn = await screen.findByRole('button', { name: /\$evaluate/i });
    await userEvent.click(btn);
    expect(btn).toBeDisabled();
    resolve({ group_id: 'g1', evaluated_at: 't', member_count: 0, members: [] });
  });

  test('renders OperationOutcome on error', async () => {
    enableAndOneGroup();
    const err = new Error('boom');
    err.body = {
      operation_outcome: {
        resourceType: 'OperationOutcome',
        issue: [{ severity: 'error', code: 'not-supported', diagnostics: 'No $evaluate here' }],
      },
    };
    api.evaluateGroup = jest.fn().mockRejectedValue(err);
    renderAt();
    await userEvent.click(await screen.findByRole('button', { name: /\$evaluate/i }));
    expect(await screen.findByText(/No \$evaluate here/i)).toBeInTheDocument();
  });

  test('shows zero-members state when evaluation returns empty member list', async () => {
    enableAndOneGroup();
    api.evaluateGroup = jest.fn().mockResolvedValue({
      group_id: 'g1', evaluated_at: 't', member_count: 0, members: [],
    });
    renderAt();
    await userEvent.click(await screen.findByRole('button', { name: /\$evaluate/i }));
    expect(await screen.findByText(/0 members/i)).toBeInTheDocument();
  });
});
```

- [ ] **Step 2: Run, confirm failure**

Run: `cd frontend && CI=true npm test -- --watchAll=false GroupsPage.test.js`
Expected: 4 new tests fail.

- [ ] **Step 3: Implement the evaluate flow**

Replace the body of `GroupsPage.js` (keep the `enabled` redirect + list-load logic above; replace from the JSX `return` down). The full updated file:

```javascript
import React, { useEffect, useState, useCallback } from 'react';
import { Navigate } from 'react-router-dom';
import { getAdminSettings, getEvaluatableGroups, evaluateGroup } from '../api/client';
import ErrorBanner from '../components/ErrorBanner';
import OperationOutcomeView from '../components/OperationOutcomeView';
import styles from './GroupsPage.module.css';

function MembersTable({ members }) {
  if (!members || members.length === 0) return null;
  return (
    <table className={styles.membersTable}>
      <thead>
        <tr><th>Name</th><th>ID</th><th>Gender</th><th>Birth date</th></tr>
      </thead>
      <tbody>
        {members.map(m => (
          <tr key={m.id} className={m.lookup_error ? styles.memberError : ''}>
            <td>{m.name ?? <span title={m.lookup_error}>(unavailable)</span>}</td>
            <td><code>{m.id}</code></td>
            <td>{m.gender ?? '--'}</td>
            <td>{m.birth_date ?? '--'}</td>
          </tr>
        ))}
      </tbody>
    </table>
  );
}

export default function GroupsPage() {
  const [enabled, setEnabled] = useState(null);
  const [groups, setGroups] = useState([]);
  const [loading, setLoading] = useState(true);
  const [listError, setListError] = useState(null);
  const [evalState, setEvalState] = useState({});  // {gid: 'running'|'complete'|'error'}
  const [results, setResults] = useState({});      // {gid: {members, evaluated_at, member_count}}
  const [errors, setErrors] = useState({});        // {gid: OperationOutcome | string}

  useEffect(() => {
    let cancelled = false;
    getAdminSettings()
      .then(s => { if (!cancelled) setEnabled(!!s.groups_enabled); })
      .catch(() => { if (!cancelled) setEnabled(false); });
    return () => { cancelled = true; };
  }, []);

  const loadGroups = useCallback(async () => {
    setLoading(true);
    setListError(null);
    try {
      const data = await getEvaluatableGroups();
      setGroups(data.groups || []);
    } catch (err) {
      setListError(err.message || 'Failed to load groups');
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => { if (enabled === true) loadGroups(); }, [enabled, loadGroups]);

  const handleEvaluate = useCallback(async (groupId) => {
    setEvalState(s => ({ ...s, [groupId]: 'running' }));
    setErrors(e => { const c = { ...e }; delete c[groupId]; return c; });
    try {
      const r = await evaluateGroup(groupId);
      setResults(prev => ({ ...prev, [groupId]: r }));
      setEvalState(s => ({ ...s, [groupId]: 'complete' }));
    } catch (err) {
      const oo = err?.body?.operation_outcome;
      setErrors(e => ({ ...e, [groupId]: oo || err.message || 'Evaluation failed' }));
      setEvalState(s => ({ ...s, [groupId]: 'error' }));
    }
  }, []);

  if (enabled === null) return <div className={styles.page} />;
  if (!enabled) return <Navigate to="/measures" replace />;

  return (
    <div className={styles.page}>
      <div className={styles.header}>
        <h1 className={styles.title}>Groups</h1>
        <button
          type="button"
          className={styles.refreshBtn}
          onClick={loadGroups}
          disabled={loading}
        >
          Refresh
        </button>
      </div>
      <p className={styles.subtitle}>
        Showing only Groups with a CQL <code>valueExpression</code>. Other Groups on the CDR are hidden.
      </p>

      {listError && <ErrorBanner message={listError} />}

      {!listError && !loading && groups.length === 0 && (
        <div className={styles.empty}>No CQL-evaluatable Groups found on this CDR.</div>
      )}

      <div className={styles.rows}>
        {groups.map(g => {
          const state = evalState[g.id] || 'idle';
          const result = results[g.id];
          const error = errors[g.id];
          const isOpen = state === 'complete' || state === 'error';
          return (
            <div key={g.id} className={styles.row} data-testid={`group-row-${g.id}`}>
              <div className={styles.rowTop}>
                <div className={styles.rowMain}>
                  <div className={styles.groupHeader}>
                    <span className={styles.groupName}>{g.name || g.id}</span>
                    <span className={styles.idChip}>{g.id}</span>
                    <span className={styles.langChip}>{g.expression_language}</span>
                  </div>
                  <code className={styles.expression}>{g.expression_preview}</code>
                </div>
                <button
                  type="button"
                  className={styles.evalBtn}
                  disabled={state === 'running'}
                  onClick={() => handleEvaluate(g.id)}
                >
                  {state === 'running'
                    ? 'Evaluating…'
                    : state === 'complete'
                    ? 'Re-evaluate'
                    : '$evaluate'}
                </button>
              </div>

              {isOpen && (
                <div className={styles.accordion}>
                  {state === 'complete' && result && (
                    <>
                      <div className={styles.accordionHeader}>
                        {result.member_count} {result.member_count === 1 ? 'member' : 'members'}
                        {result.evaluated_at && (
                          <span className={styles.evaluatedAt}> · evaluated {result.evaluated_at}</span>
                        )}
                      </div>
                      <MembersTable members={result.members} />
                    </>
                  )}
                  {state === 'error' && error && (
                    typeof error === 'object'
                      ? <OperationOutcomeView errorDetails={error} />
                      : <ErrorBanner message={error} />
                  )}
                </div>
              )}
            </div>
          );
        })}
      </div>
    </div>
  );
}
```

Append to `frontend/src/pages/GroupsPage.module.css`:

```css
.rowTop { display: flex; align-items: flex-start; justify-content: space-between; gap: 16px; width: 100%; }
.evalBtn { padding: 6px 12px; border-radius: 6px; border: 1px solid #0969da; background: #0969da; color: #fff; cursor: pointer; font-weight: 600; }
.evalBtn:disabled { opacity: 0.6; cursor: not-allowed; }
.accordion { margin-top: 12px; padding-top: 12px; border-top: 1px dashed #d0d7de; width: 100%; }
.accordionHeader { font-size: 13px; color: #57606a; margin-bottom: 8px; }
.evaluatedAt { font-style: italic; }
.membersTable { width: 100%; border-collapse: collapse; font-size: 13px; }
.membersTable th, .membersTable td { border-bottom: 1px solid #eaeef2; padding: 6px 8px; text-align: left; }
.memberError { opacity: 0.55; }
```

(Note: `.row` may need `flex-direction: column; align-items: stretch;` for the accordion to lay out below the row top. Replace its existing rules with:)

```css
.row { padding: 12px 16px; border: 1px solid #d0d7de; border-radius: 8px; background: #fff; display: flex; flex-direction: column; align-items: stretch; gap: 8px; }
```

- [ ] **Step 4: Run tests, confirm pass**

Run: `cd frontend && CI=true npm test -- --watchAll=false GroupsPage.test.js`
Expected: 8 tests pass (1 + 3 + 4).

- [ ] **Step 5: Commit**

```bash
git add frontend/src/pages/GroupsPage.js frontend/src/pages/GroupsPage.module.css frontend/src/pages/GroupsPage.test.js
git commit -m "feat(groups): \$evaluate button with accordion + OperationOutcome rendering (#322)"
```

---

## Task 11: Frontend — Register the route and nav item

**Files:**
- Modify: `frontend/src/App.js`

- [ ] **Step 1: Add the nav item below `Jobs`**

In `frontend/src/App.js`, update `ALL_NAV_ITEMS` (around line 18). Pick a Groups icon — if no specific icon exists, reuse `JobsIcon` for now; otherwise import a suitable one from `Icons.js`:

```javascript
const ALL_NAV_ITEMS = [
  { path: '/measures',   label: 'Measures',   Icon: MeasuresIcon,  kbd: 'M', feature: null },
  { path: '/jobs',       label: 'Jobs',        Icon: JobsIcon,      kbd: 'J', feature: null },
  { path: '/groups',     label: 'Groups',      Icon: JobsIcon,      kbd: 'G', feature: 'groups' },
  { path: '/results',    label: 'Results',     Icon: ResultsIcon,   kbd: 'E', feature: null },
  { path: '/validation', label: 'Validation',  Icon: ValidateIcon,  kbd: 'V', feature: 'validation' },
];
```

- [ ] **Step 2: Register the route**

In the `<Routes>` block (around line 323-330), add (alphabetical with existing routes, registered unconditionally):

```jsx
import GroupsPage from './pages/GroupsPage';
// ...
<Route path="/groups" element={<GroupsPage />} />
```

- [ ] **Step 3: Manual verification**

Run: `cd frontend && npm start` (port 3001). With backend running:

1. With Groups toggle **off** (default): no Groups tab in sidebar; visiting `http://localhost:3001/groups` redirects to `/measures`.
2. Toggle Groups **on** in Settings → Admin: sidebar gains "Groups" tab below "Jobs". Click it; page loads.
3. Press `G` from anywhere outside an input: navigates to `/groups`.
4. Toggle Groups **off** again: sidebar tab disappears; if `/groups` is the current page, it redirects to `/measures`.

If any check fails, return to the failing task and iterate.

- [ ] **Step 4: Commit**

```bash
git add frontend/src/App.js
git commit -m "feat(nav): register Groups route + sidebar entry below Jobs (#322)"
```

---

## Task 12: End-to-end smoke against a supporting CDR

**Files:** None modified — manual verification.

- [ ] **Step 1: Locate a supporting CDR**

Identify a CDR that implements `Group/<id>/$evaluate`. Candidates: a sandbox HAPI build with CQL IG support, or `cloud.alphora.com` (note: per CLAUDE.md, that server has the async-indexing bug — `Patient?_count=1000` warmup before $evaluate). Record the URL.

- [ ] **Step 2: Point Lenny at the supporting CDR**

Start the stack: `cp .env.example .env && docker compose up -d` (or use existing local stack). In Lenny, go to Settings → Connections → CDR → add or activate a connection pointing at the supporting CDR.

- [ ] **Step 3: Enable Groups in Settings → Admin**

Toggle Groups on. Verify the sidebar updates without a page reload.

- [ ] **Step 4: Verify the list**

Open `/groups`. Confirm:
- Description line is present
- Rows show only Groups carrying a `characteristicExpression` extension
- Expression preview and language chip render

- [ ] **Step 5: Verify successful evaluation**

Click `$evaluate` on a group whose CQL you know returns members. Confirm:
- Button shows "Evaluating…" and is disabled
- Row expands with member count, evaluated_at timestamp
- Members table shows name, id, gender, birth date

- [ ] **Step 6: Verify error path**

Switch the active CDR to the bundled HAPI (does not support `$evaluate`). Click `$evaluate` on a group. Confirm the row expands with an `OperationOutcomeView` showing the upstream FHIR error.

- [ ] **Step 7: Verify zero-members path**

Find or construct a Group whose CQL evaluates to an empty member list. Confirm: row expands and shows "0 members" without a table.

- [ ] **Step 8: Verify disabled-state once more**

Toggle Groups off. Confirm sidebar tab disappears and direct navigation to `/groups` redirects to `/measures`.

No commit. If issues surfaced, file follow-up commits against the corresponding task.

---

## Task 13: Lint, full pre-push suite, and finalize

**Files:** None modified.

- [ ] **Step 1: Full backend lint**

Run: `cd backend && ruff check app/ tests/ && ruff format --check app/ tests/`
Fix anything that surfaces.

- [ ] **Step 2: Full backend unit suite**

Run: `cd backend && python3 -m pytest tests/ --ignore=tests/integration -v`
Confirm all pass; investigate any unrelated failures before pushing.

- [ ] **Step 3: CI-equivalent integration suite (per CLAUDE.md pre-push checklist)**

Run:
```bash
./scripts/run-integration-tests.sh \
  --ignore=tests/integration/test_golden_measures.py \
  --ignore=tests/integration/test_connectathon_measures.py \
  --ignore=tests/integration/test_full_workflow.py \
  --ignore=tests/integration/test_groups_dropdown.py \
  --ignore=tests/integration/test_full_jobs_pipeline.py
```
Confirm pass. The Groups feature has no integration test, but the existing suite must remain green (no regressions in `/jobs/groups`).

- [ ] **Step 4: Frontend unit tests**

Run: `cd frontend && CI=true npm test -- --watchAll=false`
Confirm all pass.

- [ ] **Step 5: Push branch and open PR**

```bash
git push -u origin <branch>
gh pr create --title "feat: experimental Groups \$evaluate page (#322)" --body "$(cat <<'EOF'
## Summary
- Implements [#322](https://github.com/Bellese/Lenny/issues/322): admin-gated Groups page invoking `Group/<id>/$evaluate`.
- Architecturally independent of the Measure pipeline (asserted by `test_groups_independence.py`).
- Default `groups_enabled=false`; UI identical to main except for the new toggle row.

## Test plan
- [x] Backend lint clean
- [x] Backend unit tests pass (incl. new groups + independence tests)
- [x] CI-equivalent integration suite passes
- [x] Frontend unit tests pass
- [x] Manual smoke against supporting CDR (per Task 12)
- [x] Disabled-state redirect verified

🤖 Generated with [Claude Code](https://claude.com/claude-code)
EOF
)"
```

---

## Acceptance Criteria Coverage

| Criterion (from issue #322) | Task |
|---|---|
| Default `groups_enabled=false` → app identical to `main` except toggle row | Task 1 (default), Task 7 (toggle wiring), Task 11 (gated nav) |
| `groups_enabled=true` → Groups sidebar tab below Runs | Task 11 |
| `/groups` route registered unconditionally with disabled-state redirect | Task 11 (route), Task 8 (redirect) |
| Lists only Groups with `valueExpression` + guiding text | Task 2 (filter), Task 9 (description) |
| `$evaluate` invokes `Group/<id>/$evaluate` and expands row on success | Task 3 (helper), Task 4 (endpoint), Task 10 (UI) |
| OperationOutcome errors render inline on the row | Task 4 (pass-through), Task 10 (`OperationOutcomeView`) |
| No persistence | Component-local state in Task 10; no DB migration anywhere |
| No measure-pipeline imports | Task 5 (backend AST), Task 8 Step 5 (frontend source scan) |
