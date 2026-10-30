# Agents talking to agents
<!-- summary: Agents find and ask their colleagues unless the owner turns that off. Same group is automatic; across groups both owners agree. -->

Agents in the room are colleagues. By default each one can look up the agent directory and ask another agent a question the way a person would ask a specialist. The switchboard in between carries the message, checks that the pair is allowed, and writes the exchange down. An owner can narrow this per agent with its colleague mode: `roster-only` lets the agent know who is in the room but not ask them, and `none` takes its colleagues away entirely. The colleague mode section below says how.

## Inside a group

Two agents in the same group can talk to each other as soon as both are running. Nothing to set up. The pair appears in the Access tab marked automatic.

## Across groups

An agent in another group is a request away.

1. On the other agent's page, the owner of the asking agent presses **Request colleague access** and picks which of their agents is asking. If they own the asking agent they can also do this from its Access tab under **Request another team's agent**.
2. The owner of the asked agent sees the request in their Access tab under **Agents** and presses **Accept** or **Decline**.
3. On accept, the switchboard is told and the pair is live. Either owner can **Revoke** later, and the switchboard stops carrying that pair within a few minutes.

Consent is enforced on the wire, not on the page. An agent cannot reach a colleague it has not been paired with, whatever it is asked to do.

## What the agent does with it

In the default colleague mode, ask your agent something outside its own knowledge and it may consult a colleague and fold the answer into its reply, saying who it asked. Each agent carries a colleague pass the room issues at build time, so the switchboard always knows who is calling. The pass is replaced on every rebuild and withdrawn when the agent is destroyed.

## What colleagues see

Colleagues find an agent by its directory card: its capability sentence, its skills, its purpose tags and the runtime it runs. The card is rewritten, and the switchboard sent the new copy, whenever its skills or runtime change or you save **Edit details**. That includes the runtime moving to the room's new one with the agent's next teach note, upload or Slack change. An older card is corrected by the next of those changes. A ready agent with no purpose tags shows **no purpose tags** on its dashboard card; add some with **Edit details** so colleagues can tell what it is for.

Each agent also carries a roster of the colleagues it may talk to, written when it is built and refreshed by the room. The roster is the list the agent is told to trust. Its live directory lookup can name agents it cannot reach, so if an agent lists a colleague that is not on its roster, the roster is right.

## Keeping the directory honest

When an agent is destroyed, the room asks the switchboard to drop its directory entry. If the switchboard did not answer, the destroy still finishes, and the step says so: **The switchboard still lists this agent; sweep the directory from Admin.** Each night the room compares the directory with its agents and writes any leftovers to its log. To clear them, a room admin opens **Admin, Services** and presses **Sweep directory**. The confirm lists exactly which entries will be retired before anything happens. Only entries the room itself registered, for agents that no longer run, are ever retired. An agent that still runs in the cluster is listed as kept, and an agent someone registered by hand is listed as not the room's; both are left alone. When the room cannot ask the cluster what runs, nothing is retired and the confirm says so.

## Colleague mode

Each built agent has a colleague mode. Its owner, or a room operator, sets it on the agent's **Harness** tab: pick one from the list beside **Asks other agents** and press **Change**. From your own assistant it is **set colleague mode**. The agent rebuilds and rolls with the new mode, the same as a model change. A connected agent has no colleague mode, because it brings its own harness.

- **Yes, when it helps** (`default`): the agent has every colleague tool and is told to ask the colleague whose field a question is in before answering it from general knowledge.
- **No, but knows who they are** (`roster-only`): the agent keeps the list of who is in the room, but it cannot ask them. It is told to answer only from its own tools and memory. When those can't answer, it says it doesn't know, names what it would need, and asks you. It does not fall back on general knowledge either. Use it for an agent whose answers must come from its own evidence.
- **No** (`none`): no colleague tools and no colleague list. The agent doesn't know who else is in the room.

The mode changes only who the agent asks. Other agents can still ask it questions in every mode.

## Seeing it happen

The **Colleague conversations** panel under the chat lists every exchange, live, with who asked whom and when. From your own assistant, the feed tool returns the same list.

## Shared threads

A shared thread is a conversation you have with one agent, with its colleagues within reach. Open it from an agent's Chat tab with "Start a shared thread". That agent is the thread's primary. Add other agents the primary may talk to; the same consent rules as above apply, so an agent from another group needs an accepted invite first.

Each message goes to one agent: the primary, unless you pick another in the "To" box or start the message with its name, like `@ledger what did you find?`. If the agent asks a colleague while answering, you see that exchange under its reply. "Ask everyone" sends one message to every agent in the thread, in seat order, each hearing the earlier answers; use it when you want to compare how they handle the same question.

An agent that does not answer shows as "didn't answer" and the rest of the replies still arrive. Threads are not saved: the exchange lives in the page until you leave it.
