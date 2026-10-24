"""Name validation and persona-file parsing for kobo-api.

Personas ship as markdown templates inside the package (`templates/
personas/`); `kobo_api.personas` reads them directly via `_parse_persona_
file` for seeding (`seed_default_personas`). The old local-clone catalog
scanners (`list_skills`, over a git-managed skills repo; `list_personas`,
over an operator-supplied directory) are gone as of Task 5 — assembly now
reads both catalogs from their buckets (`kobo_api.search`, `kobo_api.steps`).
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from pathlib import Path

logger = logging.getLogger(__name__)

NAME_RE = re.compile(r"^[a-z][a-z0-9-]{1,23}$")

MAX_NAME_LENGTH = 24

# Names the platform (or kobo itself) already gives special meaning —
# never available for an agent, regardless of how they'd otherwise pass
# NAME_RE. `gojira`/`kobo`/`pnyx`/`cortex` guard against an agent shadowing
# the platform services themselves (`pnyx` and `cortex` are this project's
# former names, kept reserved so an old link or habit can't collide with a
# live agent); the rest are namespaces the cluster runs or names the suite's own
# services may run under (each agent's namespace is its own name, issue
# #193). This list is a cheap first layer: create and connect also refuse any
# name whose namespace already exists (`kobo_api.main._namespace_taken_block`).
RESERVED_NAMES = frozenset(
    {
        "default", "kube-system", "kube-public", "kube-node-lease",
        "gojira", "kobo", "pnyx", "cortex",
        # The suite's own products, by name.
        "cabal", "sero", "liminal", "noto", "techne", "daedalus", "agora",
        "kaminos", "agents",
        # Cluster add-ons the suite runs on.
        "cert-manager", "external-secrets", "ingress-nginx",
    }
)


def validate_name(name: str) -> str | None:
    """Validate a candidate agent name; return None if valid, or a short
    human-readable reason if not — shared by the availability check and
    create validation (`kobo_api.main`) so both report identical reasons.

    Order matters: length is checked before NAME_RE itself so an over-long
    name gets the specific "too long" reason instead of the generic
    "invalid name" NAME_RE would otherwise report (NAME_RE's own max is what
    enforces this length, but failing it doesn't say *why*). Trailing-hyphen
    and reserved-name checks come after NAME_RE since both describe names
    that are otherwise well-formed.
    """
    if len(name) > MAX_NAME_LENGTH:
        return f"too long (max {MAX_NAME_LENGTH})"
    if not NAME_RE.fullmatch(name):
        return "invalid name"
    if name.endswith("-"):
        return "must not end with a hyphen"
    if name in RESERVED_NAMES:
        return "reserved name"
    return None


_PACKAGE_PERSONAS_DIR = Path(__file__).parent / "templates" / "personas"

_DESCRIPTION_HEADER_RE = re.compile(r"^<!--\s*description:\s*(?P<description>.*?)\s*-->\s*$")


@dataclass
class PersonaInfo:
    id: str
    description: str
    body: str  # carries a `{name}` placeholder for the agent's chosen name


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
