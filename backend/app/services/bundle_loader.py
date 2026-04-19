"""Startup bundle loader — scans a directory and loads each FHIR bundle.

Called once during FastAPI lifespan startup. Safe to re-run (upserts).
"""

from __future__ import annotations

import asyncio
import json
import logging
import pathlib
from typing import Any

import httpx

from app.config import settings
from app.db import async_session
from app.services.validation import triage_test_bundle

logger = logging.getLogger(__name__)

_DEFAULT_DIR = pathlib.Path(__file__).resolve().parents[3] / "seed" / "connectathon-bundles"
_HAPI_READY_RETRIES = 20
_HAPI_RETRY_DELAY = 5.0


async def _wait_for_hapi() -> None:
    """Block until both HAPI instances respond, retrying up to _HAPI_READY_RETRIES times."""
    urls = [settings.MEASURE_ENGINE_URL, settings.DEFAULT_CDR_URL]
    async with httpx.AsyncClient(timeout=5.0) as client:
        for attempt in range(_HAPI_READY_RETRIES):
            try:
                for url in urls:
                    await client.get(f"{url}/metadata")
                logger.info("HAPI instances ready, starting bundle load")
                return
            except Exception:
                if attempt == 0:
                    logger.info("Waiting for HAPI instances to be ready...")
                await asyncio.sleep(_HAPI_RETRY_DELAY)
    logger.warning("HAPI instances not ready after %d attempts, proceeding anyway", _HAPI_READY_RETRIES)


async def load_connectathon_bundles(
    directory: pathlib.Path | None = None,
) -> dict[str, Any]:
    """Load all FHIR bundle .json files in the given directory.

    Routes each bundle using triage_test_bundle:
    - Measure/Library/ValueSet → MCS
    - Clinical resources → CDR (only if using default CDR)
    - Test case MeasureReports → ExpectedResult DB table (upsert)

    Returns summary dict: {"loaded": N, "failed": N, "details": [...]}
    """
    scan_dir = directory or _DEFAULT_DIR

    if not scan_dir.exists():
        logger.info(
            "Connectathon bundles directory does not exist, skipping startup load",
            extra={"directory": str(scan_dir)},
        )
        return {"loaded": 0, "failed": 0, "details": []}

    bundle_files = sorted(scan_dir.glob("*.json"))
    if not bundle_files:
        logger.info(
            "No bundle files found in connectathon bundles directory",
            extra={"directory": str(scan_dir)},
        )
        return {"loaded": 0, "failed": 0, "details": []}

    await _wait_for_hapi()

    loaded = 0
    failed = 0
    details: list[dict[str, Any]] = []

    for bundle_path in bundle_files:
        try:
            bundle_json = json.loads(bundle_path.read_bytes())
            async with async_session() as session:
                summary = await triage_test_bundle(bundle_json, bundle_path.name, session)
            loaded += 1
            details.append({"file": bundle_path.name, "status": "loaded", **summary})
            logger.info(
                "Loaded connectathon bundle",
                extra={"file": bundle_path.name, **summary},
            )
        except Exception as exc:
            failed += 1
            details.append({"file": bundle_path.name, "status": "failed", "error": str(exc)})
            logger.warning(
                "Failed to load connectathon bundle: %s — %s",
                bundle_path.name,
                str(exc),
                extra={"file": bundle_path.name, "error": str(exc)},
            )

    logger.info(
        "Connectathon bundle startup load complete",
        extra={"loaded": loaded, "failed": failed, "total": len(bundle_files)},
    )
    return {"loaded": loaded, "failed": failed, "details": details}
