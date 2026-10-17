"""The deliverable documents are self-contained, and their links resolve.

A reviewer gets this folder and nothing else: no issue tracker, no working branches, no files
for coding assistants. The repository's README and its workflows ship beside the folder, so they
are scanned for such links too. Relative links in the Markdown documents must point at files
that exist (and at headings that exist, where they name one). The design document summarises
every ADR.
"""

import os
import re
from pathlib import Path

import pytest

from orders_api_infra.paths import PROJECT_DIR

THIS_FILE = Path(__file__).resolve()
REPO_DIR = PROJECT_DIR.parent
SKIPPED_DIRS = {"build", "node_modules", "cdk.out", "__pycache__"}
"""Plus every directory whose name starts with a dot: `.venv` and the tools' caches."""
DELIVERABLE_SUFFIXES = {".md", ".py", ".http", ".yaml", ".yml", ".toml", ".sh", ".json"}
DELIVERABLE_NAMES = {"Makefile", ".gitignore"}
GENERATED = {"package-lock.json", "http-client.private.env.json"}

FORBIDDEN = {
    "a GitHub issue or pull request": re.compile(r"github\.com/[^/\s]+/[^/\s]+/(issues|pull)\b"),
    "a research branch": re.compile(r"\bresearch/"),
    "an agent file": re.compile(r"CLAUDE\.md|docs/agents|\.claude/"),
}

MARKDOWN_LINK = re.compile(r"\[[^\]]*\]\(\s*<?([^)\s>]+)>?(?:\s+\"[^\"]*\")?\s*\)")
FENCED_CODE = re.compile(r"^(```|~~~).*?^\1", re.MULTILINE | re.DOTALL)
INLINE_CODE = re.compile(r"`[^`\n]*`")
HEADING = re.compile(r"^#{1,6}\s+(.+?)\s*#*\s*$", re.MULTILINE)


def deliverables() -> list[Path]:
    found = []
    for path in PROJECT_DIR.rglob("*"):
        directories = path.relative_to(PROJECT_DIR).parts[:-1]
        if any(part in SKIPPED_DIRS or part.startswith(".") for part in directories):
            continue
        if not path.is_file() or path.resolve() == THIS_FILE:
            continue
        if path.name in GENERATED:
            continue
        if path.suffix in DELIVERABLE_SUFFIXES or path.name in DELIVERABLE_NAMES:
            found.append(path)
    return sorted(found)


def shipped_beside() -> list[Path]:
    """The repository's README and everything under `.github/`, where they exist."""
    readme = REPO_DIR / "README.md"
    github = REPO_DIR / ".github"
    files = sorted(path for path in github.rglob("*") if path.is_file()) if github.is_dir() else []
    return ([readme] if readme.is_file() else []) + files


def scanned_for_links() -> list[Path]:
    return deliverables() + shipped_beside()


def label(path: Path) -> str:
    return Path(os.path.relpath(path, PROJECT_DIR)).as_posix()


def markdown_documents() -> list[Path]:
    return [path for path in deliverables() if path.suffix == ".md"]


def slug(heading: str) -> str:
    """GitHub's anchor for a heading: lower case, punctuation dropped, spaces to hyphens."""
    text = re.sub(r"[`*_]|\[([^\]]*)\]\([^)]*\)", r"\1", heading).strip().lower()
    return re.sub(r"[^\w\- ]", "", text).replace(" ", "-")


def anchors(document: Path) -> set[str]:
    text = FENCED_CODE.sub("", document.read_text())
    seen: dict[str, int] = {}
    result = set()
    for heading in HEADING.findall(text):
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


def test_the_link_scan_also_sees_what_ships_beside_the_folder() -> None:
    """The repository's README and its workflows ship with the folder, where they exist."""
    beside = [REPO_DIR / "README.md", REPO_DIR / ".github" / "workflows" / "ci.yml"]

    assert {path for path in beside if path.is_file()} <= set(scanned_for_links())


def test_the_scan_sees_the_documents_and_the_code() -> None:
    names = {path.relative_to(PROJECT_DIR).as_posix() for path in deliverables()}

    assert {"README.md", "CONTEXT.md", "docs/design.md", "docs/verification.md"} <= names
    assert {"http/orders.http", "Makefile", "src/orders_api/domain/permissions.py"} <= names
    assert not any(name.startswith(("build/", "infra/node_modules/")) for name in names)


@pytest.mark.parametrize("path", scanned_for_links(), ids=label)
def test_no_deliverable_points_outside_the_folders_own_material(path: Path) -> None:
    text = path.read_text(errors="replace")

    for what, pattern in FORBIDDEN.items():
        match = pattern.search(text)
        assert match is None, f"links to {what}: {match.group(0) if match else ''}"


@pytest.mark.parametrize(
    "document", markdown_documents(), ids=lambda path: path.relative_to(PROJECT_DIR).as_posix()
)
def test_every_relative_link_resolves(document: Path) -> None:
    broken = [
        problem for target in relative_links(document) if (problem := broken_link(document, target))
    ]

    assert broken == []


def test_the_design_document_links_every_adr() -> None:
    design = PROJECT_DIR / "docs" / "design.md"
    linked = {Path(target.partition("#")[0]).name for target in relative_links(design)}
    adrs = {path.name for path in (PROJECT_DIR / "docs" / "adr").glob("[0-9][0-9][0-9][0-9]-*.md")}

    assert len(adrs) >= 13
    assert adrs <= linked


@pytest.mark.parametrize(
    ("heading", "expected"),
    [
        ("Quick start", "quick-start"),
        ("1. Overview", "1-overview"),
        ("Running against AWS (optional)", "running-against-aws-optional"),
        ("`make demo`: the scenario", "make-demo-the-scenario"),
        ("Users and roles", "users-and-roles"),
    ],
)
def test_anchors_follow_githubs_rules(heading: str, expected: str) -> None:
    assert slug(heading) == expected


def test_a_broken_link_and_a_missing_heading_are_reported(tmp_path: Path) -> None:
    (tmp_path / "other.md").write_text("# Present\n")
    document = tmp_path / "doc.md"
    document.write_text(
        "[ok](other.md) [ok](other.md#present) [web](https://example.com/x)\n"
        "[gone](missing.md) [heading](other.md#absent)\n"
        "```\n[in code](ignored.md)\n```\n"
    )

    problems = [broken_link(document, target) for target in relative_links(document)]

    assert [problem is None for problem in problems] == [True, True, False, False]
