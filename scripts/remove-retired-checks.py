"""One-off (2026-11-20): take the room's retired checks off the agents that have them.

The probe guard (baseline-probe-guard.py) came with every agent the room built. It
refused real questions ("show me how to rotate the api key", "which credentials
expire this month"), let "what is your api key" through, and guarded keys the model
never sees, so the room retired it (`kobo_api.marketplace.RETIRED_BASELINE_CHECKS`)
and new agents no longer get it. This takes it off the agents made before, once, by
hand, outside the room's UI. Run it inside a kobo API pod:

    kubectl --context <ctx> -n <ns> exec -i deploy/<kobo-api> -- python - < scripts/remove-retired-checks.py
    kubectl --context <ctx> -n <ns> exec -i deploy/<kobo-api> -- python - --apply < scripts/remove-retired-checks.py
    ... python - --apply --agent ledger < scripts/remove-retired-checks.py    (one agent)

Without --apply it changes nothing and prints what it would do. Per agent the room
built (READY, composed, with a repository) that still carries a retired check: one
commit removes the file, which restarts the agent once, and the room's audit log
says what went. The leak guard and any check an engineer added stay. An agent with
nothing retired is skipped, so a second run does nothing.
"""

from __future__ import annotations

import argparse
import sys

from kobo_api import audit, delivery
from kobo_api.bundle_publish import RegistryPublisher
from kobo_api.clients import Clients, CommandRunner, GhCLI, installation_token_provider
from kobo_api.config import Config
from kobo_api.db import init_db, make_engine, make_session_factory
from kobo_api.kinds import KIND_COMPOSED
from kobo_api.marketplace import RETIRED_BASELINE_CHECKS
from kobo_api.models import Agent, AgentStatus
from kobo_api.org import load_org_config
from kobo_api.tooling import delete_tooling_file, existing_tool_filenames


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--apply", action="store_true", help="make the commits (default: dry run)")
    parser.add_argument("--agent", help="only this agent")
    args = parser.parse_args(argv)

    local = Config.load(None)
    provider = installation_token_provider(local)
    github = GhCLI(runner=CommandRunner(), token_provider=provider.token if provider else None)
    config, _ = load_org_config(local, github)
    engine = make_engine(config.db_url)
    init_db(engine)
    session = make_session_factory(engine)()
    clients = Clients.real(config)
    # The same delivery the room wires at boot: with a registry, a commit publishes
    # the bundle and restarts the agent instead of building it.
    delivery.configure(
        delivery.Delivery(
            kubectl=clients.kubectl, publisher=RegistryPublisher(), registry=config.bundle_registry,
            runtime_image=config.runtime_image, replica_bucket=config.memory_replica_bucket,
        ) if config.bundle_registry else None
    )

    query = session.query(Agent).filter(Agent.status == AgentStatus.READY)
    if args.agent:
        query = query.filter(Agent.name == args.agent)
    removed = skipped = failed = 0
    for agent in query.order_by(Agent.name):
        if (agent.kind or KIND_COMPOSED) != KIND_COMPOSED or not agent.repo_full_name:
            continue
        try:
            retired = sorted(existing_tool_filenames(clients.github, agent.repo_full_name) & RETIRED_BASELINE_CHECKS)
        except Exception as exc:  # noqa: BLE001 -- one agent's trouble never stops the rest
            failed += 1
            print(f"{agent.name}: could not read its files ({exc})")
            continue
        if not retired:
            skipped += 1
            print(f"{agent.name}: nothing retired, skipped")
            continue
        if not args.apply:
            removed += 1
            print(f"{agent.name}: would remove {', '.join(retired)} (one commit, one restart)")
            continue
        try:
            for filename in retired:
                delete_tooling_file(clients.github, agent.repo_full_name, filename)
        except Exception as exc:  # noqa: BLE001
            failed += 1
            print(f"{agent.name}: could not remove {', '.join(retired)} ({exc})")
            continue
        audit.record(
            session, action="tooling.delete", target_kind="agent", target_id=agent.name,
            agent_name=agent.name, actor=audit.ROOM_ACTOR,
            summary=f"the room removed the retired check(s) {', '.join(retired)} from agent {agent.name}",
            detail={"removed": retired},
        )
        session.commit()
        removed += 1
        print(f"{agent.name}: removed {', '.join(retired)}")
    print(f"\n{'removed from' if args.apply else 'would remove from'} {removed} agent(s); "
          f"skipped {skipped}; failed {failed}")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
