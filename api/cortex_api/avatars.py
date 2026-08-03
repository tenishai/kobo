"""Avatar module (P2d): a deterministic prompt template for generating an
agent's face, plus the clone/write/commit idiom that pushes the generated PNG
to `bundle/avatar.png` in the agent's repo (the bundle of record) — the same
disposable-clone pattern `cortex_api.knowledge` uses, just writing bytes
instead of text this time.

The generated PNG itself is produced by `clients.image` (`cortex_api.clients.
ImageClient`) and stored by the caller (`cortex_api.main`'s avatar route),
which also caches a base64 copy on the `Agent` row for fast list/detail
rendering — this module only owns the prompt text and the repo push, mirroring
how `cortex_api.knowledge` owns the file write/push but never the route's
guards or response shape.
"""

from __future__ import annotations

import tempfile
from pathlib import Path

from cortex_api.clients import GitHubClient


def avatar_prompt(agent_name: str, persona_description: str) -> str:
    """A deterministic prompt for `clients.image.generate` — same inputs
    always produce the exact same string, so a route can be tested against
    it byte-for-byte without depending on the image backend at all.

    Named person-first ("an assistant named {name}") rather than a bare
    label, and closes on an explicit no-text clause: the model this backs
    (Vertex's `gemini-2.5-flash-image`, see `VertexImageClient`) tends to
    render stray lettering into "portrait" prompts unless told not to, and an
    avatar is a small square rendered at icon size — any text in it reads as
    noise, never as a legible label.
    """
    return (
        f"friendly square avatar portrait for an assistant named {agent_name}, "
        f"persona flavor from {persona_description}; flat, minimal, centered, "
        f"plain background; no text in image."
    )


def set_avatar(github: GitHubClient, *, repo: str, png: bytes) -> str:
    """Write (or overwrite) `bundle/avatar.png` with `png` and push it.

    Exactly `cortex_api.knowledge.save_knowledge_file`'s clone/write/commit/
    local_head idiom, with two differences: this always writes the same
    fixed filename (there's only ever one avatar per agent, no user-supplied
    name to validate), and it writes bytes (`Path.write_bytes`) rather than
    text, since a PNG isn't UTF-8. Regenerating just overwrites the same
    path — the commit history is the only place an old avatar survives.

    Returns the pushed commit's local sha (`github.local_head`), a fresh
    clone's own HEAD right after the push — see `save_knowledge_file`'s
    docstring for why that's preferred over a `head_sha(repo)` API read.
    """
    with tempfile.TemporaryDirectory() as tmp:
        workdir = Path(tmp)
        github.clone(repo, workdir)
        bundle_dir = workdir / "bundle"
        bundle_dir.mkdir(parents=True, exist_ok=True)
        (bundle_dir / "avatar.png").write_bytes(png)
        github.commit_push(workdir, "cortex: set avatar")
        return github.local_head(workdir)
