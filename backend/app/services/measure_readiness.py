"""Measure readiness: can the active MCS actually evaluate this measure?

Two questions, in order:
  1. Does `$data-requirements` succeed? That is the compile check — it is what
     fails when a Library the CQL includes is absent.
  2. Is every ValueSet canonical the server named actually present?

Lenny parses no CQL. The server computes the dependency closure and returns it.
"""

import logging

from sqlalchemy import update as sa_update
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.measure_readiness import MeasureReadiness, ReadinessState

logger = logging.getLogger(__name__)


async def reclaim_stranded_checks(session: AsyncSession) -> int:
    """Convert rows left in `checking` by a crashed sweep into `unknown`.

    Called at startup. `asyncio.create_task` does not survive a restart, so
    without this a killed container leaves a spinner that never resolves.
    Returns the number of rows reclaimed.
    """
    result = await session.execute(
        sa_update(MeasureReadiness)
        .where(MeasureReadiness.state == ReadinessState.checking)
        .values(state=ReadinessState.unknown, error="Interrupted by backend restart")
    )
    await session.commit()
    return result.rowcount or 0
