"""Tests for kobo_api.tooling: upload/import/list/delete an agent bundle's
`bundle/tools/*.{py,go}` staged-law files, plus the ancestor-derived
"delivered" status `list_tooling` computes for each — all against the
FakeGitHub/FakeKubectl fakes, no real git/kubectl involved.

Mirrors test_intake.py's structure closely (same module shape, same
tempdir-per-call discipline, same ancestor-derivation reuse) — see that
file's own module docstring for the shared reasoning. The one genuinely new
surface here is `import_tooling`'s partial-pass semantics: a folder of
candidate files, each independently validated, committed in one push only
when at least one passes.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from kobo_api.tooling import (
    TOOLING_MAX_BYTES,
    ToolingError,
    delete_tooling_file,
    import_tooling,
    list_tooling,
    upload_tooling_file,
)
from tests.fakes import (
    FAKE_DOCKERFILE_OLD_IMAGE,
    FAKE_LOCAL_HEAD_DEFAULT,
    FakeAgent,
    FakeGitHub,
    FakeKubectl,
)

REPO = "acme/agent-clio"
SOURCE_REPO = "acme/tools-library"
NAMESPACE = "clio"
DEPLOY = "clio"

NEWER_SHA = "newnewnewnewnewnewnewnewnewnewnewnewnewn"
OLDER_SHA = "oldoldoldoldoldoldoldoldoldoldoldoldoldo"

RUNTIME_IMAGE = "registry.example/agents/daedalus-runtime:new"

# A syntax error on line 3 ("z = )" is an unmatched-paren error) — stable
# across recent Python versions (verified against 3.12).
BROKEN_PY = "x = 1\ny = 2\nz = )\n"

# Carries a real entrypoint (2026-08-16 addendum: the import allowlist +
# entrypoint check) — every other fixture in this module that stands in for
# "a valid tooling file" reuses this constant, so it only needed updating in
# one place once the contract started requiring one.
VALID_PY = "def post_draft(draft):\n    return draft\n"


class _CapturingGitHub(FakeGitHub):
    """Snapshots bundle/tools/* content and the Dockerfile the moment
    commit_push is called — the only window in which the caller's cloned
    workdir still exists. Mirrors test_intake.py's own _CapturingGitHub."""

    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self.captured_tools: dict[str, str] = {}
        self.captured_dockerfile = ""

    def commit_push(self, workdir: Path, message: str) -> bool:
        tools_dir = workdir / "bundle" / "tools"
        if tools_dir.is_dir():
            self.captured_tools = {
                path.name: path.read_text()
                for path in tools_dir.iterdir()
                if path.is_file()
            }
        self.captured_dockerfile = (workdir / "Dockerfile").read_text()
        return super().commit_push(workdir, message)


# --- filename validation matrix -----------------------------------------------


@pytest.mark.parametrize(
    "filename",
    [
        "Tool.py",  # uppercase first char
        "tool.txt",  # wrong extension
        "-tool.py",  # can't start with a hyphen/digit
        "1tool.py",
        "_tool.py",  # can't start with an underscore either
        "tool name.py",  # space
        "../tool.py",  # path traversal
        "tool",  # no extension at all
        "a" * 65 + ".py",  # over the 64-char cap (1 + 63 more)
        "tool.py\n",  # trailing newline — must use fullmatch, not match
        "",
    ],
)
def test_upload_tooling_file_rejects_bad_filename(filename):
    github = FakeGitHub()
    with pytest.raises(ToolingError, match="invalid tooling filename"):
        upload_tooling_file(github, REPO, filename, VALID_PY)
    assert github.pushes == []


@pytest.mark.parametrize(
    "filename,content",
    [
        ("tool.py", VALID_PY),
        ("tool.go", "package main\n"),
        ("tool_name.py", VALID_PY),  # underscore allowed
        ("tool-name.go", "package main\n"),  # hyphen allowed
        ("a" * 64 + ".py", VALID_PY),  # exactly at the 64-char cap
    ],
)
def test_upload_tooling_file_accepts_good_filename(filename, content):
    github = _CapturingGitHub()
    upload_tooling_file(github, REPO, filename, content)
    assert github.captured_tools == {filename: content}


# --- content validation matrix -------------------------------------------------


def test_upload_tooling_file_rejects_empty_content():
    github = FakeGitHub()
    with pytest.raises(ToolingError, match="tooling file content"):
        upload_tooling_file(github, REPO, "tool.py", "")
    assert github.pushes == []


def test_upload_tooling_file_rejects_oversize_content():
    github = FakeGitHub()
    # A syntactically valid module that's just one big oversize string
    # literal, so this exercises ONLY the size check, not the ast branch.
    oversize = '"' + "a" * (TOOLING_MAX_BYTES + 1) + '"\n'
    with pytest.raises(ToolingError, match="exceeds"):
        upload_tooling_file(github, REPO, "tool.py", oversize)
    assert github.pushes == []


def test_upload_tooling_file_accepts_content_at_exact_byte_cap():
    github = _CapturingGitHub()
    # exact-cap content built the same oversize-avoiding way: a real
    # entrypoint (required since the 2026-08-16 addendum) followed by a
    # comment padded out to the exact byte cap.
    base = "def post_draft(draft):\n    return draft\n#"
    padding = "a" * (TOOLING_MAX_BYTES - len(base) - len("\n"))
    exact = base + padding + "\n"
    assert len(exact.encode("utf-8")) == TOOLING_MAX_BYTES
    upload_tooling_file(github, REPO, "tool.py", exact)
    assert github.captured_tools == {"tool.py": exact}


def test_upload_tooling_file_go_extension_skips_syntax_check():
    # No Go toolchain in the room (addendum) — any UTF-8, non-empty content
    # is accepted for a .go file, garbage included.
    github = _CapturingGitHub()
    upload_tooling_file(github, REPO, "tool.go", "this is not valid go at all {{{")
    assert github.captured_tools == {"tool.go": "this is not valid go at all {{{"}


def test_upload_tooling_file_python_syntax_error_reports_line_number():
    github = FakeGitHub()
    with pytest.raises(ToolingError) as excinfo:
        upload_tooling_file(github, REPO, "tool.py", BROKEN_PY)
    message = str(excinfo.value)
    assert message.startswith("tooling file has a syntax error on line 3:")
    assert github.pushes == []


def test_upload_tooling_file_valid_python_passes():
    github = _CapturingGitHub()
    upload_tooling_file(github, REPO, "tool.py", VALID_PY)
    assert github.captured_tools == {"tool.py": VALID_PY}


# --- import allowlist (2026-08-16 addendum) -------------------------------------
#
# Reuses the SAME `ast.parse` tree `_validate_content` already built for the
# syntax check above — no second parse. Every case below carries a real
# `post_draft` entrypoint (except where the rejection fires first) so each
# test isolates the import check from the entrypoint check.


def test_upload_tooling_file_rejects_non_stdlib_import_with_line_number():
    github = FakeGitHub()
    content = "import os\nimport httpx\n\n\ndef post_draft(draft):\n    return draft\n"
    with pytest.raises(ToolingError) as excinfo:
        upload_tooling_file(github, REPO, "tool.py", content)
    assert str(excinfo.value) == (
        "tooling file imports beyond the standard library on line 2: httpx"
    )
    assert github.pushes == []


def test_upload_tooling_file_rejects_non_stdlib_from_import():
    github = FakeGitHub()
    content = "from requests import get\n\n\ndef post_draft(draft):\n    return draft\n"
    with pytest.raises(ToolingError) as excinfo:
        upload_tooling_file(github, REPO, "tool.py", content)
    assert str(excinfo.value) == (
        "tooling file imports beyond the standard library on line 1: requests"
    )
    assert github.pushes == []


def test_upload_tooling_file_accepts_multi_name_stdlib_import():
    github = _CapturingGitHub()
    content = "import os, sys\n\n\ndef post_draft(draft):\n    return draft\n"
    upload_tooling_file(github, REPO, "tool.py", content)
    assert github.captured_tools == {"tool.py": content}


def test_upload_tooling_file_accepts_stdlib_from_import():
    github = _CapturingGitHub()
    content = (
        "from collections import OrderedDict\n\n\n"
        "def post_draft(draft):\n    return OrderedDict(draft)\n"
    )
    upload_tooling_file(github, REPO, "tool.py", content)
    assert github.captured_tools == {"tool.py": content}


def test_upload_tooling_file_accepts_stdlib_import_ast():
    github = _CapturingGitHub()
    content = "import ast\n\n\ndef post_draft(draft):\n    return draft\n"
    upload_tooling_file(github, REPO, "tool.py", content)
    assert github.captured_tools == {"tool.py": content}


def test_upload_tooling_file_rejects_relative_import():
    github = FakeGitHub()
    content = "from . import helper\n\n\ndef post_draft(draft):\n    return draft\n"
    with pytest.raises(ToolingError) as excinfo:
        upload_tooling_file(github, REPO, "tool.py", content)
    assert str(excinfo.value) == "tooling file uses a relative import on line 1"
    assert github.pushes == []


# --- entrypoint check (2026-08-16 addendum) -------------------------------------


def test_upload_tooling_file_rejects_async_pre_task_with_specific_message():
    github = FakeGitHub()
    content = "async def pre_task(task):\n    return task\n"
    with pytest.raises(ToolingError) as excinfo:
        upload_tooling_file(github, REPO, "tool.py", content)
    assert str(excinfo.value) == (
        "tooling file hooks must be regular functions, not async"
    )
    assert github.pushes == []


def test_upload_tooling_file_nested_def_does_not_satisfy_entrypoint():
    github = FakeGitHub()
    content = (
        "def outer():\n"
        "    def pre_task(task):\n"
        "        return task\n"
        "    return outer\n"
    )
    with pytest.raises(ToolingError, match="tooling file defines neither pre_task nor post_draft"):
        upload_tooling_file(github, REPO, "tool.py", content)
    assert github.pushes == []


def test_upload_tooling_file_missing_entrypoint_rejected():
    github = FakeGitHub()
    content = "def helper():\n    return 1\n"
    with pytest.raises(ToolingError) as excinfo:
        upload_tooling_file(github, REPO, "tool.py", content)
    assert str(excinfo.value) == "tooling file defines neither pre_task nor post_draft"
    assert github.pushes == []


def test_upload_tooling_file_only_post_draft_passes():
    github = _CapturingGitHub()
    content = "def post_draft(draft):\n    return draft\n"
    upload_tooling_file(github, REPO, "tool.py", content)
    assert github.captured_tools == {"tool.py": content}


def test_upload_tooling_file_only_pre_task_passes():
    github = _CapturingGitHub()
    content = "def pre_task(task):\n    return task\n"
    upload_tooling_file(github, REPO, "tool.py", content)
    assert github.captured_tools == {"tool.py": content}


def test_upload_tooling_file_reference_orchestrator_shape_passes():
    # A plausible real orchestrator: module constants, an exception class, a
    # dataclass, both hooks, stdlib-only imports (json/re/dataclasses) —
    # built inline here, not read from any real client's repo.
    github = _CapturingGitHub()
    content = (
        "import json\n"
        "import re\n"
        "from dataclasses import dataclass\n"
        "\n"
        "\n"
        "MAX_RETRIES = 3\n"
        "DEFAULT_TIMEOUT = 30\n"
        "\n"
        "\n"
        "class OrchestratorError(Exception):\n"
        '    """Raised when the orchestrator cannot do its job."""\n'
        "\n"
        "\n"
        "@dataclass\n"
        "class Context:\n"
        "    retries: int = MAX_RETRIES\n"
        "\n"
        "\n"
        "def pre_task(task):\n"
        '    return json.dumps({"task": task})\n'
        "\n"
        "\n"
        "def post_draft(draft):\n"
        '    if re.search(r"TODO", draft):\n'
        '        raise OrchestratorError("draft still has a TODO")\n'
        "    return draft\n"
    )
    upload_tooling_file(github, REPO, "tool.py", content)
    assert github.captured_tools == {"tool.py": content}


# --- upload_tooling_file: writes/commits/returns sha ---------------------------


def test_upload_tooling_file_writes_content_commits_and_returns_local_head():
    github = _CapturingGitHub()
    result = upload_tooling_file(github, REPO, "orchestrator.py", VALID_PY)

    assert github.captured_tools == {"orchestrator.py": VALID_PY}
    assert github.pushes[-1][1] == "kobo: add tools/orchestrator.py"
    assert result == FAKE_LOCAL_HEAD_DEFAULT
    assert github.local_head_calls == [github.pushes[-1][0]]


def test_upload_tooling_file_overwrites_existing_file():
    github = _CapturingGitHub(tooling_files={"tool.py": "old = 1\n"})
    upload_tooling_file(github, REPO, "tool.py", VALID_PY)
    assert github.captured_tools == {"tool.py": VALID_PY}


def test_upload_tooling_file_returns_local_head_even_when_nothing_changed():
    github = FakeGitHub(commit_push_results=[False])
    result = upload_tooling_file(github, REPO, "tool.py", VALID_PY)
    assert result == FAKE_LOCAL_HEAD_DEFAULT


# --- upload_tooling_file: runtime_image re-pin (same pattern as intake) -------


def test_upload_tooling_file_with_runtime_image_rewrites_dockerfile_in_same_commit():
    github = _CapturingGitHub()  # defaults to FAKE_DOCKERFILE_OLD_IMAGE
    assert FAKE_DOCKERFILE_OLD_IMAGE != RUNTIME_IMAGE

    upload_tooling_file(github, REPO, "tool.py", VALID_PY, runtime_image=RUNTIME_IMAGE)

    assert len(github.pushes) == 1
    assert github.captured_tools == {"tool.py": VALID_PY}
    assert github.captured_dockerfile == (
        "# Agent image: the shared runtime plus this agent's bundle.\n"
        f"FROM {RUNTIME_IMAGE}\n"
        "COPY bundle/ /bundle\n"
    )


def test_upload_tooling_file_runtime_image_omitted_leaves_dockerfile_untouched():
    github = _CapturingGitHub()
    upload_tooling_file(github, REPO, "tool.py", VALID_PY)
    assert github.captured_dockerfile == (
        "# Agent image: the shared runtime plus this agent's bundle.\n"
        f"FROM {FAKE_DOCKERFILE_OLD_IMAGE}\n"
        "COPY bundle/ /bundle\n"
    )


# --- delete_tooling_file --------------------------------------------------------


def test_delete_tooling_file_removes_and_commits():
    github = _CapturingGitHub(tooling_files={"tool.py": VALID_PY, "keep.go": "package main\n"})
    result = delete_tooling_file(github, REPO, "tool.py")

    assert github.captured_tools == {"keep.go": "package main\n"}
    assert github.pushes[-1][1] == "kobo: remove tools/tool.py"
    assert result == FAKE_LOCAL_HEAD_DEFAULT


def test_delete_tooling_file_missing_file_raises_and_does_not_push():
    github = FakeGitHub(tooling_files={"keep.py": VALID_PY})
    with pytest.raises(ToolingError, match="tooling file not found"):
        delete_tooling_file(github, REPO, "tool.py")
    assert github.pushes == []


def test_delete_tooling_file_missing_tools_dir_raises():
    github = FakeGitHub()  # no bundle/tools tree at all
    with pytest.raises(ToolingError):
        delete_tooling_file(github, REPO, "tool.py")
    assert github.pushes == []


def test_delete_tooling_file_rejects_bad_filename():
    github = FakeGitHub(tooling_files={"tool.py": VALID_PY})
    with pytest.raises(ToolingError, match="invalid tooling filename"):
        delete_tooling_file(github, REPO, "../escape.py")
    assert github.pushes == []


# --- import_tooling: source_repo / source_path validation -----------------------


@pytest.mark.parametrize(
    "source_repo",
    ["justaname", "owner/repo/extra", "owner/", "/repo", "owner repo/x", ""],
)
def test_import_tooling_rejects_bad_source_repo(source_repo):
    github = FakeGitHub()
    with pytest.raises(ToolingError, match="invalid source repo"):
        import_tooling(github, REPO, source_repo=source_repo, source_path="tools")
    assert github.cloned == []


@pytest.mark.parametrize(
    "source_path",
    ["/tools", "../tools", "tools/../escape", ".."],
)
def test_import_tooling_rejects_bad_source_path(source_path):
    github = FakeGitHub()
    with pytest.raises(ToolingError, match="invalid source path"):
        import_tooling(github, REPO, source_repo=SOURCE_REPO, source_path=source_path)
    assert github.cloned == []


# --- import_tooling: folder walk + partial-pass semantics -----------------------


def test_import_tooling_all_files_pass_one_commit_one_sha():
    github = _CapturingGitHub(
        tooling_source_files={
            SOURCE_REPO: {
                "tools/good_one.py": VALID_PY,
                "tools/good_two.go": "package main\n",
            }
        }
    )
    result = import_tooling(github, REPO, source_repo=SOURCE_REPO, source_path="tools")

    assert result["sha"] == FAKE_LOCAL_HEAD_DEFAULT
    assert result["imported"] == ["good_one.py", "good_two.go"]
    assert result["rejected"] == []
    assert github.captured_tools == {"good_one.py": VALID_PY, "good_two.go": "package main\n"}
    assert github.pushes[-1][1] == f"kobo: import tools from {SOURCE_REPO}/tools"
    # Source cloned first (to gather candidates), agent repo second (to
    # place the passers) — never the reverse, and never more than once each.
    assert [r for r, _ in github.cloned] == [SOURCE_REPO, REPO]


def test_import_tooling_partial_pass_reports_both_lists_and_still_commits():
    github = _CapturingGitHub(
        tooling_source_files={
            SOURCE_REPO: {
                "tools/good.py": VALID_PY,
                "tools/broken.py": BROKEN_PY,
                "tools/Bad-Name.py": VALID_PY,  # fails filename validation
                "tools/empty.py": "",
            }
        }
    )
    result = import_tooling(github, REPO, source_repo=SOURCE_REPO, source_path="tools")

    assert result["sha"] == FAKE_LOCAL_HEAD_DEFAULT
    assert result["imported"] == ["good.py"]
    assert github.captured_tools == {"good.py": VALID_PY}

    rejected_by_name = {r["name"]: r["reason"] for r in result["rejected"]}
    assert set(rejected_by_name) == {"broken.py", "Bad-Name.py", "empty.py"}
    assert rejected_by_name["broken.py"].startswith("tooling file has a syntax error on line 3:")
    assert rejected_by_name["Bad-Name.py"].startswith("invalid tooling filename:")
    assert rejected_by_name["empty.py"].startswith("tooling file content")


def test_import_tooling_none_pass_sha_is_null_and_agent_repo_never_cloned():
    github = _CapturingGitHub(
        tooling_source_files={SOURCE_REPO: {"tools/broken.py": BROKEN_PY}}
    )
    result = import_tooling(github, REPO, source_repo=SOURCE_REPO, source_path="tools")

    assert result == {
        "sha": None,
        "imported": [],
        "rejected": [
            {
                "name": "broken.py",
                "reason": "tooling file has a syntax error on line 3: unmatched ')'",
            }
        ],
    }
    assert github.pushes == []
    # Nothing passed -> the agent repo is never even cloned, only the source.
    assert [r for r, _ in github.cloned] == [SOURCE_REPO]


def test_import_tooling_invalid_utf8_file_is_rejected():
    github = _CapturingGitHub(
        tooling_source_files={SOURCE_REPO: {"tools/binary.py": b"\xff\xfe not text"}}
    )
    result = import_tooling(github, REPO, source_repo=SOURCE_REPO, source_path="tools")

    assert result["sha"] is None
    assert result["imported"] == []
    assert result["rejected"] == [
        {"name": "binary.py", "reason": "tooling file content is not valid UTF-8 text"}
    ]


def test_import_tooling_ignores_non_matching_extensions():
    github = _CapturingGitHub(
        tooling_source_files={
            SOURCE_REPO: {
                "tools/good.py": VALID_PY,
                "tools/README.md": "# not a tool\n",
                "tools/notes.txt": "also not a tool\n",
            }
        }
    )
    result = import_tooling(github, REPO, source_repo=SOURCE_REPO, source_path="tools")

    assert result["imported"] == ["good.py"]
    assert result["rejected"] == []  # README.md/notes.txt were never candidates at all


def test_import_tooling_is_non_recursive():
    github = _CapturingGitHub(
        tooling_source_files={
            SOURCE_REPO: {
                "tools/good.py": VALID_PY,
                "tools/nested/deep.py": VALID_PY,
            }
        }
    )
    result = import_tooling(github, REPO, source_repo=SOURCE_REPO, source_path="tools")

    assert result["imported"] == ["good.py"]
    assert result["rejected"] == []


def test_import_tooling_nonexistent_source_folder_raises_with_not_found_prefix():
    github = _CapturingGitHub(tooling_source_files={SOURCE_REPO: {"other/thing.py": VALID_PY}})
    with pytest.raises(ToolingError, match=r"^tooling import path not found:"):
        import_tooling(github, REPO, source_repo=SOURCE_REPO, source_path="tools")
    # Source cloned, but agent repo never cloned since validation fails before walking
    assert [r for r, _ in github.cloned] == [SOURCE_REPO]


def test_import_tooling_empty_source_folder_still_yields_empty_result():
    # Create the folder but don't put any .py/.go files in it — using a non-.py/.go file
    github = _CapturingGitHub(tooling_source_files={SOURCE_REPO: {"tools/readme.txt": "placeholder\n"}})
    result = import_tooling(github, REPO, source_repo=SOURCE_REPO, source_path="tools")

    assert result == {"sha": None, "imported": [], "rejected": []}
    assert github.pushes == []


def test_import_tooling_empty_source_path_reads_repo_root():
    github = _CapturingGitHub(tooling_source_files={SOURCE_REPO: {"good.py": VALID_PY}})
    result = import_tooling(github, REPO, source_repo=SOURCE_REPO, source_path="")

    assert result["imported"] == ["good.py"]


# --- import_tooling: runtime_image re-pin ---------------------------------------


def test_import_tooling_with_runtime_image_rewrites_dockerfile_in_same_commit():
    github = _CapturingGitHub(tooling_source_files={SOURCE_REPO: {"tools/good.py": VALID_PY}})
    assert FAKE_DOCKERFILE_OLD_IMAGE != RUNTIME_IMAGE

    import_tooling(
        github, REPO, source_repo=SOURCE_REPO, source_path="tools", runtime_image=RUNTIME_IMAGE
    )

    assert github.captured_dockerfile == (
        "# Agent image: the shared runtime plus this agent's bundle.\n"
        f"FROM {RUNTIME_IMAGE}\n"
        "COPY bundle/ /bundle\n"
    )


def test_import_tooling_runtime_image_omitted_when_nothing_passes():
    # No agent-repo clone at all when nothing passes, so there's nothing to
    # re-pin either — passing runtime_image here must not raise or otherwise
    # misbehave just because the commit never happens.
    github = _CapturingGitHub(tooling_source_files={SOURCE_REPO: {"tools/broken.py": BROKEN_PY}})

    result = import_tooling(
        github, REPO, source_repo=SOURCE_REPO, source_path="tools", runtime_image=RUNTIME_IMAGE
    )

    assert result["sha"] is None
    assert [r for r, _ in github.cloned] == [SOURCE_REPO]


# --- list_tooling: shape / sorting ----------------------------------------------

UNATTESTED = {"attested": False, "locked": False, "locked_file": None}


def test_list_tooling_empty_when_no_tools_dir():
    github = FakeGitHub()
    kubectl = FakeKubectl()
    agent = FakeAgent()
    files, enforcement = list_tooling(
        github, kubectl, agent, repo=REPO, namespace=NAMESPACE, deploy=DEPLOY
    )
    assert files == []
    assert enforcement == UNATTESTED


def test_list_tooling_sorted_by_name_with_content_bytes():
    github = FakeGitHub(tooling_files={"b.py": "second = 2\n", "a.py": "first = 1\n"})
    kubectl = FakeKubectl()
    agent = FakeAgent()
    files, _ = list_tooling(github, kubectl, agent, repo=REPO, namespace=NAMESPACE, deploy=DEPLOY)

    assert [f["name"] for f in files] == ["a.py", "b.py"]
    assert files[0]["content_bytes"] == len(b"first = 1\n")
    assert files[1]["content_bytes"] == len(b"second = 2\n")
    for f in files:
        assert isinstance(f["delivered"], bool)


# --- list_tooling: delivered derivation (reuses list_intake's mechanics) --------
# Unaffected by the card probe (Task 4 addendum): every test below uses a bare
# FakeAgent(), so enforcement is always the unattested envelope — see the
# dedicated "enforcement probe" section further down for card-shape coverage.


def test_list_tooling_defaults_wired_happy_path_all_delivered():
    github = FakeGitHub(tooling_files={"tool.py": VALID_PY})
    kubectl = FakeKubectl()
    agent = FakeAgent()
    files, enforcement = list_tooling(
        github, kubectl, agent, repo=REPO, namespace=NAMESPACE, deploy=DEPLOY
    )
    assert files == [{"name": "tool.py", "content_bytes": len(VALID_PY.encode()), "delivered": True}]
    assert enforcement == UNATTESTED


def test_list_tooling_file_commit_equals_live_commit_is_delivered():
    github = FakeGitHub(
        tooling_files={"tool.py": VALID_PY},
        intake_commit_shas={"bundle/tools/tool.py": OLDER_SHA},
        rev_parse_results={"oldoldoldold": OLDER_SHA},
    )
    kubectl = FakeKubectl(images={(NAMESPACE, DEPLOY): f"gojira-app:{OLDER_SHA[:12]}"})
    agent = FakeAgent()
    files, _ = list_tooling(github, kubectl, agent, repo=REPO, namespace=NAMESPACE, deploy=DEPLOY)
    assert files[0]["delivered"] is True


def test_list_tooling_file_newer_than_image_is_not_delivered():
    github = FakeGitHub(
        tooling_files={"tool.py": VALID_PY},
        intake_commit_shas={"bundle/tools/tool.py": NEWER_SHA},
        rev_parse_results={"oldoldoldold": OLDER_SHA},
    )
    kubectl = FakeKubectl(images={(NAMESPACE, DEPLOY): f"gojira-app:{OLDER_SHA[:12]}"})
    agent = FakeAgent()
    files, _ = list_tooling(github, kubectl, agent, repo=REPO, namespace=NAMESPACE, deploy=DEPLOY)
    assert files[0]["delivered"] is False


def test_list_tooling_tag_unresolvable_is_not_delivered():
    github = FakeGitHub(
        tooling_files={"tool.py": VALID_PY},
        intake_commit_shas={"bundle/tools/tool.py": FAKE_LOCAL_HEAD_DEFAULT},
        rev_parse_results={"deadbeef0000": None},
    )
    kubectl = FakeKubectl(images={(NAMESPACE, DEPLOY): "gojira-app:deadbeef0000"})
    agent = FakeAgent()
    files, _ = list_tooling(github, kubectl, agent, repo=REPO, namespace=NAMESPACE, deploy=DEPLOY)
    assert files[0]["delivered"] is False


def test_list_tooling_digest_pinned_image_with_matching_tag_resolves_delivered():
    github = FakeGitHub(
        tooling_files={"tool.py": VALID_PY},
        intake_commit_shas={"bundle/tools/tool.py": OLDER_SHA},
        rev_parse_results={"oldoldoldold": OLDER_SHA},
    )
    kubectl = FakeKubectl(
        images={
            (NAMESPACE, DEPLOY): (
                f"gojira-app:{OLDER_SHA[:12]}"
                "@sha256:3c68b1e1a2a3f4b5c6d7e8f900112233445566778899aabbccddeeff0011223"
            )
        }
    )
    agent = FakeAgent()
    files, _ = list_tooling(github, kubectl, agent, repo=REPO, namespace=NAMESPACE, deploy=DEPLOY)
    assert files[0]["delivered"] is True


def test_list_tooling_mixed_statuses_across_multiple_files():
    github = FakeGitHub(
        tooling_files={"old.py": "stale = 1\n", "fresh.py": "brand_new = 1\n"},
        intake_commit_shas={
            "bundle/tools/old.py": OLDER_SHA,
            "bundle/tools/fresh.py": NEWER_SHA,
        },
        rev_parse_results={"oldoldoldold": OLDER_SHA},
    )
    kubectl = FakeKubectl(images={(NAMESPACE, DEPLOY): f"gojira-app:{OLDER_SHA[:12]}"})
    agent = FakeAgent()
    files, _ = list_tooling(github, kubectl, agent, repo=REPO, namespace=NAMESPACE, deploy=DEPLOY)

    by_name = {f["name"]: f["delivered"] for f in files}
    assert by_name == {"old.py": True, "fresh.py": False}


def test_list_tooling_clones_only_once():
    github = FakeGitHub(tooling_files={"a.py": VALID_PY, "b.go": "package main\n"})
    kubectl = FakeKubectl()
    agent = FakeAgent()
    list_tooling(github, kubectl, agent, repo=REPO, namespace=NAMESPACE, deploy=DEPLOY)
    assert len(github.cloned) == 1


# --- list_tooling: enforcement probe (Task 4: card probe + banner graduation) ---
#
# The card probe is independent of delivered/pending file state — every test
# below pairs a FIXED file fixture (one delivered, one not: file naming
# mirrors the delivered-derivation section above) with a varied FakeAgent
# card, proving the two axes don't interfere. `list_tooling` never adds a
# "status" key itself (module docstring: still no guessing) — that's the
# route's job (see test_web_tooling.py), so every file dict here keeps
# exactly its plain "delivered" bool regardless of enforcement.

_DELIVERED_AND_PENDING_GITHUB_KWARGS = {
    "tooling_files": {"old.py": "stale = 1\n", "fresh.py": "brand_new = 1\n"},
    "intake_commit_shas": {
        "bundle/tools/old.py": OLDER_SHA,
        "bundle/tools/fresh.py": NEWER_SHA,
    },
    "rev_parse_results": {"oldoldoldold": OLDER_SHA},
}


def _mixed_delivery_github_and_kubectl():
    github = FakeGitHub(**_DELIVERED_AND_PENDING_GITHUB_KWARGS)
    kubectl = FakeKubectl(images={(NAMESPACE, DEPLOY): f"gojira-app:{OLDER_SHA[:12]}"})
    return github, kubectl


def test_list_tooling_attested_card_version_1_not_locked():
    github, kubectl = _mixed_delivery_github_and_kubectl()
    agent = FakeAgent(card={"enforcement": {"version": 1, "hooks": ["old.py"], "locked": False}})
    files, enforcement = list_tooling(
        github, kubectl, agent, repo=REPO, namespace=NAMESPACE, deploy=DEPLOY
    )
    assert enforcement == {"attested": True, "locked": False, "locked_file": None}
    by_name = {f["name"]: f["delivered"] for f in files}
    assert by_name == {"old.py": True, "fresh.py": False}


def test_list_tooling_attested_and_locked_reports_locked_file():
    github, kubectl = _mixed_delivery_github_and_kubectl()
    agent = FakeAgent(
        card={
            "enforcement": {
                "version": 1,
                "hooks": ["old.py"],
                "locked": True,
                "locked_file": "old.py",
            }
        }
    )
    files, enforcement = list_tooling(
        github, kubectl, agent, repo=REPO, namespace=NAMESPACE, deploy=DEPLOY
    )
    assert enforcement == {"attested": True, "locked": True, "locked_file": "old.py"}
    # Files keep their plain delivered truth even when locked — list_tooling
    # itself never derives "status" (the route does).
    by_name = {f["name"]: f["delivered"] for f in files}
    assert by_name == {"old.py": True, "fresh.py": False}


def test_list_tooling_version_0_reads_unattested():
    github, kubectl = _mixed_delivery_github_and_kubectl()
    agent = FakeAgent(card={"enforcement": {"version": 0, "hooks": [], "locked": False}})
    _, enforcement = list_tooling(
        github, kubectl, agent, repo=REPO, namespace=NAMESPACE, deploy=DEPLOY
    )
    assert enforcement == UNATTESTED


def test_list_tooling_no_enforcement_key_reads_unattested():
    # Absent block entirely — a pre-enforcement runtime's real card shape.
    github, kubectl = _mixed_delivery_github_and_kubectl()
    agent = FakeAgent(card={"hooks": ["old.py"]})
    _, enforcement = list_tooling(
        github, kubectl, agent, repo=REPO, namespace=NAMESPACE, deploy=DEPLOY
    )
    assert enforcement == UNATTESTED


def test_list_tooling_unreachable_agent_reads_unattested_files_unaffected():
    github, kubectl = _mixed_delivery_github_and_kubectl()
    agent = FakeAgent(card_error="fetch_card request failed: connection refused")
    files, enforcement = list_tooling(
        github, kubectl, agent, repo=REPO, namespace=NAMESPACE, deploy=DEPLOY
    )
    assert enforcement == UNATTESTED
    by_name = {f["name"]: f["delivered"] for f in files}
    assert by_name == {"old.py": True, "fresh.py": False}


def test_list_tooling_probes_card_with_namespace_and_deploy():
    github = FakeGitHub()
    kubectl = FakeKubectl()
    agent = FakeAgent()
    list_tooling(github, kubectl, agent, repo=REPO, namespace=NAMESPACE, deploy=DEPLOY)
    assert agent.fetch_card_calls == [(NAMESPACE, DEPLOY)]


def test_list_tooling_probes_card_even_when_no_tools_dir():
    # Enforcement is a property of the running agent, not of whether it has
    # any tooling files at all — the probe still runs on an empty list.
    github = FakeGitHub()
    kubectl = FakeKubectl()
    agent = FakeAgent(card={"enforcement": {"version": 1, "hooks": [], "locked": False}})
    files, enforcement = list_tooling(
        github, kubectl, agent, repo=REPO, namespace=NAMESPACE, deploy=DEPLOY
    )
    assert files == []
    assert enforcement == {"attested": True, "locked": False, "locked_file": None}
