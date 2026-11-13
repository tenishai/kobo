# The agent page
<!-- summary: The header, owners, the build checklist, Retry, the build log, the tabs, and destroy. -->

Every agent has one page. The header names it, shows its status (**Ready** means setup finished; if the room finds it is not actually running, the badge says **Down** instead, with the reason on hover and a banner under the header), and under the name prints the model, a **source** link to its repository, its group, who made it, its persona, and, when your room has dashboards wired, a **Health** link to its dashboard with a **Build log** button beside it. Up to three purpose and skill chips follow; a **+N** chip opens the rest. At the right of the header sit **Edit details** and **Owners**: Owners shows how many people can see or manage the agent and opens the sheet that adds one (see "Access and groups"). The camera on the portrait changes the picture.

Below that sits one sentence about what the agent can do. It reads "Not written yet. It arrives after first contact." until the agent has answered its first hello, then the agent writes it.

## The build checklist

While an agent builds, a checklist sits at the top of its page and clears on its own once every step is green. The steps, in order:

1. **Repository.** The agent's files are written and pushed to a new repository.
2. **Paint the portrait.** The agent gets a face. If this fails, the agent still builds.
3. **Deployment setup.** The repository is connected to the service that deploys it.
4. **Access.** The agent's identity is allowed to call the model you chose. When the room reaches its models through the model gateway, nothing is granted to the agent here: the gateway holds the access, and the agent gets its own key to the gateway in the next step. When the room's permission service is on and GitHub is connected, the agent is also given **Read** on its own repository, so it can see its own files from the start; its owner adds repositories or raises the level on the Access tab.
5. **Deploy wiring.** The agent's identity, storage and colleague pass are put in place.
6. **Image build.** The agent's image is built by its repository's own pipeline.
7. **Deploy.** The agent starts and the room checks it carries what was wired.
8. **Joins the agent directory.** Other agents can now find it.
9. **First contact.** The room says hello. The agent answers and writes its own capability sentence.

A connected agent has a shorter list: no directory, no first contact, and its wiring arrives as a pull request you merge rather than a push.

## When a step fails

A failed step shows a red mark and a **Why it failed** line with the reason the room saw. The banner at the top of the page carries the same text and a **Retry** button. Retry picks up from the failed step. If the image build or deploy failed, Retry also redoes the wiring in front of it, so a fix on the deployment side reaches the agent.

**Build log**, the header button beside **Health**, shows the latest build's result and its log, so you can read the pipeline's own words without leaving the room.

## When a step waits on you

A connected agent's wiring arrives as a pull request on your repository, and someone on your team merges it. While that pull request is open the agent shows **Waiting**, not Failed (its dashboard card reads "Waiting on a pull request" over its age and author): the step it stopped at, usually **Deployment setup**, carries a **What it is waiting for** line with the link, and the banner at the top offers **Resume**. A repository can get two pull requests in a row, the room's wiring and then the deployment setup that follows from it; the agent waits on each, and **Image build** does not start judging a build until the second is merged. You do not have to press it. The room checks once a minute and picks the setup back up on its own when the merge lands. Resume is for when you have just merged and do not want to wait.

## The tabs

- **Chat.** Talk to the agent. See "Talk to it and teach it".
- **Slack.** Put it in Slack. See "Slack".
- **Access.** What this agent can reach: the vendors, what it holds at each, and asking for more. Shown when you own the agent or operate the room. See "Access and groups".
- **Harness.** Everything the agent runs on, and which agents it may talk to. See "The harness".
- **Secrets.** Secrets and variables. See "Secrets and variables".

Who can see and manage the agent is under **Owners** in the header, not on a tab. **Edit details**, on a room-built agent, changes its team, purpose and description, and holds **Destroy this agent** at its bottom.

## Destroy

At the bottom of **Edit details** (for a connected agent, at the bottom of its **Secrets** tab), **Destroy this agent** takes it out of the room for good. You type the agent's exact name to confirm. The running agent, its cloud identity, its stored secrets and its Slack presence are removed. The repository is archived, not deleted, and memories die with the agent. Once the teardown finishes, the name is free again, so a new agent can take it, and nothing the room wrote down about the old one (access grants, thread seats, learnings) carries over. A connected agent's repository is left as it is, because it belongs to your team. Only an operator can destroy.

Destroy is loud about what it could not remove. Each step either finishes or fails with a plain reason, and a failed step leaves the agent as **Destroy failed** with **Destroy again** above the tabs; the steps already done are not repeated. One case to know: when the switchboard still lists the agent after the room asked it to drop the entry, the **Directory listing** step fails rather than finishing quietly, so a destroyed agent never keeps showing up to other agents. Press Destroy again once the switchboard is back. What the room tells its neighbours after the record is gone, the allowed pairs, the colleague rosters and the permission service, is written to the room's log when any of it did not land, and the next change resends it.
