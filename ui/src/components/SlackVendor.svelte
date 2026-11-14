<script lang="ts">
  // Slack as a vendor on the Access tab (issue #371, phase 2 of #304): the
  // one vendor whose connection is per agent. The room makes the agent's
  // Slack app itself and relays Slack's events to the agent, so nobody
  // opens Slack's app site or pastes a token.
  //
  // Three states, read from `GET /api/agents/{name}/slack`:
  // - none: pick Wakes on (it decides which events the app subscribes to,
  //   so it is picked before the app exists), then Create Slack app;
  // - created: the app exists at Slack; Install to Slack opens Slack's own
  //   install page in this tab and Slack sends the person back here;
  // - installed: the workspace and app, Wakes on, and Remove. There is no
  //   channel list here (owner ruling, 2026-11-01): a room-made app is
  //   wired at Create to answer in every channel it is invited to, and
  //   inviting it in Slack is the only control. The legacy Slack tab keeps
  //   its own list and the channels route.
  //
  // The row's one line carries an honest state once installed: Live (the
  // agent is ready and nothing is pending), Applying your change (a Wakes
  // on save restarts the agent; the state turns Live only when a health
  // check taken after the save finds it up and not restarting, and a
  // short settle has passed, lib/slackWake; the same words while the
  // cluster says the agent is mid-restart for any reason), or the amber
  // note that a saved choice waits for the agent's next restart.
  //
  // One row among the Access tab's vendors (VendorRow, issue #382), always
  // the last. Whether it is open is PermissionsPanel's (one row at a time);
  // this component asks to open while something is in flight (installing,
  // applying a change) and to close once that reads Live, and otherwise
  // reports the click. The install's way back (`?vendor=slack`, with
  // `slack=failed` when it did not finish) is read by the panel too; the
  // failure sentence arrives here as a prop and sits on the row.
  //
  // Renders nothing unless the room makes Slack apps itself (the agent's
  // `slack.relay`); an agent with an app from the Slack tab's paste flow
  // (`legacy`) keeps that tab, and this row shows the old app with one
  // button, Move to the new Slack setup (issue #304, phase 3): the room
  // removes the old app, makes a new one and the row becomes the created
  // state above, Install to Slack and all. Channels and Wakes on carry over.
  //
  // An agent that runs its team's own code (issue #484) gets the same app
  // when it serves a port (`room_app`): the copy says what its code has to
  // serve and that it may keep an app of its own instead. The change to its
  // files is a pull request its team merges (`wiring`), so the row carries
  // the link while it waits and a button to open it again if it was closed;
  // a Wakes on pick is a pull request too, never a restart.
  //
  // What the room asks Slack to let an app do grows with releases, and Slack
  // grants it only when a person approves. An installed app that predates
  // the latest list (`update_due`, issue #484) keeps working and shows one
  // button, Update in Slack: the room refreshes the app, Slack shows what is
  // new, and the person comes back here as after an install.
  import { createMutation, createQuery, useQueryClient } from "@tanstack/svelte-query";
  import { errorMessage, type SlackWake, type SlackWakeResult } from "../lib/api";
  import {
    agentQuery,
    createSlackAppMutation,
    moveSlackAppMutation,
    removeSlackAppMutation,
    setSlackWakeEmojiMutation,
    setSlackWakeMutation,
    slackAppQuery,
    updateSlackAppMutation,
  } from "../lib/queries";
  import { beginRestart, endRestart } from "../lib/delivery.svelte";
  import { watchWakeLive } from "../lib/slackWake";
  import VendorRow, { type Tone } from "./VendorRow.svelte";

  let {
    agentName,
    frozen = false,
    open = false,
    lit = false,
    installFailed = false,
    onOpenChange,
  }: {
    agentName: string;
    frozen?: boolean;
    /** Whether this row is the open one; the panel decides. */
    open?: boolean;
    /** Brought into view and lit for a moment: the install's way back. */
    lit?: boolean;
    /** The install did not finish (`slack=failed` on the way back). */
    installFailed?: boolean;
    /** The row asks to open or shut; the panel keeps one row open. */
    onOpenChange?: (open: boolean) => void;
  } = $props();

  const queryClient = useQueryClient();
  // agentName is read once: AgentDetail remounts the tab's panels on a
  // route change, like every other panel here. The agent detail is read
  // from the cache the page already keeps fresh (`enabled: false`: never a
  // fetch of its own), so the Access tab stays a single read; with no
  // detail in hand the block stays hidden.
  const agent = createQuery(() => ({ ...agentQuery(agentName), enabled: false }));
  const relayOn = $derived(agent.data?.slack.relay === true);
  const view = createQuery(() => ({ ...slackAppQuery(agentName), enabled: relayOn }));
  const create = createMutation(() => createSlackAppMutation(queryClient, agentName));
  const remove = createMutation(() => removeSlackAppMutation(queryClient, agentName));
  const move = createMutation(() => moveSlackAppMutation(queryClient, agentName));
  const saveWake = createMutation(() => setSlackWakeMutation(queryClient, agentName));
  const saveEmoji = createMutation(() => setSlackWakeEmojiMutation(queryClient, agentName));
  const update = createMutation(() => updateSlackAppMutation(agentName));

  const WAKE_OPTIONS: { value: SlackWake; label: string; help: string }[] = [
    { value: "mention", label: "Mentions only", help: "Only a message that @-mentions the agent reaches it." },
    { value: "thread", label: "Mentions, then the thread", help: "Mention it once at the top of a thread and every reply there reaches it." },
    { value: "channel", label: "Every message in the channel", help: "For an agent whose job is the channel. It hears everything." },
  ];

  // --- Wakes on: local before the app exists, saved on pick after ----------
  //
  // A save reaches the running agent at once (the route restarts it with
  // the choice) and the header follows it: Applying your change while a
  // health check taken after the save that finds the agent up and not
  // restarting is awaited (plus a short settle), Live once it lands. The
  // page's header reads Restarting meanwhile (lib/delivery's restarts).
  // The choices stay locked meanwhile, so a second pick cannot
  // land while the first is still applying. When the live change was
  // refused (`applied_now` false) the choice is saved and waits for the
  // agent's next restart; the header says so, in amber, for as long as the
  // row is open, since the page cannot see that restart.
  let wake = $state<SlackWake | null>(null);
  const wakeShown = $derived<SlackWake>(wake ?? view.data?.wake ?? "thread");
  let wakeError = $state<string | null>(null);
  let wakeApplying = $state(false);
  let wakeWaitsForRestart = $state(false);
  let stopWakeWatch: (() => void) | null = null;
  const wakeLocked = $derived(frozen || saveWake.isPending || saveEmoji.isPending || create.isPending || wakeApplying);

  function watchWake(result: SlackWakeResult): void {
    stopWakeWatch?.();
    wakeApplying = true;
    beginRestart(agentName);
    const settle = () => {
      wakeApplying = false;
      stopWakeWatch = null;
      endRestart(agentName);
    };
    stopWakeWatch = watchWakeLive({
      baseline: agent.data?.health?.checked_at ?? null,
      since: result.applied_at ?? null,
      read: async () => (await queryClient.fetchQuery(agentQuery(agentName))).health,
      onLive: settle,
      onGiveUp: settle,
    });
  }
  $effect(() => () => {
    stopWakeWatch?.();
    endRestart(agentName);
  });

  async function pickWake(next: SlackWake): Promise<void> {
    if (wakeLocked || next === wakeShown) return;
    wake = next;
    wakeError = null;
    if (view.data?.state !== "installed") return;
    try {
      const result = await saveWake.mutateAsync(next);
      // A connected agent's choice is a pull request: the row's wiring line says so.
      wakeWaitsForRestart = !result.applied_now && !result.pull_request;
      if (result.applied_now) watchWake(result);
    } catch (err) {
      wake = null;
      wakeError = errorMessage(err, "Couldn't save how it wakes up. Try again.");
    }
  }

  // --- Also wakes on these emoji: one field, saved with its own button --------
  //
  // The second way in, beside the mode and whatever the mode is: a reaction
  // with one of these emoji wakes the agent about the message it was put on.
  // Empty means no reaction wakes it. A save restarts the agent like a Wakes
  // on pick does, so it shares that lock and that header.
  let emojiDraft = $state<string | null>(null);
  let emojiError = $state<string | null>(null);
  const emojiSaved = $derived((view.data?.wake_emoji ?? []).join(", "));
  const emojiShown = $derived(emojiDraft ?? emojiSaved);
  const emojiDirty = $derived(emojiDraft !== null && emojiDraft.trim() !== emojiSaved);

  async function handleEmoji(event: SubmitEvent): Promise<void> {
    event.preventDefault();
    if (wakeLocked || !emojiDirty) return;
    emojiError = null;
    try {
      const result = await saveEmoji.mutateAsync(emojiShown.split(/[\s,]+/).filter(Boolean));
      // What the room kept: its own spelling of each name, each once.
      emojiDraft = (result.wake_emoji ?? []).join(", ");
      wakeWaitsForRestart = !result.applied_now && !result.pull_request;
      if (result.applied_now) watchWake(result);
    } catch (err) {
      emojiError = errorMessage(err, "Couldn't save the emoji. Try again.");
    }
  }

  // --- Create and Install ------------------------------------------------------
  let createError = $state<string | null>(null);
  let installing = $state(false);

  async function handleCreate(): Promise<void> {
    createError = null;
    try {
      await create.mutateAsync(wakeShown);
    } catch (err) {
      createError = errorMessage(err, "Couldn't create the Slack app. Try again.");
    }
  }

  // --- Move from the old setup (issue #304, phase 3) ----------------------------
  //
  // One press. The route removes the old app, makes the new one and answers
  // like Create; the queries refresh and the row reads "App made; install it
  // to finish". When the room could not remove the old app (it may already
  // be gone, or belong to a token the room never held) the answer says so
  // and the note stays on the row until the person leaves it.
  let moveError = $state<string | null>(null);
  let oldAppKept = $state(false);

  async function handleMove(): Promise<void> {
    moveError = null;
    try {
      const result = await move.mutateAsync();
      oldAppKept = !result.old_app_removed;
    } catch (err) {
      moveError = errorMessage(err, "Couldn't move the Slack setup. Try again.");
    }
  }

  // --- Update in Slack (issue #484) ----------------------------------------------
  //
  // One press, then Slack's own page in this tab, like Install to Slack. The
  // link exists only once the room has refreshed the app, so it is asked for
  // here and followed, never drawn as a link.
  let updateError = $state<string | null>(null);
  let updating = $state(false);

  async function handleUpdate(): Promise<void> {
    updateError = null;
    try {
      const result = await update.mutateAsync();
      updating = true;
      window.location.assign(result.install_url);
    } catch (err) {
      updateError = errorMessage(err, "Couldn't start the update. Try again.");
    }
  }

  // --- Remove, in two presses ----------------------------------------------------
  let removing = $state(false);
  let removeError = $state<string | null>(null);

  async function handleRemove(): Promise<void> {
    removeError = null;
    try {
      await remove.mutateAsync();
      removing = false;
    } catch (err) {
      removeError = errorMessage(err, "Couldn't remove the Slack app. Try again.");
    }
  }

  // --- The row's one line ------------------------------------------------------
  const headState = $derived.by((): { text: string; tone: Tone } => {
    const data = view.data;
    if (!data) return { text: "", tone: "ink" };
    if (data.own_app && !(data.room_app && data.available)) return { text: "This agent brings its own Slack app", tone: "ink" };
    if (!data.available) return { text: "Not connected to the room", tone: "ink" };
    if (data.legacy) {
      return move.isPending
        ? { text: "Moving", tone: "warn" }
        : { text: `Connected the old way${data.app_id ? `: ${data.app_id}` : ""}`, tone: "ink" };
    }
    if (data.state === "created") {
      return installing && !installFailed
        ? { text: "Installing", tone: "warn" }
        : { text: "App made; install it to finish", tone: "ink" };
    }
    if (data.state !== "installed") return { text: "Not connected for this agent", tone: "ink" };
    if (data.wiring?.state === "waiting") return { text: "Waiting for its team to merge a pull request", tone: "warn" };
    if (data.wiring?.state === "owed") return { text: "Its pull request was closed", tone: "warn" };
    if (wakeApplying) return { text: "Applying your change. Give it a minute.", tone: "warn" };
    if (wakeWaitsForRestart) return { text: "Saved. Takes effect when the agent next restarts.", tone: "warn" };
    const detail = agent.data;
    // The wiring commit started a build that has not rolled: the agent still
    // runs without its Slack app, about three minutes. The room retries a
    // mention for about four minutes, so one sent now is usually answered
    // once the agent is back; later ones are not.
    if (data.rebuilding) return { text: "Rebuilding with its Slack app, about three minutes. A mention sent now is kept and answered once it is back.", tone: "warn" };
    // Mid-restart by the cluster's own word: a change is going in, not an
    // outage. After Install to Slack this is the agent rebuilding with its
    // token, about two minutes; the room keeps a mention sent meanwhile and
    // hands it over once the agent is back.
    if (detail?.health?.rolling) return { text: "Restarting. A mention sent now is answered once it is back.", tone: "warn" };
    if (detail?.health?.state === "down") return { text: "Down", tone: "fail" };
    if (detail?.status === "ready") return { text: "Live", tone: "ok" };
    return { text: "Not ready yet", tone: "warn" };
  });
  /** The one action while closed: adding Slack, until the app is installed;
   * after that, only an update the app is owed. */
  const action = $derived(
    view.data?.available && !view.data.legacy && (!view.data.own_app || view.data.room_app) && (view.data.state === "none" || view.data.state === "created")
      ? "Add Slack"
      : view.data?.available && view.data.state === "installed" && view.data.update_due
        ? "Update"
        : null,
  );

  // Something is going in: the row opens itself so the person sees it land,
  // and shuts again once it reads Live. A row the person opened and left
  // alone is theirs to shut.
  const inFlight = $derived(
    wakeApplying || (installing && view.data?.state === "created") || Boolean(view.data?.rebuilding) || Boolean(agent.data?.health?.rolling),
  );
  let selfOpened = false;
  $effect(() => {
    if (inFlight) {
      if (!open) {
        selfOpened = true;
        onOpenChange?.(true);
      } else if (!selfOpened) {
        selfOpened = true;
      }
      return;
    }
    if (selfOpened && headState.tone === "ok") {
      selfOpened = false;
      if (open) onOpenChange?.(false);
    }
  });
</script>

{#snippet wakeChoice()}
  <fieldset class="wake" disabled={wakeLocked}>
    <legend class="label">Wakes on</legend>
    <div class="wake__options">
      {#each WAKE_OPTIONS as option (option.value)}
        <label class="wake__option" class:wake__option--on={wakeShown === option.value}>
          <input
            type="radio"
            name="slack-vendor-wake"
            value={option.value}
            checked={wakeShown === option.value}
            disabled={wakeLocked}
            onchange={() => pickWake(option.value)}
          />
          <span class="wake__text">
            <span class="wake__label">{option.label}</span>
            <span class="help">{option.help}</span>
          </span>
        </label>
      {/each}
    </div>
    {#if wakeError}
      <p class="copy copy--fail" role="alert">{wakeError}</p>
    {:else}
      <p class="help">A direct message always reaches the agent.</p>
    {/if}
  </fieldset>
{/snippet}

{#if relayOn && view.data}
  {@const data = view.data}
  <VendorRow
    id="slack"
    label="Slack"
    summary={headState.text}
    tone={headState.tone}
    live
    {action}
    {open}
    {lit}
    onToggle={() => onOpenChange?.(!open)}
  >
    <div class="body">
      {#if installFailed && data.state !== "installed"}
        <p class="copy copy--fail" role="alert">Installing to Slack did not finish. Try Install to Slack again.</p>
      {:else if installFailed && data.update_due}
        <p class="copy copy--fail" role="alert">The update did not finish in Slack. Try Update in Slack again.</p>
      {/if}

      {#if oldAppKept && data.state !== "installed"}
        <p class="copy">
          The room could not confirm the old app was removed. If it is still listed in Slack, remove it there.
        </p>
      {/if}

      {#if data.wiring?.state === "waiting"}
        <p class="copy">
          The change to this agent's files is waiting in a pull request. Its team merges it, then the one the deploy
          platform sends after it; the agent restarts with its Slack app.
          {#if data.wiring.pull_request}<a href={data.wiring.pull_request} target="_blank" rel="noreferrer">Open the pull request</a>{/if}
        </p>
      {:else if data.wiring?.state === "owed"}
        <p class="copy">The pull request that lets this agent read its Slack app was closed before it was merged.</p>
        <div class="row">
          <button type="button" class="btn" disabled={frozen || create.isPending} onclick={handleCreate}>
            {create.isPending ? "Opening…" : "Open it again"}
          </button>
          {#if createError}
            <span class="copy copy--fail" role="alert">{createError}</span>
          {/if}
        </div>
      {/if}

      {#if data.own_app && !(data.room_app && data.available)}
        <!-- A connected agent the room cannot make an app for: it serves no
             port for Slack's messages, or the room has no Slack connection. -->
        <p class="copy">
          This agent runs its team's own code, so it brings its own Slack app: the room does not make one for it.
          Create the app in Slack, then save its tokens under Secrets on this page. The agent restarts with them.
        </p>
      {:else if !data.available}
        <p class="copy">Ask a kobo admin to add the Slack connection under Admin › Integrations. Nothing here works until then.</p>
      {:else if data.legacy}
        <p class="copy">
          This agent's Slack app was set up on the Slack tab. The room makes a new app and you install it once; the
          old app is removed. Channels and Wakes on carry over.
        </p>
        <div class="row">
          <button type="button" class="btn btn--primary" disabled={frozen || move.isPending} onclick={handleMove}>
            {move.isPending ? "Moving…" : "Move to the new Slack setup"}
          </button>
          {#if moveError}
            <span class="copy copy--fail" role="alert">{moveError}</span>
          {/if}
        </div>
      {:else if data.state === "none"}
        {#if data.own_app}
          <p class="copy">
            This agent runs its team's own code. The room can make it a Slack app: you install it once, the room hands
            Slack's messages to the agent, and the change to its files arrives as a pull request for its team to merge.
            The agent has to take those messages; "Running your own code" in the guide says how.
          </p>
          <p class="copy">
            Already has a Slack app of its own? Keep it: save its tokens under Secrets on this page and skip this.
          </p>
        {:else}
          <p class="copy">
            The room makes a Slack app for this agent and connects it. Pick how it wakes up first: the choice decides
            which messages Slack sends it.
          </p>
        {/if}
        {@render wakeChoice()}
        <div class="row">
          <button type="button" class="btn btn--primary" disabled={frozen || create.isPending} onclick={handleCreate}>
            {#if create.isPending}<span class="slack-vendor__dot pulse" aria-hidden="true"></span>Creating…{:else}Create Slack app{/if}
          </button>
          {#if createError}
            <span class="copy copy--fail" role="alert">{createError}</span>
          {/if}
        </div>
      {:else if data.state === "created"}
        <p class="copy">
          The app exists at Slack. Install it to your workspace to finish; you come back here when it is done.
        </p>
        <div class="row">
          {#if data.install_url}
            <a class="btn btn--primary" href={data.install_url} onclick={() => (installing = true)}>Install to Slack</a>
          {:else}
            <span class="copy">The agent's owner or a room operator installs it.</span>
          {/if}
          {#if !removing}
            <button type="button" class="btn btn--quiet" disabled={frozen || remove.isPending} onclick={() => (removing = true)}>Remove</button>
          {/if}
        </div>
      {:else}
        <dl class="facts">
          <div><dt>Workspace</dt><dd>{data.workspace ?? "Unknown"}</dd></div>
          <div><dt>App</dt><dd>{data.app_name ?? agentName}</dd></div>
        </dl>
        <p class="copy">Invite @{agentName} to a channel in Slack and it answers there.</p>
        {#if data.update_due}
          <div class="block">
            <p class="copy">
              This app was installed before the room's latest Slack permissions, so the agent cannot yet do
              everything a new one can. Update it: Slack shows what is new and asks you to allow it, then brings you
              back here. The agent keeps answering until then and restarts once after.
            </p>
            <div class="row">
              <button type="button" class="btn btn--primary" disabled={frozen || update.isPending || updating} onclick={handleUpdate}>
                {update.isPending || updating ? "Opening Slack…" : "Update in Slack"}
              </button>
              {#if updateError}
                <span class="copy copy--fail" role="alert">{updateError}</span>
              {/if}
            </div>
          </div>
        {/if}
        <div class="block">
          {@render wakeChoice()}
        </div>
        <form class="field" onsubmit={handleEmoji}>
          <label class="field__label" for="slack-vendor-emoji">Also wakes on these emoji</label>
          <div class="field__row">
            <input
              id="slack-vendor-emoji"
              class="field__input"
              type="text"
              autocomplete="off"
              spellcheck="false"
              placeholder="ticket, rotating_light"
              value={emojiShown}
              disabled={wakeLocked}
              oninput={(event) => (emojiDraft = event.currentTarget.value)}
            />
            <button type="submit" class="btn" disabled={wakeLocked || !emojiDirty}>
              {saveEmoji.isPending ? "Saving…" : "Save"}
            </button>
          </div>
          {#if emojiError}
            <p class="copy copy--fail" role="alert">{emojiError}</p>
          {:else}
            <p class="help">
              When someone adds one of these to a message in a channel the agent is in, it reads that message and
              answers in its thread. Use the names Slack shows, with commas between them. Leave it empty and no
              reaction wakes it.
            </p>
          {/if}
        </form>
        {#if !removing}
          <div class="row">
            <button type="button" class="btn btn--quiet" disabled={frozen || remove.isPending} onclick={() => (removing = true)}>Remove</button>
          </div>
        {/if}
      {/if}

      {#if removing}
        <div class="remove" role="group" aria-label="Remove the Slack app">
          <p class="copy">Remove the Slack app? It is deleted at Slack and the agent stops answering there.</p>
          <div class="row">
            <button type="button" class="btn btn--danger" disabled={frozen || remove.isPending} onclick={handleRemove}>
              {remove.isPending ? "Removing…" : "Remove the Slack app"}
            </button>
            <button type="button" class="btn" disabled={remove.isPending} onclick={() => (removing = false)}>Keep it</button>
          </div>
          {#if removeError}
            <p class="copy copy--fail" role="alert">{removeError}</p>
          {/if}
        </div>
      {/if}
    </div>
  </VendorRow>
{/if}

<style>
  .body { display: flex; flex-direction: column; gap: var(--space-md); padding: var(--space-md) var(--space-md) 0; }
  .block { display: flex; flex-direction: column; gap: var(--space-sm); }
  .copy { margin: 0; color: var(--ink); font-size: 0.9rem; line-height: 1.5; }
  .copy--fail { color: var(--fail); }
  .help { color: var(--ink); font-size: 0.82rem; line-height: 1.45; }
  .label { margin: 0; padding: 0; color: var(--ink); font-size: 0.85rem; font-weight: 600; }
  .row { display: flex; flex-wrap: wrap; align-items: center; gap: var(--space-sm); }
  /* The in-progress dot every other panel shows while the room works (chat's thinking,
     the memory and skills deliveries): the Create button carries it while the app is made. */
  .slack-vendor__dot {
    display: inline-block;
    width: 0.5rem;
    height: 0.5rem;
    margin-right: var(--space-2xs);
    border-radius: 50%;
    background: var(--pulse);
    vertical-align: middle;
  }
  .facts { display: grid; grid-template-columns: max-content 1fr; gap: var(--space-2xs) var(--space-md); margin: 0; }
  .facts div { display: contents; }
  .facts dt { color: var(--muted); font-size: 0.82rem; }
  .facts dd { margin: 0; color: var(--ink); font-size: 0.9rem; }
  .wake { display: flex; flex-direction: column; gap: var(--space-xs); min-width: 0; margin: 0; padding: 0; border: 0; }
  .wake__options { display: flex; flex-direction: column; gap: var(--space-xs); }
  .wake__option { display: grid; grid-template-columns: auto 1fr; align-items: start; column-gap: var(--space-sm); padding: var(--space-sm) var(--space-md); border: 1px solid var(--border); border-radius: var(--radius-sm); background: var(--panel); cursor: pointer; transition: border-color 0.15s ease, background 0.15s ease; }
  .wake__option:hover { border-color: var(--ring); }
  .wake__option--on { border-color: var(--accent); background: var(--accent-soft); }
  .wake__option input { margin: 0.2rem 0 0; accent-color: var(--accent); }
  .wake__text { display: flex; min-width: 0; flex-direction: column; gap: 0.1rem; }
  .wake__label { color: var(--ink); font-size: 0.9rem; font-weight: 500; }
  .btn--danger { color: var(--danger, var(--fail)); }
  .remove { display: flex; flex-direction: column; gap: var(--space-sm); padding: var(--space-sm) var(--space-md); border: 1px solid var(--border); border-radius: var(--radius-sm); }
</style>
