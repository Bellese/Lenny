"""Integration tests for the patient-scoped wipe (issue #392).

The unit tests assert the URLs `wipe_patients_by_id` builds. They cannot assert
what those URLs actually DO, which is the part that matters here: whether a real
FHIR server honours `DELETE {Type}?patient=<ids>` as a scoped delete, and whether
the search-parameter map is right for every type in the sweep.

The bug being guarded is destructive and silent. Before #392, starting a job
against a shared MCS deleted every patient on it — no prompt, no warning log, no
undo. So the central assertion below is about a bystander: a patient the job never
touches must still be there, with its clinical resources intact, after the wipe.

These tests only ever wipe patients whose ids carry the `lenny-392-` or
`lenny-458-` prefix, so they cannot disturb the session-scoped seed data other
integration tests rely on.
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
    """Teardown must not be the code under test.

    `wipe_patients_by_id` now raises on an unresolvable conflict, and this runs
    from `finally:` — so a teardown raise would replace the real assertion error
    with the wipe's own, which is exactly what makes #458-class bugs unreadable in
    CI. The newer tests below already tear down with `_force_delete`; this brings
    the original three into line.
    """
    async with httpx.AsyncClient(timeout=120.0) as client:
        for patient_id in (_TARGET, _BYSTANDER):
            refs = [f"{r['resourceType']}/{r['id']}" for r in _clinical_resources(patient_id)]
            refs.append(f"Patient/{patient_id}")
            await client.post(
                measure_url,
                json={
                    "resourceType": "Bundle",
                    "type": "transaction",
                    "entry": [{"request": {"method": "DELETE", "url": ref}} for ref in refs],
                },
                headers={"Content-Type": "application/fhir+json"},
            )


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


# ---------------------------------------------------------------------------
# Reference conflicts (issue #458)
# ---------------------------------------------------------------------------
#
# HAPI's `enforce_referential_integrity_on_delete` defaults to true and this repo
# never sets it, so `DELETE {Type}?patient=` is refused while any resource points
# at a match. The sweep used to absorb that 409 and log "Scoped wipe complete"
# with the resource still resident. These tests cannot be written as unit tests:
# the whole behaviour under test is the server's, and a mock asserting HAPI 409s
# is just a restatement of the assumption being checked. Each one therefore
# probes the conflict for real first, so that a server which does NOT enforce
# integrity on delete fails the precondition instead of passing vacuously. That
# guard already paid for itself: the first version of the cycle fixture seeded
# both resources in one batch bundle and the probe caught that no cycle existed
# — see `_cycle_resources` for why a single write cannot produce one.

_PINNED = "lenny-458-pinned"
_CYCLE = "lenny-458-cycle"


def _pinned_encounter_resources(patient_id: str) -> list[dict]:
    """An Encounter pinned by a Procedure the sweep deletes *after* it.

    `Procedure` sits after `Encounter` in `_PATIENT_SCOPED_TYPES`, so the
    Encounter's conditional delete is refused on the first pass. This is the
    acyclic case from #458's issue body, and the one an ordered retry clears.
    """
    ref = {"reference": f"Patient/{patient_id}"}
    return [
        {
            "resourceType": "Encounter",
            "id": f"{patient_id}-enc",
            "status": "finished",
            "class": {"code": "AMB"},
            "subject": ref,
        },
        {
            "resourceType": "Procedure",
            "id": f"{patient_id}-proc",
            "status": "completed",
            "subject": ref,
            "encounter": {"reference": f"Encounter/{patient_id}-enc"},
        },
    ]


def _cycle_resources(patient_id: str) -> tuple[list[dict], list[dict]]:
    """A Condition and an Encounter that reference each other, in two writes.

    `Encounter.reasonReference -> Condition` and `Condition.encounter ->
    Encounter` are both conformant R4, so this is a cycle at the *type* level,
    not an artifact of one dataset — no order over `_PATIENT_SCOPED_TYPES` clears
    it. Found in unmodified MADiE CMS506 data on the CMS connectathon server,
    where the surviving Condition also stranded the Patient.

    Two writes, not one, because of how HAPI decides what blocks a delete: the
    `HFJ_RES_LINK` row that makes a reference enforceable carries a resolved
    target PID, and with `enforce_referential_integrity_on_write=false` a
    reference to a resource that does not exist *yet* is stored without one. A
    single bundle therefore produces a half-cycle whichever order its entries are
    in — the second-written resource pins the first, and the first pins nothing.
    Writing the Encounter, then the Condition that points at it, then the
    Encounter again with its `reasonReference`, resolves both directions. The
    test below asserts both halves pin before it trusts its own fixture.
    """
    ref = {"reference": f"Patient/{patient_id}"}
    encounter = {
        "resourceType": "Encounter",
        "id": f"{patient_id}-enc",
        "status": "finished",
        "class": {"code": "AMB"},
        "subject": ref,
    }
    condition = {
        "resourceType": "Condition",
        "id": f"{patient_id}-cond",
        "subject": ref,
        "clinicalStatus": {"coding": [{"code": "active"}]},
        "encounter": {"reference": f"Encounter/{patient_id}-enc"},
    }
    closing = dict(encounter, reasonReference=[{"reference": f"Condition/{patient_id}-cond"}])
    return [encounter, condition], [closing]


async def _push(measure_url: str, resources: list[dict]) -> None:
    async with httpx.AsyncClient(timeout=120.0) as client:
        resp = await client.post(
            measure_url,
            json=_put_bundle(resources),
            headers={"Content-Type": "application/fhir+json"},
        )
        resp.raise_for_status()


async def _delete_status(measure_url: str, ref: str) -> int:
    """Issue a plain instance DELETE and report the status, deleting nothing on 409."""
    async with httpx.AsyncClient(timeout=60.0) as client:
        resp = await client.delete(f"{measure_url}/{ref}")
    return resp.status_code


async def _force_delete(measure_url: str, refs: list[str]) -> None:
    """Teardown that does not depend on the code under test.

    A transaction Bundle, so a mutually-referencing pair goes in one commit even
    when the assertions above have already failed.
    """
    bundle = {
        "resourceType": "Bundle",
        "type": "transaction",
        "entry": [{"request": {"method": "DELETE", "url": ref}} for ref in refs],
    }
    async with httpx.AsyncClient(timeout=120.0) as client:
        await client.post(measure_url, json=bundle, headers={"Content-Type": "application/fhir+json"})


async def test_scoped_wipe_clears_an_encounter_pinned_by_a_later_type(measure_url):
    """The acyclic #458 case: a 409 that a retry clears.

    Before the fix the sweep issued `DELETE Encounter?patient=` once, took the
    409, and moved on — leaving the Encounter attached to a patient the next job
    is about to re-push and re-evaluate.
    """
    await _push(measure_url, [_patient(_PINNED)] + _pinned_encounter_resources(_PINNED))
    refs = [f"Procedure/{_PINNED}-proc", f"Encounter/{_PINNED}-enc", f"Patient/{_PINNED}"]

    try:
        assert await _delete_status(measure_url, f"Encounter/{_PINNED}-enc") == 409, (
            "the target server does not enforce referential integrity on delete, so this "
            "test cannot observe the bug it exists for — check "
            "hapi.fhir.enforce_referential_integrity_on_delete on the measure engine"
        )

        await wipe_patients_by_id(base_url=measure_url, patient_ids=[_PINNED])

        assert not await _exists(measure_url, "Encounter", f"{_PINNED}-enc"), (
            "the Encounter survived a wipe that reported success — this is #458"
        )
        assert not await _exists(measure_url, "Procedure", f"{_PINNED}-proc")
        assert not await _exists(measure_url, "Patient", _PINNED)
    finally:
        await _force_delete(measure_url, refs)


async def test_scoped_wipe_clears_a_condition_encounter_reference_cycle(measure_url):
    """The case no ordering can fix, against a server that really enforces it.

    Both halves 409 on every pass, so the retry terminates with the pair intact
    and the transaction Bundle is what clears them. The Patient is the tell: it is
    last in the sweep and cannot be deleted while the Condition survives, so
    "Patient gone" proves the cycle was actually broken rather than skipped.
    """
    first, closing = _cycle_resources(_CYCLE)
    await _push(measure_url, [_patient(_CYCLE)] + first)
    await _push(measure_url, closing)
    refs = [f"Condition/{_CYCLE}-cond", f"Encounter/{_CYCLE}-enc", f"Patient/{_CYCLE}"]

    try:
        # Both halves, in both directions, before trusting the fixture. A probe
        # that only checked one direction is what a single-bundle seed passes:
        # the resource written second pins the first, and the cycle is not there.
        assert await _delete_status(measure_url, f"Condition/{_CYCLE}-cond") == 409, (
            "the Condition deleted on its own, so nothing is pinning it and this test proves "
            "nothing — check that Encounter.reasonReference resolved at write time"
        )
        assert await _delete_status(measure_url, f"Encounter/{_CYCLE}-enc") == 409, (
            "the Encounter deleted on its own — Condition.encounter did not resolve, so this "
            "is a half-cycle that ordering alone would clear"
        )

        await wipe_patients_by_id(base_url=measure_url, patient_ids=[_CYCLE])

        assert not await _exists(measure_url, "Condition", f"{_CYCLE}-cond")
        assert not await _exists(measure_url, "Encounter", f"{_CYCLE}-enc")
        assert not await _exists(measure_url, "Patient", _CYCLE), (
            "the Patient is stranded behind a surviving Condition — the cycle was not cleared"
        )
    finally:
        await _force_delete(measure_url, refs)


async def test_scoped_wipe_refuses_to_report_success_when_a_bystander_pins_the_data(measure_url):
    """The fail-loud end of the chain, with a referrer the wipe cannot free.

    A second patient's Encounter references the target's Condition. No delete the
    scoped wipe is allowed to issue can free it — deleting the bystander's
    Encounter is exactly the cross-tenant damage #392 exists to prevent — so the
    only correct outcome is a raised error, not a wipe that reports success while
    the Condition stays attached to a patient the next job will re-evaluate.
    """
    target = f"{_PINNED}-loud"
    bystander = f"{_PINNED}-loud-bystander"
    resources = [
        _patient(target),
        _patient(bystander),
        {
            "resourceType": "Condition",
            "id": f"{target}-cond",
            "subject": {"reference": f"Patient/{target}"},
            "clinicalStatus": {"coding": [{"code": "active"}]},
        },
        {
            "resourceType": "Encounter",
            "id": f"{bystander}-enc",
            "status": "finished",
            "class": {"code": "AMB"},
            "subject": {"reference": f"Patient/{bystander}"},
            "reasonReference": [{"reference": f"Condition/{target}-cond"}],
        },
    ]
    await _push(measure_url, resources)
    refs = [
        f"Encounter/{bystander}-enc",
        f"Condition/{target}-cond",
        f"Patient/{target}",
        f"Patient/{bystander}",
    ]

    try:
        with pytest.raises(RuntimeError, match="still-referenced"):
            await wipe_patients_by_id(base_url=measure_url, patient_ids=[target])

        # And the bystander is untouched: failing loudly must not become a licence
        # to delete the thing that was in the way.
        assert await _exists(measure_url, "Encounter", f"{bystander}-enc"), (
            "the wipe deleted another patient's resource to clear its own conflict"
        )
        assert await _exists(measure_url, "Patient", bystander)
    finally:
        await _force_delete(measure_url, refs)
