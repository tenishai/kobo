"""The packaged user guide: its files follow the two loader rules, the house
copy rules hold, and the two read surfaces (HTTP, MCP) serve the same text."""

from __future__ import annotations

import re

import pytest
from fastapi.testclient import TestClient

from kobo_api.guide import _PACKAGE_GUIDE_DIR, GuideError, find_section, load_guide
from kobo_api.main import create_app

EXPECTED_ORDER = [
    "quick-start",
    "before-you-start",
    "create-your-first-agent",
    "the-agent-page",
    "talk-to-it-and-teach-it",
    "secrets-and-variables",
    "slack",
    "access-and-groups",
    "the-harness",
    "agents-talking-to-agents",
    "personas",
    "from-your-own-assistant",
    "glossary",
]


def test_packaged_guide_reads_in_order_with_titles_and_summaries():
    sections = load_guide()
    assert [s.id for s in sections] == EXPECTED_ORDER
    for section in sections:
        assert section.title and section.title[0].isupper()
        assert section.summary and len(section.summary) < 200
        assert section.body.strip()
        assert not section.body.startswith("#"), section.id


def test_guide_copy_follows_the_house_rules():
    """No em dashes, and the words the room's own pages avoid (cluster,
    kubernetes, pod, container, kubectl) stay out of a user's guide except
    where 'cluster' is said once to say the user never touches it."""
    forbidden = re.compile(r"\b(kubernetes|kubectl|pods?|yaml|namespace)\b", re.IGNORECASE)
    for path in sorted(_PACKAGE_GUIDE_DIR.glob("*.md")):
        text = path.read_text(encoding="utf-8")
        assert "—" not in text, f"em dash in {path.name}"
        hits = [m.group(0) for m in forbidden.finditer(text)]
        # The Secrets section says "pods go down and come back" on purpose:
        # that is what a restart is, and hiding the word would mislead.
        allowed = {"secrets-and-variables": {"pods"}}
        stem = path.stem[3:]
        assert {h.lower() for h in hits} <= allowed.get(stem, set()), (path.name, hits)


def test_every_section_the_guide_cross_references_exists():
    """Sections refer to each other by quoted title; each must resolve."""
    titles = {s.title for s in load_guide()}
    body = "\n".join(s.body for s in load_guide())
    for quoted in re.findall(r'See "([^"]+)"', body):
        assert quoted in titles, quoted


def test_find_section_and_misses():
    assert find_section("slack").title == "Slack"
    assert find_section("nope") is None


def test_loader_refuses_a_file_that_breaks_the_rules(tmp_path):
    (tmp_path / "01-good.md").write_text("# Good\n\nBody.\n", encoding="utf-8")
    (tmp_path / "02-bad.md").write_text("No heading first\n", encoding="utf-8")
    with pytest.raises(GuideError, match="02-bad.md"):
        load_guide(tmp_path)


def test_loader_summary_falls_back_to_the_first_paragraph(tmp_path):
    (tmp_path / "01-plain.md").write_text("# Plain\n\nFirst line.\n\nMore.\n", encoding="utf-8")
    (section,) = load_guide(tmp_path)
    assert section.summary == "First line."
    assert section.body == "First line.\n\nMore.\n"


def test_guide_routes_serve_the_packaged_text(session_factory, config):
    with TestClient(create_app(session_factory, config=config, clients=None)) as client:
        index = client.get("/api/guide")
        assert index.status_code == 200
        sections = index.json()["sections"]
        assert [s["id"] for s in sections] == EXPECTED_ORDER
        assert all({"id", "title", "summary", "body"} <= set(s) for s in sections)

        one = client.get("/api/guide/slack")
        assert one.status_code == 200
        assert one.json()["body"] == find_section("slack").body

        assert client.get("/api/guide/nope").status_code == 404
