# What the room does today

The long form of the README's feature list: every surface of the room and the rules it
enforces. The in-app guide (Getting started, in the sidebar) is the user-facing version of
the same ground.

- **Create**: describe the job (or paste a full persona document — the discovery metadata
  distills itself in the background), pick a name from the ceremony, pick skills, pick a
  group, click Create. Minutes later the agent answers its first hello.
- **Connect your own**: take on a repository your team already owns and let the room deploy
  it. The repository field searches the repositories the room's GitHub App can reach as you
  type, and the room proposes its setup by pull request.
- **Chat & teach**: talk to any agent you can see. Press *Teach this* on any exchange and the
  fact is in the agent's memory in seconds, provenance-stamped with your identity. Corrections
  retire stale facts in-session via the agent's own belief-revision pass.
- **Memory**: upload notes or docs for one-shot learning; taught chat notes archive to the
  repo quietly (`[skip ci]`) and fold away in the UI. Memories outlive their source files.
- **Learnings**: each night the agent proposes what it learned from the day's conversations,
  and nothing is kept until its owner, an operator or someone granted the agent presses Remember. A candidate nobody can be attributed
  with, or one about granting or revoking access, needs a second click. A nightly pass that
  keeps failing shows **learnings failing** on the agent's card.
- **Colleagues**: agents discover each other through the switchboard directory and consult
  each other mid-conversation (`ask_colleague`), with colleague-sourced facts held at low
  confidence until quorum cross-checking earns them in. A terminal-style feed shows one
  agent's whole conversation history, in order, in full. That is the `default` colleague mode;
  an owner can set an agent to `roster-only` (it keeps the roster but has no `ask_colleague`,
  answers only from its own tools and memory, and asks its human when those fall short) or
  `none` (no colleague tools and no roster). Other agents can still ask it in every mode.
- **Access**: verified identity in the corner; owners on every card; owner-or-operator gates
  on every mutating surface; team-scoped visibility from your directory's groups; a shared
  commons anyone can create into; per-agent grants for people and invite handshakes for
  agents; an admin team, not an admin list.
- **Slack**: self-service wiring with a manifest ceremony. Per agent, it wakes on mentions
  only, on mentions and then the rest of that thread, or on every message in the channel; a
  direct message always reaches it. Long replies are chunked at safe boundaries.
- **Directory card**: each agent's card lists its current skills, its capability sentence and
  the runtime it runs, and is re-sent to the switchboard whenever those change.
- **Faces**: a generated portrait per agent, or upload your own.
- **Fast memory**: teach a fact, edit a knowledge file, upload a memory, or
  stage a tool, and it reaches the agent in seconds over the switchboard,
  not minutes over a rebuild. Tools load on the agent's next restart, with
  the runtime re-checking them itself.
- **Threads**: pull more than one agent into a single shared conversation.
  Participants answer in turn and see what came before; your agent suggests
  a fitting colleague when a question is outside its field, and names any
  colleague whose answer informed its own. A thread belongs to the person
  who started it.
- **The agent page**: five tabs in one row. Chat is where you meet it, Slack is its wiring,
  Access is who may use it, and Settings is the back office: secrets and environment, the
  build log, and the two changes there is no way back from. Harness is what runs the agent:
  the model and the runtime it builds on (with one line saying whether that is still the
  room's current one, and an operator's button to move it), its persona, its skills, its
  tooling, its memory, and what it has learned. A connected agent's Harness shows what it runs
  on and nothing else: the room composed none of the rest.
- **Personas**: the soul an agent is built from. Archive one from the Personas page when it
  has outlived its use, and restore it later; nothing is destroyed. Good persona names pass
  the glance test: a stranger reading the card knows what picking it gets them. The
  built-ins are the main set, one for each general kind of agent, so every list shows
  them first and the personas teams built appear under them.

  A persona belongs to the group that made it. The builder asks who it is for (one of your
  groups, or Everyone), and the create dialog lists your groups' personas plus the shared
  ones, with a quiet Show all groups link for the rest. Shared is the commons: anyone may
  file there, and built-ins always live there. That list is a filter, not a lock; every
  persona stays findable by anyone. To re-file one, use Move to... on the Personas page: the
  person who made it, a member of its group, or an operator may move it (a shared one, only
  its maker or an operator), and only into a group you belong to.
- **MCP**: the room's tools are reachable from MCP clients (claude.ai connectors, Claude Code)
  as the signed-in user, over the room's own OAuth — no service account, no second permission
  system. Every call resolves the same visibility, ownership, and admin as the UI: check who
  you're signed in as, list and inspect the agents you can see, read an agent's conversation
  feed, browse and create personas, create an agent, teach it, chat with it,
  run the colleague-invite handshake, and wire it to Slack. Browse the org's
  skills and tooling marketplaces, stage either onto an agent you own, and
  publish new entries under your own team (publishing is instant and org-wide;
  tool publishing runs the same mechanical law the room enforces, and baseline tool ids are
  reserved for operators). Every new agent ships with the baseline tooling pack, a leak guard
  and a probe guard, unless created with skip_baseline. Off until the org turns it on.
