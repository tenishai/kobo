"""Tests for kobo_api.agent_env: the key rules every secret and every
environment variable an owner adds has to pass, the two `gojira.yaml`
edits the panel's writes make, and the one flow that gets those edits into
a repository -- committed for an agent the room composed, proposed as a
pull request for one it only deploys.

Pure functions over plain dicts, plus the flow against `FakeGitHub`/
`FakeGojira` -- no real repository, no platform, no lane.
"""

from __future__ import annotations

import pytest
import yaml

from kobo_api.agent_env import (
    COMMITTED,
    ENV_PR_BODY,
    MAX_KEYS,
    PULL_REQUEST,
    ROOM_SETS_THIS_ONE,
    UNCHANGED,
    apply_env,
    edit_gojira_yaml,
    env_secret_name,
    key_refusal,
    merged_names,
    read_env,
    secret_path,
    wire_env_secret,
    write_refusal,
)
from kobo_api.clients import ClientError
from tests.fakes import FakeGitHub, FakeGojira

AGENT = "clio"


# --- one key at a time --------------------------------------------------------


@pytest.mark.parametrize(
    "key", ["A", "API_KEY", "DB_URL", "X1", "LONG_NAME_WITH_9_DIGITS", "A" * 64]
)
def test_a_name_that_follows_the_rule_is_accepted(key):
    assert key_refusal(key) is None


@pytest.mark.parametrize(
    "key",
    [
        "",
        "lower",
        "Mixed",
        "1LEADING",
        "_LEADING",
        "HAS-HYPHEN",
        "HAS SPACE",
        "HAS.DOT",
        "TRAILING=",
    ],
)
def test_a_name_outside_the_rule_is_refused_by_name(key):
    refusal = key_refusal(key)
    assert refusal is not None
    assert "capital letters" in refusal


def test_a_name_longer_than_sixty_four_characters_is_refused():
    refusal = key_refusal("A" * 65)
    assert refusal is not None
    assert "64" in refusal


@pytest.mark.parametrize(
    "key",
    [
        "CABAL_COLLEAGUE_TOKEN",
        "SLACK_BOT_TOKEN",
        "SLACK_APP_TOKEN",
        "SLACK_SIGNING_SECRET",
        "VERTEXAI_PROJECT",
        "VERTEXAI_LOCATION",
    ],
)
def test_a_name_the_room_manages_is_refused_with_the_rooms_own_sentence(key):
    refusal = key_refusal(key)
    assert refusal is not None
    assert ROOM_SETS_THIS_ONE in refusal
    assert key in refusal


def test_no_refusal_sentence_carries_an_em_dash():
    # These land in front of a person. Plain punctuation, same as every
    # other user-facing sentence in this room.
    sentences = [
        key_refusal("lower"),
        key_refusal("A" * 65),
        key_refusal("SLACK_BOT_TOKEN"),
        write_refusal({"A": "1"}, ["A"], []),
        write_refusal({f"K{i}": "1" for i in range(MAX_KEYS + 1)}, [], []),
    ]
    for sentence in sentences:
        assert sentence is not None
        assert "—" not in sentence


# --- a whole write ------------------------------------------------------------


def test_a_write_that_breaks_no_rule_is_accepted():
    assert write_refusal({"API_KEY": "v"}, ["OLD_KEY"], ["OLD_KEY"]) is None


def test_a_write_naming_one_bad_key_is_refused_naming_that_key():
    refusal = write_refusal({"API_KEY": "v", "bad-key": "v"}, [], [])
    assert refusal is not None
    assert "bad-key" in refusal


def test_a_key_being_removed_is_checked_the_same_way():
    refusal = write_refusal({}, ["SLACK_BOT_TOKEN"], [])
    assert refusal is not None
    assert ROOM_SETS_THIS_ONE in refusal


def test_a_key_on_both_sides_of_the_write_is_refused():
    refusal = write_refusal({"API_KEY": "v"}, ["API_KEY"], [])
    assert refusal is not None
    assert "API_KEY" in refusal
    assert "both" in refusal


def test_a_write_that_would_pass_the_key_cap_is_refused():
    existing = [f"K{i}" for i in range(MAX_KEYS)]
    refusal = write_refusal({"ONE_MORE": "v"}, [], existing)
    assert refusal is not None
    assert str(MAX_KEYS) in refusal


def test_the_cap_counts_what_the_write_leaves_behind_not_what_it_adds():
    # Swapping one key for another at the cap is fine: the list does not
    # grow. A cap that counted only additions would refuse it.
    existing = [f"K{i}" for i in range(MAX_KEYS)]
    assert write_refusal({"ONE_MORE": "v"}, ["K0"], existing) is None


def test_a_write_that_replaces_an_existing_key_does_not_count_it_twice():
    existing = [f"K{i}" for i in range(MAX_KEYS)]
    assert write_refusal({"K0": "a new value"}, [], existing) is None


def test_removing_a_key_that_is_not_there_is_not_a_refusal():
    # Idempotent by design: a second remove of the same key is the same
    # answer as the first, so a retried save never fails on work already
    # done.
    assert write_refusal({}, ["GONE"], []) is None


# --- what a write leaves behind -----------------------------------------------


def test_merged_names_adds_removes_and_sorts():
    assert merged_names(["B", "A"], {"C": "v"}, ["A"]) == ["B", "C"]


def test_merged_names_never_repeats_a_key_a_write_replaced():
    assert merged_names(["A"], {"A": "v"}, []) == ["A"]


# --- the path and the Secret name ---------------------------------------------


def test_the_path_and_the_secret_name_follow_the_rooms_existing_pattern():
    # `<agent>-cabal` and `<agent>-slack` already exist; this is the third.
    assert secret_path(AGENT) == f"apps/{AGENT}/env"
    assert env_secret_name(AGENT) == f"{AGENT}-env"


# --- gojira.yaml: the plain environment variables -----------------------------


def test_read_env_of_a_file_with_no_env_key_is_empty():
    assert read_env({}) == {}


def test_read_env_drops_the_two_model_keys_the_room_sets_itself():
    data = {
        "env": {
            "VERTEXAI_PROJECT": "a-project",
            "VERTEXAI_LOCATION": "somewhere",
            "OWN_KEY": "own value",
        }
    }
    assert read_env(data) == {"OWN_KEY": "own value"}


def test_read_env_of_a_non_mapping_env_is_empty_rather_than_a_crash():
    # A file the deploy platform could never have run. Nothing here to
    # preserve, and nothing worth raising over on a read.
    assert read_env({"env": ["not", "a", "mapping"]}) == {}


def test_read_env_renders_a_non_string_value_as_text():
    # YAML reads `PORT: 8080` as an int and `DEBUG: true` as a bool. The
    # panel edits text, so what it is shown has to be text.
    assert read_env({"env": {"PORT": 8080, "DEBUG": True}}) == {
        "PORT": "8080",
        "DEBUG": "true",
    }


def test_apply_env_adds_removes_and_keeps_the_model_keys():
    data = {
        "env": {
            "VERTEXAI_PROJECT": "a-project",
            "OLD": "gone soon",
            "KEPT": "still here",
        }
    }
    changed = apply_env(data, {"NEW": "fresh"}, ["OLD"])

    assert changed is True
    assert data["env"] == {
        "VERTEXAI_PROJECT": "a-project",
        "KEPT": "still here",
        "NEW": "fresh",
    }


def test_apply_env_reports_no_change_when_the_file_already_says_this():
    data = {"env": {"KEPT": "still here"}}
    assert apply_env(data, {"KEPT": "still here"}, ["GONE"]) is False
    assert data["env"] == {"KEPT": "still here"}


def test_apply_env_replaces_a_non_mapping_env_rather_than_reading_it():
    data = {"env": "not a mapping"}
    assert apply_env(data, {"NEW": "fresh"}, []) is True
    assert data["env"] == {"NEW": "fresh"}


def test_apply_env_never_lets_a_write_reach_a_key_the_room_sets():
    # The route refuses this first (`write_refusal`), so this is the second
    # wall rather than the first: nothing that gets here can overwrite the
    # model wiring even if a caller found a way past the route.
    data = {"env": {"VERTEXAI_PROJECT": "a-project"}}
    apply_env(data, {"VERTEXAI_PROJECT": "somewhere else"}, [])
    assert data["env"]["VERTEXAI_PROJECT"] == "a-project"


def test_apply_env_cannot_remove_a_key_the_room_sets_either():
    data = {"env": {"VERTEXAI_LOCATION": "somewhere"}}
    apply_env(data, {}, ["VERTEXAI_LOCATION"])
    assert data["env"] == {"VERTEXAI_LOCATION": "somewhere"}


# --- gojira.yaml: the one-time secrets_from entry -----------------------------


def test_wire_env_secret_appends_the_entry_once():
    data = {"secrets_from": [f"{AGENT}-cabal"]}
    assert wire_env_secret(data, AGENT) is True
    assert data["secrets_from"] == [f"{AGENT}-cabal", f"{AGENT}-env"]


def test_wire_env_secret_is_a_no_op_the_second_time():
    data = {"secrets_from": [f"{AGENT}-env"]}
    assert wire_env_secret(data, AGENT) is False
    assert data["secrets_from"] == [f"{AGENT}-env"]


def test_wire_env_secret_never_replaces_what_is_already_in_the_list():
    data = {"secrets_from": ["something-a-team-put-here"]}
    wire_env_secret(data, AGENT)
    assert data["secrets_from"] == ["something-a-team-put-here", f"{AGENT}-env"]


def test_wire_env_secret_on_a_file_with_no_list_yet():
    data: dict = {}
    assert wire_env_secret(data, AGENT) is True
    assert data["secrets_from"] == [f"{AGENT}-env"]


def test_both_edits_survive_a_yaml_round_trip():
    data = yaml.safe_load("service_account: agent-clio\nenv:\n  KEPT: here\n")
    apply_env(data, {"NEW": "fresh"}, [])
    wire_env_secret(data, AGENT)
    again = yaml.safe_load(yaml.safe_dump(data, sort_keys=False))

    assert again["service_account"] == "agent-clio"
    assert again["env"] == {"KEPT": "here", "NEW": "fresh"}
    assert again["secrets_from"] == [f"{AGENT}-env"]


# --- getting an edit into the agent's repository ------------------------------


REPO = "example-org/agent-clio"


APP_ID = 1


def _gojira() -> FakeGojira:
    """A platform that already knows this agent's app, so a reprovision
    lands on something rather than raising."""
    gojira = FakeGojira()
    gojira.create_app(
        name=AGENT, repo_full_name=REPO, cluster="a-cluster", namespace=AGENT
    )
    return gojira


def _edit_env(values, remove=()):
    return lambda data: apply_env(data, values, list(remove))


def test_a_composed_agents_change_is_committed_and_the_platform_is_asked_to_rebuild():
    github = FakeGitHub(existing_repos={REPO})
    gojira = _gojira()

    result = edit_gojira_yaml(
        github,
        gojira,
        repo=REPO,
        connected=False,
        gojira_app_id=APP_ID,
        edit=_edit_env({"API_BASE": "https://example.test"}),
        message="kobo: environment variables",
    )

    assert result.how == COMMITTED
    assert result.pull_request is None
    assert github.pushes, "the change was never pushed"
    assert gojira.reprovision_calls == [APP_ID]
    pushed = yaml.safe_load(github.pushed_file("gojira.yaml"))
    assert pushed["env"]["API_BASE"] == "https://example.test"


def test_a_composed_agents_change_that_is_already_there_pushes_nothing():
    github = FakeGitHub(
        existing_repos={REPO},
        extra_clone_files={"gojira.yaml": "name: clio\nenv:\n  API_BASE: https://example.test\n"},
    )
    gojira = _gojira()

    result = edit_gojira_yaml(
        github,
        gojira,
        repo=REPO,
        connected=False,
        gojira_app_id=APP_ID,
        edit=_edit_env({"API_BASE": "https://example.test"}),
        message="kobo: environment variables",
    )

    assert result.how == UNCHANGED
    assert github.pushes == []
    assert gojira.reprovision_calls == []


def test_a_connected_agents_change_is_proposed_never_pushed():
    github = FakeGitHub(existing_repos={REPO}, pr_url="https://example.test/pr/1")
    gojira = _gojira()

    result = edit_gojira_yaml(
        github,
        gojira,
        repo=REPO,
        connected=True,
        gojira_app_id=APP_ID,
        edit=_edit_env({"API_BASE": "https://example.test"}),
        message="kobo: environment variables",
    )

    assert result.how == PULL_REQUEST
    assert result.pull_request == "https://example.test/pr/1"
    assert github.pushes == []
    # Nothing is regenerated until a person merges it.
    assert gojira.reprovision_calls == []
    assert github.open_pr_calls[0]["branch"] == "kobo/env"


def test_a_connected_agents_second_change_refuses_while_one_is_open():
    github = FakeGitHub(
        existing_repos={REPO}, open_prs={f"{REPO}#kobo/env": "https://example.test/pr/1"}
    )

    with pytest.raises(ClientError) as exc:
        edit_gojira_yaml(
            github,
            _gojira(),
            repo=REPO,
            connected=True,
            gojira_app_id=APP_ID,
            edit=_edit_env({"SECOND": "change"}),
            message="kobo: environment variables",
        )

    assert "https://example.test/pr/1" in str(exc.value)
    assert github.open_pr_calls == []


def test_the_one_line_wiring_edit_reuses_a_pull_request_that_already_carries_it():
    # Appending `<agent>-env` to secrets_from is always the same one line,
    # so an open request already proposes it. Refusing here would fail a
    # secret write whose value has already been stored.
    github = FakeGitHub(
        existing_repos={REPO}, open_prs={f"{REPO}#kobo/env": "https://example.test/pr/1"}
    )

    result = edit_gojira_yaml(
        github,
        _gojira(),
        repo=REPO,
        connected=True,
        gojira_app_id=APP_ID,
        edit=lambda data: wire_env_secret(data, AGENT),
        message="kobo: secrets wiring",
        reuse_open_pr=True,
    )

    assert result.how == PULL_REQUEST
    assert result.pull_request == "https://example.test/pr/1"
    assert github.open_pr_calls == []


def test_a_leftover_branch_with_no_pull_request_is_named_rather_than_pushed_over():
    github = FakeGitHub(existing_repos={REPO}, existing_branches={f"{REPO}#kobo/env"})

    with pytest.raises(ClientError) as exc:
        edit_gojira_yaml(
            github,
            _gojira(),
            repo=REPO,
            connected=True,
            gojira_app_id=APP_ID,
            edit=_edit_env({"API_BASE": "https://example.test"}),
            message="kobo: environment variables",
        )

    assert "kobo/env" in str(exc.value)
    assert github.open_pr_calls == []


def test_a_connected_agents_change_that_is_already_there_proposes_nothing():
    github = FakeGitHub(
        existing_repos={REPO},
        extra_clone_files={"gojira.yaml": "name: clio\nenv:\n  API_BASE: https://example.test\n"},
    )

    result = edit_gojira_yaml(
        github,
        _gojira(),
        repo=REPO,
        connected=True,
        gojira_app_id=APP_ID,
        edit=_edit_env({"API_BASE": "https://example.test"}),
        message="kobo: environment variables",
    )

    assert result.how == UNCHANGED
    assert github.open_pr_calls == []
    assert f"find_pr:{REPO}:kobo/env" not in github.calls


def test_a_repository_with_no_gojira_yaml_is_a_plain_sentence_not_a_crash():
    github = FakeGitHub(existing_repos={REPO}, clone_tree={"README.md": "nothing here\n"})

    with pytest.raises(ClientError) as exc:
        edit_gojira_yaml(
            github,
            _gojira(),
            repo=REPO,
            connected=False,
            gojira_app_id=APP_ID,
            edit=_edit_env({"API_BASE": "https://example.test"}),
            message="kobo: environment variables",
        )

    assert "gojira.yaml" in str(exc.value)


def test_the_pull_request_body_promises_no_secret_value_is_in_the_file():
    assert "VALUES are not here" in ENV_PR_BODY
    assert "—" not in ENV_PR_BODY
