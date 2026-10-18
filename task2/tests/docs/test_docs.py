"""The reviewer's documents are self-contained, their links resolve, and they say what the
assignment asks for under its own names.

A reviewer gets this folder and nothing else: no issue tracker, no working branches, no files for
coding assistants. The repository's README and its workflows ship beside the folder, so they are
scanned for such links too. Relative links in the Markdown documents must point at files that
exist (and at headings that exist, where they name one): that is what proves every path in the
README's "Deliverables at a glance" table. The design document summarises every ADR, and every
`make` target the README and the design document name exists.
"""

import re
from pathlib import Path

import pytest

THIS_FILE = Path(__file__).resolve()
TASK2 = THIS_FILE.parents[2]
REPO = TASK2.parent
README = TASK2 / "README.md"
DESIGN = TASK2 / "docs" / "design.md"

SKIPPED_DIRS = {"build", "__pycache__"}
"""Plus every directory whose name starts with a dot: `.venv` and the tools' caches."""
DELIVERABLE_SUFFIXES = {".md", ".py", ".yaml", ".yml", ".toml", ".json", ".sql", ".sh"}
DELIVERABLE_NAMES = {"Makefile", "Dockerfile", ".gitignore", ".dockerignore"}

FORBIDDEN = {
    "a GitHub issue or pull request": re.compile(r"github\.com/[^/\s]+/[^/\s]+/(issues|pull)\b"),
    "a research branch": re.compile(r"\bresearch/"),
    "an agent file": re.compile(r"CLAUDE\.md|docs/agents|\.claude/"),
}

MARKDOWN_LINK = re.compile(r"\[[^\]]*\]\(\s*<?([^)\s>]+)>?(?:\s+\"[^\"]*\")?\s*\)")
FENCED_CODE = re.compile(r"^(```|~~~).*?^\1", re.MULTILINE | re.DOTALL)
INLINE_CODE = re.compile(r"`[^`\n]*`")
HEADING = re.compile(r"^(#{1,6})\s+(.+?)\s*#*\s*$", re.MULTILINE)


def headings(document: Path, level: int | None = None) -> list[str]:
    text = FENCED_CODE.sub("", document.read_text())
    return [
        heading
        for hashes, heading in HEADING.findall(text)
        if level is None or len(hashes) == level
    ]


def slug(heading: str) -> str:
    """GitHub's anchor for a heading: lower case, punctuation dropped, spaces to hyphens."""
    text = re.sub(r"[`*_]|\[([^\]]*)\]\([^)]*\)", r"\1", heading).strip().lower()
    return re.sub(r"[^\w\- ]", "", text).replace(" ", "-")


def anchors(document: Path) -> set[str]:
    """A repeated heading gets `-1`, `-2`, … appended, as on GitHub."""
    seen: dict[str, int] = {}
    result = set()
    for heading in headings(document):
        base = slug(heading)
        count = seen.get(base, 0)
        seen[base] = count + 1
        result.add(base if count == 0 else f"{base}-{count}")
    return result


def relative_links(document: Path) -> list[str]:
    text = INLINE_CODE.sub("", FENCED_CODE.sub("", document.read_text()))
    return [
        target
        for target in MARKDOWN_LINK.findall(text)
        if not re.match(r"^[a-z][a-z0-9+.-]*:", target)  # http:, https:, mailto:
    ]


def broken_link(document: Path, target: str) -> str | None:
    path_part, _, anchor = target.partition("#")
    linked = (document.parent / path_part).resolve() if path_part else document
    if not linked.exists():
        return f"{target}: {linked} doesn't exist"
    if anchor and linked.suffix == ".md" and anchor not in anchors(linked):
        return f"{target}: no heading '#{anchor}' in {linked.name}"
    return None


def deliverables() -> list[Path]:
    found = []
    for path in TASK2.rglob("*"):
        directories = path.relative_to(TASK2).parts[:-1]
        if any(part in SKIPPED_DIRS or part.startswith(".") for part in directories):
            continue
        if not path.is_file() or path.resolve() == THIS_FILE:
            continue
        if path.suffix in DELIVERABLE_SUFFIXES or path.name in DELIVERABLE_NAMES:
            found.append(path)
    return sorted(found)


def shipped_beside() -> list[Path]:
    """The repository's README and everything under `.github/`, where they exist."""
    readme = REPO / "README.md"
    github = REPO / ".github"
    files = sorted(path for path in github.rglob("*") if path.is_file()) if github.is_dir() else []
    return ([readme] if readme.is_file() else []) + files


def label(path: Path) -> str:
    return path.relative_to(REPO).as_posix()


def markdown_documents() -> list[Path]:
    return [path for path in deliverables() if path.suffix == ".md"]


def sections(document: Path) -> list[str]:
    """The `##` headings, without their numbers: `## 2. Delivery guarantees` is the latter."""
    return [re.sub(r"^\d+\.\s+", "", heading) for heading in headings(document, level=2)]


def test_the_scan_sees_the_documents_the_code_and_what_ships_beside_the_folder() -> None:
    names = {label(path) for path in deliverables() + shipped_beside()}

    assert {"task2/README.md", "task2/CONTEXT.md", "task2/docs/design.md"} <= names
    assert {"task2/Makefile", "task2/compose.yaml", "task2/openapi/fulfilment.yaml"} <= names
    assert "task2/consumer/src/order_consumer/application/processing.py" in names
    assert {"README.md", ".github/workflows/task2-ci.yml"} <= names
    assert not any(name.startswith(("task2/build/", "task2/.venv/")) for name in names)


@pytest.mark.parametrize("path", deliverables() + shipped_beside(), ids=label)
def test_no_deliverable_points_outside_the_folders_own_material(path: Path) -> None:
    text = path.read_text(errors="replace")

    for what, pattern in FORBIDDEN.items():
        match = pattern.search(text)
        assert match is None, f"links to {what}: {match.group(0) if match else ''}"


@pytest.mark.parametrize("document", markdown_documents(), ids=label)
def test_every_relative_link_resolves(document: Path) -> None:
    broken = [
        problem for target in relative_links(document) if (problem := broken_link(document, target))
    ]

    assert broken == []


def test_the_design_document_opens_with_the_assignments_four_headings() -> None:
    assert sections(DESIGN)[:4] == [
        "Architecture",
        "Delivery guarantees",
        "Retry strategy",
        "Key design decisions",
    ]


def test_the_design_document_links_every_adr() -> None:
    linked = {Path(target.partition("#")[0]).name for target in relative_links(DESIGN)}
    adrs = {path.name for path in (TASK2 / "docs" / "adr").glob("[0-9][0-9][0-9][0-9]-*.md")}

    assert len(adrs) >= 10
    assert adrs <= linked


@pytest.mark.parametrize("document", [README, DESIGN], ids=label)
def test_every_make_target_the_document_names_exists(document: Path) -> None:
    makefile = (TASK2 / "Makefile").read_text()
    targets = set(re.findall(r"^([a-z][a-z-]*):", makefile, flags=re.MULTILINE))
    named = set(re.findall(r"\bmake (?:-s )?([a-z][a-z-]*)", document.read_text()))

    assert named, f"{document.name} names no make target"
    assert named - targets == set()


@pytest.mark.parametrize(
    ("heading", "expected"),
    [
        ("Quick start", "quick-start"),
        ("2. Delivery guarantees", "2-delivery-guarantees"),
        ("Publishing and replaying", "publishing-and-replaying"),
        ("`make demo`: the scenario", "make-demo-the-scenario"),
        (
            "Known limitations and deliberate omissions",
            "known-limitations-and-deliberate-omissions",
        ),
        ("Order event consumer: design", "order-event-consumer-design"),
    ],
)
def test_anchors_follow_githubs_rules(heading: str, expected: str) -> None:
    assert slug(heading) == expected


def test_a_broken_link_and_a_missing_heading_are_reported(tmp_path: Path) -> None:
    (tmp_path / "other.md").write_text("# Present\n## Present\n")
    document = tmp_path / "doc.md"
    document.write_text(
        "[ok](other.md) [ok](other.md#present) [ok](other.md#present-1)\n"
        "[web](https://example.com/x)\n"
        "[gone](missing.md) [heading](other.md#absent)\n"
        "```\n[in code](ignored.md)\n```\n"
    )

    problems = [broken_link(document, target) for target in relative_links(document)]

    assert [problem is None for problem in problems] == [True, True, True, False, False]
