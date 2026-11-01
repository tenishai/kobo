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
  // - installed: the workspace and app, the channels (the editor the Slack
  //   tab has, but with the open default: a room-made app is wired at Create
  //   to answer in every channel it is invited to, and the list only narrows
  //   it; clearing the list opens it again), Wakes on, and Remove.
  //
  // Renders nothing unless the room makes Slack apps itself (the agent's
  // `slack.relay`); an agent with an app from the Slack tab's paste flow
  // keeps that tab and this block says so instead of offering a second app.
  import { Hash, Plus, X } from "phosphor-svelte";
  import { createMutation, createQuery, useQueryClient } from "@tanstack/svelte-query";
  import { errorMessage, type SlackWake } from "../lib/api";
  import {
    agentQuery,
    createSlackAppMutation,
    removeSlackAppMutation,
    saveSlackChannelsMutation,
    setSlackWakeMutation,
    slackAppQuery,
  } from "../lib/queries";
  import { clearHashQuery, hashQuery } from "../lib/router.svelte";
  import VendorMark from "./VendorMark.svelte";

  let { agentName, frozen = false }: { agentName: string; frozen?: boolean } = $props();

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
  const saveChannels = createMutation(() => saveSlackChannelsMutation(queryClient, agentName));
  const saveWake = createMutation(() => setSlackWakeMutation(queryClient, agentName));

  const WAKE_OPTIONS: { value: SlackWake; label: string; help: string }[] = [
    { value: "mention", label: "Mentions only", help: "Only a message that @-mentions the agent reaches it." },
    { value: "thread", label: "Mentions, then the thread", help: "Mention it once at the top of a thread and every reply there reaches it." },
    { value: "channel", label: "Every message in the channel", help: "For an agent whose job is the channel. It hears everything." },
  ];
  const MAX_CHANNELS = 8;

  // Slack sends the person back here after Install to Slack with
  // `vendor=slack` in the hash, plus `slack=failed` when the install did not
  // finish. Both are read once at mount, the way the router's panes do: the
  // section scrolls into view and lights up for a moment, the failure
  // sentence sits on the row itself, and the query is dropped so a reload
  // or a copied link does not replay it.
  const arrival = typeof window !== "undefined" ? hashQuery(window.location.hash) : {};
  const installFailed = arrival.slack === "failed";
  const arrived = arrival.vendor === "slack" || installFailed;
  let highlight = $state(false);
  let sectionEl = $state<HTMLElement | null>(null);
  let brought = false;
  $effect(() => {
    if (!arrived || brought || !sectionEl) return;
    brought = true;
    sectionEl.scrollIntoView({ block: "start", behavior: "smooth" });
    clearHashQuery();
    highlight = true;
    const timer = setTimeout(() => (highlight = false), 2500);
    return () => clearTimeout(timer);
  });

  // --- Wakes on: local before the app exists, saved on pick after ----------
  let wake = $state<SlackWake | null>(null);
  const wakeShown = $derived<SlackWake>(wake ?? view.data?.wake ?? "thread");
  let wakeNote = $state<string | null>(null);

  async function pickWake(next: SlackWake): Promise<void> {
    if (frozen || next === wakeShown) return;
    wake = next;
    wakeNote = null;
    if (view.data?.state !== "installed") return;
    try {
      await saveWake.mutateAsync(next);
      wakeNote = "Saved.";
    } catch (err) {
      wake = null;
      wakeNote = errorMessage(err, "Couldn't save how it wakes up. Try again.");
    }
  }

  // --- Create and Install ------------------------------------------------------
  let createError = $state<string | null>(null);

  async function handleCreate(): Promise<void> {
    createError = null;
    try {
      await create.mutateAsync(wakeShown);
    } catch (err) {
      createError = errorMessage(err, "Couldn't create the Slack app. Try again.");
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
      rows = [];
      baseChannels = [];
    } catch (err) {
      removeError = errorMessage(err, "Couldn't remove the Slack app. Try again.");
    }
  }

  // --- The channels draft (the Slack tab's editor, same rules) ------------------
  type Row = { id: number; value: string };
  let nextRowId = 0;
  let rows = $state<Row[]>([]);
  let baseChannels = $state<string[]>([]);
  let seeded = $state(false);
  let focusId = $state<number | null>(null);
  let saveError = $state<string | null>(null);
  let saveNote = $state<string | null>(null);

  // Seeded once from the server's list, not on every refetch: this is a
  // draft a person edits, and a poll must not overwrite it mid-edit.
  $effect(() => {
    const data = view.data;
    if (data && !seeded) {
      rows = data.channels.map((value) => ({ id: nextRowId++, value }));
      baseChannels = [...data.channels];
      seeded = true;
    }
  });

  const cleaned = $derived(rows.map((row) => row.value.trim()).filter((value) => value.length > 0));
  const channelsPending = $derived(
    cleaned.length !== baseChannels.length || cleaned.some((value, i) => value !== baseChannels[i]),
  );

  function addRow(): void {
    if (frozen || rows.length >= MAX_CHANNELS) return;
    const row: Row = { id: nextRowId++, value: "" };
    rows = [...rows, row];
    focusId = row.id;
  }
  function removeRow(id: number): void {
    if (frozen) return;
    rows = rows.filter((row) => row.id !== id);
  }
  function discard(): void {
    rows = baseChannels.map((value) => ({ id: nextRowId++, value }));
    focusId = null;
    saveError = null;
  }
  function focusOnMount(node: HTMLInputElement, wanted: boolean) {
    if (wanted) node.focus();
  }

  async function handleSaveChannels(): Promise<void> {
    saveError = null;
    saveNote = null;
    try {
      const result = await saveChannels.mutateAsync([...cleaned]);
      baseChannels = [...(result.channels ?? [])];
      rows = baseChannels.map((value) => ({ id: nextRowId++, value }));
      saveNote =
        baseChannels.length > 0
          ? "Saved. The agent answers in these channels once it has picked the change up."
          : "Saved. The agent answers in every channel it is invited to once it has picked the change up.";
    } catch (err) {
      saveError = errorMessage(err, "Couldn't save the channels. Try again.");
    }
  }

  const note = $derived.by(() => {
    const data = view.data;
    if (!data) return "";
    if (!data.available) return "Not connected to the room";
    if (data.legacy) return "Connected from the Slack tab";
    if (data.state === "installed") return data.workspace ? `Installed in ${data.workspace}` : "Installed";
    if (data.state === "created") return "App made; install it to finish";
    return "Not connected for this agent";
  });
</script>

{#snippet wakeChoice()}
  <fieldset class="wake" disabled={frozen || saveWake.isPending || create.isPending}>
    <legend class="label">Wakes on</legend>
    <div class="wake__options">
      {#each WAKE_OPTIONS as option (option.value)}
        <label class="wake__option" class:wake__option--on={wakeShown === option.value}>
          <input
            type="radio"
            name="slack-vendor-wake"
            value={option.value}
            checked={wakeShown === option.value}
            disabled={frozen || saveWake.isPending || create.isPending}
            onchange={() => pickWake(option.value)}
          />
          <span class="wake__text">
            <span class="wake__label">{option.label}</span>
            <span class="help">{option.help}</span>
          </span>
        </label>
      {/each}
    </div>
    {#if wakeNote}
      <p class="copy" role="status">{wakeNote}</p>
    {:else}
      <p class="help">A direct message always reaches the agent.</p>
    {/if}
  </fieldset>
{/snippet}

{#snippet channelsEditor()}
  <div class="channels">
    <p class="copy">
      Invite @{agentName} to any channel in Slack and it answers there. To limit it to certain channels, list them here.
    </p>
    <ul class="channel-list">
      {#if rows.length === 0}
        <li class="channel-empty">Answers in every channel it is invited to.</li>
      {/if}
      {#each rows as row, index (row.id)}
        <li class="channel">
          <Hash class="channel__hash" size={14} aria-hidden="true" />
          <input
            class="channel__input"
            type="text"
            autocomplete="off"
            spellcheck="false"
            placeholder="C0123456789"
            aria-label={`Channel ${index + 1}`}
            bind:value={row.value}
            disabled={frozen}
            use:focusOnMount={row.id === focusId}
          />
          <button
            type="button"
            class="channel__remove"
            aria-label={row.value.trim().length > 0 ? `Remove ${row.value.trim()}` : `Remove row ${index + 1}`}
            disabled={frozen}
            onclick={() => removeRow(row.id)}
          >
            <X size={14} aria-hidden="true" />
          </button>
        </li>
      {/each}
    </ul>
    <div class="row">
      <button type="button" class="btn channels__add" disabled={frozen || rows.length >= MAX_CHANNELS} onclick={addRow}>
        <Plus size={13} weight="bold" aria-hidden="true" />
        Add channel
      </button>
      {#if channelsPending}
        <button type="button" class="btn btn--primary" disabled={frozen || saveChannels.isPending} onclick={handleSaveChannels}>
          {saveChannels.isPending ? "Saving…" : "Save channels"}
        </button>
        <button type="button" class="btn" disabled={frozen || saveChannels.isPending} onclick={discard}>Discard</button>
      {/if}
    </div>
    {#if saveError}
      <p class="copy copy--fail" role="alert">{saveError}</p>
    {:else if saveNote}
      <p class="copy" role="status">{saveNote}</p>
    {/if}
  </div>
{/snippet}

{#if relayOn && view.data}
  {@const data = view.data}
  <section class="vendor" class:vendor--arrived={highlight} aria-label="Slack" bind:this={sectionEl}>
    <div class="vendor__head">
      <VendorMark id="slack" name="Slack" size={18} />
      <span class="vendor__name">Slack</span>
      <span class="vendor__note">{note}</span>
    </div>

    <div class="body">
      {#if installFailed && data.state !== "installed"}
        <p class="copy copy--fail" role="alert">Installing to Slack did not finish. Try Install to Slack again.</p>
      {/if}

      {#if !data.available}
        <p class="copy">Ask a room admin to add the Slack integration under Admin › Integrations. Nothing here works until then.</p>
      {:else if data.legacy}
        <p class="copy">This agent's Slack app was set up on the Slack tab and keeps working there.</p>
      {:else if data.state === "none"}
        <p class="copy">
          The room makes a Slack app for this agent and connects it. Pick how it wakes up first: the choice decides
          which messages Slack sends it.
        </p>
        {@render wakeChoice()}
        <div class="row">
          <button type="button" class="btn btn--primary" disabled={frozen || create.isPending} onclick={handleCreate}>
            {create.isPending ? "Creating…" : "Create Slack app"}
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
            <a class="btn btn--primary" href={data.install_url}>Install to Slack</a>
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
        <div class="block">
          <h4 class="label">Channels</h4>
          {@render channelsEditor()}
        </div>
        <div class="block">
          {@render wakeChoice()}
        </div>
        {#if !removing}
          <div class="row">
            <button type="button" class="btn btn--quiet" disabled={frozen || remove.isPending} onclick={() => (removing = true)}>Remove</button>
          </div>
        {/if}
      {/if}

      {#if removing}
        <div class="remove" role="group" aria-label="Remove the Slack app">
          <p class="copy">
            Remove the Slack app? It is deleted at Slack and the agent stops answering there. The channel list stays
            for next time.
          </p>
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
  </section>
{/if}

<style>
  .vendor { display: flex; flex-direction: column; border: 1px solid var(--border); border-radius: var(--radius); background: var(--panel); overflow: hidden; transition: border-color 0.6s ease, box-shadow 0.6s ease; }
  .vendor--arrived { border-color: var(--accent); box-shadow: 0 0 0 3px var(--accent-soft); }
  .vendor__head { display: flex; align-items: center; gap: var(--space-sm); width: 100%; padding: var(--space-sm) var(--space-md); border-bottom: 1px solid var(--border); background: var(--bg); color: var(--ink); text-align: left; }
  .vendor__name { font-size: 0.95rem; font-weight: 600; }
  .vendor__note { flex: 1; min-width: 0; color: var(--muted); font-size: 0.82rem; }
  .body { display: flex; flex-direction: column; gap: var(--space-md); padding: var(--space-md); }
  .block { display: flex; flex-direction: column; gap: var(--space-sm); }
  .copy { margin: 0; color: var(--ink); font-size: 0.9rem; line-height: 1.5; }
  .copy--fail { color: var(--fail); }
  .help { color: var(--ink); font-size: 0.82rem; line-height: 1.45; }
  .label { margin: 0; padding: 0; color: var(--ink); font-size: 0.85rem; font-weight: 600; }
  .row { display: flex; flex-wrap: wrap; align-items: center; gap: var(--space-sm); }
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
  .channels { display: flex; flex-direction: column; gap: var(--space-sm); }
  .channel-list { display: flex; flex-direction: column; margin: 0; padding: 0; overflow: hidden; border: 1px solid var(--border); border-radius: var(--radius-sm); list-style: none; }
  .channel, .channel-empty { display: flex; align-items: center; gap: var(--space-xs); min-height: 2.6rem; padding: 0 var(--space-xs) 0 var(--space-sm); }
  .channel + .channel { border-top: 1px solid var(--border); }
  .channel-empty { color: var(--ink); font-size: 0.85rem; }
  .channel :global(.channel__hash) { flex-shrink: 0; color: var(--muted); }
  .channel__input { flex: 1; min-width: 0; padding: var(--space-2xs) var(--space-xs); border: 1px solid transparent; border-radius: 0.35rem; background: transparent; color: var(--ink); font-family: var(--font-display-mono); font-size: 0.82rem; }
  .channel__input:hover:not(:disabled) { background: var(--bg); }
  .channel__input:focus-visible { background: var(--bg); outline: none; box-shadow: 0 0 0 2px color-mix(in oklab, var(--ring) 40%, transparent); }
  .channel__remove { display: inline-flex; align-items: center; justify-content: center; width: 1.75rem; height: 1.75rem; border: none; border-radius: 0.35rem; background: transparent; color: var(--muted); cursor: pointer; }
  .channel__remove:hover:not(:disabled) { background: color-mix(in srgb, var(--fail) 10%, transparent); color: var(--fail); }
  .channel__remove:disabled { opacity: 0.4; cursor: not-allowed; }
  .channels__add { display: inline-flex; align-items: center; gap: 0.35rem; }
  .btn--danger { color: var(--danger, var(--fail)); }
  .remove { display: flex; flex-direction: column; gap: var(--space-sm); padding: var(--space-sm) var(--space-md); border: 1px solid var(--border); border-radius: var(--radius-sm); }
</style>
