"""The per-type patient scope table, asserted against a real HAPI (#455).

`_PATIENT_SCOPE_PARAM_OVERRIDES` and `_PATIENT_UNSCOPABLE_TYPES` encode an
empirical fact: which search parameter each FHIR resource type accepts to scope
a search to one patient. That fact was measured by hand against HAPI 8.8.0 and,
before this file existed, was recorded only in a code comment.

Every unit test for #455 mocks the CDR and gets 200 back for `subject=` and
`patient=` alike, so those tests only prove "the URL contains whatever the table
says" — tautological with respect to the table. If an entry is wrong, or a HAPI
upgrade changes which parameter a type accepts, the search 400s in production,
the type lands in `failed_types`, and the job still reports success. That is
exactly the silent-data-loss mode of #455 itself: `SDE Payer` was missing from
every report for 66 patients while the job showed 58 processed.

These tests turn the comment block into an executable assertion, so a HAPI bump
breaks loudly here instead of quietly in a measure job.

Per CLAUDE.md's pre-push checklist, run this file explicitly before pushing —
the CI-equivalent suite's `--ignore` flags will not pick up a new file:

    USE_PREBAKED=1 ./scripts/run-integration-tests.sh \\
        tests/integration/test_patient_scope_params.py
"""

import httpx
import pytest

from app.services.fhir_client import (
    _DEFAULT_PATIENT_SCOPE_PARAM,
    _PATIENT_SCOPE_PARAM_OVERRIDES,
    _PATIENT_UNSCOPABLE_TYPES,
)

pytestmark = pytest.mark.integration

# A patient id need not exist for these assertions. HAPI validates the search
# parameter before it looks anything up, so an unknown id still yields 200 with
# an empty bundle for a supported parameter and 400 (HAPI-0524) for an
# unsupported one. Using a literal keeps the test independent of seed data.
_PROBE_PATIENT_ID = "scope-param-probe"


def _search(cdr_url: str, resource_type: str, param: str) -> httpx.Response:
    return httpx.get(
        f"{cdr_url}/{resource_type}",
        params={param: f"Patient/{_PROBE_PATIENT_ID}", "_count": "1"},
        timeout=30,
    )


@pytest.mark.parametrize(
    "resource_type,expected_param",
    sorted(_PATIENT_SCOPE_PARAM_OVERRIDES.items()),
)
def test_overridden_type_accepts_its_param_and_rejects_the_default(cdr_url, resource_type, expected_param):
    """Each override exists because the type rejects the default parameter.

    Both halves matter. The accept half proves the override works; the reject
    half proves the override is still *needed* — if a HAPI version starts
    accepting `subject=` for Coverage, this fails and the entry can be retired
    rather than carried forever as folklore.
    """
    rejected_param = _DEFAULT_PATIENT_SCOPE_PARAM
    assert expected_param != rejected_param, (
        f"{resource_type} is in the override map but its param equals the default "
        f"({rejected_param!r}) — the entry is a no-op and should be removed"
    )

    accepted = _search(cdr_url, resource_type, expected_param)
    assert accepted.status_code == 200, (
        f"{resource_type}?{expected_param}= should be accepted but returned "
        f"{accepted.status_code}: {accepted.text[:300]}"
    )

    rejected = _search(cdr_url, resource_type, rejected_param)
    assert rejected.status_code == 400, (
        f"{resource_type} now accepts {rejected_param}= (HTTP {rejected.status_code}) — "
        f"the override map is stale and this entry can be dropped"
    )


@pytest.mark.parametrize("resource_type", _PATIENT_UNSCOPABLE_TYPES)
def test_unscopable_type_rejects_both_params(cdr_url, resource_type):
    """These four are skipped without a request; that is only correct while both fail.

    If a HAPI version adds a patient-scoped parameter for one of them, skipping
    it silently drops data the gather could now fetch — so this must fail and
    force the type out of `_PATIENT_UNSCOPABLE_TYPES`.
    """
    for param in ("subject", "patient"):
        resp = _search(cdr_url, resource_type, param)
        assert resp.status_code == 400, (
            f"{resource_type} now accepts {param}= (HTTP {resp.status_code}) — it is no "
            f"longer unscopable and must be removed from _PATIENT_UNSCOPABLE_TYPES so "
            f"the gather stops skipping it"
        )


@pytest.mark.parametrize(
    "resource_type",
    ["Condition", "Observation", "Encounter", "Procedure", "MedicationRequest", "AdverseEvent"],
)
def test_default_param_types_accept_the_default(cdr_url, resource_type):
    """Types absent from the override map must actually accept the default.

    `AdverseEvent` is the load-bearing case: it is the one type that accepts
    `subject=` and rejects `patient=`, which is why the default cannot simply be
    swapped to `patient` and why the map is per-type rather than global.
    """
    resp = _search(cdr_url, resource_type, _DEFAULT_PATIENT_SCOPE_PARAM)
    assert resp.status_code == 200, (
        f"{resource_type} rejects the default {_DEFAULT_PATIENT_SCOPE_PARAM}= parameter "
        f"(HTTP {resp.status_code}) — it needs an entry in _PATIENT_SCOPE_PARAM_OVERRIDES: "
        f"{resp.text[:300]}"
    )


def test_adverse_event_rejects_patient_param(cdr_url):
    """The inverse case, pinned explicitly.

    A future "simplification" that swaps every type to `patient=` would pass the
    override tests above and break only here.
    """
    resp = _search(cdr_url, "AdverseEvent", "patient")
    assert resp.status_code == 400, (
        f"AdverseEvent now accepts patient= (HTTP {resp.status_code}) — the default-vs-override "
        f"split exists because it did not, so re-check the whole table"
    )
