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

Without --apply it changes nothing and prints what it would do. Per READY agent
that still carries a retired check: a composed one gets one commit removing the
file, which restarts it once; a directory one (issue #207) has the file deleted
from its folder and is reloaded the way the room's load step does it. The room's
audit log says what went, one row per file, as the panel's own removal records it.
The leak guard and any check an engineer added stay. An agent with nothing retired
is skipped, so a second run does nothing.
"""

from __future__ import annotations

import argparse
import sys
from types import SimpleNamespace

from kobo_api import audit, delivery
from kobo_api.bundle_publish import RegistryPublisher
from kobo_api.clients import Clients, CommandRunner, GhCLI, installation_token_provider
from kobo_api.config import Config
from kobo_api.db import init_db, make_engine, make_session_factory
from kobo_api.kinds import KIND_COMPOSED, KIND_DIRECTORY
from kobo_api.marketplace import RETIRED_BASELINE_CHECKS
from kobo_api.models import Agent, AgentStatus
from kobo_api.org import load_org_config
from kobo_api.steps import LoadStep, agent_directory
from kobo_api.tooling import delete_tooling_file


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
    session_factory = make_session_factory(engine)
    session = session_factory()
    clients = Clients.real(config)
    # The same delivery the room wires at boot: with a registry, a commit publishes
    # the bundle and restarts the agent instead of building it.
    delivery.configure(
        delivery.Delivery(
            kubectl=clients.kubectl, publisher=RegistryPublisher(), registry=config.bundle_registry,
            runtime_image=config.runtime_image, replica_bucket=config.memory_replica_bucket,
            # A push that converts an older agent changes its gojira.yaml: ask the
            # platform to deploy it, as the room does (kobo_api.delivery.push).
            reprovision=delivery.reprovisioner(session_factory, clients.gojira),
        ) if config.bundle_registry else None
    )

    query = session.query(Agent).filter(Agent.status == AgentStatus.READY)
    if args.agent:
        query = query.filter(Agent.name == args.agent)
    removed = skipped = failed = 0
    for agent in query.order_by(Agent.name):
        kind = agent.kind or KIND_COMPOSED
        directory = kind == KIND_DIRECTORY and bool(config.directory_root)
        if not directory and (kind != KIND_COMPOSED or not agent.repo_full_name):
            continue
        try:
            if directory:
                tools = agent_directory(config, agent.name) / "bundle" / "tools"
                retired = sorted(name for name in RETIRED_BASELINE_CHECKS if (tools / name).is_file())
            else:
                # One file read each, no clone: the removal below clones once.
                retired = sorted(
                    name for name in RETIRED_BASELINE_CHECKS
                    if clients.github.read_file(agent.repo_full_name, f"bundle/tools/{name}") is not None
                )
        except Exception as exc:  # noqa: BLE001 -- one agent's trouble never stops the rest
            failed += 1
            print(f"{agent.name}: could not read its files ({exc})")
            continue
        if not retired:
            skipped += 1
            print(f"{agent.name}: nothing retired, skipped")
            continue
        how = "deleted from its folder, then reloaded" if directory else "one commit, one restart"
        if not args.apply:
            removed += 1
            print(f"{agent.name}: would remove {', '.join(retired)} ({how})")
            continue
        try:
            for filename in retired:
                if directory:
                    (tools / filename).unlink()
                else:
                    delete_tooling_file(clients.github, agent.repo_full_name, filename)
            if directory:
                LoadStep().run(SimpleNamespace(config=config, agent=agent))
        except Exception as exc:  # noqa: BLE001
            failed += 1
            print(f"{agent.name}: could not remove {', '.join(retired)} ({exc})")
            continue
        for filename in retired:
            audit.record(
                session, action="tooling.delete", target_kind="tool", target_id=filename,
                agent_name=agent.name, actor=audit.ROOM_ACTOR,
                summary=f"the room removed the retired check {filename} from agent {agent.name}",
            )
        session.commit()
        removed += 1
        print(f"{agent.name}: removed {', '.join(retired)}")
    print(f"\n{'removed from' if args.apply else 'would remove from'} {removed} agent(s); "
          f"skipped {skipped}; failed {failed}")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
