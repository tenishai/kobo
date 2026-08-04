"""Tests for pnyx_api.bootstrap: the idempotent `python -m pnyx_api.bootstrap`
CLI that wires a fresh client org — org-config repo, catalog buckets, the
skills repo's publish workflow, default personas, first catalog ingest.
Exercised entirely against fakes (FakeGitHub/FakeStorage) — no real network,
subprocess, or `httpx.post` call ever fires here.
"""

from __future__ import annotations

import re

import httpx
import pytest
import yaml

from pnyx_api import bootstrap
from pnyx_api.bootstrap import (
    StepResult,
    _exit_code,
    render_org_yaml,
    render_publish_workflow,
    run_bootstrap,
)
from pnyx_api.clients import Clients
from pnyx_api.config import Config
from pnyx_api.org import ORG_KEYS
from tests.fakes import FakeGitHub, FakeStorage

GITHUB_ORG = "acme-corp"
SKILLS_BUCKET = "acme-corp-skills"
PERSONAS_BUCKET = "acme-corp-personas"
# `_skills_repo_name`'s own computed default (and org.yaml's stored value)
# is bare — no owner prefix — but every GitHub call bootstrap itself makes
# must be owner-qualified (Finding 1: `gh` resolves a bare repo argument
# under the invoking user's own account, never the org's). SKILLS_REPO_BARE
# is what org.yaml stores/what a bare `_skills_repo_name` returns; SKILLS_REPO
# is what actually reaches every FakeGitHub call in this file.
SKILLS_REPO_BARE = "acme-corp-skills"
SKILLS_REPO = f"{GITHUB_ORG}/{SKILLS_REPO_BARE}"

# A bare `{word}` placeholder — the shape a leftover, un-substituted Python
# `.format()`/f-string token would take. Deliberately does NOT match GitHub
# Actions' `${{ expr }}` (space right after the brace, and doubled braces)
# or our own rendered `models` flow-style YAML (`{id: ..., label: ...}`,
# which has a `:` immediately after the first word, never an immediate `}`).
_BARE_PLACEHOLDER_RE = re.compile(r"\{[A-Za-z_][A-Za-z0-9_]*\}")


def _assert_no_leftover_placeholders(text: str) -> None:
    assert "__PNYX_" not in text
    assert not _BARE_PLACEHOLDER_RE.search(text)


# --- render_org_yaml (golden) --------------------------------------------------


def test_render_org_yaml_golden_with_computed_defaults():
    config = Config(
        github_org=GITHUB_ORG,
        gojira_cluster="acme-cluster",
        runtime_image="registry.example/agents/daedalus-runtime:v1",
        gcp_project="acme-project",
        vertex_project="acme-models",
        vertex_location="us-east5",
        model_group="models@acme-corp.example",
        avatar_model="gemini-2.5-flash-image",
        avatar_location="us-central1",
        skills_publish_wif_provider=(
            "projects/123/locations/global/workloadIdentityPools/acme/providers/github"
        ),
        skills_publish_service_account="skills-publisher@acme-project.iam.gserviceaccount.com",
    )

    rendered = render_org_yaml(config)
    _assert_no_leftover_placeholders(rendered)

    parsed = yaml.safe_load(rendered)
    assert set(parsed) == ORG_KEYS
    assert parsed == {
        "github_org": "acme-corp",
        "skills_repo": "acme-corp-skills",
        "skills_bucket": "acme-corp-skills",
        "personas_bucket": "acme-corp-personas",
        "gojira_cluster": "acme-cluster",
        "runtime_image": "registry.example/agents/daedalus-runtime:v1",
        "gcp_project": "acme-project",
        "vertex_project": "acme-models",
        "vertex_location": "us-east5",
        "model_group": "models@acme-corp.example",
        "avatar_model": "gemini-2.5-flash-image",
        "avatar_location": "us-central1",
        "models": [{"id": "vertex_ai/claude-sonnet-4-5", "label": "Claude Sonnet 4.5"}],
        "skills_publish_wif_provider": (
            "projects/123/locations/global/workloadIdentityPools/acme/providers/github"
        ),
        "skills_publish_service_account": (
            "skills-publisher@acme-project.iam.gserviceaccount.com"
        ),
        "skills_publish_external": False,
    }


def test_render_org_yaml_uses_explicit_names_verbatim_when_set():
    config = Config(
        github_org=GITHUB_ORG,
        skills_repo="acme-corp-curated-skills",
        skills_bucket="acme-corp-curated-skills-bucket",
        personas_bucket="acme-corp-curated-personas-bucket",
        skills_publish_external=True,
    )

    parsed = yaml.safe_load(render_org_yaml(config))
    assert parsed["skills_repo"] == "acme-corp-curated-skills"
    assert parsed["skills_bucket"] == "acme-corp-curated-skills-bucket"
    assert parsed["personas_bucket"] == "acme-corp-curated-personas-bucket"
    assert parsed["skills_publish_external"] is True


# --- render_publish_workflow ----------------------------------------------------


def test_render_publish_workflow_no_leftover_tokens_and_gha_expression_survives():
    config = Config(
        skills_publish_wif_provider="projects/123/providers/github",
        skills_publish_service_account="publisher@acme-project.iam.gserviceaccount.com",
    )
    rendered = render_publish_workflow(config, SKILLS_BUCKET)
    _assert_no_leftover_placeholders(rendered)
    assert f"--bucket {SKILLS_BUCKET}" in rendered
    assert "projects/123/providers/github" in rendered
    assert "publisher@acme-project.iam.gserviceaccount.com" in rendered
    # A real GitHub Actions bare expression, untouched by the literal replace.
    assert "github.event_name == 'pull_request'" in rendered


# --- fixtures -------------------------------------------------------------------


@pytest.fixture
def config(tmp_path):
    """The PRE-SET-buckets path: skills_bucket/personas_bucket are given
    explicitly, so every test using this fixture exercises bootstrap with
    those raw Config fields already non-empty. `fresh_config` below is the
    other path — a TRUE fresh org with neither field set at all — kept as a
    separate fixture so Finding 2's regression (the catalog-ingest fallback
    silently seeing `{}` when the raw fields are still "") stays covered by
    a test that can't accidentally pass because these fields happen to be set.
    """
    return Config(
        github_org=GITHUB_ORG,
        workdir=tmp_path / "work",
        db_url=f"sqlite:///{tmp_path / 'pnyx.db'}",
        skills_bucket=SKILLS_BUCKET,
        personas_bucket=PERSONAS_BUCKET,
    )


@pytest.fixture
def fresh_config(tmp_path):
    """A TRUE fresh org (Finding 2 regression coverage): `skills_bucket`/
    `personas_bucket` are left at their Config default ("" — unset), unlike
    `config` above. Before the fix, `_step_catalog_ingest`'s fallback built
    its bucket mapping from `pnyx_api.main.configured_catalogs`, which reads
    those raw fields directly — both "" here, so the mapping was `{}` and
    the ingest a silent no-op reported as "done". The fix reads the same
    computed `_skills_bucket_name`/`_personas_bucket_name` helpers steps
    2/3/5 already use, which always default to a non-empty
    `f"{github_org}-skills"`/`f"{github_org}-personas"` name.
    """
    return Config(
        github_org=GITHUB_ORG,
        workdir=tmp_path / "work",
        db_url=f"sqlite:///{tmp_path / 'pnyx.db'}",
    )


@pytest.fixture
def github():
    return FakeGitHub(track_pushed_content=True)


@pytest.fixture
def storage():
    return FakeStorage()


def _clients(github, storage) -> Clients:
    return Clients(github=github, gojira=None, cloud=None, agent=None, kubectl=None, storage=storage)


@pytest.fixture(autouse=True)
def no_real_network(monkeypatch):
    """Every run touches step 6, which tries `httpx.post` against
    `config.api_base_url` first — no real pnyx-api server is up during these
    tests ("Tests (all against fakes; no network)" per the brief), so this
    simulates "not reachable yet" by default for every test here. Tests that
    want the primary (refresh-endpoint) path override `bootstrap.httpx.post`
    themselves.
    """
    def _raise(*args, **kwargs):
        raise httpx.ConnectError("connection refused")

    monkeypatch.setattr(bootstrap.httpx, "post", _raise)


# --- fresh-org run: every step done ---------------------------------------------


def test_fresh_org_run_every_step_done(config, github, storage, tmp_path):
    """Pre-set-buckets path (the `config` fixture already has skills_bucket/
    personas_bucket set) — `test_fresh_config_no_buckets_set_...` below is
    the other path, a TRUE fresh org where those fields are still "".
    """
    clients = _clients(github, storage)
    lines: list[str] = []
    results = run_bootstrap(config, clients, out=lines.append)

    assert [r.name for r in results] == [
        bootstrap.STEP_ORG_CONFIG_REPO,
        bootstrap.STEP_SKILLS_BUCKET,
        bootstrap.STEP_PERSONAS_BUCKET,
        bootstrap.STEP_SKILLS_PUBLISH,
        bootstrap.STEP_DEFAULT_PERSONAS,
        bootstrap.STEP_CATALOG_INGEST,
    ]
    assert [r.status for r in results] == ["done"] * 6
    assert _exit_code(results) == 0

    # org-config repo: created (the tmpdir it was composed in is cleaned up
    # by bootstrap itself right after — inspect the pushed content the same
    # way a later boot would, via clone()).
    assert github.repo_exists(f"{GITHUB_ORG}/pnyx-config")
    org_repo, _org_src = github.created_from_dir[0]
    assert org_repo == f"{GITHUB_ORG}/pnyx-config"
    org_config_clone = tmp_path / "inspect-org-config"
    github.clone(org_repo, org_config_clone)
    org_yaml_text = (org_config_clone / "org.yaml").read_text()
    _assert_no_leftover_placeholders(org_yaml_text)
    assert yaml.safe_load(org_yaml_text)["github_org"] == GITHUB_ORG
    assert (org_config_clone / "README.md").exists()

    # both catalog buckets created.
    assert storage.ensure_bucket(SKILLS_BUCKET) == "exists"
    assert storage.ensure_bucket(PERSONAS_BUCKET) == "exists"

    # skills repo scaffolded with the rendered workflow + vendored script, no
    # leftover placeholders anywhere in the rendered workflow (inspected the
    # same clone()-replay way as the org-config repo above). Owner-prefixed
    # (Finding 1) — `create_repo_from_dir` never sees the bare computed name.
    skills_repo, _skills_src = github.created_from_dir[1]
    assert skills_repo == SKILLS_REPO
    assert f"repo_exists:{SKILLS_REPO}" in github.calls
    skills_repo_clone = tmp_path / "inspect-skills-repo"
    github.clone(skills_repo, skills_repo_clone)
    workflow_text = (skills_repo_clone / ".github" / "workflows" / "publish-skills.yml").read_text()
    _assert_no_leftover_placeholders(workflow_text)
    assert (skills_repo_clone / ".github" / "scripts" / "publish_skills.py").exists()
    assert (skills_repo_clone / "README.md").exists()

    # no open_pr call on a fresh org: the repo didn't exist yet, so it was
    # scaffolded outright rather than PR'd into an existing repo.
    assert github.open_pr_calls == []

    # default personas actually seeded into the bucket.
    assert storage.list_keys(PERSONAS_BUCKET, "")

    # catalog ingest fell back to the direct path (no real refresh endpoint)
    # and picked up the just-seeded personas.
    ingest_detail = results[-1].detail
    assert "direct" in ingest_detail
    assert "personas=2" in ingest_detail  # both packaged default personas

    # Output is column-aligned: every line's step name starts at the same index.
    name_starts = {line.index(result.name) for line, result in zip(lines, results, strict=True)}
    assert len(name_starts) == 1


# --- fresh-defaults run: catalog ingest must see the computed bucket names -------


def test_fresh_config_no_buckets_set_ingest_still_reports_seeded_personas(
    fresh_config, github, storage
):
    """Finding 2 regression: on a TRUE fresh org (no skills_bucket/
    personas_bucket configured at all — the `fresh_config` fixture, not the
    pre-set `config` one), the catalog-ingest step must still see the
    personas step 5 just seeded in this same run — via the same computed
    `f"{github_org}-personas"` bucket name step 5 wrote them into — rather
    than silently ingesting from `{}` and reporting "done" with nothing
    seeded.
    """
    clients = _clients(github, storage)

    results = run_bootstrap(fresh_config, clients, out=lambda _line: None)

    personas_result = next(r for r in results if r.name == bootstrap.STEP_DEFAULT_PERSONAS)
    assert personas_result.status == "done"

    ingest_result = results[-1]
    assert ingest_result.name == bootstrap.STEP_CATALOG_INGEST
    assert ingest_result.status == "done"
    assert "direct" in ingest_result.detail
    # Both packaged default personas, seeded moments earlier in this same
    # run — NOT "personas=0", which is what the raw-field
    # (`configured_catalogs`) mapping would have silently produced.
    assert "personas=2" in ingest_result.detail


# --- explicit skills_repo: still owner-qualified for every GitHub call ------------


def test_skills_publish_owner_qualifies_an_explicit_skills_repo_name(config, storage):
    """Finding 1 regression, the explicit-name variant: `skills_repo:
    "skills"` is a BARE name — org.yaml's own stored convention (see
    `test_render_org_yaml_uses_explicit_names_verbatim_when_set`) — but a
    bare name handed to `gh` resolves under the invoking user's own account,
    never the org's. Every GitHub call this step makes must therefore target
    `f"{github_org}/skills"`, not the bare `"skills"`.
    """
    named_config = config.model_copy(update={"skills_repo": "skills"})
    full_repo = f"{GITHUB_ORG}/skills"
    github = FakeGitHub(track_pushed_content=True)
    clients = _clients(github, storage)

    results = run_bootstrap(named_config, clients, out=lambda _line: None)

    publish_result = next(r for r in results if r.name == bootstrap.STEP_SKILLS_PUBLISH)
    assert publish_result.status == "done"

    # repo_exists and create_repo_from_dir (the fresh-repo path) both saw the
    # full, owner-qualified name — never the bare "skills".
    assert f"repo_exists:{full_repo}" in github.calls
    assert "repo_exists:skills" not in github.calls
    # created_from_dir[0] is the org-config repo (step 1) — the skills repo
    # (this step, step 4) is created second.
    created_repo, _src = github.created_from_dir[1]
    assert created_repo == full_repo


def test_skills_publish_owner_qualifies_an_explicit_skills_repo_name_pr_path(
    config, storage, tmp_path
):
    """Same as above, but the PR path (repo already exists, no workflow
    yet) — covers `clone` and `open_pr` (the other two of the four GitHub
    calls Finding 1 named) with the same explicit bare `skills_repo` value.
    """
    named_config = config.model_copy(update={"skills_repo": "skills"})
    full_repo = f"{GITHUB_ORG}/skills"
    github = FakeGitHub(
        existing_repos={f"{GITHUB_ORG}/pnyx-config", full_repo},
        track_pushed_content=True,
        pr_url="https://github.com/acme-corp/skills/pull/1",
    )
    clients = _clients(github, storage)

    results = run_bootstrap(named_config, clients, out=lambda _line: None)

    publish_result = next(r for r in results if r.name == bootstrap.STEP_SKILLS_PUBLISH)
    assert publish_result.status == "done"

    assert f"clone:{full_repo}" in github.calls
    assert "clone:skills" not in github.calls
    assert len(github.open_pr_calls) == 1
    call = github.open_pr_calls[0]
    assert call["repo"] == full_repo

    # And the content that was actually PR'd carries the rendered workflow —
    # inspected via clone() the same way the other PR-path test does.
    pr_clone = tmp_path / "inspect-pr-named-repo"
    github.clone(full_repo, pr_clone)
    workflow_text = (pr_clone / ".github" / "workflows" / "publish-skills.yml").read_text()
    _assert_no_leftover_placeholders(workflow_text)


# --- idempotence: second run -----------------------------------------------------


def test_second_run_steps_1_through_5_skipped_step_6_still_done(config, github, storage):
    clients = _clients(github, storage)
    run_bootstrap(config, clients, out=lambda _line: None)

    results = run_bootstrap(config, clients, out=lambda _line: None)

    assert [r.status for r in results[:5]] == ["skipped"] * 5
    assert results[-1].name == bootstrap.STEP_CATALOG_INGEST
    assert results[-1].status == "done"
    assert _exit_code(results) == 0
    # Still only the one create_repo_from_dir call per repo across both runs.
    assert len(github.created_from_dir) == 2
    # The skills-publish step's second-run skip came from seeing its own
    # first-run-installed workflow file on the clone, not from a fresh PR.
    assert github.open_pr_calls == []


# --- existing skills repo, no workflow yet: PR path ------------------------------


def test_existing_skills_repo_without_workflow_opens_pr(config, storage, tmp_path):
    github = FakeGitHub(
        existing_repos={f"{GITHUB_ORG}/pnyx-config", SKILLS_REPO},
        track_pushed_content=True,
        pr_url="https://github.com/acme-corp/acme-corp-skills/pull/3",
    )
    clients = _clients(github, storage)

    results = run_bootstrap(config, clients, out=lambda _line: None)

    publish_result = next(r for r in results if r.name == bootstrap.STEP_SKILLS_PUBLISH)
    assert publish_result.status == "done"
    assert publish_result.detail == "https://github.com/acme-corp/acme-corp-skills/pull/3"

    assert len(github.open_pr_calls) == 1
    call = github.open_pr_calls[0]
    assert call["repo"] == SKILLS_REPO
    assert call["branch"] == bootstrap.PUBLISH_PR_BRANCH
    assert call["title"] == "pnyx: publish skills to the catalog bucket"
    assert "publish-skills.yml" in call["body"]

    # The PR'd content actually carries the rendered workflow + script —
    # inspected via clone() (the PR'd workdir itself is a bootstrap-owned
    # tmpdir, cleaned up right after open_pr returns) rather than reading
    # `call["workdir"]` directly off disk.
    pr_clone = tmp_path / "inspect-pr"
    github.clone(SKILLS_REPO, pr_clone)
    workflow_text = (pr_clone / ".github" / "workflows" / "publish-skills.yml").read_text()
    _assert_no_leftover_placeholders(workflow_text)
    assert (pr_clone / ".github" / "scripts" / "publish_skills.py").exists()
    # Both repos pre-existed (org-config repo included) — no
    # create_repo_from_dir call at all, only the PR path.
    assert github.created_from_dir == []


# --- skills_publish_external: skip without touching GitHub -----------------------


def test_skills_publish_external_skips_without_any_github_call(config, github, storage):
    external_config = config.model_copy(update={"skills_publish_external": True})
    clients = _clients(github, storage)

    results = run_bootstrap(external_config, clients, out=lambda _line: None)

    publish_result = next(r for r in results if r.name == bootstrap.STEP_SKILLS_PUBLISH)
    assert publish_result.status == "skipped"
    assert publish_result.detail == "publish pipeline managed by the org"
    # No GitHub call at all mentions the skills repo — the skip happens
    # before github.repo_exists is ever called for it.
    assert not any(SKILLS_REPO in call for call in github.calls)


# --- bucket exists, versioning off: done via ensure_versioning -------------------


def test_bucket_exists_versioning_off_done_via_ensure_versioning(config):
    storage = FakeStorage(buckets={SKILLS_BUCKET: {}})  # pre-existing, NOT versioned
    github = FakeGitHub(track_pushed_content=True)
    clients = _clients(github, storage)

    results = run_bootstrap(config, clients, out=lambda _line: None)

    bucket_result = next(r for r in results if r.name == bootstrap.STEP_SKILLS_BUCKET)
    assert bucket_result.status == "done"
    assert "enabled versioning" in bucket_result.detail


# --- step isolation: a failing storage knob ---------------------------------------


def test_failing_storage_knob_isolates_that_step_and_later_steps_still_run(config, github):
    storage = FakeStorage(ensure_bucket_error="boom")
    clients = _clients(github, storage)

    results = run_bootstrap(config, clients, out=lambda _line: None)
    by_name = {r.name: r for r in results}

    assert by_name[bootstrap.STEP_SKILLS_BUCKET].status == "failed"
    assert by_name[bootstrap.STEP_SKILLS_BUCKET].detail == "boom"
    assert by_name[bootstrap.STEP_PERSONAS_BUCKET].status == "failed"
    assert by_name[bootstrap.STEP_PERSONAS_BUCKET].detail == "boom"

    # Every later step still ran despite the two bucket-step failures.
    assert by_name[bootstrap.STEP_ORG_CONFIG_REPO].status == "done"
    assert by_name[bootstrap.STEP_SKILLS_PUBLISH].status == "done"
    assert by_name[bootstrap.STEP_DEFAULT_PERSONAS].status == "done"
    assert by_name[bootstrap.STEP_CATALOG_INGEST].status == "done"

    assert _exit_code(results) == 1


# --- catalog ingest: primary vs. fallback path -------------------------------------


def test_catalog_ingest_primary_path_used_when_refresh_endpoint_succeeds(
    config, github, storage, monkeypatch
):
    def _post(url, timeout=None):
        assert url == f"{config.api_base_url}/api/catalogs/refresh"
        return httpx.Response(200, json={"skills": 4, "personas": 5})

    monkeypatch.setattr(bootstrap.httpx, "post", _post)
    clients = _clients(github, storage)

    results = run_bootstrap(config, clients, out=lambda _line: None)

    ingest_result = results[-1]
    assert ingest_result.status == "done"
    assert "skills=4" in ingest_result.detail
    assert "personas=5" in ingest_result.detail
    assert "direct" not in ingest_result.detail


def test_catalog_ingest_falls_back_on_non_2xx_response(config, github, storage, monkeypatch):
    monkeypatch.setattr(bootstrap.httpx, "post", lambda *a, **kw: httpx.Response(503))
    clients = _clients(github, storage)

    results = run_bootstrap(config, clients, out=lambda _line: None)

    ingest_result = results[-1]
    assert ingest_result.status == "done"
    assert "direct" in ingest_result.detail


def test_catalog_ingest_failed_only_when_both_paths_raise(config, github, monkeypatch):
    def _raise_post(*args, **kwargs):
        raise httpx.ConnectError("connection refused")

    monkeypatch.setattr(bootstrap.httpx, "post", _raise_post)
    storage = FakeStorage(read_error="storage is down")
    clients = _clients(github, storage)

    results = run_bootstrap(config, clients, out=lambda _line: None)

    ingest_result = results[-1]
    assert ingest_result.status == "failed"
    assert ingest_result.detail == "storage is down"


# --- exit code -------------------------------------------------------------------


def test_exit_code_zero_when_every_step_done_or_skipped():
    results = [StepResult("a", "done", ""), StepResult("b", "skipped", "")]
    assert _exit_code(results) == 0


def test_exit_code_one_when_any_step_failed():
    results = [StepResult("a", "done", ""), StepResult("b", "failed", "boom")]
    assert _exit_code(results) == 1


# --- output formatting -------------------------------------------------------------


def test_output_lines_are_column_aligned():
    results = [
        StepResult("first step", "done", "d"),
        StepResult("second step", "skipped", "s"),
        StepResult("third step", "failed", "f"),
    ]
    lines = [bootstrap._format_line(r) for r in results]
    name_starts = {line.index(r.name) for line, r in zip(lines, results, strict=True)}
    assert len(name_starts) == 1
