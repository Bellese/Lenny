"""Per-measure HAPI measure-engine reset.

Defeats the cross-bundle terminology / CodeSystem-stub / compiled-library-cache
shared-state class of bugs by destroying and recreating the `hapi-fhir-measure`
container between measure evaluations. With pre-baked HAPI images
(volumes:[] override in `docker-compose.prebaked.yml`), the container's H2
database lives in the image layer — recreate gives a clean engine in ~5–10s.

The container is identified via the standard compose label
`com.docker.compose.service=hapi-fhir-measure`, so this works under any
compose project name (local stacks, CI, prod).
"""

from __future__ import annotations

import asyncio
import json
import logging
import pathlib
import time
from dataclasses import dataclass

import docker
import httpx

from app.config import settings
from app.services.fhir_client import push_resources, trigger_reindex_and_wait
from app.services.validation import (
    _classify_bundle_entries,
    _prepare_measure_support_resources,
)

logger = logging.getLogger(__name__)

SERVICE_LABEL = "com.docker.compose.service"
HAPI_MEASURE_SERVICE = "hapi-fhir-measure"
DEFAULT_RESET_TIMEOUT_S = 300
HEALTH_POLL_INTERVAL_S = 1.0
SEED_BUNDLES_DIR = pathlib.Path(__file__).resolve().parents[3] / "seed" / "connectathon-bundles"


@dataclass
class ResetTimings:
    """Per-stage timings for a reset cycle (D5 timing logs)."""

    container_found: bool
    remove_ms: float
    create_ms: float
    health_ms: float
    total_ms: float


class MeasureEngineResetError(RuntimeError):
    """Raised when reset fails (container not found, recreate failure, health timeout)."""


def _reset_measure_engine_sync(timeout_s: int) -> ResetTimings:
    """Synchronous reset implementation (runs under asyncio.to_thread)."""
    t_start = time.monotonic()

    client = docker.from_env()
    matches = client.containers.list(
        all=True,
        filters={"label": f"{SERVICE_LABEL}={HAPI_MEASURE_SERVICE}"},
    )
    if not matches:
        raise MeasureEngineResetError(
            f"No container found with label {SERVICE_LABEL}={HAPI_MEASURE_SERVICE}. "
            "Is the compose stack up and is the backend running with /var/run/docker.sock mounted?"
        )

    target = matches[0]
    target.reload()
    attrs = target.attrs

    config = attrs.get("Config", {}) or {}
    host_config = attrs.get("HostConfig", {}) or {}
    network_settings = attrs.get("NetworkSettings", {}) or {}

    name = (attrs.get("Name") or "").lstrip("/")
    image = config.get("Image")
    if not image:
        raise MeasureEngineResetError(f"Container {name} has no image in its config — cannot recreate")
    env = config.get("Env") or []
    labels = config.get("Labels") or {}
    user = config.get("User") or None
    mem_limit = host_config.get("Memory") or 0
    nano_cpus = host_config.get("NanoCpus") or 0
    restart_policy = host_config.get("RestartPolicy") or {}
    raw_networks = network_settings.get("Networks") or {}
    networks = list(raw_networks.keys())
    primary_network = networks[0] if networks else None
    # Compose attaches the service to its project network with two aliases:
    # the service name (e.g. "hapi-fhir-measure") and the container ID. The
    # service-name alias is what makes `http://hapi-fhir-measure:8080/fhir`
    # resolve from the backend. We MUST forward those aliases on recreate or
    # DNS breaks and the health probe (and every subsequent /jobs request)
    # times out.
    primary_aliases: list[str] = []
    if primary_network:
        primary_aliases = list((raw_networks.get(primary_network) or {}).get("Aliases") or [])

    logger.info(
        "Captured measure-engine container config",
        extra={
            "container_name": name,
            "image": image,
            "network": primary_network,
            "aliases": primary_aliases,
            "mem_limit": mem_limit,
            "nano_cpus": nano_cpus,
        },
    )

    t_remove_start = time.monotonic()
    try:
        target.remove(force=True, v=True)
    except docker.errors.APIError as exc:
        raise MeasureEngineResetError(f"Failed to remove existing measure-engine container: {exc}") from exc
    remove_ms = (time.monotonic() - t_remove_start) * 1000.0

    t_create_start = time.monotonic()
    try:
        # Use create+connect+start (not run) so we can pass network aliases,
        # which Docker only accepts at network-attach time, not at run().
        new_container = client.containers.create(
            image=image,
            name=name or None,
            environment=env,
            labels=labels,
            user=user,
            mem_limit=mem_limit if mem_limit else None,
            nano_cpus=nano_cpus if nano_cpus else None,
            restart_policy=restart_policy or None,
            # Don't auto-attach to the default bridge — we'll connect to the
            # captured network with aliases below.
            network_mode="none" if primary_network else None,
            detach=True,
        )
        if primary_network:
            net = client.networks.get(primary_network)
            net.connect(new_container, aliases=primary_aliases or None)
        new_container.start()
    except docker.errors.APIError as exc:
        raise MeasureEngineResetError(f"Failed to recreate measure-engine container: {exc}") from exc
    create_ms = (time.monotonic() - t_create_start) * 1000.0

    t_health_start = time.monotonic()
    deadline = t_health_start + timeout_s
    metadata_url = f"{settings.MEASURE_ENGINE_URL}/metadata"
    last_error: Exception | None = None
    while time.monotonic() < deadline:
        try:
            with httpx.Client(timeout=5.0) as h:
                resp = h.get(metadata_url)
            if resp.status_code == 200:
                break
            last_error = RuntimeError(f"HTTP {resp.status_code} from {metadata_url}")
        except httpx.HTTPError as exc:
            last_error = exc
        time.sleep(HEALTH_POLL_INTERVAL_S)
    else:
        raise MeasureEngineResetError(
            f"Measure engine did not become healthy within {timeout_s}s ({metadata_url} last error: {last_error})"
        )
    health_ms = (time.monotonic() - t_health_start) * 1000.0
    total_ms = (time.monotonic() - t_start) * 1000.0

    return ResetTimings(
        container_found=True,
        remove_ms=remove_ms,
        create_ms=create_ms,
        health_ms=health_ms,
        total_ms=total_ms,
    )


async def reset_measure_engine(timeout_s: int = DEFAULT_RESET_TIMEOUT_S) -> ResetTimings:
    """Reset the `hapi-fhir-measure` container to a clean state.

    Removes the existing container (and its anonymous volume), recreates from
    the same image+config, and waits for `/fhir/metadata` to return 200.

    Raises MeasureEngineResetError on any failure.
    """
    timings = await asyncio.to_thread(_reset_measure_engine_sync, timeout_s)
    logger.info(
        "Measure-engine reset complete",
        extra={
            "reset_ms": round(timings.total_ms, 1),
            "remove_ms": round(timings.remove_ms, 1),
            "create_ms": round(timings.create_ms, 1),
            "health_ms": round(timings.health_ms, 1),
        },
    )
    return timings


@dataclass
class BundleLoadTimings:
    """Per-stage timings for loading a single measure's bundle into the engine."""

    bundle_load_ms: float
    reindex_ms: float


async def load_measure_support_to_engine(measure_id: str) -> BundleLoadTimings:
    """Push Measure/Library/ValueSet/CodeSystem for `measure_id` into the engine.

    Reads `seed/connectathon-bundles/{measure_id}-bundle.json`, classifies
    entries, prepares support resources (CodeSystem stubs, ValueSet alignment),
    pushes them, then runs `$reindex` + waits.

    Raises FileNotFoundError if the bundle file does not exist.
    """
    bundle_path = SEED_BUNDLES_DIR / f"{measure_id}-bundle.json"
    if not bundle_path.exists():
        raise FileNotFoundError(
            f"No connectathon bundle found for measure_id={measure_id} at {bundle_path}. "
            "Phase 1 only supports measures shipped in seed/connectathon-bundles/."
        )

    t_load_start = time.monotonic()
    bundle_json = json.loads(bundle_path.read_bytes())
    measure_defs, _, _ = _classify_bundle_entries(bundle_json)
    primary = [r for r in measure_defs if r.get("resourceType") in ("Measure", "Library")]
    secondary = [r for r in measure_defs if r.get("resourceType") not in ("Measure", "Library")]
    support = await _prepare_measure_support_resources(secondary, bundle_json)
    if support:
        await push_resources(support)
    if primary:
        await push_resources(primary)
    bundle_load_ms = (time.monotonic() - t_load_start) * 1000.0

    t_reindex_start = time.monotonic()
    await asyncio.to_thread(trigger_reindex_and_wait, settings.MEASURE_ENGINE_URL)
    reindex_ms = (time.monotonic() - t_reindex_start) * 1000.0

    logger.info(
        "Measure support load complete",
        extra={
            "measure_id": measure_id,
            "bundle_load_ms": round(bundle_load_ms, 1),
            "reindex_ms": round(reindex_ms, 1),
            "primary_count": len(primary),
            "support_count": len(support),
        },
    )
    return BundleLoadTimings(bundle_load_ms=bundle_load_ms, reindex_ms=reindex_ms)
