# The agent page
<!-- summary: The header, the build checklist, Retry, the build log, the tabs, and destroy. -->

Every agent has one page. The header names it, shows its status, and under the name prints the model, a **source** link to its repository, its group, who made it, its persona, and an **Observability** link when your room has dashboards wired.

Below that sits one sentence about what the agent can do. It reads "Not written yet. It arrives after first contact." until the agent has answered its first hello, then the agent writes it.

## The build checklist

While an agent builds, a checklist sits at the top of its page and clears on its own once every step is green. The steps, in order:

1. **Repository.** The agent's files are written and pushed to a new repository.
2. **Paint the portrait.** The agent gets a face. If this fails, the agent still builds.
3. **Platform onboarding.** The repository is connected to the platform that runs it.
4. **Model access.** The agent's identity is allowed to call the model you chose.
5. **Deploy wiring.** The agent's identity, storage and colleague pass are put in place.
6. **Image build.** The agent's image is built by its repository's own pipeline.
7. **Deploy.** The agent starts and the room checks it carries what was wired.
8. **Joins the agent directory.** Other agents can now find it.
9. **First contact.** The room says hello. The agent answers and writes its own capability sentence.

A connected agent has a shorter list: no directory, no first contact, and its wiring arrives as a pull request you merge rather than a push.

## When a step fails

A failed step shows a red mark and a **Why it failed** line with the reason the room saw. The banner at the top of the page carries the same text and a **Retry** button. Retry picks up from the failed step. If the image build or deploy failed, Retry also redoes the wiring in front of it, so a fix in the platform reaches the agent.

**View build log** in the Settings tab shows the latest build's result and its log, so you can read the pipeline's own words without leaving the room.

## When a step waits on you

A connected agent's wiring arrives as a pull request on your repository, and someone on your team merges it. While that pull request is open the agent shows **Waiting**, not Failed: the **Platform onboarding** step carries a **What it is waiting for** line with the link, and the banner at the top offers **Resume**. You do not have to press it. The room checks once a minute and picks the setup back up on its own when the merge lands. Resume is for when you have just merged and do not want to wait.

## The tabs

- **Chat.** Talk to the agent. See "Talk to it and teach it".
- **Slack.** Put it in Slack. See "Slack".
- **Access.** Who sees it, and which agents may talk to it. Shown when you own the agent or operate the room.
- **Harness.** Everything the agent runs on. See "The harness".
- **Settings.** Secrets and variables, the build log, and the danger zone.

The **...** menu beside the name paints a new face, uploads or downloads the portrait, and on a room-built agent opens **Edit details** for its team, purpose and description.

## Destroy

At the bottom of Settings, **Destroy this agent** takes it out of the room for good. You type the agent's exact name to confirm. The running agent, its platform identity, its stored secrets and its Slack presence are removed. The repository is archived, not deleted, and memories die with the agent. A connected agent's repository is left as it is, because it belongs to your team. Only an operator can destroy.
