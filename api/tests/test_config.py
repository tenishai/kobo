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
