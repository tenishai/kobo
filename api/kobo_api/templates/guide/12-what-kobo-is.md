# What kobo is
<!-- summary: For an assistant explaining kobo to its person: what the room is, what it sits on, what it can do for you, and every choice you make, with why you would make it. -->

Kobo is the room where a person builds, deploys and governs AI agents without touching the machinery. You pick a persona, a name and a group, press **Create agent**, and the room writes the agent's files, deploys it, and keeps it running; afterwards you chat with it, teach it, put it in Slack, and decide who and what may reach it.

This section is the overview. Each part below points at the section that has the detail.

## Where it fits

Kobo is one piece of a set of sibling projects. Each owns one job, and the room ties them together.

- **daedalus** (required). Every agent the room builds is a daedalus bundle (persona, skills, guardrails, checks, wiring) running on one shared daedalus runtime image the room names in its config.
- **gojira** (required for deployed agents). The deploy service. The room hands it each new agent's repository; it sets up the agent's build pipeline and runs the agent on the cluster. A room set to deliver agents into a folder instead (see below) does not use it.
- **techne** (comes with every agent). Each agent's own memory store, inside the runtime. Teach this, uploads and approved learnings land there. The room never calls techne itself; it writes notes the agent's store takes in.
- **cabal** (optional, but most of the room needs it). The switchboard. With it, chat and teaching reach the agent in seconds, the nightly learnings pass runs, agents can ask each other, the agent directory exists, and the room can mint its own token for the tool gateway. Without it, the room talks to agents directly and agents have no colleagues.
- **liminal** (optional). The tool gateway agents call for real tools: cloud accounts, vendors, other MCP servers. An agent with no grant there sees no tools. With the gateway's admin plane wired too, grants apply at once and the Activity view can read what each agent did.
- **sero** (optional). The secrets service. With it, secrets you add to an agent are stored there and delivered to the agent. Without it, you can still set plain variables, but the room cannot store new secrets for you.
- **noto** (optional). Dashboards. When wired, an agent's page and its card carry a **Health** link.

## What it can do for you

- **Build an agent from a persona.** Press **New agent**, then **Build an agent**. See "Create your first agent".
- **Deploy an agent your team already wrote.** **Connect a repository you already have**: the room scans it and sets it up through pull requests it merges itself. It gets the same storage, identity and gateway access as an agent the room builds. See "Before you start" and "Running your own code".
- **Teach it facts in seconds.** Press **Teach this** on any chat message, or upload documents under **Harness, Memory**. See "Talk to it and teach it".
- **Review what it learned.** Each night it proposes one-line learnings; nothing is kept until you press **Remember**.
- **Put it in Slack.** The room writes the Slack app manifest; you paste back three values. See "Slack".
- **Let agents consult each other.** Same group is automatic; across groups both owners agree. See "Agents talking to agents".
- **Give it access to cloud accounts and vendors.** An admin connects an integration once; each agent gets its own lane. See "Access and groups".
- **See what it did.** The Activity section lists every tool call it made through the gateway, and every refusal. See "The harness".
- **Do all of this from your own assistant.** The room is an MCP server. See "From your own assistant".

## The choices you make, and why

### Creating an agent

- **Build or connect.** Build when you want the room to own the agent and its repository; you get chat, Slack, teaching and the whole Harness. Connect when your team already has an agent in its own repository; the room deploys it, and connecting hands its deploy files to the room. It can use a Slack app the room makes, and its page says whether it is running, set up for the tools and taking Slack's messages; chat, teaching and most of the Harness do not apply.
- **Persona.** A built-in persona (a helpful generalist, a billing analyst, an identity admin, a code reviewer, a cloud operator) gets you started at once. Your group's own personas carry your team's way of working. **Build your own persona** when nothing fits: paste one you have, or describe the job and edit the draft. See "Personas".
- **Group.** Decides who sees the agent and which agents it talks to without asking. Pick your own team. Pick **shared** only when the whole company should see it; a shared agent is not paired with anyone automatically.
- **Model tier.** **Best** when the work needs the hardest thinking and cost matters less; **Balanced** for everyday work; **Fast** when the job is simple and cost or speed matters most. You can change it later on the Harness tab; the agent rebuilds.

From your own assistant, create agent also takes **skills** and **tags** up front, and `archetype` set to `team_guide` builds a team's informational guide: an agent that interviews people, answers questions, and never acts.

### How it talks to other agents (colleague mode)

Set on the Harness tab beside **Asks other agents**, or **set colleague mode** from your assistant. The agent rebuilds.

- **Yes, when it helps** (`default`). Why: the agent asks the colleague whose field a question is in before it answers from general knowledge, so answers draw on the whole room.
- **No, but knows who they are** (`roster-only`). Why: answers must come from the agent's own tools and memory. It knows who else is in the room but cannot ask them; when its own evidence runs out it says so and asks you. It does not fall back on general knowledge.
- **No** (`none`). Why: a self-contained agent that should not know or mention anyone else.

Other agents can still ask yours in every mode. A connected agent has no colleague mode.

### How it wakes in Slack

- **Mentions only.** Why: a busy channel where the agent should speak only when called. Every question needs the mention.
- **Mentions, then the thread.** Why: the usual bot behaviour. Mention it once at the top of a thread and it follows the replies. New agents start here.
- **Every message in the channel.** Why: the channel is the agent's job, such as an intake or support channel.

Beside any of the three, **Also wakes on these emoji** names emoji whose reaction wakes it about the message they were put on. Why: a team that already triages with emoji, where a `ticket` on a message means "file this" and nobody wants to write the agent a sentence. Empty by default.

Direct messages always reach it. Moving to or from **Mentions only** means pasting the manifest again. See "Slack".

Another app's mention is answered from what the agent knows until its owner lets it use its tools, and an agent reads the channels it is in only once its owner ticks that box. Once in Slack, an agent the room built can also offer a choice as buttons, be asked with a slash command that is its own name, post in a public channel it was not invited to, pin a message and bookmark a link. An app made before the room could do these is brought up to date with **Update in Slack**.

One of them has a setting: **Answer its slash command in any public channel**. Off, which is how every agent starts, the command works only where the agent has been added. Why you would turn it on: the whole workspace should be able to call this agent from wherever they are, and you are content for its answers to appear in channels nobody invited it to.

### Teaching

- **Teach this** on a chat message saves that message's own words. Use it when either side said something worth keeping.
- **Teach something** in the Learnings section is a one-line note when you are not in a chat.
- **Upload** under **Harness, Memory** for documents the agent should read.

With the switchboard wired and the agent reachable, a note is learned at once and filed to the repository in the background. Otherwise it is saved and arrives on the agent's next start. Either way it survives rebuilds.

### Secrets and variables

- **Secret** for anything nobody should read again: API keys, tokens, passwords. Sent once, shown only as a lock that says whether it reached the agent. Needs the secrets service.
- **Variable** for plain settings such as a region or a flag. Stays readable.

Either reaches a running agent when you press **Restart agent**. See "Secrets and variables".

### Access

- **Grant one person** outside the group from **Owners** in the agent's header. Why: a colleague on another team needs to use this one agent, and nothing else.
- **Accept a colleague request** from another group's agent. Why: two teams' agents should work together. The switchboard enforces the pair; revoking takes effect within minutes.
- **Pick repositories and a GitHub level** on the Access tab. Why: the agent should read, review or open pull requests there. A reviewer decides.
- **Ask for access to another project** on the Harness tab. Why: the agent needs a cloud project it was not given. The room writes the request for your SRE team; ask for the narrowest role.

People with a role: **operators** run the room, see everything, move agents between groups and destroy agents; **readers** see every agent and change none. An operator for the whole room is also an admin, and sees the admin panel when the room has it turned on. See "Access and groups".

### Cloud and vendor access (admins)

The admin panel, when the room has it turned on, holds the choices an agent's owner cannot make alone.

- **Integrations.** One credential per vendor, in the form that vendor uses: AWS is a role the gateway assumes, so no key is stored; Google Cloud is workload identity; Azure is a federated app registration; Datadog and Wiz are key pairs; Atlassian is a service account's API key, or an account's email with its API token; Fastly is an API token; Okta is an API token with your organization's domain. For AWS the page gives you the role three ways (a Terraform module, a CloudFormation template, or the trust policy alone), and the room checks the role with AWS before it saves.
- **Connections.** Each agent's lane to an integration, `read` or `write`, never wider than the integration allows. Why: every agent gets only the access its job needs.
- **Roles.** Four built-ins (Admin, Operator, Member, Reader) plus your own. The room records and shows them but does not enforce them yet, so giving a group a role changes nothing it can do today.

### The harness

Model, runtime, colleague mode, identity, persona, skills, guardrails, memory, learnings and activity. Press **Update to current** when the room moves to a newer runtime. Add or remove **skills** from the catalog; refresh one to the catalog's current version from your assistant. **Guardrails** holds the rules the agent follows on every task, one per line, and a rule can name the people it is about. Below them, **Always on** lists the checks the room runs, starting with a leak guard and a probe guard; skip those only with `skip_baseline` from your assistant, and only when you know why. See "The harness".

### Activity

Every call the agent made through the tool gateway, and every refusal, newest first, over the last day, 7 days (the default) or 30 days, filtered by tool if you like. Why: to see what an agent really did with a vendor, and when it was refused, what it was missing. It needs the gateway's admin plane; without one the section says so instead of showing an empty list.

## How the room itself is run

The room is a web app (FastAPI and Svelte) one team operates for the whole company. People open it in a browser or reach it from their assistant; nobody installs it to use it. The operator's choices are in the config file and the org config, described in the repository's README and `docs/operating.md`. The ones that change what people see:

- **Where agents go.** Deployed (the default) builds a repository and runs the agent on the cluster. Directory composes each agent into a folder on disk for a set of local daedalus processes to load: no repository, no image.
- **Where catalogs live.** Cloud storage buckets (the default), or folders on disk for a room with no object store.
- **What is turned on.** The switchboard, the tool gateway and its admin plane, the secrets service, Slack, the MCP connector, the admin panel and dashboards are each off until configured.

One install note for an operator: kobo's API pins `mcp>=1.15,<2`, and `tenish-daedalus[claude-code]` needs `mcp>=2.2`. Run them in separate Python environments; the room and its agents never share one anyway.

## Where to go next

- "Quick start" for the four steps to a running agent.
- "From your own assistant" for connecting and the tool list.
- "Glossary" for the words the room uses.
- In the repository: `README.md`, `docs/what-the-room-does.md` for every rule the room enforces, `docs/operating.md` for running a room.
