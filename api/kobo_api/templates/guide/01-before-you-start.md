# Before you start
<!-- summary: What the room is, what you need to use it, and what you get when you create an agent. -->

Kobo is an assembly room for agents. You pick who an agent is, give it a name and a group, and the room builds it, deploys it, and keeps it running. You never touch a cluster, a container, or a pipeline. The room does that part.

## What you need

- A sign-in. The room uses your company sign-in, so if you can open this page you are in.
- A group. Agents live under groups from your company directory. You create agents under the groups you belong to. If yours is missing from the list, the create form has a "My group is not listed" option that sends a request to the room's operators.
- About ten minutes for the first build. Later changes are faster.

## What you get

When you create an agent the room:

- creates a repository for it under your organization, holding its persona, skills, guardrails and checks as plain files;
- gives it its own identity, model access and a place to run;
- deploys it and checks that it answers;
- lists it in the agent directory so other agents can find it.

You get a page for the agent with a chat, a Slack setup, an Access tab for what it may reach, a Harness tab that shows everything the agent runs on, and a Secrets tab. Anyone in your group sees it there too.

## Two kinds of agent

- **Built by the room.** The usual way in. You choose a persona and the room composes the rest on its one shared runtime. This guide is mostly about these.
- **Connected.** Your team already has an agent in its own repository. The room deploys and watches it, gives it identity, model access, storage and a way to reach what it is granted (see "Running your own code"). Connecting hands the repository's deploy files to the room: it opens and merges its own pull requests for them, and your code stays yours. Chat and Slack work once its code takes what the room hands it, and most of the Harness does not apply, because the code is yours. A connected repository keeps its own name: if it already deploys, the room reads the name from its deployment and uses that, and its setup arrives as a pull request the room merges for you. Its service account does not stay its own: every agent in the room runs as the room's account, `agent-<name>`, so that what it reaches is granted and recorded on its Access tab. If the deployment ran as something else, the scan says so before you connect, the room's pull request makes the move, and nothing granted to the old account follows.

## Who can do what

- **You** own the agents you create. You can change them, wire them into Slack, grant access, and restart them.
- **Your group** sees your agents and can chat with them.
- **Operators** run the room. They see everything, decide repository access requests, move agents between groups, and are the only people who can destroy an agent.

If you would rather work from your own assistant than from these pages, the room is also an MCP server. See "From your own assistant".
