# The harness
<!-- summary: What runs the agent: model and runtime, identity, persona, skills, tooling, memory, learnings, activity. -->

The **Harness** tab is everything that runs your agent, in eight sections down its left side. A connected agent shows Model and runtime, Identity and access, and Activity, because it brings its own harness.

## Model and runtime

The model the agent thinks with, and the runtime it runs on. The runtime is one shared image the room maintains for every agent it builds. When the room moves to a newer runtime, this section says **Behind: the room now builds on** the newer version and offers **Update to current**. Pressing it rebuilds the agent on the new runtime. Nothing else about the agent changes: its memory, its Slack app and its learnings stay.

The room owns the `FROM` line in the agent's Dockerfile. If someone edits it by hand, the next teach note, upload, tool or Slack change the room commits puts back the runtime the room has recorded, and that commit's message names both images; skill, colleague-mode and model changes leave the line alone. To run an agent on a different runtime, its owner or an operator sets it (**set runtime image** from your own assistant). The agent's directory card shows the runtime's tag as its version. Pin as `tag@sha256:…` rather than a bare digest: the build is just as exact, and the card still shows a version instead of the start of the digest.

To move an agent onto another model after it is deployed, pick one from the list beside **Model** and press **Change model**. The room writes the choice into the agent's deploy contract and the agent rebuilds and rolls on it. The same change is **set agent model** from your own assistant.

Below them, **Asks other agents** sets whether the agent may ask its colleagues: yes, when it helps (the default); no, but it knows who they are; or no. Pick one and press **Change**; the agent rebuilds with it. See "Agents talking to agents" for what each mode does.

## Identity and access

Shown only while the permission service is off. The service account the agent runs as, the Google service account bound to it, and the room's project, with **Ask for access to another project** to write a request you hand to the SRE team. With the permission service on, access is asked for under **Permissions**, which shows the identity the agent runs as in one line.

## Persona

The persona the agent was built from, its source (Built-in, or the team whose catalog it came from), purpose tags and capability sentence. **Edit details**, in the **More** menu at the right of the agent's name, changes the team, purpose and description. The persona itself can be swapped from your own assistant with the set persona tool. See "Personas" for how personas are made.

## Skills

Skills are abilities from your organization's skills catalog: a way of working, a checklist, a domain the agent should know. Open **Browse the catalog** or **Suggestions** (skills recommended for this agent's persona), select the ones you want and press **Add N skills**. There is no second confirm: they go out together as one update and one restart of the agent, so select everything you want before you press it; one the agent already has says Added. **New guardrail skill** writes one by hand; it ends in the same table.

Each new row shows where its change is: **Pending** (saved, on its way), **Building** (the agent is being rebuilt with it), **Rolling** (the new agent is starting), **Active** (in place; the mark fades after a few seconds). **Failed** stays, with a sentence saying what to do, until you remove the row or change something and save again. While a change is building or rolling the page rests: every control that would change the agent is disabled, the banner at the top says Updating, and reading goes on as usual. The controls come back when the change lands or fails. On a current runtime the agent sees a short list of its skills in its instructions and loads one in full when a question needs it. Remove a skill from its row.

## Tooling

Tooling is code the agent runs around every task. Every agent built by the room starts with the baseline pack: a leak guard that stops secrets leaving in a reply, and a probe guard that stops the agent being led off its job. You can add tools from the catalog or upload your own. A tool is a small Python file that hooks in before a task or after a draft. The room checks it before it lands, and the agent enforces what is in its tooling folder. Nothing runs that is not there.

## Memory

The notes you have taught the agent and the files you have uploaded for it to learn from. Upload a document here and the agent reads it. See "Talk to it and teach it".

## Learnings

What the agent proposes from its own conversations each day, waiting for your decision. The badge on the Harness tab is the count. Nothing is kept until you press Remember.

## Permissions

What the agent may do at each vendor the room has connected, when the room's permission service is on. The agent holds no standing credential: when a task needs one, it borrows an hour of access scoped to exactly the permissions it holds, and the vendor's own audit trail names the agent. **Held** lists what it has, by service and account, with the tier and when it expires (read permissions stand until revoked; write permissions expire and are asked for again). Room admins see **Revoke now** on each row.

**Ask for more** walks the way access is granted. Pick the vendor first: AWS, Google Cloud and Azure are connected one account, project or subscription at a time, so the next step is the account; Datadog, Atlassian and the like are one connection each and open straight on their permissions. Only accounts a room admin has connected on Admin › Integrations are listed; an account appears here when it is connected there. Then find the service you need with the search box, tick it, and press **Confirm request**. Only read access is offered here, and read means configuration and listings, never the data a service holds. A service the connection does not allow is greyed with the reason, not hidden. Requests go to the room's reviewers in Admin › Review; until they decide, the row says "waiting for review".

## Activity

Every call the agent made through the tool gateway in the window you pick, and every one it was refused. A summary comes first: calls, refused, errors, tools used. Rows are grouped by day; the newest day and any day with a refusal or error open on their own, the rest open when clicked, 25 rows at a time, newest first. Click a row to read the whole call: its arguments in full and what happened; click again to fold it. The table scrolls sideways when it must. **Problems only** keeps the refusals and errors. Below it, **Borrowed credentials** lists each hour of access the agent took for a task when the permission service is on.