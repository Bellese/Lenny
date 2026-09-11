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


async def test_startup_deletes_stranded_checking_rows_so_they_are_re_swept(test_session, mcs_row):
    """`asyncio.create_task` does not survive a restart.

    A container that dies mid-sweep leaves rows in `checking` forever, which
    renders as a spinner that never resolves. Startup must clear them.

    The row is DELETED, not marked `unknown` (final review I5b). `unknown` was
    terminal: `claim_unchecked` only claims measures with no row, so a row left
    behind at all — whatever its state — makes the measure read "Not checked"
    permanently after one deploy-time restart. Deleting is what lets the next
    page load re-claim and re-check it, which the second half of this test
    asserts directly.
    """
    from sqlalchemy import select

    from app.models.measure_readiness import MeasureReadiness, ReadinessState
    from app.services.measure_readiness import claim_unchecked, reclaim_stranded_checks

    test_session.add(
        MeasureReadiness(
            mcs_id=mcs_row.id, measure_id="CMS122", measure_version="0.5.000", state=ReadinessState.checking
        )
    )
    test_session.add(
        MeasureReadiness(mcs_id=mcs_row.id, measure_id="CMS124", measure_version="1.0.000", state=ReadinessState.ready)
    )
    await test_session.commit()

    reclaimed = await reclaim_stranded_checks(test_session)
    assert reclaimed == 1

    rows = {r.measure_id: r for r in (await test_session.execute(select(MeasureReadiness))).scalars().all()}
    assert "CMS122" not in rows
    assert rows["CMS124"].state is ReadinessState.ready  # a settled verdict is untouched

    # The point of deleting: the next page load re-claims the stranded measure
    # (and still leaves the settled one alone).
    claimed = await claim_unchecked(test_session, mcs_row.id, [("CMS122", "0.5.000"), ("CMS124", "1.0.000")])
    assert claimed == [("CMS122", "0.5.000")]


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


async def test_check_returns_not_ready_with_a_generic_message_when_there_is_no_parseable_outcome():
    """A non-2xx response that carries no OperationOutcome at all (plain text,
    a proxy's HTML error page, ...) must still produce `not_ready` with SOME
    message, not silently fall through with `error=None`.

    `_error_diagnostic` only fires when `from_response` parses a top-level
    OperationOutcome; a plain-text 500 body makes that None, and the generic
    `f"HTTP {status} from $data-requirements."` fallback is what covers it.
    """
    import httpx

    from app.models.measure_readiness import ReadinessState
    from app.services.measure_readiness import check_measure_readiness

    transport = httpx.MockTransport(lambda request: httpx.Response(500, text="Internal Server Error"))
    verdict = await check_measure_readiness(
        "https://mcs.example.com/fhir", "CMS122", auth_headers={}, timeout=5.0, transport=transport
    )
    assert verdict.state is ReadinessState.not_ready
    assert verdict.error == "HTTP 500 from $data-requirements."
    assert verdict.missing_libraries == []


async def test_check_returns_unknown_when_the_2xx_body_is_not_valid_json():
    """A 2xx body that is not even parseable JSON (a truncated response, an
    upstream proxy's HTML) must not raise out of `check_measure_readiness` —
    it promises never to — and must not silently fall through to `ready`
    either. `resp.json()` itself is what raises here, not merely returning the
    wrong shape (the `test_check_returns_unknown_when_the_body_is_json_but_not_an_object`
    sibling covers that case; this one covers the parse failure itself).
    """
    import httpx

    from app.models.measure_readiness import ReadinessState
    from app.services.measure_readiness import check_measure_readiness

    transport = httpx.MockTransport(lambda request: httpx.Response(200, content=b"not valid json{{{"))
    verdict = await check_measure_readiness(
        "https://mcs.example.com/fhir", "CMS122", auth_headers={}, timeout=5.0, transport=transport
    )
    assert verdict.state is ReadinessState.unknown
    assert verdict.error == "$data-requirements returned a body that is not JSON."


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


async def test_find_missing_valuesets_returns_empty_without_a_network_call_for_no_canonicals():
    """A Library with zero `dataRequirement`/`relatedArtifact` canonicals (a
    measure with no terminology dependencies at all) must not issue a
    `/ValueSet` request — there is nothing to look up, and a query with an
    empty `url=` filter is not "no filter", it is a different, wrong query.
    """
    import httpx

    from app.services.measure_readiness import find_missing_valuesets

    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(str(request.url))
        return httpx.Response(200, json=_vs_bundle([]))

    missing = await find_missing_valuesets(
        "https://mcs.example.com/fhir", [], auth_headers={}, timeout=5.0, transport=httpx.MockTransport(handler)
    )
    assert missing == []
    assert calls == []


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


def _paging_valueset_server(store: list[str]):
    """A `/ValueSet?url=a,b` mock that behaves the way a FHIR server actually does.

    Two properties matter and neither is present in `_vs_bundle` alone:

    * `url=a,b` is an OR match over *resources*, and each `(url, version)` pair
      is its own resource — so N requested canonicals can match more than N
      resources.
    * `_count` is a PAGE SIZE. Matches beyond it are not dropped, they are
      paged behind `link[relation=next]`.

    `store` holds versioned canonicals, as a server loaded from several MADiE
    bundles would.
    """
    import httpx

    def handler(request: httpx.Request) -> httpx.Response:
        params = request.url.params
        requested = [u for u in (params.get("url") or "").split(",") if u]
        offset = int(params.get("_offset") or 0)
        count = int(params.get("_count") or 20)
        matches = [u for u in store if u.split("|")[0] in requested]
        body = _vs_bundle(matches[offset : offset + count])
        body["total"] = len(matches)
        if offset + count < len(matches):
            body["link"] = [{"relation": "next", "url": str(request.url.copy_set_param("_offset", offset + count))}]
        return httpx.Response(200, json=body)

    return handler


async def test_find_missing_valuesets_tolerates_a_canonical_held_at_several_versions():
    """The page must be sized to the MATCHES, not to the number of canonicals.

    `_count=len(chunk)` truncated the OR match: here two canonicals are asked
    for and three resources match (one of them is on the server twice, at two
    versions), so a two-row page contains both copies of `a` and none of `b` —
    and `b`, which is PRESENT, is reported missing. That is a false `not_ready`
    with a wrong count in the message, on a completely ordinary server.
    """
    import httpx

    from app.services.measure_readiness import find_missing_valuesets

    store = ["http://vs/a|20210101", "http://vs/a|20230101", "http://vs/b|20220101"]
    handler = _paging_valueset_server(store)
    calls = []

    def counting(request: httpx.Request) -> httpx.Response:
        calls.append(str(request.url))
        return handler(request)

    missing = await find_missing_valuesets(
        "https://mcs.example.com/fhir",
        ["http://vs/a", "http://vs/b"],
        auth_headers={},
        timeout=5.0,
        transport=httpx.MockTransport(counting),
    )
    assert missing == []
    # Following `next` (tested separately below) makes the result correct even
    # with a too-small page, so the page size needs its own assertion or the
    # `_count` half of the fix is untested: a handful of versions of a handful
    # of canonicals must not cost one round trip per resource.
    assert len(calls) == 1, f"the page was sized to the chunk, not to the possible matches: {calls}"


async def test_find_missing_valuesets_follows_the_next_link():
    """Whatever the page size, a paged match must be followed to the end.

    26 resources match the two requested canonicals and only `b`'s single
    resource is past the page boundary, so an implementation that reads page
    one and stops reports a present value set as missing.
    """
    import httpx

    from app.services.measure_readiness import find_missing_valuesets

    store = [f"http://vs/a|v{i}" for i in range(25)] + ["http://vs/b|v1"]
    handler = _paging_valueset_server(store)
    calls = []

    def counting(request: httpx.Request) -> httpx.Response:
        calls.append(str(request.url))
        return handler(request)

    missing = await find_missing_valuesets(
        "https://mcs.example.com/fhir",
        ["http://vs/a", "http://vs/b"],
        auth_headers={},
        timeout=5.0,
        chunk_size=2,
        transport=httpx.MockTransport(counting),
    )
    assert missing == []
    assert len(calls) == 2, f"expected a second page to be fetched, got {calls}"


async def test_find_missing_valuesets_stops_following_next_links_eventually():
    """The `next`-following loop needs a stop, or a misbehaving server hangs it.

    A server whose `next` link points back at itself would otherwise spin
    inside a check that is supposed to answer within the readiness timeout.

    (Pre-existing test, expectation deliberately changed.) It used to assert
    that the loop fell out of the budget and RETURNED `["http://vs/a"]` — i.e.
    reported every canonical on the unread remainder as missing, which the
    caller turns into `not_ready`. That is verbatim the harm the sibling exit
    from this same loop refuses in `UnsafePaginationLinkError`'s docstring: a
    false `not_ready` tells an operator their working server is broken. The
    budget exists to stop the spin, not to license a wrong answer, so the cap
    must raise and be mapped to `unknown`. The stop itself is still pinned —
    the call count assertion is unchanged.
    """
    import httpx

    from app.services.measure_readiness import (
        _VALUESET_MAX_PAGES,
        PaginationBudgetExceededError,
        find_missing_valuesets,
    )

    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(str(request.url))
        body = _vs_bundle([])
        body["link"] = [{"relation": "next", "url": str(request.url)}]
        return httpx.Response(200, json=body)

    with pytest.raises(PaginationBudgetExceededError):
        await find_missing_valuesets(
            "https://mcs.example.com/fhir",
            ["http://vs/a"],
            auth_headers={},
            timeout=5.0,
            transport=httpx.MockTransport(handler),
        )
    assert len(calls) == _VALUESET_MAX_PAGES


async def test_check_returns_unknown_not_not_ready_when_the_page_budget_runs_out():
    """End-to-end counterpart: an exhausted page budget is `unknown`.

    The server here HOLDS both value sets — the second is only ever visible on
    a page the budget never reaches — so truncating the walk and reporting the
    gap would mark a working server `not_ready` with a present value set listed
    missing.
    """
    import httpx

    from app.models.measure_readiness import ReadinessState
    from app.services.measure_readiness import check_measure_readiness

    def handler(request: httpx.Request) -> httpx.Response:
        if "$data-requirements" in str(request.url):
            return httpx.Response(200, json=_dr_library(["http://vs/a", "http://vs/b"]))
        body = _vs_bundle(["http://vs/a"])
        body["link"] = [{"relation": "next", "url": str(request.url)}]
        return httpx.Response(200, json=body)

    verdict = await check_measure_readiness(
        "https://mcs.example.com/fhir",
        "CMS122",
        auth_headers={},
        timeout=5.0,
        transport=httpx.MockTransport(handler),
    )
    assert verdict.state is ReadinessState.unknown
    assert verdict.missing_valuesets == []
    assert "incomplete" in (verdict.error or "")


async def test_find_missing_valuesets_accepts_a_next_link_naming_the_default_port():
    """`https://h` and `https://h:443` are ONE origin.

    HAPI builds paging links from its configured `server_address`, which
    routinely carries an explicit port the operator never typed into Lenny's
    connection URL. A raw port comparison rejects that link and the whole check
    collapses to `unknown` against a server that is working perfectly.
    """
    import httpx

    from app.services.measure_readiness import find_missing_valuesets

    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(str(request.url))
        if "_getpages" in str(request.url):
            return httpx.Response(200, json=_vs_bundle(["http://vs/b"]))
        body = _vs_bundle(["http://vs/a"])
        body["link"] = [{"relation": "next", "url": "https://mcs.example.com:443/fhir?_getpages=abc"}]
        return httpx.Response(200, json=body)

    missing = await find_missing_valuesets(
        "https://mcs.example.com/fhir",
        ["http://vs/a", "http://vs/b"],
        auth_headers={},
        timeout=5.0,
        transport=httpx.MockTransport(handler),
    )
    assert missing == [], f"the default-port link was not followed; calls={calls}"
    assert len(calls) == 2


async def test_find_missing_valuesets_follows_a_relative_next_link():
    """A relative `next` is legal FHIR and must be resolved, not rejected.

    `urlparse("/fhir?_getpages=...")` has scheme `''` and hostname `None`, so
    an origin check applied to the raw string sees a mismatch and reports
    `unknown` on a healthy server.
    """
    import httpx

    from app.services.measure_readiness import find_missing_valuesets

    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(str(request.url))
        if "_getpages" in str(request.url):
            return httpx.Response(200, json=_vs_bundle(["http://vs/b"]))
        body = _vs_bundle(["http://vs/a"])
        body["link"] = [{"relation": "next", "url": "/fhir?_getpages=abc"}]
        return httpx.Response(200, json=body)

    missing = await find_missing_valuesets(
        "https://mcs.example.com/fhir",
        ["http://vs/a", "http://vs/b"],
        auth_headers={},
        timeout=5.0,
        transport=httpx.MockTransport(handler),
    )
    assert missing == [], f"the relative link was not followed; calls={calls}"
    assert calls[1] == "https://mcs.example.com/fhir?_getpages=abc"


async def test_find_missing_valuesets_rejects_a_protocol_relative_next_link():
    """The bypass that resolving relative links would open if the check moved.

    `//evil.example/ValueSet` has no scheme and no hostname of its own, so a
    naive "it's relative, therefore it's ours" shortcut admits it — and
    `urljoin` then resolves it to `https://evil.example/ValueSet`, a fully
    off-origin host that would be fetched WITH the MCS credentials attached.
    The origin check must run AFTER the join, never instead of it.
    """
    import httpx

    from app.services.measure_readiness import UnsafePaginationLinkError, find_missing_valuesets

    off_origin_calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        if "evil.example" in str(request.url):
            off_origin_calls.append(request)
            return httpx.Response(200, json=_vs_bundle(["http://vs/b"]))
        body = _vs_bundle(["http://vs/a"])
        body["link"] = [{"relation": "next", "url": "//evil.example/ValueSet?_offset=1"}]
        return httpx.Response(200, json=body)

    with pytest.raises(UnsafePaginationLinkError):
        await find_missing_valuesets(
            "https://mcs.example.com/fhir",
            ["http://vs/a", "http://vs/b"],
            auth_headers={"Authorization": "Bearer super-secret-token"},
            timeout=5.0,
            transport=httpx.MockTransport(handler),
        )

    assert off_origin_calls == [], "the protocol-relative link was resolved and then followed"


async def test_find_missing_valuesets_rejects_a_next_link_with_an_unparseable_port():
    """A bad port must be an SSRF REJECTION, not an escaping `ValueError`.

    `urlparse("https://host:99999/").port` raises. Unhandled, that escapes the
    origin guard into the caller's generic handler and is reported as a network
    failure, so the operator never learns a link was refused — and the one
    branch that is supposed to say "we refused to follow this" is bypassed by
    the very input most likely to be hostile.
    """
    import httpx

    from app.services.measure_readiness import UnsafePaginationLinkError, find_missing_valuesets

    def handler(request: httpx.Request) -> httpx.Response:
        body = _vs_bundle(["http://vs/a"])
        body["link"] = [{"relation": "next", "url": "https://mcs.example.com:99999/fhir?_getpages=abc"}]
        return httpx.Response(200, json=body)

    with pytest.raises(UnsafePaginationLinkError):
        await find_missing_valuesets(
            "https://mcs.example.com/fhir",
            ["http://vs/a", "http://vs/b"],
            auth_headers={},
            timeout=5.0,
            transport=httpx.MockTransport(handler),
        )


def test_same_origin_normalises_default_ports_and_still_rejects_other_origins():
    """The shared guard, exercised directly on both directions of the fix."""
    from app.services.fhir_client import _same_origin

    assert _same_origin("https://mcs.example.com/fhir", "https://mcs.example.com:443/fhir?page=2")
    assert _same_origin("https://mcs.example.com:443/fhir", "https://mcs.example.com/fhir?page=2")
    assert _same_origin("http://localhost:8080/fhir", "http://localhost:8080/fhir?page=2")
    assert _same_origin("http://localhost/fhir", "http://localhost:80/fhir?page=2")

    # Still closed: a different host, a different scheme, a real port change,
    # and a port that cannot be parsed at all.
    assert not _same_origin("https://mcs.example.com/fhir", "https://evil.example/fhir")
    assert not _same_origin("https://mcs.example.com/fhir", "http://mcs.example.com/fhir")
    assert not _same_origin("https://mcs.example.com/fhir", "https://mcs.example.com:8443/fhir")
    assert not _same_origin("https://mcs.example.com/fhir", "https://mcs.example.com:99999/fhir")
    assert not _same_origin("https://mcs.example.com/fhir", "https://mcs.example.com:notaport/fhir")


async def test_find_missing_valuesets_rejects_an_off_origin_next_link():
    """The SSRF this is guarding against: a hostile/misconfigured MCS returns a
    `next` link pointing at a different host than the one Lenny was asked to
    query. Following it would hand that host `auth_headers` — the MCS
    connection's bearer/basic credentials.

    A truncated page walk cannot just stop and return whatever it has seen:
    `find_missing_valuesets`'s result is a MISSING list, so silently stopping
    would report every canonical on the unread remainder (here, the one that
    only the second, off-origin page would have confirmed present) as absent —
    a false `not_ready`. Raising instead, and letting the caller map that to
    `unknown`, is the shape this codebase already uses for a transport failure
    on this same call.
    """
    import httpx

    from app.services.measure_readiness import UnsafePaginationLinkError, find_missing_valuesets

    internal_calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        if "internal-metadata.evil.example" in str(request.url):
            internal_calls.append(request)
            return httpx.Response(200, json=_vs_bundle(["http://vs/b"]))
        body = _vs_bundle(["http://vs/a"])
        body["link"] = [{"relation": "next", "url": "http://internal-metadata.evil.example/ValueSet?_offset=1"}]
        return httpx.Response(200, json=body)

    with pytest.raises(UnsafePaginationLinkError):
        await find_missing_valuesets(
            "https://mcs.example.com/fhir",
            ["http://vs/a", "http://vs/b"],
            auth_headers={"Authorization": "Bearer super-secret-token"},
            timeout=5.0,
            transport=httpx.MockTransport(handler),
        )

    # The off-origin host must never have been reached — the guard has to
    # fire BEFORE the credentialed request, not merely be noted afterwards.
    assert internal_calls == []


async def test_check_returns_unknown_not_not_ready_when_the_next_link_points_off_origin():
    """End-to-end: `check_measure_readiness` must not turn an SSRF rejection
    into a false `not_ready`. The measure server here holds BOTH value sets —
    `b` only becomes visible on the (rejected) second page — so a version of
    this code that stopped paging and reported the gap would call this
    `not_ready` with `http://vs/b` listed missing, even though it is present.
    `unknown` is the only verdict that is honest about what happened.
    """
    import httpx

    from app.models.measure_readiness import ReadinessState
    from app.services.measure_readiness import check_measure_readiness

    def handler(request: httpx.Request) -> httpx.Response:
        if "$data-requirements" in str(request.url):
            return httpx.Response(200, json=_dr_library(["http://vs/a", "http://vs/b"]))
        body = _vs_bundle(["http://vs/a"])
        body["link"] = [{"relation": "next", "url": "http://internal-metadata.evil.example/ValueSet?_offset=1"}]
        return httpx.Response(200, json=body)

    verdict = await check_measure_readiness(
        "https://mcs.example.com/fhir",
        "CMS122",
        auth_headers={},
        timeout=5.0,
        transport=httpx.MockTransport(handler),
    )
    assert verdict.state is ReadinessState.unknown
    assert verdict.missing_valuesets == []
    assert "different origin" in (verdict.error or "") or "origin" in (verdict.error or "")


@pytest.mark.parametrize(
    "mcs_url",
    [
        "http://10.0.2.15:8080/fhir",
        "http://mcs.internal.corp/fhir",
        "https://user:pass@mcs.example.com/fhir",
    ],
)
async def test_the_pagination_rejection_error_does_not_publish_the_configured_url(mcs_url):
    """This string is persisted and rendered, so it must not carry the MCS URL.

    `sanitize_url` only redacts DOTLESS hosts, so an internal IP or an internal
    corporate domain passes through it intact — which is exactly what
    `routes/measures.py` refuses to do on its 200 path ("Identity only —
    deliberately no `url`"). The operator already knows which connection they
    are looking at, so naming it buys nothing and leaks the rest.
    """
    import httpx

    from app.models.measure_readiness import ReadinessState
    from app.services.measure_readiness import check_measure_readiness

    def handler(request: httpx.Request) -> httpx.Response:
        if "$data-requirements" in str(request.url):
            return httpx.Response(200, json=_dr_library(["http://vs/a"]))
        body = _vs_bundle([])
        body["link"] = [{"relation": "next", "url": "http://internal-metadata.evil.example/ValueSet?_offset=1"}]
        return httpx.Response(200, json=body)

    verdict = await check_measure_readiness(
        mcs_url,
        "CMS122",
        auth_headers={},
        timeout=5.0,
        transport=httpx.MockTransport(handler),
    )
    assert verdict.state is ReadinessState.unknown
    error = verdict.error or ""
    for leaked in ("10.0.2.15", "internal.corp", "user:pass", "mcs.example.com"):
        assert leaked not in error, f"{leaked!r} leaked into a stored, rendered error: {error!r}"


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


async def test_check_returns_unknown_for_a_top_level_warning_only_outcome():
    """Drives _error_diagnostic's severity filter with a real warning outcome.

    The sibling `contained` test never reaches that loop, because from_response
    only parses a TOP-LEVEL OperationOutcome. An advisory outcome must not go
    red — but it must not go GREEN either, which is what this body did before
    the final review's C1 fix: an OperationOutcome carries no `dataRequirement`
    and no `relatedArtifact`, so it yielded zero canonicals, zero missing, and
    fell through to `ready`. A body that is not a Library tells us nothing
    about the measure, so the answer is `unknown`.

    (Pre-existing test, expectation deliberately changed: it asserted
    `is not not_ready`, which still passes after C1 but for the wrong reason.)
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
    assert verdict.state is ReadinessState.unknown
    assert verdict.error == "$data-requirements did not return a Library resource."


@pytest.mark.parametrize(
    "body,label",
    [
        (
            {
                "resourceType": "OperationOutcome",
                "issue": [{"severity": "information", "code": "informational", "diagnostics": "all good"}],
            },
            "information-only OperationOutcome",
        ),
        ({"resourceType": "OperationOutcome"}, "OperationOutcome with no issue array"),
        (
            {
                "resourceType": "Parameters",
                "parameter": [{"name": "return", "resource": {"resourceType": "Library"}}],
            },
            "Parameters wrapper around the Library",
        ),
        ({"message": "ok", "status": "success"}, "a gateway's own JSON envelope"),
    ],
)
async def test_check_returns_unknown_when_the_body_is_not_a_library(body, label):
    """A dict is not proof of a Library, and a false READY is the worst output.

    Each of these is a 2xx dict with no `dataRequirement` and no
    `relatedArtifact`: before C1 every one of them produced zero canonicals,
    zero missing value sets, and therefore `ready` — telling the user a measure
    will evaluate when nothing was ever verified. None of them require a
    non-compliant server; the Parameters wrapper in particular is a common
    variation, and this feature exists precisely because the target servers are
    third-party.
    """
    import httpx

    from app.models.measure_readiness import ReadinessState
    from app.services.measure_readiness import check_measure_readiness

    transport = httpx.MockTransport(lambda request: httpx.Response(200, json=body))
    verdict = await check_measure_readiness(
        "https://mcs.example.com/fhir", "CMS122", auth_headers={}, timeout=5.0, transport=transport
    )
    assert verdict.state is ReadinessState.unknown, f"{label} produced {verdict.state}"
    assert verdict.error == "$data-requirements did not return a Library resource."


@pytest.mark.parametrize(
    "body,label",
    [
        ({"resourceType": "Library", "id": "x", "status": "active"}, "a bare Library with no dependency fields"),
        (
            {"resourceType": "Library", "id": "x", "status": "active", "dataRequirement": [], "relatedArtifact": []},
            "a Library declaring both fields empty",
        ),
        (
            {
                "resourceType": "Library",
                "id": "effective-data-requirements",
                "status": "active",
                "type": {"coding": [{"code": "module-definition"}]},
                "dataRequirement": [],
                "relatedArtifact": [],
            },
            "a module-definition Library with empty arrays",
        ),
    ],
)
async def test_check_returns_unknown_when_the_library_declares_no_dependencies(body, label):
    """The false READY that survived the `resourceType` guard.

    Each body IS a Library, so the `resourceType != "Library"` check waves it
    through — and then it yields zero canonicals, `find_missing_valuesets`
    short-circuits on the empty list WITHOUT ONE NETWORK CALL, `missing` is
    falsy, and the function returned `ready`. A green verdict produced without
    a single byte of evidence about the measure is the worst output this
    feature can emit, and the mundane producers are all real: a Measure with no
    `library` element, a Library whose content attachment is empty or went
    unparsed, a gateway serving a cached stub.

    The third case is the reason a "check the profile/type" fix is not enough
    on its own: it is correctly typed `module-definition` and still declares
    nothing. `$data-requirements` exists to return the dependency closure, so a
    response that declares none is telling us nothing rather than telling us
    there is nothing to check.
    """
    import httpx

    from app.models.measure_readiness import ReadinessState
    from app.services.measure_readiness import check_measure_readiness

    valueset_calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        if "$data-requirements" in str(request.url):
            return httpx.Response(200, json=body)
        valueset_calls.append(str(request.url))
        return httpx.Response(200, json=_vs_bundle([]))

    verdict = await check_measure_readiness(
        "https://mcs.example.com/fhir",
        "CMS122",
        auth_headers={},
        timeout=5.0,
        transport=httpx.MockTransport(handler),
    )
    assert verdict.state is ReadinessState.unknown, f"{label} produced {verdict.state}"
    assert "declares no data requirements" in (verdict.error or "")
    assert valueset_calls == [], "nothing was ever verified, so there was nothing to be `ready` about"


@pytest.mark.parametrize("status", [301, 302, 303, 307, 308])
async def test_check_returns_unknown_on_a_redirect_not_not_ready(status):
    """A 3xx is not evidence about the measure.

    `follow_redirects` is off by design, so a redirect arrives here intact and
    used to fall into the generic non-2xx branch — marking EVERY measure on the
    connection `not_ready` because a proxy in front of the MCS upgrades http to
    https or normalises a trailing slash. That tells the operator their
    measures are broken when the only thing wrong is a URL.
    """
    import httpx

    from app.models.measure_readiness import ReadinessState
    from app.services.measure_readiness import check_measure_readiness

    transport = httpx.MockTransport(
        lambda request: httpx.Response(status, headers={"location": "https://elsewhere.example/fhir"})
    )
    verdict = await check_measure_readiness(
        "https://mcs.example.com/fhir", "CMS122", auth_headers={}, timeout=5.0, transport=transport
    )
    assert verdict.state is ReadinessState.unknown, f"HTTP {status} produced {verdict.state}"
    assert verdict.missing_libraries == []
    assert str(status) in (verdict.error or "")


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
    """Claims first, exactly as `GET /measures` does, then sweeps.

    The claim is not decoration in this test: `_store_verdict` is update-only
    (final review I3), so a sweep whose rows were never claimed writes nothing.
    That IS production's sequence — `claim_unchecked` or `mark_all_checking`
    always runs synchronously before the task is spawned.
    """
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

    measures = [("CMS122", "0.5.000"), ("CMS124", "1.0.000")]
    assert await svc.claim_unchecked(test_session, mcs_row.id, measures) == measures
    await svc.run_sweep(mcs_row.id, measures)

    rows = {r.measure_id: r for r in (await test_session.execute(select(MeasureReadiness))).scalars().all()}
    assert rows["CMS122"].state is ReadinessState.not_ready
    assert rows["CMS122"].missing_libraries == ["Status 1.15.000"]
    assert rows["CMS122"].checked_at is not None
    assert rows["CMS124"].state is ReadinessState.ready


async def test_run_sweep_respects_the_concurrency_cap(test_session, mcs_row, monkeypatch):
    """fhir_client.py:371-375 records this operation OOM-killing the engine.

    Both bounds are asserted, and `READINESS_CONCURRENCY` is moved off its
    default first. `peak <= cap` alone cannot fail in the direction it claims:
    a regression that serialised the sweep (semaphore hardcoded to 1) or one
    that ignored the setting entirely would sail past it. Pinning `peak ==
    settings.READINESS_CONCURRENCY` at a non-default value catches both — the
    sweep must actually REACH the configured cap, and must read it from config.
    """
    import asyncio

    import app.services.measure_readiness as svc
    from app.config import settings
    from app.models.measure_readiness import ReadinessState

    monkeypatch.setattr(settings, "READINESS_CONCURRENCY", 3)

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

    measures = [(f"CMS{i}", "1.0.0") for i in range(8)]
    await svc.claim_unchecked(test_session, mcs_row.id, measures)
    await svc.run_sweep(mcs_row.id, measures)
    assert peak == settings.READINESS_CONCURRENCY


async def test_run_sweep_leaves_no_row_stuck_in_checking_when_a_check_explodes(test_session, mcs_row, monkeypatch):
    """A raising check must not leave a spinner that only a restart clears."""
    from sqlalchemy import select

    import app.services.measure_readiness as svc
    from app.models.measure_readiness import MeasureReadiness, ReadinessState

    async def fake_check(mcs_url, measure_id, **kwargs):
        raise RuntimeError("boom")

    monkeypatch.setattr(svc, "check_measure_readiness", fake_check)
    monkeypatch.setattr(svc, "_session_factory", lambda: _SessionCtx(test_session))

    await svc.claim_unchecked(test_session, mcs_row.id, [("CMS122", "0.5.000")])
    await svc.run_sweep(mcs_row.id, [("CMS122", "0.5.000")])

    rows = (await test_session.execute(select(MeasureReadiness))).scalars().all()
    assert len(rows) == 1
    assert rows[0].state is ReadinessState.unknown
    assert "boom" in (rows[0].error or "")


async def test_run_sweep_does_not_resurrect_a_verdict_invalidated_mid_sweep(test_session, mcs_row, monkeypatch):
    """The workflow this feature was built for must not leave a sticky red.

    User sees `not_ready` -> uploads the missing Library -> `invalidate_mcs`
    deletes every row for the connection. If the sweep that was still running
    from the page load re-INSERTS its pre-upload verdict, that stale red is
    permanent: verdicts have no TTL and `claim_unchecked` only claims measures
    with no row, so no reload ever re-checks it.

    The delete is performed from inside the fake check — i.e. after the sweep
    started and before its verdict is written — which is exactly the window the
    real race occupies.
    """
    from sqlalchemy import select

    import app.services.measure_readiness as svc
    from app.models.measure_readiness import MeasureReadiness, ReadinessState

    async def fake_check(mcs_url, measure_id, **kwargs):
        await svc.invalidate_mcs(test_session, mcs_row.id)
        return svc.ReadinessVerdict(state=ReadinessState.not_ready, error="stale pre-upload verdict")

    monkeypatch.setattr(svc, "check_measure_readiness", fake_check)
    monkeypatch.setattr(svc, "_session_factory", lambda: _SessionCtx(test_session))

    await svc.claim_unchecked(test_session, mcs_row.id, [("CMS122", "0.5.000")])
    await svc.run_sweep(mcs_row.id, [("CMS122", "0.5.000")])

    rows = (await test_session.execute(select(MeasureReadiness))).scalars().all()
    assert rows == [], f"a deleted verdict was resurrected by the in-flight sweep: {[r.state for r in rows]}"

    # And because no row survives, the next page load re-claims and re-checks.
    assert await svc.claim_unchecked(test_session, mcs_row.id, [("CMS122", "0.5.000")]) == [("CMS122", "0.5.000")]


async def test_run_sweep_isolates_one_failing_write_from_the_other_measures(test_session, mcs_row, monkeypatch):
    """One DB fault must not strand every other row in `checking` forever.

    `_store_verdict` was outside the `try` and `gather` had no
    `return_exceptions`, so a single write failure propagated out of `gather`,
    closed the shared session while sibling tasks were still using it, and left
    their rows spinning with nothing able to recover them (`claim_unchecked`
    skips rows that exist, so no later page load re-kicks them).

    The measure whose own write fails necessarily keeps its `checking` row —
    nothing can write a verdict when the write is what is broken — so this
    asserts precisely that: exactly one row stranded, the one that failed.
    """
    from sqlalchemy import select

    import app.services.measure_readiness as svc
    from app.models.measure_readiness import MeasureReadiness, ReadinessState

    async def fake_check(mcs_url, measure_id, **kwargs):
        return svc.ReadinessVerdict(state=ReadinessState.ready, duration_ms=7)

    real_store = svc._store_verdict

    async def flaky_store(session, mcs_id, measure_id, version, verdict):
        if measure_id == "CMS124":
            raise RuntimeError("connection pool exhausted")
        return await real_store(session, mcs_id, measure_id, version, verdict)

    monkeypatch.setattr(svc, "check_measure_readiness", fake_check)
    monkeypatch.setattr(svc, "_store_verdict", flaky_store)
    monkeypatch.setattr(svc, "_session_factory", lambda: _SessionCtx(test_session))

    measures = [("CMS122", "0.5.000"), ("CMS124", "1.0.000"), ("CMS125", "2.0.000")]
    await svc.claim_unchecked(test_session, mcs_row.id, measures)

    await svc.run_sweep(mcs_row.id, measures)  # must not raise: run_sweep promises that

    rows = {r.measure_id: r for r in (await test_session.execute(select(MeasureReadiness))).scalars().all()}
    assert rows["CMS122"].state is ReadinessState.ready
    assert rows["CMS125"].state is ReadinessState.ready
    assert [mid for mid, r in rows.items() if r.state is ReadinessState.checking] == ["CMS124"]


async def test_run_sweep_updates_the_claimed_checking_row_in_place(test_session, mcs_row, monkeypatch):
    """The production path: claim_unchecked inserts `checking`, the sweep updates THAT row.

    Every other sweep test starts from an empty table and so only exercises the
    insert branch. If _store_verdict inserted a second row instead of updating,
    Task 1's unique constraint on (mcs_id, measure_id, measure_version) would
    raise IntegrityError here.
    """
    from sqlalchemy import select

    import app.services.measure_readiness as svc
    from app.models.measure_readiness import MeasureReadiness, ReadinessState

    async def fake_check(mcs_url, measure_id, **kwargs):
        return svc.ReadinessVerdict(state=ReadinessState.ready, duration_ms=1234)

    monkeypatch.setattr(svc, "check_measure_readiness", fake_check)
    monkeypatch.setattr(svc, "_session_factory", lambda: _SessionCtx(test_session))

    claimed = await svc.claim_unchecked(test_session, mcs_row.id, [("CMS122", "0.5.000")])
    assert claimed == [("CMS122", "0.5.000")]

    rows = (await test_session.execute(select(MeasureReadiness))).scalars().all()
    assert len(rows) == 1 and rows[0].state is ReadinessState.checking

    await svc.run_sweep(mcs_row.id, claimed)

    rows = (await test_session.execute(select(MeasureReadiness))).scalars().all()
    assert len(rows) == 1, f"expected the claimed row to be updated in place, got {len(rows)} rows"
    assert rows[0].state is ReadinessState.ready
    assert rows[0].duration_ms == 1234
    assert rows[0].checked_at is not None


async def test_run_sweep_skips_quietly_when_the_mcs_row_is_gone(test_session, monkeypatch):
    """The MCS can be deleted between `claim_unchecked` claiming a row and the
    detached sweep actually running (a fast follow-up delete in Settings).
    `run_sweep` must not raise and must not touch the database it can no
    longer resolve a URL or credentials for — it just logs and returns,
    leaving the caller's session and any claimed rows untouched.
    """
    import app.services.measure_readiness as svc

    monkeypatch.setattr(svc, "_session_factory", lambda: _SessionCtx(test_session))

    # No MCSConfig row with this id exists at all — simulates the deleted
    # connection without needing a real delete-then-cascade dance.
    await svc.run_sweep(999999, [("CMS122", "0.5.000")])  # must not raise

    from sqlalchemy import select

    from app.models.measure_readiness import MeasureReadiness

    rows = (await test_session.execute(select(MeasureReadiness))).scalars().all()
    assert rows == []


async def test_run_sweep_records_unknown_when_credential_resolution_fails(test_session, mcs_row, monkeypatch):
    """A broken credential must not crash the sweep — and must not ANONYMISE it.

    (Pre-existing test, expectation deliberately changed.) It used to assert
    that the sweep continued with `auth_headers == {}` and stored a `ready`
    row. Both halves were wrong in the same way: every verdict in the sweep is
    derived from whatever view of the server the headers buy, so continuing
    anonymously measures the ANONYMOUS view and then files the result as though
    it described the user's connection. Against a HAPI that permits
    unauthenticated reads — the ordinary connectathon setup — that yields
    entirely plausible `ready` rows for a dataset the user's credentials would
    never have been shown. `resolve_mcs_auth_headers` raises instead of
    degrading for precisely this reason.

    The credential failure is Lenny's, not the measure's, so the honest verdict
    is `unknown` for every measure in the sweep. Written rather than left
    spinning: `claim_unchecked` skips measures that already have a row, so a
    `checking` row nobody fills in is a spinner until restart. The original
    test's real content — "must not raise", "must not abandon rows" — is kept.
    """
    from sqlalchemy import select

    import app.services.measure_readiness as svc
    from app.models.measure_readiness import MeasureReadiness, ReadinessState

    async def broken_auth(*args, **kwargs):
        raise RuntimeError("token endpoint unreachable at https://tok.example/oauth?secret=abc")

    checked = []

    async def fake_check(mcs_url, measure_id, *, auth_headers, **kwargs):
        checked.append((measure_id, auth_headers))
        return svc.ReadinessVerdict(state=ReadinessState.ready)

    monkeypatch.setattr("app.dependencies.resolve_mcs_auth_headers", broken_auth)
    monkeypatch.setattr(svc, "check_measure_readiness", fake_check)
    monkeypatch.setattr(svc, "_session_factory", lambda: _SessionCtx(test_session))

    measures = [("CMS122", "0.5.000"), ("CMS124", "1.0.000")]
    await svc.claim_unchecked(test_session, mcs_row.id, measures)
    await svc.run_sweep(mcs_row.id, measures)  # must not raise

    assert checked == [], "the sweep queried the measure server without the credentials it was told to use"
    rows = (await test_session.execute(select(MeasureReadiness))).scalars().all()
    assert len(rows) == 2
    for row in rows:
        assert row.state is ReadinessState.unknown, "an unauthenticated view must never be filed as a verdict"
        assert "credentials" in (row.error or "")
        # `sanitize_error`, not `{exc}`: a token-endpoint failure's raw text
        # carries the URL and whatever is embedded in it.
        assert "secret=abc" not in (row.error or "")


async def test_mark_all_checking_resets_existing_verdicts_and_adds_missing_rows(test_session, mcs_row):
    """The manual re-check path: every listed measure ends up `checking`.

    Covers both halves — an existing ready/not_ready verdict is discarded, and a
    measure with no row at all gains one.
    """
    from sqlalchemy import select

    from app.models.measure_readiness import MeasureReadiness, ReadinessState
    from app.services.measure_readiness import mark_all_checking

    test_session.add(
        MeasureReadiness(mcs_id=mcs_row.id, measure_id="CMS122", measure_version="0.5.000", state=ReadinessState.ready)
    )
    await test_session.commit()

    await mark_all_checking(test_session, mcs_row.id, [("CMS122", "0.5.000"), ("CMS124", "1.0.000")])

    rows = {r.measure_id: r for r in (await test_session.execute(select(MeasureReadiness))).scalars().all()}
    assert set(rows) == {"CMS122", "CMS124"}
    assert all(r.state is ReadinessState.checking for r in rows.values())


async def test_claim_unchecked_survives_a_lost_claim_race(test_session, mcs_row):
    """A lost claim race must not raise, and must not cost non-colliding measures.

    True concurrency needs two separate sessions/connections racing on the same
    (mcs_id, measure_id, measure_version); this single-connection SQLite fixture
    can't produce that directly. The same collision — a candidate that passes
    the initial "not yet claimed" check but hits `uq_measure_readiness_key` at
    insert time — is reproduced deterministically by listing the same key
    twice in one call: the first occurrence claims and flushes for real, so the
    second collides exactly as a genuine second requester would.
    """
    from app.services.measure_readiness import claim_unchecked

    claimed = await claim_unchecked(
        test_session,
        mcs_row.id,
        [("CMS124", "1.0.000"), ("CMS124", "1.0.000"), ("CMS999", "2.0.000")],
    )
    assert claimed == [("CMS124", "1.0.000"), ("CMS999", "2.0.000")]


async def test_claim_unchecked_survives_a_genuine_concurrent_race(test_engine, mcs_row):
    """Best-effort attempt at a *genuine* race: two independent sessions bound
    to the same engine, both trying to claim the same never-before-seen
    measure concurrently via `asyncio.gather`.

    Whether the two coroutines actually interleave (both pass the SELECT
    before either INSERTs) depends on asyncio/aiosqlite scheduling and is not
    guaranteed run to run. The assertion is written to hold either way: across
    both calls, the measure must be claimed exactly once and neither call may
    raise — that is true whether a real collision happened this run or one
    call's INSERT simply completed, and was visible, before the other's SELECT.
    """
    import asyncio

    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

    from app.services.measure_readiness import claim_unchecked

    session_factory = async_sessionmaker(test_engine, class_=AsyncSession, expire_on_commit=False)

    async def attempt():
        async with session_factory() as session:
            return await claim_unchecked(session, mcs_row.id, [("CMS777", "3.0.000")])

    results = await asyncio.gather(attempt(), attempt())
    combined = results[0] + results[1]
    assert combined == [("CMS777", "3.0.000")]


async def test_check_redacts_credentials_echoed_back_in_a_server_diagnostic():
    """The MCS's own diagnostic text is untrusted input, and we PERSIST it.

    `redact_outcome` records why: HAPI echoes failed request bodies into
    diagnostics, narrative and extension fields, so the Authorization header
    Lenny just sent can come straight back in the error text. That text lands
    in `measure_readiness.error` and is rendered verbatim in the measures
    table, so an operator's bearer token would be readable in the UI and
    durable in the database.
    """
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
                    "HAPI-0389: Failed to call access method; request was "
                    "GET http://hapi-fhir-measure:8080/fhir/Measure/CMS122 with "
                    "Authorization: Bearer eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiJsZW5ueSJ9.s3cr3tsig"
                ),
            }
        ],
    }
    transport = httpx.MockTransport(lambda request: httpx.Response(500, json=outcome))
    verdict = await check_measure_readiness(
        "https://mcs.example.com/fhir", "CMS122", auth_headers={}, timeout=5.0, transport=transport
    )

    assert verdict.state is ReadinessState.not_ready
    assert "eyJhbGciOiJIUzI1NiJ9" not in verdict.error
    assert "s3cr3tsig" not in verdict.error
    assert "hapi-fhir-measure" not in verdict.error
    # Still useful to a human: the redaction must not eat the whole message.
    assert "HAPI-0389" in verdict.error


async def test_check_returns_unknown_when_the_library_internals_are_the_wrong_shape():
    """A body that IS a Library but whose declared fields are mis-shaped.

    `check_measure_readiness` promises never to raise, and until now honoured
    that only because `run_sweep` happened to wrap the call — a 2xx Library
    with `dataRequirement` as a string makes `extract_valueset_canonicals`
    raise `AttributeError` straight out of the function.

    `unknown` and not `ready` matters more than the exception: silently
    skipping a mis-shaped field yields zero canonicals, zero missing, and falls
    through to READY — the one output this feature must never produce.
    """
    import httpx

    from app.models.measure_readiness import ReadinessState
    from app.services.measure_readiness import check_measure_readiness

    bodies = [
        {"resourceType": "Library", "dataRequirement": "not-a-list"},
        {"resourceType": "Library", "dataRequirement": [{"codeFilter": ["not-an-object"]}]},
        {"resourceType": "Library", "dataRequirement": 42},
        {"resourceType": "Library", "relatedArtifact": "not-a-list"},
        {"resourceType": "Library", "dataRequirement": [{"codeFilter": [{"valueSet": 7}]}]},
    ]
    for body in bodies:
        transport = httpx.MockTransport(lambda request, b=body: httpx.Response(200, json=b))
        verdict = await check_measure_readiness(
            "https://mcs.example.com/fhir", "CMS122", auth_headers={}, timeout=5.0, transport=transport
        )
        assert verdict.state is ReadinessState.unknown, f"body {body!r} produced {verdict.state}"
        assert verdict.error is not None


async def test_mark_all_checking_survives_a_lost_claim_race(test_session, mcs_row):
    """Two overlapping re-checks must not raise out of `mark_all_checking`.

    Same collision `claim_unchecked` documents, reproduced the same
    deterministic way this file already uses: listing one key twice makes the
    second insert hit `uq_measure_readiness_key` exactly as a genuine second
    requester would, on a fixture that cannot race two real connections.
    Non-colliding measures in the same batch must still be claimed — otherwise
    the sweep never runs for them.
    """
    from sqlalchemy import select

    from app.models.measure_readiness import MeasureReadiness, ReadinessState
    from app.services.measure_readiness import mark_all_checking

    await mark_all_checking(
        test_session,
        mcs_row.id,
        [("CMS124", "1.0.000"), ("CMS124", "1.0.000"), ("CMS999", "2.0.000")],
    )

    rows = (await test_session.execute(select(MeasureReadiness))).scalars().all()
    assert {(r.measure_id, r.measure_version) for r in rows} == {("CMS124", "1.0.000"), ("CMS999", "2.0.000")}
    assert {r.state for r in rows} == {ReadinessState.checking}


async def test_run_sweep_sanitizes_the_last_resort_catch_all(test_session, mcs_row, monkeypatch):
    """The catch-all in `run_sweep.one()` stores text straight into a rendered,
    durable column — and by construction the exceptions reaching it are the
    ones nothing else vetted.

    An httpx/SSL failure's raw `str()` carries the MCS URL, credentials and
    internal hostnames included. Every other error path in this codebase routes
    exception text through `sanitize_error`; this one must too.
    """
    from sqlalchemy import select

    import app.services.measure_readiness as svc
    from app.models.measure_readiness import MeasureReadiness, ReadinessState

    async def fake_check(mcs_url, measure_id, **kwargs):
        raise RuntimeError(
            "connect failed for https://svc:hunter2@mcs-internal:8080/fhir with Authorization: Bearer sk-live-abc123"
        )

    monkeypatch.setattr(svc, "check_measure_readiness", fake_check)
    monkeypatch.setattr(svc, "_session_factory", lambda: _SessionCtx(test_session))

    await svc.claim_unchecked(test_session, mcs_row.id, [("CMS122", "0.5.000")])
    await svc.run_sweep(mcs_row.id, [("CMS122", "0.5.000")])

    row = (await test_session.execute(select(MeasureReadiness))).scalars().one()
    assert row.state is ReadinessState.unknown
    assert "hunter2" not in row.error
    assert "sk-live-abc123" not in row.error
    assert "mcs-internal" not in row.error
    assert row.error.startswith("Check failed: ")
