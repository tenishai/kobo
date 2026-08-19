import re
from pathlib import Path

import pytest
import yaml
from pydantic import ValidationError

from kobo_api.config import Config


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
    assert config.model_group == "models@example.com"
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


def test_config_local_yaml_missing_falls_back_to_defaults(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("KOBO_CONFIG", raising=False)

    config = Config.load()

    assert config.github_org == "example-org"
    assert config.workdir == Path.home() / ".kobo"


# --- models: min_length=1 ------------------------------------------------------


def test_models_empty_list_raises_validation_error():
    with pytest.raises(ValidationError):
        Config(models=[])


def test_models_default_is_nonempty():
    assert len(Config().models) >= 1


# --- Config.load: non-dict YAML -------------------------------------------------


def test_load_non_dict_yaml_raises_value_error_naming_the_file(tmp_path):
    yaml_path = tmp_path / "config.yaml"
    yaml_path.write_text("- item1\n- item2\n")

    with pytest.raises(ValueError, match=re.escape(str(yaml_path))):
        Config.load(yaml_path)


def test_load_scalar_yaml_raises_value_error(tmp_path):
    yaml_path = tmp_path / "config.yaml"
    yaml_path.write_text("just a string\n")

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


def test_slack_tokens_path_default_none():
    assert Config().slack_tokens_path is None


def test_slack_tokens_path_expands_tilde(tmp_path):
    yaml_path = tmp_path / "config.yaml"
    yaml_path.write_text("slack_tokens_path: ~/slack.local.yaml\n")

    config = Config.load(yaml_path)

    assert config.slack_tokens_path == Path.home() / "slack.local.yaml"


# --- avatar_model / avatar_location (P2d: ImageClient) --------------------


def test_avatar_model_and_location_defaults():
    config = Config()
    assert config.avatar_model == "gemini-2.5-flash-image"
    assert config.avatar_location == "us-central1"


def test_avatar_model_and_location_overridable(tmp_path):
    yaml_path = tmp_path / "config.yaml"
    yaml_path.write_text(
        "avatar_model: some-other-model\navatar_location: us-east4\n"
    )

    config = Config.load(yaml_path)

    assert config.avatar_model == "some-other-model"
    assert config.avatar_location == "us-east4"


# --- name_model / name_location (create flow v2 addendum: NameClient) -----


def test_name_model_and_location_defaults():
    config = Config()
    assert config.name_model == "gemini-2.5-flash"
    assert config.name_location == "us-central1"


def test_name_model_and_location_overridable(tmp_path):
    yaml_path = tmp_path / "config.yaml"
    yaml_path.write_text(
        "name_model: some-other-text-model\nname_location: us-east4\n"
    )

    config = Config.load(yaml_path)

    assert config.name_model == "some-other-text-model"
    assert config.name_location == "us-east4"


def test_config_example_yaml_documents_name_keys():
    # Same round-trip check as the avatar keys above: config.example.yaml is
    # the operator-facing template, so both new keys must be explicitly
    # present (not merely absent-and-defaulted) and must parse into a valid
    # Config matching the real defaults baked into Config.
    example_path = Path(__file__).resolve().parent.parent / "config.example.yaml"
    data = yaml.safe_load(example_path.read_text())

    assert data["name_model"] == "gemini-2.5-flash"
    assert data["name_location"] == "us-central1"

    config = Config(**data)
    assert config.name_model == "gemini-2.5-flash"
    assert config.name_location == "us-central1"


def test_org_config_field_defaults():
    config = Config()
    assert config.org_repo is None
    assert config.api_base_url == "http://127.0.0.1:8100"
    assert config.skills_repo is None
    assert config.skills_bucket == ""
    assert config.personas_bucket == ""
    assert config.skills_publish_wif_provider == ""
    assert config.skills_publish_service_account == ""
    assert config.skills_publish_external is False


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


def test_cabal_config_fields_default_none():
    config = Config()
    assert config.cabal_url is None
    assert config.agent_endpoint_template is None


def test_cabal_config_fields_overridable(tmp_path):
    yaml_path = tmp_path / "config.yaml"
    yaml_path.write_text(
        "cabal_url: http://cabal.internal\n"
        "agent_endpoint_template: http://agent-{name}.default.svc.cluster.local\n"
    )

    config = Config.load(yaml_path)

    assert config.cabal_url == "http://cabal.internal"
    assert config.agent_endpoint_template == "http://agent-{name}.default.svc.cluster.local"


def test_config_example_yaml_documents_cabal_keys():
    example_path = Path(__file__).resolve().parent.parent / "config.example.yaml"
    data = yaml.safe_load(example_path.read_text())

    assert data["cabal_url"] is None
    assert data["agent_endpoint_template"] is None

    config = Config(**data)
    assert config.cabal_url is None
    assert config.agent_endpoint_template is None


def test_config_example_yaml_documents_avatar_keys():
    # config.example.yaml is the operator-facing template; both new keys must
    # be explicitly present (not merely absent-and-defaulted) and must parse
    # into a valid Config matching the real defaults baked into Config.
    example_path = Path(__file__).resolve().parent.parent / "config.example.yaml"
    data = yaml.safe_load(example_path.read_text())

    assert data["avatar_model"] == "gemini-2.5-flash-image"
    assert data["avatar_location"] == "us-central1"

    config = Config(**data)
    assert config.avatar_model == "gemini-2.5-flash-image"
    assert config.avatar_location == "us-central1"
