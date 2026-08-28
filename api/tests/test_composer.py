import json
from dataclasses import fields
from pathlib import Path

import pytest
import yaml

from kobo_api.clients import ClientError
from kobo_api.composer import (
    CABAL_COLLEAGUE_TOKEN_ENV,
    BundleSpec,
    ComposeError,
    compose,
    update_agent_card_metadata,
    update_agent_yaml_approvals_gates,
    update_agent_yaml_auth,
    update_agent_yaml_max_tokens,
    update_agent_yaml_memory_revision,
    update_agent_yaml_tools,
    validate_agent_yaml,
)
from kobo_api.steps import StagedSkill

_RUNTIME_IMAGE = "registry.example/agents/daedalus-runtime:latest"


def _make_spec(
    skills: list[StagedSkill] | None = None,
    *,
    name: str = "sam",
    team: str = "shared",
    group: str | None = None,
    purpose_tags: list[str] | None = None,
    capability: str | None = None,
    colleagues: list[dict] | None = None,
    cabal_url: str | None = None,
    memory_replica_bucket: str = "",
    broker_url: str = "",
) -> BundleSpec:
    return BundleSpec(
        name=name,
        persona_body=f"You are {name}, a helpful assistant.\n",
        model_id="vertex_ai/claude-sonnet-4-5",
        skills=skills or [],
        runtime_image=_RUNTIME_IMAGE,
        vertex_project="example-model-project",
        vertex_location="us-east5",
        team=team,
        group=group,
        purpose_tags=purpose_tags if purpose_tags is not None else [],
        capability=capability,
        colleagues=colleagues if colleagues is not None else [],
        cabal_url=cabal_url,
        memory_replica_bucket=memory_replica_bucket,
        broker_url=broker_url,
    )


def _make_skill(tmp_path: Path, skill_id: str = "echo") -> StagedSkill:
    """A skill already "staged" on local disk — the shape `kobo_api.steps.
    _stage_skills` hands `compose()` after downloading it from the skills
    bucket. Composer tests build this directly rather than going through
    staging, since staging itself is `_stage_skills`'s own concern
    (`tests/test_steps.py`)."""
    skill_dir = tmp_path / "staged" / skill_id
    skill_dir.mkdir(parents=True)
    (skill_dir / "SKILL.md").write_text(
        f"---\nname: {skill_id}\ndescription: Echoes things.\n---\nEcho skill body.\n"
    )
    (skill_dir / "reference.py").write_text("def helper():\n    return 1\n")
    return StagedSkill(id=skill_id, dir=skill_dir)


# --- compose(): exact file set -----------------------------------------------


def test_compose_writes_exact_file_set_without_skills(tmp_path):
    dest = tmp_path / "out"

    compose(_make_spec(), dest)

    written = {p.relative_to(dest) for p in dest.rglob("*") if p.is_file()}
    assert written == {
        Path("Dockerfile"),
        Path("bundle/agent.yaml"),
        Path("bundle/agent-card.json"),
        Path("bundle/persona.md"),
        Path("bundle/knowledge/self.md"),
        Path("bundle/knowledge/colleagues.md"),
    }


def test_compose_writes_exact_file_set_with_skills(tmp_path):
    dest = tmp_path / "out"
    skill = _make_skill(tmp_path)

    compose(_make_spec(skills=[skill]), dest)

    written = {p.relative_to(dest) for p in dest.rglob("*") if p.is_file()}
    assert written == {
        Path("Dockerfile"),
        Path("bundle/agent.yaml"),
        Path("bundle/agent-card.json"),
        Path("bundle/persona.md"),
        Path("bundle/knowledge/self.md"),
        Path("bundle/knowledge/colleagues.md"),
        Path("bundle/skills/echo/SKILL.md"),
        Path("bundle/skills/echo/reference.py"),
    }


# --- agent.yaml shape ---------------------------------------------------------


def test_compose_agent_yaml_literal_placeholder_and_exact_text(tmp_path):
    dest = tmp_path / "out"

    compose(_make_spec(), dest)

    text = (dest / "bundle" / "agent.yaml").read_text()
    assert text == (
        "name: sam\n"
        "card: agent-card.json\n"
        "persona: persona.md\n"
        "model:\n"
        "  id: ${AGENT_MODEL:-vertex_ai/claude-sonnet-4-5}\n"
        "  max_tokens: 8192\n"
        "  budget: {per_task_tokens: 50000}\n"
        "transports:\n"
        "  - {kind: a2a, host: 0.0.0.0, port: 8080}\n"
        "tools: {provider: none, allow: []}\n"
        "memory: {personal: techne, home: /techne, revision: operator}\n"
        "checkpoints: sqlite\n"
        "approvals:\n"
        "  ceiling: {write: false}\n"
        '  gates: [{match: "*", require: plan_approval}]\n'
        "  escalation: {route: cli}\n"
    )


def test_compose_agent_yaml_parses_with_expected_transport_and_gate(tmp_path):
    dest = tmp_path / "out"

    compose(_make_spec(), dest)

    data = yaml.safe_load((dest / "bundle" / "agent.yaml").read_text())
    assert data["transports"] == [{"kind": "a2a", "host": "0.0.0.0", "port": 8080}]
    assert data["tools"] == {"provider": "none", "allow": []}
    assert data["approvals"]["gates"] == [{"match": "*", "require": "plan_approval"}]
    # the placeholder is a literal string pre-validation: not yet substituted
    assert data["model"]["id"] == "${AGENT_MODEL:-vertex_ai/claude-sonnet-4-5}"
    # Truncated-answers fix: the runtime's own default is 8192 -- the room's
    # composed default must match it, not the old 1024 that cut long or
    # thinking-heavy answers off mid-sentence.
    assert data["model"]["max_tokens"] == 8192


# --- memory stanza: revision gate (belief-revision plan, Task 2) ---------------
#
# The bundle's memory stanza gains `revision: operator` (the gate daedalus's
# post-boot belief-revision pass reads, per docs/superpowers/specs/
# 2026-08-20-belief-revision-design.md) unconditionally at compose time --
# never gated on a check of the memory provider, because `BundleSpec` exposes
# no field that lets a caller select anything other than techne memory: every
# bundle this room composes already IS a techne bundle (pinned directly by
# `test_bundle_spec_has_no_memory_provider_selector` below). If a future
# `BundleSpec` grows a real memory-provider selector, the emission here
# should grow a matching conditional and this section's "always" framing
# would need to change with it.


def test_bundle_spec_has_no_memory_provider_selector():
    """Pin: there is no code path today that composes a bundle whose memory
    provider is anything other than techne -- `BundleSpec` has no field for
    it, and `_agent_yaml_data` hardcodes `personal: techne`. This is the
    reality check the brief asks for before adding `revision: operator`
    unconditionally: if this test ever starts failing (a future BundleSpec
    grows a memory-provider-selecting field), the emission of `revision`
    needs to become conditional on that field rather than unconditional."""
    field_names = {f.name for f in fields(BundleSpec)}
    assert "memory" not in field_names
    assert "memory_provider" not in field_names


@pytest.mark.parametrize(
    "spec_kwargs",
    [
        {},
        {"cabal_url": "http://cabal.example.com"},
        {"team": "acme-team", "purpose_tags": ["billing", "support"]},
    ],
    ids=["default", "cabal_url_configured", "team_and_tags"],
)
def test_compose_agent_yaml_memory_always_gains_revision_operator(tmp_path, spec_kwargs):
    """Emission matrix: whatever else varies about the spec, the memory
    stanza always parses with `personal: techne` and `revision: operator` --
    the unconditional-for-room-agents shape this section's header explains."""
    dest = tmp_path / "out"

    compose(_make_spec(**spec_kwargs), dest)

    data = yaml.safe_load((dest / "bundle" / "agent.yaml").read_text())
    assert data["memory"] == {"personal": "techne", "home": "/techne", "revision": "operator"}


# --- a2a auth block (colleagues switchboard arc, Task 4) ------------------------
#
# The composer's own gate is the presence of `cabal_url` alone (a `BundleSpec`
# field), never the fuller `cabal_enabled` truthiness check `kobo_api.clients`
# exposes to callers that also need the operator token — compose is about the
# bundle's shape, not whether the room happens to hold a token right now.


def test_compose_agent_yaml_byte_identical_when_cabal_url_none_both_ways(tmp_path):
    """Pin: whether `cabal_url` is left at its default or passed explicitly as
    `None`, composed output is byte-identical to today's (pre-Task-4) shape —
    the new field must never perturb the off path."""
    default_dest = tmp_path / "default"
    explicit_dest = tmp_path / "explicit"

    compose(_make_spec(), default_dest)
    compose(_make_spec(cabal_url=None), explicit_dest)

    default_text = (default_dest / "bundle" / "agent.yaml").read_text()
    explicit_text = (explicit_dest / "bundle" / "agent.yaml").read_text()

    assert default_text == explicit_text
    assert default_text == (
        "name: sam\n"
        "card: agent-card.json\n"
        "persona: persona.md\n"
        "model:\n"
        "  id: ${AGENT_MODEL:-vertex_ai/claude-sonnet-4-5}\n"
        "  max_tokens: 8192\n"
        "  budget: {per_task_tokens: 50000}\n"
        "transports:\n"
        "  - {kind: a2a, host: 0.0.0.0, port: 8080}\n"
        "tools: {provider: none, allow: []}\n"
        "memory: {personal: techne, home: /techne, revision: operator}\n"
        "checkpoints: sqlite\n"
        "approvals:\n"
        "  ceiling: {write: false}\n"
        '  gates: [{match: "*", require: plan_approval}]\n'
        "  escalation: {route: cli}\n"
    )


def test_compose_agent_yaml_carries_auth_block_when_cabal_url_configured(tmp_path):
    """Note: since Task 3/B3, `cabal_url` configured also swaps the `tools:`
    line to the colleagues provider's config block (see the "tools block"
    section below), and since the "fix round: the approval gates" amendment
    it also composes the approvals `gates:` list empty (see the "approvals
    gates" section further below) — this test's pinned text carries both, so
    this single byte-for-byte assertion still proves every OTHER line is
    untouched by any of the three features."""
    dest = tmp_path / "out"

    compose(_make_spec(cabal_url="http://cabal.example.com"), dest)

    text = (dest / "bundle" / "agent.yaml").read_text()
    assert text == (
        "name: sam\n"
        "card: agent-card.json\n"
        "persona: persona.md\n"
        "model:\n"
        "  id: ${AGENT_MODEL:-vertex_ai/claude-sonnet-4-5}\n"
        "  max_tokens: 8192\n"
        "  budget: {per_task_tokens: 50000}\n"
        "transports:\n"
        '  - {kind: a2a, host: 0.0.0.0, port: 8080, auth: {jwks_url: '
        '"http://cabal.example.com/.well-known/jwks.json", issuer: "cabal-core", '
        'audience: "sam"}}\n'
        'tools: {provider: colleagues, allow: [colleagues.list_colleagues, '
        'colleagues.ask_colleague, colleagues.remember], colleagues: {url: "http://cabal.example.com", '
        'token_env: CABAL_COLLEAGUE_TOKEN, self: "sam"}}\n'
        "memory: {personal: techne, home: /techne, revision: operator}\n"
        "checkpoints: sqlite\n"
        "approvals:\n"
        "  ceiling: {write: false}\n"
        "  gates: []\n"
        "  escalation: {route: cli}\n"
    )


def test_compose_agent_yaml_auth_jwks_url_strips_single_trailing_slash(tmp_path):
    """A single `rstrip("/")` on `cabal_url` before appending the well-known
    path — a trailing-slash-or-not configured base URL must never yield a
    doubled (or missing) slash in the built jwks_url."""
    dest = tmp_path / "out"

    compose(_make_spec(cabal_url="http://cabal.example.com/"), dest)

    data = yaml.safe_load((dest / "bundle" / "agent.yaml").read_text())
    auth = data["transports"][0]["auth"]
    assert auth["jwks_url"] == "http://cabal.example.com/.well-known/jwks.json"


def test_compose_agent_yaml_auth_audience_is_the_agent_name(tmp_path):
    dest = tmp_path / "out"
    spec = BundleSpec(
        name="rae",
        persona_body="You are rae, a helpful assistant.\n",
        model_id="vertex_ai/claude-sonnet-4-5",
        skills=[],
        runtime_image=_RUNTIME_IMAGE,
        vertex_project="example-model-project",
        vertex_location="us-east5",
        cabal_url="http://cabal.example.com",
    )

    compose(spec, dest)

    data = yaml.safe_load((dest / "bundle" / "agent.yaml").read_text())
    auth = data["transports"][0]["auth"]
    assert auth["audience"] == "rae"
    assert auth["issuer"] == "cabal-core"


def test_compose_agent_yaml_auth_block_parses_with_exactly_three_keys(tmp_path):
    dest = tmp_path / "out"

    compose(_make_spec(cabal_url="http://cabal.example.com"), dest)

    data = yaml.safe_load((dest / "bundle" / "agent.yaml").read_text())
    transport = data["transports"][0]
    assert transport["kind"] == "a2a"
    assert set(transport["auth"].keys()) == {"jwks_url", "issuer", "audience"}


def test_compose_agent_yaml_with_auth_block_still_validates(tmp_path):
    """The vendored schema's TransportSpec allows additional properties, so
    the new `auth` key doesn't trip schema validation."""
    dest = tmp_path / "out"

    compose(_make_spec(cabal_url="http://cabal.example.com"), dest)

    validate_agent_yaml(dest / "bundle")  # must not raise


# --- tools block (colleagues switchboard plan, Task 3/B3) -----------------------
#
# Same composer gate as the auth block above: the presence of `cabal_url`
# alone (a `BundleSpec` field), never the fuller `cabal_enabled` truthiness
# check `kobo_api.clients` exposes to callers that also need the operator
# token — compose is about the bundle's shape, not whether the room happens
# to hold a token right now.


def test_compose_agent_yaml_tools_block_byte_identical_when_cabal_url_none_both_ways(tmp_path):
    """Pin: whether `cabal_url` is left at its default or passed explicitly
    as `None`, the composed `tools:` line is byte-identical to today's
    (pre-Task-3/B3) shape — the new field must never perturb the off path."""
    default_dest = tmp_path / "default"
    explicit_dest = tmp_path / "explicit"

    compose(_make_spec(), default_dest)
    compose(_make_spec(cabal_url=None), explicit_dest)

    default_text = (default_dest / "bundle" / "agent.yaml").read_text()
    explicit_text = (explicit_dest / "bundle" / "agent.yaml").read_text()

    assert default_text == explicit_text
    assert "tools: {provider: none, allow: []}\n" in default_text


def test_compose_agent_yaml_carries_colleagues_tools_block_when_cabal_url_configured(tmp_path):
    dest = tmp_path / "out"

    compose(_make_spec(cabal_url="http://cabal.example.com"), dest)

    data = yaml.safe_load((dest / "bundle" / "agent.yaml").read_text())
    assert data["tools"] == {
        "provider": "colleagues",
        "allow": ["colleagues.list_colleagues", "colleagues.ask_colleague", "colleagues.remember"],
        "colleagues": {
            "url": "http://cabal.example.com",
            "token_env": "CABAL_COLLEAGUE_TOKEN",
            "self": "sam",
        },
    }


def test_compose_agent_yaml_tools_block_self_is_the_agent_name(tmp_path):
    dest = tmp_path / "out"
    spec = BundleSpec(
        name="rae",
        persona_body="You are rae, a helpful assistant.\n",
        model_id="vertex_ai/claude-sonnet-4-5",
        skills=[],
        runtime_image=_RUNTIME_IMAGE,
        vertex_project="example-model-project",
        vertex_location="us-east5",
        cabal_url="http://cabal.example.com",
    )

    compose(spec, dest)

    data = yaml.safe_load((dest / "bundle" / "agent.yaml").read_text())
    assert data["tools"]["colleagues"]["self"] == "rae"


def test_compose_agent_yaml_tools_block_token_env_is_the_shared_constant(tmp_path):
    dest = tmp_path / "out"

    compose(_make_spec(cabal_url="http://cabal.example.com"), dest)

    data = yaml.safe_load((dest / "bundle" / "agent.yaml").read_text())
    assert data["tools"]["colleagues"]["token_env"] == CABAL_COLLEAGUE_TOKEN_ENV
    assert CABAL_COLLEAGUE_TOKEN_ENV == "CABAL_COLLEAGUE_TOKEN"


def test_compose_agent_yaml_with_colleagues_tools_block_still_validates(tmp_path):
    dest = tmp_path / "out"

    compose(_make_spec(cabal_url="http://cabal.example.com"), dest)

    validate_agent_yaml(dest / "bundle")  # must not raise


# --- approvals gates (fix round: the approval gates) -----------------------------
#
# Live acceptance round 2: the runtime consults gates via first-fnmatch-wins,
# and a deployed pod's approver is deny-all, so the bundle's blanket
# `match: "*"` gate blocked every tool outright, including the two colleague
# tools the tool allowlist alone is meant to gate. Per the "Amendments from
# live acceptance" section of `docs/superpowers/specs/
# 2026-08-19-colleagues-talk-design.md`: when the colleague tools are
# composed, the gates list composes EMPTY. Ceiling and escalation stay
# untouched — same composer gate (`spec.cabal_url` presence alone) as the
# auth block and tools block above.


def test_compose_agent_yaml_approvals_gates_empty_when_cabal_url_configured(tmp_path):
    dest = tmp_path / "out"

    compose(_make_spec(cabal_url="http://cabal.example.com"), dest)

    data = yaml.safe_load((dest / "bundle" / "agent.yaml").read_text())
    assert data["approvals"] == {
        "ceiling": {"write": False},
        "gates": [],
        "escalation": {"route": "cli"},
    }


def test_compose_agent_yaml_approvals_gates_line_is_empty_flow_list(tmp_path):
    dest = tmp_path / "out"

    compose(_make_spec(cabal_url="http://cabal.example.com"), dest)

    text = (dest / "bundle" / "agent.yaml").read_text()
    assert "  gates: []\n" in text


def test_compose_agent_yaml_approvals_gates_byte_identical_when_cabal_url_none_both_ways(
    tmp_path,
):
    """Pin: whether `cabal_url` is left at its default or passed explicitly
    as `None`, the composed `approvals:` stanza is byte-identical to today's
    shape — the blanket `match: "*"` gate is untouched on the off path."""
    default_dest = tmp_path / "default"
    explicit_dest = tmp_path / "explicit"

    compose(_make_spec(), default_dest)
    compose(_make_spec(cabal_url=None), explicit_dest)

    default_text = (default_dest / "bundle" / "agent.yaml").read_text()
    explicit_text = (explicit_dest / "bundle" / "agent.yaml").read_text()

    assert default_text == explicit_text
    assert '  gates: [{match: "*", require: plan_approval}]\n' in default_text


def test_compose_agent_yaml_with_empty_approvals_gates_still_validates(tmp_path):
    dest = tmp_path / "out"

    compose(_make_spec(cabal_url="http://cabal.example.com"), dest)

    validate_agent_yaml(dest / "bundle")  # must not raise


# --- update_agent_yaml_tools (colleagues switchboard plan, Task 3/B3) -----------


def _write_bare_agent_yaml(tmp_path, *, extra_lines: str = "") -> Path:
    """A minimal already-composed `bundle/agent.yaml` on disk, with the
    default (off) `tools:` line — `update_agent_yaml_tools`'s backfill
    target. `extra_lines`, when given, is inserted verbatim right after the
    top-of-file comment so a test can prove those survive the edit."""
    bundle_dir = tmp_path / "bundle"
    bundle_dir.mkdir()
    (bundle_dir / "agent.yaml").write_text(
        "# a hand-written comment at the top\n"
        + extra_lines
        + "name: sam\n"
        "card: agent-card.json\n"
        "persona: persona.md\n"
        "transports:\n"
        "  - {kind: a2a, host: 0.0.0.0, port: 8080}\n"
        "tools: {provider: none, allow: []}\n"
        "memory: {personal: techne, home: /techne}\n"
        "checkpoints: sqlite\n"
    )
    return bundle_dir


def test_update_agent_yaml_tools_adds_block_and_preserves_every_other_line(tmp_path):
    bundle_dir = _write_bare_agent_yaml(tmp_path)
    before = (bundle_dir / "agent.yaml").read_text()

    update_agent_yaml_tools(
        bundle_dir,
        url="http://cabal.example.com",
        token_env="CABAL_COLLEAGUE_TOKEN",
        self_name="sam",
    )

    after = (bundle_dir / "agent.yaml").read_text()
    before_lines = before.splitlines()
    after_lines = after.splitlines()
    assert len(before_lines) == len(after_lines)
    for before_line, after_line in zip(before_lines, after_lines, strict=True):
        if before_line.startswith("tools:"):
            continue
        assert before_line == after_line

    data = yaml.safe_load(after)
    assert data["tools"] == {
        "provider": "colleagues",
        "allow": ["colleagues.list_colleagues", "colleagues.ask_colleague", "colleagues.remember"],
        "colleagues": {
            "url": "http://cabal.example.com",
            "token_env": "CABAL_COLLEAGUE_TOKEN",
            "self": "sam",
        },
    }


def test_update_agent_yaml_tools_replaces_an_existing_colleagues_block(tmp_path):
    bundle_dir = tmp_path / "bundle"
    bundle_dir.mkdir()
    (bundle_dir / "agent.yaml").write_text(
        "name: sam\n"
        'tools: {provider: colleagues, colleagues: {url: "http://old.example.com", '
        'token_env: CABAL_COLLEAGUE_TOKEN, self: "sam"}}\n'
        "memory: {personal: techne, home: /techne}\n"
    )

    update_agent_yaml_tools(
        bundle_dir,
        url="http://new.example.com",
        token_env="CABAL_COLLEAGUE_TOKEN",
        self_name="sam",
    )

    text = (bundle_dir / "agent.yaml").read_text()
    assert "http://old.example.com" not in text
    data = yaml.safe_load(text)
    assert data["tools"]["colleagues"]["url"] == "http://new.example.com"
    assert data["tools"]["allow"] == ["colleagues.list_colleagues", "colleagues.ask_colleague", "colleagues.remember"]
    # Every other line survives untouched.
    assert text.splitlines()[0] == "name: sam"
    assert text.splitlines()[-1] == "memory: {personal: techne, home: /techne}"


def test_update_agent_yaml_tools_is_idempotent(tmp_path):
    bundle_dir = _write_bare_agent_yaml(tmp_path)

    update_agent_yaml_tools(
        bundle_dir, url="http://cabal.example.com", token_env="CABAL_COLLEAGUE_TOKEN",
        self_name="sam",
    )
    once = (bundle_dir / "agent.yaml").read_text()

    update_agent_yaml_tools(
        bundle_dir, url="http://cabal.example.com", token_env="CABAL_COLLEAGUE_TOKEN",
        self_name="sam",
    )
    twice = (bundle_dir / "agent.yaml").read_text()

    assert once == twice


def test_update_agent_yaml_tools_upgrades_allow_less_colleagues_line_idempotently(tmp_path):
    """Backfill upgrade path: a tools line with colleagues block but no allow
    list (pre-fix) is upgraded to include the allow list, and repeated calls
    stay byte-identical (idempotent)."""
    bundle_dir = tmp_path / "bundle"
    bundle_dir.mkdir()
    (bundle_dir / "agent.yaml").write_text(
        "name: sam\n"
        'tools: {provider: colleagues, colleagues: {url: "http://old.example.com", '
        'token_env: CABAL_COLLEAGUE_TOKEN, self: "sam"}}\n'
        "memory: {personal: techne, home: /techne}\n"
    )

    update_agent_yaml_tools(
        bundle_dir,
        url="http://old.example.com",
        token_env="CABAL_COLLEAGUE_TOKEN",
        self_name="sam",
    )
    once = (bundle_dir / "agent.yaml").read_text()

    update_agent_yaml_tools(
        bundle_dir,
        url="http://old.example.com",
        token_env="CABAL_COLLEAGUE_TOKEN",
        self_name="sam",
    )
    twice = (bundle_dir / "agent.yaml").read_text()

    assert once == twice
    data = yaml.safe_load(once)
    assert data["tools"]["allow"] == ["colleagues.list_colleagues", "colleagues.ask_colleague", "colleagues.remember"]
    assert data["tools"]["provider"] == "colleagues"
    assert data["tools"]["colleagues"]["url"] == "http://old.example.com"


def test_update_agent_yaml_tools_raises_when_no_tools_line(tmp_path):
    bundle_dir = tmp_path / "bundle"
    bundle_dir.mkdir()
    (bundle_dir / "agent.yaml").write_text("name: sam\nmemory: {personal: techne, home: /techne}\n")

    with pytest.raises(ClientError, match="no tools"):
        update_agent_yaml_tools(
            bundle_dir, url="http://cabal.example.com", token_env="CABAL_COLLEAGUE_TOKEN",
            self_name="sam",
        )


def test_update_agent_yaml_tools_missing_file_raises_client_error(tmp_path):
    bundle_dir = tmp_path / "bundle"
    bundle_dir.mkdir()  # no agent.yaml written

    with pytest.raises(ClientError, match="agent.yaml not found"):
        update_agent_yaml_tools(
            bundle_dir, url="http://cabal.example.com", token_env="CABAL_COLLEAGUE_TOKEN",
            self_name="sam",
        )


def test_update_agent_yaml_tools_on_freshly_composed_bundle_round_trips(tmp_path):
    """The backfill path's real target: a bundle composed WITHOUT the
    switchboard configured (the default tools block), later backfilled once
    it becomes available — mirrors `update_agent_yaml_auth`'s own round-trip
    test."""
    dest = tmp_path / "out"
    compose(_make_spec(), dest)
    bundle_dir = dest / "bundle"

    update_agent_yaml_tools(
        bundle_dir,
        url="http://cabal.example.com",
        token_env="CABAL_COLLEAGUE_TOKEN",
        self_name="sam",
    )

    validate_agent_yaml(bundle_dir)  # must not raise
    data = yaml.safe_load((bundle_dir / "agent.yaml").read_text())
    assert data["tools"]["provider"] == "colleagues"
    assert data["tools"]["allow"] == ["colleagues.list_colleagues", "colleagues.ask_colleague", "colleagues.remember"]
    assert data["tools"]["colleagues"]["self"] == "sam"


# --- update_agent_yaml_approvals_gates (fix round: the approval gates) -----------
#
# The backfill path for the ceiling/gates/escalation stanza a live deployed
# agent carries verbatim: `approvals: {ceiling: {write: false}, gates:
# [{match: "*", require: plan_approval}], escalation: {route: cli}}`. The
# runtime consults gates via first-fnmatch-wins, and a deployed pod's
# approver is deny-all, so that blanket gate blocks every tool outright,
# including the two colleague tools the tool allowlist alone is meant to
# gate. This helper rewrites ONLY the `gates:` line, leaving `ceiling` and
# `escalation` (and every other line) byte-faithful — same idiom as
# `update_agent_yaml_tools` above.

_FIXTURE_AGENT_YAML_WITH_LIVE_GATES = (
    "name: sam\n"
    "card: agent-card.json\n"
    "persona: persona.md\n"
    "transports:\n"
    "  - {kind: a2a, host: 0.0.0.0, port: 8080}\n"
    "tools: {provider: colleagues, allow: [colleagues.list_colleagues, "
    'colleagues.ask_colleague, colleagues.remember], colleagues: {url: "http://cabal.example.com", '
    'token_env: CABAL_COLLEAGUE_TOKEN, self: "sam"}}\n'
    "memory: {personal: techne, home: /techne}\n"
    "checkpoints: sqlite\n"
    "approvals:\n"
    "  ceiling: {write: false}\n"
    '  gates: [{match: "*", require: plan_approval}]\n'
    "  escalation: {route: cli}\n"
)


def _write_gates_fixture(bundle_dir: Path, text: str = _FIXTURE_AGENT_YAML_WITH_LIVE_GATES) -> Path:
    bundle_dir.mkdir(parents=True, exist_ok=True)
    agent_yaml_path = bundle_dir / "agent.yaml"
    agent_yaml_path.write_text(text)
    return agent_yaml_path


def test_update_agent_yaml_approvals_gates_empties_gates_and_preserves_every_other_line(tmp_path):
    bundle_dir = tmp_path / "bundle"
    agent_yaml_path = _write_gates_fixture(bundle_dir)

    update_agent_yaml_approvals_gates(bundle_dir, gates=[])

    updated_lines = agent_yaml_path.read_text().splitlines(keepends=True)
    original_lines = _FIXTURE_AGENT_YAML_WITH_LIVE_GATES.splitlines(keepends=True)
    gates_index = original_lines.index('  gates: [{match: "*", require: plan_approval}]\n')
    assert len(updated_lines) == len(original_lines)
    for i, (original, updated) in enumerate(zip(original_lines, updated_lines)):
        if i == gates_index:
            continue
        assert updated == original, f"line {i} changed unexpectedly"

    assert updated_lines[gates_index] == "  gates: []\n"
    data = yaml.safe_load(agent_yaml_path.read_text())
    assert data["approvals"] == {
        "ceiling": {"write": False},
        "gates": [],
        "escalation": {"route": "cli"},
    }


def test_update_agent_yaml_approvals_gates_replaces_an_existing_value(tmp_path):
    bundle_dir = tmp_path / "bundle"
    fixture_with_empty_gates = _FIXTURE_AGENT_YAML_WITH_LIVE_GATES.replace(
        '  gates: [{match: "*", require: plan_approval}]\n', "  gates: []\n"
    )
    _write_gates_fixture(bundle_dir, fixture_with_empty_gates)

    update_agent_yaml_approvals_gates(
        bundle_dir, gates=[{"match": "*", "require": "plan_approval"}]
    )

    text = (bundle_dir / "agent.yaml").read_text()
    assert "gates: []" not in text
    data = yaml.safe_load(text)
    assert data["approvals"]["gates"] == [{"match": "*", "require": "plan_approval"}]
    assert data["approvals"]["ceiling"] == {"write": False}
    assert data["approvals"]["escalation"] == {"route": "cli"}
    # every other line survives untouched
    other_lines = [
        line
        for line in fixture_with_empty_gates.splitlines(keepends=True)
        if "gates:" not in line
    ]
    for line in other_lines:
        assert line in text


def test_update_agent_yaml_approvals_gates_is_idempotent(tmp_path):
    bundle_dir = tmp_path / "bundle"
    _write_gates_fixture(bundle_dir)

    update_agent_yaml_approvals_gates(bundle_dir, gates=[])
    once = (bundle_dir / "agent.yaml").read_text()

    update_agent_yaml_approvals_gates(bundle_dir, gates=[])
    twice = (bundle_dir / "agent.yaml").read_text()

    assert once == twice


def test_update_agent_yaml_approvals_gates_raises_when_no_approvals_stanza(tmp_path):
    bundle_dir = tmp_path / "bundle"
    plain_copy = (
        "name: sam\n"
        "card: agent-card.json\n"
        "persona: persona.md\n"
        "transports:\n"
        "  - {kind: a2a, host: 0.0.0.0, port: 8080}\n"
        "tools: {provider: none, allow: []}\n"
        "memory: {personal: techne, home: /techne}\n"
        "checkpoints: sqlite\n"
    )
    _write_gates_fixture(bundle_dir, plain_copy)

    with pytest.raises(ClientError, match="approvals"):
        update_agent_yaml_approvals_gates(bundle_dir, gates=[])

    # unmodified on failure
    assert (bundle_dir / "agent.yaml").read_text() == plain_copy


def test_update_agent_yaml_approvals_gates_missing_file_raises_client_error(tmp_path):
    bundle_dir = tmp_path / "bundle"
    bundle_dir.mkdir()  # no agent.yaml written

    with pytest.raises(ClientError, match="agent.yaml not found"):
        update_agent_yaml_approvals_gates(bundle_dir, gates=[])


def test_update_agent_yaml_approvals_gates_on_freshly_composed_bundle_round_trips(tmp_path):
    """The backfill's real target: a bundle composed WITHOUT the switchboard
    configured (the default, non-empty gate), later repaired once the
    colleague tools land — mirrors `update_agent_yaml_tools`'s own round-trip
    test."""
    dest = tmp_path / "out"
    compose(_make_spec(), dest)
    bundle_dir = dest / "bundle"

    update_agent_yaml_approvals_gates(bundle_dir, gates=[])

    validate_agent_yaml(bundle_dir)  # must not raise
    data = yaml.safe_load((bundle_dir / "agent.yaml").read_text())
    assert data["approvals"] == {
        "ceiling": {"write": False},
        "gates": [],
        "escalation": {"route": "cli"},
    }


# --- agent-card.json -----------------------------------------------------------


def test_compose_agent_card_lists_picked_skills(tmp_path):
    dest = tmp_path / "out"
    skill = _make_skill(tmp_path)

    compose(_make_spec(skills=[skill]), dest)

    card = json.loads((dest / "bundle" / "agent-card.json").read_text())
    assert card == {
        "name": "sam",
        "description": "Assembled by kobo.",
        "version": "0.1.0",
        "capabilities": [],
        "skills": [{"id": "echo"}],
        "metadata": {"team": "shared", "purpose_tags": [], "capability": ""},
    }


def test_compose_agent_card_empty_skills_list(tmp_path):
    dest = tmp_path / "out"

    compose(_make_spec(), dest)

    card = json.loads((dest / "bundle" / "agent-card.json").read_text())
    assert card["skills"] == []


# --- agent-card.json metadata block (persona-metadata plan, Task 3) -------------


def test_compose_agent_card_metadata_block_present_capability_empty(tmp_path):
    """The card bake at compose time (Global Constraints: capability is
    earned later, never at compose) — the metadata block is always present,
    `capability` always starts `""`, `team`/`purpose_tags` come straight
    from the spec."""
    dest = tmp_path / "out"

    compose(_make_spec(team="acme-team", purpose_tags=["billing", "support"]), dest)

    card = json.loads((dest / "bundle" / "agent-card.json").read_text())
    assert card["metadata"] == {
        "team": "acme-team",
        "purpose_tags": ["billing", "support"],
        "capability": "",
    }


def test_compose_agent_card_metadata_defaults_to_shared_no_tags(tmp_path):
    dest = tmp_path / "out"

    compose(_make_spec(), dest)

    card = json.loads((dest / "bundle" / "agent-card.json").read_text())
    assert card["metadata"] == {"team": "shared", "purpose_tags": [], "capability": ""}


# --- persona + knowledge --------------------------------------------------------


def test_compose_persona_written_verbatim(tmp_path):
    dest = tmp_path / "out"
    spec = _make_spec()

    compose(spec, dest)

    assert (dest / "bundle" / "persona.md").read_text() == spec.persona_body


def test_compose_writes_self_card_knowledge(tmp_path):
    """Pins group and team as two separate, honestly-labelled facts.
    Regression: self.md once rendered `spec.team` mislabeled as "your
    group" and carried no group fact at all -- `group_name` is the ACL
    namespace (`kobo_api.models.Agent.group_name`'s own docstring), distinct
    from `team` (the skills-catalog capability axis); the two must never be
    conflated, including in what the agent is told about itself."""
    dest = tmp_path / "out"
    spec = _make_spec(name="kestrel", team="growth", group="growth-eng-acl")

    compose(spec, dest)

    text = (dest / "bundle" / "knowledge" / "self.md").read_text()
    assert "kestrel" in text
    assert "Your group is growth-eng-acl." in text
    assert "Your team is growth." in text
    # the standing instruction, pinned by content so a future edit is deliberate
    assert "questions about you are yours to answer" in text
    assert "I don't know yet" in text


def test_compose_self_card_carries_the_origin_line(tmp_path):
    """Step 1b (operator ruling 2026-08-27): the deployment-chain fact
    returns in the self-card rather than a standalone gotchas file. Pinned
    by exact substring, byte-for-byte, so drift must be deliberate."""
    dest = tmp_path / "out"
    spec = _make_spec(name="kestrel")

    compose(spec, dest)

    text = (dest / "bundle" / "knowledge" / "self.md").read_text()
    assert (
        "You were assembled in the kobo assembly room and deployed through "
        "the platform chain: repo, onboarding, build, deploy, first contact."
    ) in text
    assert "If someone asks how you got here, that is the answer." in text


def test_compose_writes_colleague_roster(tmp_path):
    dest = tmp_path / "out"
    spec = _make_spec(
        colleagues=[
            {
                "name": "ward",
                "team": "infra-engineering",
                "purpose_tags": ["security"],
                "capability": "keeps our workshop's digital info safe",
            },
        ]
    )

    compose(spec, dest)

    text = (dest / "bundle" / "knowledge" / "colleagues.md").read_text()
    assert "ward" in text
    assert "keeps our workshop's digital info safe" in text


def test_compose_roster_omits_the_agent_itself(tmp_path):
    dest = tmp_path / "out"
    spec = _make_spec(
        name="kestrel",
        colleagues=[
            {"name": "kestrel", "team": "vidai", "purpose_tags": [], "capability": "x"},
            {"name": "ward", "team": "infra-engineering", "purpose_tags": [], "capability": "y"},
        ],
    )

    compose(spec, dest)

    text = (dest / "bundle" / "knowledge" / "colleagues.md").read_text()
    assert "ward" in text
    assert "kestrel" not in text.split("## Your colleagues")[1]


def test_compose_roster_names_the_snapshot_staleness(tmp_path):
    """The roster is written at assembly and (until the refresh lane
    ships) never updated; the copy must say so, or an agent will
    confidently suggest a colleague who has since been destroyed."""
    dest = tmp_path / "out"
    spec = _make_spec(colleagues=[
        {"name": "ward", "team": "infra-engineering", "purpose_tags": [], "capability": "x"},
    ])
    compose(spec, dest)
    text = (dest / "bundle" / "knowledge" / "colleagues.md").read_text()
    assert "snapshot from when you were assembled" in text
    assert "list_colleagues" in text


def test_compose_roster_handles_no_colleagues(tmp_path):
    dest = tmp_path / "out"
    spec = _make_spec(colleagues=[])

    compose(spec, dest)

    text = (dest / "bundle" / "knowledge" / "colleagues.md").read_text()
    assert "no colleagues" in text.lower()
    # 2026-08-27 ruling, second half: a lone agent answers freely from its
    # own knowledge -- the consult-first doctrine binds once colleagues
    # exist, never before.
    assert (
        "Until you do, every "
        "question is yours: answer from your own knowledge and memory."
    ) in text


# --- consult-first instruction (standing-knowledge arc Task 7; upgraded by ------
# --- the 2026-08-27 operator ruling) ---------------------------------------------
#
# Exact copy, no em dashes, pinned by substring so drift must be deliberate.
# The first cut of this instruction was suggest-only ("offer to bring them
# in"); the ruling flipped the default to consult-first -- an agent's
# general model knowledge is what any assistant anywhere would say, and an
# organization question in a colleague's field is answered by ASKING that
# colleague, with attribution, not by offering. Present unconditionally --
# both with and without a roster -- since a roomful of zero colleagues today
# may gain one after the refresh lane delivers a fresh roster
# (`refresh_colleague_rosters`), and the instruction must already be
# standing when that happens.


def test_compose_colleagues_md_carries_the_consult_first_instruction(tmp_path):
    dest = tmp_path / "out"
    spec = _make_spec(colleagues=[
        {"name": "ward", "team": "infra-engineering", "purpose_tags": [], "capability": "x"},
    ])

    compose(spec, dest)

    text = (dest / "bundle" / "knowledge" / "colleagues.md").read_text()
    assert (
        "Your own model knowledge is what any "
        "assistant anywhere would say to anyone; it is not how this "
        "organization does things."
    ) in text
    assert (
        "consult that colleague with ask_colleague FIRST and answer "
        "from what they say, naming them in your reply."
    ) in text
    assert (
        "When in doubt "
        "whether a question concerns them, lean toward asking"
    ) in text
    assert (
        "Do not answer an "
        "organization question from general knowledge while a colleague "
        "who owns that subject is reachable."
    ) in text
    assert (
        "Questions squarely in your own field you answer yourself"
    ) in text
    assert (
        "label the answer as general practice, not as how this "
        "organization does it."
    ) in text


def test_compose_colleagues_md_carries_the_attribution_instruction(tmp_path):
    dest = tmp_path / "out"
    spec = _make_spec(colleagues=[])

    compose(spec, dest)

    text = (dest / "bundle" / "knowledge" / "colleagues.md").read_text()
    assert (
        "When a colleague's answer informs yours, say so and name them."
    ) in text
    assert (
        "You will not remember the consultation next turn (colleague "
        "exchanges are not in your replayed history), so the attribution "
        "in your reply is the only record you get: without it you will "
        "later misremember their work as your own knowledge."
    ) in text


def test_compose_colleagues_md_instruction_copy_has_no_em_dashes(tmp_path):
    dest = tmp_path / "out"
    spec = _make_spec(colleagues=[
        {"name": "ward", "team": "infra-engineering", "purpose_tags": [], "capability": "x"},
    ])

    compose(spec, dest)

    text = (dest / "bundle" / "knowledge" / "colleagues.md").read_text()
    assert "—" not in text


# --- capability fallback chain (standing-knowledge arc, Task 7, Step 2) --------
#
# capability -> purpose_tags -> skills -> "not yet distilled". An agent
# nobody can match on any of the three is invisible to the suggest-a-
# colleague feature, which is why the fallback is required rather than
# nice to have.


def test_colleague_roster_line_uses_capability_when_present(tmp_path):
    dest = tmp_path / "out"
    spec = _make_spec(colleagues=[
        {
            "name": "ward",
            "team": "infra-engineering",
            "purpose_tags": ["security"],
            "capability": "keeps our workshop's digital info safe",
            "skills": ["firewall-config"],
        },
    ])

    compose(spec, dest)

    text = (dest / "bundle" / "knowledge" / "colleagues.md").read_text()
    assert "What they are for: keeps our workshop's digital info safe" in text


def test_colleague_roster_line_falls_back_to_purpose_tags_when_capability_empty(tmp_path):
    dest = tmp_path / "out"
    spec = _make_spec(colleagues=[
        {
            "name": "ward",
            "team": "infra-engineering",
            "purpose_tags": ["security", "networking"],
            "capability": None,
            "skills": ["firewall-config"],
        },
    ])

    compose(spec, dest)

    text = (dest / "bundle" / "knowledge" / "colleagues.md").read_text()
    assert "What they are for: security, networking" in text


def test_colleague_roster_line_falls_back_to_skills_when_capability_and_tags_empty(tmp_path):
    dest = tmp_path / "out"
    spec = _make_spec(colleagues=[
        {
            "name": "ward",
            "team": "infra-engineering",
            "purpose_tags": [],
            "capability": None,
            "skills": ["firewall-config", "incident-response"],
        },
    ])

    compose(spec, dest)

    text = (dest / "bundle" / "knowledge" / "colleagues.md").read_text()
    assert "What they are for: firewall-config, incident-response" in text


def test_colleague_roster_line_is_not_yet_distilled_when_all_three_are_empty(tmp_path):
    dest = tmp_path / "out"
    spec = _make_spec(colleagues=[
        {
            "name": "ward",
            "team": "infra-engineering",
            "purpose_tags": [],
            "capability": None,
            "skills": [],
        },
    ])

    compose(spec, dest)

    text = (dest / "bundle" / "knowledge" / "colleagues.md").read_text()
    assert "What they are for: not yet distilled" in text


# --- Dockerfile ------------------------------------------------------------------


def test_compose_dockerfile_matches_runtime_image(tmp_path):
    dest = tmp_path / "out"

    compose(_make_spec(), dest)

    assert (dest / "Dockerfile").read_text() == (
        "# Agent image: the shared runtime plus this agent's bundle.\n"
        f"FROM {_RUNTIME_IMAGE}\n"
        "COPY bundle/ /bundle\n"
    )


# --- Agent memory replication (resilience arc, Task 14) --------------------------
#
# Every agent is born with its techne database replicating continuously to
# object storage. A sidecar container cannot ride the composed bundle (the
# pod spec belongs to the platform, not the room), so replication runs
# INSIDE the agent container, supervised by the entrypoint of the composed
# image -- the lever is the Dockerfile the room already writes. Gated on
# `spec.memory_replica_bucket` alone (a `BundleSpec` field, threaded from
# `Config.memory_replica_bucket` by `kobo_api.steps.RepoStep`), the same
# presence-alone gate `cabal_url` already establishes elsewhere in this
# module -- empty/unset means the feature is off and composed bundles carry
# no supervision at all, pinned by the "both arms" tests below.


def test_compose_carries_no_replication_supervision_when_bucket_unset(tmp_path):
    """Off arm: byte-identical to the pre-Task-14 shape, pinned above --
    no litestream.yml, no entrypoint.sh, and the plain (no ENTRYPOINT)
    Dockerfile, whether the field is left at its default or passed
    explicitly as an empty string."""
    default_dest = tmp_path / "default"
    explicit_dest = tmp_path / "explicit"

    compose(_make_spec(), default_dest)
    compose(_make_spec(memory_replica_bucket=""), explicit_dest)

    for dest in (default_dest, explicit_dest):
        assert (dest / "Dockerfile").read_text() == (
            "# Agent image: the shared runtime plus this agent's bundle.\n"
            f"FROM {_RUNTIME_IMAGE}\n"
            "COPY bundle/ /bundle\n"
        )
        assert not (dest / "litestream.yml").exists()
        assert not (dest / "entrypoint.sh").exists()


def test_compose_carries_replication_supervision_when_bucket_configured(tmp_path):
    """On arm: the composed Dockerfile installs litestream, copies in its
    config and an entrypoint wrapper, and overrides ENTRYPOINT to run that
    wrapper -- litestream replicate wrapping (not replacing) whatever
    command the runtime image itself would otherwise run."""
    dest = tmp_path / "out"

    compose(_make_spec(name="kestrel", memory_replica_bucket="kobo-memory-replicas"), dest)

    dockerfile = (dest / "Dockerfile").read_text()
    assert f"FROM {_RUNTIME_IMAGE}" in dockerfile
    assert "COPY bundle/ /bundle" in dockerfile
    assert "litestream" in dockerfile.lower()
    assert "COPY litestream.yml /etc/litestream.yml" in dockerfile
    assert "COPY entrypoint.sh /entrypoint.sh" in dockerfile
    assert 'ENTRYPOINT ["/entrypoint.sh"]' in dockerfile
    # 2026-08-27, the drill's first real image build caught both of these:
    # the release ASSET name carries the v prefix too (the un-prefixed
    # spelling 404s at ADD), and the runtime image runs nonroot (USER
    # agent), so the install needs a root window and must hand the image
    # back to the runtime's own user before ENTRYPOINT -- anything else
    # either can't read the root-owned tarball or silently runs the whole
    # agent as root.
    assert "litestream-v0.3.13-linux-amd64.tar.gz" in dockerfile
    assert dockerfile.index("USER root\n") < dockerfile.index("\nADD https://")
    assert dockerfile.index("USER agent\n") < dockerfile.index("ENTRYPOINT ")
    assert dockerfile.index("RUN chmod +x /entrypoint.sh") < dockerfile.index("USER agent\n")

    entrypoint = (dest / "entrypoint.sh").read_text()
    assert "litestream replicate" in entrypoint
    assert "-exec" in entrypoint
    # Critical 4 fix: this ENTRYPOINT replaces the base runtime image's own
    # (docker rule: a new ENTRYPOINT resets the base image's CMD), so "$*"
    # here is always empty -- there is nothing for litestream's -exec to
    # inherit, and even a preserved CMD would just exec the /bundle
    # directory path, not a runnable command. The child command is
    # hardcoded instead: the base image's own entrypoint script
    # (daedalus docker/Dockerfile's ENTRYPOINT) invoked with its own
    # default bundle arg, exactly what the base image's ENTRYPOINT+CMD
    # would have run had this image never overridden either.
    assert (
        'exec litestream replicate -config /etc/litestream.yml '
        '-exec "/usr/local/bin/entrypoint.sh /bundle"'
    ) in entrypoint
    assert "$*" not in entrypoint

    litestream_yml = (dest / "litestream.yml").read_text()
    assert "/techne/techne.db" in litestream_yml
    assert "kobo-memory-replicas" in litestream_yml
    # Per-agent prefix: this agent's own name, never another agent's data or
    # the bucket root -- the same namespace the access-step grant (Task 14,
    # tests/test_steps.py) scopes its IAM condition to.
    assert "kestrel/" in litestream_yml


def test_compose_replication_prefix_is_per_agent(tmp_path):
    """Two different agents against the SAME configured bucket get two
    DIFFERENT replica prefixes -- proof this isn't a shared, bucket-wide
    path any agent's replica could collide with another's."""
    dest_a = tmp_path / "a"
    dest_b = tmp_path / "b"

    compose(_make_spec(name="alpha", memory_replica_bucket="shared-bucket"), dest_a)
    compose(_make_spec(name="beta", memory_replica_bucket="shared-bucket"), dest_b)

    yml_a = (dest_a / "litestream.yml").read_text()
    yml_b = (dest_b / "litestream.yml").read_text()
    assert "alpha/" in yml_a
    assert "beta/" not in yml_a
    assert "beta/" in yml_b
    assert "alpha/" not in yml_b


# --- staged skill copy (Task 5: bucket catalogs replace git-archive pinning) ------


def test_compose_copies_whole_staged_skill_tree(tmp_path):
    dest = tmp_path / "out"
    skill = _make_skill(tmp_path)

    compose(_make_spec(skills=[skill]), dest)

    copied = dest / "bundle" / "skills" / "echo"
    assert copied.is_dir()
    assert (copied / "SKILL.md").read_text() == (skill.dir / "SKILL.md").read_text()
    assert (copied / "reference.py").read_text() == "def helper():\n    return 1\n"


def test_compose_copies_multiple_skills_by_id(tmp_path):
    dest = tmp_path / "out"
    echo = _make_skill(tmp_path, "echo")
    other = _make_skill(tmp_path, "other")

    compose(_make_spec(skills=[echo, other]), dest)

    assert (dest / "bundle" / "skills" / "echo" / "SKILL.md").is_file()
    assert (dest / "bundle" / "skills" / "other" / "SKILL.md").is_file()


def test_compose_copies_nested_staged_files(tmp_path):
    skill = _make_skill(tmp_path)
    (skill.dir / "scripts").mkdir()
    (skill.dir / "scripts" / "helper.sh").write_text("#!/bin/sh\necho hi\n")
    dest = tmp_path / "out"

    compose(_make_spec(skills=[skill]), dest)

    assert (dest / "bundle" / "skills" / "echo" / "scripts" / "helper.sh").read_text() == (
        "#!/bin/sh\necho hi\n"
    )


def test_compose_staged_skill_symlink_member_raises_compose_error(tmp_path):
    """Symlink members are rejected loudly rather than silently
    followed/copied — a downloaded bucket skill can never legitimately
    contain one (GCS objects are plain files), but the same defense the old
    git-archive extraction path enforced is kept here verbatim in spirit:
    author the skill without symlinks instead of kobo silently dereferencing
    one."""
    skill = _make_skill(tmp_path)
    (skill.dir / "sneaky-link").symlink_to(skill.dir / "SKILL.md")
    dest = tmp_path / "out"

    with pytest.raises(ComposeError) as exc_info:
        compose(_make_spec(skills=[skill]), dest)

    message = str(exc_info.value)
    assert "sneaky-link" in message
    assert not (dest / "bundle" / "skills" / "echo" / "sneaky-link").exists()


# --- validate_agent_yaml -----------------------------------------------------------


def test_validate_agent_yaml_passes_on_composed_bundle(tmp_path):
    dest = tmp_path / "out"
    compose(_make_spec(), dest)

    validate_agent_yaml(dest / "bundle")  # must not raise


def test_validate_agent_yaml_raises_compose_error_on_corrupted_transports(tmp_path):
    dest = tmp_path / "out"
    compose(_make_spec(), dest)

    agent_yaml_path = dest / "bundle" / "agent.yaml"
    data = yaml.safe_load(agent_yaml_path.read_text())
    data["transports"] = "not-a-list"  # schema requires an array
    agent_yaml_path.write_text(yaml.safe_dump(data, sort_keys=False))

    with pytest.raises(ComposeError):
        validate_agent_yaml(dest / "bundle")


def test_validate_agent_yaml_error_is_not_a_bare_parse_failure(tmp_path):
    """The corrupted-transports fixture must still be well-formed YAML/JSON —
    proving validate_agent_yaml fails on *schema* grounds, not a parse error."""
    dest = tmp_path / "out"
    compose(_make_spec(), dest)

    agent_yaml_path = dest / "bundle" / "agent.yaml"
    data = yaml.safe_load(agent_yaml_path.read_text())
    data["transports"] = "not-a-list"
    agent_yaml_path.write_text(yaml.safe_dump(data, sort_keys=False))

    # parsing (post AGENT_MODEL-substitution) succeeds fine on its own
    import re

    substituted = re.sub(r"\$\{AGENT_MODEL:-([^}]*)\}", r"\1", agent_yaml_path.read_text())
    reparsed = yaml.safe_load(substituted)
    assert reparsed["transports"] == "not-a-list"

    with pytest.raises(ComposeError):
        validate_agent_yaml(dest / "bundle")


# --- update_agent_card_metadata (persona-metadata plan, Task 3) -----------------


def test_update_agent_card_metadata_writes_all_three_fields(tmp_path):
    dest = tmp_path / "out"
    compose(_make_spec(), dest)

    update_agent_card_metadata(
        dest / "bundle",
        team="acme-team",
        purpose_tags=["billing", "support"],
        capability="Helps customers resolve billing issues quickly.",
    )

    card = json.loads((dest / "bundle" / "agent-card.json").read_text())
    assert card["metadata"] == {
        "team": "acme-team",
        "purpose_tags": ["billing", "support"],
        "capability": "Helps customers resolve billing issues quickly.",
    }


def test_update_agent_card_metadata_preserves_every_other_top_level_field(tmp_path):
    """The load-bearing invariant: every field OTHER than `metadata` survives
    byte-faithfully, including a field this module has never heard of."""
    dest = tmp_path / "out"
    compose(_make_spec(), dest)
    card_path = dest / "bundle" / "agent-card.json"
    original = json.loads(card_path.read_text())
    original["a_field_kobo_does_not_know_about"] = {"nested": ["stuff", 1, True]}
    card_path.write_text(json.dumps(original, indent=2) + "\n")

    update_agent_card_metadata(
        dest / "bundle", team="acme-team", purpose_tags=["x"], capability="Does a thing."
    )

    card = json.loads(card_path.read_text())
    assert card["name"] == original["name"]
    assert card["description"] == original["description"]
    assert card["version"] == original["version"]
    assert card["capabilities"] == original["capabilities"]
    assert card["skills"] == original["skills"]
    assert card["a_field_kobo_does_not_know_about"] == {"nested": ["stuff", 1, True]}
    assert card["metadata"] == {
        "team": "acme-team",
        "purpose_tags": ["x"],
        "capability": "Does a thing.",
    }


def test_update_agent_card_metadata_preserves_unknown_metadata_subfield(tmp_path):
    """An unknown key already inside `metadata` itself (a hand-edited or
    future-versioned card) survives too — only the three known keys are ever
    replaced."""
    dest = tmp_path / "out"
    compose(_make_spec(), dest)
    card_path = dest / "bundle" / "agent-card.json"
    card = json.loads(card_path.read_text())
    card["metadata"]["future_field"] = "kept"
    card_path.write_text(json.dumps(card, indent=2) + "\n")

    update_agent_card_metadata(
        dest / "bundle", team="acme-team", purpose_tags=[], capability="Does a thing."
    )

    updated = json.loads(card_path.read_text())
    assert updated["metadata"]["future_field"] == "kept"
    assert updated["metadata"]["team"] == "acme-team"


def test_update_agent_card_metadata_uses_compose_style_json_dump(tmp_path):
    """Round-trip formatting must match compose()'s own `json.dumps(...,
    indent=2) + "\\n"` style exactly, so a compose-then-update cycle never
    drifts the file's own formatting."""
    dest = tmp_path / "out"
    compose(_make_spec(), dest)
    card_path = dest / "bundle" / "agent-card.json"

    update_agent_card_metadata(
        dest / "bundle", team="acme-team", purpose_tags=[], capability="Does a thing."
    )

    text = card_path.read_text()
    reserialized = json.dumps(json.loads(text), indent=2) + "\n"
    assert text == reserialized


def test_update_agent_card_metadata_overwrites_prior_metadata_values(tmp_path):
    """A second call fully replaces the three known keys' values (not a
    merge/append of purpose_tags, say) — the caller always passes the
    complete current set."""
    dest = tmp_path / "out"
    compose(_make_spec(team="old-team", purpose_tags=["old-tag"]), dest)

    update_agent_card_metadata(
        dest / "bundle",
        team="new-team",
        purpose_tags=["new-tag"],
        capability="A fresh sentence.",
    )

    card = json.loads((dest / "bundle" / "agent-card.json").read_text())
    assert card["metadata"] == {
        "team": "new-team",
        "purpose_tags": ["new-tag"],
        "capability": "A fresh sentence.",
    }


# --- update_agent_yaml_auth (colleagues switchboard arc, Task 4) ----------------
#
# The in-place backfill helper for the two live agents (and any future
# already-composed bundle) whose `agent.yaml` predates the switchboard: adds
# or replaces the `auth` block on the existing a2a transport entry, touching
# nothing else in the file — a surgical text edit (matching
# `update_dockerfile_runtime_image`'s own idiom), never a full YAML
# round-trip, since a generic `yaml.safe_load`/`yaml.safe_dump` cycle would
# reorder/reformat every other line this module's custom flow-style dumper
# deliberately controls.

_FIXTURE_AGENT_YAML_NO_AUTH = (
    "# agent.yaml -- generated by kobo, do not hand-edit lightly\n"
    "name: sam\n"
    "card: agent-card.json\n"
    "persona: persona.md\n"
    "model:\n"
    "  id: ${AGENT_MODEL:-vertex_ai/claude-sonnet-4-5}\n"
    "  max_tokens: 1024\n"
    "  budget: {per_task_tokens: 50000}\n"
    "transports:\n"
    "  # the room's own a2a endpoint\n"
    "  - {kind: a2a, host: 0.0.0.0, port: 8080}\n"
    "tools: {provider: none, allow: []}\n"
    "memory: {personal: techne, home: /techne}\n"
    "checkpoints: sqlite\n"
    "approvals:\n"
    "  ceiling: {write: false}\n"
    '  gates: [{match: "*", require: plan_approval}]\n'
    "  escalation: {route: cli}\n"
)

_JWKS_URL = "http://cabal.example.com/.well-known/jwks.json"
_ISSUER = "cabal-core"
_AUDIENCE = "sam"


def _write_fixture(bundle_dir: Path, text: str = _FIXTURE_AGENT_YAML_NO_AUTH) -> Path:
    bundle_dir.mkdir(parents=True, exist_ok=True)
    agent_yaml_path = bundle_dir / "agent.yaml"
    agent_yaml_path.write_text(text)
    return agent_yaml_path


def test_update_agent_yaml_auth_adds_block_and_preserves_every_other_line(tmp_path):
    bundle_dir = tmp_path / "bundle"
    agent_yaml_path = _write_fixture(bundle_dir)

    update_agent_yaml_auth(
        bundle_dir, jwks_url=_JWKS_URL, issuer=_ISSUER, audience=_AUDIENCE
    )

    updated_lines = agent_yaml_path.read_text().splitlines(keepends=True)
    original_lines = _FIXTURE_AGENT_YAML_NO_AUTH.splitlines(keepends=True)
    assert len(updated_lines) == len(original_lines)
    for i, (original, updated) in enumerate(zip(original_lines, updated_lines)):
        if i == original_lines.index("  - {kind: a2a, host: 0.0.0.0, port: 8080}\n"):
            continue
        assert updated == original, f"line {i} changed unexpectedly"

    assert updated_lines[original_lines.index("  - {kind: a2a, host: 0.0.0.0, port: 8080}\n")] == (
        '  - {kind: a2a, host: 0.0.0.0, port: 8080, auth: {jwks_url: '
        '"http://cabal.example.com/.well-known/jwks.json", issuer: "cabal-core", '
        'audience: "sam"}}\n'
    )


def test_update_agent_yaml_auth_replaces_existing_block(tmp_path):
    fixture_with_old_auth = _FIXTURE_AGENT_YAML_NO_AUTH.replace(
        "  - {kind: a2a, host: 0.0.0.0, port: 8080}\n",
        '  - {kind: a2a, host: 0.0.0.0, port: 8080, auth: {jwks_url: '
        '"http://old-cabal.example.com/.well-known/jwks.json", issuer: '
        '"old-issuer", audience: "old-audience"}}\n',
    )
    bundle_dir = tmp_path / "bundle"
    _write_fixture(bundle_dir, fixture_with_old_auth)

    update_agent_yaml_auth(
        bundle_dir, jwks_url=_JWKS_URL, issuer=_ISSUER, audience=_AUDIENCE
    )

    text = (bundle_dir / "agent.yaml").read_text()
    assert "old-cabal.example.com" not in text
    assert "old-issuer" not in text
    assert "old-audience" not in text
    data = yaml.safe_load(text)
    auth = data["transports"][0]["auth"]
    assert auth == {"jwks_url": _JWKS_URL, "issuer": _ISSUER, "audience": _AUDIENCE}
    # every other line is untouched
    other_lines = [
        line
        for line in fixture_with_old_auth.splitlines(keepends=True)
        if "kind: a2a" not in line
    ]
    for line in other_lines:
        assert line in text


def test_update_agent_yaml_auth_is_idempotent(tmp_path):
    bundle_dir = tmp_path / "bundle"
    _write_fixture(bundle_dir)

    update_agent_yaml_auth(
        bundle_dir, jwks_url=_JWKS_URL, issuer=_ISSUER, audience=_AUDIENCE
    )
    once = (bundle_dir / "agent.yaml").read_text()

    update_agent_yaml_auth(
        bundle_dir, jwks_url=_JWKS_URL, issuer=_ISSUER, audience=_AUDIENCE
    )
    twice = (bundle_dir / "agent.yaml").read_text()

    assert once == twice


def test_update_agent_yaml_auth_raises_when_no_a2a_transport(tmp_path):
    fixture_no_a2a = _FIXTURE_AGENT_YAML_NO_AUTH.replace(
        "  # the room's own a2a endpoint\n  - {kind: a2a, host: 0.0.0.0, port: 8080}\n",
        "  - {kind: slack, channel: general}\n",
    )
    bundle_dir = tmp_path / "bundle"
    _write_fixture(bundle_dir, fixture_no_a2a)

    with pytest.raises(ClientError) as exc_info:
        update_agent_yaml_auth(
            bundle_dir, jwks_url=_JWKS_URL, issuer=_ISSUER, audience=_AUDIENCE
        )

    assert "a2a" in str(exc_info.value)
    # unmodified on failure
    assert (bundle_dir / "agent.yaml").read_text() == fixture_no_a2a


def test_update_agent_yaml_auth_leaves_other_transport_kinds_untouched(tmp_path):
    fixture_with_slack = _FIXTURE_AGENT_YAML_NO_AUTH.replace(
        "transports:\n  # the room's own a2a endpoint\n  - {kind: a2a, host: 0.0.0.0, port: 8080}\n",
        "transports:\n"
        "  # the room's own a2a endpoint\n"
        "  - {kind: a2a, host: 0.0.0.0, port: 8080}\n"
        "  - {kind: slack, channel: general}\n",
    )
    bundle_dir = tmp_path / "bundle"
    _write_fixture(bundle_dir, fixture_with_slack)

    update_agent_yaml_auth(
        bundle_dir, jwks_url=_JWKS_URL, issuer=_ISSUER, audience=_AUDIENCE
    )

    text = (bundle_dir / "agent.yaml").read_text()
    assert "  - {kind: slack, channel: general}\n" in text


def test_update_agent_yaml_auth_missing_file_raises_client_error(tmp_path):
    bundle_dir = tmp_path / "bundle"
    bundle_dir.mkdir(parents=True)

    with pytest.raises(ClientError):
        update_agent_yaml_auth(
            bundle_dir, jwks_url=_JWKS_URL, issuer=_ISSUER, audience=_AUDIENCE
        )


def test_update_agent_yaml_auth_on_freshly_composed_bundle_round_trips(tmp_path):
    """Integration: compose() without cabal_url, then backfill via the update
    helper (the two live agents' real path) — the result still validates."""
    dest = tmp_path / "out"
    compose(_make_spec(), dest)

    update_agent_yaml_auth(
        dest / "bundle", jwks_url=_JWKS_URL, issuer=_ISSUER, audience=_AUDIENCE
    )

    validate_agent_yaml(dest / "bundle")  # must not raise
    data = yaml.safe_load((dest / "bundle" / "agent.yaml").read_text())
    assert data["transports"][0]["auth"] == {
        "jwks_url": _JWKS_URL,
        "issuer": _ISSUER,
        "audience": _AUDIENCE,
    }


# --- update_agent_yaml_max_tokens (truncated-answers fix) -----------------------
#
# The in-place backfill helper for any already-composed bundle whose
# `agent.yaml` still carries the old 1024 ceiling: replaces the `model.
# max_tokens` line wholesale, touching nothing else on the `model:` stanza
# (`id`, `budget`) or elsewhere in the file -- a surgical text edit, same
# idiom as `update_agent_yaml_approvals_gates`.


def test_update_agent_yaml_max_tokens_rewrites_and_preserves_every_other_line(tmp_path):
    bundle_dir = tmp_path / "bundle"
    agent_yaml_path = _write_fixture(bundle_dir)

    update_agent_yaml_max_tokens(bundle_dir, max_tokens=8192)

    updated_lines = agent_yaml_path.read_text().splitlines(keepends=True)
    original_lines = _FIXTURE_AGENT_YAML_NO_AUTH.splitlines(keepends=True)
    max_tokens_index = original_lines.index("  max_tokens: 1024\n")
    assert len(updated_lines) == len(original_lines)
    for i, (original, updated) in enumerate(zip(original_lines, updated_lines)):
        if i == max_tokens_index:
            continue
        assert updated == original, f"line {i} changed unexpectedly"

    assert updated_lines[max_tokens_index] == "  max_tokens: 8192\n"


def test_update_agent_yaml_max_tokens_is_idempotent(tmp_path):
    bundle_dir = tmp_path / "bundle"
    _write_fixture(bundle_dir)

    update_agent_yaml_max_tokens(bundle_dir, max_tokens=8192)
    once = (bundle_dir / "agent.yaml").read_text()

    update_agent_yaml_max_tokens(bundle_dir, max_tokens=8192)
    twice = (bundle_dir / "agent.yaml").read_text()

    assert once == twice


# --- update_agent_yaml_memory_revision (belief-revision plan, Task 2) -----------
#
# The in-place backfill helper for any already-composed bundle whose
# `agent.yaml` predates the belief-revision feature (or that a caller wants
# to flip to a different revision mode without recomposing the whole
# bundle): adds or replaces the `revision` key on the existing `memory:`
# line, touching nothing else on that line (`personal`, `home`) or in the
# file -- a surgical text edit, same idiom as `update_agent_yaml_auth`.

_FIXTURE_AGENT_YAML_NO_MEMORY_REVISION = (
    "# agent.yaml -- generated by kobo, do not hand-edit lightly\n"
    "name: sam\n"
    "card: agent-card.json\n"
    "persona: persona.md\n"
    "model:\n"
    "  id: ${AGENT_MODEL:-vertex_ai/claude-sonnet-4-5}\n"
    "  max_tokens: 1024\n"
    "  budget: {per_task_tokens: 50000}\n"
    "transports:\n"
    "  - {kind: a2a, host: 0.0.0.0, port: 8080}\n"
    "tools: {provider: none, allow: []}\n"
    "memory: {personal: techne, home: /techne}\n"
    "checkpoints: sqlite\n"
    "approvals:\n"
    "  ceiling: {write: false}\n"
    '  gates: [{match: "*", require: plan_approval}]\n'
    "  escalation: {route: cli}\n"
)


def _write_memory_fixture(
    bundle_dir: Path, text: str = _FIXTURE_AGENT_YAML_NO_MEMORY_REVISION
) -> Path:
    bundle_dir.mkdir(parents=True, exist_ok=True)
    agent_yaml_path = bundle_dir / "agent.yaml"
    agent_yaml_path.write_text(text)
    return agent_yaml_path


def test_update_agent_yaml_memory_revision_adds_field_and_preserves_every_other_line(tmp_path):
    bundle_dir = tmp_path / "bundle"
    agent_yaml_path = _write_memory_fixture(bundle_dir)

    update_agent_yaml_memory_revision(bundle_dir, revision="operator")

    updated_lines = agent_yaml_path.read_text().splitlines(keepends=True)
    original_lines = _FIXTURE_AGENT_YAML_NO_MEMORY_REVISION.splitlines(keepends=True)
    memory_index = original_lines.index("memory: {personal: techne, home: /techne}\n")
    assert len(updated_lines) == len(original_lines)
    for i, (original, updated) in enumerate(zip(original_lines, updated_lines)):
        if i == memory_index:
            continue
        assert updated == original, f"line {i} changed unexpectedly"

    assert updated_lines[memory_index] == (
        "memory: {personal: techne, home: /techne, revision: operator}\n"
    )


def test_update_agent_yaml_memory_revision_replaces_an_existing_value(tmp_path):
    fixture_with_old_revision = _FIXTURE_AGENT_YAML_NO_MEMORY_REVISION.replace(
        "memory: {personal: techne, home: /techne}\n",
        "memory: {personal: techne, home: /techne, revision: old-mode}\n",
    )
    bundle_dir = tmp_path / "bundle"
    _write_memory_fixture(bundle_dir, fixture_with_old_revision)

    update_agent_yaml_memory_revision(bundle_dir, revision="operator")

    text = (bundle_dir / "agent.yaml").read_text()
    assert "old-mode" not in text
    data = yaml.safe_load(text)
    assert data["memory"] == {"personal": "techne", "home": "/techne", "revision": "operator"}
    # every other line is untouched
    other_lines = [
        line
        for line in fixture_with_old_revision.splitlines(keepends=True)
        if "memory:" not in line
    ]
    for line in other_lines:
        assert line in text


def test_update_agent_yaml_memory_revision_is_idempotent(tmp_path):
    bundle_dir = tmp_path / "bundle"
    _write_memory_fixture(bundle_dir)

    update_agent_yaml_memory_revision(bundle_dir, revision="operator")
    once = (bundle_dir / "agent.yaml").read_text()

    update_agent_yaml_memory_revision(bundle_dir, revision="operator")
    twice = (bundle_dir / "agent.yaml").read_text()

    assert once == twice


def test_update_agent_yaml_memory_revision_raises_when_no_memory_stanza(tmp_path):
    fixture_no_memory = _FIXTURE_AGENT_YAML_NO_MEMORY_REVISION.replace(
        "memory: {personal: techne, home: /techne}\n", ""
    )
    bundle_dir = tmp_path / "bundle"
    _write_memory_fixture(bundle_dir, fixture_no_memory)

    with pytest.raises(ClientError, match="memory") as exc_info:
        update_agent_yaml_memory_revision(bundle_dir, revision="operator")

    assert "memory" in str(exc_info.value)
    # unmodified on failure
    assert (bundle_dir / "agent.yaml").read_text() == fixture_no_memory


def test_update_agent_yaml_memory_revision_missing_file_raises_client_error(tmp_path):
    bundle_dir = tmp_path / "bundle"
    bundle_dir.mkdir(parents=True)

    with pytest.raises(ClientError, match="agent.yaml not found"):
        update_agent_yaml_memory_revision(bundle_dir, revision="operator")


def test_update_agent_yaml_memory_revision_on_freshly_composed_bundle_round_trips(tmp_path):
    """Integration: compose() already emits `revision: operator` (see the
    emission-matrix section above), so calling the backfill helper on a
    freshly composed bundle is itself an idempotent replace -- proving the
    helper and compose() agree on the exact shape they each produce."""
    dest = tmp_path / "out"
    compose(_make_spec(), dest)

    update_agent_yaml_memory_revision(dest / "bundle", revision="operator")

    validate_agent_yaml(dest / "bundle")  # must not raise
    data = yaml.safe_load((dest / "bundle" / "agent.yaml").read_text())
    assert data["memory"] == {"personal": "techne", "home": "/techne", "revision": "operator"}


# --- optional: validate against the real daedalus runtime, if available -----------


def test_bundle_loads_with_real_daedalus_if_available(tmp_path):
    daedalus = pytest.importorskip("daedalus")
    from daedalus.bundle import Bundle

    dest = tmp_path / "out"
    compose(_make_spec(), dest)

    bundle = Bundle.load(dest / "bundle", env={})

    assert bundle.spec.name == "sam"
    assert bundle.spec.model.id == "vertex_ai/claude-sonnet-4-5"
    assert bundle.spec.transports[0].kind == "a2a"
    assert bundle.spec.transports[0].options() == {"host": "0.0.0.0", "port": 8080}
    del daedalus  # only imported to prove importorskip's module handle works


# --- github sidecar (agent PR broker spec, 2026-08-27) ---------------------------


def test_tools_block_gains_github_sidecar_when_broker_url_set(tmp_path):
    dest = tmp_path / "out"
    compose(
        _make_spec(
            name="kestrel",
            cabal_url="http://cabal.example.com",
            broker_url="http://kobo.example.com",
        ),
        dest,
    )
    text = (dest / "bundle" / "agent.yaml").read_text()
    assert "github.open_pr" in text
    assert 'broker_url: "http://kobo.example.com"' in text
    assert "token_env: CABAL_COLLEAGUE_TOKEN" in text
    assert 'self: "kestrel"' in text


def test_tools_block_carries_no_github_without_broker_url(tmp_path):
    """cabal alone (today's every agent): byte-for-byte no github anywhere."""
    dest = tmp_path / "out"
    compose(_make_spec(name="kestrel", cabal_url="http://cabal.example.com"), dest)
    text = (dest / "bundle" / "agent.yaml").read_text()
    assert "github" not in text


def test_tools_block_ignores_broker_url_without_cabal(tmp_path):
    """A broker with no switchboard could never authenticate an agent (the
    broker verifies the cabal-issued JWT), so the sidecar never composes."""
    dest = tmp_path / "out"
    compose(_make_spec(name="kestrel", broker_url="http://kobo.example.com"), dest)
    text = (dest / "bundle" / "agent.yaml").read_text()
    assert "github" not in text
