import re
from pathlib import Path

import pytest
import yaml
from pydantic import ValidationError

from cortex_api.config import Config


def test_load_none_returns_defaults_without_touching_disk(tmp_path, monkeypatch):
    # A real config.local.yaml can legitimately exist in a developer's cwd
    # (it's gitignored precisely so it can hold real deploy values) — chdir
    # to an empty tmp dir so this test's "no file, pure defaults" claim holds
    # regardless of what's sitting in whatever directory the suite happens
    # to run from.
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("CORTEX_CONFIG", raising=False)

    config = Config.load(None)

    assert config.github_org == "example-org"
    assert config.model_group == "models@example.com"
    assert config.workdir == Path.home() / ".cortex"


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
    yaml_path.write_text("workdir: ~/.cortex\n")

    config = Config.load(yaml_path)

    assert config.workdir == Path.home() / ".cortex"


def test_skills_repo_path_expands_tilde(tmp_path):
    yaml_path = tmp_path / "config.yaml"
    yaml_path.write_text("skills_repo_path: ~/.cortex/skills\n")

    config = Config.load(yaml_path)

    assert config.skills_repo_path == Path.home() / ".cortex/skills"


def test_personas_dir_expands_tilde(tmp_path):
    yaml_path = tmp_path / "config.yaml"
    yaml_path.write_text("personas_dir: ~/personas\n")

    config = Config.load(yaml_path)

    assert config.personas_dir == Path.home() / "personas"


def test_cortex_config_env_var_missing_raises_error(tmp_path, monkeypatch):
    missing_path = tmp_path / "does-not-exist.yaml"
    monkeypatch.setenv("CORTEX_CONFIG", str(missing_path))

    with pytest.raises(FileNotFoundError, match=re.escape(str(missing_path))):
        Config.load()


def test_config_local_yaml_missing_falls_back_to_defaults(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("CORTEX_CONFIG", raising=False)

    config = Config.load()

    assert config.github_org == "example-org"
    assert config.workdir == Path.home() / ".cortex"


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
