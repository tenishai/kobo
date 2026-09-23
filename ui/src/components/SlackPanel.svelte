<script lang="ts">
  // Slack, per agent (compacted 2026-09-23) -- the channels the agent
  // joins, and the one-time ceremony that creates the Slack app.
  //
  // ONE table, one disclosure, one Save. The first cut showed the whole
  // ceremony at every state: nine numbered steps, a manifest, three paste
  // fields and a second filled button, all still on screen for an agent
  // that had been talking in Slack for a month. The setup is a thing you
  // do once, so it lives behind a disclosure that is open while the agent
  // is not connected and shut once it is. The state itself is a chip beside
  // the heading rather than a sentence under it.
  //
  // Both lanes read the same `slack` prop (`AgentDetail.slack`, refetched
  // by the caller's agentQuery -- see AgentDetail.svelte): `channels` and
  // `state` say what is saved and how wired-up it is. Neither lane holds a
  // "which phase am I in" flag of its own beyond the disclosure's initial
  // position, so a save/connect success (which invalidates the agent detail
  // query) advances the card for free once the refetch lands.
  //
  // WHAT SAVE SENDS. The two calls behind this panel are unchanged: PUT
  // .../slack/channels with the cleaned channel list, and POST
  // .../slack/connect with the app id and both tokens. Save sends whichever
  // of the two has something to send, and sends the channels FIRST when it
  // sends both -- connect refuses an agent with no channel, and a person
  // who filled in everything at once should not have to press Save twice to
  // get past a precondition they already satisfied.
  //
  // Edits are PENDING until Save, the same discipline as SecretsPanel:
  // adding a row, typing in one and removing one all change a draft, and
  // Discard throws the draft away and puts the saved list back.
  //
  // The one thing that *is* local state independent of `slack` is the
  // delivery chip's sha store (shared by both calls), and it's deliberately
  // NOT reset when `slack` changes shape -- a save that just fired keeps its
  // chip alive across the next refetch. See MemoryPanel's doc comment for
  // why this is a plain `writable`/`derived` pair rather than `$state` --
  // same @tanstack/svelte-query version quirk applies here (deliveryQuery
  // needs a *new* query per sha, and only a real Svelte store makes
  // createQuery re-derive its options after the first one). The manifest
  // query below (`slackManifestQuery`) needs the exact same treatment for
  // its own `opened` flag.
  import { createMutation, createQuery, useQueryClient } from "@tanstack/svelte-query";
  import { errorMessage, type AgentSlack } from "../lib/api";
  import {
    connectSlackMutation,
    deliveryQuery,
    saveSlackChannelsMutation,
    slackManifestQuery,
  } from "../lib/queries";

  let {
    agentName,
    slack,
    frozen = false,
  }: {
    agentName: string;
    slack: AgentSlack;
    frozen?: boolean;
  } = $props();

  const queryClient = useQueryClient();
  // `agentName` is read once here rather than tracked reactively: AgentDetail
  // (the only caller) remounts this component via `{#key route.name}`
  // whenever the routed agent changes, so this prop is fixed for the
  // component's entire lifetime — same reasoning as ChatPanel/MemoryPanel.
  // svelte-ignore state_referenced_locally
  const connect = createMutation(() => (connectSlackMutation(queryClient, agentName)));
  // svelte-ignore state_referenced_locally
  const saveChannels = createMutation(() => (saveSlackChannelsMutation(queryClient, agentName)));

  // --- The header -----------------------------------------------------------

  // The state as a word, not a sentence. "Connecting" is client-side only:
  // `slack.state` itself has no such value.
  const CHIP: Record<AgentSlack["state"], string> = {
    idle: "Not connected",
    waiting: "Waiting to connect",
    connected: "Connected",
  };

  const chipText = $derived(connect.isPending ? "Connecting" : CHIP[slack.state]);
  const chipTone = $derived(
    slack.state === "connected" && !connect.isPending ? "ok" : "muted",
  );

  // The /invite line lives here rather than in the numbered steps below: it
  // is about the channels in the table, not about creating the app, and the
  // runtime's Slack transport never joins a channel by itself.
  const lead = $derived(
    `List the channels this agent should join, and type /invite @${agentName} in each ` +
      `one. Set up the Slack app once to connect it.`,
  );

  // --- The channels draft ---------------------------------------------------

  // Same client-side courtesy cap as the server's `MAX_CHANNELS`
  // (kobo_api.slack_state) — disables "+ Channel" before the round trip,
  // but the server is still the real gate: a request that somehow exceeds
  // this comes back 422 with its own "Eight channels at most for now."
  // copy, rendered verbatim through `saveError` below.
  const MAX_CHANNELS = 8;

  /** One row of the table. `id` never changes and the `{#each}` below is
   * keyed on it: keying on the index meant removing a row re-keyed every
   * row after it, so Svelte reused the wrong DOM nodes and an in-progress
   * edit landed on the neighbour of the row somebody actually removed. */
  type Row = { id: number; value: string };

  let nextRowId = 0;

  function seedRows(channels: string[]): Row[] {
    return channels.map((value) => ({ id: nextRowId++, value }));
  }

  // Seeded once from whatever's already saved, same "read the prop once at
  // mount" discipline as `agentName` above — this is a local draft the
  // human edits, not something that should get silently overwritten by a
  // background poll landing mid-edit. A save re-seeds it from the server's
  // echoed-back list, which is the one time the draft *should* move.
  // svelte-ignore state_referenced_locally
  let rows = $state<Row[]>(seedRows(slack.channels));
  // svelte-ignore state_referenced_locally
  let baseChannels = $state<string[]>([...slack.channels]);

  /** The newest row, so the field of a row somebody just added takes focus
   * without them reaching for the mouse. */
  let focusId = $state<number | null>(null);

  let saveError = $state<string | null>(null);

  /** What a channels save would send: trimmed, with the empty rows dropped.
   * An empty row is a row somebody has not filled in yet, not a request to
   * save an empty channel id. */
  const cleaned = $derived(
    rows.map((row) => row.value.trim()).filter((value) => value.length > 0),
  );

  const channelsPending = $derived(
    cleaned.length !== baseChannels.length ||
      cleaned.some((value, i) => value !== baseChannels[i]),
  );

  // --- The setup draft ------------------------------------------------------

  let appId = $state("");
  let botToken = $state("");
  let appToken = $state("");

  // Per-field show/hide (mis-paste fix): both tokens start masked
  // (`type="password"`), and each has its own independent toggle so a
  // pasted value can be checked before Save is pressed. Purely client-side
  // and pre-submit — it changes nothing about the never-stored-by-the-room
  // guarantee below, since the value never leaves this input either way.
  let showBotToken = $state(false);
  let showAppToken = $state(false);

  /** Connect takes all three or none: two of the three is not a state the
   * server has anything to do with. */
  const setupPending = $derived(
    appId.trim().length > 0 && botToken.trim().length > 0 && appToken.trim().length > 0,
  );

  // The setup is a once-per-agent ceremony, so it starts open for an agent
  // that has not finished it and shut for one that has. A local flag from
  // there on: somebody who opens it on a connected agent (to read the
  // manifest again, say) keeps it open.
  // svelte-ignore state_referenced_locally
  let setupOpen = $state(slack.state !== "connected");

  // --- Saving ---------------------------------------------------------------

  const saving = $derived(saveChannels.isPending || connect.isPending);
  const hasPending = $derived(channelsPending || setupPending);
  const canSave = $derived(!frozen && !saving && hasPending);

  // The server's own copy for the missing-channels precondition (POST
  // .../slack/connect, 409 — see `kobo_api.main.connect_slack`), matched
  // verbatim so the swap below only fires for that exact case and never
  // masks a different server error under the wrong warning.
  const MISSING_CHANNELS_ERROR = "Add at least one channel first.";

  /** Sends whatever changed, through the same two calls as ever. The three
   * setup fields are only cleared on success, once they're actually spent —
   * any server error (including the missing-channels 409) leaves them
   * exactly as pasted, since tokens are never persisted server-side and
   * re-pasting them is a real cost to the human. */
  async function handleSave(): Promise<void> {
    if (!canSave) {
      return;
    }
    saveError = null;
    // Read before the first await: the rows below are re-seeded from the
    // server's answer, and this is what the human asked to save.
    const channels = [...cleaned];
    if (channelsPending) {
      try {
        const result = await saveChannels.mutateAsync(channels);
        const saved = result.channels ?? [];
        baseChannels = [...saved];
        rows = seedRows(saved);
        focusId = null;
        if (result.sha !== undefined) {
          beginDelivery(result.sha);
        }
      } catch (err: unknown) {
        saveError = errorMessage(err, "Couldn't save the channels. Try again.");
        return;
      }
    }
    if (setupPending) {
      try {
        const result = await connect.mutateAsync({
          app_id: appId.trim(),
          bot_token: botToken.trim(),
          app_token: appToken.trim(),
        });
        appId = "";
        botToken = "";
        appToken = "";
        beginDelivery(result.sha);
      } catch (err: unknown) {
        const message = errorMessage(err, "Couldn't finish setting up Slack. Try again.");
        saveError =
          message === MISSING_CHANNELS_ERROR
            ? "Add at least one channel, then press Save again. Your values are kept."
            : message;
      }
    }
  }

  function addRow(): void {
    if (frozen || rows.length >= MAX_CHANNELS) {
      return;
    }
    const row: Row = { id: nextRowId++, value: "" };
    rows = [...rows, row];
    focusId = row.id;
  }

  function removeRow(id: number): void {
    if (frozen) {
      return;
    }
    rows = rows.filter((row) => row.id !== id);
  }

  function discard(): void {
    rows = seedRows(baseChannels);
    focusId = null;
    appId = "";
    botToken = "";
    appToken = "";
    saveError = null;
  }

  /** Focus the field of a row somebody just added. Runs at mount, so only
   * the new row gets it and an older row never steals it back. */
  function focusOnMount(node: HTMLInputElement, wanted: boolean) {
    if (wanted) {
      node.focus();
    }
  }

  // --- Manifest reveal ------------------------------------------------------
  //
  // "Show the manifest" is a one-way reveal (the spec's own word), not a
  // toggle: clicking it flips `manifestOpened` true for good, which both
  // shows the block below AND (via the query's own `opened` flag) is the
  // only thing that ever enables `slackManifestQuery` — the fetch never
  // fires on mount, only once a human actually asks to see it.
  let manifestOpened = $state(false);
  const manifest = createQuery(() => (slackManifestQuery(agentName, manifestOpened)));

  function handleShowManifest(): void {
    manifestOpened = true;
  }

  // The exact pretty-printed string rendered in the `<pre>` below, kept as a
  // single source: the copy button hands this same value to the clipboard,
  // so there's never a second `JSON.stringify` call that could drift from
  // what the human actually sees.
  const manifestText = $derived(
    manifest.data ? JSON.stringify(manifest.data, null, 2) : undefined,
  );

  let copyState = $state<"idle" | "copied" | "failed">("idle");

  /** Copies `manifestText` to the clipboard. Falls back to a plain failure
   * note (the `<pre>` itself stays selectable via `user-select: all`) when
   * `navigator.clipboard` isn't there (e.g. a non-secure context) or the
   * write itself rejects. */
  async function handleCopyManifest(): Promise<void> {
    if (manifestText === undefined) {
      return;
    }
    try {
      if (!navigator.clipboard) {
        throw new Error("Clipboard unavailable");
      }
      await navigator.clipboard.writeText(manifestText);
      copyState = "copied";
    } catch {
      copyState = "failed";
    }
  }

  // --- Delivery chip (copied pattern from MemoryPanel; see its doc comment
  // and the module comment above for the full rationale) -------------------

  const DELIVERED_CLEAR_MS = 5000;

  let deliverySha = $state<string | null>(null);
  const delivery = createQuery(() => (deliveryQuery(agentName, deliverySha)));

  let clearTimer: ReturnType<typeof setTimeout> | undefined;

  function beginDelivery(sha: string): void {
    if (clearTimer !== undefined) {
      clearTimeout(clearTimer);
      clearTimer = undefined;
    }
    deliverySha = sha;
  }

  $effect(() => {
    if (delivery.data?.state !== "delivered") {
      return;
    }
    clearTimer = setTimeout(() => {
      deliverySha = null;
      clearTimer = undefined;
    }, DELIVERED_CLEAR_MS);
    return () => {
      if (clearTimer !== undefined) {
        clearTimeout(clearTimer);
        clearTimer = undefined;
      }
    };
  });

  type DeliveryView = { tone: "pending" | "ok" | "fail"; text: string };

  const deliveryView = $derived.by((): DeliveryView => {
    if (delivery.isError) {
      return {
        tone: "fail",
        text: errorMessage(delivery.error, "Couldn't check on your changes."),
      };
    }
    const data = delivery.data;
    if (data === undefined) {
      return { tone: "pending", text: "Delivering changes…" };
    }
    if (data.state === "delivered") {
      return { tone: "ok", text: "Changes are in place" };
    }
    if (data.state === "failed") {
      return {
        tone: "fail",
        text: errorMessage(
          data.detail !== null ? new Error(data.detail) : undefined,
          "Something went wrong. Try saving again.",
        ),
      };
    }
    // "building" | "rolling", or any state this code doesn't recognize yet —
    // treated the same as still-in-progress, never claiming success or
    // failure it can't back up.
    return { tone: "pending", text: "Delivering changes…" };
  });
</script>

<section class="slack-panel" aria-labelledby="slack-panel-heading">
  <div class="slack-panel__header">
    <div class="slack-panel__title">
      <h2 id="slack-panel-heading" class="slack-panel__heading">Slack</h2>
      <span class="slack-panel__chip slack-panel__chip--{chipTone}">{chipText}</span>
    </div>
    <p class="slack-panel__lead">{lead}</p>
  </div>

  <div class="slack-panel__toolbar">
    <button
      type="button"
      class="slack-panel__button"
      disabled={frozen || rows.length >= MAX_CHANNELS}
      onclick={addRow}
    >
      + Channel
    </button>
    <button
      type="button"
      class="slack-panel__button slack-panel__button--quiet"
      aria-expanded={setupOpen}
      aria-controls="slack-panel-setup"
      disabled={frozen}
      onclick={() => (setupOpen = !setupOpen)}
    >
      Set up the Slack app
    </button>
  </div>

  <table class="slack-panel__table">
    <thead>
      <tr>
        <th scope="col">Channel ID</th>
        <th scope="col" class="slack-panel__col-remove">
          <span class="slack-panel__sr">Remove</span>
        </th>
      </tr>
    </thead>
    <tbody>
      {#if rows.length === 0}
        <tr>
          <td class="slack-panel__empty" colspan="2">No channels yet.</td>
        </tr>
      {/if}
      {#each rows as row, index (row.id)}
        <tr class="slack-panel__row">
          <td class="slack-panel__cell">
            <input
              class="slack-panel__input"
              type="text"
              autocomplete="off"
              spellcheck="false"
              aria-label={`Channel ${index + 1}`}
              bind:value={row.value}
              disabled={frozen}
              use:focusOnMount={row.id === focusId}
            />
          </td>
          <td class="slack-panel__cell slack-panel__cell--remove">
            <button
              type="button"
              class="slack-panel__remove"
              aria-label={row.value.trim().length > 0
                ? `Remove ${row.value.trim()}`
                : `Remove row ${index + 1}`}
              disabled={frozen}
              onclick={() => removeRow(row.id)}
            >
              <span aria-hidden="true">✕</span>
            </button>
          </td>
        </tr>
      {/each}
    </tbody>
  </table>

  {#if setupOpen}
    <div class="slack-panel__setup" id="slack-panel-setup">
      <div class="slack-panel__manifest">
        <div class="slack-panel__manifest-actions">
          <button
            type="button"
            class="slack-panel__button slack-panel__button--quiet"
            disabled={frozen}
            onclick={handleShowManifest}
          >
            Show the manifest
          </button>
          <button
            type="button"
            class="slack-panel__button slack-panel__button--quiet"
            disabled={frozen || manifestText === undefined}
            onclick={handleCopyManifest}
          >
            {copyState === "copied" ? "Copied." : "Copy the manifest"}
          </button>
        </div>
        {#if copyState === "failed"}
          <p class="slack-panel__error" role="alert">Copy failed. Select the text instead.</p>
        {/if}
        {#if manifestOpened}
          {#if manifest.isPending}
            <p class="slack-panel__note">Loading the manifest…</p>
          {:else if manifest.isError}
            <p class="slack-panel__error" role="alert">
              {errorMessage(manifest.error, "Couldn't load the manifest.")}
            </p>
          {:else if manifestText !== undefined}
            <pre class="slack-panel__manifest-block">{manifestText}</pre>
          {/if}
        {/if}
      </div>

      <ol class="slack-panel__steps">
        <li class="slack-panel__step">
          <p class="slack-panel__step-text">
            At
            <a
              class="slack-panel__step-link"
              href="https://api.slack.com/apps"
              target="_blank"
              rel="noopener noreferrer"
            >
              api.slack.com/apps
              <span class="slack-panel__sr"> (opens Slack)</span>
            </a>, press "Create New App".
          </p>
        </li>
        <li class="slack-panel__step">
          <p class="slack-panel__step-text">Choose "From a manifest", then select your workspace.</p>
        </li>
        <li class="slack-panel__step">
          <p class="slack-panel__step-text">Paste the manifest above on the JSON tab, then create the app.</p>
        </li>
        <li class="slack-panel__step">
          <p class="slack-panel__step-text">
            On "Basic Information", copy the App ID, then generate an app-level token with the connections:write scope.
          </p>
        </li>
        <li class="slack-panel__step">
          <p class="slack-panel__step-text">Install the app to your workspace, then copy the bot token.</p>
        </li>
      </ol>

      <div class="slack-panel__fields">
        <div class="slack-panel__field">
          <label class="slack-panel__label" for="slack-panel-app-id">App ID</label>
          <input
            id="slack-panel-app-id"
            class="slack-panel__input slack-panel__input--boxed"
            type="text"
            autocomplete="off"
            spellcheck="false"
            placeholder="A0123456789"
            bind:value={appId}
            disabled={frozen}
          />
        </div>
        <div class="slack-panel__field">
          <label class="slack-panel__label" for="slack-panel-bot-token">Bot token</label>
          <div class="slack-panel__token-row">
            <input
              id="slack-panel-bot-token"
              class="slack-panel__input slack-panel__input--boxed"
              type={showBotToken ? "text" : "password"}
              autocomplete="off"
              spellcheck="false"
              placeholder="xoxb-..."
              bind:value={botToken}
              disabled={frozen}
            />
            <button
              type="button"
              class="slack-panel__button slack-panel__button--quiet"
              disabled={frozen}
              aria-label={showBotToken ? "Hide bot token" : "Show bot token"}
              onclick={() => (showBotToken = !showBotToken)}
            >
              {showBotToken ? "Hide" : "Show"}
            </button>
          </div>
        </div>
        <div class="slack-panel__field">
          <label class="slack-panel__label" for="slack-panel-app-token">App-level token</label>
          <div class="slack-panel__token-row">
            <input
              id="slack-panel-app-token"
              class="slack-panel__input slack-panel__input--boxed"
              type={showAppToken ? "text" : "password"}
              autocomplete="off"
              spellcheck="false"
              placeholder="xapp-..."
              bind:value={appToken}
              disabled={frozen}
            />
            <button
              type="button"
              class="slack-panel__button slack-panel__button--quiet"
              disabled={frozen}
              aria-label={showAppToken ? "Hide app-level token" : "Show app-level token"}
              onclick={() => (showAppToken = !showAppToken)}
            >
              {showAppToken ? "Hide" : "Show"}
            </button>
          </div>
        </div>
      </div>

      <p class="slack-panel__note">
        For safety, the room never keeps these values. They go to the agent when you press Save.
      </p>
    </div>
  {/if}

  <div class="slack-panel__footer">
    {#if saveError}
      <p class="slack-panel__status slack-panel__status--fail" role="alert">{saveError}</p>
    {:else if deliverySha !== null}
      <p
        class="slack-panel__status slack-panel__status--{deliveryView.tone}"
        role="status"
        aria-live="polite"
      >
        {#if deliveryView.tone === "pending"}
          <span class="slack-panel__delivery-dot pulse" aria-hidden="true"></span>
        {/if}
        <span>{deliveryView.text}</span>
      </p>
    {/if}
    <div class="slack-panel__actions">
      {#if hasPending}
        <button
          type="button"
          class="slack-panel__button slack-panel__button--quiet"
          disabled={frozen || saving}
          onclick={discard}
        >
          Discard
        </button>
      {/if}
      <button
        type="button"
        class="slack-panel__button slack-panel__button--primary"
        disabled={!canSave}
        onclick={handleSave}
      >
        {saving ? "Saving…" : "Save"}
      </button>
    </div>
  </div>
</section>

<style>
  .slack-panel {
    display: flex;
    flex-direction: column;
    gap: var(--space-sm);
    padding: var(--space-lg);
    border: 1px solid var(--border);
    border-radius: var(--radius);
    background: var(--panel);
  }

  .slack-panel__header {
    display: flex;
    flex-direction: column;
    gap: var(--space-2xs);
  }

  .slack-panel__title {
    display: flex;
    flex-wrap: wrap;
    align-items: center;
    gap: var(--space-xs);
  }

  .slack-panel__heading {
    margin: 0;
    color: var(--muted);
    font-size: 0.8rem;
    font-weight: 600;
    letter-spacing: 0.06em;
    text-transform: uppercase;
  }

  .slack-panel__chip {
    padding: 0 var(--space-xs);
    border: 1px solid var(--border);
    border-radius: 999px;
    color: var(--muted);
    font-size: 0.7rem;
    line-height: 1.6;
    white-space: nowrap;
  }

  .slack-panel__chip--ok {
    border-color: var(--ok);
    color: var(--ok);
  }

  .slack-panel__lead,
  .slack-panel__note {
    margin: 0;
    color: var(--muted);
    font-size: 0.85rem;
  }

  .slack-panel__note {
    font-size: 0.8rem;
  }

  .slack-panel__error {
    margin: 0;
    color: var(--fail);
    font-size: 0.85rem;
  }

  .slack-panel__toolbar {
    display: flex;
    flex-wrap: wrap;
    align-items: center;
    justify-content: space-between;
    gap: var(--space-xs);
  }

  .slack-panel__button {
    padding: var(--space-2xs) var(--space-sm);
    border: 1px solid var(--border);
    border-radius: var(--radius-sm);
    background: transparent;
    color: var(--ink);
    font: inherit;
    font-size: 0.85rem;
    font-weight: 600;
    line-height: 1.4;
    cursor: pointer;
  }

  .slack-panel__button:hover:not(:disabled) {
    border-color: var(--accent);
    color: var(--accent);
  }

  .slack-panel__button:disabled {
    opacity: 0.5;
    cursor: not-allowed;
  }

  .slack-panel__button--quiet {
    border-color: transparent;
    color: var(--muted);
  }

  .slack-panel__button--primary {
    border-color: var(--accent);
    background: var(--accent);
    color: var(--accent-ink);
  }

  .slack-panel__button--primary:hover:not(:disabled) {
    color: var(--accent-ink);
    filter: brightness(1.05);
  }

  .slack-panel__table {
    width: 100%;
    border-collapse: collapse;
    table-layout: fixed;
  }

  .slack-panel__table th {
    padding: 0 var(--space-xs) var(--space-2xs);
    border-bottom: 1px solid var(--border);
    color: var(--muted);
    font-size: 0.7rem;
    font-weight: 600;
    letter-spacing: 0.06em;
    text-align: left;
    text-transform: uppercase;
  }

  .slack-panel__col-remove {
    width: 2.25rem;
  }

  .slack-panel__row {
    height: 36px;
  }

  .slack-panel__cell {
    padding: 2px var(--space-xs);
    border-bottom: 1px solid var(--border);
    vertical-align: middle;
  }

  .slack-panel__cell--remove {
    text-align: right;
  }

  .slack-panel__empty {
    padding: var(--space-md);
    border-bottom: 1px solid var(--border);
    color: var(--muted);
    font-size: 0.85rem;
    text-align: center;
  }

  .slack-panel__input {
    width: 100%;
    padding: var(--space-2xs) var(--space-xs);
    border: 1px solid transparent;
    border-radius: var(--radius-sm);
    background: transparent;
    color: var(--ink);
    font-family: var(--font-mono);
    font-size: 0.8rem;
  }

  .slack-panel__input:hover:not(:disabled) {
    border-color: var(--border);
  }

  .slack-panel__input:focus-visible {
    border-color: transparent;
    background: var(--bg);
    outline: 2px solid var(--accent);
    outline-offset: -1px;
  }

  .slack-panel__input--boxed {
    border-color: var(--border);
    background: var(--bg);
  }

  .slack-panel__remove {
    padding: 0 var(--space-2xs);
    border: none;
    background: transparent;
    color: var(--muted);
    font: inherit;
    font-size: 0.85rem;
    line-height: 1;
    cursor: pointer;
  }

  .slack-panel__remove:hover:not(:disabled) {
    color: var(--fail);
  }

  .slack-panel__remove:disabled {
    opacity: 0.4;
    cursor: not-allowed;
  }

  .slack-panel__setup {
    display: flex;
    flex-direction: column;
    gap: var(--space-sm);
    padding-top: var(--space-sm);
    border-top: 1px solid var(--border);
  }

  .slack-panel__manifest {
    display: flex;
    flex-direction: column;
    align-items: flex-start;
    gap: var(--space-xs);
  }

  .slack-panel__manifest-actions {
    display: flex;
    flex-wrap: wrap;
    align-items: center;
    gap: var(--space-xs);
  }

  .slack-panel__manifest-block {
    max-width: 100%;
    max-height: 16rem;
    padding: var(--space-sm);
    overflow: auto;
    border: 1px solid var(--border);
    border-radius: var(--radius-sm);
    background: var(--bg);
    color: var(--ink);
    font-family: var(--font-mono);
    font-size: 0.75rem;
    white-space: pre;
    user-select: all;
  }

  .slack-panel__steps {
    display: flex;
    flex-direction: column;
    gap: var(--space-2xs);
    margin: 0;
    padding: 0;
    list-style: none;
    counter-reset: ceremony-step;
  }

  .slack-panel__step {
    display: flex;
    align-items: baseline;
    gap: var(--space-xs);
    counter-increment: ceremony-step;
  }

  .slack-panel__step::before {
    content: counter(ceremony-step);
    display: grid;
    flex-shrink: 0;
    width: 1.25rem;
    height: 1.25rem;
    border: 1px solid var(--border);
    border-radius: 50%;
    background: var(--panel);
    color: var(--muted);
    font-size: 0.7rem;
    font-weight: 600;
    place-items: center;
  }

  .slack-panel__step-text {
    margin: 0;
    color: var(--ink);
    font-size: 0.85rem;
  }

  .slack-panel__step-link {
    display: inline-flex;
    color: var(--accent);
    text-decoration: none;
  }

  .slack-panel__step-link:hover {
    text-decoration: underline;
  }

  .slack-panel__fields {
    display: flex;
    flex-wrap: wrap;
    gap: var(--space-sm);
  }

  .slack-panel__field {
    display: flex;
    flex: 1 1 12rem;
    flex-direction: column;
    gap: var(--space-2xs);
    min-width: 0;
  }

  .slack-panel__label {
    color: var(--muted);
    font-size: 0.7rem;
    font-weight: 600;
    letter-spacing: 0.06em;
    text-transform: uppercase;
  }

  .slack-panel__token-row {
    display: flex;
    align-items: center;
    gap: var(--space-2xs);
  }

  .slack-panel__token-row .slack-panel__input {
    flex: 1;
    min-width: 0;
  }

  .slack-panel__footer {
    display: flex;
    flex-wrap: wrap;
    align-items: center;
    justify-content: space-between;
    gap: var(--space-xs);
  }

  .slack-panel__status {
    display: flex;
    flex-wrap: wrap;
    align-items: center;
    gap: var(--space-xs);
    margin: 0;
    color: var(--muted);
    font-size: 0.8rem;
  }

  .slack-panel__status--ok {
    color: var(--ok);
  }

  .slack-panel__status--fail {
    color: var(--fail);
  }

  .slack-panel__actions {
    display: flex;
    align-items: center;
    gap: var(--space-xs);
    margin-left: auto;
  }

  .slack-panel__delivery-dot {
    width: 0.5rem;
    height: 0.5rem;
    border-radius: 50%;
    background: var(--pulse);
  }

  .slack-panel__sr {
    position: absolute;
    width: 1px;
    height: 1px;
    padding: 0;
    overflow: hidden;
    clip-path: inset(50%);
    white-space: nowrap;
  }
</style>
