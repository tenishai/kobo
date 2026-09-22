"""The repository typeahead's matching and its cache (kobo_api/repo_search.py).

The rules here are the ones a person typing into the connect form feels:
nothing comes back for an empty box, an archived repository is never
offered, the repository whose NAME starts with what was typed comes before
one that merely contains it, and two that match equally well are ordered by
which was worked on most recently.

The listing behind them is one GitHub read shared by everyone in the room,
held for five minutes. The clock is injected here rather than slept
through, so the TTL is pinned exactly.
"""

from __future__ import annotations

import threading

import pytest

from kobo_api.clients import ClientError, InstallationRepo
from kobo_api.repo_search import MAX_MATCHES, TTL_S, RepoIndex


def _repo(full_name: str, **overrides) -> InstallationRepo:
    return InstallationRepo(
        full_name=full_name,
        archived=overrides.get("archived", False),
        private=overrides.get("private", True),
        default_branch=overrides.get("default_branch", "main"),
        pushed_at=overrides.get("pushed_at", "2026-01-01T00:00:00Z"),
    )


class StubGitHub:
    """Answers `list_installation_repos` from a fixed list and counts the
    calls, which is how the cache is observed."""

    def __init__(self, repos: list[InstallationRepo], error: str | None = None):
        self.repos = list(repos)
        self.error = error
        self.calls = 0

    def list_installation_repos(self) -> list[InstallationRepo]:
        self.calls += 1
        if self.error is not None:
            raise ClientError(self.error)
        return list(self.repos)


def _names(matches: list[InstallationRepo]) -> list[str]:
    return [match.full_name for match in matches]


# --- what matches -----------------------------------------------------------


def test_an_empty_query_returns_nothing():
    """The rule, not an edge case: the field shows nothing on focus or on a
    click, and an empty box is what focus looks like from here."""
    index = RepoIndex(StubGitHub([_repo("example-org/clio")]))

    assert index.search("", now=0.0) == ([], False)


def test_a_query_of_only_spaces_returns_nothing():
    index = RepoIndex(StubGitHub([_repo("example-org/clio")]))

    assert index.search("   ", now=0.0) == ([], False)


def test_matches_a_prefix_of_the_name():
    index = RepoIndex(StubGitHub([_repo("example-org/clio"), _repo("example-org/warden")]))

    matches, truncated = index.search("cli", now=0.0)

    assert _names(matches) == ["example-org/clio"]
    assert truncated is False


def test_matches_a_prefix_of_the_full_name():
    index = RepoIndex(StubGitHub([_repo("example-org/clio"), _repo("other-org/clio")]))

    matches, _ = index.search("example-org/cl", now=0.0)

    assert _names(matches) == ["example-org/clio"]


def test_matches_a_substring_of_the_name():
    index = RepoIndex(StubGitHub([_repo("example-org/team-chatbot")]))

    assert _names(index.search("chat", now=0.0)[0]) == ["example-org/team-chatbot"]


def test_the_query_is_matched_case_insensitively():
    index = RepoIndex(StubGitHub([_repo("example-org/Clio")]))

    assert _names(index.search("  CLI  ", now=0.0)[0]) == ["example-org/Clio"]


def test_an_archived_repository_is_never_offered():
    """It cannot be connected, and offering it is how someone spends a scan
    finding that out."""
    index = RepoIndex(
        StubGitHub([_repo("example-org/clio", archived=True), _repo("example-org/clio-old")])
    )

    assert _names(index.search("clio", now=0.0)[0]) == ["example-org/clio-old"]


def test_nothing_matches_when_the_query_is_in_neither_half():
    index = RepoIndex(StubGitHub([_repo("example-org/clio")]))

    assert index.search("warden", now=0.0) == ([], False)


def test_the_owner_alone_is_not_a_substring_match_on_the_name():
    """A substring match reads the NAME, not the whole `owner/name`: typing
    an organisation everything belongs to would otherwise return the whole
    installation one keystroke in."""
    index = RepoIndex(StubGitHub([_repo("example-org/clio")]))

    assert index.search("xample", now=0.0) == ([], False)


# --- what order -------------------------------------------------------------


def test_name_prefix_comes_before_full_name_prefix_comes_before_substring():
    index = RepoIndex(
        StubGitHub(
            [
                _repo("example-org/team-clio"),   # substring of the name
                _repo("clio-org/warden"),         # prefix of the full name
                _repo("example-org/clio"),        # prefix of the name
            ]
        )
    )

    matches, _ = index.search("clio", now=0.0)

    assert _names(matches) == [
        "example-org/clio",
        "clio-org/warden",
        "example-org/team-clio",
    ]


def test_matches_of_the_same_kind_are_ordered_by_most_recently_pushed():
    index = RepoIndex(
        StubGitHub(
            [
                _repo("example-org/clio-old", pushed_at="2019-04-01T00:00:00Z"),
                _repo("example-org/clio-new", pushed_at="2026-09-01T00:00:00Z"),
                _repo("example-org/clio-mid", pushed_at="2024-02-01T00:00:00Z"),
            ]
        )
    )

    matches, _ = index.search("clio", now=0.0)

    assert _names(matches) == [
        "example-org/clio-new",
        "example-org/clio-mid",
        "example-org/clio-old",
    ]


def test_a_repository_github_gave_no_push_date_sorts_last():
    index = RepoIndex(
        StubGitHub(
            [
                _repo("example-org/clio-unknown", pushed_at=""),
                _repo("example-org/clio-old", pushed_at="2019-04-01T00:00:00Z"),
            ]
        )
    )

    assert _names(index.search("clio", now=0.0)[0]) == [
        "example-org/clio-old",
        "example-org/clio-unknown",
    ]


# --- how many ---------------------------------------------------------------


def test_at_most_ten_come_back_and_the_rest_are_reported_as_truncated():
    index = RepoIndex(
        StubGitHub([_repo(f"example-org/clio-{i:02d}") for i in range(MAX_MATCHES + 3)])
    )

    matches, truncated = index.search("clio", now=0.0)

    assert len(matches) == MAX_MATCHES
    assert truncated is True


def test_exactly_ten_matches_is_not_truncated():
    index = RepoIndex(
        StubGitHub([_repo(f"example-org/clio-{i:02d}") for i in range(MAX_MATCHES)])
    )

    matches, truncated = index.search("clio", now=0.0)

    assert len(matches) == MAX_MATCHES
    assert truncated is False


# --- the cache --------------------------------------------------------------


def test_the_listing_is_read_once_and_reused_inside_the_ttl():
    github = StubGitHub([_repo("example-org/clio")])
    index = RepoIndex(github)

    index.search("clio", now=0.0)
    index.search("cli", now=TTL_S - 1)

    assert github.calls == 1


def test_the_listing_is_read_again_once_the_ttl_has_passed():
    github = StubGitHub([_repo("example-org/clio")])
    index = RepoIndex(github)

    index.search("clio", now=0.0)
    index.search("clio", now=TTL_S)

    assert github.calls == 2


def test_an_empty_query_never_reads_github_at_all():
    """Nothing to answer, so nothing to fetch -- a field being focused is
    not a reason to spend a listing."""
    github = StubGitHub([_repo("example-org/clio")])
    index = RepoIndex(github)

    index.search("", now=0.0)

    assert github.calls == 0


def test_only_one_refresh_runs_at_a_time():
    """Twenty people typing into the form the moment the cache expires is
    one GitHub listing, not twenty."""
    started = threading.Event()
    release = threading.Event()

    class SlowGitHub(StubGitHub):
        def list_installation_repos(self):
            started.set()
            release.wait(timeout=5)
            return super().list_installation_repos()

    github = SlowGitHub([_repo("example-org/clio")])
    index = RepoIndex(github)

    threads = [
        threading.Thread(target=lambda: index.search("clio", now=0.0)) for _ in range(5)
    ]
    for thread in threads:
        thread.start()
    started.wait(timeout=5)
    release.set()
    for thread in threads:
        thread.join(timeout=5)

    assert github.calls == 1


def test_a_github_failure_reaches_the_caller():
    """"the room could not ask" is not "the room can see no repositories",
    and the route answers the two differently."""
    index = RepoIndex(StubGitHub([], error="gh: Bad credentials (HTTP 401)"))

    with pytest.raises(ClientError):
        index.search("clio", now=0.0)


def test_a_failed_refresh_is_retried_rather_than_cached():
    github = StubGitHub([_repo("example-org/clio")], error="gh: Bad credentials (HTTP 401)")
    index = RepoIndex(github)

    with pytest.raises(ClientError):
        index.search("clio", now=0.0)
    github.error = None

    assert _names(index.search("clio", now=0.0)[0]) == ["example-org/clio"]
