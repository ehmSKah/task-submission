"""`make demo`: the assignment's four test categories, live against the running stack.

Each section publishes new orders, prints a ✓ line for each outcome it checks and ends with the
integration test file that proves the same; a summary follows. The run stops and fails at the
first outcome that isn't the one the design promises. It takes under half a minute and can run
again on the same stack without `make clean`.

Usage: python -m demo  (the Makefile sets the stack's host and ports)
"""

import sys
import time

from demo.runner import run
from demo.scenario import SCENARIO
from demo.stack import Endpoints, Stack


def main() -> int:
    endpoints = Endpoints.from_env()
    print(f"task2 demo against the stack on {endpoints.host}\n")
    started = time.monotonic()
    try:
        passed = run(SCENARIO, Stack(endpoints))
    except Exception as error:
        print(f"\nThe demo failed: {type(error).__name__}: {error}")
        print("Is the stack up and healthy? `make up`, then `make ps`.")
        return 1
    took = time.monotonic() - started
    if not passed:
        print(f"\nThe demo failed after {took:.0f} s.")
        return 1
    print(f"\nEvery outcome was the expected one ({took:.0f} s).")
    print("`make demo-tests` runs the four test files named above.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
