# Slack
<!-- summary: One-time Slack app setup from a manifest, the three values to paste back, and where the agent answers. -->

The **Slack** tab walks you through connecting the agent to your workspace. The first time, it shows three numbered steps. Once connected, it shows only the channel list, with the setup folded away under **Reconnect the Slack app**.

If the room itself has no Slack connection yet, the tab is a greyed box with one line, "Ask a kobo admin to add the Slack connection", and no steps, fields or buttons. An admin sets it up under **Admin > Integrations**, on the **Slack** row; the tab works as soon as the values are saved. Nothing an agent owner does can stand in for that.

## When the room makes the app for you

In some rooms Slack is a row on the **Access** tab instead, with the other vendors. There is nothing to copy or paste: pick **Wakes on**, press **Create Slack app**, then **Install to Slack**. Until the app is installed the agent keeps working everywhere else; it just is not in Slack yet. Slack asks you to allow the app in your workspace and sends you straight back to the agent's page, with the Slack row in view: the workspace, **Wakes on**, and the row's state. Installing is instant, and the agent restarts to pick Slack up, usually under a minute (in a room still building agents for every change, it rebuilds first, about three minutes, and the row reads **Rebuilding**), so the row reads **Restarting**, and **Live** only once the agent is answering; a mention sent while it restarts is kept and answered once it is back. Invite the agent to any channel in Slack and it answers there; that is the whole of it, there is no channel list on the row. Changing **Wakes on** later applies on its own: the row reads **Applying your change** and then **Restarting** while the agent restarts with the new choice, and **Live** only once the room has seen the agent back and listening, usually within a minute; wait for **Live** before trying the new choice. While it restarts, the agent's page says **Restarting** rather than Down. **Remove** takes the app out of Slack. An agent connected the older way, from the Slack tab, keeps that tab and keeps working. The rest of this section describes the Slack tab.

### Reactions

A reaction can wake the agent. On the Slack row, under **Wakes on**, the field **Also wakes on these emoji** takes the names of the emoji that should, as Slack shows them, with commas between them: `ticket, rotating_light`. Press **Save** and the agent restarts with them, the way it does for a **Wakes on** change. From then on, when someone adds one of those emoji to a message in a channel the agent is in, the agent reads that message and answers in its thread, whichever **Wakes on** you picked. The reaction is the ask: nobody has to write anything, and the person who reacted is the one the agent answers.

Leave the field empty and no reaction wakes the agent, which is how every agent starts. The rest is fixed. Taking a reaction off does nothing. The agent answers once for each emoji on a message, however many people add it. Its own reactions never wake it. You can name up to ten, and not the four marks agents leave on the messages they answer (`eyes`, `white_check_mark`, `x`, `octagonal_sign`).

An agent that runs its team's own code is handed the names and its code decides what a reaction does. An app made before the room listened for reactions hears them from the first time you save the field; there is nothing to reinstall.

### What it can do in Slack

Beside answering where it is asked, an agent the room built can do five things in Slack. They come with the app. One of them, the slash command, has a switch for where it is answered.

- **Offer choices.** When a question has a short list of answers, the agent posts it with a button for each, or a menu when there are more than five. Press one and that is your answer: the message changes to say who chose what, and the agent carries on in the same thread. The first pick counts and the buttons go away.
- **Take a slash command.** Type `/` and the agent's name, then what you want: `/clio what changed today?`. The agent posts who asked what in that channel and answers in the thread under it. It works wherever the agent has been added, a channel or a group message, and in a direct message with it; anywhere else the agent tells you, and only you, to add it first. To let people ask from any public channel, tick **Answer its slash command in any public channel** on the Slack row. The agent restarts, and from then on it posts the question and its answer in whichever public channel the command was typed in, whether or not it was ever added there. It is off for every agent until its owner ticks it; a private channel always needs the agent added. An agent whose name Slack already uses for a command of its own, such as `remind`, has no slash command.
- **Post in a public channel it was not invited to.** Ask it to announce something in `#releases` and it can, without anyone inviting it there first. It still hears only the channels it is in, so invite it wherever people should be able to talk back. Private channels always need the invite.
- **Pin a message.** It can pin a message in a channel it is in, list what is pinned, and take a pin off.
- **Bookmark a link.** It can add a link to the bar at the top of a channel it is in, list what is there, and remove one.

A team's informational guide, which never acts, gets none of the four things an agent does on its own; its slash command still asks it a question.

### When the room asks Slack for more

Now and then a release of the room lets a Slack app do more. The latest adds what "What it can do in Slack" describes: choices, the slash command, posting without an invite, pins and bookmarks. The one before added posting a file, reading the workspace's custom emoji, reading its user groups, and hearing when someone joins a channel the agent is in; an agent the room built does nothing with those four yet.

Slack only gives an app more when a person allows it, so an app installed before such a release keeps working exactly as it was and its row shows **Update**. Open the row and press **Update in Slack**: Slack shows what is new and asks you to allow it, then sends you back to the agent's page. The agent keeps answering until then and restarts once after. One update brings everything the app is owed, however many releases it missed. An app installed after the release has it all from the start, and its row never asks.

The agent also has to be on the room's current runtime to use what is new. If the Harness tab offers **Update to current**, press it. An agent connected the older way, from the Slack tab, gets all of this when it moves to the new Slack setup.

### An agent that runs its team's own code

A connected agent that serves a port can have the room make its Slack app the same way: pick **Wakes on**, press **Create Slack app**, then **Install to Slack**. The pull request the room opened when the agent was connected already lets it read the app, so nothing more is asked of its team: the agent restarts with the app once it is installed. Changing **Wakes on**, the emoji or the slash command choice later is no pull request either. The row reads **Saved. The agent restarts with it in a minute or two.**, and the room restarts the agent once the choice has reached it.

One thing differs for an agent connected before the room did this. The change that hands the app to the agent arrives in its repository as one pull request, which the room merges itself. If the repository's rules stop the room, the row reads **The room could not merge its pull request**, with a link to it, until someone who can merge there does. That is the only one Slack asks for; later choices are restarts. If the request is closed without being merged the row says so, and **Open it again** opens it afresh; no second app is made. The agent's code has to take the messages the room hands it. See "Running your own code".

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
