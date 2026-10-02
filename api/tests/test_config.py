import re
from pathlib import Path

import pytest
import yaml
from pydantic import ValidationError

from kobo_api.config import (
    SHARED_GROUP,
    Config,
    GroupChoice,
    MCPConfig,
    ModelChoice,
    SkillsFrontmatterConfig,
    vertex_location_for,
)


def test_load_none_returns_defaults_without_touching_disk(tmp_path, monkeypatch):
    # A real config.local.yaml can legitimately exist in a developer's cwd
    # (it's gitignored precisely so it can hold real deploy values) — chdir
    # to an empty tmp dir so this test's "no file, pure defaults" claim holds
    # regardless of what's sitting in whatever directory the suite happens
    # to run from.
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("KOBO_CONFIG", raising=False)

    config = Config.load(None)

    assert config.github_org == "example-org"
    assert config.model_group == ""  # no group: the Vertex role is granted directly
    assert config.workdir == Path.home() / ".kobo"


def test_load_tmp_yaml_overrides_github_org_and_coerces_workdir(tmp_path):
    yaml_path = tmp_path / "config.yaml"
    yaml_path.write_text(
        "github_org: acme-corp\n"
        f"workdir: {tmp_path}/custom-workdir\n"
    )

    config = Config.load(yaml_path)

    assert config.github_org == "acme-corp"
    assert config.workdir == tmp_path / "custom-workdir"
    assert isinstance(config.workdir, Path)


def test_workdir_expands_tilde(tmp_path):
    yaml_path = tmp_path / "config.yaml"
    yaml_path.write_text("workdir: ~/.kobo\n")

    config = Config.load(yaml_path)

    assert config.workdir == Path.home() / ".kobo"


def test_kobo_config_env_var_missing_raises_error(tmp_path, monkeypatch):
    missing_path = tmp_path / "does-not-exist.yaml"
    monkeypatch.setenv("KOBO_CONFIG", str(missing_path))

    with pytest.raises(FileNotFoundError, match=re.escape(str(missing_path))):
        Config.load()


# --- models: min_length=1 ------------------------------------------------------


def test_models_empty_list_raises_validation_error():
    with pytest.raises(ValidationError):
        Config(models=[])


# --- Config.load: non-dict YAML -------------------------------------------------


def test_load_non_dict_yaml_raises_value_error_naming_the_file(tmp_path):
    yaml_path = tmp_path / "config.yaml"
    yaml_path.write_text("- item1\n- item2\n")

    with pytest.raises(ValueError, match=re.escape(str(yaml_path))):
        Config.load(yaml_path)


def test_load_empty_yaml_file_falls_back_to_defaults(tmp_path):
    """An empty (or all-comments) file parses to `None`, not a non-dict — that
    still means "no overrides", same as a missing file, not an error."""
    yaml_path = tmp_path / "config.yaml"
    yaml_path.write_text("# just a comment\n")

    config = Config.load(yaml_path)

    assert config.github_org == "example-org"


# --- slack_tokens_path -----------------------------------------------------


# --- slack_tokens_secret (two-replica safety) ------------------------------


def test_slack_tokens_secret_accepts_namespace_slash_name(tmp_path):
    yaml_path = tmp_path / "config.yaml"
    yaml_path.write_text("slack_tokens_secret: kobo/kobo-slack-tokens\n")

    config = Config.load(yaml_path)

    assert config.slack_tokens_secret == "kobo/kobo-slack-tokens"


@pytest.mark.parametrize(
    "value",
    ["kobo-slack-tokens", "kobo/", "/kobo-slack-tokens", "kobo/ns/name", "kobo / name"],
)
def test_slack_tokens_secret_rejects_anything_but_namespace_slash_name(value):
    with pytest.raises(ValidationError) as exc_info:
        Config(slack_tokens_secret=value)

    assert "slack_tokens_secret" in str(exc_info.value)


def test_slack_tokens_secret_is_not_org_settable():
    # Deployment-local, exactly like slack_tokens_path: it names a Secret in
    # THIS cluster's namespace, not anything the org hands out.
    from kobo_api.org import ORG_KEYS

    assert "slack_tokens_secret" not in ORG_KEYS
    assert "slack_tokens_path" not in ORG_KEYS


# --- ladon_* (the secrets platform's machine lane) -------------------------


def test_ladon_settings_default_empty_and_the_lane_is_off():
    config = Config()

    assert config.ladon_url == ""
    assert config.ladon_audience == ""
    assert config.ladon_namespace == ""
    assert config.ladon_enabled is False


def test_ladon_settings_load_from_yaml(tmp_path):
    yaml_path = tmp_path / "config.yaml"
    yaml_path.write_text(
        "ladon_url: http://secrets.example\n"
        "ladon_audience: http://secrets.example\n"
        "ladon_namespace: platform\n"
    )

    config = Config.load(yaml_path)

    assert config.ladon_url == "http://secrets.example"
    assert config.ladon_audience == "http://secrets.example"
    assert config.ladon_namespace == "platform"
    assert config.ladon_enabled is True


@pytest.mark.parametrize(
    "settings",
    [
        {"ladon_url": "http://secrets.example"},
        {"ladon_url": "http://secrets.example", "ladon_audience": "aud"},
        {"ladon_url": "http://secrets.example", "ladon_namespace": "platform"},
        {"ladon_audience": "aud", "ladon_namespace": "platform"},
    ],
)
def test_ladon_is_off_until_all_three_settings_are_present(settings):
    # A half-configured lane reads as off, never as half-on: a URL with no
    # audience could never authenticate, and one with no namespace has
    # nowhere to write.
    assert Config(**settings).ladon_enabled is False


# --- avatar_model / avatar_location (P2d: ImageClient) --------------------


# --- name_model / name_location (create flow v2 addendum: NameClient) -----


def test_org_config_fields_overridable(tmp_path):
    yaml_path = tmp_path / "config.yaml"
    yaml_path.write_text(
        "org_repo: acme-corp/kobo-config\n"
        "api_base_url: https://kobo.acme.example\n"
        "skills_repo: acme-corp/kobo-skills\n"
        "skills_bucket: acme-skills-bucket\n"
        "personas_bucket: acme-personas-bucket\n"
        "skills_publish_wif_provider: projects/123/locations/global/workloadIdentityPools/x\n"
        "skills_publish_service_account: publisher@acme.iam.gserviceaccount.com\n"
        "skills_publish_external: true\n"
    )

    config = Config.load(yaml_path)

    assert config.org_repo == "acme-corp/kobo-config"
    assert config.api_base_url == "https://kobo.acme.example"
    assert config.skills_repo == "acme-corp/kobo-skills"
    assert config.skills_bucket == "acme-skills-bucket"
    assert config.personas_bucket == "acme-personas-bucket"
    assert config.skills_publish_wif_provider == (
        "projects/123/locations/global/workloadIdentityPools/x"
    )
    assert config.skills_publish_service_account == "publisher@acme.iam.gserviceaccount.com"
    assert config.skills_publish_external is True


def test_config_example_yaml_documents_org_config_keys():
    example_path = Path(__file__).resolve().parent.parent / "config.example.yaml"
    data = yaml.safe_load(example_path.read_text())

    assert data["org_repo"] is None
    assert data["api_base_url"] == "http://127.0.0.1:8100"
    assert data["skills_repo"] is None
    assert data["skills_bucket"] == ""
    assert data["personas_bucket"] == ""
    assert data["skills_publish_wif_provider"] == ""
    assert data["skills_publish_service_account"] == ""
    assert data["skills_publish_external"] is False

    config = Config(**data)
    assert config.org_repo is None
    assert config.api_base_url == "http://127.0.0.1:8100"


# --- cabal_url / agent_endpoint_template (colleagues switchboard arc, Task 1) ---


# --- ModelChoice.location / vertex_location_for (Gemini-3.1-Pro-Preview arc) --
#
# Gemini previews serve ONLY on Vertex's global endpoint (a regional endpoint
# 404s), while the Claude partner models stay on the org's regional default
# (vertex_location). ModelChoice.location is the per-model override that
# lets a single models list carry both kinds; vertex_location_for is the
# lookup that resolves it at compose time (tests/test_steps.py exercises the
# compose-level wiring, RepoStep threading it into BundleSpec).


def test_model_choice_parses_explicit_location():
    model = ModelChoice(
        id="vertex_ai/gemini-3.1-pro-preview",
        label="Gemini 3.1 Pro Preview",
        location="global",
    )
    assert model.location == "global"


def test_vertex_location_for_returns_model_location_when_set():
    config = Config(
        vertex_location="us-east5",
        models=[
            ModelChoice(id="vertex_ai/claude-sonnet-4-5", label="Claude Sonnet 4.5"),
            ModelChoice(
                id="vertex_ai/gemini-3.1-pro-preview",
                label="Gemini 3.1 Pro Preview",
                location="global",
            ),
        ],
    )

    assert vertex_location_for(config, "vertex_ai/gemini-3.1-pro-preview") == "global"


def test_vertex_location_for_falls_back_to_org_default_when_model_has_no_location():
    config = Config(
        vertex_location="us-east5",
        models=[ModelChoice(id="vertex_ai/claude-sonnet-4-5", label="Claude Sonnet 4.5")],
    )

    assert vertex_location_for(config, "vertex_ai/claude-sonnet-4-5") == "us-east5"


def test_vertex_location_for_unknown_model_id_falls_back_to_org_default():
    """An agent created under an older `models` list whose model has since
    been retired from the catalog must still resolve a location, not
    raise — same 'still render, not 500' stance as main.py's _model_label."""
    config = Config(vertex_location="us-east5")

    assert vertex_location_for(config, "vertex_ai/retired-model") == "us-east5"


# --- admin_groups (operator-status-by-group ruling) --------------------------


def test_admin_groups_overridable(tmp_path):
    yaml_path = tmp_path / "config.yaml"
    yaml_path.write_text(
        "admin_groups:\n"
        "  - infra-engineering\n"
        "groups:\n"
        "  - name: infra-engineering\n"
        "    okta_group: okta-infra-engineering\n"
    )

    config = Config.load(yaml_path)

    assert config.admin_groups == ["infra-engineering"]


# --- admin_groups must exist in the groups universe (HIGH finding, fix round 1) --


def test_admin_groups_entry_with_no_matching_groups_name_raises_naming_it():
    with pytest.raises(ValidationError) as exc_info:
        Config(
            admin_groups=["ghost-team"],
            groups=[GroupChoice(name="infra-engineering", okta_group="okta-infra-engineering")],
        )

    message = str(exc_info.value)
    assert "ghost-team" in message


# --- SHARED_GROUP (shared-group + group-requests wave) -----------------------


def test_a_real_group_may_not_be_named_shared():
    with pytest.raises(ValidationError) as exc_info:
        Config(groups=[GroupChoice(name=SHARED_GROUP, okta_group="okta-shared")])

    assert "shared" in str(exc_info.value).lower()


# --- groups / okta_domain (access-control arc, Task B1: GroupResolver) -------


def test_groups_and_okta_domain_overridable(tmp_path):
    yaml_path = tmp_path / "config.yaml"
    yaml_path.write_text(
        "okta_domain: acme.okta.com\n"
        "groups:\n"
        "  - name: SRE\n"
        "    okta_group: okta-sre-group\n"
        "  - name: QA\n"
        "    okta_group: okta-qa-group\n"
    )

    config = Config.load(yaml_path)

    assert config.okta_domain == "acme.okta.com"
    assert config.groups == [
        GroupChoice(name="SRE", okta_group="okta-sre-group"),
        GroupChoice(name="QA", okta_group="okta-qa-group"),
    ]
    assert isinstance(config.groups[0], GroupChoice)


# --- GroupChoice okta_group/okta_groups sugar (multi-okta-groups amendment) --


def test_group_choice_okta_group_sugar_folds_into_okta_groups():
    """okta_group is 1:1 sugar for the common single-group case -- setting
    only it must still populate okta_groups (what the resolver actually
    consumes)."""
    group = GroupChoice(name="SRE", okta_group="okta-sre-group")

    assert group.okta_group == "okta-sre-group"
    assert group.okta_groups == ["okta-sre-group"]


def test_group_choice_okta_groups_list_form():
    """The directory splits one human team across regional groups -- the
    general case is a list, no okta_group sugar involved at all."""
    group = GroupChoice(
        name="infra-engineering",
        okta_groups=[
            "engineering_operations_infra-engineering_usa",
            "engineering_operations_infra-engineering_india",
            "engineering_operations_infra-engineering_france",
            "engineering_operations_infra-engineering_lead",
        ],
    )

    assert group.okta_groups == [
        "engineering_operations_infra-engineering_usa",
        "engineering_operations_infra-engineering_india",
        "engineering_operations_infra-engineering_france",
        "engineering_operations_infra-engineering_lead",
    ]


def test_group_choice_both_set_unions_and_dedupes():
    group = GroupChoice(
        name="SRE",
        okta_group="okta-sre-usa",
        okta_groups=["okta-sre-india", "okta-sre-usa"],
    )

    # okta_group folded in, but not duplicated -- it was already present in
    # okta_groups.
    assert group.okta_groups == ["okta-sre-india", "okta-sre-usa"]


def test_group_choice_requires_at_least_one_okta_field_set():
    with pytest.raises(ValidationError) as exc_info:
        GroupChoice(name="SRE")

    message = str(exc_info.value)
    assert "okta_group" in message


# --- GroupChoice.kind (create-modal non-engineering-wing ruling) -------------


def test_group_choice_kind_defaults_to_engineering():
    # Every group configured before this field existed must keep rendering
    # in the same dropdown section it always has -- "engineering" is the
    # default, not a forced choice.
    group = GroupChoice(name="SRE", okta_group="okta-sre-group")

    assert group.kind == "engineering"


def test_group_choice_kind_rejects_unknown_value():
    with pytest.raises(ValidationError) as exc_info:
        GroupChoice(name="SRE", okta_group="okta-sre-group", kind="shared")

    message = str(exc_info.value)
    assert "shared" in message


# --- mcp (MCP server arc, Task 1) --------------------------------------------


def test_mcp_config_defaults_and_disabled():
    config = Config()
    assert config.mcp.oauth_client_id == ""
    assert config.mcp.oauth_client_secret_env == "KOBO_MCP_GOOGLE_CLIENT_SECRET"
    assert config.mcp.workspace_domain == ""
    assert config.mcp.jwt_key_env == "KOBO_MCP_JWT_KEY"
    assert config.mcp_enabled is False


def test_mcp_config_overridable_via_yaml(tmp_path):
    yaml_path = tmp_path / "config.yaml"
    yaml_path.write_text(
        "mcp:\n"
        "  oauth_client_id: some-client-id.apps.example.com\n"
        "  workspace_domain: example.com\n"
        "  issuer_base_url: https://kobo.example.org\n"
    )

    config = Config.load(yaml_path)

    assert config.mcp.oauth_client_id == "some-client-id.apps.example.com"
    assert config.mcp.workspace_domain == "example.com"
    assert config.mcp.issuer_base_url == "https://kobo.example.org"
    # Fields left out of the overlay still fall back to MCPConfig's own
    # defaults, same as every other overlay test in this file.
    assert config.mcp.oauth_client_secret_env == "KOBO_MCP_GOOGLE_CLIENT_SECRET"
    assert config.mcp.jwt_key_env == "KOBO_MCP_JWT_KEY"


def test_mcp_enabled_true_when_oauth_client_id_set():
    config = Config(
        mcp=MCPConfig(
            oauth_client_id="some-client-id",
            workspace_domain="example.com",
            issuer_base_url="https://kobo.example.org",
        )
    )
    assert config.mcp_enabled is True


def test_mcp_enabled_requires_workspace_domain():
    """An AS with no domain check must refuse to boot, fail-closed -- same
    "fail loudly at construction" posture as `_admin_groups_must_be_
    configured_groups` above."""
    with pytest.raises(ValidationError) as exc_info:
        Config(mcp=MCPConfig(oauth_client_id="some-client-id"))

    message = str(exc_info.value)
    assert (
        "mcp.oauth_client_id is set but mcp.workspace_domain is empty; "
        "the domain check is the org gate on the MCP path"
    ) in message


# --- mcp.issuer_base_url must be set when enabled (fix round 1) --------------


def test_mcp_enabled_requires_issuer_base_url():
    """Same fail-closed posture as test_mcp_enabled_requires_workspace_domain
    above -- an enabled MCP server left with no issuer_base_url boots
    silently serving issuer:"" in the well-known documents (non-conformant
    for MCP clients) and falls back to trusting the incoming request's Host
    header to build the google redirect_uri
    (kobo_api.mcp.oauth_as._google_redirect_uri) -- fine for local
    dev/tests, not for a real deployment."""
    with pytest.raises(ValidationError) as exc_info:
        Config(
            mcp=MCPConfig(oauth_client_id="some-client-id", workspace_domain="example.com")
        )

    message = str(exc_info.value)
    assert "mcp.oauth_client_id is set but mcp.issuer_base_url is empty" in message


# --- mcp.issuer_base_url must be https:// (CYBER-1132 ride-along) -----------


def test_mcp_issuer_base_url_over_plain_http_is_rejected():
    """The well-known documents echo `issuer_base_url` verbatim into
    `issuer`, `authorization_endpoint`, etc -- an http:// value would be the
    one in-repo way left to advertise a plain-http OAuth surface, so
    construction fails loudly instead of booting with it."""
    with pytest.raises(ValidationError) as exc_info:
        Config(mcp=MCPConfig(issuer_base_url="http://example.com"))

    message = str(exc_info.value)
    assert (
        "mcp.issuer_base_url must be https:// -- the OAuth discovery "
        "documents echo it verbatim"
    ) in message


def test_mcp_issuer_base_url_over_https_constructs_fine():
    config = Config(mcp=MCPConfig(issuer_base_url="https://kobo.example.org"))

    assert config.mcp.issuer_base_url == "https://kobo.example.org"


def test_mcp_issuer_base_url_over_http_localhost_is_allowed():
    """Dev rooms run the MCP server and its OAuth AS locally over plain http
    with no load balancer in front to upgrade the scheme -- deliberately
    exempted, same as the `127.0.0.1` form below."""
    config = Config(mcp=MCPConfig(issuer_base_url="http://localhost:8000"))

    assert config.mcp.issuer_base_url == "http://localhost:8000"


# --- skills_frontmatter (hardening wave, Task 1) -----


def test_skills_frontmatter_accepts_vocabularies():
    cfg = Config(
        skills_frontmatter={
            "allowed_profiles": ["agent-sdk"],
            "allowed_tags": ["networking", "agent-ops"],
        }
    )
    assert cfg.skills_frontmatter.allowed_profiles == ["agent-sdk"]
    assert "agent-ops" in cfg.skills_frontmatter.allowed_tags
