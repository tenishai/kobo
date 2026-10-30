# Slack
<!-- summary: One-time Slack app setup from a manifest, the three values to paste back, and where the agent answers. -->

The **Slack** tab walks you through connecting the agent to your workspace. The first time, it shows three numbered steps. Once connected, it shows only the channel list, with the setup folded away under **Reconnect the Slack app**.

If the room itself has no Slack integration yet, the tab is a greyed box with one line, "Ask a kobo admin to add the Slack integration", and no steps, fields or buttons. An admin sets it up under **Admin > Integrations**, on the **Slack** row; the tab works as soon as the values are saved. Nothing an agent owner does can stand in for that.

## 1. Where it answers

Add the ID of each channel the agent may answer in, then invite the agent to that channel in Slack with `/invite @agent-name`. Both are needed: the channel list is an allowlist the agent enforces, and the invite is Slack's own permission. Up to eight channels.

Pick **Wakes on** here too, before the next step: the manifest the room writes carries the event list your choice decides.

## 2. Create the Slack app from this manifest

The room writes a Slack app manifest for this agent, with exactly the permissions it needs. Press **Copy the manifest**, open Slack's app management page from the link in the step, press **Create New App**, choose **From a manifest**, pick your workspace, and paste the manifest on the JSON tab. Slack creates the app.

## 3. Paste the three values back

Slack shows you three values. Paste each into its field:

- **App ID**, from Basic Information.
- **Bot token**, from Install App. It starts with `xoxb-`.
- **App-level token**, which you generate on Basic Information with the `connections:write` scope. It starts with `xapp-`.

Press **Connect**. The room delivers the tokens straight to the agent as secrets and keeps no copy. They are never shown again.

## How it wakes up

**Wakes on** decides which channel messages reach the agent. Pick one:

- **Mentions only.** Only a message that @-mentions the agent reaches it. A reply in a thread that does not mention it is never delivered, so each question needs the mention.
- **Mentions, then the thread.** Mention it once at the top of a thread and every reply in that thread reaches it. A mention buried in someone else's thread gets an answer and nothing more. New agents start here.
- **Every message in the channel.** For an agent whose job is the channel. It hears everything, mentioned or not.

A direct message always reaches the agent, whichever you pick. The agent is told its own setting, so if someone asks whether they must keep mentioning it, it answers truthfully.

The choice saves as soon as you pick it. Moving between **Mentions only** and either of the other two changes which events the Slack app receives, so copy the manifest again and paste it over the old one at api.slack.com/apps under App Manifest. The room tells you when that is needed. No reinstall, and no new tokens.

## After it is connected

The status chip reads **Connected**. Add or remove channels any time and press **Save**. The status line under the table tells you when the change is in place. To rotate tokens or move the app, open **Reconnect the Slack app** and repeat steps 2 and 3.
