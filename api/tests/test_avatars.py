"""Tests for cortex_api.avatars: the deterministic avatar prompt template
and the clone/write/commit push of a generated PNG to `bundle/avatar.png`,
against the FakeGitHub fake — no real git/Vertex AI involved.
"""

from __future__ import annotations

from pathlib import Path

from cortex_api.avatars import avatar_prompt, set_avatar
from tests.fakes import FAKE_LOCAL_HEAD_DEFAULT, FakeGitHub

NAME = "clio"
DESCRIPTION = "A friendly, concise generalist assistant."


class _CapturingGitHub(FakeGitHub):
    """Snapshots bundle/avatar.png bytes the moment commit_push is called —
    the only window in which set_avatar's cloned workdir still exists before
    it's discarded. Same trick as test_knowledge.py's own _CapturingGitHub,
    replicated locally rather than imported cross-file."""

    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self.captured_avatar_bytes: bytes | None = None

    def commit_push(self, workdir: Path, message: str) -> bool:
        avatar_path = workdir / "bundle" / "avatar.png"
        if avatar_path.is_file():
            self.captured_avatar_bytes = avatar_path.read_bytes()
        return super().commit_push(workdir, message)


# --- avatar_prompt --------------------------------------------------------------


def test_avatar_prompt_contains_name_and_description():
    prompt = avatar_prompt(NAME, DESCRIPTION)
    assert NAME in prompt
    assert DESCRIPTION in prompt


def test_avatar_prompt_contains_no_text_in_image_clause():
    prompt = avatar_prompt(NAME, DESCRIPTION)
    assert "no text in image" in prompt


def test_avatar_prompt_is_deterministic():
    first = avatar_prompt(NAME, DESCRIPTION)
    second = avatar_prompt(NAME, DESCRIPTION)
    assert first == second


def test_avatar_prompt_exact_template():
    # Pins the exact template text — a route test asserts the fake image
    # client recorded exactly this string, so the template's shape is a
    # contract, not just "contains the right substrings".
    assert avatar_prompt(NAME, DESCRIPTION) == (
        "friendly square avatar portrait for an assistant named clio, "
        "persona flavor from A friendly, concise generalist assistant.; "
        "flat, minimal, centered, plain background; no text in image."
    )


# --- set_avatar ------------------------------------------------------------------


def test_set_avatar_writes_exact_bytes_commits_and_returns_local_head():
    github = _CapturingGitHub()
    png_bytes = b"\x89PNG\r\n\x1a\nfake-png-payload"

    result = set_avatar(github, repo="acme/agent-clio", png=png_bytes)

    assert github.captured_avatar_bytes == png_bytes
    assert github.pushes[-1][1] == "cortex: set avatar"
    assert result == FAKE_LOCAL_HEAD_DEFAULT
    assert github.local_head_calls == [github.pushes[-1][0]]


def test_set_avatar_overwrite_regenerates_with_new_bytes():
    github = _CapturingGitHub()
    first_bytes = b"\x89PNG\r\n\x1a\nfirst-generation"
    second_bytes = b"\x89PNG\r\n\x1a\nsecond-generation"

    set_avatar(github, repo="acme/agent-clio", png=first_bytes)
    assert github.captured_avatar_bytes == first_bytes

    set_avatar(github, repo="acme/agent-clio", png=second_bytes)
    assert github.captured_avatar_bytes == second_bytes


def test_set_avatar_does_not_leak_the_temporary_clone_dir():
    captured: dict[str, Path] = {}
    github = _CapturingGitHub()
    real_clone = github.clone

    def spying_clone(repo, dest):
        real_clone(repo, dest)
        captured["dest"] = dest

    github.clone = spying_clone
    set_avatar(github, repo="acme/agent-clio", png=b"bytes")
    assert not captured["dest"].exists()
