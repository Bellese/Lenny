"""Integration tests for the patient-scoped wipe (issue #392).

The unit tests assert the URLs `wipe_patients_by_id` builds. They cannot assert
what those URLs actually DO, which is the part that matters here: whether a real
FHIR server honours `DELETE {Type}?patient=<ids>` as a scoped delete, and whether
the search-parameter map is right for every type in the sweep.

The bug being guarded is destructive and silent. Before #392, starting a job
against a shared MCS deleted every patient on it — no prompt, no warning log, no
undo. So the central assertion below is about a bystander: a patient the job never
touches must still be there, with its clinical resources intact, after the wipe.

These tests only ever wipe patients whose ids carry the `lenny-392-` prefix, so
they cannot disturb the session-scoped seed data other integration tests rely on.
"""

import httpx
import pytest

from app.services.fhir_client import wipe_patients_by_id

pytestmark = [pytest.mark.integration, pytest.mark.asyncio]

_TARGET = "lenny-392-target"
_BYSTANDER = "lenny-392-bystander"


def _patient(patient_id: str) -> dict:
    return {"resourceType": "Patient", "id": patient_id, "name": [{"family": patient_id}]}


def _clinical_resources(patient_id: str) -> list[dict]:
    """One resource per interesting branch of the scoped-wipe sweep.

    Condition covers the ordinary `patient=` case. AdverseEvent covers the one
    type HAPI answers on `subject=` instead — it 400s on `patient=`, which is why
    the map was built by probing the server rather than reading the R4 spec.
    Encounter is included because it is the type most measures actually retrieve.
    """
    ref = {"reference": f"Patient/{patient_id}"}
    return [
        {
            "resourceType": "Condition",
            "id": f"{patient_id}-cond",
            "subject": ref,
            "clinicalStatus": {"coding": [{"code": "active"}]},
        },
        {
            "resourceType": "Encounter",
            "id": f"{patient_id}-enc",
            "status": "finished",
            "class": {"code": "AMB"},
            "subject": ref,
        },
        {
            "resourceType": "AdverseEvent",
            "id": f"{patient_id}-ae",
            "subject": ref,
            "actuality": "actual",
        },
    ]


def _put_bundle(resources: list[dict]) -> dict:
    return {
        "resourceType": "Bundle",
        "type": "batch",
        "entry": [
            {"resource": r, "request": {"method": "PUT", "url": f"{r['resourceType']}/{r['id']}"}} for r in resources
        ],
    }


async def _seed(measure_url: str) -> None:
    """PUT both patients and their clinical resources onto the measure engine."""
    resources = [_patient(_TARGET), _patient(_BYSTANDER)]
    resources += _clinical_resources(_TARGET)
    resources += _clinical_resources(_BYSTANDER)
    async with httpx.AsyncClient(timeout=120.0) as client:
        resp = await client.post(
            measure_url,
            json=_put_bundle(resources),
            headers={"Content-Type": "application/fhir+json"},
        )
        resp.raise_for_status()


async def _exists(measure_url: str, resource_type: str, resource_id: str) -> bool:
    """Direct read, deliberately not a search.

    Per CLAUDE.md's HAPI async-indexing section, a direct `GET /{Type}/{id}` works
    regardless of index state, while a search can return a stale snapshot. Using a
    search here would make this test flaky in exactly the way that doc warns about.
    """
    async with httpx.AsyncClient(timeout=60.0) as client:
        resp = await client.get(f"{measure_url}/{resource_type}/{resource_id}")
    return resp.status_code == 200


async def _cleanup(measure_url: str) -> None:
    await wipe_patients_by_id(base_url=measure_url, patient_ids=[_TARGET, _BYSTANDER])


async def test_scoped_wipe_leaves_other_patients_intact(measure_url):
    """The acceptance criterion for #392, against a real FHIR server.

    A full wipe here would delete the bystander too. That is precisely what a job
    against a shared connectathon MCS used to do to other participants' data.
    """
    await _seed(measure_url)
    assert await _exists(measure_url, "Patient", _TARGET), "seed failed"
    assert await _exists(measure_url, "Patient", _BYSTANDER), "seed failed"

    try:
        await wipe_patients_by_id(base_url=measure_url, patient_ids=[_TARGET])

        # The target and everything hanging off it is gone.
        assert not await _exists(measure_url, "Patient", _TARGET)
        assert not await _exists(measure_url, "Condition", f"{_TARGET}-cond")
        assert not await _exists(measure_url, "Encounter", f"{_TARGET}-enc")
        assert not await _exists(measure_url, "AdverseEvent", f"{_TARGET}-ae"), (
            "AdverseEvent survived — the sweep must scope it with subject=, not patient="
        )

        # The bystander is untouched. This is the whole point of the issue.
        assert await _exists(measure_url, "Patient", _BYSTANDER), (
            "the scoped wipe deleted a patient it was not given — this is the #392 bug"
        )
        assert await _exists(measure_url, "Condition", f"{_BYSTANDER}-cond")
        assert await _exists(measure_url, "Encounter", f"{_BYSTANDER}-enc")
        assert await _exists(measure_url, "AdverseEvent", f"{_BYSTANDER}-ae")
    finally:
        await _cleanup(measure_url)


async def test_scoped_wipe_with_no_patients_deletes_nothing(measure_url):
    """An empty patient list must not degrade into an unscoped sweep.

    A job that gathers zero patients reaches the wipe with an empty list. If that
    fell through to `DELETE {Type}?_lastUpdated=gt1900-01-01`, the safe default
    would quietly become the destructive one.
    """
    await _seed(measure_url)
    try:
        await wipe_patients_by_id(base_url=measure_url, patient_ids=[])

        assert await _exists(measure_url, "Patient", _TARGET)
        assert await _exists(measure_url, "Patient", _BYSTANDER)
        assert await _exists(measure_url, "Condition", f"{_TARGET}-cond")
    finally:
        await _cleanup(measure_url)


async def test_scoped_wipe_is_idempotent(measure_url):
    """Wiping patients that are already gone must not raise.

    Every job starts with this wipe, so the first job against a fresh server hits
    exactly this case. A 404-intolerant sweep would fail every such job.
    """
    await _seed(measure_url)
    await wipe_patients_by_id(base_url=measure_url, patient_ids=[_TARGET, _BYSTANDER])

    # Second pass over an already-empty set.
    await wipe_patients_by_id(base_url=measure_url, patient_ids=[_TARGET, _BYSTANDER])

    assert not await _exists(measure_url, "Patient", _TARGET)
    assert not await _exists(measure_url, "Patient", _BYSTANDER)
