# Access and groups
<!-- summary: Who sees an agent, granting one person, requesting access to another team's agent, and moving an agent. -->

An agent belongs to one group, chosen when it was created. Everyone in that group sees the agent on the dashboard and can chat with it. Its owner and the room's operators always see it. Nobody else does.

The **shared** group is the exception: an agent filed there is visible to everyone in the company.

## The Access tab

You see this tab on agents you own or manage. It opens with one line stating who can see the agent, then two sections.

**People.** Who can see and manage this agent besides its owner and the room's operators. Press **Add someone**, type an address (the room offers matches from the directory) and pick a role. **Can see** opens the agent and chats with it. **Can manage** also edits its skills, tooling, Slack, secrets and permission requests; it cannot destroy the agent, and it cannot add or remove people here. Adding someone again with the other role changes their role. **Remove** takes the access back.

**Other agents.** Agents this one may ask for help, across teams. **Request another team's agent** sends the request. When the room's permission service is on, that request waits in **Admin, Review** like any other permission and the row reads **Waiting for review** until a room admin approves it; otherwise it goes to the other agent's owner, and the requests waiting on your side appear here to accept or decline. Pairs inside the same group are automatic and marked so; accepted pairs can be revoked, and revoking takes effect within a few minutes. Every pair can join shared threads. See "Agents talking to agents" for the whole flow.

**Repositories.** Which repositories this agent may open pull requests against. You request them here, one owner/name per line, and a room operator decides.

## When you can see an agent but do not own it

Instead of an Access tab you get **Request colleague access**: pick one of your own agents and ask the owner to let it talk to this one. The owner sees the request on their Access tab.

## Moving an agent

Only an operator can move an agent to a different group. Ask one if an agent was created under the wrong group. Everything else about the agent stays as it is.

## Readers

Some groups are configured as readers. Members see every agent in the room and can change none of them. This is for people whose job is to know what exists.

## Your account menu

The bottom of the left rail is your account menu: press your email to open it. It shows who you are signed in as, a link to this guide, and **Log out** when the room's sign-in has a sign-out path. Room admins also see **Admin** there once the room's admin panel is turned on; nobody else sees the entry, and the page itself refuses anyone who is not an admin.

The admin panel has eight pages. **Overview** is one row per page with the number that matters. **Services** lists what the room runs on and whether each answers; its one button re-reads the org config and asks before it does. **Integrations** is one table: the vendors agents can reach, one connection each, and two rows that are always there for the services the room itself uses, **Slack** (the config tokens it creates agents' Slack apps with) and **GitHub** (the app that opens their pull requests). On a fresh room both read **Not set up**, and every agent's Slack tab and Repositories card is a greyed box asking a kobo admin to add the integration. An admin pastes each once with **Set up**; agents can use it at once, no restart. A value the room's deployment config carries shows as **Set by deployment**, **Replace** swaps what was pasted and **Remove** (which asks first) takes it back. **Add integration** adds a vendor. AWS connects as an organization. Pick how you create roles (Terraform, CloudFormation or by hand) and the page shows only that method: one hub role in the organization's management account, trusting the room's identity and allowed only to assume each member account's role, list the accounts and read the bill; and one spoke role for every member account, from one template with the organization's one external id, so a StackSet puts it everywhere at once. Paste the hub's ARN; the room checks it with AWS, saves, and then checks every account through the hub. Accounts that carry the spoke role connect themselves with the read permissions agents may ask for; the rest show under the organization as **No role yet**. **Check accounts** on the organization runs that check again after you deploy the role to more accounts. A second organization is a second row, never an assume across organizations. **Connections** gives an agent a lane to a vendor; it is shown only while the permission service is off, since agents then hold permissions instead of lanes. **Review** is where permission requests wait when the permission service is on: a **Borrowed now** table of the credentials out at this moment, then the requests; tick rows, **Approve**, or **Deny** with a reason; **Sync agents** tells the service which agents exist. A request to ask another team's agent is in the same list, with the asked agent as the account and **ask** as its one permission; approving it pairs the two agents on the switchboard at once. **Roles** records what a group should be allowed to do; the room shows roles but does not enforce them yet. **People** shows who runs the room and who is asking for a group. **Audit** is who changed what from the panel, one sentence per change, newest first, with whether it has landed; a filter narrows it to one kind of change.
