# Glossary
<!-- summary: The words the room uses, in one place. -->

- **Agent.** A running assistant with a persona, a model, skills and tooling, built and kept by the room.
- **Built by the room.** An agent the room composed from a persona. It runs on the room's shared runtime.
- **Connected.** An agent from a repository your team already owns. The room deploys and watches it and never pushes to your branches.
- **Group.** A team from your company directory. An agent belongs to one, and its members see it.
- **Shared.** The group everyone is in. Agents and personas filed there are visible to the whole company.
- **Operator.** A person who runs the room. Sees everything, moves agents, decides repository access, destroys agents.
- **Reader.** A member of a group configured to see every agent and change none.
- **Persona.** Who an agent is: one line of identity, what it does, and its rules. An agent is built from one.
- **Built-in persona.** A persona the room ships for a common kind of agent. Shared with everyone, never moved.
- **Harness.** Everything that runs an agent: model and runtime, persona, skills, tooling, memory, learnings.
- **Runtime.** The one shared image every room-built agent runs on. The room updates it; you press Update to current.
- **Skill.** An ability from your organization's catalog that an agent picks up on its next build.
- **Tooling.** Code that runs around every task. The baseline pack is a leak guard and a probe guard.
- **Memory.** Notes taught to an agent and files uploaded for it. Stays with the agent.
- **Teach this.** The link on any chat message that files it into the agent's memory.
- **Learnings.** One-line statements an agent proposes daily from its conversations, kept only when you press Remember.
- **Secret.** A sensitive value stored in the secrets platform and delivered to the agent as an environment variable. Shown only as a lock.
- **Variable.** A plain setting written into the agent's files.
- **Colleague.** Another agent this one may ask. Same group is automatic; across groups both owners agree.
- **Colleague pass.** The credential the room issues an agent so the switchboard knows who is calling.
- **Switchboard.** The service that carries agent-to-agent messages, checks the pair is allowed, and records the exchange.
- **Agent directory.** The list of agents the switchboard knows, joined near the end of the build.
- **Checklist.** The build steps at the top of a new agent's page, gone once every step is green.
- **Manifest.** The Slack app definition the room writes for an agent, pasted into Slack once.
- **MCP.** The protocol your own assistant uses to work in the room as you.
