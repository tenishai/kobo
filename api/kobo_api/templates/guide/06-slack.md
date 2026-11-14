# Slack
<!-- summary: One-time Slack app setup from a manifest, the three values to paste back, and where the agent answers. -->

The **Slack** tab walks you through connecting the agent to your workspace. The first time, it shows three numbered steps. Once connected, it shows only the channel list, with the setup folded away under **Reconnect the Slack app**.

If the room itself has no Slack connection yet, the tab is a greyed box with one line, "Ask a kobo admin to add the Slack connection", and no steps, fields or buttons. An admin sets it up under **Admin > Integrations**, on the **Slack** row; the tab works as soon as the values are saved. Nothing an agent owner does can stand in for that.

## When the room makes the app for you

In some rooms Slack is a row on the **Access** tab instead, with the other vendors. There is nothing to copy or paste: pick **Wakes on**, press **Create Slack app**, then **Install to Slack**. Slack asks you to allow the app in your workspace and sends you straight back to the agent's page, with the Slack row in view: the workspace, **Wakes on**, and the row's state. Installing is instant, and the agent restarts to pick Slack up, usually under a minute (in a room still building agents for every change, it rebuilds first, about three minutes, and the row reads **Rebuilding**), so the row reads **Restarting**, and **Live** only once the agent is answering; a mention sent while it restarts is kept and answered once it is back. Invite the agent to any channel in Slack and it answers there; that is the whole of it, there is no channel list on the row. Changing **Wakes on** later applies on its own: the row reads **Applying your change** and then **Restarting** while the agent restarts with the new choice, and **Live** only once the room has seen the agent back and listening, usually within a minute; wait for **Live** before trying the new choice. While it restarts, the agent's page says **Restarting** rather than Down. **Remove** takes the app out of Slack. An agent connected the older way, from the Slack tab, keeps that tab and keeps working. The rest of this section describes the Slack tab.

### Reactions

A reaction can wake the agent. On the Slack row, under **Wakes on**, the field **Also wakes on these emoji** takes the names of the emoji that should, as Slack shows them, with commas between them: `ticket, rotating_light`. Press **Save** and the agent restarts with them, the way it does for a **Wakes on** change. From then on, when someone adds one of those emoji to a message in a channel the agent is in, the agent reads that message and answers in its thread, whichever **Wakes on** you picked. The reaction is the ask: nobody has to write anything, and the person who reacted is the one the agent answers.

Leave the field empty and no reaction wakes the agent, which is how every agent starts. The rest is fixed. Taking a reaction off does nothing. The agent answers once for each emoji on a message, however many people add it. Its own reactions never wake it. You can name up to ten, and not the four marks agents leave on the messages they answer (`eyes`, `white_check_mark`, `x`, `octagonal_sign`).

An agent that runs its team's own code is handed the names and its code decides what a reaction does. An app made before the room listened for reactions hears them from the first time you save the field; there is nothing to reinstall.

### When the room asks Slack for more

Now and then a release of the room lets a Slack app do more. The latest adds three things: posting a file, reading the workspace's custom emoji, and reading its user groups, so an agent can tell who `@oncall` is. It also tells the agent when someone joins a channel it is in. A connected agent's own code can use all of it at once. An agent the room built does nothing with these yet.

Slack only gives an app more when a person allows it, so an app installed before such a release keeps working exactly as it was and its row shows **Update**. Open the row and press **Update in Slack**: Slack shows what is new and asks you to allow it, then sends you back to the agent's page. The agent keeps answering until then and restarts once after. An app installed after the release has it all from the start, and its row never asks.

### An agent that runs its team's own code

A connected agent that serves a port can have the room make its Slack app the same way: pick **Wakes on**, press **Create Slack app**, then **Install to Slack**. One thing differs. The room never pushes to a team's repository, so the change that hands the app to the agent arrives there as a pull request, and the row reads **Waiting for its team to merge a pull request**, with a link to it, until the team merges it. Changing **Wakes on** later is a pull request too. If the request is closed without being merged the row says so, and **Open it again** opens it afresh; no second app is made. The agent's code has to take the messages the room hands it. See "Running your own code".

A team that already has a Slack app of its own can keep it. Save its tokens as secrets on the **Secrets** tab, under `SLACK_BOT_TOKEN`, `SLACK_APP_TOKEN` and `SLACK_SIGNING_SECRET` or whatever names the code reads, and the agent restarts with them. Take those tokens off the **Secrets** tab before asking the room for an app: the room's app brings its own, under the same names.

A connected agent that serves no port has nowhere to be handed Slack's messages, so it always brings its own app, and its row has no **Create Slack app**.

### The agent's face

Every message the agent posts in Slack carries the picture it has in kobo. Change the picture on the agent's page and Slack shows the new one from the next message on; there is nothing to set in Slack. An agent connected before this needs one **Reconnect the Slack app** so its app may show a picture; until then it posts without one. The small picture Slack shows beside the agent in its member list is Slack's own and stays as it is.

### Who said what

When an agent reads a Slack thread it sees each person by name and email: who wrote each message and who left each reaction. That is how an agent checking an approval can tell whose thumbs-up it was. An agent connected before this sees people only as Slack ids until its app is reconnected once with **Reconnect the Slack app**.

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
