"""Tests for kobo_api.slack_setup: manifest rendering, the KubectlCLI
secret-delivery argv, and wire_slack's fake-level repo/reprovision wiring.

No real subprocess/network/git anywhere: KubectlCLI's two-stage
`apply_secret` is exercised against a small local recording double for
CommandRunner (a local duplicate of test_clients.py's RecordingRunner idiom
rather than a cross-file import, since test_clients.py is outside this
task's file scope); wire_slack is exercised against tests/fakes.py's
FakeGitHub/FakeKubectl/FakeGojira.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from kobo_api.clients import ClientError, KubectlCLI
from kobo_api.composer import update_dockerfile_runtime_image
from kobo_api.slack_setup import (
    _update_agent_yaml_transports,
    render_manifest,
    rewire_slack_channels,
    wait_active,
    wire_slack,
)
from tests.fakes import (
    FAKE_DOCKERFILE_OLD_IMAGE,
    FAKE_LOCAL_HEAD_DEFAULT,
    FakeGitHub,
    FakeGojira,
    FakeKubectl,
)

REPO = "acme/agent-clio"
NAME = "clio"
BOT_TOKEN = "xoxb-super-secret-bot-token"
APP_TOKEN = "xapp-super-secret-app-token"
# The "current" configured runtime image every wire_slack test passes by
# default — deliberately distinct from FakeGitHub's own FAKE_DOCKERFILE_OLD_IMAGE
# default so a test that doesn't override either sees the FROM line actually
# change (proving the rewrite ran, not just that nothing happened to differ).
RUNTIME_IMAGE = "registry.example/agents/daedalus-runtime:new"


@pytest.fixture(autouse=True)
def _no_real_sleep(monkeypatch):
    """wait_active (direct, and via wire_slack) runs a poll loop — never let
    it sleep for real, same discipline as test_steps_wire.py's own fixture."""
    import kobo_api.slack_setup as slack_setup_module

    monkeypatch.setattr(slack_setup_module, "_sleep", lambda seconds: None)


def _onboarded_gojira(**kwargs) -> tuple[FakeGojira, int]:
    """A FakeGojira with one already-onboarded app (mirrors a READY agent:
    OnboardStep already ran create_app for it during original provisioning)."""
    gojira = FakeGojira(**kwargs)
    app_id = gojira.create_app(
        name=NAME, repo_full_name=REPO, cluster="example-cluster", namespace=NAME
    )
    return gojira, app_id


# --- render_manifest -----------------------------------------------------------


def test_render_manifest_scopes_byte_equal_to_constraint():
    manifest = render_manifest("echo", "A friendly test agent.")
    assert manifest["oauth_config"]["scopes"]["bot"] == [
        "app_mentions:read",
        "channels:history",
        "groups:history",
        "groups:read",
        "channels:read",
        "im:history",
        "im:read",
        "im:write",
        "mpim:history",
        "mpim:read",
        "chat:write",
        "reactions:read",
        "reactions:write",
    ]


def test_render_manifest_settings_and_events_exact():
    manifest = render_manifest("echo", "A friendly test agent.")
    assert manifest["settings"] == {
        "event_subscriptions": {
            "bot_events": [
                "app_mention",
                "message.channels",
                "message.groups",
                "message.im",
                "message.mpim",
            ]
        },
        "interactivity": {"is_enabled": False},
        "org_deploy_enabled": False,
        "socket_mode_enabled": True,
        "token_rotation_enabled": False,
    }


def test_render_manifest_name_and_bot_display_name_are_agent_name():
    manifest = render_manifest("echo", "A friendly test agent.")
    assert manifest["display_information"]["name"] == "echo"
    assert manifest["features"]["bot_user"]["display_name"] == "echo"


def test_render_manifest_description_is_persona_first_line():
    manifest = render_manifest("echo", "A friendly test agent.")
    assert manifest["display_information"]["description"] == "A friendly test agent."


def test_render_manifest_description_stripped():
    manifest = render_manifest("echo", "   padded   ")
    assert manifest["display_information"]["description"] == "padded"


def test_render_manifest_description_truncated_to_100_chars():
    manifest = render_manifest("echo", "A" * 150)
    description = manifest["display_information"]["description"]
    assert len(description) == 100
    assert description == "A" * 100


def test_render_manifest_description_under_100_chars_not_padded():
    manifest = render_manifest("echo", "short")
    assert manifest["display_information"]["description"] == "short"


# --- KubectlCLI.apply_secret: real-implementation argv (both stages) -----------


class _RecordingRunner:
    """Local duplicate of test_clients.py's RecordingRunner: records exact
    (argv, stdin) per call and replays canned stdout results in order (or
    reuses the last one for every subsequent call). Also records each call's
    `redact_args` flag separately (`redact_flags`) — final-review Finding 3 —
    without changing `calls`' existing 2-tuple shape, which pre-existing
    tests below already unpack."""

    def __init__(self, results: list[str] | None = None):
        self._results = list(results) if results is not None else [""]
        self.calls: list[tuple[list[str], str | None]] = []
        self.redact_flags: list[bool] = []

    def run(
        self, args: list[str], stdin: str | None = None, *, redact_args: bool = False
    ) -> str:
        self.calls.append((list(args), stdin))
        self.redact_flags.append(redact_args)
        if len(self._results) > 1:
            return self._results.pop(0)
        return self._results[0]


class _FailingRunner:
    """Mimics CommandRunner's real on-failure behavior (a ClientError whose
    message embeds the full argv it ran) for whichever call index
    `fail_at` names — the fixture proving apply_secret's own error handling
    strips literal secret values back out of a re-raised message rather
    than passing the argv-embedding message straight through."""

    def __init__(self, fail_at: int = 0):
        self._fail_at = fail_at
        self.calls: list[list[str]] = []

    def run(
        self, args: list[str], stdin: str | None = None, *, redact_args: bool = False
    ) -> str:
        index = len(self.calls)
        self.calls.append(list(args))
        if index == self._fail_at:
            raise ClientError(
                f"{' '.join(args)} failed (rc=1): error from server",
                returncode=1, stdout="", stderr="error from server",
            )
        return ""


def test_apply_secret_dry_run_create_argv():
    runner = _RecordingRunner(results=["<rendered secret yaml>", ""])
    client = KubectlCLI(runner=runner)

    client.apply_secret(
        NAME, f"{NAME}-slack", {"SLACK_BOT_TOKEN": BOT_TOKEN, "SLACK_APP_TOKEN": APP_TOKEN}
    )

    first_args, first_stdin = runner.calls[0]
    assert first_args == [
        "kubectl", "-n", NAME, "create", "secret", "generic", f"{NAME}-slack",
        f"--from-literal=SLACK_BOT_TOKEN={BOT_TOKEN}",
        f"--from-literal=SLACK_APP_TOKEN={APP_TOKEN}",
        "--dry-run=client", "-o", "yaml",
    ]
    assert first_stdin is None


def test_apply_secret_apply_stage_pipes_dry_run_output_as_stdin():
    runner = _RecordingRunner(results=["<rendered secret yaml>", ""])
    client = KubectlCLI(runner=runner)

    client.apply_secret(NAME, f"{NAME}-slack", {"SLACK_BOT_TOKEN": BOT_TOKEN})

    assert len(runner.calls) == 2
    second_args, second_stdin = runner.calls[1]
    assert second_args == ["kubectl", "apply", "-f", "-"]
    assert second_stdin == "<rendered secret yaml>"


def test_apply_secret_dry_run_failure_message_excludes_token_values():
    runner = _FailingRunner(fail_at=0)
    client = KubectlCLI(runner=runner)

    with pytest.raises(ClientError) as exc_info:
        client.apply_secret(NAME, f"{NAME}-slack", {"SLACK_BOT_TOKEN": BOT_TOKEN})

    assert BOT_TOKEN not in str(exc_info.value)


def test_apply_secret_apply_stage_failure_message_excludes_token_values():
    runner = _FailingRunner(fail_at=1)
    client = KubectlCLI(runner=runner)

    with pytest.raises(ClientError) as exc_info:
        client.apply_secret(NAME, f"{NAME}-slack", {"SLACK_BOT_TOKEN": BOT_TOKEN})

    assert BOT_TOKEN not in str(exc_info.value)


def test_apply_secret_passes_redact_args_true_on_both_stages():
    # Final-review Finding 3: apply_secret's stage-1 argv is the only place
    # either token literal ever appears — it (and, defense-in-depth,
    # stage-2) must ask CommandRunner.run to redact its debug-log argv
    # rather than relying on the default (which logs it in full).
    runner = _RecordingRunner(results=["<rendered secret yaml>", ""])
    client = KubectlCLI(runner=runner)

    client.apply_secret(NAME, f"{NAME}-slack", {"SLACK_BOT_TOKEN": BOT_TOKEN})

    assert runner.redact_flags == [True, True]


# --- wait_active -----------------------------------------------------------------


def test_wait_active_returns_once_status_is_active():
    gojira, app_id = _onboarded_gojira(activate_after=1)
    wait_active(gojira, app_id)  # must not raise
    assert gojira.app_status(app_id)["status"] == "active"


def test_wait_active_raises_client_error_on_failed_naming_failed_steps():
    # ClientError, not bare RuntimeError: connect_slack's route only catches
    # ClientError, and ClientError subclasses RuntimeError (not the other
    # way around) — see test_api_slack.py's route-level coverage of this.
    gojira, app_id = _onboarded_gojira(
        activate_after=1,
        end_status="failed",
        failed_steps=[{"name": "wire", "status": "failed"}],
    )

    with pytest.raises(ClientError, match="failed steps: wire"):
        wait_active(gojira, app_id)


def test_wait_active_raises_client_error_on_timeout():
    # activate_after far beyond the 60-poll budget (300s / 5s) — app_status
    # stays "pending" for the whole loop.
    gojira, app_id = _onboarded_gojira(activate_after=10_000)

    with pytest.raises(ClientError, match="timed out waiting for gojira app"):
        wait_active(gojira, app_id)


# --- _update_agent_yaml_transports ---------------------------------------------


def test_transports_one_entry_per_channel_preserving_others(tmp_path):
    path = tmp_path / "agent.yaml"
    path.write_text(yaml.safe_dump({
        "transports": [
            {"kind": "a2a", "port": 9200},
            {"kind": "slack", "channel": "COLD000001"},
        ]
    }, sort_keys=False))
    _update_agent_yaml_transports(path, ["CNEW0000001", "CNEW0000002"])
    data = yaml.safe_load(path.read_text())
    kinds = [t["kind"] for t in data["transports"]]
    assert kinds == ["a2a", "slack", "slack"]
    assert [t["channel"] for t in data["transports"] if t["kind"] == "slack"] == [
        "CNEW0000001",
        "CNEW0000002",
    ]


def test_transports_empty_list_removes_slack_only(tmp_path):
    path = tmp_path / "agent.yaml"
    path.write_text(yaml.safe_dump({
        "transports": [
            {"kind": "a2a", "port": 9200},
            {"kind": "slack", "channel": "COLD000001"},
        ]
    }, sort_keys=False))
    _update_agent_yaml_transports(path, [])
    data = yaml.safe_load(path.read_text())
    assert [t["kind"] for t in data["transports"]] == ["a2a"]


# --- wire_slack --------------------------------------------------------------------


# FakeGitHub.clone only writes bundle/agent.yaml when agent_yaml_transports
# is given (default None => no bundle/ tree at all, so bare FakeGitHub()
# stays inert for every pre-existing non-wire_slack test). Every wire_slack
# test needs the file on disk to edit, so _CapturingGitHub below defaults
# the knob to a real bundle's starting shape (matching kobo_api.composer's
# rendering) unless a test passes its own transports list explicitly.
_DEFAULT_AGENT_YAML_TRANSPORTS = [{"kind": "a2a", "host": "0.0.0.0", "port": 8080}]


class _CapturingGitHub(FakeGitHub):
    """Snapshots bundle/agent.yaml and gojira.yaml content at the moment
    commit_push is called — the only window in which wire_slack's cloned
    workdir still exists before it's discarded. Same trick as
    test_api_knowledge.py's own _CapturingGitHub, duplicated locally rather
    than imported cross-file."""

    def __init__(self, agent_yaml_transports=None, **kwargs):
        if agent_yaml_transports is None:
            agent_yaml_transports = _DEFAULT_AGENT_YAML_TRANSPORTS
        super().__init__(agent_yaml_transports=agent_yaml_transports, **kwargs)
        self.captured_agent_yaml = ""
        self.captured_gojira_yaml = ""
        self.captured_dockerfile = ""

    def commit_push(self, workdir: Path, message: str) -> bool:
        self.captured_agent_yaml = (workdir / "bundle" / "agent.yaml").read_text()
        self.captured_gojira_yaml = (workdir / "gojira.yaml").read_text()
        self.captured_dockerfile = (workdir / "Dockerfile").read_text()
        return super().commit_push(workdir, message)


def _wire(
    github=None, kubectl=None, gojira=None, app_id=None, channels=None, **overrides
):
    github = github if github is not None else _CapturingGitHub()
    kubectl = kubectl if kubectl is not None else FakeKubectl()
    if gojira is None:
        gojira, app_id = _onboarded_gojira()
    if channels is None:
        channels = ["C1234567"]
    kwargs = {
        "repo": REPO,
        "name": NAME,
        "gojira_app_id": app_id,
        "channels": channels,
        "bot_token": BOT_TOKEN,
        "app_token": APP_TOKEN,
        "runtime_image": RUNTIME_IMAGE,
    }
    kwargs.update(overrides)
    sha = wire_slack(github, kubectl, gojira, **kwargs)
    return sha, github, kubectl, gojira


def test_wire_slack_applies_secret_with_both_tokens():
    kubectl = FakeKubectl()
    _wire(kubectl=kubectl)

    assert kubectl.secrets_applied == [
        (NAME, f"{NAME}-slack", {"SLACK_BOT_TOKEN": BOT_TOKEN, "SLACK_APP_TOKEN": APP_TOKEN})
    ]


def test_wire_slack_adds_slack_transport_preserving_a2a():
    github = _CapturingGitHub()
    _wire(github=github, channels=["C1234567"])

    data = yaml.safe_load(github.captured_agent_yaml)
    assert data["transports"] == [
        {"kind": "a2a", "host": "0.0.0.0", "port": 8080},
        {"kind": "slack", "channel": "C1234567"},
    ]


def test_wire_slack_idempotently_replaces_existing_slack_transport():
    github = _CapturingGitHub(
        agent_yaml_transports=[
            {"kind": "a2a", "host": "0.0.0.0", "port": 8080},
            {"kind": "slack", "channel": "COLDOLD1"},
        ]
    )
    _wire(github=github, channels=["CNEWNEW1"])

    data = yaml.safe_load(github.captured_agent_yaml)
    assert data["transports"] == [
        {"kind": "a2a", "host": "0.0.0.0", "port": 8080},
        {"kind": "slack", "channel": "CNEWNEW1"},
    ]


def test_wire_slack_two_channels_produce_two_transport_entries():
    github = _CapturingGitHub()
    _wire(github=github, channels=["C1111111", "C2222222"])

    data = yaml.safe_load(github.captured_agent_yaml)
    assert data["transports"] == [
        {"kind": "a2a", "host": "0.0.0.0", "port": 8080},
        {"kind": "slack", "channel": "C1111111"},
        {"kind": "slack", "channel": "C2222222"},
    ]


def test_wire_slack_appends_gojira_secrets_from_when_absent():
    github = _CapturingGitHub()
    _wire(github=github)

    data = yaml.safe_load(github.captured_gojira_yaml)
    assert data["secrets_from"] == [f"{NAME}-slack"]


def test_wire_slack_appends_gojira_secrets_from_without_clobbering_existing():
    github = _CapturingGitHub(gojira_secrets_from=["other-secret"])
    _wire(github=github)

    data = yaml.safe_load(github.captured_gojira_yaml)
    assert data["secrets_from"] == ["other-secret", f"{NAME}-slack"]


def test_wire_slack_gojira_secrets_from_append_is_idempotent():
    github = _CapturingGitHub(gojira_secrets_from=[f"{NAME}-slack"])
    _wire(github=github)

    data = yaml.safe_load(github.captured_gojira_yaml)
    assert data["secrets_from"] == [f"{NAME}-slack"]


def test_wire_slack_commit_message_exact():
    github = _CapturingGitHub()
    _wire(github=github)

    assert github.pushes[-1][1] == "kobo: connect slack"


def test_wire_slack_returns_local_head():
    sha, github, _, _ = _wire()

    assert sha == FAKE_LOCAL_HEAD_DEFAULT
    assert github.local_head_calls  # local_head was actually called


def test_wire_slack_reprovisions_when_push_changed():
    github = _CapturingGitHub(commit_push_results=[True])
    gojira, app_id = _onboarded_gojira()

    _wire(github=github, gojira=gojira, app_id=app_id)

    assert gojira.reprovision_calls == [app_id]


def test_wire_slack_skips_reprovision_but_still_waits_active_when_no_change():
    github = _CapturingGitHub(commit_push_results=[False])
    gojira, app_id = _onboarded_gojira()

    _wire(github=github, gojira=gojira, app_id=app_id)

    assert gojira.reprovision_calls == []
    # wait_active must still have polled app_status at least once (the
    # crash-window rationale from WireStep, carried over verbatim) —
    # app_status calls are the only "calls" FakeGojira records that aren't
    # create_app/reprovision.
    assert any(call.startswith("app_status:") for call in gojira.calls)


def test_wire_slack_tokens_absent_from_every_file_written_to_the_clone():
    github = _CapturingGitHub()
    _wire(github=github)

    assert BOT_TOKEN not in github.captured_agent_yaml
    assert APP_TOKEN not in github.captured_agent_yaml
    assert BOT_TOKEN not in github.captured_gojira_yaml
    assert APP_TOKEN not in github.captured_gojira_yaml


# --- rewire_slack_channels -----------------------------------------------------


# rewire_slack_channels takes no kubectl/gojira argument at all (unlike
# wire_slack) — there is no FakeKubectl/FakeGojira for it to touch even by
# mistake, so that half of "commit-only" is enforced by the signature
# itself, not by an assertion below.


def test_rewire_slack_channels_pushes_two_transport_entries_and_returns_sha():
    github = _CapturingGitHub()

    sha = rewire_slack_channels(github, repo=REPO, channels=["CNEW0000001", "CNEW0000002"])

    data = yaml.safe_load(github.captured_agent_yaml)
    assert data["transports"] == [
        {"kind": "a2a", "host": "0.0.0.0", "port": 8080},
        {"kind": "slack", "channel": "CNEW0000001"},
        {"kind": "slack", "channel": "CNEW0000002"},
    ]
    assert sha == FAKE_LOCAL_HEAD_DEFAULT
    assert github.local_head_calls  # local_head was actually called


def test_rewire_slack_channels_commit_message_exact():
    github = _CapturingGitHub()

    rewire_slack_channels(github, repo=REPO, channels=["C1234567"])

    assert github.pushes[-1][1] == "kobo: slack channels updated"


def test_rewire_slack_channels_touches_only_agent_yaml():
    # gojira.yaml and Dockerfile come back byte-identical to what clone
    # wrote — proof this is commit-only: no secrets_from edit, no runtime
    # image re-pin, no kubectl/gojira mechanics of any kind.
    github = _CapturingGitHub()

    rewire_slack_channels(github, repo=REPO, channels=["C1234567"])

    assert "secrets_from" not in yaml.safe_load(github.captured_gojira_yaml)
    assert github.captured_dockerfile == _DOCKERFILE_TEXT.format(image=FAKE_DOCKERFILE_OLD_IMAGE)


def test_rewire_slack_channels_empty_list_removes_slack_transport():
    github = _CapturingGitHub(
        agent_yaml_transports=[
            {"kind": "a2a", "host": "0.0.0.0", "port": 8080},
            {"kind": "slack", "channel": "COLD000001"},
        ]
    )

    rewire_slack_channels(github, repo=REPO, channels=[])

    data = yaml.safe_load(github.captured_agent_yaml)
    assert data["transports"] == [{"kind": "a2a", "host": "0.0.0.0", "port": 8080}]


# --- update_dockerfile_runtime_image (unit-level) -----------------------------------
#
# The runtime-bump incident fix: a repo's Dockerfile pins its base image at
# compose time; wire_slack must re-pin it to the CURRENT configured runtime
# image on every connect, so a stale base can never ship without the
# transport extra it needs. Exercised directly against a tmp_path Dockerfile
# first (no fake/clone involved) to pin the exact byte-level behavior, then
# through wire_slack itself below. The helper itself now lives in
# kobo_api.composer (hoisted so kobo_api.intake.upload_intake_file can reuse
# it for the sibling incident there) — imported from there, not from
# kobo_api.slack_setup.


_DOCKERFILE_TEXT = (
    "# Agent image: the shared runtime plus this agent's bundle.\n"
    "FROM {image}\n"
    "COPY bundle/ /bundle\n"
)


def test_update_dockerfile_runtime_image_rewrites_from_preserves_rest(tmp_path):
    dockerfile = tmp_path / "Dockerfile"
    dockerfile.write_text(_DOCKERFILE_TEXT.format(image=FAKE_DOCKERFILE_OLD_IMAGE))

    update_dockerfile_runtime_image(dockerfile, RUNTIME_IMAGE)

    assert dockerfile.read_text() == _DOCKERFILE_TEXT.format(image=RUNTIME_IMAGE)


def test_update_dockerfile_runtime_image_idempotent_when_already_current(tmp_path):
    dockerfile = tmp_path / "Dockerfile"
    text = _DOCKERFILE_TEXT.format(image=RUNTIME_IMAGE)
    dockerfile.write_text(text)

    update_dockerfile_runtime_image(dockerfile, RUNTIME_IMAGE)

    assert dockerfile.read_text() == text


def test_update_dockerfile_runtime_image_only_touches_from_line(tmp_path):
    # A Dockerfile with content beyond composer's minimal 3-line template
    # (extra comments/instructions before and after FROM) proves the edit
    # only ever touches the FROM line itself, never anything else in the
    # file.
    dockerfile = tmp_path / "Dockerfile"
    dockerfile.write_text(
        "# leading comment, unrelated to the runtime image\n"
        f"FROM {FAKE_DOCKERFILE_OLD_IMAGE}\n"
        "COPY bundle/ /bundle\n"
        "# trailing comment\n"
        'ENTRYPOINT ["/bin/run-agent"]\n'
    )

    update_dockerfile_runtime_image(dockerfile, RUNTIME_IMAGE)

    assert dockerfile.read_text() == (
        "# leading comment, unrelated to the runtime image\n"
        f"FROM {RUNTIME_IMAGE}\n"
        "COPY bundle/ /bundle\n"
        "# trailing comment\n"
        'ENTRYPOINT ["/bin/run-agent"]\n'
    )


def test_update_dockerfile_runtime_image_raises_client_error_when_no_from_line(tmp_path):
    # Final-review Finding 2: an unrecognized Dockerfile shape (no FROM line
    # at all) used to make re.sub's zero-match substitution a silent no-op —
    # the file got written back byte-identical and the caller had no signal
    # the re-pin never happened. Must raise ClientError instead, naming the
    # problem, so it surfaces as connect_slack's fixed 502 rather than a
    # stale-base crashloop discovered later with no trail back to its cause.
    dockerfile = tmp_path / "Dockerfile"
    dockerfile.write_text("# no FROM line in this file\nCOPY bundle/ /bundle\n")

    with pytest.raises(ClientError, match="no recognizable FROM line"):
        update_dockerfile_runtime_image(dockerfile, RUNTIME_IMAGE)

    # Untouched: the whole point is not to silently rewrite it.
    assert dockerfile.read_text() == "# no FROM line in this file\nCOPY bundle/ /bundle\n"


def test_update_dockerfile_runtime_image_raises_client_error_when_dockerfile_missing(tmp_path):
    # Same idiom as kobo_api.clients._read_slack_tokens: a missing file
    # raises ClientError naming only the path, not a raw FileNotFoundError/
    # OSError escaping to a caller that only expects ClientError.
    missing = tmp_path / "Dockerfile"

    with pytest.raises(ClientError, match="Dockerfile not found"):
        update_dockerfile_runtime_image(missing, RUNTIME_IMAGE)


# --- wire_slack: Dockerfile runtime-image rewrite -----------------------------------


def test_wire_slack_rewrites_dockerfile_from_line_to_runtime_image():
    github = _CapturingGitHub()  # defaults to FAKE_DOCKERFILE_OLD_IMAGE
    _wire(github=github, runtime_image=RUNTIME_IMAGE)

    assert github.captured_dockerfile == _DOCKERFILE_TEXT.format(image=RUNTIME_IMAGE)


def test_wire_slack_dockerfile_already_current_is_byte_identical():
    github = _CapturingGitHub(dockerfile_image=RUNTIME_IMAGE)
    _wire(github=github, runtime_image=RUNTIME_IMAGE)

    assert github.captured_dockerfile == _DOCKERFILE_TEXT.format(image=RUNTIME_IMAGE)


def test_wire_slack_tokens_absent_from_dockerfile():
    github = _CapturingGitHub()
    _wire(github=github)

    assert BOT_TOKEN not in github.captured_dockerfile
    assert APP_TOKEN not in github.captured_dockerfile
