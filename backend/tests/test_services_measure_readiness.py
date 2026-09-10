"""Tests for the measure readiness check, sweep, and storage model."""

import pytest
import pytest_asyncio


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
