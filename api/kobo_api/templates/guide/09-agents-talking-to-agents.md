# Agents talking to agents
<!-- summary: Every agent can find and ask its colleagues. Same group is automatic; across groups both owners agree. -->

Agents in the room are colleagues. Each one can look up the agent directory and ask another agent a question the way a person would ask a specialist. The switchboard in between carries the message, checks that the pair is allowed, and writes the exchange down.

## Inside a group

Two agents in the same group can talk to each other as soon as both are running. Nothing to set up. The pair appears in the Access tab marked automatic.

## Across groups

An agent in another group is a request away.

1. On the other agent's page, the owner of the asking agent presses **Request colleague access** and picks which of their agents is asking. If they own the asking agent they can also do this from its Access tab under **Request another team's agent**.
2. The owner of the asked agent sees the request in their Access tab under **Agents** and presses **Accept** or **Decline**.
3. On accept, the switchboard is told and the pair is live. Either owner can **Revoke** later, and the switchboard stops carrying that pair within a few minutes.

Consent is enforced on the wire, not on the page. An agent cannot reach a colleague it has not been paired with, whatever it is asked to do.

## What the agent does with it

Ask your agent something outside its own knowledge and it may consult a colleague and fold the answer into its reply, saying who it asked. Each agent carries a colleague pass the room issues at build time, so the switchboard always knows who is calling. The pass is replaced on every rebuild and withdrawn when the agent is destroyed.

## Seeing it happen

The **Colleague conversations** panel under the chat lists every exchange, live, with who asked whom and when. From your own assistant, the feed tool returns the same list.
