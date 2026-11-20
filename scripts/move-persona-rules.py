"""One-off (issue #524): move each existing agent's persona rules into its guardrails.

New agents are born this way once `guardrails: true` is set. This moves the agents
made before, once, by hand, outside the room's UI. Run it inside a kobo API pod of
a room that already has `guardrails: true` and a runtime that reads guardrails.md:

    kubectl --context <ctx> -n <ns> exec -i deploy/<kobo-api> -- python - < scripts/move-persona-rules.py
    kubectl --context <ctx> -n <ns> exec -i deploy/<kobo-api> -- python - --apply < scripts/move-persona-rules.py
    ... python - --apply --agent ledger < scripts/move-persona-rules.py    (one agent)

Without --apply it changes nothing and prints what it would do. Per agent the room
built (READY, composed, with a repository) whose persona still holds rules:
- the persona's `## Rules` move into bundle/guardrails.md; a rule the list would
  refuse, or one past its 4,000 characters, stays in persona.md, so nothing is lost;
- a persona with no house Rules section gets an empty list, which marks the agent
  as one whose rules live in its guardrails;
- both files land in ONE commit, which restarts the agent once (persona.md is read
  at boot);
- each moved rule is recorded as from the agent's persona, and the room's audit log
  says what moved.
An owner may have started the list before this ran: the persona's rules then go in
after theirs, which stay as they are, so a rule lives in one place either way. An
agent with nothing left in its persona is skipped, so a second run does nothing.
An agent not yet on the room's runtime is left alone and named: an older runtime
ignores guardrails.md, so moving its rules would take them out of its prompt. Move
it first (Harness, Move to the room's runtime), then run this again.
"""

from __future__ import annotations

import argparse
import sys
import tempfile
from pathlib import Path

from kobo_api import audit, delivery
from kobo_api.bundle_publish import RegistryPublisher
from kobo_api.clients import Clients, CommandRunner, GhCLI, installation_token_provider
from kobo_api.config import Config
from kobo_api.db import init_db, make_engine, make_session_factory
from kobo_api.guardrails import (
    GUARDRAILS_PATH,
    RuleIn,
    fit_starter_rules,
    parse_rules,
    persona_with_rules,
    read_guardrails,
    record_starter_rows,
    render_file,
    sync_rows,
)
from kobo_api.kinds import KIND_COMPOSED
from kobo_api.models import Agent, AgentStatus, GuardrailRule
from kobo_api.org import load_org_config
from kobo_api.personas import split_rules
from kobo_api.repo_lock import repo_write_lock
from sqlalchemy import select


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--apply", action="store_true", help="make the commits (default: dry run)")
    parser.add_argument("--agent", help="only this agent")
    args = parser.parse_args(argv)

    local = Config.load(None)
    provider = installation_token_provider(local)
    github = GhCLI(runner=CommandRunner(), token_provider=provider.token if provider else None)
    config, _ = load_org_config(local, github)
    if not config.guardrails:
        print("guardrails is off in this room's config; set it with the runtime pin first")
        return 2
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
    moved = skipped = failed = behind = 0
    for agent in query.order_by(Agent.name):
        if (agent.kind or KIND_COMPOSED) != KIND_COMPOSED or not agent.repo_full_name:
            continue
        repo = agent.repo_full_name
        if (agent.built_runtime_image or "") != config.runtime_image:
            behind += 1
            print(f"{agent.name}: not on the room's runtime yet, left alone; move it first")
            continue
        try:
            current = read_guardrails(clients.github, repo)
            persona = clients.github.read_file(repo, "bundle/persona.md") or ""
        except Exception as exc:  # noqa: BLE001 -- one agent's trouble never stops the rest
            failed += 1
            print(f"{agent.name}: could not read its files ({exc})")
            continue
        body, rules = split_rules(persona)
        if current and not rules:
            skipped += 1
            print(f"{agent.name}: already has guardrails and nothing left in its persona, skipped")
            continue
        # The list as it stands comes first and stays whole; the persona's rules
        # fill what room is left after it.
        existing = parse_rules(current) if current else []
        new_list, _ = fit_starter_rules(existing + rules)
        if new_list[: len(existing)] != existing:
            skipped += 1
            print(f"{agent.name}: its list does not fit the limit as it stands, left alone")
            continue
        moving = new_list[len(existing):]
        left = [rule for rule in rules if rule not in new_list]
        if current and not moving and len(left) == len(rules):
            skipped += 1
            print(f"{agent.name}: none of its persona's {len(rules)} rule(s) fit its list, left alone")
            continue
        new_persona = persona_with_rules(body, left)
        print(
            f"{agent.name}: {len(moving)} rule(s) move to guardrails"
            + (f", after the {len(existing)} already there" if existing else "")
            + (f", {len(left)} stay in the persona" if left else "")
            + ("" if rules else " (no house Rules section: an empty list marks it)")
        )
        if not args.apply:
            continue
        try:
            with repo_write_lock(repo), tempfile.TemporaryDirectory() as tmp:
                workdir = Path(tmp)
                clients.github.clone(repo, workdir)
                (workdir / "bundle" / "persona.md").write_text(new_persona, encoding="utf-8")
                (workdir / GUARDRAILS_PATH).write_text(render_file(new_list), encoding="utf-8")
                delivery.push(
                    clients.github, workdir, "kobo: move the persona's rules into guardrails",
                    restart=True,
                )
        except Exception as exc:  # noqa: BLE001
            failed += 1
            print(f"{agent.name}: the commit failed ({exc}); nothing recorded")
            continue
        if current:
            # The rows mirror the list: each rule already there keeps its own row,
            # named by id so `sync_rows` keeps it, and each moved rule is recorded as
            # from the persona. A line someone wrote in the file by hand has no row
            # and gets none here.
            ids = {
                row.text: row.id
                for row in session.scalars(select(GuardrailRule).where(GuardrailRule.agent_name == agent.name))
            }
            items = [RuleIn(text=rule, id=ids[rule]) for rule in existing if rule in ids]
            items += [RuleIn(text=rule, source_kind="persona", source_ref=agent.persona) for rule in moving]
            sync_rows(session, agent.name, items, agent.created_by or "room")
        else:
            record_starter_rows(session, agent, new_list)
        audit.record(
            session, action="guardrails.move", target_kind="agent", target_id=agent.name,
            agent_name=agent.name, actor=audit.ROOM_ACTOR,
            summary=f"the room moved {len(moving)} rule(s) of agent {agent.name} from its "
            "persona into its guardrails",
            detail={"rules": len(moving), "left_in_persona": len(left), "already_listed": len(existing)},
        )
        session.commit()
        moved += 1
    print(f"\n{'moved' if args.apply else 'would move'} {moved if args.apply else '-'}; "
          f"skipped {skipped}; not on the room's runtime {behind}; failed {failed}")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
