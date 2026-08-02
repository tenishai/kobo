import subprocess
from pathlib import Path

import pytest

from cortex_api.catalog import (
    NAME_RE,
    PersonaInfo,
    SkillInfo,
    list_personas,
    list_skills,
)
from cortex_api.models import Agent


def _git(repo: Path, *args: str) -> None:
    subprocess.run(
        ["git", "-c", "user.email=test@example.com", "-c", "user.name=Test", *args],
        cwd=repo,
        check=True,
        capture_output=True,
    )


def _init_git_repo(repo: Path) -> str:
    repo.mkdir(parents=True, exist_ok=True)
    _git(repo, "init", "-q")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "init")
    result = subprocess.run(
        ["git", "-C", str(repo), "rev-parse", "HEAD"],
        check=True,
        capture_output=True,
        text=True,
    )
    return result.stdout.strip()


# --- personas ---------------------------------------------------------------


def test_list_personas_from_package_templates_has_two_starters():
    personas = list_personas(None)

    ids = {p.id for p in personas}
    assert ids == {"helpful-generalist", "blank"}
    for persona in personas:
        assert isinstance(persona, PersonaInfo)
        assert persona.description
        assert "{name}" in persona.body


def test_list_personas_helpful_generalist_description_parsed():
    personas = {p.id: p for p in list_personas(None)}

    assert personas["helpful-generalist"].description == (
        "A friendly, concise generalist assistant."
    )
    assert personas["blank"].description == "Start from scratch."


def test_list_personas_custom_dir(tmp_path):
    persona_file = tmp_path / "custom.md"
    persona_file.write_text("<!-- description: Custom one. -->\nYou are {name}, custom.\n")

    personas = list_personas(tmp_path)

    assert len(personas) == 1
    assert personas[0].id == "custom"
    assert personas[0].description == "Custom one."
    assert "{name}" in personas[0].body


def test_list_personas_missing_dir_returns_empty(tmp_path):
    missing = tmp_path / "does-not-exist"

    assert list_personas(missing) == []


def test_list_personas_skips_zero_byte_file(tmp_path, caplog):
    persona_file = tmp_path / "empty.md"
    persona_file.write_text("")
    valid_file = tmp_path / "valid.md"
    valid_file.write_text("<!-- description: Valid one. -->\nYou are {name}.\n")

    with caplog.at_level("WARNING"):
        personas = list_personas(tmp_path)

    assert len(personas) == 1
    assert personas[0].id == "valid"
    assert any("empty" in message for message in caplog.messages)


# --- skills ------------------------------------------------------------------


def test_list_skills_none_returns_empty_list():
    assert list_skills(None) == []


def test_list_skills_scans_fixture_repo(tmp_path):
    repo = tmp_path / "skills-repo"
    skill_dir = repo / "skills" / "echo"
    skill_dir.mkdir(parents=True)
    (skill_dir / "SKILL.md").write_text(
        "---\nname: echo\ndescription: Echoes things.\n---\nEcho skill body.\n"
    )
    sha = _init_git_repo(repo)

    skills = list_skills(repo)

    assert len(skills) == 1
    skill = skills[0]
    assert isinstance(skill, SkillInfo)
    assert skill.id == "echo"
    assert skill.description == "Echoes things."
    assert skill.path == skill_dir / "SKILL.md"
    assert skill.sha == sha


def test_list_skills_no_skills_dir_returns_empty(tmp_path):
    repo = tmp_path / "empty-repo"
    repo.mkdir(parents=True)
    (repo / "README.md").write_text("Nothing to see here.\n")
    _init_git_repo(repo)

    assert list_skills(repo) == []


def test_list_skills_skips_malformed_entries(tmp_path, caplog):
    repo = tmp_path / "skills-repo"
    good_dir = repo / "skills" / "echo"
    good_dir.mkdir(parents=True)
    (good_dir / "SKILL.md").write_text(
        "---\nname: echo\ndescription: Echoes things.\n---\nEcho skill body.\n"
    )
    bad_dir = repo / "skills" / "broken"
    bad_dir.mkdir(parents=True)
    (bad_dir / "SKILL.md").write_text("no frontmatter fences here\n")
    _init_git_repo(repo)

    with caplog.at_level("WARNING"):
        skills = list_skills(repo)

    assert [s.id for s in skills] == ["echo"]
    assert any("broken" in message for message in caplog.messages)


def test_list_skills_skips_path_traversal_name(tmp_path, caplog):
    # `name:` flows verbatim into a copytree destination (skills_dir /
    # skill.id) and the agent card -- a malicious/typo'd `../../x` must be
    # rejected by the same NAME_RE the rest of the catalog enforces, not
    # taken on faith.
    repo = tmp_path / "skills-repo"
    good_dir = repo / "skills" / "echo"
    good_dir.mkdir(parents=True)
    (good_dir / "SKILL.md").write_text(
        "---\nname: echo\ndescription: Echoes things.\n---\nEcho skill body.\n"
    )
    escape_dir = repo / "skills" / "escape-attempt"
    escape_dir.mkdir(parents=True)
    (escape_dir / "SKILL.md").write_text(
        "---\nname: ../escape\ndescription: Sneaky.\n---\nBody.\n"
    )
    _init_git_repo(repo)

    with caplog.at_level("WARNING"):
        skills = list_skills(repo)

    assert [s.id for s in skills] == ["echo"]
    assert any("escape-attempt" in message for message in caplog.messages)


@pytest.mark.parametrize(
    "skill_name,frontmatter_content,description",
    [
        ("unclosed-fence", "---\nname: unclosed\ndescription: Incomplete\n", "unclosed fence"),
        (
            "invalid-yaml",
            "---\nname: invalid\n: : :\n---\nBody text\n",
            "invalid YAML",
        ),
        ("non-dict", "---\n- item1\n- item2\n---\nBody text\n", "non-dict YAML"),
        ("missing-name", "---\ndescription: Missing name\n---\nBody text\n", "missing name"),
    ],
)
def test_list_skills_skips_malformed_entries_parametrized(tmp_path, caplog, skill_name, frontmatter_content, description):
    repo = tmp_path / "skills-repo"
    good_dir = repo / "skills" / "good"
    good_dir.mkdir(parents=True)
    (good_dir / "SKILL.md").write_text(
        "---\nname: good\ndescription: Good skill.\n---\nBody.\n"
    )
    bad_dir = repo / "skills" / skill_name
    bad_dir.mkdir(parents=True)
    (bad_dir / "SKILL.md").write_text(frontmatter_content)
    _init_git_repo(repo)

    with caplog.at_level("WARNING"):
        skills = list_skills(repo)

    assert [s.id for s in skills] == ["good"], f"Failed for {description}"
    assert any(skill_name in message for message in caplog.messages)


# --- NAME_RE -----------------------------------------------------------------


@pytest.mark.parametrize(
    "name,expected",
    [
        ("echo", True),
        ("a1", True),
        ("valid-name", True),
        ("A", False),
        ("1abc", False),
        ("ab", True),
        ("a", False),
        ("has_underscore", False),
        ("Has-Caps", False),
        ("a" * 30, True),
        ("a" * 31, False),
    ],
)
def test_name_re(name, expected):
    assert bool(NAME_RE.fullmatch(name)) == expected


# --- availability route -------------------------------------------------------


def test_availability_invalid_name(client):
    response = client.get("/api/availability", params={"name": "Not_Valid"})

    assert response.status_code == 200
    assert response.json() == {"available": False, "reason": "invalid name"}


def test_availability_fresh_name(client):
    response = client.get("/api/availability", params={"name": "brand-new"})

    assert response.status_code == 200
    assert response.json() == {"available": True, "reason": None}


def test_availability_name_taken(client, session_factory):
    session = session_factory()
    session.add(
        Agent(
            name="taken-name",
            persona="helpful-generalist",
            model_id="vertex_ai/claude-sonnet-4-5",
        )
    )
    session.commit()
    session.close()

    response = client.get("/api/availability", params={"name": "taken-name"})

    assert response.status_code == 200
    assert response.json() == {"available": False, "reason": "name taken"}


def test_availability_invalid_name_with_trailing_newline(client):
    response = client.get("/api/availability", params={"name": "ab\n"})

    assert response.status_code == 200
    assert response.json() == {"available": False, "reason": "invalid name"}


# --- catalog routes ------------------------------------------------------------


def test_catalog_personas_route(client):
    response = client.get("/api/catalog/personas")

    assert response.status_code == 200
    body = response.json()
    ids = {p["id"] for p in body["personas"]}
    assert ids == {"helpful-generalist", "blank"}
    for persona in body["personas"]:
        assert set(persona.keys()) == {"id", "description"}


def test_catalog_skills_route_empty_when_no_skills_repo(client):
    response = client.get("/api/catalog/skills")

    assert response.status_code == 200
    assert response.json() == {"skills": []}


def test_catalog_skills_route_with_fixture_repo(tmp_path, session_factory):
    from fastapi.testclient import TestClient

    from cortex_api.config import Config
    from cortex_api.main import create_app

    repo = tmp_path / "skills-repo"
    skill_dir = repo / "skills" / "echo"
    skill_dir.mkdir(parents=True)
    (skill_dir / "SKILL.md").write_text(
        "---\nname: echo\ndescription: Echoes things.\n---\nEcho skill body.\n"
    )
    sha = _init_git_repo(repo)

    config = Config(workdir=tmp_path / "work", skills_repo_path=repo)
    local_client = TestClient(create_app(session_factory, config=config))

    response = local_client.get("/api/catalog/skills")

    assert response.status_code == 200
    assert response.json() == {
        "skills": [{"id": "echo", "description": "Echoes things.", "sha": sha}]
    }
