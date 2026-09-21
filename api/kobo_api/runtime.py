"""Changing an existing agent's runtime image (#15).

Every agent is BUILT on the org default (`Config.runtime_image`) — there is
no runtime choice at create time. This module is the only way that moves
afterwards, and it exists for two jobs: pinning one agent to a specific tag
or digest of the org runtime, and carrying an agent that was created on an
older alternative runtime back onto the default. Without it either job
means destroy-and-recreate — which discards Slack wiring, accumulated agent
memory and channel membership, none of which is a runtime concern.

THE VALUE LIVES IN TWO PLACES AND BOTH HAVE TO MOVE TOGETHER. The
`Dockerfile`'s `FROM` ref is what a build actually uses, but
`Agent.runtime_image` (`kobo_api.models`) is what `kobo_api.main.
_runtime_image_for_repo` reads, and that accessor is what four *unrelated*
post-create write paths pass when they re-pin the Dockerfile on their own
commits:

    slack_setup.wire_slack            tooling.upload_tooling_file
    intake.upload_intake_file         tooling.import_tooling

So a change that rewrote only the Dockerfile would be reverted by the next
teach note, tool upload or Slack re-wire — and because the generated CI
rebuilds and hard-rotates the pod (Recreate strategy) on every push, the
revert would take effect rather than sit inert. The row is the authority;
the file is a projection of it. `commit_runtime_image` below moves the
file, and the caller moves the row in the same request.
"""

from __future__ import annotations

import tempfile
from pathlib import Path

from kobo_api.clients import GitHubClient
from kobo_api.composer import update_dockerfile_runtime_image


def validate_runtime_image(runtime_image: str, *, org_default: str) -> str | None:
    """`None` when `runtime_image` may be used, else the rejection copy.

    PROVENANCE IS THE WHOLE RULE: an image may be any ref under the org
    default's own registry root, and nothing else. Existence is CI's
    problem — a bad tag fails the first build loudly — but a foreign
    registry is not something a room ever runs, so it is refused before it
    can reach a Dockerfile.

    The root is derived from `org_default` rather than configured beside
    it, so the one runtime an org builds on is also the one thing that says
    where its images come from; there is no second list to drift from it.

    Returns the message rather than raising, because both callers answer
    422 with it in a JSON body and neither wants an exception to format.
    """
    registry_root = org_default.rsplit("/", 1)[0] + "/"
    if not runtime_image.startswith(registry_root):
        return (
            f"runtime_image must live under {registry_root!r} "
            f"(got {runtime_image!r})"
        )
    return None


def commit_runtime_image(github: GitHubClient, repo: str, runtime_image: str) -> str:
    """Clone `repo`, re-point its `Dockerfile`'s `FROM` at `runtime_image`,
    commit, and push. Returns the pushed commit's local sha.

    Same clone-mutate-push idiom as `kobo_api.metadata.commit_card_metadata`
    and `kobo_api.tooling.upload_tooling_file`, and the same
    no-op-still-returns-head semantics: re-pinning to the image the file
    already names pushes nothing and reports the unchanged head, because
    `update_dockerfile_runtime_image` is idempotent by construction.

    Raises `ClientError` on any GitHub-side failure — the caller answers
    502 and, critically, must do so BEFORE committing its session, so the
    row and the file cannot disagree. Nothing is swallowed here.

    THE PUSH IS THE DEPLOY. The agent repo's generated CI rebuilds and
    hard-rotates the pod on every push, so there is no separate rebuild or
    redeploy step for a caller to forget; landing the commit is what moves
    the running agent onto the new runtime.
    """
    with tempfile.TemporaryDirectory() as tmp:
        workdir = Path(tmp)
        github.clone(repo, workdir)
        update_dockerfile_runtime_image(workdir / "Dockerfile", runtime_image)
        github.commit_push(workdir, f"kobo: runtime image -> {runtime_image}")
        return github.local_head(workdir)
