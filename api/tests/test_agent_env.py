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
    CONTAINER_SETS_THIS_ONE,
    ENV_BRANCH,
    ENV_PR_ALREADY_OPEN,
    ENV_PR_BODY,
    LIVE,
    MANAGED_PURPOSES,
    MAX_KEYS,
    PULL_REQUEST,
    ROOM_SETS_THIS_ONE,
    SECRETS_UNWIRE_PR_BODY,
    SECRETS_WIRING_BRANCH,
    SECRETS_WIRING_PR_BODY,
    SECRETS_WIRING_PR_TITLE,
    SYNC_ERROR,
    SYNC_FAILED,
    SYNC_NOT_DELIVERED,
    SYNC_UNKNOWN,
    SYNCING,
    UNCHANGED,
    UNREADABLE_GOJIRA_YAML,
    UNREADABLE_REPOSITORY,
    UNWIRED,
    WIRING_PR_ALREADY_OPEN,
    DeployTriggerFailed,
    RepoBusy,
    apply_env,
    branch_not_ours,
    edit_gojira_yaml,
    env_secret_name,
    has_env_secret,
    key_refusal,
    managed_secret_name,
    managed_secret_path,
    merged_names,
    read_env,
    read_repo_env,
    secret_path,
    sync_state,
    sync_unknown,
    unwire_env_secret,
    wire_env_secret,
    write_refusal,
)
from kobo_api.clients import COMMIT_AUTHOR_EMAIL, BranchComparison, ClientError
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


@pytest.mark.parametrize(
    "key",
    ["PATH", "HOME", "PYTHONPATH", "PYTHONHOME", "LD_PRELOAD", "LD_LIBRARY_PATH"],
)
def test_a_name_the_container_owns_is_refused_with_its_own_sentence(key):
    # A different fact from "the room sets this one", and a different fix:
    # nobody here decided these, the image and the runtime did, and
    # overwriting one gives an agent that cannot find its own interpreter
    # rather than a custom variable.
    refusal = key_refusal(key)
    assert refusal is not None
    assert CONTAINER_SETS_THIS_ONE in refusal
    assert key in refusal


def test_a_container_name_is_refused_on_a_whole_write_too_either_side():
    assert CONTAINER_SETS_THIS_ONE in (write_refusal({"PATH": "/tmp"}, [], []) or "")
    assert CONTAINER_SETS_THIS_ONE in (write_refusal({}, ["LD_PRELOAD"], []) or "")


def test_a_name_that_merely_starts_like_a_container_one_is_fine():
    # Exact names only: `PATH_PREFIX` is an ordinary variable.
    assert key_refusal("PATH_PREFIX") is None
    assert key_refusal("HOME_PAGE_URL") is None


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


def _wire(github, gojira=None):
    """The one-line secrets wiring edit, on its own branch, asking of a
    proposed branch's head whether it already carries the line."""
    return edit_gojira_yaml(
        github,
        gojira or _gojira(),
        repo=REPO,
        connected=True,
        gojira_app_id=APP_ID,
        edit=lambda data: wire_env_secret(data, AGENT),
        message="kobo: read the agent's own secrets",
        branch=SECRETS_WIRING_BRANCH,
        title=SECRETS_WIRING_PR_TITLE,
        body=SECRETS_WIRING_PR_BODY,
        carries=lambda data: has_env_secret(data, AGENT),
    )


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


def test_a_composed_agents_change_that_is_already_there_pushes_nothing_but_still_deploys():
    # Nothing to push, and the platform is asked to regenerate anyway: a
    # reprovision is cheap and idempotent, and the case that matters is a
    # save whose push landed and whose reprovision then failed. A retry has
    # nothing left to push, so refusing to reprovision would leave that
    # change undeployed forever.
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
    assert gojira.reprovision_calls == [APP_ID]


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


def test_the_wiring_edit_reuses_a_pull_request_whose_branch_really_carries_the_line():
    # Only when the BRANCH says so. Assuming an open request carried it was
    # the bug: a request opened for something else was reported as
    # proposing a line it had never seen.
    github = FakeGitHub(
        existing_repos={REPO},
        open_prs={f"{REPO}#{SECRETS_WIRING_BRANCH}": "https://example.test/pr/1"},
        branch_files={
            SECRETS_WIRING_BRANCH: {
                "gojira.yaml": f"name: clio\nsecrets_from:\n  - {AGENT}-env\n"
            }
        },
    )

    result = _wire(github)

    assert result.how == PULL_REQUEST
    assert result.pull_request == "https://example.test/pr/1"
    assert github.open_pr_calls == []


def test_the_wiring_edit_refuses_a_pull_request_whose_branch_does_not_carry_the_line():
    github = FakeGitHub(
        existing_repos={REPO},
        open_prs={f"{REPO}#{SECRETS_WIRING_BRANCH}": "https://example.test/pr/1"},
        branch_files={SECRETS_WIRING_BRANCH: {"gojira.yaml": "name: clio\n"}},
    )

    with pytest.raises(RepoBusy) as exc:
        _wire(github)

    assert "https://example.test/pr/1" in str(exc.value)
    assert github.open_pr_calls == []


def test_an_open_environment_request_never_stands_in_for_the_wiring_one():
    # The two changes live on two branches. Sharing one meant a pending
    # environment request could be mistaken for a wiring one.
    github = FakeGitHub(
        existing_repos={REPO},
        open_prs={f"{REPO}#{ENV_BRANCH}": "https://example.test/pr/env"},
        pr_url="https://example.test/pr/wiring",
    )

    result = _wire(github)

    assert result.pull_request == "https://example.test/pr/wiring"
    assert github.open_pr_calls[0]["branch"] == SECRETS_WIRING_BRANCH
    assert github.open_pr_calls[0]["title"] == SECRETS_WIRING_PR_TITLE


def test_a_leftover_branch_of_the_rooms_own_is_recycled_rather_than_refused():
    # A merged pull request in a repository that does not delete branches
    # leaves this behind. It is a branch the room named itself with nothing
    # open from it, so refusing would leave the panel permanently unable to
    # save on somebody's repository for a box they did not tick.
    github = FakeGitHub(
        existing_repos={REPO},
        existing_branches={f"{REPO}#{ENV_BRANCH}"},
        pr_url="https://example.test/pr/2",
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

    assert result.how == PULL_REQUEST
    assert github.deleted_branches == [(REPO, ENV_BRANCH)]
    assert github.open_pr_calls[0]["branch"] == ENV_BRANCH


def test_a_branch_with_an_open_pull_request_is_never_deleted():
    # Only a branch with nothing open from it is the room's to recycle.
    github = FakeGitHub(
        existing_repos={REPO},
        existing_branches={f"{REPO}#{ENV_BRANCH}"},
        open_prs={f"{REPO}#{ENV_BRANCH}": "https://example.test/pr/1"},
    )

    with pytest.raises(RepoBusy) as exc:
        edit_gojira_yaml(
            github,
            _gojira(),
            repo=REPO,
            connected=True,
            gojira_app_id=APP_ID,
            edit=_edit_env({"SECOND": "change"}),
            message="kobo: environment variables",
        )

    assert str(exc.value) == f"{ENV_PR_ALREADY_OPEN}https://example.test/pr/1"
    assert github.deleted_branches == []


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
    assert "VALUES are not here" in SECRETS_WIRING_PR_BODY
    assert "—" not in SECRETS_WIRING_PR_BODY
    assert "—" not in ENV_PR_BODY


# --- a file, or a repository, the room cannot read -----------------------------


def test_an_unparseable_gojira_yaml_is_a_named_refusal_not_an_empty_read():
    # Showing an empty list over a file nobody here could parse invites a
    # save that would overwrite whatever a person meant to put there.
    github = FakeGitHub(
        existing_repos={REPO}, repo_files={"gojira.yaml": "env: [unclosed\n"}
    )

    with pytest.raises(RepoBusy) as exc:
        read_repo_env(github, REPO)

    assert str(exc.value) == UNREADABLE_GOJIRA_YAML
    assert "gojira.yaml" in str(exc.value)


def test_a_gojira_yaml_that_is_not_a_mapping_reads_the_same_way():
    github = FakeGitHub(
        existing_repos={REPO}, repo_files={"gojira.yaml": "- one\n- two\n"}
    )

    with pytest.raises(RepoBusy):
        read_repo_env(github, REPO)


def test_a_missing_gojira_yaml_reads_as_no_variables():
    # Not every repository has been wired yet, and that is not an error.
    github = FakeGitHub(existing_repos={REPO}, repo_files={})

    assert read_repo_env(github, REPO) == {}


def test_a_repository_the_room_cannot_read_is_told_apart_from_a_missing_file():
    # `read_file` answers None for both, so permission is checked first,
    # the way the connect scan does. "Add the file" and "the room lost
    # access to this repository" are different things to tell somebody.
    github = FakeGitHub(existing_repos=set(), repo_files={})

    with pytest.raises(RepoBusy) as exc:
        read_repo_env(github, REPO)

    assert str(exc.value) == UNREADABLE_REPOSITORY


def test_an_unparseable_file_stops_a_write_before_anything_is_pushed():
    github = FakeGitHub(
        existing_repos={REPO}, extra_clone_files={"gojira.yaml": "env: [unclosed\n"}
    )

    with pytest.raises(RepoBusy) as exc:
        edit_gojira_yaml(
            github,
            _gojira(),
            repo=REPO,
            connected=False,
            gojira_app_id=APP_ID,
            edit=_edit_env({"API_BASE": "https://example.test"}),
            message="kobo: environment variables",
        )

    assert str(exc.value) == UNREADABLE_GOJIRA_YAML
    assert github.pushes == []


# --- taking the wiring line back out ------------------------------------------


def test_unwire_removes_the_entry_and_leaves_every_other_one():
    data = {"secrets_from": [f"{AGENT}-cabal", f"{AGENT}-env", "a-team-secret"]}

    assert unwire_env_secret(data, AGENT) is True
    assert data["secrets_from"] == [f"{AGENT}-cabal", "a-team-secret"]


def test_unwire_is_a_no_op_when_the_entry_is_not_there():
    data = {"secrets_from": [f"{AGENT}-cabal"]}

    assert unwire_env_secret(data, AGENT) is False
    assert data["secrets_from"] == [f"{AGENT}-cabal"]


def test_unwire_on_a_file_with_no_list_is_a_no_op():
    data: dict = {}

    assert unwire_env_secret(data, AGENT) is False


def test_wire_and_unwire_agree_with_has_env_secret():
    data: dict = {}
    assert has_env_secret(data, AGENT) is False
    wire_env_secret(data, AGENT)
    assert has_env_secret(data, AGENT) is True
    unwire_env_secret(data, AGENT)
    assert has_env_secret(data, AGENT) is False


# --- proving a branch is the room's before deleting it ------------------------


def _leftover(comparison=None):
    return FakeGitHub(
        existing_repos={REPO},
        existing_branches={f"{REPO}#{ENV_BRANCH}"},
        comparisons={ENV_BRANCH: comparison} if comparison is not None else None,
        pr_url="https://example.test/pr/2",
    )


def test_a_branch_touching_only_gojira_yaml_under_the_rooms_name_is_recycled():
    github = _leftover(
        BranchComparison(
            found=True, files=("gojira.yaml",), authors=(COMMIT_AUTHOR_EMAIL,)
        )
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

    assert result.how == PULL_REQUEST
    assert github.deleted_branches == [(REPO, ENV_BRANCH)]


def test_a_branch_touching_another_file_is_refused_by_name():
    github = _leftover(
        BranchComparison(
            found=True,
            files=("gojira.yaml", "src/main.py"),
            authors=(COMMIT_AUTHOR_EMAIL,),
        )
    )

    with pytest.raises(RepoBusy) as exc:
        edit_gojira_yaml(
            github,
            _gojira(),
            repo=REPO,
            connected=True,
            gojira_app_id=APP_ID,
            edit=_edit_env({"API_BASE": "https://example.test"}),
            message="kobo: environment variables",
        )

    assert str(exc.value) == branch_not_ours(ENV_BRANCH)
    assert github.deleted_branches == []
    assert github.open_pr_calls == []


def test_a_branch_carrying_a_persons_commit_is_refused_too():
    # A person has been working here, whatever the branch is called.
    github = _leftover(
        BranchComparison(
            found=True,
            files=("gojira.yaml",),
            authors=(COMMIT_AUTHOR_EMAIL, "someone@example.com"),
        )
    )

    with pytest.raises(RepoBusy):
        edit_gojira_yaml(
            github,
            _gojira(),
            repo=REPO,
            connected=True,
            gojira_app_id=APP_ID,
            edit=_edit_env({"API_BASE": "https://example.test"}),
            message="kobo: environment variables",
        )

    assert github.deleted_branches == []


def test_a_comparison_that_could_not_be_made_is_a_refusal_not_a_pass():
    # The important half: an empty file list from a failed lookup and an
    # empty one from an identical branch are the same value, so treating
    # "cannot tell" as "fine" would delete exactly the branches this check
    # protects.
    github = _leftover(BranchComparison(found=False, files=(), authors=()))

    with pytest.raises(RepoBusy):
        edit_gojira_yaml(
            github,
            _gojira(),
            repo=REPO,
            connected=True,
            gojira_app_id=APP_ID,
            edit=_edit_env({"API_BASE": "https://example.test"}),
            message="kobo: environment variables",
        )

    assert github.deleted_branches == []


def test_the_rooms_own_commit_identity_matches_whatever_its_case():
    github = _leftover(
        BranchComparison(
            found=True,
            files=("gojira.yaml",),
            authors=(COMMIT_AUTHOR_EMAIL.upper(),),
        )
    )

    edit_gojira_yaml(
        github,
        _gojira(),
        repo=REPO,
        connected=True,
        gojira_app_id=APP_ID,
        edit=_edit_env({"API_BASE": "https://example.test"}),
        message="kobo: environment variables",
    )

    assert github.deleted_branches == [(REPO, ENV_BRANCH)]


# --- the two refusal sentences name their own branch --------------------------


def test_the_wiring_refusal_says_wiring_and_the_environment_one_says_variables():
    # "Your environment request is open" in front of somebody adding a
    # SECRET names the wrong thing to go and merge.
    assert "secrets wiring" in WIRING_PR_ALREADY_OPEN
    assert "environment variables" in ENV_PR_ALREADY_OPEN
    assert "—" not in WIRING_PR_ALREADY_OPEN
    assert "—" not in branch_not_ours(ENV_BRANCH)


def test_both_pull_request_bodies_warn_that_the_file_is_rewritten():
    for body in (ENV_PR_BODY, SECRETS_WIRING_PR_BODY, SECRETS_UNWIRE_PR_BODY):
        assert "rewritten from its parsed form" in body
        assert "NOT preserved" in body
        assert "—" not in body


# --- a deploy the platform would not run --------------------------------------


def test_a_reprovision_failure_says_whether_anything_was_pushed():
    github = FakeGitHub(existing_repos={REPO})
    gojira = _gojira()
    gojira.reprovision_error = "the platform is having a moment"

    with pytest.raises(DeployTriggerFailed) as exc:
        edit_gojira_yaml(
            github,
            gojira,
            repo=REPO,
            connected=False,
            gojira_app_id=APP_ID,
            edit=_edit_env({"API_BASE": "https://example.test"}),
            message="kobo: environment variables",
        )

    assert exc.value.pushed is True


def test_a_reprovision_failure_on_a_no_op_save_says_nothing_was_pushed():
    github = FakeGitHub(
        existing_repos={REPO},
        extra_clone_files={"gojira.yaml": "name: clio\nenv:\n  A: b\n"},
    )
    gojira = _gojira()
    gojira.reprovision_error = "the platform is having a moment"

    with pytest.raises(DeployTriggerFailed) as exc:
        edit_gojira_yaml(
            github,
            gojira,
            repo=REPO,
            connected=False,
            gojira_app_id=APP_ID,
            edit=_edit_env({"A": "b"}),
            message="kobo: environment variables",
        )

    assert exc.value.pushed is False


def test_the_reprovision_can_be_held_back_for_a_caller_with_more_to_do():
    # Removing an agent's last secret commits the line away first, deletes
    # the Secret, and only then asks for a deploy.
    github = FakeGitHub(existing_repos={REPO})
    gojira = _gojira()

    edit_gojira_yaml(
        github,
        gojira,
        repo=REPO,
        connected=False,
        gojira_app_id=APP_ID,
        edit=_edit_env({"API_BASE": "https://example.test"}),
        message="kobo: environment variables",
        reprovision=False,
    )

    assert github.pushes
    assert gojira.reprovision_calls == []


# --- The lock: is a stored secret actually on the agent? ----------------------


def _ready(status: str, reason: str | None = None, message: str | None = None) -> dict:
    condition: dict = {"type": "Ready", "status": status}
    if reason is not None:
        condition["reason"] = reason
    if message is not None:
        condition["message"] = message
    return {"conditions": [{"type": "Deleted", "status": "False"}, condition]}


def test_a_ready_external_secret_reads_as_live():
    assert sync_state(_ready("True")) == {"state": LIVE, "message": None}


def test_an_external_secret_with_no_ready_condition_yet_reads_as_syncing():
    assert sync_state({}) == {"state": SYNCING, "message": None}
    assert sync_state({"conditions": []}) == {"state": SYNCING, "message": None}


def test_a_missing_external_secret_reads_as_unwired():
    assert sync_state(None) == {"state": UNWIRED, "message": SYNC_NOT_DELIVERED}


def test_a_failing_external_secret_reads_as_an_error_naming_the_reason_word():
    answer = sync_state(_ready("False", reason="SecretSyncedError"))

    assert answer["state"] == SYNC_ERROR
    assert answer["message"] == f"{SYNC_FAILED} It reported SecretSyncedError."


def test_the_platforms_own_message_never_reaches_the_answer():
    # The one field of somebody else's object that could carry what was
    # stored. The reason word comes along; the sentence around it does not.
    answer = sync_state(
        _ready(
            "False",
            reason="SecretSyncedError",
            message="could not store not-a-real-key-value at apps/clio/env",
        )
    )

    assert "not-a-real-key-value" not in str(answer)
    assert "apps/clio/env" not in str(answer)


def test_a_reason_that_is_not_one_plain_word_is_not_repeated_either():
    answer = sync_state(_ready("False", reason="secret value: not-a-real-key-value"))

    assert answer == {"state": SYNC_ERROR, "message": SYNC_FAILED}


def test_a_ready_condition_that_says_neither_true_nor_false_reads_as_syncing():
    assert sync_state(_ready("Unknown")) == {"state": SYNCING, "message": None}


def test_a_cluster_the_room_could_not_ask_reads_as_syncing_and_says_so():
    # NOT "not on the agent": a failed look is not evidence that the
    # secret is missing, and the tag for that state would claim it is.
    assert sync_unknown() == {"state": SYNCING, "message": SYNC_UNKNOWN}


def test_the_rooms_own_two_secrets_are_named_the_way_the_agents_own_is():
    assert managed_secret_path("clio", "cabal") == "apps/clio/cabal"
    assert managed_secret_name("clio", "slack") == "clio-slack"
    assert MANAGED_PURPOSES == ("cabal", "slack")


def test_contract_only_commit_is_marked_skip_ci():
    """A gojira.yaml-only commit fails the generated contract check and the
    reprovision's setup pull request is the deploy, so the push skips CI."""
    github = FakeGitHub(existing_repos={REPO})
    gojira = _gojira()
    edit_gojira_yaml(
        github,
        gojira,
        repo=REPO,
        connected=False,
        gojira_app_id=APP_ID,
        edit=_edit_env({"API_BASE": "https://example.test"}),
        message="kobo: environment variables",
    )
    assert github.pushes[-1][1] == "kobo: environment variables [skip ci]"
