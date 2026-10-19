"""Org config repo (kobo-config) load + merge (Phase A Task 2).

At boot, `build_default_app` (kobo_api.main) clones the client's org's
`kobo-config` repo (named by `Config.org_repo`) and reads its `org.yaml`,
whose values — when present and valid — override the matching fields on the
locally-loaded `Config`. This is the only place org values ever reach a
running kobo: every downstream reader (catalog search, bootstrap, org status,
...) just reads the already-merged `Config` `build_default_app` hands to
`create_app`, unchanged.

Boot must never crash on an org-config problem — a missing/unset `org_repo`,
an unreachable/private repo, a malformed `org.yaml`, or an org.yaml value
that fails `Config`'s own validation all fall back to the local config
untouched, paired with an `OrgState` an unwired room can render honestly
(Tasks 3/8 consume `app.state.org`).
"""

from __future__ import annotations

import logging
import tempfile
import warnings
from dataclasses import dataclass
from pathlib import Path

import yaml
from pydantic import ValidationError

from kobo_api.clients import ClientError, GitHubClient
from kobo_api.config import Config

log = logging.getLogger(__name__)

# The only Config fields an org.yaml may ever set. Deliberately an allowlist
# (not "anything Config has, minus a blocklist"): a machine-local field like
# `db_url` or `workdir` must never become org-supplied just because a future
# Config field gets added and someone forgets to think about this list.
ORG_KEYS = frozenset(
    {
        "github_org",
        "skills_repo",
        "skills_bucket",
        "personas_bucket",
        "gojira_cluster",
        "runtime_image",
        # Agent memory replication target (resilience arc). The lesson that
        # put this comment here: Task 14 added the Config FIELD but not this
        # allowlist entry, and one unrecognized org.yaml key unwires ALL org
        # config, so setting the key in org.yaml degraded the whole room
        # (MCP + catalogs) on the next boot. A Config field an org should
        # set is not org-settable until it is named HERE.
        "memory_replica_bucket",
        "broker_url",
        "liminal_url",
        "noto_url",
        "noto_project",
        "reader_groups",
        "gcp_project",
        "vertex_project",
        "vertex_location",
        "model_group",
        "avatar_model",
        "avatar_location",
        "models",
        "skills_publish_wif_provider",
        "skills_publish_service_account",
        "skills_publish_external",
        "slack_operators",
        "operators",
        "admin_groups",
        "admin_panel",
        "logout_url",
        "gateway_config_repo",
        "gateway_namespace",
        "cabal_url",
        "agent_endpoint_template",
        "groups",
        "okta_domain",
        # MCP server arc, Task 1 -- mcp is a nested object; org.yaml sets it
        # as a mapping, same as groups above.
        "mcp",
    }
)

# Keys an org.yaml may still carry that no longer map to anything on
# `Config`. They are ACCEPTED and then dropped: an unrecognized key unwires
# ALL org config (see `load_org_config` below), so removing a Config field
# whose key is already written into a deployment's org.yaml would degrade
# that room's whole catalog surface on the next boot. Retiring the key here
# instead of deleting it outright keeps boot safe while the file catches up.
# Disjoint from ORG_KEYS by construction — a key is live or retired, never
# both. `_disclose_retired` says so once per boot rather than letting the
# key look honored.
RETIRED_ORG_KEYS = frozenset({"runtimes"})

# Keys that changed name and still mean the same thing: an org.yaml written
# before the rename keeps working, with one WARNING per old key so the file
# catches up. Applied before the unknown-key check, since an unknown key
# unwires ALL org config. The new name wins when both are present.
RENAMED_ORG_KEYS = {
    # noto was named argus until 2026-10-12.
    "argus_url": "noto_url",
    "argus_project": "noto_project",
}

# What each retired key's WARNING says, so an operator reading pod logs
# learns what replaced it rather than only that the key did nothing.
_RETIRED_REASONS = {
    "runtimes": "every agent builds on runtime_image",
}


@dataclass
class OrgState:
    """Whether org config is wired in, and why not when it isn't.

    Stored at `app.state.org` (`kobo_api.main.create_app`) so routes that
    need to render an honest "org not configured" / "org config broken"
    state (Tasks 3/8) can do so, rather than an unwired room's empty
    catalogs looking like a coincidence.
    """

    wired: bool
    repo: str | None
    error: str | None = None  # plain sentence when load failed / repo unset


def _revalidate(merged: Config) -> Config:
    """Re-validate a `model_copy(update=...)`'d Config so a bad org.yaml
    value (e.g. `models` not shaped like `list[ModelChoice]`) surfaces as a
    `pydantic.ValidationError` the caller can turn into an unwired
    `OrgState`, rather than silently living on as an un-coerced raw value.

    `model_copy(update=...)` sets fields directly without validating them,
    so the `merged.model_dump()` below can be dumping a model still holding
    e.g. plain dicts in a `list[ModelChoice]` field — pydantic warns about
    that mismatch (`PydanticSerializationUnexpectedValue`) even though it's
    expected and is exactly what the `model_validate` right after this
    fixes; suppressed here rather than left to spam every successful org
    merge that touches `models`.
    """
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", UserWarning)
        dumped = merged.model_dump()
    return Config.model_validate(dumped)


def _shown(value: object) -> str:
    """A value as it may appear in a log line: scalars verbatim (the whole
    point is seeing the two runtime_image refs side by side), composites by
    shape only — `groups`/`models` dumped verbatim would drown the line, and
    `operators`/`slack_operators` would echo addresses into logs."""
    if value is None or isinstance(value, (str, int, float, bool)):
        return repr(value)
    try:
        size = len(value)  # type: ignore[arg-type]
    except TypeError:
        return f"a {type(value).__name__}"
    return f"a {type(value).__name__} with {size} entries"


def _disclose_overrides(local: Config, merged: Config, org_keys: set[str]) -> None:
    """Say when org.yaml defeats a value the deployment config explicitly
    set (IE-6065): the Secret carried a digest-pinned `runtime_image`, this
    merge replaced it with a floating tag on every boot, and nothing showed
    that from either file alone — only a diff of the two. One WARNING per
    differing key (the app configures no logging, so INFO from this module
    never reaches pod logs). Overriding an untouched default is org config
    doing its job and stays silent."""
    for key in sorted(org_keys & local.model_fields_set):
        before, after = getattr(local, key), getattr(merged, key)
        if before != after:
            log.warning(
                "org.yaml overrides %s: deployment config had %s, org.yaml sets %s",
                key,
                _shown(before),
                _shown(after),
            )


def _translate_renamed(data: dict) -> dict:
    """Move each old-named key to its current name, warning once per key; a
    file that sets both keeps the current one."""
    out = dict(data)
    for old, new in sorted(RENAMED_ORG_KEYS.items()):
        if old not in out:
            continue
        value = out.pop(old)
        if new in out:
            log.warning("org.yaml sets both %r and %r; %r wins", old, new, new)
            continue
        log.warning("org.yaml key %r was renamed %r; honoured, rename it in the file", old, new)
        out[new] = value
    return out


def _disclose_retired(present: set[str]) -> None:
    """Say which retired keys org.yaml still carries, one WARNING each.
    Same level and reasoning as `_disclose_overrides` above: the app
    configures no logging, so INFO from this module never reaches pod logs,
    and a key that reads as configured but changes nothing is exactly the
    kind of silence that costs a debugging session."""
    for key in sorted(present & RETIRED_ORG_KEYS):
        log.warning(
            "org.yaml key %r is retired and ignored: %s", key, _RETIRED_REASONS[key]
        )


def load_org_config(local: Config, github: GitHubClient) -> tuple[Config, OrgState]:
    """Clone `local.org_repo`, read its `org.yaml`, and merge validated org
    values over `local`.

    Boot-time only: a fresh `tempfile.TemporaryDirectory()` clone per call —
    no fetch/caching — meant to run once in `build_default_app`, never on a
    request path.

    Every failure mode (repo unset, clone failure, missing/empty/malformed
    org.yaml, an unknown key, or a value that fails `Config`'s own
    validation) returns `local` unchanged, paired with an unwired `OrgState`
    naming a plain reason (or, for an unknown key, the key itself) — this
    function never raises.
    """
    if not local.org_repo:
        return local, OrgState(False, None, "no org configured")

    with tempfile.TemporaryDirectory() as tmp_dir:
        dest = Path(tmp_dir)
        try:
            github.clone(local.org_repo, dest)
        except ClientError as exc:
            log.warning("org config clone failed for %s: %s", local.org_repo, exc)
            return local, OrgState(
                False, local.org_repo, f"couldn't clone {local.org_repo}: {exc}"
            )

        org_yaml_path = dest / "org.yaml"
        if not org_yaml_path.exists():
            return local, OrgState(
                False, local.org_repo, f"{local.org_repo} has no org.yaml"
            )

        raw = org_yaml_path.read_text()
        try:
            data = yaml.safe_load(raw)
        except yaml.YAMLError:
            log.warning("org config: org.yaml in %s is not valid YAML", local.org_repo, exc_info=True)
            return local, OrgState(
                False, local.org_repo, "org.yaml isn't valid YAML — fix the file in the org config repo"
            )

        if data is None:
            return local, OrgState(False, local.org_repo, "org.yaml is empty")
        if not isinstance(data, dict):
            return local, OrgState(
                False,
                local.org_repo,
                f"org.yaml must be a mapping, got {type(data).__name__}",
            )

        data = _translate_renamed(data)
        unknown_keys = sorted(set(data) - ORG_KEYS - RETIRED_ORG_KEYS)
        if unknown_keys:
            return local, OrgState(
                False,
                local.org_repo,
                f"org.yaml has an unrecognized key: {unknown_keys[0]!r}",
            )

        _disclose_retired(set(data))
        data = {k: v for k, v in data.items() if k not in RETIRED_ORG_KEYS}

        try:
            merged = _revalidate(local.model_copy(update=data))
        except ValidationError as exc:
            fields = sorted({str(err["loc"][0]) for err in exc.errors() if err.get("loc")})
            names = ", ".join(fields) if fields else "one of its values"
            log.warning("org config: org.yaml failed validation: %s", exc)
            return local, OrgState(
                False, local.org_repo, f"org.yaml has an invalid value for {names}"
            )

        _disclose_overrides(local, merged, set(data))
        return merged, OrgState(True, local.org_repo)
