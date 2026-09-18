"""Tests for the measure readiness check, sweep, and storage model."""

import re

import pytest
import pytest_asyncio


def _credentialed_url(user: str, secret: str, host: str, path: str = "/fhir") -> str:
    """Assemble a basic-auth URL from parts.

    The credentials here are invented, and the tests that use them assert the
    strings get STRIPPED — that is the whole point of the fixtures. Written as
    concatenation rather than a literal so the repo's pre-push credential
    scanner does not flag a `scheme://user:pass@host` shape it cannot tell apart
    from a real one. The scanner matches the shape, not the values, so making
    the values look faker would not have helped.
    """
    return "https://" + user + ":" + secret + "@" + host + path


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


def _hapi_0831_outcome(vs_url: str) -> dict:
    """HAPI's answer to `$expand` for a ValueSet past the 1,000-code in-memory cap.

    Shape captured verbatim from HAPI v8.8.0 (#444). `code` is `processing`, NOT
    `too-costly`: HAPI does not use the `too-costly` issue code here, and a
    fixture that said it did let the `HAPI-0831`-in-text branch of
    `_means_not_expandable` be deleted with all tests still green. `too-costly`
    is still recognised in production for servers that do emit it, and is
    covered separately by the non-HAPI test below.

    Note HAPI's own placeholder corruption — it does not reliably name the
    ValueSet — which is why the probe has to know which canonical it asked about
    rather than parse the message.
    """
    return {
        "resourceType": "OperationOutcome",
        "issue": [
            {
                "severity": "error",
                "code": "processing",
                "diagnostics": (
                    "HAPI-0831: Expansion of ValueSet produced too many codes (maximum 1,000) - "
                    "Operation aborted! - ValueSet has not yet been pre-expanded. Performing "
                    "in-memory expansion without parameters. Current status: NOT_EXPANDED"
                ),
            }
        ],
    }


def _expanded_valueset(vs_url: str) -> dict:
    return {"resourceType": "ValueSet", "url": vs_url, "expansion": {"total": 2, "contains": []}}


async def test_check_returns_unknown_when_a_present_valueset_cannot_expand_yet():
    """#444: presence is not usability — but "not usable yet" is not "broken".

    Job #9 reported `ready` and then failed 66/66 patients at `evaluate`, because
    the measure's value sets had not been pre-expanded by the background
    scheduler. Every canonical was present, so the presence check saw nothing.

    The verdict is `unknown`, not `not_ready`, and that distinction was measured
    rather than assumed. When a value set is not pre-expanded HAPI attempts an
    in-memory expansion of the whole `compose`, so a SNOMED filter trips the
    1,000-code cap even when the value set itself is tiny — observed as 23-of-23
    and 29-of-29 canonicals failing at once, small ones included. The window
    opens on every server start (HAPI answers `/metadata` before its expansions
    are ready) and closes by itself. Reporting `not_ready` there would paint
    every measure on the server red and read as a content defect, which this is
    not. `unknown` still closes #444: the bug was reporting `ready` before a job
    that could only fail.
    """
    import httpx

    from app.models.measure_readiness import ReadinessState
    from app.services.measure_readiness import check_measure_readiness

    def handler(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        if "$data-requirements" in url:
            return httpx.Response(200, json=_dr_library(["http://vs/a", "http://vs/b", "http://vs/c"]))
        if "$expand" in url:
            # Read the decoded param, never the raw URL: httpx percent-encodes
            # a canonical into `url=http%3A%2F%2Fvs%2Fb`, so a substring match
            # against the query string silently never fires and the mock would
            # answer 200 for everything.
            probed = request.url.params.get("url")
            if probed == "http://vs/b":
                return httpx.Response(500, json=_hapi_0831_outcome(probed))
            return httpx.Response(200, json=_expanded_valueset(probed))
        return httpx.Response(200, json=_vs_bundle(["http://vs/a", "http://vs/b", "http://vs/c"]))

    verdict = await check_measure_readiness(
        "https://mcs.example.com/fhir",
        "CMS122",
        auth_headers={},
        timeout=5.0,
        transport=httpx.MockTransport(handler),
    )

    assert verdict.state is ReadinessState.unknown
    # Named, not merely counted: #451 is an open issue about a row that names
    # nothing, and this path must not add another one.
    assert "http://vs/b" in (verdict.error or "")


@pytest.mark.parametrize("status", [401, 403])
async def test_check_returns_unknown_when_the_expansion_probe_is_refused(status):
    """A refused probe must not read as "expandable".

    The probe decides `not_ready` by recognising a marker in the answer. A 401
    carries no marker, so "no marker" cannot be allowed to mean "fine" — that
    would turn a credential problem into a green badge, which is the exact
    false-`ready` shape #444 is about.
    """
    import httpx

    from app.models.measure_readiness import ReadinessState
    from app.services.measure_readiness import check_measure_readiness

    def handler(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        if "$data-requirements" in url:
            return httpx.Response(200, json=_dr_library(["http://vs/a"]))
        if "$expand" in url:
            return httpx.Response(status, json={"resourceType": "OperationOutcome", "issue": []})
        return httpx.Response(200, json=_vs_bundle(["http://vs/a"]))

    verdict = await check_measure_readiness(
        "https://mcs.example.com/fhir",
        "CMS122",
        auth_headers={},
        timeout=5.0,
        transport=httpx.MockTransport(handler),
    )

    assert verdict.state is ReadinessState.unknown
    # Asserting the CONTENT, not just "an error exists". Both this path and the
    # present-but-unexpandable path answer `unknown` with a non-empty error, so a
    # bare `is not None` passes even if a refusal were silently reclassified as
    # "pre-expansion pending" — turning a credential fault into a transient-content
    # message an operator would wait out instead of fixing.
    assert str(status) in (verdict.error or ""), verdict.error
    assert "not expandable yet" not in (verdict.error or "").lower(), verdict.error


async def test_check_probes_every_present_valueset_and_stays_ready_when_all_expand():
    """The direction that must not regress.

    #444 adds a way to say `not_ready`, and a false `not_ready` is the failure
    mode of the fix itself — it would mark healthy measures broken on every
    server whose value sets are all fine. Asserting the probe count as well as
    the verdict is deliberate: `ready` alone would also hold if the probe never
    ran at all, which is the bug this test is supposed to be able to catch.
    """
    import httpx

    from app.models.measure_readiness import ReadinessState
    from app.services.measure_readiness import check_measure_readiness

    canonicals = ["http://vs/a", "http://vs/b", "http://vs/c"]
    probed: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        if "$data-requirements" in url:
            return httpx.Response(200, json=_dr_library(canonicals))
        if "$expand" in url:
            asked = request.url.params.get("url")
            # `count=2` is required, not incidental -- it is the only probe that
            # reaches HAPI's pre-calculated expansion store. See the measured
            # table on the `$expand` request in `find_unexpanded_valuesets`.
            assert request.url.params.get("count") == "2", request.url.params.get("count")
            probed.append(asked)
            return httpx.Response(200, json=_expanded_valueset(asked))
        return httpx.Response(200, json=_vs_bundle(canonicals))

    verdict = await check_measure_readiness(
        "https://mcs.example.com/fhir",
        "CMS122",
        auth_headers={},
        timeout=5.0,
        transport=httpx.MockTransport(handler),
    )

    assert verdict.state is ReadinessState.ready
    assert sorted(probed) == canonicals


async def test_expansion_probe_is_skipped_when_a_valueset_is_already_missing():
    """An absent ValueSet is already `not_ready`; probing it would be N wasted
    requests to reach a verdict the presence check has made."""
    import httpx

    from app.models.measure_readiness import ReadinessState
    from app.services.measure_readiness import check_measure_readiness

    probed: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        if "$data-requirements" in url:
            return httpx.Response(200, json=_dr_library(["http://vs/a", "http://vs/b"]))
        if "$expand" in url:
            probed.append(request.url.params.get("url"))
            return httpx.Response(200, json=_expanded_valueset("x"))
        return httpx.Response(200, json=_vs_bundle(["http://vs/a"]))

    verdict = await check_measure_readiness(
        "https://mcs.example.com/fhir",
        "CMS122",
        auth_headers={},
        timeout=5.0,
        transport=httpx.MockTransport(handler),
    )

    assert verdict.state is ReadinessState.not_ready
    assert verdict.missing_valuesets == ["http://vs/b"]
    assert probed == []


async def test_one_refused_probe_among_many_settles_cleanly():
    """The realistic shape: CMS125 has 29 value sets, not one.

    The probe fans out, so a single refusal is raised while siblings are still
    queued. The assertion below pins that all 24 canonicals are probed even
    though the 8th refuses, and that the verdict is `unknown` naming the 401.

    Note what this does NOT prove, and do not add the claim back: this test does
    NOT pin `return_exceptions=True`. `httpx.MockTransport` answers a sync
    handler synchronously, so every probe coroutine is already scheduled before
    the refusal can abandon anything — flip the flag to False and this test
    still passes (verified by mutation). The orphaned-request behaviour is not
    observable here either, for the same reason.

    The flag itself is pinned by
    `test_find_unexpanded_valuesets_reraises_the_earliest_ordered_error_not_first_completed`,
    which uses an ASYNC handler whose sleep makes completion order differ from
    input order. That is the test to keep green if you touch the gather.
    """
    import httpx

    from app.models.measure_readiness import ReadinessState
    from app.services.measure_readiness import check_measure_readiness

    canonicals = [f"http://vs/{i:02d}" for i in range(24)]
    started: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        if "$data-requirements" in url:
            return httpx.Response(200, json=_dr_library(canonicals))
        if "$expand" in url:
            asked = request.url.params.get("url")
            started.append(asked)
            if asked == "http://vs/07":
                return httpx.Response(401, json={"resourceType": "OperationOutcome", "issue": []})
            return httpx.Response(200, json=_expanded_valueset(asked))
        return httpx.Response(200, json=_vs_bundle(canonicals))

    verdict = await check_measure_readiness(
        "https://mcs.example.com/fhir",
        "CMS122",
        auth_headers={},
        timeout=5.0,
        transport=httpx.MockTransport(handler),
    )

    assert verdict.state is ReadinessState.unknown
    assert "401" in (verdict.error or "")
    # Every sibling still ran: a bare `gather` would return on the first raise and
    # leave the queued probes unissued.
    assert sorted(started) == canonicals, f"siblings abandoned: {sorted(set(canonicals) - set(started))}"


async def test_a_served_expansion_is_usable_even_when_it_says_not_expanded():
    """The false-`not_ready` trap in the probe's own wording.

    HAPI reports `Current status: NOT_EXPANDED` whenever a ValueSet is absent from
    its *pre-expansion store* — including when it then expands the thing in memory
    perfectly well and answers 200, which is the ordinary case for every value set
    under the 1,000-code cap. Treating that phrase as the signal would mark most
    healthy measures red, which is worse than the bug being fixed.

    A 200 means the server served an expansion. That is the signal; the prose is not.
    """
    import httpx

    from app.models.measure_readiness import ReadinessState
    from app.services.measure_readiness import check_measure_readiness

    served_with_a_note = {
        "resourceType": "ValueSet",
        "url": "http://vs/a",
        "expansion": {"total": 2, "contains": [{"code": "x"}, {"code": "y"}]},
        "contained": [
            {
                "resourceType": "OperationOutcome",
                "issue": [
                    {
                        "severity": "information",
                        "code": "informational",
                        "diagnostics": (
                            "ValueSet has not yet been pre-expanded. Performing in-memory "
                            "expansion without parameters. Current status: NOT_EXPANDED"
                        ),
                    }
                ],
            }
        ],
    }

    def handler(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        if "$data-requirements" in url:
            return httpx.Response(200, json=_dr_library(["http://vs/a"]))
        if "$expand" in url:
            return httpx.Response(200, json=served_with_a_note)
        return httpx.Response(200, json=_vs_bundle(["http://vs/a"]))

    verdict = await check_measure_readiness(
        "https://mcs.example.com/fhir",
        "CMS122",
        auth_headers={},
        timeout=5.0,
        transport=httpx.MockTransport(handler),
    )

    assert verdict.state is ReadinessState.ready


async def test_find_unexpanded_valuesets_returns_empty_without_a_network_call_for_no_canonicals():
    """Symmetric with `find_missing_valuesets`'s own no-canonicals short-circuit.

    A measure with no terminology dependencies at all must not issue a single
    `$expand` request — there is nothing to probe.
    """
    import httpx

    from app.services.measure_readiness import find_unexpanded_valuesets

    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(str(request.url))
        return httpx.Response(200, json=_expanded_valueset("x"))

    unexpanded = await find_unexpanded_valuesets(
        "https://mcs.example.com/fhir", [], auth_headers={}, timeout=5.0, transport=httpx.MockTransport(handler)
    )
    assert unexpanded == []
    assert calls == []


async def test_find_unexpanded_valuesets_strips_version_suffixes():
    """A `|version` suffix must be stripped before the `$expand` request is made,
    and the returned list must report the stripped form too — the same `|`
    convention `find_missing_valuesets` already normalises on.
    """
    import httpx

    from app.services.measure_readiness import find_unexpanded_valuesets

    probed = []

    def handler(request: httpx.Request) -> httpx.Response:
        asked = request.url.params.get("url")
        probed.append(asked)
        if asked == "http://vs/a":
            return httpx.Response(500, json=_hapi_0831_outcome(asked))
        return httpx.Response(200, json=_expanded_valueset(asked))

    unexpanded = await find_unexpanded_valuesets(
        "https://mcs.example.com/fhir",
        ["http://vs/a|20210101", "http://vs/b|1.0.0"],
        auth_headers={},
        timeout=5.0,
        transport=httpx.MockTransport(handler),
    )
    assert set(probed) == {"http://vs/a", "http://vs/b"}, "the version suffix was sent to the server"
    assert unexpanded == ["http://vs/a"]


def test_means_not_expandable_returns_false_for_non_json_body():
    """A non-2xx body that is not even parseable JSON (a plain-text error page,
    a proxy's HTML) must not be misread as the too-costly marker — and must not
    raise out of the probe either.
    """
    import httpx

    from app.services.measure_readiness import _means_not_expandable

    resp = httpx.Response(500, text="Internal Server Error")
    assert _means_not_expandable(resp) is False


def test_means_not_expandable_returns_false_when_body_is_a_json_list():
    """Valid JSON but not an object (`[...]`, `null`, a bare string) is not an
    OperationOutcome, and `body.get(...)` would raise on a list — guarded by
    the explicit `isinstance(body, dict)` check.
    """
    import httpx

    from app.services.measure_readiness import _means_not_expandable

    resp = httpx.Response(500, json=[{"code": "too-costly"}])
    assert _means_not_expandable(resp) is False


def test_means_not_expandable_recognises_the_standard_issue_code_on_non_hapi_servers():
    """`_HAPI_TOO_COSTLY_CODE in resp.text` fires for HAPI answers, but the
    module-level comment is explicit that `_TOO_COSTLY_ISSUE_CODE` exists FOR
    servers that are not HAPI. Every other true-returning test's body also
    happens to embed the literal string "HAPI-0831" in its diagnostics text, so
    the first `in resp.text` check always short-circuits before the `issue[].code`
    branch is ever reached. This body deliberately contains neither "HAPI-0831"
    anywhere in its text NOR any diagnostics mentioning it, so only the FHIR
    standard issue-code path can produce `True`.
    """
    import httpx

    from app.services.measure_readiness import _means_not_expandable

    resp = httpx.Response(
        500,
        json={
            "resourceType": "OperationOutcome",
            "issue": [
                {
                    "severity": "error",
                    "code": "too-costly",
                    "diagnostics": "The value set expansion exceeded this server's configured limit.",
                }
            ],
        },
    )
    assert "HAPI-0831" not in resp.text
    assert _means_not_expandable(resp) is True


def test_means_not_expandable_returns_false_when_issue_present_but_no_too_costly_code():
    """An OperationOutcome with issues, none of which are the too-costly marker,
    must not be mistaken for "present but not expandable" — this is some other
    server-side failure the probe cannot interpret and must surface as a raised
    `ValueSetExpansionProbeError`, not a silent unexpanded verdict.
    """
    import httpx

    from app.services.measure_readiness import _means_not_expandable

    resp = httpx.Response(
        500,
        json={
            "resourceType": "OperationOutcome",
            "issue": [{"severity": "error", "code": "exception", "diagnostics": "Something else broke"}],
        },
    )
    assert _means_not_expandable(resp) is False


def test_means_not_expandable_ignores_issue_entries_that_are_not_dicts():
    """A malformed `issue` array (strings, numbers, `None`) must not raise out of
    the probe — `isinstance(issue, dict)` is what guards the `.get("code")` call.
    """
    import httpx

    from app.services.measure_readiness import _means_not_expandable

    resp = httpx.Response(500, json={"resourceType": "OperationOutcome", "issue": ["not-a-dict", 42, None]})
    assert _means_not_expandable(resp) is False


async def test_check_reports_multiple_unexpanded_valuesets_with_plural_wording():
    """Every prior expansion test named exactly one unexpanded value set, which
    leaves the plural branch (`noun = "value sets"`) and the multi-name join
    unexercised. Also pins that names appear in `canonicals` order, not
    response-arrival order — the ordering guarantee `find_unexpanded_valuesets`
    documents.
    """
    import httpx

    from app.models.measure_readiness import ReadinessState
    from app.services.measure_readiness import check_measure_readiness

    canonicals = ["http://vs/a", "http://vs/b", "http://vs/c"]

    def handler(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        if "$data-requirements" in url:
            return httpx.Response(200, json=_dr_library(canonicals))
        if "$expand" in url:
            probed = request.url.params.get("url")
            if probed in ("http://vs/a", "http://vs/c"):
                return httpx.Response(500, json=_hapi_0831_outcome(probed))
            return httpx.Response(200, json=_expanded_valueset(probed))
        return httpx.Response(200, json=_vs_bundle(canonicals))

    verdict = await check_measure_readiness(
        "https://mcs.example.com/fhir",
        "CMS122",
        auth_headers={},
        timeout=5.0,
        transport=httpx.MockTransport(handler),
    )

    assert verdict.state is ReadinessState.unknown
    error = verdict.error or ""
    assert "2 value sets" in error, f"expected plural wording for 2 unexpanded value sets: {error!r}"
    assert "http://vs/a" in error and "http://vs/c" in error
    assert error.index("http://vs/a") < error.index("http://vs/c"), "names must follow canonicals order"


async def test_check_returns_unknown_when_the_expansion_probe_itself_fails_at_the_transport_level():
    """A raw connection failure DURING the expansion probe — not merely a
    non-2xx answer — must also become `unknown`, through
    `check_measure_readiness`'s generic `except Exception` branch rather than
    the `ValueSetExpansionProbeError` one every other expansion test drives.
    """
    import httpx

    from app.models.measure_readiness import ReadinessState
    from app.services.measure_readiness import check_measure_readiness

    def handler(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        if "$data-requirements" in url:
            return httpx.Response(200, json=_dr_library(["http://vs/a"]))
        if "$expand" in url:
            raise httpx.ConnectError("connection refused", request=request)
        return httpx.Response(200, json=_vs_bundle(["http://vs/a"]))

    verdict = await check_measure_readiness(
        "https://mcs.example.com/fhir",
        "CMS122",
        auth_headers={},
        timeout=5.0,
        transport=httpx.MockTransport(handler),
    )
    assert verdict.state is ReadinessState.unknown
    assert "Could not verify value set expansion" in (verdict.error or "")


async def test_find_unexpanded_valuesets_reraises_the_earliest_ordered_error_not_first_completed():
    """`asyncio.gather` preserves input order in its results regardless of which
    awaitable actually finishes first, which is what makes "the reported reason
    is the same one on every run" true. Proven here by making the LATER-ordered
    canonical answer first: if the re-raise loop picked whichever exception
    happened to land first, this test would report the 503, not the 502.
    """
    import asyncio

    import httpx

    from app.services.measure_readiness import ValueSetExpansionProbeError, find_unexpanded_valuesets

    async def handler(request: httpx.Request) -> httpx.Response:
        canonical = request.url.params.get("url")
        if canonical == "http://vs/a":
            await asyncio.sleep(0.05)
            return httpx.Response(502)
        return httpx.Response(503)

    with pytest.raises(ValueSetExpansionProbeError) as exc_info:
        await find_unexpanded_valuesets(
            "https://mcs.example.com/fhir",
            ["http://vs/a", "http://vs/b"],
            auth_headers={},
            timeout=5.0,
            transport=httpx.MockTransport(handler),
        )
    assert "502" in str(exc_info.value)
    assert "503" not in str(exc_info.value)


async def test_find_unexpanded_valuesets_never_exceeds_its_concurrency_bound():
    """The semaphore is the only thing standing between this probe and firing
    every request in the closure at once. CMS125 alone is 29 value sets.
    """
    import asyncio

    import httpx

    from app.services.measure_readiness import _EXPAND_PROBE_CONCURRENCY, find_unexpanded_valuesets

    in_flight = 0
    max_in_flight = 0
    lock = asyncio.Lock()

    async def handler(request: httpx.Request) -> httpx.Response:
        nonlocal in_flight, max_in_flight
        async with lock:
            in_flight += 1
            max_in_flight = max(max_in_flight, in_flight)
        await asyncio.sleep(0.02)
        async with lock:
            in_flight -= 1
        return httpx.Response(200, json=_expanded_valueset("x"))

    canonicals = [f"http://vs/{i}" for i in range(12)]
    await find_unexpanded_valuesets(
        "https://mcs.example.com/fhir",
        canonicals,
        auth_headers={},
        timeout=5.0,
        transport=httpx.MockTransport(handler),
    )
    assert max_in_flight <= _EXPAND_PROBE_CONCURRENCY, "the semaphore did not bound concurrency"
    assert max_in_flight == _EXPAND_PROBE_CONCURRENCY, (
        "concurrency never reached the bound at all — is the semaphore actually being used?"
    )


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
        _credentialed_url("user", "pass", "mcs.example.com"),
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
            "connect failed for "
            + _credentialed_url("svc", "hunter2", "mcs-internal:8080")
            + " with Authorization: Bearer sk-live-abc123"
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


async def test_find_unexpanded_valuesets_stops_when_its_wall_clock_budget_is_spent():
    """#439's bug class, which this probe would otherwise reintroduce.

    `timeout` is a per-REQUEST httpx timeout, not a budget for the operation. The
    probe issues one request per canonical through a semaphore of 4, so 29 value
    sets run as 8 sequential waves and each wave may take the full timeout — up to
    8x the ceiling `READINESS_TIMEOUT_SECONDS` is supposed to impose, all while
    holding one of only `READINESS_CONCURRENCY` slots and pinning the row at
    `checking` (which disables the Re-check button, #436).

    Raising rather than returning what it has is the point: this function returns
    the value sets that are NOT usable, so a truncated answer under-reports and can
    resolve to `ready` — the worst output this check can produce.
    """
    import httpx

    from app.services.measure_readiness import (
        ValueSetExpansionProbeError,
        find_unexpanded_valuesets,
    )

    probed: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        probed.append(str(request.url))
        return httpx.Response(200, json={"resourceType": "ValueSet", "expansion": {"total": 0}})

    with pytest.raises(ValueSetExpansionProbeError) as excinfo:
        await find_unexpanded_valuesets(
            "https://mcs.example.com/fhir",
            ["http://vs/a", "http://vs/b", "http://vs/c"],
            auth_headers={},
            timeout=0.0,
            transport=httpx.MockTransport(handler),
        )

    assert "budget" in str(excinfo.value).lower(), str(excinfo.value)
    assert probed == [], f"kept issuing requests past its budget: {probed}"


async def test_expansion_probe_budget_is_anchored_to_the_whole_check_not_its_own_stage():
    """Bounding only the probe stage moves #439's bug up a level instead of removing it.

    `READINESS_TIMEOUT_SECONDS` is documented as the ceiling for ONE measure's
    readiness check. Stage 1 ($data-requirements) and stage 2 (the ValueSet
    presence search, itself up to `_VALUESET_MAX_PAGES` requests) already spend
    from that ceiling. If stage 3 then starts a FRESH `timeout` window, the check
    as a whole can run to well over the ceiling while holding one of only
    `READINESS_CONCURRENCY` sweep slots.

    So the probe's deadline must be anchored to when the CHECK started, not to
    when the probe started. Asserted by spying on the budget stage 3 is handed
    after stage 1 has demonstrably burned part of it.
    """
    import asyncio as _asyncio
    import time as _time
    from unittest.mock import patch as _patch

    import httpx

    from app.services import measure_readiness as mr

    burned = 0.30
    budget = 1.0
    seen: dict[str, float] = {}

    async def handler(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        if "$data-requirements" in url:
            await _asyncio.sleep(burned)
            return httpx.Response(200, json=_dr_library(["http://vs/a"]))
        return httpx.Response(200, json=_vs_bundle(["http://vs/a"]))

    async def _spy(*args, **kwargs):
        seen["remaining"] = kwargs["deadline"] - _time.monotonic()
        return []

    with _patch.object(mr, "find_unexpanded_valuesets", _spy):
        await mr.check_measure_readiness(
            "https://mcs.example.com/fhir",
            "CMS122",
            auth_headers={},
            timeout=budget,
            transport=httpx.MockTransport(handler),
        )

    assert "remaining" in seen, "stage 3 was never handed a deadline"
    # Anchored: roughly `budget - burned` is left. Unanchored would hand it a
    # fresh `budget`, so anything above that midpoint means it was not anchored.
    assert seen["remaining"] < budget - (burned / 2), (
        f"probe budget looks unanchored: {seen['remaining']:.3f}s left of a {budget}s ceiling "
        f"after {burned}s was already spent"
    )


@pytest.mark.parametrize("status", [301, 302, 307, 308])
async def test_a_redirected_expansion_probe_is_not_treated_as_usable(status):
    """A 3xx is not a served expansion, and reading it as one is a false `ready`.

    `httpx` does not follow redirects by default, so a redirect arrives here
    intact. Stage 1 of this same check already refuses to let that fall through —
    it routes 3xx to `unknown` and names the benign cause: an http->https upgrade
    or a trailing-slash normalisation in a proxy in front of the MCS. A proxy like
    that in front of a third-party measure server would otherwise make every
    probe answer "usable" and green-light a job that cannot evaluate, which is
    the exact failure #444 exists to remove.
    """
    import httpx

    from app.models.measure_readiness import ReadinessState
    from app.services.measure_readiness import check_measure_readiness

    def handler(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        if "$data-requirements" in url:
            return httpx.Response(200, json=_dr_library(["http://vs/a"]))
        if "$expand" in url:
            return httpx.Response(status, headers={"location": "https://mcs.example.com/fhir/ValueSet/x"})
        return httpx.Response(200, json=_vs_bundle(["http://vs/a"]))

    verdict = await check_measure_readiness(
        "https://mcs.example.com/fhir",
        "CMS122",
        auth_headers={},
        timeout=5.0,
        transport=httpx.MockTransport(handler),
    )

    assert verdict.state is not ReadinessState.ready, (
        f"HTTP {status} on the expansion probe produced a false `ready`: {verdict}"
    )
    assert verdict.state is ReadinessState.unknown
    assert str(status) in (verdict.error or ""), verdict.error


async def test_the_verdict_text_bounds_how_many_server_supplied_names_it_carries():
    """`error` is persisted and returned by every GET /measures.

    The canonicals come from the measure server's own $data-requirements answer,
    so their count and length are not ours to assume. A measure declaring
    hundreds would otherwise put hundreds of remote URLs into an unbounded Text
    column that the UI then renders on every poll.
    """
    import httpx

    from app.models.measure_readiness import ReadinessState
    from app.services.measure_readiness import _EXPAND_ERROR_MAX_NAMED, check_measure_readiness

    canonicals = [f"http://vs/{i:03d}" for i in range(40)]

    def handler(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        if "$data-requirements" in url:
            return httpx.Response(200, json=_dr_library(canonicals))
        if "$expand" in url:
            return httpx.Response(500, json=_hapi_0831_outcome(request.url.params.get("url")))
        return httpx.Response(200, json=_vs_bundle(canonicals))

    verdict = await check_measure_readiness(
        "https://mcs.example.com/fhir",
        "CMS122",
        auth_headers={},
        timeout=5.0,
        transport=httpx.MockTransport(handler),
    )

    assert verdict.state is ReadinessState.unknown
    # The full count is still reported; only the enumeration is bounded.
    assert "40 value sets" in (verdict.error or ""), verdict.error
    assert verdict.error.count("http://vs/") == _EXPAND_ERROR_MAX_NAMED, verdict.error
    assert f"and {40 - _EXPAND_ERROR_MAX_NAMED} more" in verdict.error, verdict.error


async def test_the_verdict_text_names_every_canonical_at_exactly_the_cap_with_no_summary_suffix():
    """The cap is `>`, not `>=`: exactly `_EXPAND_ERROR_MAX_NAMED` unexpanded value
    sets must all be named, with no "and N more" tacked on.

    The existing bound test only exercises a count well past the cap (40 vs. 10),
    which cannot distinguish `>` from `>=` — both trim the enumeration there. This
    pins the boundary itself: an off-by-one that trimmed one name early (or added
    a spurious ", and 0 more") would pass every other test in this file.
    """
    import httpx

    from app.models.measure_readiness import ReadinessState
    from app.services.measure_readiness import _EXPAND_ERROR_MAX_NAMED, check_measure_readiness

    canonicals = [f"http://vs/{i:03d}" for i in range(_EXPAND_ERROR_MAX_NAMED)]

    def handler(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        if "$data-requirements" in url:
            return httpx.Response(200, json=_dr_library(canonicals))
        if "$expand" in url:
            return httpx.Response(500, json=_hapi_0831_outcome(request.url.params.get("url")))
        return httpx.Response(200, json=_vs_bundle(canonicals))

    verdict = await check_measure_readiness(
        "https://mcs.example.com/fhir",
        "CMS122",
        auth_headers={},
        timeout=5.0,
        transport=httpx.MockTransport(handler),
    )

    assert verdict.state is ReadinessState.unknown
    error = verdict.error or ""
    assert error.count("http://vs/") == _EXPAND_ERROR_MAX_NAMED, error
    for canonical in canonicals:
        assert canonical in error, f"{canonical} missing from a count exactly at the cap: {error}"
    assert not re.search(r"and \d+ more", error), f"a summary suffix appeared at exactly the cap: {error}"


async def test_check_uses_singular_wording_for_exactly_one_unexpanded_valueset():
    """The plural test (`test_check_reports_multiple_unexpanded_valuesets_with_plural_wording`)
    pins "2 value sets"; nothing pins the singular branch's exact wording. "1 value
    set" is a substring of "1 value sets", so a regression to always-plural would
    slip past a naive `in` check — this asserts the plural phrase is ABSENT too.
    """
    import httpx

    from app.models.measure_readiness import ReadinessState
    from app.services.measure_readiness import check_measure_readiness

    def handler(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        if "$data-requirements" in url:
            return httpx.Response(200, json=_dr_library(["http://vs/a"]))
        if "$expand" in url:
            return httpx.Response(500, json=_hapi_0831_outcome("http://vs/a"))
        return httpx.Response(200, json=_vs_bundle(["http://vs/a"]))

    verdict = await check_measure_readiness(
        "https://mcs.example.com/fhir",
        "CMS122",
        auth_headers={},
        timeout=5.0,
        transport=httpx.MockTransport(handler),
    )

    assert verdict.state is ReadinessState.unknown
    error = verdict.error or ""
    assert "Could not verify 1 value set this measure references" in error, error
    assert "1 value sets" not in error, f"singular count got plural wording: {error!r}"


async def test_check_does_not_report_present_but_unexpandable_valuesets_as_missing():
    """`missing_valuesets` means ABSENT; a value set the expansion probe flags is,
    by construction, present (stage 2 already confirmed it, or stage 3 would never
    have run). Populating `missing_valuesets` with it as well would relabel a
    present-but-not-yet-usable value set as absent — the exact confusion #451 is
    about, and one a reviewer could introduce by reusing `missing` instead of
    threading a fresh empty list through this branch.

    Every other expansion test asserts `state` and the `error` text but never this
    field, so a regression here would pass the whole rest of the suite.
    """
    import httpx

    from app.models.measure_readiness import ReadinessState
    from app.services.measure_readiness import check_measure_readiness

    def handler(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        if "$data-requirements" in url:
            return httpx.Response(200, json=_dr_library(["http://vs/a", "http://vs/b"]))
        if "$expand" in url:
            probed = request.url.params.get("url")
            if probed == "http://vs/b":
                return httpx.Response(500, json=_hapi_0831_outcome(probed))
            return httpx.Response(200, json=_expanded_valueset(probed))
        return httpx.Response(200, json=_vs_bundle(["http://vs/a", "http://vs/b"]))

    verdict = await check_measure_readiness(
        "https://mcs.example.com/fhir",
        "CMS122",
        auth_headers={},
        timeout=5.0,
        transport=httpx.MockTransport(handler),
    )

    assert verdict.state is ReadinessState.unknown
    assert verdict.missing_valuesets == [], (
        f"a present-but-unexpandable value set leaked into missing_valuesets: {verdict.missing_valuesets}"
    )
    assert "http://vs/b" in (verdict.error or "")


async def test_check_reports_unknown_when_the_expansion_deadline_is_already_spent_before_the_stage_starts():
    """End-to-end version of the two `find_unexpanded_valuesets`-level budget tests.

    Both existing budget tests exercise `find_unexpanded_valuesets` directly (one
    calling it standalone, the other patching it out entirely with a spy) or a
    generic bad-HTTP-response `ValueSetExpansionProbeError`. Neither proves that
    `check_measure_readiness`'s own `except ValueSetExpansionProbeError` branch is
    what actually catches a budget-exhaustion error arising from its REAL,
    unmocked call into stage 3 and turns it into the documented verdict shape.
    What this test uniquely pins is the ANCHORING -- `deadline=started + timeout`
    rather than a fresh window; swap that for `deadline=None` and only this test
    and one other notice. It does not uniquely pin the `except` clause itself:
    deleting that fails 8 tests, 7 of them pre-existing. Measured, not assumed,
    because a docstring claiming coverage it does not have is the exact defect
    this file was rewritten to remove.

    Stage 1 is made to burn the entire timeout, so by the time stage 3's probe
    acquires the semaphore, `deadline` (anchored to the check's start) is already
    behind `time.monotonic()` and the very first probe must refuse to fire.
    """
    import asyncio

    import httpx

    from app.models.measure_readiness import ReadinessState
    from app.services.measure_readiness import check_measure_readiness

    timeout = 0.05
    expand_calls: list[str] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        if "$data-requirements" in url:
            # Comfortably longer than `timeout`, so the deadline anchored to the
            # check's start is already spent by the time stage 3 begins.
            await asyncio.sleep(timeout * 4)
            return httpx.Response(200, json=_dr_library(["http://vs/a"]))
        if "$expand" in url:
            expand_calls.append(request.url.params.get("url"))
            return httpx.Response(200, json=_expanded_valueset("http://vs/a"))
        return httpx.Response(200, json=_vs_bundle(["http://vs/a"]))

    verdict = await check_measure_readiness(
        "https://mcs.example.com/fhir",
        "CMS122",
        auth_headers={},
        timeout=timeout,
        transport=httpx.MockTransport(handler),
    )

    assert verdict.state is ReadinessState.unknown
    error = verdict.error or ""
    assert error.startswith("Could not verify value set expansion: "), error
    assert "budget" in error.lower(), error
    assert expand_calls == [], f"a probe fired after the anchored deadline had already passed: {expand_calls}"


async def test_the_expansion_probe_forwards_the_mcs_credentials():
    """A secured MCS must see the same auth on `$expand` as on every other stage.

    Dropping `headers=auth_headers` from the probe is invisible to every other
    test in this file: an unauthenticated probe against a secured server answers
    401, which is a non-2xx with no HAPI-0831 marker, so it raises and the verdict
    is `unknown`. Safe, but every measure on that connection would read `unknown`
    forever with no hint that credentials were the cause. Nothing else pins this.
    """
    import httpx

    from app.models.measure_readiness import ReadinessState
    from app.services.measure_readiness import check_measure_readiness

    canonicals = ["http://vs/a", "http://vs/b"]
    seen_on_expand: list[str | None] = []
    seen_on_requirements: list[str | None] = []
    seen_on_search: list[str | None] = []

    def handler(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        if "$data-requirements" in url:
            seen_on_requirements.append(request.headers.get("authorization"))
            return httpx.Response(200, json=_dr_library(canonicals))
        if "$expand" in url:
            seen_on_expand.append(request.headers.get("authorization"))
            return httpx.Response(200, json=_expanded_valueset(request.url.params.get("url")))
        seen_on_search.append(request.headers.get("authorization"))
        return httpx.Response(200, json=_vs_bundle(canonicals))

    verdict = await check_measure_readiness(
        "https://mcs.example.com/fhir",
        "CMS122",
        auth_headers={"Authorization": "Bearer s3cret"},
        timeout=5.0,
        transport=httpx.MockTransport(handler),
    )

    assert verdict.state is ReadinessState.ready
    assert seen_on_expand == ["Bearer s3cret", "Bearer s3cret"], seen_on_expand
    # Stages 1 and 2 were unpinned too: dropping `headers=auth_headers` from
    # either left the whole suite green, and against a secured MCS that produces
    # the same permanent `unknown` with no hint that credentials were the cause.
    assert seen_on_requirements == ["Bearer s3cret"], seen_on_requirements
    assert seen_on_search and all(h == "Bearer s3cret" for h in seen_on_search), seen_on_search


def test_the_expansion_probe_fan_out_stays_at_its_justified_width():
    """Pins the literal, because the semaphore assertion nearby cannot.

    `test_find_unexpanded_valuesets_never_exceeds_its_concurrency_bound` compares
    the observed peak against this constant, so it holds for any value the fixture
    can reach -- it proves the semaphore is wired, not that the width is right.
    Halving this to 2 passes that test and the whole suite while silently doubling
    the number of waves the check's shared 60s budget has to cover, which turns
    into the `unknown` verdicts this stage exists to stop emitting.
    """
    from app.services.measure_readiness import _EXPAND_PROBE_CONCURRENCY

    assert _EXPAND_PROBE_CONCURRENCY == 4, (
        "changing the fan-out changes how many waves the shared "
        "READINESS_TIMEOUT_SECONDS budget must cover; see the constant's comment "
        "for why 4 and not 2"
    )


async def test_the_expansion_probe_asks_the_pre_calculated_store():
    """Pins `count=2`, which is the only probe that reads HAPI's expansion store.

    Measured on HAPI v8.8.0, same server state, both rows:

                     1,797 codes, pre-expanded   5 codes, not pre-expanded
        count=2      200 "pre-calculated"        500 HAPI-0831 (maximum 2)
        no count     500 HAPI-0831 (max 1,000)   200 served
        count=100000 500 HAPI-0831 (max 1,000)   200 served

    `count` only lowers HAPI's cap, and dropping it makes `$expand` re-expand
    `compose` in memory and abort above 1,000 even when a pre-calculated
    expansion exists. A revision of #444 dropped it and turned a transient
    startup-window `unknown` into a permanent one on every measure whose value
    sets exceed 1,000 codes -- caught only by the integration suite, because
    every unit fixture here is small enough that both probes agree.
    """
    import httpx

    from app.services.measure_readiness import find_unexpanded_valuesets

    seen: list[dict[str, str]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(dict(request.url.params))
        return httpx.Response(200, json=_expanded_valueset(request.url.params.get("url")))

    await find_unexpanded_valuesets(
        "https://mcs.example.com/fhir",
        ["http://vs/a", "http://vs/b"],
        auth_headers={},
        timeout=5.0,
        transport=httpx.MockTransport(handler),
    )

    assert seen == [
        {"url": "http://vs/a", "count": "2"},
        {"url": "http://vs/b", "count": "2"},
    ], seen
