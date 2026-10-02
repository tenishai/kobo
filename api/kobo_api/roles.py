"""Which half of the room this process is.

One image, one app, two jobs. The API serves people and their assistants:
routes, the health probe, the catalog, the per-replica health sweep. The
worker runs the pipelines and the sweeps that move agents along:
provisioning, the interrupted and waiting sweeps, the nightly learnings
pass. Until 2026-10-01 every replica did both, which is how a hot route
in the API failed an unrelated agent's build, and how any roll or
scale-down of the API would kill in-flight pipelines.

`KOBO_ROLE` picks the job. `all` (the default, and what every test
runs) is the old shape: routes launch pipelines inline and every loop
runs. `api` enqueues instead of launching, and runs none of the worker
loops. `worker` claims what the API enqueued and runs it, and serves
nothing but the health probe. Keeping `all` as the default means a
single-replica room, a developer's laptop and the whole test suite are
untouched by the split.
"""

from __future__ import annotations

import os
from collections.abc import Mapping

ROLE_ALL = "all"
ROLE_API = "api"
ROLE_WORKER = "worker"
ROLES = (ROLE_ALL, ROLE_API, ROLE_WORKER)

ENV_VAR = "KOBO_ROLE"


def role_from_env(environ: Mapping[str, str] | None = None) -> str:
    """`KOBO_ROLE`, defaulting to `all`; anything else is a boot error, loud,
    since a typo that silently fell back to `all` would run pipelines in
    every API pod again."""
    raw = (environ if environ is not None else os.environ).get(ENV_VAR, ROLE_ALL)
    role = raw.strip().lower() or ROLE_ALL
    if role not in ROLES:
        raise ValueError(f"{ENV_VAR} must be one of {', '.join(ROLES)}; got {raw!r}")
    return role


def serves_requests(role: str) -> bool:
    """Runs the request-side work: the catalog warmers, the persona seed,
    the per-replica health sweep. The routes themselves always exist; a
    worker is simply not behind the Service."""
    return role in (ROLE_ALL, ROLE_API)


def runs_pipelines(role: str) -> bool:
    """Claims queued agents and runs the sweeps that move agents along."""
    return role in (ROLE_ALL, ROLE_WORKER)


def launches_inline(role: str) -> bool:
    """Whether a route starts the pipeline thread itself (the old shape) or
    leaves the agent PENDING for a worker to claim."""
    return role == ROLE_ALL
