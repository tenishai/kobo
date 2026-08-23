"""Bootstrap CLI (Phase A Task 7): `python -m kobo_api.bootstrap` wires a
fresh client org in one idempotent command — the org-config repo, the two
catalog buckets, the skills repo's publish workflow, the packaged default
personas, and a first catalog ingest.

Every step is independently safe to re-run: each one checks the live state
first (a repo/bucket already there, a file already installed, an index entry
already seeded) and reports "skipped" rather than repeating work or
clobbering something an operator may have hand-edited since. Running this
against an already-wired org converges to a no-op except for the catalog
ingest, which always re-syncs (Task 7 controller decision #2 — re-running it
is itself the point, not a side effect to avoid).

Each step also runs in isolation: an exception in one is caught, reported as
`failed` with a one-line message, and every later step still runs — one
broken bucket permission shouldn't block seeding personas into the other
bucket, say. `main()`'s exit code is 1 iff any step failed, 0 otherwise.
"""

from __future__ import annotations

import logging
import tempfile
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

import httpx
import yaml

from kobo_api.config import Config
from kobo_api.db import make_engine
from kobo_api.org import ORG_KEYS, load_org_config
from kobo_api.personas import seed_default_personas
from kobo_api.search import ensure_fts, ingest_indexes

if TYPE_CHECKING:
    from kobo_api.clients import Clients

log = logging.getLogger(__name__)

_TEMPLATES_DIR = Path(__file__).parent / "templates"
_ORG_CONFIG_DIR = _TEMPLATES_DIR / "org_config"
_SKILLS_REPO_DIR = _TEMPLATES_DIR / "skills_repo"
_SKILLS_PUBLISH_DIR = _TEMPLATES_DIR / "skills_publish"
_PUBLISH_SCRIPT_PATH = _SKILLS_PUBLISH_DIR / "publish_skills.py"

# The branch bootstrap opens its skills-repo publish-workflow PR from — fixed
# (not per-run unique) so a second bootstrap run against a repo whose PR is
# still open reuses the same branch name rather than piling up duplicates.
PUBLISH_PR_BRANCH = "kobo-skills-publish"

STEP_ORG_CONFIG_REPO = "org-config repo"
STEP_SKILLS_BUCKET = "skills bucket"
STEP_PERSONAS_BUCKET = "personas bucket"
STEP_SKILLS_PUBLISH = "skills repo + publish workflow"
STEP_DEFAULT_PERSONAS = "default personas"
STEP_CATALOG_INGEST = "catalog ingest"

# Operator-facing copy for the skills-publish-workflow install PR (step 4's
# non-fresh-repo path) — what it does, plus the merge instruction the brief
# calls for. Never built from any config value, so it's a fixed constant.
_PUBLISH_PR_BODY = (
    "kobo's bootstrap noticed this repo has no skills-publish workflow yet, "
    "and opened this PR to add one.\n\n"
    "This PR adds:\n"
    "- `.github/workflows/publish-skills.yml` — validates every skill on "
    "each pull request (no cloud auth needed for that), and publishes to "
    "the catalog bucket on every push to main\n"
    "- `.github/scripts/publish_skills.py` — the validator/publisher itself\n\n"
    "Nothing here touches any existing skill content. Merge whenever it's "
    "convenient — kobo reads the published bucket, never this repo "
    "directly, so nothing depends on this landing quickly.\n"
)


@dataclass
class StepResult:
    name: str
    status: str  # "done" | "skipped" | "failed"
    detail: str = ""


def _skills_repo_name(config: Config) -> str:
    if config.skills_repo and "/" in config.skills_repo:
        raise ValueError(
            "skills_repo must be a bare repo name (no '/'); kobo adds the owner"
        )
    return config.skills_repo or f"{config.github_org}-skills"


def _skills_bucket_name(config: Config) -> str:
    return config.skills_bucket or f"{config.github_org}-skills"


def _personas_bucket_name(config: Config) -> str:
    return config.personas_bucket or f"{config.github_org}-personas"


# --- org.yaml rendering (literal token replacement, matching Task 6's --------
# publish-skills.yml convention: never .format()'d/Jinja-rendered, so nothing
# in the template's own content is at risk of being misread as a format spec) -


# One token per kobo_api.org.ORG_KEYS entry — kept in exact 1:1 correspondence
# with that frozenset (the module-level assert right below fails fast on
# import if they ever drift; a missing/extra key is also independently
# caught by any test that renders org.yaml at all — a missing token leaves a
# literal `__KOBO_*__` behind, an extra one raises KeyError immediately).

_ORG_YAML_TOKENS = {
    "github_org": "__KOBO_GITHUB_ORG__",
    "skills_repo": "__KOBO_SKILLS_REPO__",
    "skills_bucket": "__KOBO_SKILLS_BUCKET__",
    "personas_bucket": "__KOBO_PERSONAS_BUCKET__",
    "gojira_cluster": "__KOBO_GOJIRA_CLUSTER__",
    "runtime_image": "__KOBO_RUNTIME_IMAGE__",
    "gcp_project": "__KOBO_GCP_PROJECT__",
    "vertex_project": "__KOBO_VERTEX_PROJECT__",
    "vertex_location": "__KOBO_VERTEX_LOCATION__",
    "model_group": "__KOBO_MODEL_GROUP__",
    "avatar_model": "__KOBO_AVATAR_MODEL__",
    "avatar_location": "__KOBO_AVATAR_LOCATION__",
    "models": "__KOBO_MODELS__",
    "skills_publish_wif_provider": "__KOBO_SKILLS_PUBLISH_WIF_PROVIDER__",
    "skills_publish_service_account": "__KOBO_SKILLS_PUBLISH_SERVICE_ACCOUNT__",
    "skills_publish_external": "__KOBO_SKILLS_PUBLISH_EXTERNAL__",
    "slack_operators": "__KOBO_SLACK_OPERATORS__",
    "operators": "__KOBO_OPERATORS__",
    "admin_groups": "__KOBO_ADMIN_GROUPS__",
    "cabal_url": "__KOBO_CABAL_URL__",
    "agent_endpoint_template": "__KOBO_AGENT_ENDPOINT_TEMPLATE__",
    "groups": "__KOBO_GROUPS__",
    "okta_domain": "__KOBO_OKTA_DOMAIN__",
    "mcp": "__KOBO_MCP__",
}

assert set(_ORG_YAML_TOKENS) == ORG_KEYS, (
    "bootstrap's org.yaml token map has drifted from kobo_api.org.ORG_KEYS"
)


def _org_yaml_context(config: Config) -> dict[str, object]:
    """The exact ORG_KEYS-keyed value set org.yaml renders from — the
    CURRENT merged config's values, with the skills_repo/skills_bucket/
    personas_bucket fallback-when-unset defaulting applied (every other key
    is carried over verbatim, cluster/runtime/vertex/model settings
    included).
    """
    return {
        "github_org": config.github_org,
        "skills_repo": _skills_repo_name(config),
        "skills_bucket": _skills_bucket_name(config),
        "personas_bucket": _personas_bucket_name(config),
        "gojira_cluster": config.gojira_cluster,
        "runtime_image": config.runtime_image,
        "gcp_project": config.gcp_project,
        "vertex_project": config.vertex_project,
        "vertex_location": config.vertex_location,
        "model_group": config.model_group,
        "avatar_model": config.avatar_model,
        "avatar_location": config.avatar_location,
        "models": [{"id": m.id, "label": m.label} for m in config.models],
        "skills_publish_wif_provider": config.skills_publish_wif_provider,
        "skills_publish_service_account": config.skills_publish_service_account,
        "skills_publish_external": config.skills_publish_external,
        "slack_operators": config.slack_operators,
        "operators": config.operators,
        "admin_groups": config.admin_groups,
        "cabal_url": config.cabal_url,
        "agent_endpoint_template": config.agent_endpoint_template,
        "groups": [{"name": g.name, "okta_groups": list(g.okta_groups)} for g in config.groups],
        "okta_domain": config.okta_domain,
        "mcp": {
            "oauth_client_id": config.mcp.oauth_client_id,
            "oauth_client_secret_env": config.mcp.oauth_client_secret_env,
            "workspace_domain": config.mcp.workspace_domain,
            "issuer_base_url": config.mcp.issuer_base_url,
            "jwt_key_env": config.mcp.jwt_key_env,
        },
    }


def _yaml_scalar(value: object) -> str:
    """`value` rendered as a single-line YAML value (a flow-style `[...]`
    for `models`, a plain/quoted scalar for everything else) via
    `yaml.safe_dump`, which already knows how to quote/escape whatever needs
    it (an empty string, a bool, a string containing a `/` or `:`) — safer
    than hand-rolling that. `safe_dump` appends a `...` document-end marker
    for some bare scalars (e.g. `true`/`false`); stripped back off here since
    this is substituted into a single existing line, not its own document.
    """
    dumped = yaml.safe_dump(value, default_flow_style=True, sort_keys=False).strip()
    if dumped.endswith("..."):
        dumped = dumped[: -len("...")].rstrip()
    return dumped


def render_org_yaml(config: Config) -> str:
    """`org_config/org.yaml.tmpl` with every `__KOBO_*__` token literally
    replaced by `config`'s current value for that key (see
    `_org_yaml_context`) — deterministic, golden-tested.
    """
    text = (_ORG_CONFIG_DIR / "org.yaml.tmpl").read_text()
    context = _org_yaml_context(config)
    for key, token in _ORG_YAML_TOKENS.items():
        text = text.replace(token, _yaml_scalar(context[key]))
    return text


def _org_config_readme() -> str:
    return (_ORG_CONFIG_DIR / "README.md").read_text()


def _skills_repo_readme() -> str:
    return (_SKILLS_REPO_DIR / "README.md").read_text()


def render_publish_workflow(config: Config, bucket: str) -> str:
    """Task 6's `publish-skills.yml` template with its three `__KOBO_*__`
    tokens literally replaced — same non-.format()/non-Jinja convention as
    `render_org_yaml` (and as that template's own docstring demands, so its
    `${{ }}` GitHub Actions expressions pass through untouched).
    """
    text = (_SKILLS_PUBLISH_DIR / "publish-skills.yml").read_text()
    text = text.replace("__KOBO_BUCKET__", bucket)
    text = text.replace("__KOBO_WIF_PROVIDER__", config.skills_publish_wif_provider)
    text = text.replace("__KOBO_SERVICE_ACCOUNT__", config.skills_publish_service_account)
    return text


# --- Steps --------------------------------------------------------------------


def _step_org_config_repo(config: Config, clients: Clients) -> StepResult:
    repo = f"{config.github_org}/kobo-config"
    if clients.github.repo_exists(repo):
        return StepResult(STEP_ORG_CONFIG_REPO, "skipped", f"{repo} already exists")

    with tempfile.TemporaryDirectory() as tmp:
        tmp_dir = Path(tmp)
        (tmp_dir / "org.yaml").write_text(render_org_yaml(config))
        (tmp_dir / "README.md").write_text(_org_config_readme())
        clients.github.create_repo_from_dir(repo, tmp_dir)

    return StepResult(
        STEP_ORG_CONFIG_REPO,
        "done",
        f"created {repo} — point org_repo at it in your local config",
    )


def _step_bucket(name: str, bucket: str, clients: Clients) -> StepResult:
    result = clients.storage.ensure_bucket(bucket)
    if result == "created":
        return StepResult(name, "done", f"created bucket {bucket}")

    changed = clients.storage.ensure_versioning(bucket)
    if changed:
        return StepResult(name, "done", f"bucket {bucket} already existed — enabled versioning")
    return StepResult(name, "skipped", f"bucket {bucket} already exists and is versioned")


def _step_skills_bucket(config: Config, clients: Clients) -> StepResult:
    return _step_bucket(STEP_SKILLS_BUCKET, _skills_bucket_name(config), clients)


def _step_personas_bucket(config: Config, clients: Clients) -> StepResult:
    return _step_bucket(STEP_PERSONAS_BUCKET, _personas_bucket_name(config), clients)


def _scaffold_skills_repo_dir(dest: Path, config: Config, bucket: str) -> None:
    (dest / "README.md").write_text(_skills_repo_readme())
    workflows_dir = dest / ".github" / "workflows"
    workflows_dir.mkdir(parents=True, exist_ok=True)
    (workflows_dir / "publish-skills.yml").write_text(render_publish_workflow(config, bucket))
    scripts_dir = dest / ".github" / "scripts"
    scripts_dir.mkdir(parents=True, exist_ok=True)
    (scripts_dir / "publish_skills.py").write_bytes(_PUBLISH_SCRIPT_PATH.read_bytes())


def _step_skills_publish(config: Config, clients: Clients) -> StepResult:
    if config.skills_publish_external:
        # A real client already runs its own publisher into its bucket;
        # kobo adopts what lands there, never overwrites it.
        return StepResult(STEP_SKILLS_PUBLISH, "skipped", "publish pipeline managed by the org")

    # `_skills_repo_name` is a BARE name (org.yaml's own stored convention —
    # see `_org_yaml_context`) — but every GitHub call bootstrap itself makes
    # must be owner-qualified: `gh` resolves a bare repo argument under the
    # invoking user's own account, never the org's, so a bare name here would
    # silently target the wrong namespace (Finding 1). Step 1's org-config
    # repo already gets this right; this is the same fix applied here.
    repo = f"{config.github_org}/{_skills_repo_name(config)}"
    bucket = _skills_bucket_name(config)

    if not clients.github.repo_exists(repo):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_dir = Path(tmp)
            _scaffold_skills_repo_dir(tmp_dir, config, bucket)
            clients.github.create_repo_from_dir(repo, tmp_dir)
        return StepResult(
            STEP_SKILLS_PUBLISH, "done", f"created {repo} with the publish workflow"
        )

    with tempfile.TemporaryDirectory() as tmp:
        workdir = Path(tmp)
        clients.github.clone(repo, workdir)

        workflow_path = workdir / ".github" / "workflows" / "publish-skills.yml"
        if workflow_path.exists():
            return StepResult(
                STEP_SKILLS_PUBLISH, "skipped", f"{repo} already has a publish workflow"
            )

        # Live-incident fix (idempotence re-run): a prior run may have
        # already opened the publish-workflow PR from this same fixed branch
        # (PUBLISH_PR_BRANCH) and it's still open — the workflow hasn't
        # reached the default branch yet, so workflow_path.exists() above
        # still says "absent". Retrying open_pr() in that state re-pushes
        # the same branch name and fails non-fast-forward against the
        # remote branch the earlier run already pushed. Checking for that
        # open PR first and reporting "skipped" is the idempotent response;
        # only a genuinely fresh branch falls through to open_pr() below.
        existing_pr_url = clients.github.find_pr(repo, branch=PUBLISH_PR_BRANCH)
        if existing_pr_url is not None:
            return StepResult(
                STEP_SKILLS_PUBLISH, "skipped", f"PR already open: {existing_pr_url}"
            )

        workflow_path.parent.mkdir(parents=True, exist_ok=True)
        workflow_path.write_text(render_publish_workflow(config, bucket))
        scripts_path = workdir / ".github" / "scripts" / "publish_skills.py"
        scripts_path.parent.mkdir(parents=True, exist_ok=True)
        scripts_path.write_bytes(_PUBLISH_SCRIPT_PATH.read_bytes())

        pr_url = clients.github.open_pr(
            workdir,
            repo=repo,
            branch=PUBLISH_PR_BRANCH,
            title="kobo: publish skills to the catalog bucket",
            body=_PUBLISH_PR_BODY,
        )

    return StepResult(STEP_SKILLS_PUBLISH, "done", pr_url)


def _step_default_personas(config: Config, clients: Clients) -> StepResult:
    bucket = _personas_bucket_name(config)
    counts = seed_default_personas(clients.storage, bucket=bucket)
    detail = f"seeded {counts['seeded']}, skipped {counts['skipped']}"
    status = "done" if counts["seeded"] > 0 else "skipped"
    return StepResult(STEP_DEFAULT_PERSONAS, status, detail)


def _step_catalog_ingest(config: Config, clients: Clients) -> StepResult:
    base_url = config.api_base_url.rstrip("/")
    try:
        resp = httpx.post(f"{base_url}/api/catalogs/refresh", timeout=5.0)
        if 200 <= resp.status_code < 300:
            body = resp.json()
            return StepResult(
                STEP_CATALOG_INGEST,
                "done",
                f"skills={body.get('skills', 0)}, personas={body.get('personas', 0)}",
            )
        log.info(
            "catalog ingest: refresh endpoint returned %s, falling back to a direct ingest",
            resp.status_code,
        )
    except Exception as exc:  # noqa: BLE001
        # Broad by design (Task 7 controller decision #2: "ANY exception or
        # non-2xx -> fall back") — a connect error, a timeout, a malformed
        # JSON body, anything: this kobo-api may simply not be up yet on a
        # fresh org's very first bootstrap run, and the exact failure mode
        # is irrelevant to what happens next (the direct-ingest fallback
        # below), only worth logging.
        log.info(
            "catalog ingest: refresh endpoint unreachable (%r), falling back to a direct ingest",
            exc,
        )

    engine = make_engine(config.db_url)
    ensure_fts(engine)
    # Built from the COMPUTED bucket-name helpers (same ones steps 2/3/5 use),
    # never `kobo_api.main.configured_catalogs` (Finding 2): that reads the
    # raw `config.skills_bucket`/`personas_bucket` fields, which are still ""
    # on a fresh org (the fallback-when-unset defaulting lives in
    # `_skills_bucket_name`/`_personas_bucket_name`, not on the Config fields
    # themselves) — so the raw-field mapping would silently be `{}` and this
    # ingest a no-op reported as "done". Both helpers always return a
    # non-empty name after defaulting, so this mapping is never empty.
    catalogs = {
        "skill": _skills_bucket_name(config),
        "persona": _personas_bucket_name(config),
    }
    counts = ingest_indexes(engine, clients.storage, catalogs)
    return StepResult(
        STEP_CATALOG_INGEST,
        "done",
        f"direct: skills={counts.get('skill', 0)}, personas={counts.get('persona', 0)}",
    )


_STEPS: list[Callable[[Config, Clients], StepResult]] = [
    _step_org_config_repo,
    _step_skills_bucket,
    _step_personas_bucket,
    _step_skills_publish,
    _step_default_personas,
    _step_catalog_ingest,
]

# Every step's name, in run order — used only to label a step that raised
# before it could build its own StepResult (the step functions themselves
# still carry their own name into every StepResult they DO return, so this
# list and each function's own STEP_* constant must never drift apart; the
# fixed run order below zips them back together 1:1).
_STEP_NAMES = [
    STEP_ORG_CONFIG_REPO,
    STEP_SKILLS_BUCKET,
    STEP_PERSONAS_BUCKET,
    STEP_SKILLS_PUBLISH,
    STEP_DEFAULT_PERSONAS,
    STEP_CATALOG_INGEST,
]

# Status-prefix column width for run_bootstrap's aligned output (Task 7
# controller decision #4) — "skipped:" is the longest of the three prefixes,
# plus one trailing space, so every step's name starts at the same column
# regardless of which status it reports.
_STATUS_COL_WIDTH = len("skipped:") + 1


def _format_line(result: StepResult) -> str:
    return f"{(result.status + ':').ljust(_STATUS_COL_WIDTH)}{result.name} — {result.detail}"


def run_bootstrap(
    config: Config, clients: Clients, *, out: Callable[[str], None] = print
) -> list[StepResult]:
    """Run every bootstrap step in order, in isolation: a step that raises is
    reported as `failed` with a plain one-line message (the full exception is
    logged), and every later step still runs regardless. `out` receives one
    aligned, operator-facing line per step as it completes (default: print).
    """
    results: list[StepResult] = []
    for step_name, step_func in zip(_STEP_NAMES, _STEPS, strict=True):
        try:
            result = step_func(config, clients)
        except Exception as exc:  # broad by design: a step boundary, not a specific failure mode
            log.exception("bootstrap step %r failed", step_name)
            result = StepResult(step_name, "failed", str(exc))
        results.append(result)
        out(_format_line(result))
    return results


def _exit_code(results: list[StepResult]) -> int:
    return 1 if any(r.status == "failed" for r in results) else 0


def main(argv: list[str] | None = None) -> int:
    """Load config + merge org config exactly like `build_default_app`
    (`kobo_api.main`), then run every bootstrap step against the real
    clients.

    The org repo may not exist yet on a first run — `load_org_config`
    already tolerates that (falls back to the local config, unwired) rather
    than raising, and proceeding on an unwired org here is exactly right:
    fixing that is this command's own job, not a precondition for running
    it. `argv` is accepted (for parity with a standard CLI entry point) but
    unused — this command takes no flags.
    """
    # Local import: keeps the httpx/subprocess-backed real clients out of
    # this module's own import graph for callers that only need
    # `run_bootstrap`/`StepResult` against fakes (same rationale as
    # `kobo_api.main.build_default_app`'s identical local import).
    from kobo_api.clients import Clients, CommandRunner, GhCLI

    local_config = Config.load(None)
    github = GhCLI(runner=CommandRunner())
    config, _org = load_org_config(local_config, github)

    config.workdir.mkdir(parents=True, exist_ok=True)
    clients = Clients.real(config)

    results = run_bootstrap(config, clients)
    return _exit_code(results)


if __name__ == "__main__":
    raise SystemExit(main())
