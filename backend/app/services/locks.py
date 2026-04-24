"""Shared async locks for MCT2 services."""

import asyncio

# Module-level lock shared between run_job and run_validation to serialize measure
# engine reloads and evaluations across both flows, preventing concurrent measure
# terminology overwrites.
_measure_engine_lock = asyncio.Lock()
