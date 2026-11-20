# The agent page
<!-- summary: The header, owners, the build checklist, Retry, the build log, the tabs, and destroy. -->

Every agent has one page. The header names it, shows its status (**Ready** means setup finished; if the room finds it is not actually running, the badge says **Down** instead, with the reason on hover and a banner under the header), and under the name prints the model, a **source** link to its repository, its group, who made it, its persona, and, when your room has dashboards wired, a **Health** link to its dashboard with a **Build log** button beside it. Up to three purpose and skill chips follow; a **+N** chip opens the rest. At the right of the header sit **Edit details** and **Owners**: Owners shows how many people can see or manage the agent and opens the sheet that adds one (see "Access and groups"). The camera on the portrait changes the picture.

Below that sits one sentence about what the agent can do. It reads "Not written yet. It arrives after first contact." until the agent has answered its first hello, then the agent writes it.

## The build checklist

While an agent builds, a checklist sits at the top of its page and clears on its own once every step is green. The steps, in order:

1. **Repository.** The agent's files are written and pushed to a new repository.
2. **Paint the portrait.** The agent gets a face. A name an image model won't draw, like a famous character's, is tried again without the name, and when no face can be painted the agent gets a placeholder of its initials, so every agent has one. **New face** on the portrait tries again.
3. **Deployment setup.** The repository is connected to the service that deploys it.
4. **Access.** The agent's identity is allowed to call the model you chose. When the room reaches its models through the model gateway, nothing is granted to the agent here: the gateway holds the access, and the agent gets its own key to the gateway in the next step. When the room's permission service is on and GitHub is connected, the agent is also given **Read** on its own repository, so it can see its own files from the start; its owner adds repositories or raises the level on the Access tab. The room allows that one read itself. If the step says the read **waits for a reviewer**, a reviewer approves it under **Admin, Review**.
5. **Deploy wiring.** The agent's identity, storage and colleague pass are put in place.
6. **Image build.** The agent's image is built by its repository's own pipeline.
7. **Deploy.** The agent starts and the room checks it carries what was wired.
8. **Joins the agent directory.** Other agents can now find it.
9. **First contact.** The room says hello. The agent answers and writes its own capability sentence.

A connected agent has a shorter list: no directory, no first contact, and its wiring arrives as a pull request the room merges rather than a push.

## When a step fails

A failed step shows a red mark and a **Why it failed** line with the reason the room saw. The banner at the top of the page carries the same text and a **Retry** button. Retry picks up from the failed step. If the image build or deploy failed, Retry also redoes the wiring in front of it, so a fix on the deployment side reaches the agent.

**Build log**, the header button beside **Health**, shows the latest build's result and its log, so you can read the pipeline's own words without leaving the room. A connected agent is built in its team's own repository, and the room shows that log to room operators only; its owner gets a link to the repository's own builds instead.

## When a step waits on you

A connected agent's setup arrives as pull requests on its repository, and the room and the deploy service merge their own, so most agents never wait. The exception is a repository whose own rules stop them, a protected branch that requires a review for one. Then the pull request stays open for someone who can merge there, and the agent shows **Waiting**, not Failed (its dashboard card reads "Waiting on a pull request" over its age and author): the step it stopped at, usually **Deployment setup**, carries a **What it is waiting for** line with the link, and the banner at the top offers **Resume**. A repository can get two pull requests in a row, the room's wiring and then the deployment setup that follows from it; **Image build** does not start judging a build until the second is merged. You do not have to press Resume. The room checks once a minute and picks the setup back up on its own when the merge lands. Resume is for when you have just merged and do not want to wait.

## The tabs

- **Chat.** Talk to the agent. See "Talk to it and teach it". A connected agent has the tab too: it holds the box to type in once the agent's code answers, and one line on what is missing until then. See "Running your own code".
- **Slack.** Put it in Slack. See "Slack".
- **Access.** What this agent can reach: the vendors, what it holds at each, and asking for more. Shown when you own the agent or operate the room. See "Access and groups".
- **Harness.** Everything the agent runs on, and which agents it may talk to. See "The harness".
- **Secrets.** Secrets and variables. See "Secrets and variables".

Who can see and manage the agent is under **Owners** in the header, not on a tab. **Edit details** changes a room-built agent's team, purpose and description; its persona is changed with **Edit persona**, in the Harness tab's Persona section. On a connected agent it holds one line on what the agent does, which its owner writes and the page shows under its name. For both it holds **Destroy this agent** at its bottom.

## Destroy

At the bottom of **Edit details**, **Destroy this agent** takes it out of the room for good. You type the agent's exact name to confirm. The running agent, its cloud identity, its stored secrets and its Slack presence are removed. So is everything it held in the permission service: its list of repositories, the access it was granted and anything it was still asking for. The repository is archived, not deleted, and memories die with the agent. Once the teardown finishes, the name is free again within a minute or so, so a new agent can take it; asked for sooner, **New agent** and **Connect** say the name is still being cleared away. Nothing the room wrote down about the old one (access grants, thread seats, learnings) carries over. A connected agent's repository is left as it is, because it belongs to your team. Only an operator can destroy.

Destroy is loud about what it could not remove. Each step either finishes or fails with a plain reason, and a failed step leaves the agent as **Destroy failed** with **Destroy again** above the tabs; the steps already done are not repeated. One case to know: when the switchboard still lists the agent after the room asked it to drop the entry, the **Directory listing** step fails rather than finishing quietly, so a destroyed agent never keeps showing up to other agents. Press Destroy again once the switchboard is back. What the room tells its neighbours after the record is gone, the allowed pairs, the colleague rosters and the permission service, is written to the room's log when any of it did not land, and the next change resends it.
