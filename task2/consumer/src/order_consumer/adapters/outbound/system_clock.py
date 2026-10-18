"""The real clock behind the Clock port (ADR 0005)."""

import time


class SystemClock:
    def monotonic(self) -> float:
        return time.monotonic()
