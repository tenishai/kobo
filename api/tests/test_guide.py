"""The packaged user guide: its files follow the two loader rules, the house
copy rules hold, and the two read surfaces (HTTP, MCP) serve the same text."""

from __future__ import annotations

import os
import re
from pathlib import Path

import pytest
import yaml
from fastapi.testclient import TestClient

from kobo_api.composer import BundleSpec, compose
from kobo_api.guide import _PACKAGE_GUIDE_DIR, GuideError, find_section, load_guide
from kobo_api.main import create_app
from kobo_api.namegen import _FALLBACK_NAMES
from kobo_api.steps import StagedSkill

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
    "what-kobo-is",
    "running-your-own-code",
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


def test_loader_refuses_a_file_that_breaks_the_rules(tmp_path):
    (tmp_path / "01-good.md").write_text("# Good\n\nBody.\n", encoding="utf-8")
    (tmp_path / "02-bad.md").write_text("No heading first\n", encoding="utf-8")
    with pytest.raises(GuideError, match="02-bad.md"):
        load_guide(tmp_path)


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

# --- from test_composer.py ---


_RUNTIME_IMAGE = "registry.example/agents/daedalus-runtime:latest"


def _make_spec(
    skills: list[StagedSkill] | None = None,
    *,
    name: str = "sam",
    team: str = "shared",
    group: str | None = None,
    purpose_tags: list[str] | None = None,
    capability: str | None = None,
    colleagues: list[dict] | None = None,
    cabal_url: str | None = None,
    memory_replica_bucket: str = "",
    liminal_url: str = "",
    archetype: str | None = None,
) -> BundleSpec:
    return BundleSpec(
        name=name,
        persona_body=f"You are {name}, a helpful assistant.\n",
        model_id="vertex_ai/claude-sonnet-4-5",
        skills=skills or [],
        runtime_image=_RUNTIME_IMAGE,
        vertex_project="example-model-project",
        vertex_location="us-east5",
        team=team,
        group=group,
        purpose_tags=purpose_tags if purpose_tags is not None else [],
        capability=capability,
        colleagues=colleagues if colleagues is not None else [],
        cabal_url=cabal_url,
        memory_replica_bucket=memory_replica_bucket,
        liminal_url=liminal_url,
        archetype=archetype,
    )


def test_compose_colleagues_md_instruction_copy_has_no_em_dashes(tmp_path):
    """House rule: Compose colleagues md instruction copy has no em dashes."""
    dest = tmp_path / "out"
    spec = _make_spec(colleagues=[
        {"name": "ward", "team": "infra-engineering", "purpose_tags": [], "capability": "x"},
    ])

    compose(spec, dest)

    text = (dest / "bundle" / "knowledge" / "colleagues.md").read_text()
    assert "—" not in text


# --- from test_persona_templates.py ---


TEMPLATES_DIR = Path(__file__).resolve().parents[1] / "kobo_api" / "templates" / "personas"


# Product and vendor names a built-in persona must never carry. Names the
# room's operator does not want in templates either (their own company,
# their clients) come from PERSONA_BLOCKLIST_EXTRA, comma-separated, so this
# public file never has to name them.
BLOCKLIST = (
    *(w.strip().lower() for w in os.environ.get("PERSONA_BLOCKLIST_EXTRA", "").split(",") if w.strip()),
    "tenishai",
    "kobo",
    "gojira",
    "daedalus",
    "cabal",
    "liminal",
    "limenal",
    "techne",
    "sero",
    "noto",
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


ALL_FILES = sorted(TEMPLATES_DIR.glob("*.md"))


def _ids(paths):
    return [p.stem for p in paths]


@pytest.mark.parametrize("path", ALL_FILES, ids=_ids(ALL_FILES))
def test_no_company_client_or_product_names(path):
    """House rule: No company client or product names."""
    text = path.read_text().lower()
    hits = [w for w in BLOCKLIST if re.search(rf"\b{re.escape(w)}\b", text)]
    assert hits == []


@pytest.mark.parametrize("path", ALL_FILES, ids=_ids(ALL_FILES))
def test_inside_the_render_ceiling(path):
    """House rule: Inside the render ceiling."""
    text = path.read_text()
    assert len(text.splitlines()) < MAX_LINES
    assert len(text.encode("utf-8")) < MAX_BYTES


# --- from test_namegen.py ---


# The SDD vendor-name deny list itself (repo root, outside the api/
# subproject) — read dynamically rather than spelling out any vendor name
# literally in this file, so the pool-hygiene check below stays meaningful
# without itself becoming a vendor-scan false positive.
_VENDOR_NAMES_PATH = Path(__file__).resolve().parents[2] / ".superpowers" / "sdd" / "vendor-names.txt"


def test_fallback_pool_has_at_least_five_and_no_vendor_names():
    """House rule: Fallback pool has at least five and no vendor names."""
    assert len(_FALLBACK_NAMES) >= 5
    # No vendor/company names anywhere in the curated pool.
    lowered = {name.lower() for name in _FALLBACK_NAMES}
    if _VENDOR_NAMES_PATH.is_file():
        vendor_names = {
            line.strip().lower()
            for line in _VENDOR_NAMES_PATH.read_text().splitlines()
            if line.strip()
        }
        assert not (lowered & vendor_names)


# --- the CI workflows ---


ROOT = Path(__file__).resolve().parents[2]


def _workflows() -> dict[str, dict]:
    return {
        path.name: yaml.safe_load(path.read_text())
        for path in sorted((ROOT / ".github" / "workflows").glob("*.y*ml"))
    }


def test_every_job_runs_on_a_github_hosted_runner():
    """A public repository runs strangers' pull requests: every job names a
    GitHub-hosted image outright, never a variable that could point at our own runner."""
    seen = 0
    for name, doc in _workflows().items():
        if name == "gojira-ci.yaml":
            continue  # generated by the deploy tool, not this repository's CI
        for job, body in (doc.get("jobs") or {}).items():
            if str(body.get("uses", "")).startswith("./.github/workflows/"):
                continue  # runs another workflow of this repository, checked on its own
            runs_on = body.get("runs-on")
            assert isinstance(runs_on, str) and runs_on.startswith("ubuntu-"), (
                f"{name}:{job} runs on {runs_on!r}, not a GitHub-hosted image"
            )
            seen += 1
    assert seen, "no job found: the workflow layout changed, update this test"


def test_the_ci_check_needs_every_other_ci_job():
    """The branch rules require one check, `ci`: it must wait on every job in ci.yml,
    and run even when one of them failed, or a red job could merge."""
    jobs = _workflows()["ci.yml"]["jobs"]
    assert sorted(jobs["ci"]["needs"]) == sorted(set(jobs) - {"ci"})
    assert "always()" in str(jobs["ci"]["if"])

