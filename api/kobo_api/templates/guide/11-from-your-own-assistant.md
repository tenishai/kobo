# From your own assistant
<!-- summary: The room is an MCP server. Connect your assistant and create, wire and manage agents from a conversation. -->

Everything on these pages can also be done from your own assistant, because the room is an MCP server. Once connected, your assistant can read this guide, create an agent, set its secrets, wire Slack, teach it, and restart it, all from a conversation, as you.

## Connect

Add the room as an MCP server in your assistant. The address is this room's address followed by `/mcp`. The room signs you in with the same account you use here, so your assistant acts with your permissions and nothing more. There is no shared service account.

The first thing to try is the whoami tool. It tells you who the room thinks you are, whether you operate the room, and which groups you belong to.

## Read the guide first

The guide tool returns this guide, section by section. "What kobo is" is the overview to start from when someone asks what the room is or which options it has. Ask your assistant to read the relevant section before it acts, so it knows what a step does and what the room will ask for.

## The tools, by what they do

Look around:
- **whoami**, **list agents**, **get agent**: who you are and what you can see. An agent's card carries its persona, model, skills, checklist and Slack state.
- **list personas**, **list skills**, **list tools**: the catalogs the create form draws from.
- **feed**: an agent's colleague conversations.
- **list agent secrets**: secret names only, never values, plus plain variables.

Create and shape an agent:
- **create agent**: persona, group, name, tags, skills. Same rules as the form, same checklist on the agent's page.
- **set agent persona**, **set agent skills**, **add agent tool**: change what the agent is and does.
- **create persona**, **move persona**, **archive persona**: the persona builder and the Manage row from chat.
- **set agent model**: move a running agent onto another model from the room's list; it rebuilds and rolls.
- **set colleague mode**: `default`, `roster-only` or `none`, the Harness tab's Colleagues choice.
- **set agent skills** also refreshes an attached catalog skill to the version the catalog serves now, in one commit.
- **create skill**, **create tool**: publish to your organization's catalogs.

Run it:
- **set agent secrets**, **set agent env**, **restart agent**: the Settings tab from chat. Changes reach the agent on restart.
- **wire slack**: paste the tokens for a Slack app you already created from the manifest.
- **set slack wake**: mentions only, mentions then the thread, or every message in the channel.
- **chat**, **teach**: talk to the agent and file a note.
- **list learnings**, **decide learning**, **decide learnings bulk**: the Learnings section from chat.
- **request colleague**, **accept invite**: the cross-group handshake.
- **request repo access**, **list repo access**: pull request permissions, decided by an operator.

Operators only: **move agent** between groups and **decide repo access**. An agent's owner or an operator can **set runtime image**.

## What it will not do

Your assistant cannot create a Slack app for you, because that step happens on Slack's own site. It cannot read a secret's value back. And it cannot destroy an agent; that stays a button on the page, for an operator, with the name typed by hand.

Every call is recorded with your identity, the same as a click in the room.
