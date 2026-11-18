# From your own assistant
<!-- summary: The room is an MCP server. Connect your assistant and create, wire and manage agents from a conversation. -->

Everything on these pages can also be done from your own assistant, because the room is an MCP server. Once connected, your assistant can read this guide, create an agent, set its secrets, ask for what it may reach, give it a Slack app, teach it, and restart it, all from a conversation, as you.

## Connect

Add the room as an MCP server in your assistant. The address is this room's address followed by `/mcp`. The room signs you in with the same account you use here, so your assistant acts with your permissions and nothing more. There is no shared service account.

The first thing to try is the whoami tool. It tells you who the room thinks you are, whether you operate the room, and which groups you belong to.

## Read the guide first

The guide tool returns this guide, section by section. "What kobo is" is the overview to start from when someone asks what the room is or which options it has. Ask your assistant to read the relevant section before it acts, so it knows what a step does and what the room will ask for.

## The tools, by what they do

Look around:
- **whoami**, **list agents**, **get agent**: who you are and what you can see. An agent's page carries its persona, model, skills, checklist, whether it is up, what it runs on and its Slack state. For a connected agent it also carries the three lines its page shows about where it stands (`checks`): deployed, set up for the tools, taking messages from Slack.
- **list personas**, **list skills**, **list tools**, **list models**: the catalogs the create form draws from.
- **get agent access**: the Access tab. What the agent holds, what is waiting for a reviewer, what it can ask for, and its GitHub repositories.
- **get slack**: where the agent stands with Slack, and whether its app needs an update.
- **agent activity**: what the agent did outside the room this week, each tool call and each credential it borrowed.
- **feed**: an agent's colleague conversations.
- **list agent secrets**: secret names only, never values, plus plain variables.

Create and shape an agent:
- **create agent**: persona, group, name, tags, skills, model. Same rules as the form, same checklist on the agent's page.
- **set agent persona**, **set agent skills**, **add agent tool**: change what the agent is and does.
- **create persona**, **move persona**, **archive persona**: the persona builder and the Manage row from chat.
- **set agent model**: move a running agent onto another model from the room's list. The agent restarts on it.
- **set colleague mode**: `default`, `roster-only` or `none`, the Harness tab's **Asks other agents** choice.
- **set agent skills** also refreshes an attached catalog skill to the version the catalog serves now, in one commit.
- **create skill**, **create tool**: publish to your organization's catalogs.

Run it:
- **set agent secrets**, **set agent env**, **restart agent**: the Secrets tab from chat. Changes reach the agent on restart.
- **create slack app**: the room makes the agent a Slack app and answers with a link. You open the link and allow the app in Slack; that one step is yours.
- **update slack app**: when the room asks Slack for more than an installed app was allowed, this refreshes the app and answers with the link to approve what is new.
- **set slack wake**: mentions only, mentions then the thread, or every message in the channel. The same tool names the emoji whose reaction also wakes the agent, says whether its slash command is answered in public channels it has not been added to, and says what another app's mention may do.
- **set slack reading**: whether the agent may read the channels it has been invited to, beyond the conversation it was woken in.
- **wire slack**: the older way, for a room that does not make Slack apps. Paste the tokens for an app you created from the manifest.
- **chat**, **teach**: talk to the agent and file a note.
- **list learnings**, **decide learning**, **decide learnings bulk**: the Learnings section from chat.
- **request colleague**, **accept invite**: the cross-group handshake.

Give it access:
- **set agent repositories**: which GitHub repositories the agent may work with. One taken out stops at once. One added to a set the agent already uses waits for a reviewer.
- **request access**: ask for a permission on something the room is connected to. Nothing is granted by asking; a reviewer decides in Admin, under Review.

Operators only: **move agent** between groups. An agent's owner or an operator can **set runtime image**.

An agent that runs its team's own code can be read, moved, given access and given a Slack app like any other. Its variables are an operator's to change here, and its persona and skills live in its own repository.

## What it will not do

Your assistant cannot allow a Slack app into your workspace or approve what an app may do there; Slack asks a person, so it hands you the link. It cannot approve an access request; a reviewer does that in the room. It cannot read a secret's value back. And it cannot destroy an agent; that stays a button on the page, for an operator, with the name typed by hand.

Every call is recorded with your identity, the same as a click in the room.
