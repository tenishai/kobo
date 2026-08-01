from pathlib import Path

from cortex_api.config import Config


def test_load_none_returns_defaults_without_touching_disk():
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


def test_workdir_expands_tilde(tmp_path, monkeypatch):
    yaml_path = tmp_path / "config.yaml"
    yaml_path.write_text("workdir: ~/.cortex\n")

    config = Config.load(yaml_path)

    assert config.workdir == Path.home() / ".cortex"


def test_skills_repo_path_expands_tilde(tmp_path, monkeypatch):
    yaml_path = tmp_path / "config.yaml"
    yaml_path.write_text("skills_repo_path: ~/.cortex/skills\n")

    config = Config.load(yaml_path)

    assert config.skills_repo_path == Path.home() / ".cortex/skills"


def test_personas_dir_expands_tilde(tmp_path, monkeypatch):
    yaml_path = tmp_path / "config.yaml"
    yaml_path.write_text("personas_dir: ~/personas\n")

    config = Config.load(yaml_path)

    assert config.personas_dir == Path.home() / "personas"


def test_cortex_config_env_var_missing_raises_error(tmp_path, monkeypatch):
    missing_path = tmp_path / "does-not-exist.yaml"
    monkeypatch.setenv("CORTEX_CONFIG", str(missing_path))

    try:
        Config.load()
        assert False, "Should have raised FileNotFoundError"
    except FileNotFoundError as e:
        assert str(missing_path) in str(e)


def test_config_local_yaml_missing_falls_back_to_defaults(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("CORTEX_CONFIG", raising=False)

    config = Config.load()

    assert config.github_org == "example-org"
    assert config.workdir == Path.home() / ".cortex"
