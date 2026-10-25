"""The directory delivery backend (issue #207): compose into a folder, tell
the running set, destroy only the folder and the listing."""

from __future__ import annotations

import json

import httpx
import pytest

from kobo_api.config import Config
from kobo_api.destroy import (
    DESTROY_STEPS,
    DIRECTORY_DESTROY_STEPS,
    destroy_steps_for,
    run_destroy,
)
from kobo_api.kinds import KIND_COMPOSED, KIND_DIRECTORY, is_directory, owns_repo
from kobo_api.models import Agent, AgentStatus
from kobo_api.provisioner import (
    CONNECTED_PIPELINE,
    DIRECTORY_PIPELINE,
    PIPELINE,
    STEP_LABELS,
    pipeline_for,
)
from kobo_api.steps import READY_MARKER, ComposeDirectoryStep, LoadStep, agent_directory
from tests.fakes import FakeGitHub, FakeGojira
from tests.test_destroy import _clients as destroy_clients
from tests.test_destroy import _seed_agent
from tests.test_steps import _clients, _make_agent, _make_ctx


def _directory_config(config: Config, tmp_path, **more) -> Config:
    return config.model_copy(update={
        "delivery_backend": "directory", "directory_root": str(tmp_path / "agents"), **more,
    })


def test_the_kind_picks_the_pipeline_and_the_destroy_stages():
    composed = Agent(name="a", persona="p", model_id="m", kind=KIND_COMPOSED)
    folder = Agent(name="b", persona="p", model_id="m", kind=KIND_DIRECTORY)
    assert pipeline_for(composed) is PIPELINE
    assert pipeline_for(folder) is DIRECTORY_PIPELINE
    assert [s.name for s in DIRECTORY_PIPELINE] == ["compose", "avatar", "directory", "load"]
    assert destroy_steps_for(composed) == DESTROY_STEPS
    assert destroy_steps_for(folder) == DIRECTORY_DESTROY_STEPS == ("destroy-files", "destroy-directory", "destroy-forget")
    assert is_directory(folder) and not owns_repo(folder) and owns_repo(composed)
    assert {"compose", "load"} <= set(STEP_LABELS)
    assert "hello" not in {s.name for s in DIRECTORY_PIPELINE} and "repo" not in {s.name for s in CONNECTED_PIPELINE}


def test_compose_writes_a_complete_folder_and_a_retry_replaces_it(session_factory, config, fake_storage, tmp_path):
    cfg = _directory_config(config, tmp_path)
    session, agent = _make_agent(session_factory)
    agent.kind = KIND_DIRECTORY
    session.commit()
    ctx = _make_ctx(session, agent, cfg, _clients(storage=fake_storage))

    detail = ComposeDirectoryStep().run(ctx)

    target = agent_directory(cfg, agent.name)
    assert detail == f"composed into {target}"
    assert (target / "bundle" / "agent.yaml").is_file()
    assert (target / "bundle" / "persona.md").is_file()
    card = json.loads((target / "bundle" / "agent-card.json").read_text())
    assert card["name"] == agent.name
    assert not (target.parent / f".{agent.name}.incoming").exists()
    (target / "stale.txt").write_text("old")
    ComposeDirectoryStep().run(ctx)
    assert not (target / "stale.txt").exists()
    assert (target / "bundle" / "agent.yaml").is_file()
    assert not (target.parent / f".{agent.name}.previous").exists()


def test_load_writes_the_marker_without_a_url_and_posts_with_one(session_factory, config, tmp_path, monkeypatch):
    cfg = _directory_config(config, tmp_path)
    session, agent = _make_agent(session_factory)
    target = agent_directory(cfg, agent.name)
    target.mkdir(parents=True)
    ctx = _make_ctx(session, agent, cfg, _clients())

    assert "marker" in LoadStep().run(ctx)
    assert (target / READY_MARKER).is_file()

    posted = {}

    def fake_post(url, *, json, timeout):
        posted.update({"url": url, "json": json})
        return httpx.Response(202, request=httpx.Request("POST", url))

    monkeypatch.setattr(httpx, "post", fake_post)
    with_url = cfg.model_copy(update={"directory_reload_url": "http://127.0.0.1:7777/reload"})
    ctx = _make_ctx(session, agent, with_url, _clients())
    assert LoadStep().run(ctx) == f"running set told to load {agent.name}"
    assert posted == {"url": "http://127.0.0.1:7777/reload", "json": {"name": agent.name, "path": str(target)}}

    def refuse(url, *, json, timeout):
        return httpx.Response(500, text="no", request=httpx.Request("POST", url))

    monkeypatch.setattr(httpx, "post", refuse)
    with pytest.raises(RuntimeError, match="refused the reload"):
        LoadStep().run(ctx)


def test_destroy_for_a_folder_agent_removes_the_folder_and_touches_no_platform(session_factory, config, tmp_path):
    cfg = _directory_config(config, tmp_path)
    folder = tmp_path / "agents" / "clio"
    (folder / "bundle").mkdir(parents=True)
    (folder / "bundle" / "agent.yaml").write_text("name: clio\n")
    agent_id = _seed_agent(session_factory, status=AgentStatus.READY, kind=KIND_DIRECTORY)
    github, gojira = FakeGitHub(), FakeGojira()

    thread = run_destroy(session_factory, agent_id, destroy_clients(github=github, gojira=gojira), cfg)
    thread.join(timeout=5)

    assert not folder.exists()
    assert gojira.delete_calls == [] and github.rename_calls == []
    session = session_factory()
    assert session.get(Agent, agent_id) is None
    session.close()


def test_config_requires_a_root_for_the_directory_backend():
    with pytest.raises(ValueError, match="directory_root"):
        Config(delivery_backend="directory")
    with pytest.raises(ValueError, match="deployed"):
        Config(delivery_backend="cloud")
    assert Config(delivery_backend="directory", directory_root="/tmp/agents").delivery_backend == "directory"
