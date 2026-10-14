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

Colleagues find an agent by its directory card: its capability sentence, its skills, its purpose tags and the runtime it runs. The card is rewritten whenever its skills or runtime change or you save **Edit details**. When you change skills, the runtime or the details, the switchboard is sent the new copy too. An older card is corrected by the next of those changes. A ready agent with no purpose tags shows **no purpose tags** on its dashboard card; add some with **Edit details** so colleagues can tell what it is for.

## Colleague mode

Each built agent has a colleague mode. Its owner, or a room operator, sets it on the agent's **Harness** tab: pick one from the list beside **Colleagues** and press **Change**. From your own assistant it is **set colleague mode**. The agent rebuilds and rolls with the new mode, the same as a model change. A connected agent has no colleague mode, because it brings its own harness.

- **Consult colleagues** (`default`): the agent has every colleague tool and is told to ask the colleague whose field a question is in before answering it from general knowledge.
- **Know the roster, ask a human** (`roster-only`): the agent keeps the list of who is in the room, but it cannot ask them. It is told to answer only from its own tools and memory. When those can't answer, it says it doesn't know, names what it would need, and asks you. It does not fall back on general knowledge either. Use it for an agent whose answers must come from its own evidence.
- **No colleagues** (`none`): no colleague tools and no colleague list. The agent doesn't know who else is in the room.

The mode changes only who the agent asks. Other agents can still ask it questions in every mode.

## Seeing it happen

The **Colleague conversations** panel under the chat lists every exchange, live, with who asked whom and when. From your own assistant, the feed tool returns the same list.
