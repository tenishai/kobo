"""The marketplace door: list/fetch the tools tree, publish skills and
tools by direct commit. All through FakeGitHub -- seed the fake's clone
tree the same way test_tooling.py's import tests seed a source repo
(read that file's fixtures first and reuse its idiom)."""

import pytest

from kobo_api.config import Config
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

GATED_GOOD_SKILL = "---\ntags: [networking]\n---\n# PTR primer\n\nA PTR record maps an IP back to a hostname.\n"

EXISTING_SKILL_MD = (
    "---\nname: existing-skill\ndescription: Already there.\nowner: sre\n"
    "---\nAlready published.\n"
)

# An unquoted colon inside a plain scalar value ("uh oh: this breaks yaml")
# is a classic YAML gotcha: the parser reads the SECOND colon as introducing
# a nested mapping value where none is legal, so `yaml.safe_load` raises
# ScannerError ("mapping values are not allowed here") rather than parsing
# it as a string. One team's malformed frontmatter, authored by hand, is
# exactly the shape review issue #1 guards against.
BROKEN_TOOL_MD = "---\nname: broken-tool\ndescription: uh oh: this breaks yaml\nowner: sre\n---\n"


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


def test_fetch_tool_py_invalid_utf8_raises_marketplace_error(config):
    # MUST-FIX 1b (final review): this module's own contract is "every
    # caller-facing error out of here is a MarketplaceError" (module
    # docstring) -- a decode failure on the `.py` read must be wrapped,
    # never escape as a bare UnicodeDecodeError. Same `tooling_source_files`
    # raw-bytes seam as the list_tools test above.
    config.skills_repo = "org-skills"
    repo = marketplace_repo(config)
    bad_bytes = b'"""caf\xe9 bug."""\ndef post_draft(d):\n    return d\n'
    github = FakeGitHub(
        tooling_source_files={repo: {"tools/bad-tool/bad-tool.py": bad_bytes}}
    )
    with pytest.raises(MarketplaceError, match="not valid UTF-8"):
        fetch_tool_py(github, config, "bad-tool")


def test_list_tools_skips_malformed_tool_md(config):
    # The marketplace repo is org-shared: one team's unquoted colon in its
    # own TOOL.md must not take down the whole catalog for everyone else.
    config.skills_repo = "org-skills"
    github = FakeGitHub(
        clone_tree={
            "tools/baseline-leakguard/TOOL.md": TOOL_MD,
            "tools/baseline-leakguard/baseline-leakguard.py": TOOL_PY,
            "tools/broken-tool/TOOL.md": BROKEN_TOOL_MD,
        }
    )
    tools = list_tools(github, config)
    assert [t["id"] for t in tools] == ["baseline-leakguard"]


def test_list_tools_skips_tool_md_that_is_not_valid_utf8(config):
    # Same "one team's own malformed file must never take the whole
    # catalog down" tolerance as the YAML-parse-failure test just above,
    # but for a stray latin-1 byte that fails to even DECODE as UTF-8
    # (final review, MUST-FIX 1a). `clone_tree` only ever writes plain
    # `str` (encoded utf-8, always valid), so it cannot seed this shape --
    # `tooling_source_files` is FakeGitHub's own seam for exactly this
    # ("the latter is how a test seeds a candidate file that ISN'T valid
    # UTF-8", that fixture's own docstring): it accepts raw `bytes` and
    # applies independently of `clone_tree`, so a plain (non-`clone_tree`)
    # FakeGitHub with the marketplace repo named in this dict still
    # materializes `tools/` with exactly these two files on top of the
    # generic scaffolding, which `list_tools` never looks past `tools/`
    # anyway.
    config.skills_repo = "org-skills"
    repo = marketplace_repo(config)
    bad_bytes = b"---\nname: bad-tool\ndescription: caf\xe9 bug\nowner: sre\n---\n"
    github = FakeGitHub(
        tooling_source_files={
            repo: {
                "tools/baseline-leakguard/TOOL.md": TOOL_MD,
                "tools/bad-tool/TOOL.md": bad_bytes,
            }
        }
    )
    tools = list_tools(github, config)
    assert [t["id"] for t in tools] == ["baseline-leakguard"]


def test_create_skill_injects_owner_frontmatter_and_commits(config, marketplace_github):
    sha = create_skill(
        marketplace_github, config,
        skill_id="ptr-primer", content=GATED_GOOD_SKILL,
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
            skill_id="existing-skill", content=GATED_GOOD_SKILL,
            team="infra-engineering", author="user@example.com",
        )


def test_create_skill_collision_when_path_is_a_file(config):
    # skills/<id> exists but as a plain FILE, not a directory -- the
    # collision check must still raise the typed MarketplaceError rather
    # than let a bare FileExistsError escape from mkdir().
    config.skills_repo = "org-skills"
    github = FakeGitHub(clone_tree={"skills/blocked": "oops, not a directory\n"})
    with pytest.raises(MarketplaceError, match="already exists"):
        create_skill(
            github, config,
            skill_id="blocked", content=GATED_GOOD_SKILL,
            team="infra-engineering", author="user@example.com",
        )


def test_create_tool_collision_raises(config, marketplace_github):
    # marketplace_github's tree already carries tools/baseline-leakguard/*
    with pytest.raises(MarketplaceError, match="already exists"):
        create_tool(
            marketplace_github, config,
            tool_id="baseline-leakguard", content=TOOL_PY,
            team="infra-engineering", description="d", tags=[],
            author="user@example.com",
        )
    assert marketplace_github.push_count == 0


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


def _gate_config(**kwargs) -> Config:
    return Config(github_org="org", skills_repo="skills", **kwargs)


def test_create_skill_completes_version_and_profiles_2026_08_23_brick_regression():
    """The exact 2026-08-23 shape: caller content with no version and no
    profiles used to publish as-is and brick the whole publish pipeline.
    Completion now makes it lawful from birth."""
    github = FakeGitHub()
    create_skill(github, _gate_config(), skill_id="ptr-primer",
                 content=GATED_GOOD_SKILL, team="sre", author="a@b.c")
    pushed = github.pushed_file("skills/ptr-primer/SKILL.md")
    assert "version: 1.0.0" in pushed
    assert "profiles:" in pushed and "agent-sdk" in pushed


def test_create_skill_refuses_malformed_version():
    github = FakeGitHub()
    bad = "---\nversion: 2.oops\n---\nBody.\n"
    with pytest.raises(MarketplaceError, match="version"):
        create_skill(github, _gate_config(), skill_id="v-bad",
                     content=bad, team="sre", author="a@b.c")


def test_create_skill_refuses_float_version_yaml_gotcha():
    """`version: 1.0` parses as a YAML float, not a string. Refused with
    the field named rather than published unlawfully."""
    github = FakeGitHub()
    bad = "---\nversion: 1.0\n---\nBody.\n"
    with pytest.raises(MarketplaceError, match="version"):
        create_skill(github, _gate_config(), skill_id="v-float",
                     content=bad, team="sre", author="a@b.c")


def test_create_skill_refuses_missing_tags():
    github = FakeGitHub()
    with pytest.raises(MarketplaceError, match="tags"):
        create_skill(github, _gate_config(), skill_id="no-tags",
                     content=GOOD_SKILL, team="sre", author="a@b.c")


def test_create_skill_vocab_unknown_profile_refused_when_configured():
    github = FakeGitHub()
    cfg = _gate_config(skills_frontmatter={"allowed_profiles": ["agent-sdk"]})
    content = "---\ntags: [networking]\nprofiles: [desktop]\n---\nBody.\n"
    with pytest.raises(MarketplaceError, match="profiles"):
        create_skill(github, cfg, skill_id="vocab-bad",
                     content=content, team="sre", author="a@b.c")


def test_create_skill_vocab_unconfigured_passes_any_profile():
    github = FakeGitHub()
    content = "---\ntags: [networking]\nprofiles: [desktop]\n---\nBody.\n"
    create_skill(github, _gate_config(), skill_id="vocab-open",
                 content=content, team="sre", author="a@b.c")
    assert github.pushed_file("skills/vocab-open/SKILL.md")
