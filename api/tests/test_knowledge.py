"""Tests for kobo_api.knowledge: read/save/delete an agent bundle's
`bundle/knowledge/*.md` seed-memory files, and the stateless delivery check
(kobo_api.knowledge.delivery_state), all against the FakeGitHub/FakeKubectl
fakes — no real git/kubectl involved.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from kobo_api.clients import ClientError
from kobo_api.knowledge import (
    KNOWLEDGE_MAX_BYTES,
    KnowledgeError,
    KnowledgeFile,
    delete_knowledge_file,
    delivery_state,
    read_knowledge,
    rollout_in_progress,
    save_knowledge_file,
)
from tests.fakes import FAKE_LOCAL_HEAD_DEFAULT, FakeGitHub, FakeKubectl


class _CapturingGitHub(FakeGitHub):
    """A FakeGitHub that snapshots bundle/knowledge/*.md content the moment
    commit_push is called — the only window in which the caller's cloned
    workdir still exists, since knowledge.py discards its TemporaryDirectory
    as part of returning. Lets a test assert on exactly what was on disk
    right before it was pushed, without needing a stateful fake."""

    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self.captured_knowledge: dict[str, str] = {}

    def commit_push(self, workdir: Path, message: str) -> bool:
        knowledge_dir = workdir / "bundle" / "knowledge"
        if knowledge_dir.is_dir():
            self.captured_knowledge = {
                path.name: path.read_text() for path in knowledge_dir.glob("*.md")
            }
        return super().commit_push(workdir, message)


# --- read_knowledge -----------------------------------------------------------


def test_read_knowledge_lists_files_sorted_by_name():
    github = FakeGitHub(knowledge_files={"b.md": "second", "a.md": "first"})
    files = read_knowledge(github, "acme/agent-clio")
    assert files == [
        KnowledgeFile(name="a.md", content="first"),
        KnowledgeFile(name="b.md", content="second"),
    ]


def test_read_knowledge_empty_when_no_knowledge_dir():
    github = FakeGitHub()  # no knowledge_files -> clone writes no bundle/knowledge tree
    assert read_knowledge(github, "acme/agent-clio") == []


def test_read_knowledge_does_not_leak_the_temporary_clone_dir():
    captured: dict[str, Path] = {}
    github = FakeGitHub(knowledge_files={"a.md": "hi"})
    real_clone = github.clone

    def spying_clone(repo, dest):
        real_clone(repo, dest)
        captured["dest"] = dest

    github.clone = spying_clone
    read_knowledge(github, "acme/agent-clio")
    assert not captured["dest"].exists()


# --- save_knowledge_file --------------------------------------------------------


def test_save_knowledge_file_writes_content_commits_and_returns_local_head():
    github = _CapturingGitHub()
    result = save_knowledge_file(github, "acme/agent-clio", "gotchas.md", "new content")

    assert github.captured_knowledge == {"gotchas.md": "new content"}
    assert github.pushes[-1][1] == "kobo: update knowledge/gotchas.md"
    assert result == FAKE_LOCAL_HEAD_DEFAULT
    assert github.local_head_calls == [github.pushes[-1][0]]


def test_save_knowledge_file_overwrites_existing_file():
    github = _CapturingGitHub(knowledge_files={"gotchas.md": "old content"})
    save_knowledge_file(github, "acme/agent-clio", "gotchas.md", "replaced")
    assert github.captured_knowledge == {"gotchas.md": "replaced"}


def test_save_knowledge_file_returns_local_head_even_when_nothing_changed():
    # commit_push -> False (identical content, nothing to commit) still
    # returns local_head — the delivery check then reports delivered
    # immediately, which is truthful (deliberate, per the task brief).
    github = FakeGitHub(commit_push_results=[False])
    result = save_knowledge_file(github, "acme/agent-clio", "gotchas.md", "same")
    assert result == FAKE_LOCAL_HEAD_DEFAULT


@pytest.mark.parametrize(
    "filename",
    [
        "Gotchas.md",  # uppercase first char
        "gotchas.txt",  # wrong extension
        "-gotchas.md",  # can't start with a hyphen/digit
        "1gotchas.md",
        "gotchas md.md",  # space
        "../gotchas.md",  # path traversal
        "gotchas",  # no extension at all
        "a" * 41 + ".md",  # over the 40-char cap (1 + 39 more)
        "",
    ],
)
def test_save_knowledge_file_rejects_bad_filename(filename):
    github = FakeGitHub()
    with pytest.raises(KnowledgeError):
        save_knowledge_file(github, "acme/agent-clio", filename, "content")
    assert github.pushes == []


def test_save_knowledge_file_rejects_empty_content():
    github = FakeGitHub()
    with pytest.raises(KnowledgeError):
        save_knowledge_file(github, "acme/agent-clio", "gotchas.md", "")
    assert github.pushes == []


def test_save_knowledge_file_rejects_oversize_content():
    github = FakeGitHub()
    oversize = "a" * (KNOWLEDGE_MAX_BYTES + 1)
    with pytest.raises(KnowledgeError):
        save_knowledge_file(github, "acme/agent-clio", "gotchas.md", oversize)
    assert github.pushes == []


def test_save_knowledge_file_accepts_content_at_exact_byte_cap():
    github = _CapturingGitHub()
    exact = "a" * KNOWLEDGE_MAX_BYTES
    save_knowledge_file(github, "acme/agent-clio", "gotchas.md", exact)
    assert github.captured_knowledge == {"gotchas.md": exact}


# --- delete_knowledge_file -------------------------------------------------------


def test_delete_knowledge_file_removes_and_commits():
    github = _CapturingGitHub(knowledge_files={"gotchas.md": "bye", "keep.md": "stay"})
    result = delete_knowledge_file(github, "acme/agent-clio", "gotchas.md")

    assert github.captured_knowledge == {"keep.md": "stay"}
    assert github.pushes[-1][1] == "kobo: remove knowledge/gotchas.md"
    assert result == FAKE_LOCAL_HEAD_DEFAULT


def test_delete_knowledge_file_missing_file_raises_and_does_not_push():
    github = FakeGitHub(knowledge_files={"keep.md": "stay"})
    with pytest.raises(KnowledgeError):
        delete_knowledge_file(github, "acme/agent-clio", "gotchas.md")
    assert github.pushes == []


def test_delete_knowledge_file_missing_knowledge_dir_raises():
    github = FakeGitHub()  # no bundle/knowledge tree at all
    with pytest.raises(KnowledgeError):
        delete_knowledge_file(github, "acme/agent-clio", "gotchas.md")
    assert github.pushes == []


def test_delete_knowledge_file_rejects_bad_filename():
    github = FakeGitHub(knowledge_files={"gotchas.md": "bye"})
    with pytest.raises(KnowledgeError):
        delete_knowledge_file(github, "acme/agent-clio", "../escape.md")
    assert github.pushes == []


# --- delivery_state -------------------------------------------------------------


def test_delivery_state_building_when_no_runs_yet():
    github = FakeGitHub(runs_by_sha={"sha1": [[]]})
    kubectl = FakeKubectl()
    state = delivery_state(
        github, kubectl, repo="acme/agent-clio", namespace="clio", deploy="clio", sha="sha1"
    )
    assert state == {"state": "building", "detail": None}


def test_delivery_state_building_when_run_in_progress():
    github = FakeGitHub(
        runs_by_sha={"sha1": [[{"id": 1, "status": "in_progress", "conclusion": None}]]}
    )
    kubectl = FakeKubectl()
    state = delivery_state(
        github, kubectl, repo="acme/agent-clio", namespace="clio", deploy="clio", sha="sha1"
    )
    assert state == {"state": "building", "detail": None}


def test_delivery_state_building_when_run_queued():
    github = FakeGitHub(
        runs_by_sha={"sha1": [[{"id": 1, "status": "queued", "conclusion": None}]]}
    )
    kubectl = FakeKubectl()
    state = delivery_state(
        github, kubectl, repo="acme/agent-clio", namespace="clio", deploy="clio", sha="sha1"
    )
    assert state == {"state": "building", "detail": None}


def test_delivery_state_failed_when_run_completed_non_success():
    github = FakeGitHub(
        runs_by_sha={"sha1": [[{"id": 1, "status": "completed", "conclusion": "failure"}]]}
    )
    kubectl = FakeKubectl()
    state = delivery_state(
        github, kubectl, repo="acme/agent-clio", namespace="clio", deploy="clio", sha="sha1"
    )
    assert state == {
        "state": "failed",
        "detail": "the update didn't go through — change something and save to try again",
    }


def test_delivery_state_failed_when_run_cancelled():
    github = FakeGitHub(
        runs_by_sha={"sha1": [[{"id": 1, "status": "completed", "conclusion": "cancelled"}]]}
    )
    kubectl = FakeKubectl()
    state = delivery_state(
        github, kubectl, repo="acme/agent-clio", namespace="clio", deploy="clio", sha="sha1"
    )
    assert state["state"] == "failed"


def test_delivery_state_rolling_when_success_but_image_stale():
    github = FakeGitHub(
        runs_by_sha={"sha1": [[{"id": 1, "status": "completed", "conclusion": "success"}]]}
    )
    kubectl = FakeKubectl(images={("clio", "clio"): "gojira-app:stalestalestal"})
    state = delivery_state(
        github, kubectl, repo="acme/agent-clio", namespace="clio", deploy="clio", sha="sha1"
    )
    assert state == {"state": "rolling", "detail": None}


def test_delivery_state_delivered_when_success_and_image_matches():
    sha = "abcdefabcdefabcdefabcdefabcdefabcdefabcd"
    github = FakeGitHub(
        runs_by_sha={sha: [[{"id": 1, "status": "completed", "conclusion": "success"}]]}
    )
    kubectl = FakeKubectl(images={("clio", "clio"): f"gojira-app:{sha[:12]}"})
    state = delivery_state(
        github, kubectl, repo="acme/agent-clio", namespace="clio", deploy="clio", sha=sha
    )
    assert state == {"state": "delivered", "detail": None}


def test_delivery_state_newest_run_is_index_zero():
    # Mirrors BuildStep's convention (kobo_api.steps.BuildStep): runs are
    # newest-first, so a stale earlier failing entry at index 1 must not
    # override a successful run at index 0.
    sha = "ed17ed17ed17ed17ed17ed17ed17ed17ed17ed17"
    github = FakeGitHub(
        runs_by_sha={
            sha: [
                [
                    {"id": 2, "status": "completed", "conclusion": "success"},
                    {"id": 1, "status": "completed", "conclusion": "failure"},
                ]
            ]
        }
    )
    kubectl = FakeKubectl(images={("clio", "clio"): "gojira-app:ed17ed17ed17"})
    state = delivery_state(
        github,
        kubectl,
        repo="acme/agent-clio",
        namespace="clio",
        deploy="clio",
        sha=sha,
    )
    assert state == {"state": "delivered", "detail": None}


def test_delivery_state_delivered_when_image_is_digest_pinned_with_matching_tag():
    # Live repro (digest-pinned deploy fix): the platform's regenerated CI
    # now applies Deployments pinned BY DIGEST (`repo:tag@sha256:...`) — the
    # tag before the '@' must still be matched against sha[:12], not the
    # digest hex. Must be RED (rolling, not delivered) before the image_tag
    # fix lands.
    sha = "abcdefabcdefabcdefabcdefabcdefabcdefabcd"
    github = FakeGitHub(
        runs_by_sha={sha: [[{"id": 1, "status": "completed", "conclusion": "success"}]]}
    )
    kubectl = FakeKubectl(
        images={
            ("clio", "clio"): (
                f"gojira-app:{sha[:12]}"
                "@sha256:3c68b1e1a2a3f4b5c6d7e8f900112233445566778899aabbccddeeff0011223"
            )
        }
    )
    state = delivery_state(
        github, kubectl, repo="acme/agent-clio", namespace="clio", deploy="clio", sha=sha
    )
    assert state == {"state": "delivered", "detail": None}


def test_delivery_state_rolling_when_image_is_digest_only_no_tag():
    # A tagless digest-only image (`repo@sha256:...`) can't be matched to a
    # sha at all — an honest "rolling" (pending) state, not a crash.
    sha = "abcdefabcdefabcdefabcdefabcdefabcdefabcd"
    github = FakeGitHub(
        runs_by_sha={sha: [[{"id": 1, "status": "completed", "conclusion": "success"}]]}
    )
    kubectl = FakeKubectl(
        images={
            ("clio", "clio"): (
                "gojira-app"
                "@sha256:3c68b1e1a2a3f4b5c6d7e8f900112233445566778899aabbccddeeff0011223"
            )
        }
    )
    state = delivery_state(
        github, kubectl, repo="acme/agent-clio", namespace="clio", deploy="clio", sha=sha
    )
    assert state == {"state": "rolling", "detail": None}


def test_delivery_state_defaults_wired_happy_path_reads_delivered():
    # FakeGitHub()/FakeKubectl() defaults are deliberately consistent (see
    # tests/fakes.py FAKE_LOCAL_HEAD_DEFAULT/FAKE_DEPLOY_IMAGE_DEFAULT) — a
    # test that wires the bare fakes together with the default sha reads
    # "delivered" without configuring either one.
    github = FakeGitHub(runs_by_sha={FAKE_LOCAL_HEAD_DEFAULT: [[{"id": 1, "status": "completed", "conclusion": "success"}]]})
    kubectl = FakeKubectl()
    state = delivery_state(
        github,
        kubectl,
        repo="acme/agent-clio",
        namespace="clio",
        deploy="clio",
        sha=FAKE_LOCAL_HEAD_DEFAULT,
    )
    assert state == {"state": "delivered", "detail": None}


# --- rollout_in_progress -----------------------------------------------------


def test_rollout_in_progress_truth_table():
    def dep(generation=2, observed=2, replicas=1, updated=1, available=1):
        return {
            "metadata": {"generation": generation},
            "spec": {"replicas": replicas},
            "status": {
                "observedGeneration": observed,
                "updatedReplicas": updated,
                "availableReplicas": available,
            },
        }

    class Kubectl:
        def __init__(self, payload):
            self.payload = payload

        def deployment_status_json(self, namespace, deploy):
            if isinstance(self.payload, Exception):
                raise self.payload
            return self.payload

    assert rollout_in_progress(Kubectl(dep()), namespace="w", deploy="w") is False
    assert rollout_in_progress(Kubectl(dep(observed=1)), namespace="w", deploy="w") is True
    assert rollout_in_progress(Kubectl(dep(updated=0)), namespace="w", deploy="w") is True
    assert rollout_in_progress(Kubectl(dep(available=0)), namespace="w", deploy="w") is True
    assert (
        rollout_in_progress(
            Kubectl(dep(replicas=0, updated=0, available=0)), namespace="w", deploy="w"
        )
        is False
    )
    # A probe error must NEVER mask the switchboard copy:
    assert (
        rollout_in_progress(Kubectl(ClientError("kubectl down")), namespace="w", deploy="w")
        is False
    )
