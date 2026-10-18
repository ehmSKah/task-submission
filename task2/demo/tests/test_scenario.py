"""The demo's four sections are the assignment's four test categories, each naming the
integration test file that proves it, and `make demo-tests` runs exactly those files."""

import re
from pathlib import Path

from demo.scenario import SCENARIO

TASK2 = Path(__file__).parents[2]


def test_the_sections_are_the_assignments_four_test_categories_in_order() -> None:
    assert [section.title for section in SCENARIO] == [
        "1. Successful processing",
        "2. Invalid messages",
        "3. Duplicate events",
        "4. Downstream API failures",
    ]


def test_each_section_is_proven_by_its_own_integration_test_file() -> None:
    assert [section.proven_by for section in SCENARIO] == [
        "tests/integration/test_successful_processing.py",
        "tests/integration/test_invalid_messages.py",
        "tests/integration/test_duplicate_events.py",
        "tests/integration/test_downstream_failures.py",
    ]
    for section in SCENARIO:
        assert (TASK2 / section.proven_by).is_file(), section.proven_by


def test_make_demo_tests_runs_exactly_the_files_the_sections_name() -> None:
    makefile = (TASK2 / "Makefile").read_text()
    [files] = re.findall(r"^DEMO_TESTS := (.+)$", makefile, flags=re.MULTILINE)
    [recipe] = re.findall(r"^demo-tests:.*\n\t(.+)$", makefile, flags=re.MULTILINE)

    assert files.split() == [section.proven_by for section in SCENARIO]
    assert recipe == "$(PYTEST) $(DEMO_TESTS) $(PYTEST_ARGS)"
