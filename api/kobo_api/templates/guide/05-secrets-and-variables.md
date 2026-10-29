# Secrets and variables
<!-- summary: Give the agent API keys and settings, and know at a glance that a secret is really set. Save applies the change. -->

Open the **Settings** tab. **Secrets and environment** is one table with two kinds of row.

- **Secret.** A value the agent needs and nobody should read again: an API key, a token, a password. Press **+ Secret**, type the name and the value, and press **Save**. The value is sent once and never shown again.
- **Variable.** A plain setting that is not sensitive, like a region or a feature flag. Press **+ Variable**. Variables stay readable.

**Paste .env** takes a block of `KEY=value` lines and sorts them into the table for you. Rows are a draft until you press **Save**; **Discard** drops the draft.

A name can appear in the table once. A second row with the same name, whether a secret or a variable, is marked **already listed** and Save waits until it is fixed. To change a stored secret's value, remove its row, add it again with the new value, and save once.

## Where secrets live

Secrets are stored in the room's secrets service, never in the agent's repository, and reach the agent as environment variables. A room with no secrets service keeps the table and the variables but cannot store a new secret, so the controls that add one are not there. Variables are written into the agent's own files. For a connected agent the room proposes that change as a pull request, because it never pushes to your branches.

## The lock

A stored secret's value cell shows a padlock and nothing else. The lock tells you whether the value has actually reached the agent:

- **Secret, live on the agent.** Set and delivered.
- **Secret, syncing.** Saved, on its way.
- **Secret, not on the agent yet.** Saved, not delivered. Rest on it to read why.

Secrets and variables the room set itself, like the agent's colleague pass, its Slack tokens, its model or how Slack wakes it, show as **Set by the room** and cannot be edited or removed here.

## Save applies the change

A secret or variable reaches a running agent when it next starts, not before, so when the agent is running and the save changed something, Save restarts it for you: the agent's pods go down and come back on the same image with the new values, and the line under the table says so. Nothing is rebuilt. Anything the agent was doing at that moment is lost, so do not save a change mid task. A save that changed nothing, or a change waiting in a pull request, does not restart anything.
