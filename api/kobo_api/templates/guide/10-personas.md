# Personas
<!-- summary: Built-in personas, finding one by group, building and editing your own, the naming rule, and which group a persona belongs to. -->

A persona is the soul of an agent: who it is, how it works, and its always and never rules. An agent is built from exactly one. You meet them on the create form, where you pick one.

## Built-in personas

The room ships five general personas, kept to the house standard: efficient, pleasant, plain. They sit first in every list and belong to everyone: a helpful generalist, a billing analyst, an identity admin, a code reviewer, and a cloud operator. Start from one of these when it fits, and build your own when it does not.

## Personas from your groups

Above the list sits a row of pills: **Built-in** first, then every group that has personas of its own, then **Everyone**. One pill is pressed at a time and the list shows that group's personas. It opens on Built-in. A persona is filed under the group that made it, so it stays that team's unless they choose to share it. Some of the room's earlier built-ins now live this way: the other cloud operators under infra-engineering, the marketing strategist under marketing, the workspace admin under corporate-it.

The search box looks across every group at once, by name and description. While you search, the pills step aside and each match shows under the name of its group.

## Reading one in full

Rest your pointer on a persona to see its card. **Read the full persona** opens the whole thing in place: who they are, everything they do, every rule, and who made it. **Back** returns you to the list with your pick unchanged.

On an agent's page, the persona name links to the `bundle/persona.md` file in the agent's repository. That file is exactly what the running agent was built from.

## Build your own

From the create form, **Build your own persona** opens the builder. It is one form with four parts:

- **Name.** One or two words. This names the persona, not the agent; the agent gets its own name later.
- **Who they are.** One line that finishes the sentence "You are (the agent's name), ...".
- **What they do.** The body of the persona: the work and who it is for, the ground it owns and what it leaves to others, each kind of request and how to handle it, what wins when two of those pull apart, and what to do when unsure. Most personas run 25 to 45 lines. The room asks for at least 300 characters.
- **Rules.** One per line: things it always or never does. At least 3. Most personas have 8 to 12. In a room where guardrails are on, the form calls them **Starter guardrails**: each new agent starts its Guardrails list with them (see "The harness"), its owners change them there, and giving the agent another persona later leaves them alone.

You do not have to start from a blank form. **Start from a description**, at the top, takes a few sentences about the job, or notes or a document you already have: paste it, or use **Upload a file**. Press **Draft persona** and the room writes the four parts in the same shape the built-ins are written in. A long document keeps its detail: its steps, names and templates are carried over under bold labels, and every rule it states is kept, so a draft made from one runs longer than most. Anything you had already typed stays as you typed it. Read the draft through and change whatever you like.

**Group** is where the persona is filed. It starts on your own group, and its members can edit the persona later. If you are in several groups, you choose one. Choose **Everyone** to share it with the whole room.

**Create persona** stays off until the form has enough, and a line beside it says what is still missing. Once created, the persona is picked for you and shows in the list under its group.

## The naming rule

A persona name is one or two words, three at most, like `sentinel`, `ledger-keeper` or `billing-analyst`. The room refuses a longer one. Traits belong in the who-they-are line, not in the name. Names are lowercase letters, numbers and hyphens. If a persona already has the name, the room says which group it is filed under, so you can find it instead of making it twice.

## Editing, moving and archiving

At the bottom of the full persona, its maker, a member of its group, or an operator can **Edit** it, **Move to** another group or to Everyone, and **Archive** it. A persona shared with everyone is edited and moved only by its maker or an operator.

**Edit** opens the same form with the persona's text in it. You can change who they are, what they do and the rules; a persona keeps its name. The persona of a team's informational guide has no Edit: the room wrote it whole, not in the form's four parts. An agent copies its persona when it is made, so an edit reaches agents made from then on and an agent already made keeps the persona it was made with.

Archiving takes a persona out of every list without deleting it; agents already built from it keep running. Ask an operator to bring an archived persona back. Built-in personas stay shared, and are never edited or moved.
