# Secrets and variables
<!-- summary: Give the agent API keys and settings, know at a glance that a secret is really set, and restart to apply. -->

Open the **Settings** tab. **Secrets and environment** is one table with two kinds of row.

- **Secret.** A value the agent needs and nobody should read again: an API key, a token, a password. Press **+ Secret**, type the name and the value, and press **Save**. The value is sent once and never shown again.
- **Variable.** A plain setting that is not sensitive, like a region or a feature flag. Press **+ Variable**. Variables stay readable.

**Paste .env** takes a block of `KEY=value` lines and sorts them into the table for you. Rows are a draft until you press **Save**; **Discard** drops the draft.

## Where secrets live

Secrets are stored in the room's secrets platform, never in the agent's repository, and reach the agent as environment variables. Variables are written into the agent's own files. For a connected agent the room proposes that change as a pull request, because it never pushes to your branches.

## The lock

A stored secret's value cell shows a padlock and nothing else. The lock tells you whether the value has actually reached the agent:

- **Secret, live on the agent.** Set and delivered.
- **Secret, syncing.** Saved, on its way.
- **Secret, not on the agent yet.** Saved, not delivered. Rest on it to read why.

Secrets the room set itself, like the agent's colleague pass or its Slack tokens, show as **Set by the room** and cannot be removed here.

## Restart to apply

A secret or variable reaches a running agent when it next starts, not before. After you save, press **Restart agent**. The agent's pods go down and come back on the same image with the new values. Nothing is rebuilt. Anything the agent was doing at that moment is lost, so do not restart it mid task.
