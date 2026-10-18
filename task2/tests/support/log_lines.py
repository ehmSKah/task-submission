"""The consumer's log lines as written, for tests that check what reaches the logs (ADR 0008)."""

import io
import json
import sys
import threading
from typing import Any


class LogLines(io.TextIOBase):
    """A stream for `logs.configure` that keeps every line, and still echoes it, so pytest shows
    a failing test's lines. Each line arrives in one write (ADR 0008)."""

    def __init__(self) -> None:
        self._lines: list[str] = []
        self._lock = threading.Lock()

    def write(self, text: str) -> int:
        with self._lock:
            self._lines.extend(text.splitlines())
        sys.stdout.write(text)
        return len(text)

    def flush(self) -> None:
        sys.stdout.flush()

    def clear(self) -> None:
        with self._lock:
            self._lines.clear()

    def text(self) -> str:
        """Every line as written, for checking what must never appear."""
        with self._lock:
            return "\n".join(self._lines)

    def entries(self) -> list[dict[str, Any]]:
        with self._lock:
            return [json.loads(line) for line in self._lines]
