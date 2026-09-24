"""The packaged built-in personas meet the house standard.

Every file under `kobo_api/templates/personas/` ships to every team as a
starting point, so each one is held to the same shape `render_persona`
lays down and the same copy rules the room's own text follows: no em or
en dashes, no company, client or product names, and a size well inside
the room's persona ceiling.

`helpful-generalist` predates the house shape and is kept as it is: it is
held to the universal checks (header, placeholder, dashes, names, size)
but not to the section checks.

No shared name blocklist existed in the repo when this was written, so
the one below is a small explicit list: the operating company, the
product family and its codenames, and the vendors a generic persona
should never name.
"""
import re
from pathlib import Path

import pytest

from kobo_api.catalog import _parse_persona_file
from kobo_api.personas import parse_persona_body

TEMPLATES_DIR = Path(__file__).resolve().parents[1] / "kobo_api" / "templates" / "personas"

EXPECTED_IDS = {
    "helpful-generalist",
    "code-reviewer",
    "billing-analyst",
    "onboarding-guide",
    "legal-reader",
    "frontend-designer",
    "aws-operator",
    "gcp-operator",
    "azure-operator",
    "workspace-admin",
    "identity-admin",
}

# Kept in the pre-house shape on purpose (see module docstring).
LEGACY_IDS = {"helpful-generalist"}

VOICE_PARAGRAPH = (
    "Speak in the first person, plainly. Lead with the answer; skip\n"
    "throat-clearing. Say \"I don't know yet\" when it's true."
)

BLOCKLIST = (
    "acme",
    "tenishai",
    "kobo",
    "gojira",
    "daedalus",
    "cabal",
    "liminal",
    "limenal",
    "techne",
    "ladon",
    "argus",
    "kodama",
    "kaminos",
    "okta",
    "google",
    "microsoft",
    "amazon",
    "anthropic",
    "claude",
)

MAX_LINES = 200
MAX_BYTES = 49152
DESCRIPTION_RE = re.compile(r"^<!--\s*description:\s*(?P<d>.*?)\s*-->\s*$")

ALL_FILES = sorted(TEMPLATES_DIR.glob("*.md"))
HOUSE_FILES = [p for p in ALL_FILES if p.stem not in LEGACY_IDS]


def _ids(paths):
    return [p.stem for p in paths]


def test_the_id_set_is_the_baseline_eleven():
    assert {p.stem for p in ALL_FILES} == EXPECTED_IDS


@pytest.mark.parametrize("path", ALL_FILES, ids=_ids(ALL_FILES))
def test_description_header(path):
    first = path.read_text().splitlines()[0]
    match = DESCRIPTION_RE.match(first)
    assert match, first
    description = match.group("d")
    assert description
    assert len(description) < 140, len(description)
    assert "—" not in description
    info = _parse_persona_file(path)
    assert info is not None and info.id == path.stem


@pytest.mark.parametrize("path", ALL_FILES, ids=_ids(ALL_FILES))
def test_name_placeholder_appears_exactly_once(path):
    assert path.read_text().count("{name}") == 1


@pytest.mark.parametrize("path", ALL_FILES, ids=_ids(ALL_FILES))
def test_no_em_or_en_dashes(path):
    text = path.read_text()
    assert "—" not in text
    assert "–" not in text


@pytest.mark.parametrize("path", ALL_FILES, ids=_ids(ALL_FILES))
def test_no_company_client_or_product_names(path):
    text = path.read_text().lower()
    hits = [w for w in BLOCKLIST if re.search(rf"\b{re.escape(w)}\b", text)]
    assert hits == []


@pytest.mark.parametrize("path", ALL_FILES, ids=_ids(ALL_FILES))
def test_inside_the_render_ceiling(path):
    text = path.read_text()
    assert len(text.splitlines()) < MAX_LINES
    assert len(text.encode("utf-8")) < MAX_BYTES


@pytest.mark.parametrize("path", HOUSE_FILES, ids=_ids(HOUSE_FILES))
def test_house_shape(path):
    text = path.read_text()
    lines = text.splitlines()
    assert 40 <= len(lines) <= 90, len(lines)
    assert lines[1].startswith("You are {name}, ")
    assert VOICE_PARAGRAPH in text
    assert "## What you do" in lines
    assert "## Rules" in lines
    assert lines.index("## Rules") > lines.index("## What you do")


@pytest.mark.parametrize("path", HOUSE_FILES, ids=_ids(HOUSE_FILES))
def test_rule_count(path):
    lines = path.read_text().splitlines()
    rules = [line for line in lines[lines.index("## Rules") + 1 :] if line.startswith("- ")]
    assert 8 <= len(rules) <= 14, len(rules)


@pytest.mark.parametrize("path", HOUSE_FILES, ids=_ids(HOUSE_FILES))
def test_parses_back_into_the_builder_fields(path):
    parsed = parse_persona_body(path.read_text())
    assert parsed.who_they_are is not None
    assert parsed.what_they_do is not None
    assert parsed.what_they_do.strip()
    assert 8 <= len(parsed.rules) <= 14
