# Create your first agent
<!-- summary: New agent, pick a persona, type a name, choose a group. The room does the rest. -->

Press **New agent** in the left rail. The room asks how you want to start:

- **Build an agent.** Pick a persona and the room builds and deploys it. Start here.
- **Connect a repository you already have.** Your code, your runtime. Covered in "Before you start".

Choose **Build an agent**. The create form is one column with four fields.

## 1. Persona

A persona is who the agent is: its manner, its judgment, and the work it does. The list opens with the room's five **built-in** personas. Above it, a pill for each group that has personas of its own; press one to see them. The search box looks across every group by name and description.

Rest your pointer on a persona to see its card, and **Read the full persona** for all of it. If nothing fits, **Build your own persona** opens the persona builder without leaving the form. See "Personas".

## 2. Name

Type the agent's name. Lowercase letters, numbers and hyphens, 2 to 24 characters. The form checks availability as you type. A name is a name, so `release-notes` or `ledger` works; a hyphenated list of traits does not.

## 3. Group

Who can see and use this agent. The menu lists your directory groups. Pick your own.

If your group is not listed, choose **My group is not listed**, type its name, and press **Request it**. An operator adds it, and you create the agent once it is there.

## 4. Model

The model the agent thinks with, named by family and tier: **Best** costs the most and thinks the hardest, **Balanced** is the everyday choice, **Fast** is the cheapest. Pick the tier the work calls for. The exact model behind each tier is the room's choice and shows on the agent's Harness tab.

When the room reaches its models through the model gateway, the list is one list across every cloud the room is connected to, and a short tag after each name says where that model comes from: GCP, AWS, Azure or Self-hosted. A room admin chooses which models the list offers, under Admin, Models; a model nobody has chosen is not in it. A model in grey cannot be picked yet, and the grey words beside it say why: its cloud is not connected, or the model gateway is not answering. With no model available at all, **Create agent** stays off and one sentence asks for a cloud with models to be connected under Admin, Integrations, first.

Press **Create agent**. The room takes you to the agent's page, where a checklist shows the build as it happens. Nothing else is asked of you.
