# Running your own code
<!-- summary: For a team whose agent is its own code: what the room gives a connected agent, the names it arrives under, and how the agent reaches what it was granted. -->

A connected agent is your team's code in your team's repository. The room gives it the same things it gives an agent it builds: storage that outlives a restart, an identity, model access, secrets, and a way to reach whatever it has been granted on the **Access** tab. Your code finds each of them in its environment. This section is the contract: the names, what each one holds, and what your code does with it.

## How it arrives

All of it is added to your repository's deploy contract when you connect, in a pull request your team merges.

- **A repository with no deploy files yet gets one pull request.** The deploy service's setup pull request carries the room's part too, and lists which lines and files the room asked for. Merge it and the agent starts with everything above. Nothing from the room follows.
- **A repository that already has deploy files gets two.** The deploy service's setup pull request comes first and leaves your files as you wrote them; the room's wiring pull request follows with its part, so that nothing is laid over a person's work unseen.

A room whose deploy service predates the single pull request sends two in both cases. An agent connected before the room wired anything is offered the wiring pull request within a few minutes of its first approved permission, and its **Access** tab says it is waiting on that merge. The room never merges that later one for you.

Merging a pull request from the room is followed, a few minutes later, by a second one from the deploy service that regenerates the deployment files. Your repository's build refuses to deploy in between; that is expected. Merge the second and the agent restarts with the change. You do not have to ask for it.

## What your code reads

- `KOBO_DATA_DIR`. A folder that is still there after a restart, with 1 GiB to start. Put logs, caches and anything else you want back in it. One copy of the agent runs at a time, so nothing else writes there. If your deploy contract already mounts storage of its own, the room leaves it as it is and sets no folder.
- `CABAL_COLLEAGUE_TOKEN`. The agent's pass, a secret. It proves which agent is asking. Send it to the switchboard and nowhere else.
- `KOBO_SWITCHBOARD_URL`. Where the pass is traded for a gateway token.
- `KOBO_GATEWAY_URL`. The tool gateway, an MCP server. It offers exactly the tools the agent's permissions allow, and nothing when it holds none.
- `KOBO_MODELS_URL` and `MODELS_GATEWAY_KEY`. Only when the room reaches its models through the model gateway: an OpenAI-compatible address, and the agent's own key for it. Send the key as the bearer token.
- Your own secrets and variables, under the names you gave them on the **Secrets** tab. See "Secrets and variables".

The pass and both gateway names are there only when the room has a tool gateway. A room without one sets the folder and nothing else.

## Reaching what the agent was granted

The pass does not open the gateway by itself. Three steps:

1. Send a `POST` to `KOBO_SWITCHBOARD_URL` with `/auth/liminal-token` on the end, and the header `Authorization: Bearer` followed by the pass. No body. The answer is JSON with `access_token` and `expires_at`.
2. Connect to `KOBO_GATEWAY_URL` as an MCP server over HTTP, with the header `Authorization: Bearer` followed by that `access_token`. List the tools and call them like any other MCP server's.
3. The token lasts 15 minutes. Trade the pass again a minute before `expires_at`, and once more if the gateway answers 401.

A permission approved a minute ago shows up in the tool list without a restart, and a revoked one leaves it. Each vendor call borrows an hour of access scoped to the permissions the agent holds; your code never sees a standing credential. What the agent did, and what it was refused, is under **Harness, Activity**.

The room replaces the pass when the agent's wiring is redone. The agent picks the new one up when it restarts, which the same change brings.

## Cloning with git

The gateway's tools read files and pull requests for the agent, and the credential stays on the gateway. That does not help code that runs `git clone`. So an agent that runs its own code and holds **Read** on GitHub has one more tool: `github-clone.clone_token`.

1. Call `github-clone.clone_token` on the gateway, with no arguments, like any other tool.
2. The answer has `username` (always `x-access-token`), `token`, `expires_at`, and `repositories`: how many the token opens and their names.
3. Clone over HTTPS from `github.com` with that username and the token as the password. Hand them to `git` through a credential helper or `GIT_ASKPASS`. Do not put the token in the URL, where it ends up in logs and in the clone's own settings.
4. Ask again shortly before `expires_at`. The token lasts an hour at most.

The token only reads, whatever else the agent holds. An agent with **Open pull requests** still gets a clone token that cannot push, and opens its pull requests through the gateway's tools, where each one is signed with its name. The token opens the repositories picked for the agent on its **Access** tab and no others. Each one is recorded under **Harness, Activity** like any other borrowed access.

Keep the token in memory. Do not write it to `KOBO_DATA_DIR`, and do not print it. An agent the room builds never gets this tool.

## Taking Slack messages

A connected agent that serves a port can use a Slack app the room makes, set up from the Slack row on the **Access** tab. See "Slack". The room receives Slack's events, checks each one really came from Slack, and hands it to your agent. Slack never calls your agent directly, and your code keeps no connection open to Slack.

What your code does:

1. Serve `POST /slack/events` on the port the agent already serves.
2. Refuse a request unless its `X-Kobo-Relay-Secret` header equals `SLACK_RELAY_SECRET`. Answer 401 and do nothing else with it.
3. Answer `200` with the JSON `{"accepted": true}` at once, and do the work after. The room waits ten seconds for that answer and no longer.
4. Remember each `event_id` for a few minutes and drop one you have seen. When the agent is restarting the room tries again for about four minutes, so the same event can arrive twice.
5. Post to Slack with `SLACK_BOT_TOKEN`, the usual way.

The body is Slack's own event envelope, untouched: the same JSON Slack's Events API sends, with `event_id`, `team_id` and the `event` inside it.

Two more kinds of body arrive at the same address, with the same header, and want the same answer. Tell them apart by `type`:

- `block_actions`. Someone pressed a button or picked from a menu on a message your agent posted. The body is the JSON Slack sends for the click, as Slack sent it: `user`, `channel`, `message`, `actions` and a `trigger_id`.
- `slash_command`. Someone used the agent's slash command, `/` and its name. The body is the command's fields as one JSON object, `command`, `text`, `user_id`, `channel_id`, `trigger_id` and `response_url`, with `type` added by the room. The person sees nothing until your code posts, so post in the channel or answer them alone through `response_url`.

Neither has an `event_id`; drop a repeat by `trigger_id`. Code that reads only `event_callback` bodies can ignore both.

What arrives with the app, beside the names above:

- `SLACK_BOT_TOKEN`. There once the app is installed in a workspace. The agent restarts to pick it up.
- `SLACK_RELAY_SECRET`. The value the room sends in `X-Kobo-Relay-Secret`.
- `SLACK_WAKE_MODE`. The **Wakes on** choice: `mention`, `thread` or `channel`. With `mention` Slack sends only mentions and direct messages. With the other two it sends every message in a channel the agent is in, and telling a thread the agent was mentioned in from the rest of the channel is your code's to do.
- `SLACK_WAKE_EMOJI`. The names in **Also wakes on these emoji**, with commas between them and no colons: `ticket,rotating_light`. Not set at all when the field is empty. What a reaction with one of them does is your code's to decide.
- `SLACK_COMMAND_PUBLIC`. Set to `on` when **Answer its slash command in any public channel** is ticked, and not set at all otherwise. The room hands your code a slash command from wherever it was typed, so answering only where the agent has been added, when this is not set, is your code's to do.
- `SLACK_ICON_URL`. The agent's picture in the room. Pass it as `icon_url` when you post and each message carries the agent's face.

Reactions arrive too, as `reaction_added` and `reaction_removed`, whichever **Wakes on** is picked and whatever emoji are named: every reaction in a channel the agent is in, for your code to match against `SLACK_WAKE_EMOJI` or to use as it likes.

So does `member_joined_channel`, when someone joins a channel the agent is in.

The app's token may also post files (`files:write`), list the workspace's custom emoji (`emoji:read`), read its user groups (`usergroups:read`), post in a public channel the agent was not invited to (`chat:write.public`), pin and unpin messages (`pins:read`, `pins:write`) and keep a channel's bookmarks (`bookmarks:read`, `bookmarks:write`). An app installed before the room asked for these gets them when its owner presses **Update in Slack** on the Slack row; the token keeps its name and the agent restarts once.

All of it reaches the agent with no further pull request. The pull request you merged at connect lets the agent read the app's values when there are some, and every name above is one of them, the three choices included. Making the app, or changing **Wakes on**, the emoji or the slash command choice, is a delivery and a restart the room does a minute or two later.

An agent connected before the room did this gets one pull request the first time, which lets it read the app's values and takes `SLACK_WAKE_MODE`, `SLACK_WAKE_EMOJI` and `SLACK_COMMAND_PUBLIC` out of your deploy contract's own variables. A value you set there yourself hides the room's, so leave them out.

## What its page tells you

Under a connected agent's name, its page keeps three lines in the order they have to come true. Each is one sentence with a dot beside it: green when it holds, amber when it waits on someone, red when something is missing, a ring when there is nothing to report.

- **Deployed.** Whether the agent is running now, looked at once a minute, and why not when it is not.
- **Tools.** Whether your repository carries the room's setup for the tool gateway, and when the gateway last heard from the agent. A line that waits on a pull request links to it. Left out in a room with no tool gateway.
- **Slack.** Where the room-made app stands: none yet, made and waiting to be installed, waiting on a pull request, or installed. Once it is installed, the line says what became of the last message the room handed your agent. Left out for an agent that serves no port.

Three of the Slack sentences point at your code. **It refused the last message from Slack** means your code answered with an error: check that it compares `X-Kobo-Relay-Secret` with `SLACK_RELAY_SECRET` and answers `200`. **It did not answer the last message from Slack in time** means your code took the message and kept the room waiting past its ten seconds: answer `200` first and do the work after. **The last message from Slack did not reach it** means nothing took the message at `/slack/events` for about four minutes: the agent was not running, or your code does not serve that address.

The lines only report. What one asks for is done where it always was, in a pull request your team merges or on the **Access** tab. From your own assistant, **get agent** carries the same lines as `checks`.

## What stays yours

The room does not read or change your code, your image, or how the agent talks to people. Chat, teaching, skills and colleague modes belong to agents the room builds, and a connected agent has none of them. Your repository's own builds are where its build log lives.
