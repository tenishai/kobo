"""Capability generation + card lockstep (persona-metadata plan, Task 3).

Two responsibilities, kept in one small module since every caller needs both
together (HelloStep's tail, and the PATCH/regenerate routes in kobo_api.main):

- `generate_capability`: turn a persona body, its attached skills'
  descriptions, and its tooling filenames into one plain, user-facing
  sentence describing what an agent can actually do, for its directory
  card. Model-drafted when a model client is given and well-behaved, falling
  back to a deterministic tier (the persona body's own first sentence)
  whenever it isn't. Global Constraints: capability generation must NEVER
  block an agent's readiness — every failure here (a missing/unconfigured
  model client, a raised ClientError, an unusable reply) is caught and
  answered with the fallback tier instead; this function never raises.
- `commit_card_metadata`: the delivery half — clone the agent's repo, edit
  its `agent-card.json`'s `metadata` block in place
  (`kobo_api.composer.update_agent_card_metadata`, which also re-derives the
  card's skills, description, version and runtime from the repo; every
  other field survives byte-faithfully), commit, and push. Unlike generation, a delivery failure here
  IS the caller's to handle (HelloStep logs and leaves the card uncommitted;
  the PATCH/regenerate routes answer 502) — this raises `ClientError` same as
  every other GitHub-backed helper in this codebase, never swallowed here.

`skill_descriptions_for_agent`/`tooling_filenames_for` are the two other
generation inputs, factored out here (rather than duplicated in both
kobo_api.steps' HelloStep tail and kobo_api.main's regenerate route) since
both callers need the exact same "what does this agent actually have"
gathering logic.
"""

from __future__ import annotations

import logging
import tempfile
from pathlib import Path

from kobo_api import delivery
from kobo_api.clients import AgentClient, ClientError, GitHubClient, KubectlClient
from kobo_api.composer import (
    CAPABILITY_MAX_LEN,
    _plain_copy,
    fallback_capability,
    update_agent_card_metadata,
)
from kobo_api.purpose_tags import MAX_CREATE_TAGS, normalize_tags
from kobo_api.search import get_entry
from kobo_api.tooling import list_tooling

log = logging.getLogger(__name__)

# Global Constraints: a capability string is one plain sentence, at most
# `CAPABILITY_MAX_LEN` chars, with no em/en dashes. `CAPABILITY_MAX_LEN` and
# the cleanup helpers live in `kobo_api.composer`, because the agent card's
# description uses the same fallback tier (issue #147).


def generate_capability(
    model_client,
    persona_body: str,
    skill_descriptions: list[str],
    tooling_filenames: list[str],
) -> str:
    """One plain sentence describing what an agent can do, clamped to
    `CAPABILITY_MAX_LEN` chars and stripped of em/en dashes — model-drafted
    via `model_client.capability(...)` when `model_client` is given and
    well-behaved, the deterministic fallback tier (`fallback_capability`)
    otherwise.

    NEVER raises: `model_client` being `None` (unconfigured), its `.capability`
    call raising `ClientError`, or its reply being blank/unusable after
    cleanup all fall through to the same fallback tier — capability
    generation must never block an agent's readiness (Global Constraints).
    """
    if model_client is not None:
        try:
            raw = model_client.capability(persona_body, skill_descriptions, tooling_filenames)
        except ClientError as exc:
            log.warning("capability generation failed, falling back: %r", exc)
        else:
            cleaned = _plain_copy(raw)[:CAPABILITY_MAX_LEN] if isinstance(raw, str) else ""
            if cleaned:
                return cleaned
    return fallback_capability(persona_body)


def generate_tags(
    model_client, persona_body: str, capability: str, group: str | None
) -> list[str]:
    """Two to five purpose tags an agent gives itself after first contact,
    normalized the way caller-typed tags are and capped at the same
    ceiling. NEVER raises: no client, a ClientError, or an unusable reply
    all mean no tags, which is what the agent had before."""
    if model_client is None:
        return []
    try:
        raw = model_client.tags(persona_body, capability, group)
    except ClientError as exc:
        log.warning("tag generation failed, leaving tags unset: %r", exc)
        return []
    return normalize_tags(raw)[:MAX_CREATE_TAGS]


def clean_capability_text(text: str) -> str | None:
    """Plain-copy cleanup for a caller-submitted capability string (`PATCH
    .../metadata`) — strips em/en dashes defensively and collapses
    whitespace, same as every other tier (`_plain_copy`). Returns `None`
    (never raises) when the cleaned result is empty or still over
    `CAPABILITY_MAX_LEN` — the route treats `None` as "reject the whole
    PATCH", the same "caller input still over cap after normalizing is
    rejected outright, not silently truncated" policy
    `kobo_api.purpose_tags.clean_tags` applies to purpose_tags.
    """
    cleaned = _plain_copy(text)
    if not cleaned or len(cleaned) > CAPABILITY_MAX_LEN:
        return None
    return cleaned


def skill_descriptions_for_agent(engine, agent) -> list[str]:
    """Catalog descriptions for every catalog-sourced skill attached to
    `agent` (its `skills` JSON column — `{"name", "team", "sha"}` entries,
    `kobo_api.steps._stage_skills`'s own shape) — a cheap FTS lookup per
    skill (`kobo_api.search.get_entry`) rather than re-cloning the agent's
    bundle to re-read each SKILL.md, since the catalog already carries the
    same description text a skill's own frontmatter does.

    A `source: local` entry (never catalog-backed to begin with) or a skill
    whose catalog entry has since vanished contributes nothing rather than
    failing the whole lookup — this is a best-effort generation input, not
    something capability generation strictly depends on having in full.
    """
    descriptions = []
    for entry in agent.skills:
        if entry.get("source") == "local":
            continue
        skill_id = entry.get("name")
        if not skill_id:
            continue
        catalog_entry = get_entry(engine, "skill", skill_id, entry.get("team"))
        if catalog_entry is not None and catalog_entry.description:
            descriptions.append(catalog_entry.description)
    return descriptions


def tooling_filenames_for(
    github: GitHubClient,
    kubectl: KubectlClient,
    agent_client: AgentClient,
    *,
    repo: str,
    namespace: str,
    deploy: str,
) -> list[str]:
    """Best-effort tooling filenames for a capability-generation prompt —
    reuses `kobo_api.tooling.list_tooling`'s existing `bundle/tools/*`
    listing seam (its enforcement envelope is irrelevant here and discarded)
    rather than a new bespoke read.

    ANY failure (an unreachable agent pod, a broken github clone, ...) reads
    as "no tooling to mention" rather than aborting generation — this input
    is a nice-to-have for the sentence, never something generation can't
    proceed without.
    """
    try:
        files, _enforcement = list_tooling(
            github, kubectl, agent_client, repo=repo, namespace=namespace, deploy=deploy
        )
    except Exception as exc:  # noqa: BLE001 - best-effort input, never fatal
        log.warning("tooling listing failed while gathering capability inputs: %r", exc)
        return []
    return [f["name"] for f in files]


def commit_card_metadata(
    github: GitHubClient,
    repo: str,
    *,
    team: str | None,
    purpose_tags: list[str],
    capability: str,
) -> str:
    """Clone `repo`, update its bundle's `agent-card.json` `metadata` block
    in place, commit, and push. Returns the pushed commit's local sha
    (`github.local_head`) — same no-op-but-still-returns-head semantics as
    `kobo_api.knowledge.save_knowledge_file`/`kobo_api.intake.
    upload_intake_file`: a no-change edit still reports the (unchanged)
    current head.

    Raises `ClientError` on any GitHub-side failure — delivery failures are
    the caller's to handle (HelloStep's tail logs and leaves the card
    uncommitted; the PATCH/regenerate routes answer 502), never swallowed
    here the way `generate_capability`'s own model failures are.

    `capability or ""` (fix round 2, whole-arc review residual): the DB
    column this flows from (`Agent.capability`) is nullable and stays
    `None` until earned, and a team-only PATCH on an unearned agent passes
    that `None` straight through as `capability` here — without
    normalizing, the card would end up with a literal `null` where compose
    itself always bakes `""` (`kobo_api.composer._agent_card`). Normalizing
    at this seam, the one place every caller's capability value converges
    before it's written into the card payload, keeps an unearned agent's
    card byte-identical to what compose produced, regardless of which
    caller (PATCH, regenerate, HelloStep's tail) passed it through.
    """
    with tempfile.TemporaryDirectory() as tmp:
        workdir = Path(tmp)
        github.clone(repo, workdir)
        update_agent_card_metadata(
            workdir / "bundle", team=team, purpose_tags=purpose_tags, capability=capability or ""
        )
        delivery.push(github, workdir, "kobo: directory details updated")
        return github.local_head(workdir)
