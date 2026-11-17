# What the room does today

The long form of the README's feature list: every surface of the room and the rules it
enforces. The in-app guide (Getting started, in the sidebar) is the user-facing version of
the same ground.

- **Create**: pick a persona (or build one: describe the job or paste a document you
  already have, and the room drafts it in the house shape for you to edit), pick a name
  from the ceremony, pick skills, pick a group, click Create. Minutes later the agent answers its first hello.
- **Connect your own**: take on a repository your team already owns and let the room deploy
  it. The repository field searches the repositories the room's GitHub App can reach as you
  type, and the room proposes its setup by pull request.
- **Chat & teach**: talk to any agent you can see. Press *Teach this* on any message and that
  message's own words are in the agent's memory in seconds (the modal names the file it saved), provenance-stamped with your identity. Corrections
  retire stale facts in-session via the agent's own belief-revision pass.
- **Memory**: upload notes or docs for one-shot learning; taught chat notes archive to the
  repo quietly (`[skip ci]`) and fold away in the UI. Memories outlive their source files.
- **Learnings**: each night the agent proposes what it learned from the day's conversations,
  and nothing is kept until its owner, an operator or someone with a grant on the agent presses
  Remember. A candidate nobody can be attributed
  with, or one about granting or revoking access, needs a second click. A nightly pass that
  keeps failing shows **learnings failing** on the agent's card.
- **Colleagues**: agents discover each other through the switchboard directory and consult
  each other mid-conversation (`ask_colleague`), with colleague-sourced facts held at low
  confidence until quorum cross-checking earns them in. A terminal-style feed shows one
  agent's whole conversation history, in order, in full. That is the `default` colleague mode;
  its owner or an operator can set an agent to `roster-only` (it keeps the roster but has no
  `ask_colleague`,
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
  and Access is who may use it. Harness is what runs the agent:
  the model and the runtime it builds on (with one line saying whether that is still the
  room's current one, and an operator's button to move it), its persona, its skills, its
  tooling, its memory, and what it has learned. Settings is the back office: secrets and
  environment, the build log, and the two changes there is no way back from. A connected
  agent's Harness shows what it runs on, its identity and its activity: the room composed none
  of the rest.
- **Activity**: a Harness section on every agent, connected ones included, listing each call the
  tool gateway admitted and each it refused, newest first, with the arguments in brief and
  secrets masked; filter by tool, over a day, 7 days or 30. Read live from the gateway's admin
  plane; without one the section says so.
- **Admin panel**: behind the `admin_panel` org key, for the room's admins only. Services (what
  the room runs on, and Reload org config), Integrations (one credential per vendor in the form
  that vendor uses; AWS is a read-only role the gateway assumes, set up from a Terraform module,
  a CloudFormation template or a trust policy and checked with AWS before it is saved),
  Connections (each agent's `read` or `write` lane to an integration, applied live through the
  gateway's admin plane and proposed as a pull request on its grants file), Roles (four
  built-ins and custom ones), People and Audit.
- **Personas**: the soul an agent is built from. Good persona names pass the glance test:
  a stranger reading the card knows what picking it gets them. The built-ins are the main
  set, one for each general kind of agent.

  A persona belongs to the group that made it. In the create dialog a row of pills sits
  above the list (Built-in, each group that has personas, Everyone last); one is pressed at
  a time, and a search looks across every group and shows each match under its group. That
  row is a filter, not a lock; every persona stays findable by anyone.

  The builder is one form: name, who they are, what they do, rules, and the group it is
  filed under, which starts on the maker's own group. An optional description box drafts
  all four parts in the shape the built-ins are written in. Every persona is held to the
  same floor (at least 300 characters of what they do and 3 rules) and the same tidying
  (one who-they-are line, headings lowered, rules as plain one-liners), whether it was
  typed, drafted or sent by an assistant.

  Edit, Move to... and Archive sit at the bottom of the full persona: the person who made
  it, a member of its group, or an operator may use them (a shared one, only its maker or
  an operator), and a move goes only into a group you belong to. An edit keeps the name
  and reaches agents made from then on. Archive takes one out of every list; nothing is
  destroyed, and an operator can bring it back.
- **MCP**: the room's tools are reachable from MCP clients (claude.ai connectors, Claude Code)
  as the signed-in user, over the room's own OAuth — no service account, no second permission
  system. Every call resolves the same visibility, ownership, and admin as the UI: check who
  you're signed in as, list and inspect the agents you can see, read an agent's conversation
  feed, browse and create personas, create an agent, teach it, chat with it,
  run the colleague-invite handshake, and wire it to Slack. Browse the org's
  skills and tooling marketplaces, stage either onto an agent you own, and
  publish new entries under your own team (publishing is instant and org-wide;
  tool publishing runs the same mechanical law the room enforces, and baseline tool ids are
  reserved for operators). The same surface moves and archives personas, sets an
  agent's persona, model, colleague mode and Slack wake mode, refreshes an attached skill to
  the catalog's current version, re-files an agent under another group
  (operators only), reviews an agent's queued learnings one at a time or in bulk, and runs
  the agent PR broker's repository-access requests and the operator's batch approve or
  revoke. The tools live in `api/kobo_api/mcp/tools/`. Every new agent ships with the baseline
  tooling pack, a leak guard
  and a probe guard, unless created with skip_baseline. Off until the org turns it on.
