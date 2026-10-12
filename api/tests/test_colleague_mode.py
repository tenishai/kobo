"""Contract: the owner's per-agent colleague mode (issue #131) decides which
colleague tools a composed bundle allows and which colleague doctrine it
carries, and `default` leaves every bundle exactly as it was before the
setting existed."""

from __future__ import annotations

import hashlib
from pathlib import Path

import yaml

from kobo_api.composer import (
    BundleSpec,
    compose,
    update_agent_yaml_colleague_mode,
    update_agent_yaml_tools,
    validate_agent_yaml,
)

ROSTER = [
    {"name": "ada", "team": "sre", "purpose_tags": ["gcp"], "capability": "Knows the GCP estate."}
]
CONSULT_FIRST = "consult that colleague with ask_colleague FIRST"
REPLACEMENT = "say you don't know, and name what you would need"
ASK = "colleagues.ask_colleague"


def _spec(*, switchboard: bool = True, **overrides) -> BundleSpec:
    base = {
        "name": "clio",
        "persona_body": "You are clio.\n",
        "model_id": "vertex_ai/claude-sonnet-4-5",
        "skills": [],
        "runtime_image": "registry.example/agents/daedalus-runtime:v1",
        "vertex_project": "p",
        "vertex_location": "us-east5",
    }
    if switchboard:
        base.update(
            cabal_url="https://cabal.example",
            broker_url="http://kobo.svc",
            liminal_url="https://liminal.example",
            colleagues=ROSTER,
            group="g",
            team="t",
            purpose_tags=["a"],
            capability="x",
        )
    base.update(overrides)
    return BundleSpec(**base)


def _compose(tmp_path: Path, spec: BundleSpec) -> Path:
    compose(spec, tmp_path)
    return tmp_path / "bundle"


def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def test_default_mode_composes_the_same_bytes_as_before_the_setting(tmp_path):
    """Acceptance for issue #131: `default` is today's output byte for byte.
    The digests were taken from origin/main 753a46f's composer for these
    exact specs. A deliberate change to the default bundle updates them;
    nothing in the colleague-mode work may."""
    on = _compose(tmp_path / "on", _spec(colleague_mode="default"))
    off = _compose(tmp_path / "off", _spec(switchboard=False, colleague_mode="default"))

    assert {
        "on/agent.yaml": _sha(on / "agent.yaml"),
        "on/colleagues.md": _sha(on / "knowledge" / "colleagues.md"),
        "off/agent.yaml": _sha(off / "agent.yaml"),
        "off/colleagues.md": _sha(off / "knowledge" / "colleagues.md"),
    } == {
        "on/agent.yaml": "2a46115a54bac2daafeaf1b326f83e73bbff66da2c67ffb160f08bb687c65b6e",
        "on/colleagues.md": "0fdc38a8b14e4a82014bb47681183c38f5012a5387dbe78b72a1e4eb69fb2438",
        "off/agent.yaml": "5f9e77b49c959a5233f2d619ab3899b4b2d36c6c1ef2879bf2df05394c152311",
        "off/colleagues.md": "4d177ec18d1c269e6b42aa1ba092c4cc083c1dc12720c1e1601cd64ba0cd26c5",
    }


def test_roster_only_drops_the_ask_tool_and_the_consult_first_doctrine(tmp_path):
    bundle = _compose(tmp_path, _spec(colleague_mode="roster-only"))

    validate_agent_yaml(bundle)
    allow = yaml.safe_load((bundle / "agent.yaml").read_text())["tools"]["allow"]
    assert ASK not in allow
    assert "colleagues.list_colleagues" in allow and "github.open_pr" in allow
    doc = (bundle / "knowledge" / "colleagues.md").read_text()
    assert "- ada (team: sre)" in doc
    assert REPLACEMENT in doc and "ask your human" in doc.lower()
    assert CONSULT_FIRST not in doc


def test_none_composes_no_colleague_tools_and_no_colleague_document(tmp_path):
    bundle = _compose(tmp_path, _spec(colleague_mode="none"))

    validate_agent_yaml(bundle)
    tools = yaml.safe_load((bundle / "agent.yaml").read_text())["tools"]
    assert not [t for t in tools["allow"] if t.startswith("colleagues.")]
    # The sidecars ride the same colleague pass and stay.
    assert "github.open_pr" in tools["allow"]
    assert not (bundle / "knowledge" / "colleagues.md").exists()


def test_switching_an_existing_bundle_edits_only_the_colleague_allows(tmp_path):
    bundle = _compose(tmp_path, _spec())
    before = yaml.safe_load((bundle / "agent.yaml").read_text())["tools"]

    assert update_agent_yaml_colleague_mode(bundle, mode="roster-only") is True
    after = yaml.safe_load((bundle / "agent.yaml").read_text())["tools"]
    assert after["allow"] == [t for t in before["allow"] if t != ASK]
    assert {k: v for k, v in after.items() if k != "allow"} == {
        k: v for k, v in before.items() if k != "allow"
    }

    update_agent_yaml_colleague_mode(bundle, mode="default")
    assert yaml.safe_load((bundle / "agent.yaml").read_text())["tools"]["allow"] == before["allow"]


def test_default_mode_never_rewrites_a_composed_agent_yaml(tmp_path):
    bundle = _compose(tmp_path, _spec())
    text = (bundle / "agent.yaml").read_text()

    assert update_agent_yaml_colleague_mode(bundle, mode="default") is False
    assert (bundle / "agent.yaml").read_text() == text


def test_a_rewire_leaves_a_roster_only_agent_without_its_ask_tool(tmp_path):
    """Neither the colleague allows a rewire adds nor the entries it keeps
    from the file may carry the ask tool for a roster-only agent."""
    bundle = _compose(tmp_path, _spec())

    update_agent_yaml_tools(
        bundle,
        url="https://cabal2.example",
        token_env="CABAL_COLLEAGUE_TOKEN",
        self_name="clio",
        colleague_mode="roster-only",
    )

    assert ASK not in yaml.safe_load((bundle / "agent.yaml").read_text())["tools"]["allow"]


def test_switching_works_on_a_slack_wired_block_style_agent_yaml(tmp_path):
    """Slack wiring re-dumps agent.yaml in block style (slack_setup.py), and
    most live agents are wired: the mode change must still land there."""
    bundle = _compose(tmp_path, _spec())
    path = bundle / "agent.yaml"
    path.write_text(yaml.safe_dump(yaml.safe_load(path.read_text()), sort_keys=False))
    before = yaml.safe_load(path.read_text())

    update_agent_yaml_colleague_mode(bundle, mode="roster-only")

    after = yaml.safe_load(path.read_text())
    assert ASK not in after["tools"]["allow"]
    assert {k: v for k, v in after.items() if k != "tools"} == {
        k: v for k, v in before.items() if k != "tools"
    }
    validate_agent_yaml(bundle)
