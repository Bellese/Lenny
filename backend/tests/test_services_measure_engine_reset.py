"""Tests for measure_engine_reset — reset + per-measure support reload."""

from __future__ import annotations

import json
from unittest.mock import AsyncMock, MagicMock, patch

import docker.errors
import httpx
import pytest

from app.services import measure_engine_reset
from app.services.measure_engine_reset import (
    HAPI_MEASURE_SERVICE,
    SERVICE_LABEL,
    BundleLoadTimings,
    MeasureEngineResetError,
    ResetTimings,
    _reset_measure_engine_sync,
    load_measure_support_to_engine,
    reset_measure_engine,
)

# ---------------------------------------------------------------------------
# Reset — happy path
# ---------------------------------------------------------------------------


def _fake_container(
    name: str = "mct2-hapi-fhir-measure-1",
    aliases: list[str] | None = None,
) -> MagicMock:
    """Build a mock docker-py Container with attrs populated."""
    if aliases is None:
        aliases = ["hapi-fhir-measure", "abcdef123456"]
    container = MagicMock()
    container.attrs = {
        "Name": f"/{name}",
        "Config": {
            "Image": "ghcr.io/bellese/mct2-hapi-measure:latest",
            "Env": ["JAVA_TOOL_OPTIONS=-Xmx1g"],
            "Labels": {SERVICE_LABEL: HAPI_MEASURE_SERVICE},
            "User": "root",
        },
        "HostConfig": {
            "Memory": 2 * 1024 * 1024 * 1024,
            "NanoCpus": 0,
            "RestartPolicy": {"Name": "unless-stopped"},
        },
        "NetworkSettings": {
            "Networks": {
                "mct2_default": {"Aliases": aliases},
            },
        },
    }
    container.reload = MagicMock()
    container.remove = MagicMock()
    return container


def _fake_client_with_network(target_container: MagicMock) -> tuple[MagicMock, MagicMock, MagicMock]:
    """Wire a fake docker client where create→connect→start is observable."""
    fake_client = MagicMock()
    fake_client.containers.list.return_value = [target_container]
    new_container = MagicMock()
    fake_client.containers.create.return_value = new_container
    network = MagicMock()
    fake_client.networks.get.return_value = network
    return fake_client, new_container, network


def test_reset_happy_path_recreates_container_with_network_aliases():
    container = _fake_container(aliases=["hapi-fhir-measure", "9b8c7d6e5f4a"])
    fake_client, new_container, network = _fake_client_with_network(container)

    resp_ok = MagicMock(spec=httpx.Response)
    resp_ok.status_code = 200

    httpx_client = MagicMock()
    httpx_client.__enter__ = MagicMock(return_value=httpx_client)
    httpx_client.__exit__ = MagicMock(return_value=False)
    httpx_client.get.return_value = resp_ok

    with (
        patch.object(measure_engine_reset.docker, "from_env", return_value=fake_client),
        patch.object(measure_engine_reset.httpx, "Client", return_value=httpx_client),
    ):
        timings = _reset_measure_engine_sync(timeout_s=10)

    assert isinstance(timings, ResetTimings)
    assert timings.container_found is True
    assert timings.total_ms > 0
    fake_client.containers.list.assert_called_once_with(
        all=True,
        filters={"label": f"{SERVICE_LABEL}={HAPI_MEASURE_SERVICE}"},
    )
    container.remove.assert_called_once_with(force=True, v=True)

    create_kwargs = fake_client.containers.create.call_args.kwargs
    assert create_kwargs["image"] == "ghcr.io/bellese/mct2-hapi-measure:latest"
    assert create_kwargs["name"] == "mct2-hapi-fhir-measure-1"
    # Must NOT auto-attach to default bridge — we attach with aliases below.
    assert create_kwargs["network_mode"] == "none"

    fake_client.networks.get.assert_called_once_with("mct2_default")
    connect_kwargs = network.connect.call_args.kwargs
    # CRITICAL: the service-name alias ("hapi-fhir-measure") must be forwarded
    # so http://hapi-fhir-measure:8080/fhir keeps resolving from the backend.
    assert connect_kwargs["aliases"] == ["hapi-fhir-measure", "9b8c7d6e5f4a"]
    new_container.start.assert_called_once()


def test_reset_no_container_raises():
    fake_client = MagicMock()
    fake_client.containers.list.return_value = []

    with patch.object(measure_engine_reset.docker, "from_env", return_value=fake_client):
        with pytest.raises(MeasureEngineResetError, match="No container found"):
            _reset_measure_engine_sync(timeout_s=10)


def test_reset_remove_failure_wraps_api_error():
    container = _fake_container()
    container.remove.side_effect = docker.errors.APIError("boom")
    fake_client = MagicMock()
    fake_client.containers.list.return_value = [container]

    with patch.object(measure_engine_reset.docker, "from_env", return_value=fake_client):
        with pytest.raises(MeasureEngineResetError, match="Failed to remove"):
            _reset_measure_engine_sync(timeout_s=10)


def test_reset_recreate_failure_wraps_api_error():
    container = _fake_container()
    fake_client, _, _ = _fake_client_with_network(container)
    fake_client.containers.create.side_effect = docker.errors.APIError("can't bind port")

    with patch.object(measure_engine_reset.docker, "from_env", return_value=fake_client):
        with pytest.raises(MeasureEngineResetError, match="Failed to recreate"):
            _reset_measure_engine_sync(timeout_s=10)


def test_reset_health_timeout_raises():
    container = _fake_container()
    fake_client, _, _ = _fake_client_with_network(container)

    httpx_client = MagicMock()
    httpx_client.__enter__ = MagicMock(return_value=httpx_client)
    httpx_client.__exit__ = MagicMock(return_value=False)
    httpx_client.get.side_effect = httpx.ConnectError("nope")

    # Use a tiny poll interval to keep the test fast; timeout_s=0 forces immediate failure.
    with (
        patch.object(measure_engine_reset.docker, "from_env", return_value=fake_client),
        patch.object(measure_engine_reset.httpx, "Client", return_value=httpx_client),
        patch.object(measure_engine_reset, "HEALTH_POLL_INTERVAL_S", 0.01),
    ):
        with pytest.raises(MeasureEngineResetError, match="did not become healthy"):
            _reset_measure_engine_sync(timeout_s=0)


def test_reset_image_missing_raises():
    container = _fake_container()
    container.attrs["Config"]["Image"] = None
    fake_client = MagicMock()
    fake_client.containers.list.return_value = [container]

    with patch.object(measure_engine_reset.docker, "from_env", return_value=fake_client):
        with pytest.raises(MeasureEngineResetError, match="no image"):
            _reset_measure_engine_sync(timeout_s=10)


@pytest.mark.asyncio
async def test_async_reset_wrapper_dispatches_to_thread():
    fake_timings = ResetTimings(container_found=True, remove_ms=10.0, create_ms=20.0, health_ms=30.0, total_ms=60.0)
    with patch.object(measure_engine_reset, "_reset_measure_engine_sync", return_value=fake_timings) as sync_mock:
        result = await reset_measure_engine(timeout_s=42)
    assert result is fake_timings
    sync_mock.assert_called_once_with(42)


# ---------------------------------------------------------------------------
# load_measure_support_to_engine
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_load_measure_support_missing_bundle_raises(tmp_path, monkeypatch):
    monkeypatch.setattr(measure_engine_reset, "SEED_BUNDLES_DIR", tmp_path)
    with pytest.raises(FileNotFoundError, match="No connectathon bundle found"):
        await load_measure_support_to_engine("CMS124FHIRDoesNotExist")


@pytest.mark.asyncio
async def test_load_measure_support_pushes_only_measure_defs(tmp_path, monkeypatch):
    """MeasureReports in the bundle must NOT be pushed to the measure engine —
    they belong in the ExpectedResult table. _classify_bundle_entries already
    separates them; this guards the contract."""
    measure_id = "CMSXTEST"
    bundle = {
        "resourceType": "Bundle",
        "type": "collection",
        "entry": [
            {"resource": {"resourceType": "Measure", "id": measure_id, "url": f"http://x/Measure/{measure_id}"}},
            {"resource": {"resourceType": "Library", "id": "lib1", "url": "http://x/Library/lib1"}},
            {"resource": {"resourceType": "ValueSet", "id": "vs1", "url": "http://x/ValueSet/vs1"}},
            # Test case MeasureReport — must be filtered out by _classify_bundle_entries.
            {
                "resource": {
                    "resourceType": "MeasureReport",
                    "id": "tc1",
                    "extension": [
                        {
                            "url": "http://hl7.org/fhir/us/cqfmeasures/StructureDefinition/cqfm-isTestCase",
                            "valueBoolean": True,
                        }
                    ],
                    "measure": f"http://x/Measure/{measure_id}",
                    "subject": {"reference": "Patient/p1"},
                    "period": {"start": "2024-01-01", "end": "2024-12-31"},
                    "group": [],
                }
            },
        ],
    }
    bundle_path = tmp_path / f"{measure_id}-bundle.json"
    bundle_path.write_text(json.dumps(bundle))
    monkeypatch.setattr(measure_engine_reset, "SEED_BUNDLES_DIR", tmp_path)

    push_mock = AsyncMock()
    prepare_mock = AsyncMock(return_value=[{"resourceType": "ValueSet", "id": "vs1"}])
    reindex_mock = MagicMock()

    with (
        patch.object(measure_engine_reset, "push_resources", push_mock),
        patch.object(measure_engine_reset, "_prepare_measure_support_resources", prepare_mock),
        patch.object(measure_engine_reset, "trigger_reindex_and_wait", reindex_mock),
    ):
        timings = await load_measure_support_to_engine(measure_id)

    assert isinstance(timings, BundleLoadTimings)
    assert timings.bundle_load_ms > 0
    # Two pushes: support resources, then primary (Measure + Library).
    assert push_mock.await_count == 2
    primary_arg = push_mock.await_args_list[1].args[0]
    rt_set = {r["resourceType"] for r in primary_arg}
    assert rt_set == {"Measure", "Library"}
    # MeasureReport must not appear in any pushed batch.
    for call in push_mock.await_args_list:
        for resource in call.args[0]:
            assert resource["resourceType"] != "MeasureReport"
    reindex_mock.assert_called_once()
