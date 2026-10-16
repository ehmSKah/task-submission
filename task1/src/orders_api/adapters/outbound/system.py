"""The real `Clock` and `IdGenerator` (ADR 0009); tests use fixed stand-ins instead."""

import uuid
from datetime import UTC, datetime


class SystemClock:
    def now(self) -> datetime:
        return datetime.now(UTC)


class RandomOrderIds:
    def new_id(self) -> str:
        """Random, so ids reveal neither creation time nor volume (ADR 0004)."""
        return str(uuid.uuid4())
