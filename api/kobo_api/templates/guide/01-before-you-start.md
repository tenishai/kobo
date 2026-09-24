# Before you start
<!-- summary: What the room is, what you need to use it, and what you get when you create an agent. -->

Kobo is an assembly room for agents. You pick who an agent is, give it a name and a group, and the room builds it, deploys it, and keeps it running. You never touch a cluster, a container, or a pipeline. The room does that part.

## What you need

- A sign-in. The room uses your company sign-in, so if you can open this page you are in.
- A group. Agents live under groups from your company directory. You create agents under the groups you belong to. If yours is missing from the list, the create form has a "My group is not listed" option that sends a request to the room's operators.
- About ten minutes for the first build. Later changes are faster.

## What you get

When you create an agent the room:

- creates a repository for it under your organization, holding its persona, skills and tooling as plain files;
- gives it its own identity, model access and a place to run;
- deploys it and checks that it answers;
- lists it in the agent directory so other agents can find it.

You get a page for the agent with a chat, a Slack setup, an access list, and a Harness tab that shows everything the agent runs on. Anyone in your group sees it there too.

## Two kinds of agent

- **Built by the room.** The usual way in. You choose a persona and the room composes the rest on its one shared runtime. This guide is mostly about these.
- **Connected.** Your team already has an agent in its own repository. The room deploys and watches it, gives it identity and model access, and never pushes to your branches. Chat, Slack and most of the Harness do not apply, because the code is yours.

## Who can do what

- **You** own the agents you create. You can change them, wire them into Slack, grant access, and restart them.
- **Your group** sees your agents and can chat with them.
- **Operators** run the room. They see everything, decide repository access requests, move agents between groups, and are the only people who can destroy an agent.

If you would rather work from your own assistant than from these pages, the room is also an MCP server. See "From your own assistant".
