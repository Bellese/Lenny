"""Tests for the measure readiness check, sweep, and storage model."""

import pytest
import pytest_asyncio


class _SessionCtx:
    """Async-context wrapper that yields the test session without closing it."""

    def __init__(self, session):
        self._session = session

    async def __aenter__(self):
        return self._session

    async def __aexit__(self, *exc_info):
        return False


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
        test_session.add(MeasureReadiness(mcs_id=mcs_row.id, measure_id="NoVersion", state=ReadinessState.unknown))
    with pytest.raises(IntegrityError):
        await test_session.commit()
    await test_session.rollback()


def test_readiness_settings_have_defaults():
    from app.config import settings

    assert settings.READINESS_TIMEOUT_SECONDS == 60
    assert settings.READINESS_CONCURRENCY == 2


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
        MeasureReadiness(mcs_id=mcs_row.id, measure_id="CMS124", measure_version="1.0.000", state=ReadinessState.ready)
    )
    await test_session.commit()

    await reclaim_stranded_checks(test_session)

    rows = {r.measure_id: r for r in (await test_session.execute(select(MeasureReadiness))).scalars().all()}
    assert rows["CMS122"].state is ReadinessState.unknown
    assert rows["CMS122"].error == "Interrupted by backend restart"
    assert rows["CMS124"].state is ReadinessState.ready  # untouched


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
            {"type": "depends-on", "resource": "http://cts.nlm.nih.gov/fhir/ValueSet/vs-three"},
            {"type": "depends-on", "resource": "https://madie.cms.gov/Library/FHIRHelpers|4.4.000"},
        ],
    }
    assert extract_valueset_canonicals(library) == [
        "http://cts.nlm.nih.gov/fhir/ValueSet/vs-three",
        "http://vs/one",
        "http://vs/two",
    ]


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


def test_extract_valueset_canonicals_excludes_codesystem_canonicals():
    """relatedArtifact mixes Library, ValueSet and CodeSystem canonicals.

    LOINC and SNOMED are code systems, not ValueSet resources — searching a FHIR
    server for them as ValueSets can never match, so admitting them here would
    mark every real measure permanently not-ready.
    """
    from app.services.measure_readiness import extract_valueset_canonicals

    library = {
        "relatedArtifact": [
            {"type": "depends-on", "resource": "http://loinc.org"},
            {"type": "depends-on", "resource": "http://snomed.info/sct"},
            {"type": "depends-on", "resource": "http://www.ama-assn.org/go/cpt"},
            {"type": "depends-on", "resource": "http://terminology.hl7.org/CodeSystem/condition-clinical"},
            {
                "type": "depends-on",
                "resource": "http://cts.nlm.nih.gov/fhir/ValueSet/2.16.840.1.113883.3.464.1003.1003",
            },
        ],
    }
    assert extract_valueset_canonicals(library) == [
        "http://cts.nlm.nih.gov/fhir/ValueSet/2.16.840.1.113883.3.464.1003.1003"
    ]


def test_extract_valueset_canonicals_excludes_unlisted_code_systems():
    """A blocklist of known code systems fails open; the filter must be positive.

    HCPCS carries no `/CodeSystem/` path segment, so any exclusion-list approach
    admits it and the measure then reports a value set that can never be found.
    """
    from app.services.measure_readiness import extract_valueset_canonicals

    library = {
        "relatedArtifact": [
            {"type": "depends-on", "resource": "http://www.cms.gov/Medicare/Coding/HCPCSReleaseCodeSets"},
            {
                "type": "depends-on",
                "resource": "http://cts.nlm.nih.gov/fhir/ValueSet/2.16.840.1.113883.3.464.1003.1003",
            },
        ],
    }
    assert extract_valueset_canonicals(library) == [
        "http://cts.nlm.nih.gov/fhir/ValueSet/2.16.840.1.113883.3.464.1003.1003"
    ]


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


async def test_check_returns_unknown_when_the_body_is_json_but_not_an_object():
    """A 2xx body of `null` or a bare array parses fine but is not a Library.

    `check_measure_readiness` promises never to raise — Task 4's sweep stores
    whatever verdict comes back, so an escaping AttributeError leaves the row
    stuck in `checking` until a restart.
    """
    import httpx

    from app.models.measure_readiness import ReadinessState
    from app.services.measure_readiness import check_measure_readiness

    for body in (None, [], "not a library", 42):
        transport = httpx.MockTransport(lambda request, b=body: httpx.Response(200, json=b))
        verdict = await check_measure_readiness(
            "https://mcs.example.com/fhir", "CMS122", auth_headers={}, timeout=5.0, transport=transport
        )
        assert verdict.state is ReadinessState.unknown, f"body {body!r} produced {verdict.state}"
        assert verdict.error is not None


async def test_find_missing_valuesets_normalises_versioned_input():
    """Versions must be stripped on BOTH sides of the comparison.

    Otherwise a canonical passed in as `...|1.0.0` never matches the server's
    unversioned url and is reported missing while actually present.
    """
    import httpx

    from app.services.measure_readiness import find_missing_valuesets

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=_vs_bundle(["http://vs/a", "http://vs/b"]))

    missing = await find_missing_valuesets(
        "https://mcs.example.com/fhir",
        ["http://vs/a|1.0.0", "http://vs/b", "http://vs/c|2.0.0"],
        auth_headers={},
        timeout=5.0,
        transport=httpx.MockTransport(handler),
    )
    assert missing == ["http://vs/c"]


async def test_check_ignores_a_top_level_warning_only_outcome():
    """Drives _error_diagnostic's severity filter with a real warning outcome.

    The sibling `contained` test never reaches that loop, because from_response
    only parses a TOP-LEVEL OperationOutcome. Advisory outcomes must not go red.
    """
    import httpx

    from app.models.measure_readiness import ReadinessState
    from app.services.measure_readiness import check_measure_readiness

    warning_outcome = {
        "resourceType": "OperationOutcome",
        "issue": [
            {"severity": "warning", "code": "informational", "diagnostics": "advisory only"},
            {"severity": "information", "code": "informational", "diagnostics": "also advisory"},
        ],
    }

    def handler(request: httpx.Request) -> httpx.Response:
        if "$data-requirements" in str(request.url):
            return httpx.Response(200, json=warning_outcome)
        return httpx.Response(200, json=_vs_bundle([]))

    verdict = await check_measure_readiness(
        "https://mcs.example.com/fhir",
        "CMS122",
        auth_headers={},
        timeout=5.0,
        transport=httpx.MockTransport(handler),
    )
    assert verdict.state is not ReadinessState.not_ready


async def test_claim_unchecked_only_claims_measures_with_no_row(test_session, mcs_row):
    from app.models.measure_readiness import MeasureReadiness, ReadinessState
    from app.services.measure_readiness import claim_unchecked

    test_session.add(
        MeasureReadiness(mcs_id=mcs_row.id, measure_id="CMS122", measure_version="0.5.000", state=ReadinessState.ready)
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
    """Rows for a second, unrelated MCS must survive.

    `mcs_id` carries a real FK to `mcs_configs`, so the second row needs a real
    connection row of its own rather than an arbitrary id — a fabricated id
    would just fail the FK constraint the test never gets to exercise.
    """
    from sqlalchemy import select

    from app.models.connection_base import AuthType
    from app.models.mcs_config import MCSConfig
    from app.models.measure_readiness import MeasureReadiness, ReadinessState
    from app.services.measure_readiness import invalidate_mcs

    other_mcs = MCSConfig(
        name="Other MCS",
        mcs_url="https://other-mcs.example.com/fhir",
        auth_type=AuthType.none,
        is_active=False,
        is_default=False,
        is_read_only=False,
        request_timeout_seconds=30,
    )
    test_session.add(other_mcs)
    await test_session.commit()
    await test_session.refresh(other_mcs)

    test_session.add(
        MeasureReadiness(mcs_id=mcs_row.id, measure_id="CMS122", measure_version="1", state=ReadinessState.ready)
    )
    test_session.add(
        MeasureReadiness(mcs_id=other_mcs.id, measure_id="CMS122", measure_version="1", state=ReadinessState.ready)
    )
    await test_session.commit()

    removed = await invalidate_mcs(test_session, mcs_row.id)
    assert removed == 1
    remaining = (await test_session.execute(select(MeasureReadiness))).scalars().all()
    assert [r.mcs_id for r in remaining] == [other_mcs.id]


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
