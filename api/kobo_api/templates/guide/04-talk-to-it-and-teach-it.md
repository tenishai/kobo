# Talk to it and teach it
<!-- summary: The Chat tab, Teach this, how memory works, and the nightly learnings you approve. -->

The **Chat** tab is where you meet your agent. Open it and say hello. Its **First words**, the reply it gave the room at first contact, sit above the conversation.

## Teach this

Every message the agent sends has a quiet **Teach this** link. Press it when the agent said something worth keeping, or when you have just told it something it should remember. Edit the wording if you like and save. The room files the note into the agent's memory and tells you how it landed:

- **Learned. Filed to the repo in the background.** The agent took it in at once.
- **Saved to memory. It arrives with the next delivery.** The agent was not reachable for an instant update, so the note lands on its next start.

Either way the note is written as a file in the agent's repository, dated and attributed, so it survives restarts and rebuilds.

## Memory

An agent's memory is the set of notes you have taught it and the files you have uploaded for it under **Harness, Memory**. Memory is per agent and stays with it. Nothing an agent hears is kept unless someone teaches it or approves it.

## Learnings

Once a day the room asks each agent what it picked up from its conversations. The agent proposes a handful of one-line statements, each attributed to the person who said it where it can be. Nothing is kept until you decide.

The **Learnings** section in the Harness tab lists what is pending, with **Remember** and **Skip** on each row, a pencil to fix the wording first, and **Remember all** or **Skip all** for a batch. Remembering rides the same lane as Teach this. Skipped rows are kept in the history. Anything left undecided for two weeks expires.

Two kinds of row are marked and need a second click on **Remember**, and **Remember all** leaves them for you to check one by one: a statement nobody can be attributed with, and one about access (who approved, granted or revoked what). An approval is an event, not a standing fact, and once remembered the agent could cite it as authority.

If the nightly pass keeps failing for an agent (for example, its runtime is too old to run it), the section says so with the last error, and the agent's card on the dashboard shows **learnings failing**.

The same section has a **Teach something** box for a one-line note when you are not in a chat.

## Agent conversations

Below the chat, **Colleague conversations** lists every exchange this agent has had with other agents, live, one line each. It is the paper trail for the wire described in "Agents talking to agents".
