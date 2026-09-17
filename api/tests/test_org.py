"""Tests for kobo_api.org: clone kobo-config, read org.yaml, merge over the
locally-loaded Config. Every case exercises real org.yaml content flowing
through FakeGitHub.clone into an actual merged Config — never mocks
asserting mocks.
"""

from kobo_api.config import Config, GroupChoice, ModelChoice
from kobo_api.main import create_app
from kobo_api.org import ORG_KEYS, OrgState, load_org_config
from tests.fakes import FakeGitHub

ORG_REPO = "acme-corp/kobo-config"


def test_org_repo_none_is_unwired_without_cloning():
    local = Config()
    github = FakeGitHub()

    merged, org = load_org_config(local, github)

    assert merged is local
    assert org == OrgState(False, None, "no org configured")
    assert github.cloned == []


def test_org_yaml_overrides_local_for_org_keys():
    local = Config(org_repo=ORG_REPO)
    github = FakeGitHub(
        org_yaml=(
            "github_org: acme-corp\n"
            "gojira_cluster: acme-cluster\n"
            "vertex_project: acme-models\n"
        )
    )

    merged, org = load_org_config(local, github)

    assert org.wired is True
    assert org.repo == ORG_REPO
    assert org.error is None
    assert merged.github_org == "acme-corp"
    assert merged.gojira_cluster == "acme-cluster"
    assert merged.vertex_project == "acme-models"
    # Fields org.yaml didn't touch keep the local (default) value.
    assert merged.vertex_location == local.vertex_location
    assert merged.workdir == local.workdir
    assert merged.db_url == local.db_url


def test_clone_is_called_with_org_repo_into_a_fresh_temp_dir_each_time():
    local = Config(org_repo=ORG_REPO)
    github = FakeGitHub(org_yaml="github_org: acme-corp\n")

    load_org_config(local, github)
    load_org_config(local, github)

    assert len(github.cloned) == 2
    (first_repo, first_dest), (second_repo, second_dest) = github.cloned
    assert first_repo == ORG_REPO
    assert second_repo == ORG_REPO
    # A fresh temp dir per call (no fetch/caching), and cleaned up again
    # once load_org_config returns — never left behind on disk.
    assert first_dest != second_dest
    assert not first_dest.exists()
    assert not second_dest.exists()


def test_clone_client_error_is_unwired_with_reason():
    local = Config(org_repo=ORG_REPO)
    github = FakeGitHub(clone_error="repo not found or access denied")

    merged, org = load_org_config(local, github)

    assert merged is local
    assert org.wired is False
    assert org.repo == ORG_REPO
    assert org.error is not None
    assert "repo not found or access denied" in org.error


def test_missing_org_yaml_in_cloned_repo_is_unwired():
    local = Config(org_repo=ORG_REPO)
    github = FakeGitHub()  # org_yaml=None -> clone writes no org.yaml at all

    merged, org = load_org_config(local, github)

    assert merged is local
    assert org.wired is False
    assert org.repo == ORG_REPO
    assert org.error is not None


def test_empty_org_yaml_is_unwired():
    local = Config(org_repo=ORG_REPO)
    github = FakeGitHub(org_yaml="")

    merged, org = load_org_config(local, github)

    assert merged is local
    assert org.wired is False
    assert org.repo == ORG_REPO
    assert org.error is not None


def test_bad_yaml_is_unwired():
    local = Config(org_repo=ORG_REPO)
    github = FakeGitHub(org_yaml="github_org: [unterminated\n")

    merged, org = load_org_config(local, github)

    assert merged is local
    assert org.wired is False
    assert org.repo == ORG_REPO
    assert org.error is not None
    # Message must be a plain single-line sentence, not raw exception text.
    assert "\n" not in org.error
    assert "http" not in org.error
    assert "org.yaml" in org.error


def test_org_yaml_not_a_mapping_is_unwired():
    local = Config(org_repo=ORG_REPO)
    github = FakeGitHub(org_yaml="- one\n- two\n")

    merged, org = load_org_config(local, github)

    assert merged is local
    assert org.wired is False
    assert org.repo == ORG_REPO
    assert org.error is not None


def test_unknown_key_is_unwired_and_names_the_key():
    local = Config(org_repo=ORG_REPO)
    github = FakeGitHub(org_yaml="totally_made_up_key: whatever\n")

    merged, org = load_org_config(local, github)

    assert merged is local
    assert org.wired is False
    assert org.repo == ORG_REPO
    assert "totally_made_up_key" in org.error


def test_db_url_in_org_yaml_is_rejected_as_unknown_key():
    """db_url is machine-local, not org-scoped — it's deliberately absent
    from ORG_KEYS, so an org.yaml that sets it fails the same way any other
    unrecognized key does, and the local db_url is never touched."""
    local = Config(org_repo=ORG_REPO, db_url="sqlite:///real.db")
    github = FakeGitHub(org_yaml="db_url: sqlite:///evil.db\n")

    merged, org = load_org_config(local, github)

    assert merged is local
    assert merged.db_url == "sqlite:///real.db"
    assert org.wired is False
    assert "db_url" in org.error


def test_workdir_in_org_yaml_is_rejected_as_unknown_key():
    local = Config(org_repo=ORG_REPO)
    github = FakeGitHub(org_yaml="workdir: /tmp/somewhere-else\n")

    merged, org = load_org_config(local, github)

    assert merged is local
    assert merged.workdir == local.workdir
    assert org.wired is False
    assert "workdir" in org.error


def test_bad_org_value_fails_config_validation_and_is_unwired():
    """models must be a list of {id, label} — a plain string value for it
    passes the ORG_KEYS allowlist check but fails Config's own validation
    on merge, which must be caught (never crash boot)."""
    local = Config(org_repo=ORG_REPO)
    github = FakeGitHub(org_yaml="models: not-a-list\n")

    merged, org = load_org_config(local, github)

    assert merged is local
    assert org.wired is False
    assert org.repo == ORG_REPO
    assert org.error is not None
    # Message must be a plain single-line sentence naming the field, not raw pydantic text.
    assert "\n" not in org.error
    assert "http" not in org.error
    assert "models" in org.error


def test_models_list_round_trips_through_model_choice():
    local = Config(org_repo=ORG_REPO)
    github = FakeGitHub(
        org_yaml=(
            "models:\n"
            "  - id: vertex_ai/claude-opus-4\n"
            "    label: Claude Opus 4\n"
        )
    )

    merged, org = load_org_config(local, github)

    assert org.wired is True
    assert merged.models == [ModelChoice(id="vertex_ai/claude-opus-4", label="Claude Opus 4")]
    assert isinstance(merged.models[0], ModelChoice)


def test_skills_publish_external_bool_round_trips():
    local = Config(org_repo=ORG_REPO)
    github = FakeGitHub(
        org_yaml=(
            "skills_publish_external: true\n"
            "skills_publish_wif_provider: projects/123/locations/global/workloadIdentityPools/x\n"
            "skills_publish_service_account: publisher@acme.iam.gserviceaccount.com\n"
        )
    )

    merged, org = load_org_config(local, github)

    assert org.wired is True
    assert merged.skills_publish_external is True
    assert merged.skills_publish_wif_provider == (
        "projects/123/locations/global/workloadIdentityPools/x"
    )
    assert merged.skills_publish_service_account == "publisher@acme.iam.gserviceaccount.com"


# --- create_app's `org` keyword (stored at app.state.org) ---------------------


def test_create_app_defaults_org_state_when_omitted(session_factory):
    app = create_app(session_factory, config=Config())

    assert app.state.org == OrgState(False, None, "no org configured")


def test_create_app_stores_the_given_org_state(session_factory):
    given = OrgState(True, ORG_REPO)

    app = create_app(session_factory, config=Config(), org=given)

    assert app.state.org is given


def test_create_app_default_org_state_is_not_shared_across_apps(session_factory):
    """The `org` keyword defaults to None in the signature, with the actual
    OrgState built fresh inside the function body — never a single mutable
    OrgState(...) instance living in the signature itself and handed out to
    every caller that omits `org`. Two apps built without passing `org` must
    each get their own instance."""
    app1 = create_app(session_factory, config=Config())
    app2 = create_app(session_factory, config=Config())

    assert app1.state.org == app2.state.org
    assert app1.state.org is not app2.state.org


def test_org_keys_is_exactly_the_brief_set():
    assert ORG_KEYS == frozenset(
        {
            "github_org",
            "skills_repo",
            "skills_bucket",
            "personas_bucket",
            "gojira_cluster",
            "runtime_image",
            "memory_replica_bucket",
        "broker_url",
        "liminal_url",
        "argus_url",
        "argus_project",
        "runtimes",
        "reader_groups",
            "gcp_project",
            "vertex_project",
            "vertex_location",
            "model_group",
            "avatar_model",
            "avatar_location",
            "models",
            "skills_publish_wif_provider",
            "skills_publish_service_account",
            "skills_publish_external",
            "slack_operators",
            "operators",
            "admin_groups",
            "cabal_url",
            "agent_endpoint_template",
            "groups",
            "okta_domain",
            "mcp",
        }
    )


def test_override_of_explicit_local_value_is_disclosed(caplog):
    """IE-6065: org.yaml silently defeating a value the deployment config
    explicitly set (the Secret's digest pin vs. org.yaml's floating tag) was
    invisible from either file alone. The merge now says so, once per
    differing key, at WARNING (the app configures no logging, so INFO from
    this logger never reaches pod logs)."""
    import logging

    local = Config(
        org_repo=ORG_REPO,
        runtime_image="reg.example/ai/runtime@sha256:aaa111",
        gojira_cluster="acme-cluster",
    )
    github = FakeGitHub(
        org_yaml=(
            "runtime_image: reg.example/ai/runtime:floating\n"
            "gojira_cluster: acme-cluster\n"
            "vertex_project: acme-models\n"
        )
    )

    with caplog.at_level(logging.WARNING, logger="kobo_api.org"):
        merged, org = load_org_config(local, github)

    assert org.wired is True
    assert merged.runtime_image == "reg.example/ai/runtime:floating"
    messages = [r.getMessage() for r in caplog.records]
    disclosures = [m for m in messages if "org.yaml overrides" in m]
    # The differing explicitly-set key is disclosed, with both values.
    assert len(disclosures) == 1
    assert "runtime_image" in disclosures[0]
    assert "sha256:aaa111" in disclosures[0]
    assert "floating" in disclosures[0]
    # Same-value override and default-only override stay silent.
    assert "gojira_cluster" not in disclosures[0]
    assert "vertex_project" not in disclosures[0]


def test_composite_override_is_disclosed_without_dumping_the_value(caplog):
    """A differing composite (list/dict) key is disclosed by shape, not by
    value — `groups` or `models` dumped verbatim would drown the line that
    matters and could echo operator emails into logs."""
    import logging

    local = Config(org_repo=ORG_REPO, operators=["a@acme.test"])
    github = FakeGitHub(org_yaml="operators: [a@acme.test, b@acme.test]\n")

    with caplog.at_level(logging.WARNING, logger="kobo_api.org"):
        merged, org = load_org_config(local, github)

    assert org.wired is True
    disclosures = [
        r.getMessage() for r in caplog.records if "org.yaml overrides" in r.getMessage()
    ]
    assert len(disclosures) == 1
    assert "operators" in disclosures[0]
    assert "a@acme.test" not in disclosures[0]
    assert "list with 1" in disclosures[0]
    assert "list with 2" in disclosures[0]


def test_slack_operators_list_round_trips():
    """slack_operators (Task 5) is org-scoped like the other gating knobs
    above — a client's kobo-config repo can name its own operator emails."""
    local = Config(org_repo=ORG_REPO)
    github = FakeGitHub(
        org_yaml=(
            "slack_operators:\n"
            "  - ops@acme-corp.example.com\n"
            "  - Root@Acme-Corp.example.com\n"
        )
    )

    merged, org = load_org_config(local, github)

    assert org.wired is True
    assert merged.slack_operators == [
        "ops@acme-corp.example.com",
        "Root@Acme-Corp.example.com",
    ]


def test_cabal_keys_round_trip():
    """cabal_url/agent_endpoint_template (colleagues switchboard arc, Task 1)
    are org-scoped exactly like the other feature-gating knobs above - a
    client's kobo-config repo names its own switchboard coordinates."""
    local = Config(org_repo=ORG_REPO)
    github = FakeGitHub(
        org_yaml=(
            "cabal_url: http://cabal.internal\n"
            "agent_endpoint_template: http://agent-{name}.default.svc.cluster.local\n"
        )
    )

    merged, org = load_org_config(local, github)

    assert org.wired is True
    assert merged.cabal_url == "http://cabal.internal"
    assert merged.agent_endpoint_template == "http://agent-{name}.default.svc.cluster.local"


def test_mcp_keys_round_trip():
    """mcp (MCP server arc, Task 1) is org-scoped exactly like the other
    feature-gating knobs above -- a client's kobo-config repo points its
    own MCP server at its own Google OAuth app, Workspace domain, and
    externally-reachable issuer_base_url. Only the fields a real client
    sets are given here; MCPConfig's own defaults fill in the rest on
    re-validation (mirrors admin_groups/groups re-validation the same way).
    issuer_base_url is included (not just oauth_client_id/workspace_domain)
    because Config._mcp_requires_an_issuer_base_url_when_enabled (fix round
    1) now fails construction without it once oauth_client_id is set."""
    local = Config(org_repo=ORG_REPO)
    github = FakeGitHub(
        org_yaml=(
            "mcp:\n"
            "  oauth_client_id: some-client-id.apps.example.com\n"
            "  workspace_domain: example.com\n"
            "  issuer_base_url: https://kobo.example.org\n"
        )
    )

    merged, org = load_org_config(local, github)

    assert org.wired is True
    assert merged.mcp.oauth_client_id == "some-client-id.apps.example.com"
    assert merged.mcp.workspace_domain == "example.com"
    assert merged.mcp.issuer_base_url == "https://kobo.example.org"
    assert merged.mcp.oauth_client_secret_env == "KOBO_MCP_GOOGLE_CLIENT_SECRET"
    assert merged.mcp.jwt_key_env == "KOBO_MCP_JWT_KEY"
    assert merged.mcp_enabled is True


def test_operators_list_round_trips():
    """operators (Task 4: agent destruction) is org-scoped exactly like
    slack_operators above — a client's kobo-config repo can name its own
    destroy-operator emails, independently of who may set up Slack."""
    local = Config(org_repo=ORG_REPO)
    github = FakeGitHub(
        org_yaml=(
            "operators:\n"
            "  - ops@acme-corp.example.com\n"
            "  - Root@Acme-Corp.example.com\n"
        )
    )

    merged, org = load_org_config(local, github)

    assert org.wired is True
    assert merged.operators == [
        "ops@acme-corp.example.com",
        "Root@Acme-Corp.example.com",
    ]


def test_admin_groups_list_round_trips():
    """admin_groups (operator-status-by-group ruling) is org-scoped exactly
    like operators above -- a client's kobo-config repo can name its own
    admin teams, independently of the plain operators email list. Every
    admin_groups entry must name a groups entry (Config's own `_admin_
    groups_must_be_configured_groups` validator), so the org.yaml here
    defines the matching group too."""
    local = Config(org_repo=ORG_REPO)
    github = FakeGitHub(
        org_yaml=(
            "admin_groups:\n"
            "  - infra-engineering\n"
            "groups:\n"
            "  - name: infra-engineering\n"
            "    okta_group: okta-infra-engineering\n"
        )
    )

    merged, org = load_org_config(local, github)

    assert org.wired is True
    assert merged.admin_groups == ["infra-engineering"]


def test_admin_groups_entry_with_no_matching_group_is_unwired_not_crash():
    """A dangling admin_groups entry (no groups entry with that name) fails
    Config's own validation on merge -- caught by load_org_config's existing
    ValidationError handling (test_bad_org_value_fails_config_validation_and_
    is_unwired above), the SAME containment as any other invalid org.yaml
    value: the room boots on its local defaults with an org-status surface
    that says broken, rather than crashing or booting half-configured with a
    resolver that can never answer for the group it was told to trust."""
    local = Config(org_repo=ORG_REPO)
    github = FakeGitHub(
        org_yaml=(
            "admin_groups:\n"
            "  - ghost-team\n"
        )
    )

    merged, org = load_org_config(local, github)

    # Not a crash: load_org_config returned normally.
    # Not silent half-config: the local config is untouched, not partially
    # merged with the dangling admin_groups value.
    assert merged is local
    assert merged.admin_groups == []
    assert org.wired is False
    assert org.repo == ORG_REPO
    assert org.error is not None


def test_groups_and_okta_domain_round_trip():
    """groups/okta_domain (access-control arc, Task B1) are org-scoped like
    every other feature-gating knob above — a client's kobo-config repo
    names its own directory group universe."""
    local = Config(org_repo=ORG_REPO)
    github = FakeGitHub(
        org_yaml=(
            "okta_domain: acme.okta.com\n"
            "groups:\n"
            "  - name: SRE\n"
            "    okta_group: okta-sre-group\n"
        )
    )

    merged, org = load_org_config(local, github)

    assert org.wired is True
    assert merged.okta_domain == "acme.okta.com"
    assert merged.groups == [GroupChoice(name="SRE", okta_group="okta-sre-group")]
    assert isinstance(merged.groups[0], GroupChoice)
