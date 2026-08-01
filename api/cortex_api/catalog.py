"""Persona and skill catalogs for cortex-api.

Personas ship as markdown templates inside the package (`templates/personas/`)
or in an operator-supplied directory. Skills are optional in P1: they live in
a separate git-managed repo and are only scanned when one is configured.
"""

from __future__ import annotations

import logging
import re
import subprocess
from dataclasses import dataclass
from pathlib import Path

import yaml

logger = logging.getLogger(__name__)

NAME_RE = re.compile(r"^[a-z][a-z0-9-]{1,29}$")

_PACKAGE_PERSONAS_DIR = Path(__file__).parent / "templates" / "personas"

_DESCRIPTION_HEADER_RE = re.compile(r"^<!--\s*description:\s*(?P<description>.*?)\s*-->\s*$")


@dataclass
class PersonaInfo:
    id: str
    description: str
    body: str  # carries a `{name}` placeholder for the agent's chosen name


@dataclass
class SkillInfo:
    id: str
    description: str
    path: Path
    sha: str


def _parse_persona_file(path: Path) -> PersonaInfo | None:
    """Parse a persona template: first line is `<!-- description: ... -->`, rest is body."""
    try:
        lines = path.read_text().splitlines()
    except OSError as exc:
        logger.warning("skipping persona %s: could not read file (%s)", path, exc)
        return None

    if not lines:
        logger.warning("skipping persona %s: empty file", path)
        return None

    match = _DESCRIPTION_HEADER_RE.match(lines[0])
    if not match:
        logger.warning("skipping persona %s: missing '<!-- description: ... -->' header", path)
        return None

    body = "\n".join(lines[1:]).strip("\n")
    return PersonaInfo(id=path.stem, description=match.group("description"), body=body)


def list_personas(personas_dir: Path | None) -> list[PersonaInfo]:
    """List personas from `personas_dir`, or the package's built-in templates when None."""
    directory = personas_dir if personas_dir is not None else _PACKAGE_PERSONAS_DIR
    if not directory.is_dir():
        return []

    personas = []
    for path in sorted(directory.glob("*.md")):
        info = _parse_persona_file(path)
        if info is not None:
            personas.append(info)
    return personas


def _parse_skill_frontmatter(text: str) -> tuple[str, str] | None:
    """Split `---`-fenced YAML frontmatter from a SKILL.md body; return (name, description)."""
    if not text.startswith("---"):
        return None

    parts = text.split("---", 2)
    if len(parts) < 3:
        return None

    _, frontmatter_text, body = parts
    try:
        frontmatter = yaml.safe_load(frontmatter_text)
    except yaml.YAMLError:
        return None

    if not isinstance(frontmatter, dict):
        return None

    name = frontmatter.get("name")
    if not name or not isinstance(name, str):
        return None

    description = frontmatter.get("description")
    if not description:
        description = next((line.strip() for line in body.splitlines() if line.strip()), "")

    return name, str(description)


def _resolve_head_sha(skills_repo: Path) -> str | None:
    try:
        result = subprocess.run(
            ["git", "-C", str(skills_repo), "rev-parse", "HEAD"],
            capture_output=True,
            text=True,
            check=True,
        )
    except (subprocess.CalledProcessError, OSError) as exc:
        logger.warning("could not resolve HEAD sha for skills repo %s: %s", skills_repo, exc)
        return None
    return result.stdout.strip()


def list_skills(skills_repo: Path | None) -> list[SkillInfo]:
    """Scan `<skills_repo>/skills/*/SKILL.md` for skill frontmatter.

    Returns [] when `skills_repo` is None (skills are optional in P1), when there's no
    `skills/` directory, or when the repo's HEAD sha can't be resolved. Malformed SKILL.md
    files are skipped with a log warning rather than raising.
    """
    if skills_repo is None:
        return []

    skills_dir = skills_repo / "skills"
    if not skills_dir.is_dir():
        return []

    sha = _resolve_head_sha(skills_repo)
    if sha is None:
        return []

    skills = []
    for skill_md in sorted(skills_dir.glob("*/SKILL.md")):
        try:
            text = skill_md.read_text()
        except OSError as exc:
            logger.warning("skipping skill file %s: could not read file (%s)", skill_md, exc)
            continue

        parsed = _parse_skill_frontmatter(text)
        if parsed is None:
            logger.warning("skipping malformed skill file %s", skill_md)
            continue

        name, description = parsed
        skills.append(SkillInfo(id=name, description=description, path=skill_md, sha=sha))

    return skills
