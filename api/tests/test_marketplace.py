"""The marketplace door: list/fetch the tools tree, publish skills and
tools by direct commit. All through FakeGitHub -- seed the fake's clone
tree the same way test_tooling.py's import tests seed a source repo
(read that file's fixtures first and reuse its idiom)."""

import pytest

from kobo_api.marketplace import (
    MarketplaceError,
    create_skill,
    create_tool,
    fetch_tool_py,
    list_tools,
    marketplace_repo,
)
from tests.fakes import FakeGitHub

TOOL_MD = """---
name: baseline-leakguard
description: Redacts credential-shaped strings from every reply.
owner: sre
tags: [security]
---
"""

TOOL_PY = (
    '"""Baseline leakguard."""\n'
    "import re\n"
    "def post_draft(draft):\n"
    "    return re.sub(r'xox[abps]-[A-Za-z0-9-]{10,}', '[redacted]', draft)\n"
)

GOOD_SKILL = "# PTR primer\n\nA PTR record maps an IP back to a hostname.\n"

EXISTING_SKILL_MD = (
    "---\nname: existing-skill\ndescription: Already there.\nowner: sre\n"
    "---\nAlready published.\n"
)


@pytest.fixture
def marketplace_github(config):
    # marketplace_repo(config) needs config.skills_repo set (falsy by
    # default in the bare `config` fixture) -- set it here, on the shared
    # per-test Config instance, the same instance the test itself also
    # receives when it requests `config` alongside this fixture.
    config.skills_repo = "org-skills"
    return FakeGitHub(
        clone_tree={
            "skills/existing-skill/SKILL.md": EXISTING_SKILL_MD,
            "tools/baseline-leakguard/TOOL.md": TOOL_MD,
            "tools/baseline-leakguard/baseline-leakguard.py": TOOL_PY,
        }
    )


def test_marketplace_repo_requires_config(config):
    config.skills_repo = None
    with pytest.raises(MarketplaceError):
        marketplace_repo(config)


def test_list_tools_reads_tool_md_frontmatter(config, marketplace_github):
    tools = list_tools(marketplace_github, config)
    assert [t["id"] for t in tools] == ["baseline-leakguard"]
    assert tools[0]["owner"] == "sre"
    assert tools[0]["description"].startswith("Redacts")


def test_fetch_tool_py_returns_filename_and_content(config, marketplace_github):
    filename, content = fetch_tool_py(marketplace_github, config, "baseline-leakguard")
    assert filename == "baseline-leakguard.py"
    assert "post_draft" in content


def test_fetch_tool_py_unknown_id_raises(config, marketplace_github):
    with pytest.raises(MarketplaceError):
        fetch_tool_py(marketplace_github, config, "nope")


def test_create_skill_injects_owner_frontmatter_and_commits(config, marketplace_github):
    sha = create_skill(
        marketplace_github, config,
        skill_id="ptr-primer", content=GOOD_SKILL,
        team="infra-engineering", author="user@example.com",
    )
    assert sha
    written = marketplace_github.pushed_file("skills/ptr-primer/SKILL.md")
    assert written.startswith("---\n")
    assert "owner: infra-engineering" in written
    assert "PTR record" in written


def test_create_skill_rejects_conflicting_owner_frontmatter(config, marketplace_github):
    content = "---\nname: x\nowner: other-team\n---\nbody\n"
    with pytest.raises(MarketplaceError, match="owner"):
        create_skill(
            marketplace_github, config,
            skill_id="x", content=content,
            team="infra-engineering", author="user@example.com",
        )


def test_create_skill_collision_raises(config, marketplace_github):
    # marketplace_github's tree already carries skills/existing-skill/SKILL.md
    with pytest.raises(MarketplaceError, match="already exists"):
        create_skill(
            marketplace_github, config,
            skill_id="existing-skill", content=GOOD_SKILL,
            team="infra-engineering", author="user@example.com",
        )


def test_create_tool_validates_py_before_any_commit(config, marketplace_github):
    with pytest.raises(MarketplaceError, match="neither pre_task nor post_draft"):
        create_tool(
            marketplace_github, config,
            tool_id="broken", content="x = 1\n",
            team="infra-engineering", description="d", tags=[],
            author="user@example.com",
        )
    assert marketplace_github.push_count == 0  # validate-then-commit ordering


def test_create_tool_writes_tool_md_and_py(config, marketplace_github):
    sha = create_tool(
        marketplace_github, config,
        tool_id="my-guard", content=TOOL_PY,
        team="infra-engineering", description="A guard.", tags=["security"],
        author="user@example.com",
    )
    assert sha
    md = marketplace_github.pushed_file("tools/my-guard/TOOL.md")
    assert "owner: infra-engineering" in md and "A guard." in md
    assert marketplace_github.pushed_file("tools/my-guard/my-guard.py") == TOOL_PY
