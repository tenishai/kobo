# Slack
<!-- summary: One-time Slack app setup from a manifest, the three values to paste back, and where the agent answers. -->

The **Slack** tab walks you through connecting the agent to your workspace. The first time, it shows three numbered steps. Once connected, it shows only the channel list, with the setup folded away under **Reconnect the Slack app**.

## 1. Create the Slack app from this manifest

The room writes a Slack app manifest for this agent, with exactly the permissions it needs. Press **Copy the manifest**, open Slack's app management page from the link in the step, press **Create New App**, choose **From a manifest**, pick your workspace, and paste the manifest on the JSON tab. Slack creates the app.

## 2. Paste the three values back

Slack shows you three values. Paste each into its field:

- **App ID**, from Basic Information.
- **Bot token**, from Install App. It starts with `xoxb-`.
- **App-level token**, which you generate on Basic Information with the `connections:write` scope. It starts with `xapp-`.

Press **Connect**. The room delivers the tokens straight to the agent as secrets and keeps no copy. They are never shown again.

## 3. Where it answers

Add the ID of each channel the agent may answer in, then invite the agent to that channel in Slack with `/invite @agent-name`. Both are needed: the channel list is an allowlist the agent enforces, and the invite is Slack's own permission. Up to eight channels.

In a channel the agent answers only when you @-mention it, so it does not join every thread. A direct message always reaches it. Messaging the agent directly is the ask.

## After it is connected

The status chip reads **Connected**. Add or remove channels any time and press **Save**. The status line under the table tells you when the change is in place. To rotate tokens or move the app, open **Reconnect the Slack app** and repeat steps 1 and 2.
