"""The room's user guide, one markdown file per section.

The guide is packaged with the API (`templates/guide/NN-<id>.md`) so the two
places people read it agree byte for byte: the Getting started page in the
room, and the `kobo_guide` MCP tool a person's own assistant reads it through.
There is no third copy anywhere.

A section file is plain markdown with two rules the loader depends on:

* the first line is the title, as an `# ` heading;
* an optional `<!-- summary: ... -->` comment right under it is the one-line
  summary the section list shows (and the MCP listing returns). Without it the
  summary is the first paragraph.

Files sort by name, so the `NN-` prefix is the reading order and the rest of
the stem is the section id (`01-before-you-start.md` -> `before-you-start`).
Loaded once per process: the files are part of the image and cannot change
underneath a running room.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

_PACKAGE_GUIDE_DIR = Path(__file__).parent / "templates" / "guide"

_FILENAME_RE = re.compile(r"^(?P<order>\d{2})-(?P<id>[a-z][a-z0-9-]*)\.md$")
_SUMMARY_RE = re.compile(r"^<!--\s*summary:\s*(?P<summary>.+?)\s*-->$")


@dataclass(frozen=True)
class GuideSection:
    id: str
    title: str
    summary: str
    body: str

    def listing(self) -> dict:
        return {"id": self.id, "title": self.title, "summary": self.summary}

    def full(self) -> dict:
        return {**self.listing(), "body": self.body}


class GuideError(Exception):
    """A packaged section file does not follow the two rules above."""


def _parse_section(path: Path) -> GuideSection:
    match = _FILENAME_RE.fullmatch(path.name)
    if match is None:
        raise GuideError(f"guide file {path.name!r} is not NN-<id>.md")
    lines = path.read_text(encoding="utf-8").splitlines()
    if not lines or not lines[0].startswith("# "):
        raise GuideError(f"guide file {path.name!r} must open with a '# Title' line")
    title = lines[0][2:].strip()
    rest = lines[1:]
    summary = ""
    # Skip blank lines, then take the summary comment if it is there.
    i = 0
    while i < len(rest) and not rest[i].strip():
        i += 1
    if i < len(rest):
        found = _SUMMARY_RE.match(rest[i].strip())
        if found:
            summary = found.group("summary")
            i += 1
    body = "\n".join(rest[i:]).strip() + "\n"
    if not summary:
        for line in body.splitlines():
            if line.strip() and not line.startswith("#"):
                summary = line.strip()
                break
    if not title or not body.strip():
        raise GuideError(f"guide file {path.name!r} has an empty title or body")
    return GuideSection(id=match.group("id"), title=title, summary=summary, body=body)


@lru_cache(maxsize=1)
def load_guide(directory: Path | None = None) -> tuple[GuideSection, ...]:
    """Every section in reading order. Raises `GuideError` on a malformed
    file, so a broken guide fails the test suite rather than a reader."""
    folder = directory or _PACKAGE_GUIDE_DIR
    sections = tuple(_parse_section(p) for p in sorted(folder.glob("*.md")))
    ids = [s.id for s in sections]
    if len(ids) != len(set(ids)):
        raise GuideError("two guide files share an id")
    return sections


def find_section(section_id: str, directory: Path | None = None) -> GuideSection | None:
    for section in load_guide(directory):
        if section.id == section_id:
            return section
    return None
