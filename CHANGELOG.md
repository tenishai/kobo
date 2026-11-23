# Changelog

All notable changes to kobo are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/); versions follow
[Semantic Versioning](https://semver.org/).

## [Unreleased]

### Changed

- **Licence: MIT.** The project moves from AGPL-3.0-or-later to MIT, with the agreement of every contributor (tenishai/agora discussion 69). Releases already published keep the licence they shipped under.
- **CI runs on GitHub-hosted runners, behind one `ci` check.** Every job names a hosted image, so a pull request from a fork gets the same checks as one from a branch; the fork guards and the runner variables (`CI_RUNS_ON`, `CI_BUILD_RUNS_ON`) are gone. A last job, `ci`, needs every other job and is the one check the branch rules require, with `dco` (tenishai/agora discussion 71). The repo card no longer pins a workflow file, so a Dependabot actions update passes the card check.
- **No client or private product name at head, and a check that keeps it so.** `scripts/check_names.py` runs in CI and fails on such a name in any tracked file; it holds each name as a hash, so the check itself names nothing. The README no longer says the repositories are private.

## [0.3.81] - 2026-11-20

### Fixed
- **A save restarts the agent once.** In a room that delivers bundles as artifacts, every save restarted the agent twice: the room's own restart within seconds, then the agent repository's build a minute or two later, which built the same image again and deployed it under the commit's tag. Chat refused in both gaps, and the page said **Restarting** a second time after it had said **Ready**. Now a commit that cannot change the image (a persona, skill, rule, knowledge, Edit details, a picture) skips that build, and the room's one restart names the commit it delivered, so **Delivered**, **Ingested** and the **Updating** banner still know when the change is live. A new runtime, an agent whose image still carries its bundle (restarted by its build alone now), and a save after a runtime build that never landed (which retries it, as any save did) build as before. An instant tool upload restarts the agent once too.

## [0.3.80] - 2026-11-20

### Changed
- **The dashboard's pictures load once.** Every card fetched its agent's full picture, a 1024px PNG of about a megabyte, on every page load, and every list of agents read each one from the database. Now each picture is kept with a version and a 160px copy: the cards and the page header ask for the small copy by version, and a browser keeps it until the picture changes, so a reload shows the faces at once. The full picture is still what **Download the portrait** gets.

### Fixed
- **Teaching an agent no longer moves it onto a new runtime.** In a room that delivers bundles as artifacts, a teach note, an upload and a check re-pinned the agent's Dockerfile to the room's runtime in the same commit. Teaching johnny-bravo right after the room's runtime moved rebuilt it on the new runtime and restarted it twice, and chat answered "receiving a delivery" in between. Now only **Update to current**, **Move to the room's runtime** and **set runtime image** move an agent's runtime there, and a save no longer records the room's runtime as the agent's (Slack wiring did too), so the Harness says what the agent really runs.
- **Restarting shows when it happens and clears on its own.** A change that restarts an agent (an upload, a persona edit, a skill, Edit details, a model, runtime or environment change that landed, colleague mode, a teach note, guardrails or knowledge change the room had to commit, **Restart**) says **Restarting** on the agent's page at once, and keeps saying it through a restart that begins minutes later (a model, runtime or environment change is built first). A save that changed nothing, or waits on a pull request, says nothing. The page asks for a fresh reading every few seconds until the agent has restarted and is back, so **Ready** returns without a reload.
- **What you change shows everywhere at once.** A taught note shows in Learnings without a refresh, a new, archived or restored persona's Edit and Move show at once, and Edit details' tags and line show on the main page's cards at once.
- **An older agent moved to the new delivery shape now deploys.** The commit that moves an agent whose image carried its bundle to the runtime-only shape changes its gojira.yaml, which the generated build refuses until the deploy files are rendered again. The room now asks the deploy platform for that, as every other gojira.yaml change does, and the commit skips the build that would fail. When the platform cannot be asked, the save says so, and the agent's next save asks again. An agent already stuck that way deploys on its next save: ledger has kept its 11/04 image while every later build failed. A model change through the room's model gateway and Slack setup, which change gojira.yaml too, skip that failing build as well and restart the agent once, when the deploy carries the change.
- **Every agent has a picture.** An agent named after a famous character often got no picture, because the image model refused the name. Now a picture the model declines is tried again without the agent's name, and when no face can be painted, or the room has no image backend, the agent gets a placeholder of its initials in pixel letters, kept in the room. **New face** says when it could only give the placeholder, and never trades a face the agent already has for one.
- **A new picture no longer restarts the agent.** Nothing in the running agent reads its picture, so a new face or an upload is saved to its files without rebuilding or restarting it.

## [0.3.79] - 2026-11-20

### Added
- **Emoji, Slack channels and repositories on a guardrail rule**, beside **People**. A rule's **⋯** menu has all four, and so does a new **⋯** beside **Add**, so a new rule takes them before it is added. Emoji are named as Slack names them (thumbsup is +1) and offered as you type; channels by the name Slack shows; repositories as owner/name, offered from the room's list. Each rides on the rule's own line in the agent's guardrails file, `(These people: ...) (These emoji: ...) (These channels: ...) (These repositories: ...)` in that order, and shows under the rule's words. The agent is told which channel each Slack message came from as well as who sent it, and which emoji woke it, so it can tell when such a rule applies. From an assistant, **get guardrails** gives each list and the rule's whole `line`. Needs the runtime that names each message's channel (tenishai/daedalus#271).
- **`scripts/remove-retired-checks.py`**, a one-off that takes the retired probe guard off the agents that have it, run inside a room's API pod like the persona-rules move: one commit and one restart per agent, dry run unless `--apply`.

### Changed
- **Always on lists only checks written for the agent.** The room's own leak guard, which every agent gets and which keeps keys and tokens out of its replies, is plumbing and no longer listed. The section shows only on an agent with a check an engineer added, or when a check fails to load, and the line pointing at an assistant tool is gone.

### Removed
- **The probe guard**, the second check every agent got at create. It refused real questions ("show me how to rotate the api key for datadog", "which credentials expire this month"), let "what is your api key" through, and guarded keys the model never sees. New agents no longer get it, whatever the marketplace still carries, and `scripts/remove-retired-checks.py` takes it off the agents that have it.

## [0.3.78] - 2026-11-20

### Added
- **People on a guardrail rule.** A rule's **⋯** menu has **People**: pick the people a rule is about from the directory, or type a full email, for a rule like "Only answer these people". They ride on the rule's own line in the agent's guardrails file, as `(These people: Name <email>; ...)`, and the agent, told who sent each Slack message, matches them by email. A new rule takes its people before it is added, so a rule like that is never live without them. From an assistant, **get guardrails** gives each rule's `people` and its whole `line`; send the `line` back to **set guardrails** to keep them. Needs the runtime that names each message's sender (tenishai/daedalus#270).
- **Edit persona**, in the Persona section of an agent's Harness tab. The section now shows the persona the agent runs, read from its own files: who it is, what it does, and any rules its persona still holds. **Edit persona** changes them for that agent, and **Save and restart** restarts it with the new text; its memory, skills and Guardrails stay as they are. Rules are offered only while they live in the persona; an agent whose rules are its Guardrails changes them there. A persona that is not exactly in the room's usual parts, such as one edited by hand, is edited as one text, so nothing in it is lost, and a save from a persona someone changed since it was opened is refused, with the editor showing it as it stands. When the person may also edit the catalog persona the agent was made from, a line after the save offers to save the new text to that persona too, so agents made from it later start from it; agents already made keep their own. The persona name in the agent's header opens this section instead of the file on GitHub. From an assistant: **get agent persona** reads it and **edit agent persona** rewrites it.

### Changed
- **Guardrails is one line a rule.** Each rule shows its words and a short tag for where it came from, with the whole sentence on hover. **Edit**, **People**, **It broke this** and **Remove** sit behind one **⋯** button that is always on screen, and clicking a rule's words edits them. From twelve rules on, a box above the list finds a rule by its words or its people.
- **Always on replaces Checks the room runs.** Each check says in a line what it does, in the room's own words for the leak guard and the probe guard and from the check's own description otherwise, and whether it is on. The upload form is gone: a check is code, and an engineer adds one from their own assistant with **add agent tool**. Only a room operator can remove the leak guard or the probe guard, and the page offers **Remove** only to someone the room lets remove that check.
- **No rules box when people write a persona**, in a room where guardrails are on. Each agent keeps its rules in its own Guardrails, so writing them into a persona and then changing them again on each agent was the same work twice. The builder asks for a name, who they are and what they do, a draft's rules are not used, and the room no longer asks a persona for at least three rules. Editing a persona that still has rules shows them, so nothing is hidden. Built-in personas keep their rules, which start a new agent's Guardrails. A room with guardrails off is unchanged.

### Fixed
- **The guide's probe guard.** The user guide said the probe guard stops an agent being led off its job. It refuses requests that fish for the agent's own keys, and the guide now says so.
- **Edit details called an agent's team its "Persona source".** People looked there for the agent's persona, which was in no list, because the field is the team the agent works for. It is now **Team**, with a line saying what it is, and the Persona section's fact says Team too.

## [0.3.77] - 2026-11-20

### Added
- **Moving older agents onto guardrails.** `scripts/move-persona-rules.py` moves the persona rules of agents made before guardrails into their lists, once, from inside a room's API pod after the room turns guardrails on. It leaves alone an agent not yet on the room's runtime, because an older runtime ignores the list, and names it so it can be moved first. An agent whose owners already started a list gets its persona's rules after theirs, which stay as they are. Without `--apply` it only says what it would do.

### Fixed
- **A removed agent's name, asked for again right away.** For a minute or so after a destroy, the room is still clearing the old agent away. Connect's scan said nothing was in the way, Connect then answered "That name is already taken in this room", and the form kept Connect off even after the name came free. Now the scan says the name is still being cleared away and to try again in a minute, and Connect and New agent say the same without calling the name taken. A new scan clears an earlier refusal, and a name something outside the room uses is said as that.

## [0.3.76] - 2026-11-20

### Added
- **Every repository, Read only**, a box in an agent's GitHub step on the Access tab. Ticked, the agent may read every repository the room's GitHub app can see, ones added later included, without a list to keep up to date. The set is for Read alone: Review pull requests and Open pull requests still need picked repositories, so the box is off while the agent holds or has asked for either. Once the agent holds Read on picked repositories, ticking it waits for a reviewer, as any addition does. Review shows "Every repository, Read only". From an assistant: **set agent repositories** takes `every_repository`. Needs bab 0.4.26.

### Fixed
- **Adding Slack to a connected agent is one press in every case.** **Create Slack app** went on to Slack's install page unless the room could not open the pull request that hands the app to the agent's repository. Then it answered an error after Slack had already made the app, and the row waited for **Install to Slack**. Now the install goes ahead and the row offers **Open it again** for the pull request. **Open it again** on an app not installed yet also goes straight on to Slack's install page.

## [0.3.75] - 2026-11-19

### Added
- **Guardrails**, a section on the Harness tab. It holds the rules an agent follows on every task: one plain list, one rule per line. Each rule says where it came from: the persona, a skill, a correction, the person who added it, or the agent's repository when someone wrote it there by hand. **Add a rule**, **Edit** and **Remove** change the list. The running agent uses the change from its next task, with no restart, and the line under the list says when the room could only reach the agent's files, so the change waits for its next restart. The list has room for about 4,000 characters, 30 to 40 rules, and the count at the top says how many more fit. A longer list is refused rather than cut, and so is a save made from a list someone else changed since it was opened: the panel then shows the list as it stands. The section says plainly that rules guide and don't lock anything, and points a rule that must never fail at Access or a check. The rules are the agent's `guardrails.md`, which needs daedalus with guardrails (tenishai/daedalus#267). A room turns them on with `guardrails: true` in its deployment config, set with the runtime pin that reads the file. Until then the section shows the list and saves nothing, because an older runtime would follow none of it. From an assistant, **get guardrails** reads the list and **set guardrails** replaces it.
- **It broke this**, on each rule in Guardrails, for anyone who can decide the agent's learnings. The rule counts its breaks and says when the last one was. After the second break it says the rule keeps breaking, and that a rule only guides: to make it hold, take the permission away on the Access tab, or add a check.
- **A new agent's persona rules start its Guardrails** in a room where guardrails are on. The persona's rules move into the agent's list, each marked as from the persona, so a rule lives in one place. A rule the list would refuse, or one past its limit, stays in the persona, so nothing is lost. The persona builder calls the field **Starter guardrails** there. Giving an agent another persona later leaves its list alone. An agent made before guardrails keeps its persona's rules until it is moved over.

### Changed
- **Which tools a permission opens comes from the gateway** (#524, step 5; needs liminal 0.10.0 on the gateway). The room used to keep its own table of the gateway's handlers, and a new tool or handler waited for a room release. Now each handler says on its own card which vendor's permissions open it, which permission opens each tool, and how risky each tool is. The room reads that from the gateway and puts the tools in front of agents that hold the permission, within a few minutes. The vendors' own servers the room connects (Datadog's, Atlassian's, GitHub's) keep their lanes in the room. Two things keep it safe:
  - The room remembers everything the cards have ever opened, so a handler that leaves the gateway is taken away from every agent that held it.
  - A gateway that cannot answer never reads as "no tools": the room keeps the last cards it read, in its database across its own restarts. A handler that leaves the cards keeps its tools for an hour first, so a gateway that restarts without one for a moment restarts no agent.
- **Tooling** is now the **Checks the room runs** part of Guardrails: the same leak guard, probe guard and uploaded checks, under the rules list.
- **New guardrail skill** on the Skills section is now **Write a skill**. A skill loads only when the model asks for it, so it never was a guardrail. Rules the agent must always follow go in Guardrails.

### Upgrading
- Three additive migrations, run at boot: `d7f2a4c6e8b1` (guardrail rules), `e8a3b5c7d9f1` (rule breaks), `f4c6e8a0b2d5` (the gateway's cards and what their lanes have named).
- `guardrails` (deployment config, default off) turns the Guardrails list on. Turn it on in the same change as a runtime pin that carries tenishai/daedalus#267; a runtime from before it follows none of the list.
- Upgrade the tool gateway first: liminal 0.10.0, with handler cards that carry `labels.vendor` and each tool's `permissions` and `risk`. Until the room has read the cards once, it offers and changes only the vendors' own servers it connects, and every approve or revoke says so. A permission revoked in that window keeps a handler's tools until the cards are read.

## [0.3.74] - 2026-11-17

### Added
- **When another app mentions it**, on an agent's Slack row. Alerts, tickets and other agents arrive in Slack as apps, and one that @-mentions an agent has always been answered from what the agent knows, with none of its tools. That stays the start for every agent; its owner can now pick **Work with all its tools**, for an agent whose job is to act on alerts, or **Do not answer**. The row says what does not change: an app's message that does not mention the agent never reaches it, and an agent answers an app at most three times in a row in a thread and thirty times an hour. An agent on its team's own code is handed the choice as `SLACK_BOT_MENTIONS`.
- **Read the channels it is in**, a box on the same row for an agent the room built. Unticked, an agent sees only the conversation it was woken in. Ticked, it can look through the messages and threads of every channel it has been invited to. Inviting it in Slack is what lets it read a channel, so the room keeps no channel list. This was a block written into an agent's files by hand; an agent that already has one keeps the channels it names, and shows the box unticked until someone ticks it once.
- From an assistant: **set slack wake** takes what another app's mention may do, and **set slack reading** is new. An agent's Slack state carries both.

### Fixed
- A persona drafted from a document keeps the document. A team's 160-line runbook with 30 rules came back from **Draft persona** as 36 lines and 10 rules, its channel names, steps and reply template gone. A short description still drafts short; a document now keeps its working detail, up to 120 lines and forty rules, and the draft comes back in about a quarter of the time.

## [0.3.73] - 2026-11-17

### Added
- A persona can be edited. **Edit** sits beside **Move to** and **Archive** at the bottom of the full persona, for its maker, a member of its group, or an operator (a persona shared with everyone: its maker or an operator; a built-in: nobody). It opens the builder with the persona's text in it; who they are, what they do and the rules can change, the name stays. An agent copies its persona when it is made, so an edit reaches agents made from then on. The persona of a team's informational guide is not edited: the room wrote it whole, not in the form's parts.

### Changed
- **Build your own persona** is one form. The two ways in (paste a persona, describe the job) are gone: there are four parts, each saying what goes in it and how much (name, who they are, what they do, rules), and a group. An optional box at the top takes a few sentences, notes or an uploaded file, and **Draft persona** writes all four parts in the shape the built-in personas are written in; whatever was typed by hand stays. A pasted document is no longer kept word for word: it is rewritten into that shape, to edit before saving. When the room cannot write a draft it says so and leaves the form alone, where it filled the form with a stand-in cut from the description.
- Every new or edited persona meets the same floor: at least 300 characters of what they do and at least 3 rules, with **Create persona** off until the form has them and a line beside it saying what is missing. The room also tidies what it is given the same way for everyone (the who-they-are line is one line without "You are", headings in the body are lowered, rules lose their bullets and numbers). The floor holds for a persona sent by an assistant too. Personas made before this are untouched until someone edits them.
- A new persona is filed under its maker's group, not under Everyone. Someone in one group gets it without choosing; someone in several chooses; **Everyone** is still there for a persona meant for the whole room, last in the menu.
- The persona groups in **Create agent** are a row of pills above the list, with **Built-in** pressed first and **Everyone** last, where they were a row below the built-ins that had to be scrolled to. The list shows the pressed group's personas.

### Fixed
- A persona you search for is in the results. A search narrowed the group pills to the ones holding a match and showed no row until that pill was pressed, so a persona filed under Everyone looked as if it had not been saved. A search now lists every match, each under the name of its group.
- A persona made from **Create agent** is on screen and picked when the builder closes, with its group's pill pressed. It was picked, but out of sight until its group was pressed.
- The full persona and its hover card show the bold labels a persona's kinds of request are written with, where they showed the asterisks.
- A name that is already taken says where the persona is ("It is under Everyone in the persona list"), or that it was archived, where it said only that the name existed.

## [0.3.72] - 2026-11-17

### Changed
- Adding Slack to an agent is one press. **Create Slack app** on the **Access** tab makes the app and goes straight on to Slack's install page, where it used to stop on the row for a second press of **Install to Slack**. **Move to the new Slack setup** does the same, unless the room could not confirm the old app was removed, when it stays on the row to say so. **Install to Slack** is still there for an app someone left uninstalled.

### Fixed
- An agent whose Slack app the room made, and nobody has installed yet, keeps running. The room wrote Slack into the agent as soon as the app existed, and an agent built as an image (a room without bundles as artifacts) then restarted into a crash loop until someone pressed **Install to Slack**, because Slack was required and its token had not arrived. Slack is now written as optional everywhere the room wires it: the agent starts without it, and serves Slack from its next start once the token is there. Agents wired before this keep the required form until their Slack is next changed; one that is installed is unaffected.
- Connecting a repository again after its agent was destroyed deploys it. The repository was already wired and built from the first connect, so nothing new was committed and no build started; the room took the old build, whose deploy went to the place the destroy removed, and **Deploy** failed with "rollout failed". A successful build that ended before the agent was created is now run again, once, and **Image build** waits for it.

## [0.3.71] - 2026-11-16

### Changed
- Connecting a repository hands its deploy files to the room and the deploy service, and nobody is asked to merge. The deploy service merges its own setup pull request, and every one after it. The room merges its own too: the wiring, a variable, the first and the last secret, the Slack reference. An agent parked on a setup pull request from before this is picked up within a minute. The repository's own rules still outrank the hand-over: when GitHub refuses a merge (a protected branch that requires a review, say) the pull request stays open, the page links to it and says the room could not merge it, and the agent carries on once someone who can merge there does. A pull request on one of the room's branches that carries anybody else's commits is never merged by the room. The connect form says the hand-over before you connect. This needs a deploy service that knows the hand-over; an older one still leaves its own pull request on a repository with hand-written deploy files for a person.
- Every agent in the room runs as the room's own account, a connected one included. A connected repository that names its own service account no longer keeps it (0.3.69 let it): the room's wiring pull request moves the agent onto `agent-<name>` and replaces a service account file the room did not write, and says so in its first lines, including that nothing granted to the old account follows. The scan says the same before you connect. What the agent reaches is asked for on its **Access** tab, where it is granted and recorded; when the room does not offer something yet, an admin adds it as an integration. The **Access** and **Harness** tabs no longer have a "team's own account" reading. No agent connected so far kept an account of its own, so nothing running changes; the column that recorded one is no longer used and is removed in a later release.

### Fixed
- The permission service is told the account a connected agent really runs as, `agent-<name>`. It was told the bare agent name, an account no connected agent ran as.

## [0.3.70] - 2026-11-16

### Added
- Okta is an integration agents can use. On **Admin**, **Integrations**, pick Okta, give the organization's domain and an API token, and choose whether agents may read, or read and write; the room checks the token with Okta before anything is saved. An agent's Access tab then offers two levels: **Look up people and groups** (find a person or a group, list a person's groups and a group's members, check an app assignment), which stands until revoked, and **Change group membership and assignments** (add a person to a group, assign a person to an app, set the profile attributes the room allows), offered only on a connection made for reading and writing and granted for thirty days. Every agent shares the one token, so the role of the admin who made it is the most any agent can do in Okta. Needs a gateway that carries the `okta` handler and a permission service whose catalogue has the two Okta rows; without them the levels are listed and open nothing.

## [0.3.69] - 2026-11-15

### Changed
- A connected repository keeps the service account it already names. The room used to say at connect that the setup pull request would move the agent onto `agent-<name>`, and then refuse the repository at **Deploy wiring** with "this repository already sets its own service account". It now leaves that account in place: the wiring adds no account and no manifest for one, the **Deploy** step expects the account the repository names, and the **Access** tab reads "Runs as its team's own account" with that name. The room grants such an account nothing and does not list what it reaches on its own; everything granted on the Access tab reaches the agent the same way, whichever account its code runs as. A repository that names none still runs as `agent-<name>`. One case is still refused, with how to settle it: a service account file the room did not write, in a repository whose deploy contract names no account of its own.

## [0.3.68] - 2026-11-15

### Added
- A connected agent has a **Chat** tab. The room asks it through the switchboard, the way one agent asks another, so the tab works once the agent's own code answers: it serves `GET /.well-known/agent-card.json` and `POST /a2a/tasks`, and checks the switchboard's token. The room looks every five minutes and lists an agent that has started answering; what it lists is the agent's name and its owner's **Edit details** line, never what the team's code said about itself. Until then the tab holds one line on what is missing instead of a box that could only fail, and the page's standing lines gain a fourth, **Chat**, with the same sentence (`GET /api/agents/{name}/checks`, and `checks` on `kobo_get_agent`). `POST /api/agents/{name}/chat` and `kobo_chat` no longer refuse a connected agent; in a room with no switchboard they answer 409. Destroy now asks the switchboard to drop a connected agent's entry like any other. Other agents cannot ask a connected agent yet. The guide's "Running your own code" has the contract under **Answering chat**.

### Changed
- A connected agent has **Edit details**, like an agent the room built. It holds one line on what the agent does, written by its owner and shown under its name, and **Destroy this agent** at its bottom. Destroy used to sit at the bottom of the **Secrets** tab for a connected agent. The room keeps the line itself and writes nothing to the team's repository.

### Fixed
- A connected agent's page opens on a tab. It has no Chat, and the page used to open on nothing, so the space under its tab bar was blank until a tab was picked. It now opens on **Access** for a person who may manage the agent and on **Harness** for anyone else.

## [0.3.67] - 2026-11-14

### Added
- A connected agent's page says where it stands, under its name: whether it is deployed, whether its files carry the room's setup for the tool gateway (and when the gateway last heard from it), and whether it takes messages from Slack. One sentence each, no controls. The relay keeps what became of the last message it handed each agent (taken, refused, reached but not answered in time, never arrived) on the app's row, which is new: `slack_relay_apps.last_event_at` and `last_event_outcome` (migration `a7c9e1b3d5f6`). `GET /api/agents/{name}/checks` serves the lines, and `kobo_get_agent` carries them as `checks`. A line says only what the room saw: a cluster or a gateway that could not be asked gets no claim either way.

### Changed
- Connecting a repository that has no deploy files asks its team for one pull request, not two. The room hands the deploy service its share of the setup when it connects the repository (`seed` on the connect call: the contract keys, the service account and storage files, and a note for whoever reviews it), the deploy service writes that into its own setup pull request, and the wiring step then finds the repository already reads the way it would write it and proposes nothing. The agent's Secrets are delivered while that pull request waits, and again after it merges if that did not work, and a Secret that already carries its value is left alone: a second look never takes back a pass the agent holds, and a Secret that could not be read is never taken for a missing one. The setup pull request of a seeded repository is never merged unreviewed. Needs a deploy service that reads `seed`; an older one ignores it, one that refuses it is asked again without it, and in both cases the wiring pull request follows as before. A repository with deploy files of its own is unchanged, because a seed is never laid over what a person wrote.
- Slack asks a connected agent's team for no pull request. Until now making the app was one pull request on the team's repository, and every later change to **Wakes on**, the emoji or the slash command choice was another, each followed by a second from the deploy service. Now the wiring pull request a connect opens names the app's Secret `<agent>-slack` under `secrets_from_optional` (in a room that makes Slack apps, for an agent that serves a port), so the agent starts without it and reads it once there is an app. How Slack wakes the agent (`SLACK_WAKE_MODE`, `SLACK_WAKE_EMOJI`, `SLACK_COMMAND_PUBLIC`) travels in that Secret instead of the repository's own variables. Making the app is a delivery and the restart at install; a changed choice is a delivery and one restart, made once the new value has reached the agent's Secret, and `PUT /api/agents/{name}/slack/wake` says so with `restart_pending`. The Slack row reads **Saved. The agent restarts with it in a minute or two.**
- An agent connected before this keeps working as it is. While its file agrees with the room, a change is a restart and nothing else. The first choice its file would hide (it still sets `SLACK_WAKE_MODE` itself, or does not read the Secret) opens one pull request on `kobo/slack` that takes the three settings out of its variables and names the Secret; that is the last one Slack asks for. `POST /api/agents/{name}/slack/app` and `kobo_create_slack_app` answer `pull_request: null` when none is needed.

### Fixed
- In a room with no secrets service, an agent's Slack Secret is written back whole when the bot token or a wake choice is added to it. A read of it that failed was taken for an empty Secret, so the write could leave the new value as the only one. A failed read now stops the save; only a Secret that is not there yet starts empty.

## [0.3.66] - 2026-11-14

### Changed
- An agent answers its slash command only where it has been added, unless its owner says otherwise. The app's token can post in any public channel, so a command typed anywhere used to be answered there. One box on the Slack row, **Answer its slash command in any public channel**, turns that back on for one agent; it is off for every agent. It rides the wake lane: `command_public` on `PUT /api/agents/{name}/slack/wake` and on `kobo_set_slack_wake`, `SLACK_COMMAND_PUBLIC=on` in the agent's environment (set on the running agent at once, a pull request for a connected agent, absent when off), and a new column `agents.slack_command_public` (migration `f5b7d9e1a3c4`, false for every row). `slack.command_public` is on the agent's detail and on `GET /api/agents/{name}/slack`. The runtime enforces it, so it needs a runtime that reads the variable; posting in a public channel the agent was not invited to, when it is asked to, is unchanged.

## [0.3.65] - 2026-11-14

### Added
- An agent in Slack can offer choices, take a slash command, post without an invite, pin and bookmark (#484). Every manifest the room writes now turns interactivity on, gives the agent a slash command that is its own name (none for a name Slack keeps for itself, such as `remind`), and asks for six more scopes: `commands`, `chat:write.public`, `pins:read`, `pins:write`, `bookmarks:read` and `bookmarks:write`. For an app the room made, clicks and commands come to two new public addresses beside the events one, `POST /slack/relay/{relay_id}/actions` and `POST /slack/relay/{relay_id}/commands`: each checks Slack's signature over the raw form, answers an empty `200`, and hands the agent a JSON body at `/slack/events` under the same `X-Kobo-Relay-Secret` (the click's payload as Slack sent it, `type: block_actions`; the command's fields with `type: slash_command` added). Each reaches the agent once (Slack's `trigger_id`), a payload that is not a click is acknowledged and goes no further, and a signed body that is not a form of plain field names is refused, so a signed event cannot be replayed as a command. A bundle the room composes allows `slack.*` from birth, except a team's informational guide, which never acts. An agent built before this gets the entry when Slack is connected, in the commit that wires it, or in the install callback, which is where **Update in Slack** lands, ahead of the one restart its token brings (that step is tried three times, and the restart does not wait on it past five minutes); a connected agent's repository is never pushed to, and its code is handed the new bodies and the wider token. There is no new setting: an installed app shows **Update** once and one approval brings all six scopes. The tools are the runtime's (`slack.post_message`, `slack.offer_choices`, `slack.pins`, `slack.bookmarks`), so the agent has to be on a runtime that has them; an older one drops the two new bodies and offers no tool. An agent already connected by pasted tokens, and built before this, gets the entry when it is connected again or moved to the new Slack setup.

## [0.3.64] - 2026-11-13

### Added
- A reaction can wake an agent in Slack. The Slack row has one new field under **Wakes on**, **Also wakes on these emoji**: name the emoji (Slack's names, commas between them, at most ten) and a reaction with one of them on a message in a channel the agent is in wakes it about that message, whatever **Wakes on** is. Empty, the default, means no reaction wakes it. The names travel as `SLACK_WAKE_EMOJI` on the lane the wake mode uses: the deploy contract and the live deployment for an agent the room builds, a pull request for a connected one, whose own code decides what a reaction does. `PUT /api/agents/{name}/slack/wake` takes `emoji` beside `wake` (either may be left out), `kobo_set_slack_wake` takes the same, and both Slack views answer `wake_emoji`. The four marks agents leave on messages they answer cannot be named. A room-made app is handed the current manifest when emoji are saved, so one made before the room subscribed to reactions hears them. Needs a runtime that reads `SLACK_WAKE_EMOJI`; an older one ignores it. Migration `e4a6c8d0f2b3` (`agents.slack_wake_emoji`).

## [0.3.63] - 2026-11-13

### Added
- An assistant can do what the Access tab and the Slack row do. Eight new MCP tools: `kobo_list_models` (the ids `kobo_create_agent` and `kobo_set_agent_model` take; there was no way to see them), `kobo_get_agent_access` (grants, what is pending, what can be asked for, and the agent's repository set with a search in place of the whole list), `kobo_request_access` and `kobo_set_agent_repositories` (the page's own routes, so a request waits for a reviewer as one made on the page does), `kobo_get_slack`, `kobo_create_slack_app` and `kobo_update_slack_app` (the room-made app; each answers with the link a person opens, because Slack asks a person), and `kobo_agent_activity` (gateway calls and borrowed credentials). `kobo_create_agent` takes `model`. A refusal that is about the room or the request ("access is off for this room", a repository the room cannot see) is said in the route's words; one about who is asking is still "agent not found". Approving a request, connecting a repository and the build log are not tools yet.
- A room-made Slack app can do more, and an installed one can be brought up to date (#484). Every manifest the room writes now asks for `files:write` (post a file), `emoji:read` (the workspace's custom emoji) and `usergroups:read` (who a user group is), and subscribes to `member_joined_channel`. Slack grants a scope only when a person approves it, and until now nothing could send an installed room-made app back through Slack's approval, so a scope the room added later never reached it. The room now remembers what each install was granted (`slack_relay_apps.scopes`, migration `c8e2a4f6b1d9`); a row that holds less than the room asks for shows **Update**, and **Update in Slack** refreshes the app's manifest and sends its owner through one approval. The agent keeps answering until then and restarts once after. A connected agent's code can use the new abilities at once; the room's own runtime does nothing with them yet.

### Changed
- `kobo_get_agent` answers from the agent page's own route. It rebuilt the page field by field and had fallen behind it: it now carries `health`, `identity`, `archetype`, `colleague_mode`, what the agent runs on (`runtime_image`, `runtime_pinned`, `room_runtime_image`) and the whole Slack block (`wake`, `available`, `relay`, `legacy`). The names it always used (`group`, `owner`, `purpose`) stay.
- `kobo_set_slack_wake` answers with `applied_now` and, for a connected agent, the `pull_request` its team merges. The tool descriptions for the persona, the colleague mode, the model and the runtime say what happens today (the agent restarts with the change) where they still described a rebuild.

### Fixed
- The owner of a connected agent who set a variable from an assistant is told why. `kobo_set_agent_env` answered "agent not found" for an agent the caller owns; the variables of a connected repository are operator-only (#200) and the tool now says so. A stranger still gets "agent not found".
- An operator can move a connected agent to another group. The group is the room's own filing and touches no file in the agent's repository, and the move was refused with the sentence for edits the room does not make to a team's repository.
- Pasted Slack tokens are refused for an app the room made. `POST /api/agents/{name}/slack/connect` and `kobo_wire_slack` would have written a second app's credentials over the one the room still relays events for. `kobo_wire_slack` also points a connected agent at the tools that fit it.
- A room that names its own MCP signing key variable can sign in. `mcp.jwt_key_env` was read when a token was issued and ignored when it was checked, so such a room refused every token it issued. Rooms on the default name were never affected.
- Three MCP tools no longer hold up the room while they wait. `kobo_wire_slack`, `kobo_set_runtime_image` and the name suggestions in `kobo_create_agent` ran their route on the event loop, where a slow answer from Slack or GitHub stalls every other request on that replica.

## [0.3.62] - 2026-11-13

### Fixed
- A connected repository's build no longer stays red after its team merges a pull request from the room. Every change the room proposes to a connected agent edits its deploy contract, and the repository's own build refuses to deploy until the deploy platform regenerates the files rendered from it. Nothing asked the platform to: the platform regenerates on a provision call, never on a push, and the room made that call only inside the connect pipeline. A secrets wiring or a variables pull request merged days later left the team with a failing build and a message telling them to press a button on a page they cannot open. The room now compares the contract on the default branch with the one the deploy files were rendered from, every five minutes, and asks for the regeneration once per version; the platform's setup pull request follows, as the guide already said it would.
- A connected agent in a room with the model gateway gets its key. The wire step took the cloud model variables out, as it should when models go through the gateway, and delivered no key and no address in their place, while the pull request's text said the Secret was there. The key is now minted and delivered before the request opens, `<agent>-models` is in the contract, and `KOBO_MODELS_URL` carries the address.
- The owner of a connected agent sees and saves its secrets. The variables of a connected repository are operator-only (#200), and the Secrets tab took that one refusal for a failed load: the owner saw the refusal where the whole table should be and could not add a secret the room would have accepted. The tab now keeps the secrets, drops **+ Variable**, and says the variables live in the agent's own repository. `kobo_list_agent_secrets` answered "agent not found" for the same reason; it now lists the secrets and answers `env_readable: false`.
- A connected agent's picture can be changed. The picture of a connected agent lives in the room and nowhere in its repository, and both picture routes still refused with "this agent's repository belongs to its team". They now store the picture in the room and never touch the repository.
- A connected agent's Slack row no longer offers a button that fails. The room's Slack app is wired by commits to the agent's own repository, and the room never makes those to a connected one, so **Create Slack app** was refused with "this agent's repository belongs to its team". A connected agent that serves no port now reads as bringing its own Slack app, with its tokens under Secrets; one that serves a port gets the room's app through a pull request (below).
- The owner of a connected agent is sent to its repository's own builds. The room keeps a connected repository's build log to operators (#200), and the sheet told the agent's own owner that only an owner could read it. It now links to the repository's builds page, where GitHub checks who may read them.
- A connected team that brings its own Slack app can save its tokens. The Slack row told the team to put the tokens under Secrets, and the Secrets tab refused `SLACK_BOT_TOKEN`, `SLACK_APP_TOKEN` and `SLACK_SIGNING_SECRET` with "the room sets this one", for an agent the room sets none of them on. Those three names are the team's on a connected agent with no room-made Slack app, as secrets and in a pasted file; the secrets read lists them under `released`. They stay the room's on every agent it builds and on a connected agent once the room has made it an app.
- A connected repository's variables cannot be read by saving them (#200). The variables write answers with every value the file holds and carried none of the read's rule, so an owner who was refused the read got the values back from a save that changed nothing. The write is operator-only on the same terms.

### Added
- A connected agent gets what an agent the room builds gets (#483). The wiring pull request at connect now mounts storage that outlives a restart (`KOBO_DATA_DIR`, 1 GiB, its own file `k8s/base/kobo-storage.yaml`; a mount the team already declared is kept and nothing is added beside it) and, in a room with a tool gateway, the agent's pass (`CABAL_COLLEAGUE_TOKEN` from the Secret `<agent>-cabal`) with the two addresses its code needs, `KOBO_SWITCHBOARD_URL` and `KOBO_GATEWAY_URL`. The agent's code trades the pass for a 15-minute gateway token and reaches exactly what its **Access** tab shows as held. A room with no gateway wires the storage and nothing else. The guide has the contract as a new section, **Running your own code**, and `docs/examples/gateway_token.py` is a client a team can copy. An agent connected before this is offered the same wiring as one pull request, within a few minutes of its first approved permission; the room never merges that one, and the **Access** tab says the agent is waiting on it and links to it. The four `KOBO_*` names are reserved on the Secrets tab.
- The room makes a Slack app for a connected agent (#484). A connected agent that serves a port gets **Create Slack app** and **Install to Slack** like any other: the room makes the app, receives Slack's events and hands each one to the agent at `POST /slack/events` with `X-Kobo-Relay-Secret`, so the team runs no socket and creates nothing in Slack. The app's values are delivered to `<agent>-slack` at once; the reference to that Secret and `SLACK_WAKE_MODE` reach the agent as one pull request on `kobo/slack`, which the room never merges, and the Slack row says the agent is waiting on it and links to it. Changing **Wakes on** is a pull request on the same branch and sets nothing on the running agent. A request closed without a merge is opened again from the row, with no second app. A connected agent that serves no port keeps its own app, and so may any team that prefers to; one with Slack tokens of its own under Secrets is asked to remove them first. The guide's **Running your own code** has what the agent's code does with an event.
- A connected agent can `git clone` what it was granted (#483). Read on GitHub now also opens the gateway's `github-clone` handler for an agent that runs its team's own code: one tool, `clone_token`, answers with a GitHub token to use as the HTTPS password. The token is leased for Read alone, so it cannot push even when the agent holds **Open pull requests**; it opens the agent's own repository set and lasts an hour at most. An agent the room builds never gets the handler, and it leaves with the permission. The guide's "Running your own code" says how to use it. Needs a gateway that serves `github-clone`; on one that does not, the grant names a handler that is not there and nothing changes.
- A room-made Slack app hears reactions. `reaction_added` and `reaction_removed` are in every manifest the room writes, whichever wake mode; the `reactions:read` scope was already there, so nothing is reinstalled, and an existing app takes the events the next time its manifest is applied. The room's own runtime still drops them; a runtime that reads them gets them through the relay like any other event.

## [0.3.61] - 2026-11-13

### Fixed
- A new agent's Read on its own repository lands (#476). The room asks for that one read as itself and approves it as itself, and the permission service took an approval from an admin or security reviewer only, so the default never landed anywhere: the Access step went green on Retry with a request left waiting in Review and no read held. With the permission service 0.4.23 the room may approve that one request (its own, Read only, on the agent's own repository set) and nothing else. Retry now picks up a request the room left waiting instead of returning early or asking twice. Against an older permission service the step says the read **waits for a reviewer**, who approves it under Admin, Review. An agent born before this release with a request already waiting is approved the same way; nothing sweeps them.
- Destroy takes everything the agent held out of the permission service (#476). It removed the agent's `agents` row only, and only when the person destroying was a reviewer; the agent's repository set, its grants on other connections and its pending requests stayed behind, where a later agent of the same name would have inherited them. Once the agent is out of the topology the room now tells the permission service 0.4.23 the agent is gone, which revokes its grants, ends the access it has in hand, withdraws its requests and removes its set. An older permission service gets the old removal, and what was missed is named on the destroy.
- A connected agent's build no longer fails once per wiring (#475). After the room merged its wiring pull request it read the platform's status at once, saw the stale `active`, and watched a build that must fail until a person merges the setup pull request. The wire step now waits for the platform to notice the change, and the build step waits on the setup pull request, saying so, instead of failing.
- The same bundle packs to the same bytes whatever second it is. The bundle tarball's gzip header carried the clock, so an unchanged bundle had a different digest each second, against what the packer promised. Nothing skipped work on that digest; it failed a test now and then.

### Added
- The Access tab greys a permission no tool uses yet (#474). A grant can only open a tool that exists, and the catalogue offers permissions with none behind them: a person could ask, see it approved, and the agent could do nothing more. The picker now carries `tool` on every permission (`gateway_lane.opens_tool`); a row with no tool is greyed, says **no tool uses this yet**, cannot be ticked and is not counted as something to ask for, and one already held or waiting says so beside its state. Google Cloud's 23 read permissions are marked as tooled, which needs the tool gateway's handler package that serves all of them (it served 12); Datadog's nine with tools in the vendor's default set are, and monitors write, SLOs, Synthetics and Security are not. A vendor with no lane (Azure) and any permission the catalogue gains later open nothing until the room learns the tool, so a new one cannot arrive offered and useless unnoticed.

## [0.3.60] - 2026-11-13

### Added
- The AWS gateway lane: any AWS permission opens the gateway's `aws` handler and allows its tools, so an agent granted a permission on a connected account can use it. An AWS permission used to be grantable and approvable with no tool behind it: the agent held CloudTrail read on an account and had nothing to call. The handler (in the gateway's own handler package) leases the account's session for each call, and AWS narrows that session to the permissions held, so one lane serves every AWS permission in the catalogue and every account, including permissions added later. A cloud the Access tab can offer and no lane serves is pinned by a test (Azure today), so the next one cannot arrive granted and without tools unnoticed.

### Changed
- Admin, Models opens at once and reads as one list. The page used to wait on one request that asked every connected cloud for its models, one cloud after another, before it drew anything: three to eight seconds on most visits. It now opens on the room's own ticks and whatever list of each cloud the room already holds, asks each cloud that is owed a fresh list on its own and side by side, and says under the table how long ago each was read; a slow cloud holds up only its own column. A tick no longer waits on any cloud.
- The Models page is laid out by model, not by cloud. **Offered in New agent** is first and always in view, across every cloud. Under it, every model the clouds list is folded by who makes it, with a count of models and of offered ones; a model is one row with a box under each cloud that has it, so which clouds carry a model shows at a glance. Two clouds' entries share a row only when they are surely the same model (same maker, same name once each cloud's own prefix, date and version suffix are taken off); anything less certain stays two rows. Search stays, and a cloud filter sits beside it.
- `GET /api/admin/models` takes `listing=held` (answer at once from what the room holds; a provider owed a list is `pending`) and `source=<row>` (one provider alone); every model carries the cloud's own `name` beside the room's `label`. The route reads the ticks and lets its database session go before any cloud is asked.
- A lane added in a release reaches the agents that already hold its vendor. The five-minute heal pass already set back a gateway grant that was short of what the agent's permissions owe; for the agents it sets back it now brings the bundle's allow list in step in the same pass (one commit, one restart, only when the list has to change), and tries again on the next pass when the agent's repository could not be written. Before, the allow list moved only when a grant changed or an admin pressed Sync agents, so an agent granted before the lane existed stayed without tools.

## [0.3.59] - 2026-11-12

### Changed
- An agent's owner picks any repository for it, and what widens a set the agent already holds a level on goes to Review. Saving repositories on the Access tab used to be refused for anyone who is not a reviewer ("is not on the admin or security team"), which left owners unable to ask at all. With the permission service 0.4.22: a set with no level on it, held or asked for, is the owner's to change freely; once the agent holds a level or has one waiting in Review, a repository the owner unticks leaves on save and a repository added waits as one request in Admin, Review, shown with what would be added, the set as it stands and the levels it would be reached with. The Access tab marks the waiting repositories; saving without them takes the request back. A new agent holds Read on its own repository from birth, so in practice every added repository is reviewed. An older permission service keeps refusing as before.
- A changed repository set applies to the agent's next call. The permission service 0.4.22 ends the hour of access in hand when the set under it changes, and the audit row for the save says so; an older one lets the old set last out its hour.

## [0.3.58] - 2026-11-09

### Fixed
- The Google Cloud project picker asks for the list once per parent, and again only on Try again. The effect that started the listing also watched the request's own state, so every change of that request re-ran it: the dialog flashed and the permission service was asked dozens of times a minute (#466).
- The picker's search box is one input high. Its shared search class carries a flex basis meant for a row; in the picker's column that basis became the box's height (#464).

## [0.3.57] - 2026-11-09

### Changed
- A Google Cloud folder or organization connects nothing by itself. Connecting one used to register every project under it the identity could read, and only the projects directly under it: six rows out of a hundred-project tree, with no say in which. Connect now saves the parent and opens a picker of every project under it, the whole tree down (the permission service 0.4.18 lists it), with the folder each sits in and whether the identity can read it; the admin searches, ticks, and connects the picked ones. **Check projects** opens the same list later. A picked project is read once with the service's identity and held under its own project id, which is the id the gateway leases by.
- A Google Cloud project may be connected for its Vertex AI models alone (**Models only** on the project sheet, the AWS Bedrock shape): it is held with nothing an agent could be granted, so no Access tab or picker offers it, while the Models pane still lists it.

### Added
- The Google Cloud gateway lane: any `gcp_*_read` permission opens the gateway's `gcp` handler and allows its tools, so an agent granted a read permission on a connected project reaches it through the gateway.

## [0.3.56] - 2026-11-08

### Added
- An agent's Jira work is signed. Jira names the account behind the credential as the author of everything it writes, and the room connects Atlassian with one shared account, so every issue and comment an agent made would read as that account. Issues and comments an agent makes now go through the gateway's `jira-write` tools: the summary carries the agent's name and the body opens with a signature that links to the agent's page in the room. The vendor's own issue-creating and commenting tools are closed to agents; editing fields and transitions stay under Write issues.

## [0.3.55] - 2026-11-06

### Added
- An agent's GitHub work is signed. GitHub names the room's app as the author of everything an installation token writes, so with several reviewer agents every review read as the room's app. Comments, reviews and pull requests an agent makes now open with its name and its portrait, served from the agent's own repository to anyone who can see it, through the gateway's `github-write` tools; the vendor's own comment, review and pull-request-creating tools are closed to agents, and each agent is told to name the branches it creates after itself. Review pull requests and Open pull requests open the signing tools alongside the vendor's branch and commit tools.
- The room fills in a gateway target an applied connection owes and the gateway lacks, every five minutes with the grant heal pass and on **Sync** in Admin, Access. Connect was the only writer, so GitHub connected on 2026-10-31 never got the `github-mcp-write` target that arrived the next day, and an agent granted Review pull requests saw only read tools (2026-11-06). Nothing the gateway already serves is overwritten.

### Fixed
- Access no longer fails when Cloud Storage has not yet seen a brand-new identity. The replica bucket binding ran eight seconds after the service account was created and Google answered "does not exist"; an identity recreated under a name destroyed days earlier is seen later still. The binding now waits that out, about two minutes at most, and only for that answer.
- Destroy takes back the replica bucket's two bindings before deleting the identity. Deleted first, the bindings lingered in the bucket policy as `deleted:` members nothing could match later (two left behind by one agent's earlier lives, 2026-11-06).

## [0.3.54] - 2026-11-04

### Added
- An agent's Slack app may look people up (`users:read`, `users:read.email`). A thread read names who wrote each message and who reacted instead of a bare Slack id, so an agent checking an approval can say whose thumbs-up it was, and a search can find someone a message names only by id. An existing agent's app takes the scopes on its next **Reconnect the Slack app**.

### Fixed
- An agent recreated under the name of one destroyed in the last 30 days gets its cloud identity back. The deleted identity is still being removed and reads as a permission refusal; the room now treats that as absent and creates it, and says what to do when the old name cannot be reused yet (kobo#448).
- The room sets back an agent's gateway grant that something else rewrote. The gateway re-applies its own grants file on every restart, and a stanza there for a room-managed agent silently replaced what the agent's permissions had opened (ledger lost its GitHub tools for a day, 2026-11-04). Every five minutes the worker compares each ready agent's grant with what its permissions owe and writes it back when they differ, logging a warning. An agent that holds no permission in the room is left alone.

### Removed
- The room's own Jira read lane for agents (`/broker/agents/{name}/jira/issues/{key}` and `/jira/search`), with the agent-token check only it still used. It signed in with a stored API token Jira had stopped accepting, so an agent's first ticket read failed before it fell back to its Atlassian tools. Agents read Jira through the Atlassian connection. The `jira_*` config keys are now ignored with a warning, and the next access sync withdraws any `jira.*` entry left in an agent's allow list. `/broker` is no longer exempt from the sign-in gate, since nothing is served there.

## [0.3.53] - 2026-11-03

### Fixed
- A new agent's first deployment mounts its bundle. Compose writes a seed gojira.yaml (`bundle_image`, the runtime's env, the optional Secret references) that the deploy platform reads before its first render; without it the first pod ran a runtime with no /bundle and crashlooped until the wiring commit's second setup PR landed (dev, 2026-11-03).

## [0.3.52] - 2026-11-03

### Fixed
- `bundle_registry` may be set in org.yaml. The loader's allowlist did not know the key 0.3.49 added, so an org.yaml naming it was rejected whole and the room came up unwired: no catalogs, no operators (dev, 2026-11-03). The key is on the list, and an unrecognized key is now also an error line in the log, not only a health-route detail.

## [0.3.51] - 2026-11-03

### Changed
- The agent's bundle is mounted, not pulled (tenishai/kobo#434). The room publishes each bundle as a plain OCI image and the pod mounts it read-only at /bundle as a Kubernetes image volume, which the node pulls on every start with its own image-pull credentials, exactly as it pulls the runtime image. The pod holds no registry credential and no agent service account needs a registry role: the access step no longer grants one (the grant 0.3.50 added is withdrawn). Needs the deploy platform's `bundle_image` key (tenishai/gojira#132).

## [0.3.50] - 2026-11-03

## [0.3.49] - 2026-11-03

### Changed
- Nothing but the runtime builds (tenishai/kobo#434). With `bundle_registry` set in the org config, a change the room makes to an agent (persona, skills, knowledge, intake, tools, colleague mode, access, model, picture, details, Slack) is a commit for the record, a publish of the agent's bundle to the registry and a restart, about thirty seconds, instead of a three-minute image build. New agents are born that way: a one-line Dockerfile, the runtime's environment and the optional Slack and env Secret references in gojira.yaml, and Slack wired dormant, so Install to Slack is a Secret and a restart. An existing agent converts on its first change once it runs the room's current runtime; that one change still builds. Needs a runtime that pulls bundles (tenishai/daedalus bundle-at-start) and a Gojira that renders optional Secret references and reuses builds (tenishai/gojira no-rebuild-for-settings). Empty `bundle_registry` keeps today's behaviour.

## [0.3.48] - 2026-11-03

### Fixed
- The Atlassian sheet refuses a token its MCP server accepts but cannot use. Atlassian opens a session for any valid API token and only refuses the first tool call when the token lacks the scopes the MCP server reads (a classic token, or one made under the Jira app instead of the Atlassian MCP app); the room's check now makes that call and says where the right token is made, instead of the agent failing later in chat.

### Changed
- The Access tab's Slack row no longer reads Live the moment the app is installed. Wiring the app is a commit that rebuilds the agent, and it answers in Slack only once that build has rolled, about three minutes; the room now remembers the image the agent ran when the app was wired and the row reads Rebuilding while the deployment still runs it, then Restarting, then Live. Migration `d1e3f5a7b9c2` adds two nullable columns.

## [0.3.47] - 2026-11-03

### Added
- The agent's face on every Slack message. The room serves each agent's picture at a public address under the Slack relay (`/slack/relay/<relay id>/face.png`), hands it to the runtime as `SLACK_ICON_URL`, and asks for Slack's `chat:write.customize` scope in every app it makes, so the runtime posts each message with that picture as its icon. An owner changes the picture in kobo and Slack follows on the next message; an agent connected before this takes the scope on its next Reconnect.

## [0.3.46] - 2026-11-03

### Added
- The Atlassian sheet takes a service account's API key, the credential that belongs to no person: an admin makes it under Directory, Service accounts with its scopes fixed at creation, and the room checks it with Atlassian's MCP server as the Bearer the gateway sends before it saves. A user's email and API token stays as the second choice on the sheet.

### Added
- Agents can write to Jira. An Atlassian connection added with "Agents may read and write" offers the permission service's new Write issues level (`atlassian_jira_issues_write`, bab's mutate tier); an agent granted it earns the gateway scope that opens the four Jira writing tools (create, edit, comment, transition) and nothing else. Confluence's writing tools and Atlassian's generic executors stay behind scopes no grant opens. A connection added for reading keeps the read tier as its ceiling.

### Fixed
- The room's Jira lane no longer reports a rejected API token as "no such issue". Jira treats a credential it cannot verify as an anonymous reader, so every issue read came back 404 and every search empty; the lane now asks the tracker who the credential is before it calls an issue missing, and answers "the tracker rejected the room's credential" (a 502 the agent passes on, never a 404 the runtime reads as "not granted").

## [0.3.45] - 2026-11-02

### Changed
- The Atlassian sheet's usual address is Atlassian's v2 MCP endpoint, the one that serves the Jira and Confluence tools to an API token (v1 serves only the four Teamwork Graph tools). The target keeps v2's creating, editing, transitioning and deleting tools behind a scope no lane grants, so an agent reads with the room's credential and never writes with it.
- The access sync withdraws the token broker's `github.<tool>` allow entries an older agent still lists (they answered HTTP 405 since the broker went), so the agent is offered the gateway's GitHub tools instead of dead ones.

## [0.3.44] - 2026-11-02

### Added
- Atlassian connects with an account's email and its API token, the pair its MCP server takes as Basic auth (a plain token was sent as a Bearer, which Atlassian refuses for a user's token). The room checks the pair with Atlassian before saving, the gateway target gets its own session per agent and opens only to an agent granted Atlassian on its Access tab, and an address pasted with the trailing `/mcp` from Atlassian's docs is taken as is.

## [0.3.43] - 2026-11-02

### Added
- **Bedrock models only**, a third choice on the AWS sheet: a role that holds the Bedrock permissions and nothing else (its own Terraform, trust policy and Bedrock policy; no template). The row reads "Models only: Bedrock, <region>", connects in the permission service with an empty ceiling, and the Access picker leaves out any account with nothing to grant, so no agent is offered an account it could not use.
- A new agent reads its own repository on GitHub: with the permission service on and GitHub connected, the Access step (was Model access) writes the agent's repository set as its own repository and approves Read on it, and the wiring step puts the lane's allow entry into the bundle in the same commit; the owner adds repositories or raises the level on the Access tab as before.

### Fixed
- A Slack event that meets an agent mid-restart (the Install to Slack rebuild, a model or Wakes on change) is kept and sent again for about four minutes instead of being dropped at the first 404 or refused connection; the Slack row reads Restarting and says a mention sent now is answered once the agent is back.
- An agent created with the room's model gateway on no longer carries `VERTEXAI_PROJECT` and `VERTEXAI_LOCATION` (its model access is the gateway key and the model block in its bundle); a reprovision or a model change removes the two from an agent made before the gateway, and the deploy check no longer looks for them on a gateway agent.
- The Vertex AI default location is `global` in the room's own config too (`vertex_location`, was us-east5), matching the Connect Google Cloud sheet.

## [0.3.42] - 2026-11-02

### Fixed
- A self-hosted model that refuses the row's request options (a Mistral tokenizer refuses `chat_template_kwargs`) is asked once more without them and served without them, instead of failing the row's probe.
- Reload org config builds the model gateway client and fills the gateway when the org config brings the gateway URL, so no restart stands between connecting a cloud and its models being offered.

## [0.3.41] - 2026-11-02

### Fixed
- A Google Cloud project connected on its own now shows on the Integrations table (it was written as an account with no parent, and the table listed only parents and their children).

### Added

- **The Models pane and the tagged picker (kobo#405).** An admin chooses which models New agent
  offers. The new table `room_models` (migration `a7c9e1f3b5d7`) holds every model the room knows,
  offered only while `enabled`; at boot, when it is empty, the org's `models:` list seeds it enabled
  with its price names and tiers, and so do the answering models of any self-hosted row already
  connected, so nothing disappears on the roll. **Models**, in the admin rail after Integrations
  (`GET`/`PUT /api/admin/models`), is one section per connected provider with its tag, its name and
  the chat models the permission service lists for it (`GET /integrations/{id}/models`, Vertex AI
  and Bedrock, cached ten minutes) or a self-hosted row's answering models, each with a tick; a tick
  upserts the row, writes one audit row (`models.offered`, `models.withdrawn`) and runs the gateway
  filler, which registers enabled rows only. The catalogue, the gateway filler, a new agent's key
  and Change model all read the enabled set, and every model carries `provider_tag` (GCP, AWS,
  Azure, Self-hosted). The pickers drop the per-cloud groups: one list, tiers in order within a
  family, the tag as a small grey chip after the name. A self-hosted row connected after the seed
  waits for its tick like a cloud's models. Azure shows as not connected.
- **Google Cloud connects as an organization, granted once (kobo#405).** The Connect Google
  Cloud sheet opens on **An organization** (`organizations/<number>`; every project the room's
  identity can read connects by itself, as before), with **A project** as the other scope; the
  grant block reads "On the organization, once". Both scopes carry **Include this project's
  Vertex AI models in the room** with a **Vertex AI project** field (on a project sheet it is the
  project itself unless another is named) and a location, `global` unless said otherwise; ticked,
  the room lists that project's models at the location through the permission service before
  saving, refuses the connect when it cannot, and the row's spec carries
  `models = {provider: "vertex_ai", project, location}` (for an organization row the named
  project, not the row's). The row reads "Models: Vertex AI, <project>, <location>", and the
  project's own row under the organization says the same. The grant page includes the models pack
  on the organization when the box is ticked. The identity shown is whatever the permission
  service reports.

### Changed
- The Connect Google Cloud sheet no longer offers **Service account to act as**; the API still
  accepts `service_account` for rows that carry it. The default Vertex AI location is `global`
  (was us-east5).

## [0.3.40] - 2026-11-01

### Added

- **Bedrock models through the AWS connection (kobo#390).** The Connect AWS sheet now offers
  **One account** beside **An organization**: a role in one account the room assumes directly,
  named by the person (a second direct account is a second row). An account's sheet (and a member
  account's role, through the API) carries **Use this account's Bedrock models in the room** with a
  region (us-east-1 unless said otherwise); ticked, the row's spec carries
  `models = {provider: "bedrock", region, role_arn}` and the role's snippets gain the Bedrock
  permissions block (an `aws_iam_role_policy` under the Terraform module, one JSON policy for
  CloudFormation and by hand; the trust policy is unchanged). The org's `models:` list names the
  Bedrock entries (`bedrock/<model id or inference profile id>`); the model gateway serves each
  through every ticked account by assuming its role with the room's own identity token
  (`aws_role_name`, `aws_web_identity_token`, `aws_session_name: kobo-gateway`), so nothing is
  stored. The catalogue lists them under **AWS** (naming the accounts when several serve), with
  "Ask a kobo admin to connect AWS with Bedrock" while none does; the Services row counts the
  accounts as providers. The permission service has no Bedrock read yet, so the row says the models
  are registered and not yet checked with AWS; an organization's hub never carries the tick.

### Fixed
- Check accounts no longer times out on an organization of a few dozen accounts: the room waits up to two minutes for the permission service to probe them (its other calls keep the fifteen-second wait).

## [0.3.39] - 2026-11-01

### Added

- **Self-hosted models (kobo#390).** A fourth kind under Admin, Integrations, and the one that is
  only models: models you run yourself, on your own machines or clusters, at an address you give
  the room (vendor `self-hosted`, auth kind `self_hosted_endpoint`, `kobo_api.self_hosted`). One
  row per endpoint, named by the person; the form takes the base address (the room appends `/v1`
  when it is missing), an optional key, the models it serves one per line (`name` or
  `name = label`) and one optional block of request options (YAML or JSON) sent with every call to
  every model of the row, such as `chat_template_kwargs: {enable_thinking: false}`. On connect the
  room itself lists the models at the address and asks each named model for a short answer
  (`max_tokens: 8`, with the options); a model that fails is marked on the row with the server's
  sentence ("2 of 3 models answer; `old-model` failed: ...") and is not registered, and the row
  connects as long as one answers. **Check models** on the row runs the probe again. The key is
  held in the room's own Secret (`self-hosted-<row id>`), never echoed. With the model gateway on,
  each answering model is one deployment (`<row slug>-<model slug>`, LiteLLM's `openai/` route at
  the endpoint, the options as `extra_body`), the catalogue lists them under the row's name and the
  pickers group them there; the org's `models:` list is untouched. Nothing of this touches the
  permission service or the tool gateway: nothing is leased.

### Changed

- **The Create agent dialog in the room's look.** The dialog now wears the room's restyle: a title with one sentence under it, the persona list as hairline rows with the name and its line in ink under small-caps group labels and **Build your own persona** as a button on the header row, Name, Group and Model each the whole width with the model list's reasons beside the names and the no-model sentence under the list, Cancel and Create agent on the right of the footer. Focus lands on the persona search and Enter in Name creates. Phone width gets 16px gutters.

## [0.3.38] - 2026-11-01

## [0.3.37] - 2026-11-01

### Added

- **The room's model gateway (kobo#390).** Models now come from the clouds the room connects. With
  the new org key `models_gateway_url` set (the LiteLLM proxy the install deploys; its master key is
  read from the Secret the machine-local `models_gateway_master_key_secret` names, never from org
  config), a Google Cloud project connected with the Vertex AI box ticked becomes a model provider:
  the room registers every `vertex_ai/` entry of the org's `models:` list on the gateway as one
  deployment per project (`model_name` = the label as a slug, `model_info.id` = `<row id>:<entry
  id>`), at connect, disconnect and boot, adding and deleting only the difference; a gateway that
  refuses leaves the row connected with the refusal on its line. `GET /api/catalog/models` then
  carries `provider`, `provider_label`, `gateway_model`, `available` and `reason` for each model,
  and `default` is the first available one. New agent and Change model group the list by cloud and
  show a model nobody serves in grey with its reason (its cloud is not connected, or the model
  gateway is not reachable); with no model available New agent is off with one sentence pointing
  at Admin, Integrations, the create route refuses with 409 and the same sentence, and Change model
  refuses an unavailable model the same way. A new agent's bundle thinks through the gateway
  (`model: {id: openai/<gateway_model>, api_base: <gateway>/v1, api_key_env: MODELS_GATEWAY_KEY}`),
  the room mints it a key of its own (alias `agent:<name>`) and delivers it through the same lane
  the Slack values use as the Secret `<agent>-models`, the deploy contract names that Secret, the
  per-agent Vertex grant is skipped (the gateway's identity holds the role), and destroy revokes the
  key. Agents made before the key was set are untouched until an owner presses Change model, which
  moves them onto the gateway. The Services row **Model gateway** (a stub on a retired key) is now
  **Models**: off, Not reachable, or "N providers connected, M models available, default X". With
  `models_gateway_url` empty nothing changes.

- **Move an agent from the old Slack setup to the room-made app (kobo#304, phase 3).** On a
  legacy agent (one whose Slack app came from the Slack tab, with an app-level token), the
  Slack row on the Access tab reads **Connected the old way** with its app id and offers one
  button, **Move to the new Slack setup**. `POST /api/agents/{name}/slack/move`
  (owner-or-operator, behind `slack_relay`) deletes the old app with the room's config token,
  as destroy does, then runs Create's path: the relay id is minted, the app is created from the
  manifest with the agent's current Wakes on, the five create-time values go to the agent's
  Slack secret in the same write that takes `SLACK_APP_TOKEN` and the old `SLACK_BOT_TOKEN` out
  (so the runtime takes the HTTP way in at its next start), the open transport block and the
  wiring land in one commit, and the saved channels stay and still narrow it. The answer is
  Create's (`app_id`, `install_url`) plus `old_app_removed`; a failure deleting the old app is
  reported as `old_app_removed: false` and does not stop the move, since the old app may already
  be gone. The owner presses Install to Slack as usual. Audit action `slack.move`; 409 for an
  agent that is not legacy. Per the owner ruling of 2026-11-01 the dev agents still on Socket
  Mode may be moved freely. The Slack tab already hides itself for a moved agent; its full
  removal, along with the Slack tab's guide text, is a later cleanup once dev has moved.

- **Connect Google Cloud (kobo#390).** Admin > Integrations > Add integration > Google Cloud is
  its own sheet, modelled on the AWS one: pick **A project** (the common case) or **A folder or
  organization**, and the page shows the room's identity (the permission service's service
  account, email and id) and the roles to grant it three ways, `gcloud` commands, Terraform
  (`google_project_iam_member` and the folder and organization forms) or the plain list for the
  console, rendered from a vendored pack file (`packs-gcp.json`: `inventory`, every viewer role the
  catalogue's gcp read permissions name; `models`, `roles/aiplatform.user`). No key is made
  anywhere. Connect asks the permission service to read the project as that identity (`POST
  /probe/gcp`) and saves nothing until Google Cloud agrees; the row is held there with every gcp
  read permission as the ceiling and nothing goes to the gateway. A folder or organization is the
  hub shape: every project under it the identity can read connects by itself (`POST
  /integrations/{id}/discover`), the rest read **Not granted yet** until **Check projects** finds
  them granted. Tick **Use this project's Vertex AI models in the room** (location `us-east5` by
  default, `global` allowed) and the probe also lists Vertex AI models there; the row's spec then
  carries `models = {provider: vertex_ai, location}` and its line reads "Models: Vertex AI,
  us-east5". That spec is the hook the room's model gateway reads in the next lane; `GET
  /api/catalog/models` is unchanged here. New auth kind `gcp_parent`; `gcp_identity` now requires
  the project id. The permission service client gains generic `identity(vendor)` and
  `probe(vendor, **body)`; the picker reads a project row as a "project" under its folder.

## [0.3.36] - 2026-11-01

### Changed

- **Slack and GitHub are connections, not service rows (kobo#377).** The two rows that were
  always at the top of Admin > Integrations (**Slack**, the config token pair the room creates
  agents' Slack apps with; **GitHub**, the app behind the room's pull requests and agent
  repositories) are gone, and with them the `service` kind and the **Set up** button. Each is
  now an ordinary connection added through **Add integration**: pick Slack and paste the
  pair, or pick GitHub and paste the app id, installation id and private key, with the same
  checks as before; the row shows logo, name, kind `connection`, status, Added by and when,
  **Replace** and **Remove** like every vendor. The room's own needs read the credential from
  the connection through one accessor module (`kobo_api.room_credentials`); the credential
  stays in the room's own Secret, where the service rows kept it, so a rollback to 0.3.35
  reads the same bytes. A fresh room shows an empty table. An older install's deployment
  config (`slack_tokens_*`, `pr_app_*`) still stands behind a vendor with no row, shown as a
  row marked **set in the deployment config** with **Replace** to move it into the room. At
  boot, once and idempotent, a Secret left by a service row becomes the connection row
  (logged in one line); where both already exist the connection wins and adopts the Secret.
  The Slack row on an agent's Access tab and the Slack tab read **Ask a kobo admin to add
  the Slack connection** until one exists. Overview's **N vendors connected** counts them
  with the rest. GitHub keeps its permission service connection unchanged; Slack is not a
  leased vendor and gets no permission service row. The `GET`/`PUT`/`DELETE
  /api/admin/room-services*` routes are retired; `kobo_api.room_services` stays as the
  Secret store behind the connections for one release.

- **The Access tab stays one screen as vendors multiply (#382).** Every vendor is one compact
  row, closed by default: its logo, its name, one line on where things stand ("2 held, 1 waiting
  for review", "Nothing held yet", or Slack's Live / Applying your change) and the one action
  (Ask for more, or Add Slack). A row opens on click to show what it did before (the held table,
  the pending rows, Revoke and the picker, unchanged); one row is open at a time, and the row
  last opened stays open for the session. `?vendor=<id>` opens a row from a link (the Slack
  install's way back keeps working), Slack opens itself while installing or applying a change
  and shuts once it reads Live, rows with something held or waiting sort first and Slack is
  always last. The heading is one sentence; the "Runs as" line stays.

### Added

- **A failed attempt is explained on the vendor's row.** When the permission service could not
  mint or refused a credential for the agent in the last day, the vendor's row reads **Last
  attempt failed 12 min ago: <the service's reason>** in the warn tone, the whole reason on
  hover, so an owner no longer learns only "my credential did not resolve" from the agent. The
  access payload carries it as `last_failure` per vendor (`{at, kind, reason}`), from one read of
  the service's events; a lease at that vendor issued after the failure clears it, and the
  service being away leaves every vendor without one rather than failing the tab.

## [0.3.35] - 2026-11-01

### Fixed

- **Live means live.** After a Wakes on save, the Slack row (and the Slack tab's note) said
  **Live** on the room's first health reading after the save that found the agent up, which
  could be the old agent before it went, or another replica's older reading. The health probe
  now also reports `rolling` (the agent is mid-restart: the controller has not caught up with
  the latest spec, or the new pod is not ready while the controller says the rollout is still
  progressing) and `ready_since`; the wake route answers `applied_at`; and the row waits for a
  reading taken after the save that is up and not rolling, then settles fifteen seconds before
  saying Live, since the agent reads as up a few seconds before it listens. The three-minute
  give-up and the amber "takes effect when the agent next restarts" stay.
- **Restarting, not down.** While the agent is mid-restart (`health.rolling`, or a Wakes on
  change this page just saved), the agent page reads **This agent is restarting. Back in
  about a minute.** in amber instead of the red down banner, the header pill reads
  **Restarting**, and the Slack row reads **Applying your change** rather than Down. A pod
  that is simply down, including a crash loop after a finished rollout, still reads Down.

### Added

- **Agent connections on the admin Overview.** With the permission service on, a card after
  Integrations reads **N agents hold access** and opens Agent connections. Its figure comes
  from `GET /api/admin/access/summary`, which counts agents with a connection of their own, an
  unexpired lease or an approved request from the same three reads the pane starts with and
  none of its per-agent grant calls.

## [0.3.34] - 2026-11-01

### Fixed

- **The admin panel's "Deploys to" names the cluster, not the alias.** It read the name the
  deploy platform registered the cluster under; it now asks the platform where that alias
  points and shows the cluster, with the project, location and alias on hover. The alias
  stands in when the platform cannot say.

### Fixed

- **The Harness page now says what an agent really runs on.** The room records what each
  agent is built on (`Agent.built_runtime_image`, the ref its Dockerfile's `FROM` carries,
  migration `f2a7c4d9e1b3`): compose writes it at create, every fast-lane commit that re-pins
  the file (teach notes, uploads, tools, Slack) and `PATCH /api/agents/{name}/runtime` keep
  it current, and a boot backfill reads each older agent's Dockerfile once (unreadable
  repositories are retried next boot; never on the request path). The detail payload serves
  `runtime_image` as that fact when known (the old resolved value otherwise), plus
  `built_runtime_image` and `runtime_pinned`, and keeps `room_runtime_image`. The Harness
  page compares the fact with the room's pin: **Runs on v9. The room now builds on v10.**
  with **Update to current**; **Pinned to v9** with **Move to the room's runtime** for an
  agent deliberately held; **Checking what it runs on.** until the backfill lands; nothing
  for a connected agent, which brings its own. Before this the page compared the room's pin
  with itself for every agent that followed the room, so every agent read "Runtime is up to
  date" through a room runtime move and the button never appeared. Asking the runtime route
  for the room's own runtime now clears the agent's hold so it follows the room again,
  instead of pinning it to that day's runtime for good.

## [0.3.33] - 2026-11-01

### Changed

- **A Wakes on change reaches the agent at once.** Saving **Wakes on** used to write the
  choice into the agent's files and leave the running agent on the old one until the deploy
  platform next rolled it, minutes later, with the page saying only "Saved." Now the route
  (`PUT /api/agents/{name}/slack/wake`) also sets the value on the live deployment
  (`KubectlClient.set_env`, `kubectl set env`, which rolls the pods itself) and answers
  `applied_now`; the committed and live values agree, so the later apply changes nothing.
  Both the Slack tab and the Slack row on Access keep the choices locked while it applies, so
  two changes cannot race. The Slack tab's note reads "Saved. The agent restarts with this
  choice now; give it a minute." and then "Live." once the room's next check finds the agent
  up. The Slack row's header carries an honest state instead of "Installed in <workspace>":
  "Live" (green) when the agent is ready and nothing is pending, "Applying your change. Give
  it a minute." (amber) from the save until the agent is back up, "Installing" while the
  install is in flight, and "Saved. Takes effect when the agent next restarts." (amber) when
  the live change was refused. The guide's Slack section says the same. The channel list on
  the legacy Slack tab is unchanged: it is a bundle change that takes a rebuild.
- **No channel list on the Slack row.** A room-made app answers wherever it is invited, so
  the Slack row on Access drops the channel list and **Add channel**; one sentence takes their
  place: "Invite @<agent> to a channel in Slack and it answers there." The channels route and
  the legacy Slack tab's own list are untouched, and Create still wires the open entry.

- **A room-made Slack app answers wherever it is invited.** The Slack row on the Access tab
  now says so: invite the agent to any channel and it answers there; the channel list only
  narrows it, and the empty list reads "Answers in every channel it is invited to." Clearing
  the list on a room-made app writes the open entry back into the agent's files, so it keeps
  answering instead of going silent (`PUT /api/agents/{name}/slack/channels`). After
  **Install to Slack** the person lands on the Access tab with the Slack row scrolled into
  view and lit for a moment (`?vendor=slack`, read once and dropped), and an install that did
  not finish says so on the row itself. The guide's Slack section says the same. The Slack
  tab's own allowlist wording is unchanged.

## [0.3.32] - 2026-11-01

### Changed

- **Agent to agent picks by team.** On the Harness tab, **Request another team's agent**
  now opens a list grouped by team (name, how many agents, a chevron); a team opens into
  compact rows with a checkbox, the agent's name and one line on what it does. Tick one or
  many across teams and one **Request access** sends a request per agent, with a toast
  saying how many went out. An agent already asked, or not ready, shows its state in place
  of the box. The viewer's own team never appears. The table below gains Team and Date
  columns and a plain empty sentence. `GET /api/agents/{name}/requestable` answers `group`
  on each `already` row, leaves same-group agents out of `not_ready`, and names the group
  it leaves out as `paired_group`.

## [0.3.31] - 2026-11-01

### Added

- **Slack as a vendor on Access (#371, phase 2 of #304), behind `slack_relay`.** With the
  org key on, the Access tab gains a Slack section: pick **Wakes on**, **Create Slack app**
  (the room creates the app from the manifest with its config token, pointing Slack's events
  at the room's relay instead of Socket Mode, so no app-level token is ever made), then
  **Install to Slack** (Slack's own install page; the callback stores the bot token through
  the agent's secrets lane and restarts the agent). Installed: workspace, channels editor,
  Wakes on (applied with `apps.manifest.update`), Remove. Two public routes, `POST
  /slack/relay/{relay_id}` (signature verified with the app's signing secret inside a five
  minute window, challenge answered for a pending id, `event_id` dedupe, 200 at once, forward
  in the background with `X-Kobo-Relay-Secret`, token bucket per id) and `GET
  /slack/oauth/callback` (single-use state), exempt from the identity proxy like `/oauth`. New
  table `slack_relay_apps` (migration `b4d8e2f6a1c3`) holds the relay id, app id and the
  signing, relay and client secrets plain, the way the room's other held tokens are; the bot
  token never lands there. The Slack tab shows only for an agent with a paste-flow app
  (`slack.legacy`) or when the switch is off; with the switch off nothing changes.

## [0.3.30] - 2026-11-01

### Added

- **The Audit page has two tabs, People and Agents (#371, section 3).** People is the room's
  own log as before. Agents is what agents did with their connections, read live from the
  permission service and shown in the same row shape: a lease it minted (`lease.minted`,
  outcome `ok`), one it refused (`lease.refused`) and one it could not mint (`lease.failed`),
  each as one sentence ("ward borrowed read on AWS aws-prod for 1h: count instances"). The
  Actor and Action selects hide on Agents; the agent and outcome facets come from that
  answer; Export hands that window back as CSV or JSON. `#/admin/audit?tab=agents&agent=x`
  opens it filtered. `GET /api/admin/audit/agents` (admin gate; `agent`, `outcome`, `since`,
  `until`, `q`, `limit`, `format=csv|json`) is the route, `kobo_api.audit_agents` the merge
  and the sentence, `BabClient.events(...)` the new read (bab `GET /events`). The people
  route and its export are unchanged.

### Changed

- **Agent connections leave the Integrations table and get their own page (#371).** `GET
  /api/admin/integrations` no longer appends a permission-service row that has a `parent`
  (an agent's GitHub repository set, kobo#365), so the Integrations table is the room's
  connections only. A new admin page, **Agent connections** (the `connections` id with the
  permission service on; the older lanes page keeps the id when it is off), is one card per
  agent the room knows, newest activity first, with one row per vendor: the agent's own
  connections there, what it holds (permission, tier, expires, granted by) and what it is
  borrowing right now (permissions, task, issued, ends, via). **Revoke** calls the existing
  `DELETE /api/agents/{name}/access/grants/{grant_id}`; a search box narrows by agent, vendor
  or repository; `#/admin/connections/<agent>` puts that agent's card first. Served by `GET
  /api/admin/access/agents` (admin gate): one `integrations()` for the children, one
  `all_leases()` filtered to the unexpired, `grants()` per agent that has a child, a request
  or a lease, fanned out on a thread pool of six after the room's own table is read and the
  session closed (the 2026-10-01 pool rule). An agent with nothing is still listed.
- **The Access tab lists the vendors the room has connected, nothing else (#371).** `picker()`
  carries `connected` per vendor (a cloud with a connected account, a service or GitHub with a
  parentless connection); `PermissionsPanel` draws those alone. The "0 of 0 projects
  connected" subtitles are gone; GitHub keeps its repository count because that is about the
  agent. With nothing connected the tab says so and links the word Integrations into the admin
  panel for someone who may open it. `GET /api/access/picker` carries the same field; the
  Review pane is unchanged.

## [0.3.29] - 2026-11-01

### Changed

- **The repository picker reads the warm listing (#368).** `GET` and `PUT
  /api/agents/{name}/github/repositories` read the five-minute listing the connect form
  already keeps (`repo_search.RepoIndex.listing`, filled by the warmer at boot and every
  TTL) instead of walking the installation a page at a time on every open and again on
  every save; a room with no index still walks. The `GET` payload carries `total`, the
  count of repositories the room can see, so the picker can show the best matches for what
  is typed and still say how many there are.
- **The agent page's shape (#368).** The Access tab opens on **What this agent can reach**:
  the vendor list with logos, what the agent holds shown under each vendor, then pick a
  vendor, pick the level, Confirm; the runs-as identity line stays. People moved to an
  **Owners** button in the header beside Edit details (same sheet, same roles). Other agents
  moved to a Harness section, **Agent to agent**. The Settings tab is now **Secrets** and
  holds secrets and variables only; **Build log** is a header button beside Health;
  **Destroy this agent** sits at the bottom of Edit details behind the same confirm. Tabs:
  Chat, Slack, Access, Harness, Secrets. The guide's sections follow.

## [0.3.28] - 2026-11-01

### Changed

- **GitHub per agent: a repository set and a level (#365).** The room's GitHub connection is
  never granted to an agent. Each agent's GitHub access is a child integration
  `<connection>-<agent>` in the permission service, carrying the repository set its owner picks
  on the Access tab from the installation's repositories (`GET`/`PUT
  /api/agents/{name}/github/repositories`, owner or manager; strays and an empty set are 422,
  no GitHub connection is 404), and a level requested on it: Read (`github_repos_read`), Review
  pull requests (`github_pulls_review`) or Open pull requests (`github_pulls_write`). `GET
  /api/agents/{name}/access` shows GitHub as that agent's own set (or the connection as a
  placeholder while none is picked), never another agent's; every account entry carries
  `repositories` and `has_repo_set`, every permission carries its `label`; Review rows and
  decide results carry `repositories` for a request on a child.
- **Lanes per permission.** Read opens the read-only `github-mcp` target (`github:read`);
  Review pull requests opens `github-mcp-write` (`github:review`); Open pull requests adds
  `github:write`. The gateway's `github` handler is in no lane. `follow` recomputes on approve
  and revoke and now withdraws allow patterns the agent no longer earns (one commit, "kobo:
  allow the ... tools" or "kobo: withdraw the ... tools", one restart).
- **Two live targets per GitHub connection.** `<id>-mcp` (read-only header, `github:read`) and
  `<id>-mcp-write` (no header, `required_scope github:review`, `tool_scopes` sending the
  branch-, commit-, pull-request- and issue-creating tools to `github:write` and merge and
  delete to `github:never`), attached, written into the gateway pull request fallback and
  removed together. The connection's ceiling is every `github` catalogue permission; the
  room-level "Repositories agents may be granted" field is gone (an older row that has it is
  read as before).
- **Guide.** The Access tab holds who can reach the agent and what it can reach; Permissions
  moved there from the Harness; GitHub is repositories first, then a level.

### Removed

- **The pull request broker's lane for agents.** `POST /broker/agents/{name}/github-token`,
  `POST /api/agents/{name}/repo-access`, `GET /api/repo-access`, `POST /api/repo-access/decide`,
  the MCP tools `kobo_request_repo_access`, `kobo_list_repo_access` and
  `kobo_decide_repo_access`, and the composed `github` sidecar block (`BundleSpec.broker_url`).
  `pr_broker.py` keeps only the agent token verification the Jira lane shares. The Jira lane
  and the room's own GitHub client are untouched.

### Migration

- `repo_access.migrated_at` (alembic `e1f2a3b4c5d6`). At boot, with the permission service on
  and a GitHub connection present, every agent with approved repo-access rows and no GitHub
  child gets the child with those repositories and an approved Open pull requests grant, its
  lane brought in step, the rows stamped and an audit row written; an agent that already
  picked a set keeps it. Without a connection the rows wait for the next boot. Nothing an
  agent held is lost.

## [0.3.27] - 2026-10-31

### Changed

- **The room's look: the shadcn-svelte foundation, on the room's own tokens.** Tailwind v4 and
  shadcn-svelte (style Vega, Stone palette, Geist headings, Public Sans body, Geist Mono for
  names and labels, Phosphor icons) are wired in; `ui/src/lib/shadcn.css` maps shadcn's tokens
  onto app.css's, so the room still has one palette in one file and dark mode still follows
  the operating system. No production screen imports a shadcn component yet; the restyle below
  is the room's own CSS on the new tokens, and Tailwind's preflight is deliberately off until
  the hand-rolled components are gone.
- **Shell.** White page, a floating dark rail with the dot-matrix KOBO wordmark (the hammer
  lives on as the favicon), Phosphor icons, and the main content in a warm grey frame.
- **Dashboard.** Cards carry a portrait (or the agent's initial), a two-line description, an
  inset facts tile, and a Health button that opens the agent's dashboard. The filter bar is one
  row (Group, a new Status filter, Search). "Made by" reads "Author:"; a failed or waiting card
  shows the real age and author instead of "Open for details".
- **Agent page.** A header card (portrait is the portrait menu; Repository, Health and Edit
  details as buttons; labelled facts row), a back button to the cards, underline tabs with
  icons, the first reply as the chat's opening message, an inline composer, Shared thread and
  Colleague conversations side by side. Slack, Access and the Harness sections read as settings
  rows; permission lists and vendor rows line up; "Convert..." and "Destroy..." lost their
  ellipses; "Add someone" is "Add person".
- **Getting started.** One top bar (section picker, previous/next, a progress track) instead of
  a second side list; the section in a centred reading card with previous/next at the bottom.
- **Admin.** A page header, an icon navigation card, Overview as status cards, Review as
  Waiting/Everything tabs with a selection bar, the integration catalog as alphabetical vendor
  cards with logos, Audit's filters in one panel with Export beside the title, and the role
  editor as settings rows. Every native dropdown and field in the room shares one style.
- **Account menu** no longer lists the guide; the rail's Getting started entry is the one way in.

### Added

- `make -C api lab`: a local room over the test fakes with seeded agents in every state the UI
  draws, for working on the UI without a cluster (`api/scripts/ui_lab.py`).

### Removed

- The `FirstReply` component (its content moved into the chat log).

## [0.3.26] - 2026-10-31

### Changed

- **The GitHub MCP catalog authenticates with a token the gateway mints, not a stored one** (#360).
  The `github-mcp` target names `catalog_token_provider = "github-app"` (liminal 0.9.2) instead of
  a catalog `Authorization` header liminal refuses; the gateway mints its own App installation
  token before every catalog fetch, so no personal token is stored or rotated. The vendor
  checklist records the rule. Needs the gateway on liminal 0.9.2.

## [0.3.25] - 2026-10-31

## [0.3.24] - 2026-10-31

### Fixed

- Approving an agent's first permission on a vendor now also lets the agent see that vendor's gateway tools: its bundle's allow list gains the vendor's patterns as one commit and one restart. Before, the grant and the gateway lane were set and the runtime still offered none of the tools, because the bundle's allow list never named them (#353).

## [0.3.23] - 2026-10-31

### Changed

- Admin > Services is a status page with one action, Reload org config. The **Wire agents to the tool gateway** button from 0.3.22 and the **Sweep directory** button are gone, with their routes: new agents get the gateway at create, older ones are migrated by hand once, and the room retires stale switchboard directory entries itself each night, writing an audit row as the room (#351).

## [0.3.22] - 2026-10-30

### Added

- **Admin > Services: Wire agents to the tool gateway.** A room that named its tool gateway (\`liminal_url\`) after its agents were made had agents with permissions and no provider to use them with. The button gives every READY agent of the room's own the composer's gateway block, as one commit and one restart each, and leaves one that already carries it alone. \`GET/POST /api/admin/agents/gateway-wiring\` (#349).

### Fixed

- Admin > Overview no longer flashes the Connections row on a hard refresh; the delivery chip keeps polling while the tab is in the background, so it stops pulsing when the change lands rather than on the next reload (#348).

## [0.3.21] - 2026-10-30

### Changed

- Skills: the catalog and Suggestions rows read **Select**, the bar says the selected skills go out together as one update and one restart, and the one button reads **Add N skills**; before, each row read Add and invited one add, one save, one restart at a time (#345).

## [0.3.20] - 2026-10-30

### Fixed

- A change the room pushed counts as delivered once the agent's new pod is available, not once the manifest carries the commit. With one replica and a Recreate strategy that moment is the restart itself, so the header said Down for a roll the room had started; it now keeps saying Updating until the agent is back (#344).

## [0.3.19] - 2026-10-30

### Changed

- Admin > Integrations: the Slack and GitHub rows read Connected like every other integration. A value an older install's deployment config carries is marked so and offers Replace, which moves it into the room, where the room's value wins; a fresh room has nothing baked in and an admin pastes both (#337, #339).

## [0.3.18] - 2026-10-30

### Added

- **A connect adds the vendor's target to the gateway live.** With the gateway's admin plane wired (liminal 0.9.0), connecting Datadog, or a token vendor, puts its target on the gateway through `PUT /admin/targets/<name>` the moment the credential is accepted; the row reads Connected at once, with no pull request and no button. Disconnecting removes it the same way. A gateway that predates the lane, or a room with no admin plane, gets the pull request as before, and the row says which (#335, liminal #222).
- **A row waiting on a gateway pull request learns how it ended.** The Integrations listing reads each waiting row's pull request: merged makes it Connected and drops the link, closed without merging makes it Failed and says to add again (#334).

### Fixed

- Gateway add and remove are separate pull requests, on separate branches; opening one closes a stale opposite one, so a row never links to a change that would undo it. A connect whose edit changes nothing on the gateway repo reads Connected with "the gateway already carries the target" (#333).
- The Add button spins and names its step; the row reads Removing while a remove runs; a second remove is "already removed", not an error (#332, #333).

## [0.3.17] - 2026-10-30

### Fixed

- **Gateway edits go out as a pull request's branch, never as a push to the gateway's main.** The pull-request helper committed and pushed the checked-out default branch before creating the branch, so every connect landed on the gateway repo's main first and the pull request then failed with nothing to commit; a Datadog connect doubled the `datadog-mcp` stanza on main and the gateway refused to start. A gateway edit now pushes only its branch, and a test pins that (#330).
- Removing or replacing a target strips every stanza of that name with the comment lines above it, whoever wrote it, so a hand-written stanza is replaced rather than doubled (#330).
- The Datadog connect waits up to a minute for the permission service to see the Secret it just wrote (a mounted Secret takes the kubelet a moment), and the waiting sentence no longer asks for a restart (#330).

## [0.3.16] - 2026-10-30

### Added

- **Datadog's MCP server as a gateway target, on each agent's leased key.** Connecting Datadog through the permission service now also opens a pull request on the gateway's config repository adding the `datadog-mcp` target (Datadog's hosted MCP server for the site, `auth_method = "service-headers"`, liminal 0.8.0): every agent's call carries the application key the permission service leased it, the catalog fetch uses the gateway's own Datadog key. The row reads Pending until that merges and the gateway rolls; disconnecting opens the pull request that removes it. The Integrations form shows the Datadog MCP server address, prefilled from the site (#327).
- **The gateway lane follows the permission.** Approving an agent's Datadog permission sets its gateway grant too (`datadog:read`, handlers `datadog` and `datadog-mcp`), revoking its last one takes the lane back, and Review > Sync agents brings every agent in step; scopes and handlers the room did not put there are left alone. The permission is the only thing anyone grants (#327).

### Fixed

- Removing a gateway target now also removes its `[target.*]` sub-tables from `targets.toml`; before, a target with an auth table left those lines behind.

## [0.3.15] - 2026-10-30

### Fixed

- Datadog connect: the permission service's "cannot mint for datadog yet" answer (no Secret read yet) is the not-configured case, so the Secret stays and the row waits for a restart instead of being refused; the Datadog form hides the vendor address and the read/write choice, which do not apply (#322).

## [0.3.14] - 2026-10-30

### Added

- **Datadog through the permission service.** Integrations > Datadog takes a service account id, the API key and a management application key; the room writes them where the permission service reads them, probes, and connects Datadog with every read permission as the ceiling. Each agent then borrows a one-hour application key scoped to its grants, through the tool gateway (#322).

## [0.3.13] - 2026-10-30

### Fixed

- Admin > Integrations no longer lists the permission service's per-agent `agents` entries; they are plumbing behind Review and the Access tab (#310).

## [0.3.12] - 2026-10-30

### Added

- **Audit log.** One `audit_log` table of what people, the room and agents did: actor, action, target, a sentence, outcome, source, ip and bounded detail, landed state; 38 mutating paths record into it; 30-day retention (`audit_retention_days`) pruned nightly; `GET /api/admin/audit` with search, filters, cursor paging and facets, `GET /api/admin/audit/export` as CSV or JSON; the Audit page and a per-agent link from Activity (#316).
- **Asking another team's agent is a permission.** With the permission service on, Request another team's agent posts an `a2a_ask` request on the target agent, approved in Review; switchboard pairs follow those grants, the same group, and accepted invites. Every ready agent is an `agents` integration in the permission service. Needs bab 0.4.5 (#310, room half).

### Fixed

- Admin: Connections no longer flashes on a hard refresh before Access is known (#314). A Can manage grant does not reach colleague requests or agent asks. The Slack set-up sheet says to use a dedicated kobo Slack account (#304).

## [0.3.11] - 2026-10-30

### Added

- Access tab: **People** carries a role per grant, **Can see** or **Can manage** (owner-type actions, never destroy); **Other agents** is its own section for cross-team requests and pairs (#309).

### Changed

- Slack and GitHub are rows of the Integrations table with Set up / Set by deployment / Not set up; the agent's Slack tab and Repositories card grey out with "Ask a kobo admin to add the ... integration" until an admin sets them up (#304, phase 1).
- Every timestamp leaves the API as UTC with a Z and is read as such by the UI, so pages show the viewer's local time (#306).
- Audit events follow their row (Applied or Failed) and read as one plain sentence, newest first, with a kind filter (#307).
- Activity rows run newest first, open on click to the full arguments, and the table scrolls sideways (#308).

## [0.3.10] - 2026-10-30

### Changed

- Activity: a summary strip (calls, refused, errors, tools used), rows grouped by day with the newest and any day with a problem open, **Problems only**, and 25 rows at a time (#300).
- Permissions picker: a cloud lists only the accounts connected under Admin > Integrations, under their organization; accounts the hub can see but nobody connected are no longer offered, and the `cloud_accounts` org key is retired (#301).

## [0.3.9] - 2026-10-30

### Added

- **Room services** under Admin > Integrations: the Slack app config tokens and the GitHub app for pull requests are set from the room and read live, stored in a Secret the room owns in its own namespace; deployment config stays as the fallback and shows as "Set by deployment". The Slack tab and the Repositories card say plainly what is missing and where to add it (#284, #288).
- `GET /api/agents/{name}/pairs`: the agents an agent may share a thread with. The shared thread's **Add participant** lists only those; the Access tab says "Can join shared threads" on paired rows (#289).

### Changed

- Persona card reads Persona, Source (Built-in, or the team), Purpose, Capability; Edit details says Persona source. The colleagues row is **Asks other agents**: Yes, when it helps; No, but knows who they are; No. With the permission service on, the Harness has no Identity and access section; Permissions shows which identity the agent runs as (#285, #286, #292).
- Skills: **Save N skills** with no second confirm; rows show Pending, Building, Rolling, Active or Failed, the same for a new guardrail skill. While a change this page pushed is building or rolling, the whole agent page is frozen, with the banner saying why (#287, #290).
- Destroy fails loudly when the switchboard keeps the agent's entry, so Destroy again retries it; forget also deletes the user grants, thread seats, learnings and system tasks keyed by the agent's name and records which room projections did not catch up (#291, part).

## [0.3.8] - 2026-10-29

### Changed

- Owner QA pass of 2026-10-29. Slack: the tab says up front when the room has no Slack, and the first run picks channels and **Wakes on** before the manifest (#262). The Persona card and Edit details say **Persona team** and what it is (#263). The colleagues choice reads **Works with colleagues** with plain options (#264). A new guardrail skill starts from a template, and a plain body gets its frontmatter written for it (#265). **+ Tool** opens on a working example that states the rules (#267). The Repositories card treats a room with no pull request app as a state, not an error (#270). The Access picker lists agents that are not ready yet and says when same-group agents are already reachable (#273).
- Secrets and environment: room-set variables are locked rows and never re-sent, one name may appear once across secrets and variables, the new row has placeholders, and **Save** restarts the agent itself (#250, #271, #272).
- Teach something shows the taught line in the Learnings table at once, as a kept learning that survives reload (#269).
- Admin > Services > **Sweep directory** retires switchboard entries the room created for agents that no longer exist and do not run in the cluster, after a preview you confirm; destroy says when the switchboard still lists the agent; the agent's roster no longer defers to the live directory (#274, room side).
- **Skills are picked in one go.** The catalog and Suggestions lists share one picker: tick skills, press **Add N skills**, confirm once. One request (`POST /api/agents/{name}/skills/catalog/batch`, 1 to 20 picks) lands them in one change and one rebuild; a pick the agent already carries or the catalog no longer serves is refused on its own and the rest still land. The chosen rows show in the table at once as Delivering, the delivery chip sits beside the table, and errors show in both lists (#266).
- **Writes to one agent queue instead of racing.** The skills, tooling and intake lanes hold a per-agent lock from clone to push, so two changes at once no longer fail as "couldn't reach your agent's files" or rotate the agent twice. While a change this page pushed is still rolling, the agent header reads Updating rather than Down (#268).
- AWS organizations: one external id per organization, so one spoke template deploys the role to every member account (a StackSet). Connecting the hub checks every account through it and connects the ones that carry the role; **Check accounts** runs that again. No per-account Add account (#275).
- Connect AWS sheet: pick Terraform, CloudFormation or by hand first; the sheet shows only that method, for the management account and for the member accounts (#276).

## [0.3.7] - 2026-10-29

### Changed

- The vendored cloud-access release is 0.2.0: the `aws/hub` module the Integrations page points at for an organization's hub role, and the CloudFormation template's trust conditions on `oaud`.

## [0.3.6] - 2026-10-29

### Changed

- **AWS connects as an organization with accounts under it.** Add integration › AWS creates the hub role in the organization's management account (trusting the room's identity; allowed only to assume each member account's role, list the accounts, read the bill). **Add account** on the organization lists its accounts through the hub; the account's role trusts the hub with an external id of its own; the room probes the two hops before saving. Integrations shows organizations with their accounts indented; the Permissions picker names each account's organization and offers the organization's other accounts as not connected yet. A second organization is a second row. The trust policies the page renders condition on `oaud`, which is where AWS puts a Google token's audience; the earlier `aud` condition refused the room's own token. Flat roles keep working. New: `GET /api/admin/integrations/{org}/accounts`; the setup page takes `mode=org|account`, `parent`, `account_id`.

## [0.3.5] - 2026-10-29

## [0.3.4] - 2026-10-29

### Changed

- **The admin panel follows the permission service.** Connect AWS names the permission service's identity as what the role must trust, asks it to try the role before saving, and connects the account there (the ceiling is every read permission the catalogue offers for the vendor); other vendors connect there too. Disconnect takes the connection out of it, revoking the grants on it. Integrations lists connections the service holds that the panel never made. Connections is shown only while the service is off. Review opens with **Borrowed now**, the credentials out in the room at this moment (`GET /api/admin/access/leases`), and Overview counts them.

### Changed

- A base image, `ghcr.io/tenishai/kobo-base` (`docker/base/Dockerfile`, workflow `base-image.yml`), carries git, gh, kubectl and gcloud on its own cadence, and the product image builds on it by digest (`ARG BASE_IMAGE`), so a release does only the Python and UI layers.
- **Faster release builds.** The UI build and the lockfile export run once on the build host instead of once per target architecture (under arm64 emulation they were most of a release's build time), and unchanged layers come from a registry cache (`ghcr.io/tenishai/kobo:buildcache`) instead of being rebuilt. The image's contents do not change.

## [0.3.3] - 2026-10-29

### Changed

- **The Permissions picker walks vendor, account, service.** The first level is the vendor (AWS, Google Cloud, Azure, Datadog, Atlassian, ...); a cloud then lists its accounts, projects or subscriptions (connected, or listed in the new org key `cloud_accounts` and not connected yet); a service vendor opens straight on its permissions. The last level is a searchable list of services with read access and one **Confirm request**; write tiers are not offered there. Ticking a service no longer closes the list (the first version re-derived the accordion's open state on every click). `GET /api/access/picker` now returns `vendors` with `accounts` and the vendor's permission tree; the agent's Access payload carries the same `vendors`.

## [0.3.2] - 2026-10-29

### Added

- **Access through bab.** With `bab_url` in the org config and the console token in `KOBO_BAB_TOKEN`, the room is the console for its permission service: a **Permissions** section on the agent page (what the agent holds, with tier and expiry; the vendor's permission tree to ask for more, anything above the connection's ceiling greyed with the reason; Revoke now for admins), a **Review** pane under Admin (requests waiting, bulk Approve, Deny with a reason, Sync agents), borrowed credentials under Activity, and an **Access** row on Services. The room tells the service which agents exist (at boot, after every create and destroy, and from Review) with the pod identity that is each one, so a lease asked for by the pod lands under the agent's name. Routes: `GET /api/access/picker`, `GET /api/agents/{name}/access`, `POST /api/agents/{name}/access/requests`, `DELETE /api/agents/{name}/access/grants/{id}`, `GET /api/agents/{name}/leases`, `GET /api/admin/access/requests`, `POST /api/admin/access/decide`, `POST /api/admin/access/sync`. Everything is off, and the room runs as before, without `bab_url`.

## [0.3.1] - 2026-10-28

### Added

- A release workflow: a `vX.Y.Z` tag builds the multi-arch image (linux/amd64, linux/arm64), publishes it to ghcr under the tag and the short sha, and records the digest on the GitHub release. No image is built on a push to main.

### Changed

- Shared threads talk to one agent at a time (kobo #240). A turn goes to the thread's primary, the agent it was started with, or to the agent you pick (a "To" box, or `@name` at the start of the message); what that agent asked its colleagues while answering shows under its reply. "Ask everyone" is the old everyone-answers behaviour, on request. An agent that does not answer is a "didn't answer" entry and the turn still completes, where one down agent used to fail the whole turn with a 502. API: `POST /api/threads/{id}/turns` takes `to` and `broadcast`; each reply carries `error` and `consults`, and the response names `to`.

- The image is pinned end to end: base images by digest, Python packages from `api/uv.lock` with hashes verified by pip, kubectl by version with its checksum, gh and gcloud from per-arch apt repos.


## [0.3.0] - 2026-10-27

### Upgrading

- **Four additive migrations**, run at boot: `a1b2c3d4e5f6` (admin store), `b7c8d9e0f1a2`
  (integrations, roles, role groups, grants), `c4d5e6f7a8b9` (MCP redeemed codes and
  revocations) and `d7e8f9a0b1c2` (`agents.pass_minted_at`). The alembic head moves from
  `a8e2c4f6d913` to `d7e8f9a0b1c2`. Back up the database first; to go back, restore the backup
  or run `alembic downgrade a8e2c4f6d913`.
- **Check the config file before rolling.** `Config` and its `mcp:` block now refuse unknown
  keys, so a typo or a key from another product stops the room at boot instead of being
  dropped. `runtimes` is the one retired key still accepted, with a warning.
- **An open room must say so.** A room with no `iap_audience` now logs an ERROR at every boot
  unless `allow_unauthenticated: true` is set, and an MCP server on such a room (any
  `mcp.oauth_client_id`) refuses to start unless `mcp.allow_without_iap: true` is set too.
  Deployed rooms behind IAP change nothing.
- **New optional org keys**, all off when absent: `admin_panel` (the admin panel and the
  account menu's Admin entry), `logout_url` (the account menu's Log out, for Google's proxy
  `/_gcp_iap/clear_login_cookie`), `gateway_config_repo` and `gateway_namespace` (where a
  connected integration lands), and `liminal_admin_url` (the tool gateway's admin plane). The
  admin plane is used only when the switchboard is wired too, because the room's own gateway
  token (persona `kobo-room`, `admin:*`) is minted there: list `kobo-room` as an admin persona in
  the gateway's trust registry. The Activity view needs a gateway that writes its audit rows
  (tenishai/liminal#211); the AWS connect check needs `aws_identity` and `aws_probe` (liminal
  #209).
- **New deployment keys for ergon-style rooms**: `delivery_backend: directory` with
  `directory_root` (and optionally `directory_reload_url`), and `storage_backend: directory`
  with `storage_root`. The defaults (`deployed`, `gcs`) change nothing.
- **Names whose namespace already exists are refused** at create and connect (409), and the
  reserved names grow (see Fixed).
- For the api/worker split, apply `k8s/overlays/api` and `k8s/overlays/worker` (see
  `docs/operating.md`); `k8s/base` is still the one-replica shape.

### Added

- **"What kobo is", a guide section for an assistant explaining the room to its person**
  (`api/kobo_api/templates/guide/12-what-kobo-is.md`, id `what-kobo-is`): what the room is,
  where it sits beside daedalus, gojira, techne, cabal, liminal and sero (required or optional),
  what it can do, and every choice a person makes with why they would make it. The glossary
  moves to `13-glossary.md` (its id is unchanged). The `kobo_guide` tool's description now says
  to start there for "what is kobo", and its listing hint names it. README gains a "Where this
  fits" section.
- **Admin panel, Connections for real** (PR #191, issue #173 step 3). A lane is a grant row
  (`read` or `write`): `GET /api/admin/connections`, `PUT` and `DELETE
  /api/admin/connections/{agent}/{integration}`. Applying one recomputes the agent's whole
  gateway grant from its rows, merges the handlers the gateway already holds for that persona,
  writes it live through the gateway's admin plane with the room's own token, and rewrites the
  persona's block in the gateway repository's grants file as a pull request. Every agent is a
  row, so "no lane" is visible. New org key `liminal_admin_url`; empty means the pull request
  only.
- **Admin panel: reload the org config without a restart** (PR #187, issue #185). `POST
  /api/admin/org/reload` merges the org config over the boot-time local config again and swaps
  the live config, so a new model, group or people list lands at once; the response names what
  still needs a restart (add-on URLs). The button is on the Services page.
- **The admin panel behind a switch, and an account menu** (PR #174). Org key `admin_panel`
  (default off) turns on `#/admin`; its routes answer 404 while it is off and 403 to anyone who
  is not an admin. The sidebar's bare email becomes an account menu: the email, **Admin** (admins
  only, while the panel is on), **Guide**, and **Log out** when `logout_url` is set.

- **Activity: what an agent did through the tool gateway** (Kyle's ask 2026-10-25). A
  new Harness section on every agent, connected ones included, lists each call the gateway
  admitted and each one it refused, newest first: the tool, the outcome, the duration, the
  arguments in brief with secrets masked, and for a refusal what was missing. Filter by
  tool or widen the window to 30 days. Read live from the gateway's audit through its admin
  plane (`GET /api/agents/{name}/activity?days=`); the gateway writes the rows since
  tenishai/liminal#211, and without an admin plane the section says so instead of showing an
  empty list. Guide section 08 describes it.

- **Connect AWS without a key** (design 2026-10-25, cloud access for integrations). The
  Integrations page's AWS entry is now a sheet: see what the role allows (every read-only
  pack: `inventory`, `logs`, `security-audit`, `cost`; what each agent may do inside it is
  set per lane), create the role one of three ways
  (a Terraform module snippet, the CloudFormation template with its parameters
  pre-answered, or the trust policy alone), paste the role's ARN. The room asks the gateway
  to assume the role once before saving it, so "connected" is said only after AWS agreed;
  a refusal comes back as AWS's sentence and nothing is written. The account lands in the
  gateway's trust registry as `[tool_extras."aws.roles".<id>]`, not as a target, and agents
  reach it through the gateway's `aws` handler once a lane grants `<id>:read`. The page is
  rendered from a pinned release of `tenishai/cloud-access` vendored under
  `api/kobo_api/vendor/cloud_access/` (`VERSION` names it), so it works while that repo is
  private. New route `GET /api/admin/integrations/aws/setup?packs=`; the gateway admin
  client learns `aws_identity` and `aws_probe` (liminal #209).

- **A directory delivery backend** (issue #207, for ergon). With `delivery_backend: directory`
  and a `directory_root`, create composes the same bundle it would push to a repository into
  `<root>/<agent>/` in one rename, keeps the picture in the room, lists the agent in the
  switchboard directory when one is wired, and tells the running set: a POST to
  `directory_reload_url` with the agent's name and folder, or a `.ready` marker in the folder for
  a watcher. Destroy for that kind removes the folder and the listing and nothing else. A name
  whose folder already exists is refused at create. `deployed` stays the default.
- **A directory-backed catalog store** (issue #208, for ergon's laptop and team tiers). With
  `storage_backend: directory` and a `storage_root`, each "bucket" is a folder on disk and a key
  is a relative path under it; custom metadata rides in a `<key>.meta.json` sidecar. The persona
  and skill catalogs, the built-in persona seed at boot and create all work with no GCP
  credentials. `gcs` stays the default, so deployed rooms change nothing.

### Fixed

- The room's own INFO boot lines (the auth regime, the dispatch-pairs sync, the built-in persona
  seed) now reach the pod log: the deployed entry configures a handler for the `kobo_api` loggers.
  Library loggers are untouched.
- **A destroy is claimed, beats, and is swept when its pod dies** (issue #203). The DELETE route
  now takes the row with one conditional update (a second DELETE while a destroy is in flight
  answers 202 and launches nothing; a sweep racing it loses the claim), the destroy thread writes the same heartbeat a provision does, and
  the interrupted sweep flips a dead destroy to DESTROY_FAILED, which the next DELETE resumes. The
  waiting sweep claims a row with `UPDATE ... WHERE status = 'waiting'`, so it can no longer flip
  a row a DELETE just took back to provisioning and run the pipeline over the teardown.
- **A crash between an external create and its commit no longer strands the agent** (issue #201).
  `RepoStep` records the repository name and commits before asking GitHub to create it, so a retry
  finds the row naming the repo instead of refusing to adopt it. `OnboardStep` commits the platform
  app id the moment it exists, before the long wait for `active`, and when the platform reports a
  conflict for an agent with no recorded id it adopts the app the platform holds under the agent's
  own name and repository (the room's own), raising as before when there is none.
- **Shutdown drains inline pipelines too, and the split shape ships as overlays** (issue #202).
  Create, connect and retry now register the pipeline thread they start, so `drain_pipelines`
  waits for it like it waits for the worker's; before, only queue-started runs were drained and a
  roll in role `all` killed a running build at the grace period. `k8s/overlays/api` and
  `k8s/overlays/worker` carry the role, replica count, strategy and grace period the
  documented api/worker split needs, and `docs/operating.md` says which shape `k8s/base` deploys.
- **The PR and Jira brokers refuse a colleague pass the room has since replaced** (issue #199).
  A re-wire revokes the earlier passes at the switchboard, but the brokers verified a pass by
  signature and claims alone, so a pass that leaked from a pod kept minting GitHub tokens for the
  rest of its year. The room now records when it minted each agent's pass (`agents.pass_minted_at`,
  migration d7e8f9a0b1c2) and both brokers refuse a pass issued before that instant. The JWKS
  client is built once per switchboard instead of once per call.
- **A connected repository's CI log and environment are operator-only, and build logs are
  scrubbed** (issue #200). The room cannot check who owns a repository on GitHub, so whoever
  connected it became its owner in kobo and could read its CI output and `gojira.yaml` env.
  For connected agents those two reads now follow the operator gate (with its dev-parity arm);
  composed agents are unchanged. Every build log is run through `kobo_api.redact` first, which
  masks well-known token shapes, private-key blocks, bearers and values after secret-ish keys.
- **MCP authorization codes are spent once, and a person's MCP access can be cut off**
  (issues #196, #197). A code exchanged a second time within its minute of life now fails and
  revokes the tokens the first exchange produced; before, each exchange minted an independent
  refresh family. A refresh family now ends after thirty days, so the next refresh sends the
  person back through Google and the Workspace check. Admins can cut one person off from the
  admin API (`POST /api/admin/mcp/revocations`): every family they hold is revoked at once and
  access tokens issued before that instant are refused until they expire. Two additive tables
  (`mcp_redeemed_codes`, `mcp_revocations`, migration c4d5e6f7a8b9).

### Added

- **Refresh an attached skill in one commit** (issue #182): `POST .../skills/catalog` with
  `refresh: true`, and `kobo_set_agent_skills(refresh=[...])`, move an attached catalog skill to the
  version the catalog serves now in one commit and one roll, instead of a remove and an add. A
  skill the agent does not carry, or a local skill, is refused.
- **Admin panel, Roles for real** (issue #173 step 3): `GET /api/admin/roles` seeds the four built-ins
  (Admin, Operator, Member, Reader) from the org keys on first read and returns every role with the
  catalogue the editor offers (capabilities, admin permissions, the room's models and groups, the
  connected integrations); `PUT`/`DELETE /api/admin/roles/{id}` save and delete custom roles, every
  change in the audit log. A built-in cannot be deleted.
- **Admin panel, Integrations connect for real** (issue #173 step 3): `GET/POST /api/admin/integrations`
  and `DELETE /api/admin/integrations/{id}`. A connect records the row, stores the credential in the
  secrets add-on under `apps/kobo/integrations/<id>` synced into the gateway's namespace, and opens a
  pull request on the gateway's config repository adding the vendor's `[[target]]`; the card says how
  far that has gone. Two org keys: `gateway_config_repo`, `gateway_namespace`. Disconnect reverses it.
- **Admin panel tables for integrations, roles and grants** (issue #173 step 3): `admin_integrations`,
  `admin_roles`, `admin_role_groups`, `admin_grants` (migration `b7c8d9e0f1a2`), with store functions
  that write the row and its audit event together and refuse a lane wider than the integration's
  base access, an unknown role body key, or deleting a built-in role. No routes yet.
- **Admin panel, People and Audit read real data** (issue #173): `GET /api/admin/people` returns the
  room's admin groups, operators, reader groups and Slack operators from the org keys; `GET
  /api/admin/audit` returns the store's audit events newest first with their `landed` state.
  The People pane decides the real group-request queue; both panes say where a list is edited.
- **Admin panel store** (issue #173, data model): two additive tables, `admin_audit_events` and
  `admin_settings`, JSONB bodies with a `spec_version`; `kobo_api.admin_store` is the one way to
  write and records an audit event in the same transaction. Migration `a1b2c3d4e5f6`.
- **The boot-time migration takes a Postgres advisory lock**, so two replicas on a rolling
  update never run `alembic upgrade head` at once (`migrations/env.py`). `docs/operating.md`
  records the two rules: migrations are additive, one pod migrates at a time.
- **Admin panel, Add-ons pane reads the room** (issue #173 step 2): `GET /api/admin/addons` returns one
  card per add-on (deploy platform, secrets, observability, switchboard, tool gateway, memory,
  model gateway) with its state derived from the org keys that turn it on; the pane shows the
  cards, the keys, and whose add-on it is. Admins only, and only while `admin_panel` is on.
- **Bundle publisher** (`kobo_api.bundle_publish`, issue #172 step 1): pushes an agent's `bundle/`
  as a registry artifact tagged by commit sha and moves the per-agent `:current` pointer, only
  when the bundle's git tree id changed. Not wired into any lane yet; the one-runtime design
  (`docs/superpowers/specs/2026-10-18-one-runtime-and-admin-panel-design.md`) says where it goes.

### Changed

- **No "platform" anywhere a person reads it** (PR #228). The admin page is **Services** (old
  `#/admin/addons` and `#/admin/platform` links still open it), the deploy add-on is
  Deployment, the build step is **Deployment setup**, the destroy stage is **Deployment
  teardown**, the secrets service is "the secrets service", and an agent's identity is its cloud
  identity. The guide and glossary follow. Internal ids (`destroy-platform`, the
  `deploy-platform` check id, the secrets panel's `platform` flag) are unchanged.
- **Teach this on a chat turn saves that bubble's own words** (PR #168, issue #123 items 12 and
  13). It used to prefill "You said: ... / agent replied: ..." from both sides. The saved
  message now names the file it wrote (`Saved as intake/<file>`).
- **Integrations ask each vendor for the credential it really uses.** AWS is a role the gateway
  assumes (ARN, optional external ID), Google Cloud is workload identity with an optional service
  account to act as, Azure is a federated app registration (client and tenant IDs), Datadog and
  Wiz are service-account key pairs, Atlassian, Fastly and Okta are API tokens. The connect body
  carries `auth`, `identity` (plain fields) and `secrets` (named values stored in the secrets
  service, one env each at the gateway); the gateway target gets `auth_method` and a
  `[target.auth]` table. A kind with no secret stores nothing.
- **The admin panel reads like a settings screen.** Every page is a title, one sentence, a toolbar
  and a table. Overview is one row per page, each a link with the number that matters, read from the
  room (the demo store is gone). Add-ons is now Services: a table of what the room runs on,
  with the secrets service named as OpenBao through sero, and Reload org config asks before it acts.
  The integration catalogue is this room's vendors (Atlassian, Datadog, Google Cloud, AWS, Azure,
  Wiz, Fastly, Okta, Other). Roles, People and Audit use the same table. The rail's "This room"
  facts come from a new `GET /api/admin/room` instead of placeholder text.
- README's "fail closed" bullet says what ships: an explicit tool allowlist, and with the
  switchboard on, colleague (and broker read) tools with no approval gate (issue #205).
  `docs/operating.md` says what the `kobo-room` ClusterRole really grants, cluster-wide Secret
  access included, and the per-namespace shape it should become (issue #204, docs half).
- **The vendored daedalus bundle schema is the v0.5.5 copy** (issue #152). It was 0.4.0.dev0's;
  the newer schema adds the `engine` block and the GitHub, CloudTrail, Jira and Slack-read tool
  specs, allows `${VAR}` placeholders and no longer requires a skill `id`. Every shape kobo
  composes still validates, and a test pins which copy is vendored.

- **A commit that changes only the deploy contract skips CI.** The environment and model
  changes commit `gojira.yaml` alone; that commit failed the generated contract check and
  the setup pull request Gojira opens on reprovision was the deploy anyway. The push now
  carries `[skip ci]`, the same as the archive commits (agora discussion #55, item 2).
- **Composed agents no longer declare `approvals.ceiling.write: false`.** The runtime
  enforces only `ceiling.exec`, so the line promised a limit nothing applied (agora
  discussion #55, item 7). Existing agents keep theirs until they are recomposed.
- **The Harness tab's Behind line shows the tag of a `tag@digest` pin**, the same rule as
  the agent card's `version`, instead of the digest (issue #158).

### Fixed

- **A destroy that refuses a namespace still revokes the agent's colleague pass** (issue #193,
  follow-up to PR #213). The namespace check ran before everything in the secrets stage, so a
  refusal also skipped the pass revocation. The check now runs first in that stage whatever the
  secrets platform's state; a namespace older than the agent, or a cluster the room cannot ask,
  keeps the stored secrets, still revokes the pass, and fails the stage, so the rest of the
  destroy waits for an operator. An unreachable cluster is now reported as "could not check the
  namespace" rather than as someone else's namespace.
- **A typo in the config can no longer turn authentication off** (issue #198). `Config` and its
  `mcp:` block refuse unknown keys instead of dropping them (`iap_audiance: x` used to run the
  room open and say nothing); the retired `runtimes` key is dropped with a warning so old files
  still boot. An MCP server on a room with no IAP is refused at construction unless
  `mcp.allow_without_iap: true` says so. Every boot logs one line naming the auth regime, and an
  ERROR for each accidental opening: no IAP without `allow_unauthenticated: true`, IAP with no
  operators and no admin groups, or groups configured with no directory resolver.
- **The room-wide colleagues routes follow agent visibility** (issue #192). `GET
  /api/colleagues/conversations`, `GET /api/colleagues/conversations/{task_id}` and `GET
  /api/colleagues/stream` were gated only on the switchboard being wired, so any signed-in user could
  list every switchboard conversation and read the full transcript of another person's chat with an
  agent they could not see. Now the list keeps only rows with a participant the caller can see, the
  detail answers 404 otherwise, and the stream needs an `?agent=` the caller can see (400 without one,
  404 for one they can't). Operators, readers, and rooms without IAP or groups see everything as
  before. The UI already passes `?agent=` everywhere, so it is unchanged.
- **An agent created or connected under `KOBO_ROLE=api` is provisioned** (issue #195). The create
  and connect routes stamped a heartbeat on the new row in every role, and a worker's `claim_queued`
  takes only rows with no heartbeat, so under the api/worker split no worker ever claimed the agent and
  the sweep later failed it as "interrupted by an API restart". The row is now born without a
  heartbeat when the route leaves it for a worker, as `docs/arch/provisioning.md` already said. Role
  `all`, the shipped default, is unchanged.
- **An empty dispatch-pair set no longer opens the switchboard** (issue #194). Cabal reads an
  empty pair table as "every agent may call every agent", and the room pushed an empty set
  whenever nothing was allowed: a room of commons agents, one agent per group with no invites,
  the last invite revoked, or one of two paired agents destroyed. The room now sends one
  sentinel pair, `kobo:deny-all` to `kobo:deny-all`, which no real agent can match, and logs a
  warning on every such push instead of only after a non-empty one in the same process. Syncs
  in one process now compute and push one at a time, so an older set cannot overwrite a newer
  one; pushes from separate processes or replicas are still unordered.
- **An agent can no longer take over, or on destroy delete, a namespace the room does not own**
  (issue #193). An agent's name is its Kubernetes namespace, and the deploy platform adopts a
  namespace that already exists. Create and connect now refuse a name whose namespace already
  exists (409), and answer 503 when the cluster cannot be asked. Destroy's secrets, platform and
  access stages first check the agent's namespace: one older than the agent's row (beyond two
  minutes of clock skew) was there before the agent, so the stage fails with nothing deleted and
  an operator has to look. The reserved names now also cover kube-node-lease, the suite's
  products (cabal, sero, liminal, noto, techne, daedalus, agora, kaminos, agents) and
  cert-manager, external-secrets and ingress-nginx.

## [0.2.2] - 2026-10-14

### Upgrading

- No database migration. The alembic head is still `a8e2c4f6d913` from 0.2.0.
- Nothing to change. To give a digest-pinned agent's card a readable `version`, re-pin its
  runtime as `repo:tag@sha256:...` (see Fixed).

### Changed

- **The in-app guide and the operating doc follow the release** (PR #153): guide 11 says an
  owner or an operator can **set runtime image** (it said operators only), guides 03 and 08
  follow the code, and `docs/operating.md` gains an Upgrading section (migrations run at
  boot, so back up first).
- **CONTRIBUTING: on macOS, put the GNU tools first on PATH** (PR #155).

### Fixed

- **A connected agent no longer loops on reformatting pull requests.** `ConnectedWireStep`
  decided whether there was wiring to propose by rewriting its files and comparing bytes, so
  a file that already carried the wiring in Gojira's own formatting came back changed. The
  step opened a pull request of pure reformatting, and Gojira reformatted it back. Both
  writers now leave a file alone when its parsed content is unchanged (PR #151, issue #123
  item 11).
- **The 0.2.1 note on the card's `version` was only true for a tag pin.** `version` is the
  runtime image's tag when the reference has one, including `repo:tag@sha256:...`. A
  digest-only reference names no version, and the runtime images carry no version label, so
  its card shows the digest's first 12 hex (`sha256:a0b09334649d`). To keep an exact build
  and a readable version, pin `tag@digest`, for the room's default and for **set runtime
  image** alike. The full reference stays in `metadata.runtime_image` (issue #156).

## [0.2.1] - 2026-10-14

### Upgrading

- No database migration. The alembic head is still `a8e2c4f6d913` from 0.2.0.
- Agent cards change shape. `capabilities` becomes an object (`{}`), not a list, and
  `version` becomes the runtime image's tag, not `0.1.0`. Anything that reads the card from
  an agent's repository or from the switchboard and expects a list there needs updating.
- Existing cards are corrected one at a time, on each agent's next skill change, runtime
  change, **Edit details** save or other kobo commit that moves `FROM`. The room doesn't
  rewrite them at boot, because each rewrite is a push and each push redeploys the agent.

### Fixed

- **An agent's directory card lists its current skills and says what the agent is.** The
  card's `skills` were written once, at compose, so a skill added or removed later never
  reached it or the switchboard's copy; every card also said "Assembled by kobo." at version
  0.1.0 (issue #147). The card's skills, description, version and runtime are now derived
  from the repository in the same commit as any skill change, runtime change or **Edit
  details** save, and the switchboard is sent the new card. That includes any kobo commit
  that moves the Dockerfile's `FROM` (a teach note, intake upload, tool upload or import, or
  Slack wiring after a room runtime upgrade). The skill and runtime routes' responses carry
  `directory` (`synced`, `failed` or `off`); the dashboard doesn't show it yet.
  - The description is the capability sentence, or the persona's first sentence until there
    is one.
  - `version` is the runtime image's tag, and `metadata.runtime_image` names the full image.
  - `capabilities` is `{}`, the object A2A defines, instead of a list.
  - An existing card is corrected by its next such change.
  - A ready agent with no purpose tags shows **no purpose tags** on its dashboard card.

## [0.2.0] - 2026-10-13

### Upgrading

- **This release migrates the database: back it up first.** Alembic revision
  `a8e2c4f6d913` adds one table, `agent_system_tasks`, and the room runs `alembic upgrade
  head` at boot. A version from before this one won't start on a database that has the
  revision, because its own upgrade can't locate it. To roll back, restore the backup or run
  `alembic downgrade e5a9c3f17b20` first.
- Leave `learnings_require_attribution` off until every agent runs a daedalus release that
  includes daedalus issue #165. Older runtimes send turns from the room's chat unattributed,
  and the switch would drop them.

### Fixed

- **The destroy confirm closes once the room accepts.** After the 202 the dialog stayed
  open with the name still typed, and a second click started a second destroy (issue #123,
  item 6).
- **A failing nightly learnings pass shows on the agent's card.** An agent whose runtime
  refused the task ("Unknown system task 'learnings'") failed every night and still looked
  healthy; the failure was only a log line (issue #142). The room now records each run per
  agent in the new `agent_system_tasks` table. After `system_task_flag_after` failures in a
  row (default 3), the card shows "learnings failing" and the Learnings section shows the
  last error.
- **Unattributed and access-related learnings need a second confirmation.** A nightly
  candidate whose quote only introduces the speaker ("i'm Ana ana@example.com") is dropped.
  A candidate nobody can be attributed with, or one about approving, granting or revoking
  access, is marked on the Learnings tab and is remembered only on a second click (API:
  `confirm: true`; `kobo_decide_learning` takes `confirm`). Remember all leaves those rows
  pending (issue #143). `learnings_require_attribution` drops unattributed candidates
  outright. Leave it off until every agent runs a daedalus with issue #165, which marks
  turns from the room's chat as `user`; before that, everything taught through the room's
  chat arrives unattributed and would be dropped too.
- **A connected agent no longer waits forever on an install with no Gojira webhook.** The
  waiting sweep read the platform's status, which moves only when the webhook reports the
  setup PR's merge, so the agent stayed at "waiting" after the merge (issue #118). The sweep
  now asks Gojira to check the pull request (`POST /api/v1/apps/{id}/sync`) before it reads
  the status. With a webhook the call is a no-op.
- **A re-pinned runtime image is named, not silent.** Intake, teach, tooling and Slack
  commits put the Dockerfile `FROM` back to the image the room has recorded for the agent,
  which reverted a hand-pinned `FROM` with nothing said (issue #140). The re-pin stays,
  because the recorded image is the authority, but the commit message and a warning log now
  name both images. Change an agent's image with `kobo_set_runtime_image`.
- **The GitHub token broker says why it has nothing to mint.** After the agent's own token
  verified, its 404 said "no such agent" (issue #11). The status is still 404, but the
  message now says whether repository access was never requested, is waiting for an
  operator, or was revoked, and names the route that fixes each.
- **"The switchboard is not configured" names the fix.** The 409, and the `kobo_feed` MCP
  tool's error for the same gate, now list the three settings it checks: `cabal_url` and `agent_endpoint_template` in the org config, and
  `CABAL_OPERATOR_TOKEN` in the room's environment (issue #123, item 1).
- **A waiting agent's card says what it waits on.** The agents list showed a bare
  `waiting` chip; the card now reads "Waiting on a pull request. Open for details."
  (issue #123, item 7).
- **Composed agents with skills now get `skills.load`.** daedalus 0.5.0 shows assigned
  skills as an index in the system prompt plus a `skills.load(name)` tool, but only when
  `tools.allow` admits the tool, and the composer never added it, so every composed agent
  with skills logged a warning at boot and lost the index (issue #137). The composer adds it
  for any agent born with skills, and the self-card names it. Adding a skill later, from the
  catalog or as a local skill, adds `skills.load` to `tools.allow` in the same commit when
  the allow list doesn't already admit it. An agent that already has skills and gets none
  added keeps its allow list: add `skills.load` to `tools.allow` in its `bundle/agent.yaml`.
  Older runtimes ignore the entry.
- **Changing an agent's colleague mode no longer locks its row across a GitHub push.** The
  route set `colleague_mode` before rendering the colleague document, so the roster query
  flushed the UPDATE and Postgres held the agent's row lock through the clone and push. The
  document now renders for the new mode without touching the row, and the row is written
  after the push lands (issue #133).
- **Colleague mode is documented.** The guide, glossary, README and `docs/what-the-room-does.md`
  said every agent asks its colleagues; they now describe `roster-only` and `none`, and the
  guide says where an owner sets the mode. The route and MCP tool docstrings, and a note under
  the Harness tab's Colleagues row, now say that `roster-only` also stops the agent answering
  from general knowledge. A stranger-gets-403 test now covers the colleague-mode, runtime,
  model and Slack wake routes.
- **An agent can be created again under a destroyed agent's name.** Destroy renames the
  repository to `archived-agent-<name>-<id>`, and GitHub keeps answering at the old name
  through a redirect; the room read that as "a repository already exists and is not
  recorded for this agent" and refused. The existence check now compares the name GitHub
  answers with, so a redirected name counts as free, which GitHub allows a new repository
  to take.

### Changed

- The generated deploy workflow passes `AR_TOKEN` to the image build as a BuildKit secret
  instead of a build argument, which the build log printed (PR #145).

## [0.1.0] - 2026-10-12

The first tagged release, `0.1.0`, is the room as the README describes it: create, connect,
chat and teach, memory, colleagues and threads, access, Slack, the agent page, personas and the
MCP surface. Everything before the tag is in the git history and in the design record under
[docs/superpowers/](docs/superpowers/). The entries below cover the changes after this
changelog started (PR #103).

### Added

- **The deploy workflow reads its registry, deploy identity and cluster from repository
  variables** (`GOJIRA_DEV_*`, PR #115) and fails fast when any is unset, so a fork runs it
  without editing the file.
- **Colleague mode, set per agent by its owner** (PR #132). `default` is unchanged: the agent
  has the colleague tools and is told to consult colleagues first. `roster-only` keeps the
  roster as knowledge but drops `ask_colleague`, and the agent answers from its own tools and
  memory and asks its human when those fall short. `none` removes the colleague tools and the
  roster. Other agents can still ask the agent in every mode. An owner sets it on the Harness
  tab, through the API or with `kobo_set_colleague_mode`; each change is logged with who
  made it and what it was before.
- **Slack wake modes** (PR #111). Each agent wakes on mentions only, on a mention and then the
  rest of that thread (the default for new agents), or on every message in the channel. A
  direct message always reaches it, and the agent is told its own setting.
- **DCO sign-off is checked on every pull request** (PRs #106 and #112). Authors with write
  access to the repository are exempt; CONTRIBUTING.md says how to sign off.
- **How this was built and the AI-tools policy** in the README and CONTRIBUTING.md, and a test
  that fails if a CI job on a self-hosted runner loses its fork guard (PR #120).
- A quickstart that runs from a clean clone, community files (code of conduct, support,
  issue and pull request templates, CODEOWNERS) and this changelog, ahead of going public.

### Changed

- **Two platform names.** The secrets platform the room hands per-agent Secrets to is
  **sero** (named ladon until 2026-10-12) and the observability platform it links to is
  **noto** (named argus). The deployment config keys are `sero_url`, `sero_audience`,
  `sero_namespace`; the org keys are `noto_url`, `noto_project`; the module is
  `kobo_api.sero`. The old key names are still honoured, each with a boot WARNING,
  so no running room changes behaviour before its config files catch up.
- **The API package is published as `tenish-kobo`** (PR #113). The import package
  (`kobo_api`) and the deployment name are unchanged.
- **The deploy contract names the switchboard token.** The room reads `CABAL_OPERATOR_TOKEN`
  from a Secret named `kobo-cabal` (PR #122), and Gojira now binds the room's ServiceAccount to
  the `kobo-room` ClusterRole through `cluster_roles` (PR #116); `k8s/cluster/rbac.yaml` keeps
  only the ClusterRole.
- The README's claims are narrowed to what the code does (PR #128).
- CI runs the architecture-card check, Dependabot leaves semver-major bumps to a person, and
  SQLAlchemy is held below 2.1 (PRs #108 and #114).
- `docs/superpowers/` carries a README saying the specs and plans are a historical record,
  not a description of the room today.
- CI installs from the committed `api/uv.lock` with `uv sync --locked`.
- The README is a two-screen read; the operator's material moved to `docs/operating.md` and the
  long feature list to `docs/what-the-room-does.md`, both verbatim.
