"""Avatar module (P2d): a deterministic prompt template for generating an
agent's face, plus the clone/write/commit idiom that pushes the generated PNG
to `bundle/avatar.png` in the agent's repo (the bundle of record) — the same
disposable-clone pattern `kobo_api.knowledge` uses, just writing bytes
instead of text this time.

The generated PNG itself is produced by `clients.image` (`kobo_api.clients.
ImageClient`) and stored by the caller (`kobo_api.main`'s avatar route),
which also caches a base64 copy on the `Agent` row for fast list/detail
rendering — this module only owns the prompt text and the repo push, mirroring
how `kobo_api.knowledge` owns the file write/push but never the route's
guards or response shape.
"""

from __future__ import annotations

import tempfile
from pathlib import Path

from kobo_api.clients import GitHubClient


def avatar_prompt(agent_name: str, persona_description: str, purpose_tags: list[str] | None = None) -> str:
    """A deterministic prompt for `clients.image.generate` — same inputs
    always produce the exact same string, so a route can be tested against
    it byte-for-byte without depending on the image backend at all.

    Named person-first ("a character named {name}") and enriched with persona
    description and optional purpose tags. Closes on an explicit no-text
    clause: the model this backs (Vertex's `gemini-2.5-flash-image`, see
    `VertexImageClient`) tends to render stray lettering into "portrait"
    prompts unless told not to, and an avatar is a small square rendered at
    icon size -- any text in it reads as noise, never as a legible label.

    When purpose_tags is empty or None, the themes clause is omitted entirely.
    """
    parts = [
        f"friendly square avatar portrait of a character named {agent_name}; "
        f"persona: {persona_description};"
    ]

    if purpose_tags:
        tags_str = ", ".join(purpose_tags)
        parts.append(f" themes: {tags_str};")

    parts.append(
        f" the face and styling should feel like a \"{agent_name}\" -- let the name "
        f"and persona set the character's vibe; flat, minimal, centered, plain "
        f"background; no text in image."
    )

    return "".join(parts)


def set_avatar(github: GitHubClient, *, repo: str, png: bytes) -> str:
    """Write (or overwrite) `bundle/avatar.png` with `png` and push it.

    Exactly `kobo_api.knowledge.save_knowledge_file`'s clone/write/commit/
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
        github.commit_push(workdir, "kobo: set avatar")
        return github.local_head(workdir)
