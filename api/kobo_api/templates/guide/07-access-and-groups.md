# Access and groups
<!-- summary: Who sees an agent, granting one person, requesting access to another team's agent, and moving an agent. -->

An agent belongs to one group, chosen when it was created. Everyone in that group sees the agent on the dashboard and can chat with it. Its owner and the room's operators always see it. Nobody else does.

The **shared** group is the exception: an agent filed there is visible to everyone in the company.

## The Access tab

You see this tab on agents you own. It opens with one line stating who can see the agent, then two tables.

**People.** Grant one person outside the group by email. Type their address, the room offers matches from the directory, press **Add**. They can see and chat with the agent. **Remove** takes it back.

**Agents.** Which other agents may talk to this one. Pairs inside the same group are automatic and marked so. Pairs across groups appear here as requests to accept or decline, and once accepted can be revoked. Revoking takes effect within a few minutes. See "Agents talking to agents" for the whole flow.

**Repositories.** Which repositories this agent may open pull requests against. You request them here, one owner/name per line, and a room operator decides.

## When you can see an agent but do not own it

Instead of an Access tab you get **Request colleague access**: pick one of your own agents and ask the owner to let it talk to this one. The owner sees the request on their Access tab.

## Moving an agent

Only an operator can move an agent to a different group. Ask one if an agent was created under the wrong group. Everything else about the agent stays as it is.

## Readers

Some groups are configured as readers. Members see every agent in the room and can change none of them. This is for people whose job is to know what exists.

## Your account menu

The bottom of the left rail is your account menu: press your email to open it. It shows who you are signed in as, a link to this guide, and **Log out** when the room's sign-in has a sign-out path. Room admins also see **Admin** there once the room's admin panel is turned on; nobody else sees the entry, and the page itself refuses anyone who is not an admin.

The admin panel has seven pages. **Overview** is one row per page with the number that matters. **Services** lists what the room runs on and whether each answers; its one button re-reads the org config and asks before it does. **Integrations** is the vendors agents can reach, one credential each, with **Add integration** for the usual ones. AWS connects without a key: the page gives you a Terraform module, a CloudFormation template or a trust policy that creates a read-only role trusting the room's gateway, you paste the role's ARN, and the room checks it with AWS before saving. **Connections** gives an agent a lane to a vendor. **Roles** says what a group may do. **People** shows who runs the room and who is asking for a group. **Audit** is who changed what from the panel.
