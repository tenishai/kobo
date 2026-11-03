# Slack
<!-- summary: One-time Slack app setup from a manifest, the three values to paste back, and where the agent answers. -->

The **Slack** tab walks you through connecting the agent to your workspace. The first time, it shows three numbered steps. Once connected, it shows only the channel list, with the setup folded away under **Reconnect the Slack app**.

If the room itself has no Slack connection yet, the tab is a greyed box with one line, "Ask a kobo admin to add the Slack connection", and no steps, fields or buttons. An admin sets it up under **Admin > Integrations**, on the **Slack** row; the tab works as soon as the values are saved. Nothing an agent owner does can stand in for that.

## When the room makes the app for you

In some rooms Slack is a row on the **Access** tab instead, with the other vendors. There is nothing to copy or paste: pick **Wakes on**, press **Create Slack app**, then **Install to Slack**. Slack asks you to allow the app in your workspace and sends you straight back to the agent's page, with the Slack row in view: the workspace, **Wakes on**, and the row's state. Installing is instant, but the agent rebuilds and restarts to pick Slack up, so the row reads **Restarting** for a couple of minutes and **Live** once the agent is answering; a mention sent while it restarts is kept and answered once it is back. Invite the agent to any channel in Slack and it answers there; that is the whole of it, there is no channel list on the row. Changing **Wakes on** later applies on its own: the row reads **Applying your change** and then **Restarting** while the agent restarts with the new choice, and **Live** only once the room has seen the agent back and listening, usually within a minute; wait for **Live** before trying the new choice. While it restarts, the agent's page says **Restarting** rather than Down. **Remove** takes the app out of Slack. An agent connected the older way, from the Slack tab, keeps that tab and keeps working. The rest of this section describes the Slack tab.

### The agent's face

Every message the agent posts in Slack carries the picture it has in kobo. Change the picture on the agent's page and Slack shows the new one from the next message on; there is nothing to set in Slack. An agent connected before this needs one **Reconnect the Slack app** so its app may show a picture; until then it posts without one. The small picture Slack shows beside the agent in its member list is Slack's own and stays as it is.

### Moving an agent made the old way

An agent whose Slack app came from the Slack tab shows **Connected the old way** and its app id on the Slack row of the **Access** tab, with one button, **Move to the new Slack setup**. The room makes a new app and you install it once; the old app is removed. Channels and **Wakes on** carry over. After the move the row reads **App made; install it to finish**: press **Install to Slack** as usual, and the agent answers through the new app once you are back on its page. If the room could not confirm the old app was removed, the row says so; look for it in Slack's app list and remove it there. The Slack tab goes away for that agent once it has moved.

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

The choice saves as soon as you pick it, and the agent restarts with the new choice; the note under the choices reads **Live.** only once the room has seen the agent back and listening, usually within a minute. Moving between **Mentions only** and either of the other two changes which events the Slack app receives, so copy the manifest again and paste it over the old one at api.slack.com/apps under App Manifest. The room tells you when that is needed. No reinstall, and no new tokens.

## After it is connected

The status chip reads **Connected**. Add or remove channels any time and press **Save**. The status line under the table tells you when the change is in place. To rotate tokens or move the app, open **Reconnect the Slack app** and repeat steps 2 and 3.
