# The harness
<!-- summary: What runs the agent: model and runtime, agent to agent, identity, persona, skills, guardrails, memory, learnings, activity. -->

The **Harness** tab is everything that runs your agent, in eight sections down its left side. A connected agent shows Model and runtime, Identity and access, and Activity, because it brings its own harness. What the agent may reach at each vendor is on the **Access** tab (see "Access and groups").

## Model and runtime

The model the agent thinks with, and the runtime it runs on. The runtime is one shared image the room maintains for every agent it builds. The line under the runtime says what this agent is built on right now, read from the agent's own files, against what the room builds on today. When the room has moved on, it reads **Runs on** the older version, **the room now builds on** the newer one, and offers **Update to current**. Pressing it rebuilds the agent on the new runtime. Nothing else about the agent changes: its memory, its Slack app and its learnings stay. An agent held on a runtime of its own reads **Pinned to** that version and stays there when the room moves; **Move to the room's runtime** lets it follow the room again. Just after the room starts, the line may read **Checking what it runs on** for a moment while the room reads each agent's files. Whenever something you save restarts the agent (an upload, a persona edit, a model or runtime change, **Restart**), its page says **Restarting** at once and **Ready** again as soon as it is back, without a reload. A model, runtime or environment change is built first, so its restart can take a few minutes to begin, and the page says **Restarting** until it is done.

The room owns the `FROM` line in the agent's Dockerfile. Only **Update to current**, **Move to the room's runtime** and **set runtime image** move it: teaching the agent, an upload, a check, a Slack change, skills, colleague mode and a model change all leave the runtime as it is, so nothing you save moves the agent onto a runtime you didn't choose. In a room still building agents for every change, a teach note, upload, check or Slack change also puts back the runtime the room has recorded if someone edited the line by hand, and that commit's message names both images. To run an agent on a different runtime, its owner or an operator sets it (**set runtime image** from your own assistant). The agent's directory card shows the runtime's tag as its version. Pin as `tag@sha256:…` rather than a bare digest: the build is just as exact, and the card still shows a version instead of the start of the digest.

To move an agent onto another model after it is deployed, pick one from the list beside **Model** and press **Change model**. The room writes the choice into the agent's deploy contract and the agent rebuilds and rolls on it. The same change is **set agent model** from your own assistant.

When the room reaches its models through the model gateway, the list is the models a room admin offers, each with a tag saying which cloud it comes from, and a model in grey cannot be picked: the line under it says whether its cloud is not connected or the model gateway is not answering. Pressing **Change model** then also moves the agent onto the gateway: the room gives the agent its own key and points it at the gateway, so an agent made before the gateway was turned on keeps working as it was until someone presses **Change model**.

Below them, **Asks other agents** sets whether the agent may ask its colleagues: yes, when it helps (the default); no, but it knows who they are; or no. Pick one and press **Change**; the agent restarts with it. See "Agents talking to agents" for what each mode does.

## Agent to agent

Which agents from other teams this one may ask for help, and who has asked to reach it. **Request another team's agent** opens a list of the other teams; open a team to see its agents, each with one line on what it does, tick one or many across teams, and press **Request access** to send one request per agent. An agent already asked shows its state in place of the box, so nobody asks twice. When the room's permission service is on, that request waits in **Admin, Review** like any other permission and the row reads **Waiting for review** until a room admin approves it; otherwise it goes to the other agent's owner, and the requests waiting on your side appear here to accept or decline. The table below the picker lists every request and pair with the agent's team, its status and the date. Pairs inside the same group are automatic and marked so; accepted pairs can be revoked, and revoking takes effect within a few minutes. Every pair can join shared threads. See "Agents talking to agents" for the whole flow.

## Identity and access

Shown only while the permission service is off. The service account the agent runs as, the Google service account bound to it, and the room's project, with **Ask for access to another project** to write a request you hand to the SRE team. With the permission service on, access is asked for on the Access tab, which shows the identity the agent runs as in one line.

## Persona

The persona the agent runs, read in place: who it is, what it does, and any rules its persona still holds. The persona name in the agent's header opens this section too.

**Edit persona** changes it for this agent. **Save and restart** writes the new text into the agent's own files, and the agent restarts with it, usually within a minute or two; its memory, skills and Guardrails stay as they are, and the page says Updating until the new persona is in place. Where the agent's rules live in its Guardrails, the editor has no rules box: change them there. A persona that is not exactly in the room's usual parts, such as one edited by hand in its repository, is edited as one text, so nothing in it is lost. If someone else changed the persona while you had it open, your save is refused and the editor shows it as it stands, so nobody's change is lost.

The edit changes this agent alone. When you may also edit the persona it was made from, a line after the save says agents made from that persona later still start from the old text, and its button, **Save to** and the persona's name, puts the new text there as well. Agents already made from it keep their own.

Under it, **Details** shows the agent's team, purpose tags and capability sentence. **Edit details**, at the right of the agent's header, changes the team, purpose and description. The team is the one it works for, shown on its card in the agent directory; who can see the agent is its group. To give the agent a different persona altogether, use the set persona tool from your own assistant. See "Personas" for how personas are made.

## Skills

Skills are abilities from your organization's skills catalog: a way of working, a checklist, a domain the agent should know. Open **Browse the catalog** or **Suggestions** (skills recommended for this agent's persona), select the ones you want and press **Add N skills**. There is no second confirm: they go out together as one update and one restart of the agent, so select everything you want before you press it; one the agent already has says Added. **Write a skill** writes one by hand; it ends in the same table. A skill is how to do a job, and the agent reads it when a question needs it. A rule it must always follow belongs in **Guardrails**.

Each new row shows where its change is: **Pending** (saved, on its way), **Building** (the agent is being rebuilt with it), **Rolling** (the new agent is starting), **Active** (in place; the mark fades after a few seconds). **Failed** stays, with a sentence saying what to do, until you remove the row or change something and save again. While a change is building or rolling the page rests: every control that would change the agent is disabled, the banner at the top says Updating, and reading goes on as usual. The controls come back when the change lands or fails. On a current runtime the agent sees a short list of its skills in its instructions and loads one in full when a question needs it. Remove a skill from its row.

## Guardrails

The rules the agent follows on every task, one per line, in plain words. Each rule ends with a short tag saying where it came from: the persona, a skill, a correction, the person who added it, or the repository for a line someone wrote there by hand. Hover the tag for the whole story.

Type a rule in the box at the top and press **Add**; it goes at the bottom of the list. Click a rule's words to change them. The **⋯** button at the end of each rule holds **Edit**; **People**, **Emoji**, **Slack channels** and **Repositories**; **It broke this**; and **Remove**. A change reaches the agent for its next task without a restart. When the room can't reach it, the line under the list says the change lands at the agent's next restart instead. If someone else changed the list while you had it open, your save is refused and the panel shows the list as it stands, so nobody's change is lost. Write each rule so it makes sense on its own: "When searching tickets, use the person's full email", not "Use the full email". Always or never: a guardrail. How to do a job: a skill.

A rule can also name what it is about, so its words can say "these": **People**, like "Only answer these people", picked from the directory or typed as a full email; **Emoji**, like "Only open a ticket for a reaction with these emoji", by the name Slack shows (thumbsup is +1); **Slack channels**, like "Only post updates in these channels", by the name Slack shows; and **Repositories**, like "Open pull requests here only as drafts", picked from the room's list or typed as owner/name. Each shows under the rule's words. The agent is told who sent each Slack message and in which channel, and which emoji a reaction that woke it used, and it names the repository in all it does there, so it can tell when a rule applies. For a new rule, open the **⋯** beside **Add** first, so a rule like "Only answer these people" is never live without them. The room's own chat doesn't say who is typing yet, so there a rule about people can't tell. A renamed channel needs its new name on the rule. Like every rule, these guide the agent and lock nothing: which emoji wake it is set on the Slack row, and which repositories it can reach on the Access tab.

The list has room for about 4,000 characters, which is 30 to 40 rules, and the count at the top says how many more fit. What a rule names counts toward it, and one rule with all it names can be up to 1,000 characters. From twelve rules on, a box above the list finds a rule by its words or what it names. A rule that only matters inside one job belongs in that job's skill.

If the section says guardrails haven't turned on yet, this room's runtime doesn't read them, and an agent's rules are in its persona until it does.

Rules guide the agent; they don't lock anything. To make sure something never happens, take the permission away on the **Access** tab. When the agent breaks a rule, choose **It broke this** from the rule's **⋯** menu: anyone who can decide the agent's learnings can. The rule counts the breaks, and after the second it says the rule keeps breaking and needs a permission to hold.

Every agent the room builds also keeps keys and tokens out of its replies: anything in a reply shaped like one is cut out before it is sent. There is nothing to set or manage for it. **Always on**, below the rules, shows only on an agent that has a check written for it: a small program an engineer added, which runs on every task and can stop a request or change a reply, which a rule can't. Each says in a line what it does and whether it is on, and if one fails to load the section says so, because the agent then answers every message with a refusal until it is fixed or removed.

## Memory

The notes you have taught the agent and the files you have uploaded for it to learn from. Upload a document here and the agent reads it. See "Talk to it and teach it".

## Learnings

What the agent proposes from its own conversations each day, waiting for your decision. The badge on the Harness tab is the count. Nothing is kept until you press Remember.

## Activity

Every call the agent made through the tool gateway in the window you pick, and every one it was refused. A summary comes first: calls, refused, errors, tools used. Rows are grouped by day; the newest day and any day with a refusal or error open on their own, the rest open when clicked, 25 rows at a time, newest first. Click a row to read the whole call: its arguments in full and what happened; click again to fold it. The table scrolls sideways when it must. **Problems only** keeps the refusals and errors. Below it, **Borrowed credentials** lists each hour of access the agent took for a task when the permission service is on.