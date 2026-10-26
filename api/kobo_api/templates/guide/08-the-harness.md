# The harness
<!-- summary: What runs the agent: model and runtime, persona, skills, tooling, memory, learnings. -->

The **Harness** tab is everything that runs your agent, in seven sections down its left side. A connected agent shows only the first two, because it brings its own harness.

## Model and runtime

The model the agent thinks with, and the runtime it runs on. The runtime is one shared image the room maintains for every agent it builds. When the room moves to a newer runtime, this section says **Behind: the room now builds on** the newer version and offers **Update to current**. Pressing it rebuilds the agent on the new runtime. Nothing else about the agent changes: its memory, its Slack app and its learnings stay.

The room owns the `FROM` line in the agent's Dockerfile. If someone edits it by hand, the next teach note, upload, tool or Slack change the room commits puts back the runtime the room has recorded, and that commit's message names both images; skill, colleague-mode and model changes leave the line alone. To run an agent on a different runtime, its owner or an operator sets it (**set runtime image** from your own assistant). The agent's directory card shows the runtime's tag as its version. Pin as `tag@sha256:…` rather than a bare digest: the build is just as exact, and the card still shows a version instead of the start of the digest.

To move an agent onto another model after it is deployed, pick one from the list beside **Model** and press **Change model**. The room writes the choice into the agent's deploy contract and the agent rebuilds and rolls on it. The same change is **set agent model** from your own assistant.

Below them, **Colleagues** sets whether the agent may ask other agents: consult colleagues, know the roster and ask a human, or no colleagues. Pick one and press **Change**; the agent rebuilds with it. See "Agents talking to agents" for what each mode does.

## Identity and access

The service account the agent runs as, the Google service account bound to it, and the room's project. Below them, **Ask for access to another project**: name the project and the roles, one per line, and the room writes the request you hand to the SRE team. Basic roles like owner, editor and viewer are refused; ask for the narrowest role that does the job.

## Persona

The persona the agent was built from, with its team, purpose tags and capability sentence. **Edit details**, in the **More** menu at the right of the agent's name, changes the team, purpose and description. The persona itself can be swapped from your own assistant with the set persona tool. See "Personas" for how personas are made.

## Skills

Skills are abilities from your organization's skills catalog: a way of working, a checklist, a domain the agent should know. Add one from the catalog and the agent picks it up on its next build. On a current runtime the agent sees a short list of its skills in its instructions and loads one in full when a question needs it. The section also lists skills recommended for this agent's persona. Remove a skill the same way.

## Tooling

Tooling is code the agent runs around every task. Every agent built by the room starts with the baseline pack: a leak guard that stops secrets leaving in a reply, and a probe guard that stops the agent being led off its job. You can add tools from the catalog or upload your own. A tool is a small Python file that hooks in before a task or after a draft. The room checks it before it lands, and the agent enforces what is in its tooling folder. Nothing runs that is not there.

## Memory

The notes you have taught the agent and the files you have uploaded for it to learn from. Upload a document here and the agent reads it. See "Talk to it and teach it".

## Learnings

What the agent proposes from its own conversations each day, waiting for your decision. The badge on the Harness tab is the count. Nothing is kept until you press Remember.

## Activity

Every call the agent made through the tool gateway, and every one it was refused, newest first: the tool, how it went, how long it took, and the arguments in brief with anything secret masked. Filter by tool to see what it does with one vendor, or pick a longer window. A refusal names what was missing, which is the moment to widen the agent's lane under Connections. Calls into a cloud account also appear in that account's own audit trail under the agent's name.

