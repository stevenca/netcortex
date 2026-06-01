"""Reference stub ``SensoryAdapter`` used by contract tests.

Emits three canned events with stable identifiers so the contract test can
verify field-level invariants without depending on any real backend.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import datetime, timezone

from netcortex.contracts.sensory_adapter import SensoryAdapter, SensoryEvent


class StubSensoryAdapter(SensoryAdapter):
    adapter_id = "stub"

    async def discover(self) -> AsyncIterator[SensoryEvent]:  # type: ignore[override]
        now = datetime.now(tz=timezone.utc)
        for i in range(3):
            yield SensoryEvent(
                modality="stub.poll",
                received_at=now,
                occurred_at=now,
                target={"device_id": f"stub-{i}"},
                payload={"index": i, "value": "ok"},
                confidence=1.0,
                provenance=(self.adapter_id,),
            )
