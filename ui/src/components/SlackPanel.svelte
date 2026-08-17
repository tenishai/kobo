<script lang="ts">
  // Two-lane Slack (2026-08-17): the channels lane (bottom-up, always
  // rendered — the tab itself is unconditional now, see tabs.ts) is open to
  // everyone in the room; the setup lane (create-app guidance + bot/app
  // tokens) stays behind `adminVisible`, since that's the half that carries
  // actual Slack secrets. Both lanes read the same `slack` prop
  // (`AgentDetail.slack`, refetched by the caller's agentQuery — see
  // AgentDetail.svelte): `app_id` says whether an app exists yet,
  // `channels`/`state` say what's saved and how wired-up it is. Neither lane
  // holds a "which phase am I in" flag of its own — they just render
  // whatever `slack` says, so a save/connect success (which invalidates the
  // agent detail query) advances the card for free once the refetch lands,
  // same as MemoryPanel's knowledge list advances after a save.
  //
  // The one thing that *is* local state independent of `slack` is the
  // delivery chip's sha store (shared by both lanes: a channels save and a
  // connect finish both end in the same `beginDelivery(sha)`), and it's
  // deliberately NOT reset when `slack` changes shape — a save/connect that
  // just fired keeps its chip alive across the next refetch. See
  // MemoryPanel's doc comment for why this is a plain `writable`/`derived`
  // pair rather than `$state` — same @tanstack/svelte-query version quirk
  // applies here (deliveryQuery needs a *new* query per sha, and only a real
  // Svelte store makes createQuery re-derive its options after the first
  // one).
  import { createMutation, createQuery, useQueryClient } from "@tanstack/svelte-query";
  import { derived, writable } from "svelte/store";
  import { errorMessage, type AgentSlack } from "../lib/api";
  import {
    connectSlackMutation,
    createSlackAppMutation,
    deliveryQuery,
    saveSlackChannelsMutation,
  } from "../lib/queries";

  let {
    agentName,
    slack,
    adminVisible = false,
    frozen = false,
  }: {
    agentName: string;
    slack: AgentSlack;
    adminVisible?: boolean;
    frozen?: boolean;
  } = $props();

  const queryClient = useQueryClient();
  // `agentName` is read once here rather than tracked reactively: AgentDetail
  // (the only caller) remounts this component via `{#key route.name}`
  // whenever the routed agent changes, so this prop is fixed for the
  // component's entire lifetime — same reasoning as ChatPanel/MemoryPanel.
  // svelte-ignore state_referenced_locally
  const createApp = createMutation(createSlackAppMutation(queryClient, agentName));
  // svelte-ignore state_referenced_locally
  const connect = createMutation(connectSlackMutation(queryClient, agentName));
  // svelte-ignore state_referenced_locally
  const saveChannels = createMutation(saveSlackChannelsMutation(queryClient, agentName));

  // --- Channels lane (open to everyone) ------------------------------------

  // Same client-side courtesy cap as the server's `MAX_CHANNELS`
  // (kobo_api.slack_state) — disables "Add another channel" before the
  // round trip, but the server is still the real gate: a request that
  // somehow exceeds this comes back 422 with its own "Eight channels at
  // most for now." copy, rendered verbatim through `saveError` below.
  const MAX_CHANNELS = 8;

  // Seeded once from whatever's already saved, same "read the prop once at
  // mount" discipline as `agentName` above — this is a local draft the
  // human edits, not something that should get silently overwritten by a
  // background poll landing mid-edit. A save's own `onSuccess` (below)
  // re-seeds it from the server's echoed-back list, which is the one time
  // the draft *should* move.
  // svelte-ignore state_referenced_locally
  let rows = $state<string[]>(slack.channels.length > 0 ? [...slack.channels] : [""]);
  let saveError = $state<string | null>(null);

  const canSave = $derived(
    !frozen && !$saveChannels.isPending && rows.some((row) => row.trim().length > 0),
  );

  function handleAddRow(): void {
    if (rows.length >= MAX_CHANNELS) {
      return;
    }
    rows = [...rows, ""];
  }

  function handleRemoveRow(index: number): void {
    rows = rows.filter((_, i) => i !== index);
  }

  function handleSaveChannels(): void {
    if (!canSave) {
      return;
    }
    const cleaned = rows.map((row) => row.trim()).filter((row) => row.length > 0);
    saveError = null;
    $saveChannels.mutate(cleaned, {
      onSuccess: (result) => {
        rows = result.channels.length > 0 ? [...result.channels] : [""];
        if (result.sha !== undefined) {
          beginDelivery(result.sha);
        }
      },
      onError: (err: unknown) => {
        saveError = errorMessage(err, "Couldn't save the channels. Try again.");
      },
    });
  }

  // --- Status line ----------------------------------------------------------

  // The spec's state-table copy, verbatim — no em dashes, nothing paraphrased.
  const STATE_COPY: Record<AgentSlack["state"], string> = {
    idle: "Not connected. Add the channels this agent should join.",
    waiting: "Channels saved. Waiting on an operator to finish the connection.",
    connected: "Connected.",
  };

  // "Connecting..." overrides the state-derived line while a connect is in
  // flight — client-side only, `slack.state` itself has no such value.
  const statusText = $derived($connect.isPending ? "Connecting..." : STATE_COPY[slack.state]);

  // --- Setup lane (operator-only: create-app guidance + tokens) -----------

  let createError = $state<string | null>(null);
  let connectError = $state<string | null>(null);

  let botToken = $state("");
  let appToken = $state("");

  const canFinish = $derived(
    botToken.trim().length > 0 && appToken.trim().length > 0 && !$connect.isPending,
  );

  function handleCreateApp(): void {
    createError = null;
    $createApp.mutate(undefined, {
      onError: (err: unknown) => {
        createError = errorMessage(err, "Couldn't set up Slack. Try again.");
      },
    });
  }

  /** Submits both tokens (two-lane Slack, 2026-08-17: no channel here — the
   * channels lane above owns that). Tokens are always cleared from local
   * state afterward — on success because they're spent, on failure because
   * the human needs to re-paste them (the error text says so). */
  function handleFinishSetup(): void {
    if (!canFinish) {
      return;
    }
    connectError = null;
    $connect.mutate(
      {
        bot_token: botToken.trim(),
        app_token: appToken.trim(),
      },
      {
        onSuccess: (result) => {
          botToken = "";
          appToken = "";
          beginDelivery(result.sha);
        },
        onError: (err: unknown) => {
          botToken = "";
          appToken = "";
          connectError = `${errorMessage(err, "Couldn't finish setting up Slack. Try again.")} Paste the tokens again.`;
        },
      },
    );
  }

  // --- Delivery chip (copied pattern from MemoryPanel; see its doc comment
  // and the module comment above for the full rationale) -------------------

  const DELIVERED_CLEAR_MS = 5000;

  const deliverySha = writable<string | null>(null);
  const deliveryOptions = derived(deliverySha, ($sha) => deliveryQuery(agentName, $sha));
  const delivery = createQuery(deliveryOptions);

  let clearTimer: ReturnType<typeof setTimeout> | undefined;

  function beginDelivery(sha: string): void {
    if (clearTimer !== undefined) {
      clearTimeout(clearTimer);
      clearTimer = undefined;
    }
    deliverySha.set(sha);
  }

  $effect(() => {
    if ($delivery.data?.state !== "delivered") {
      return;
    }
    clearTimer = setTimeout(() => {
      deliverySha.set(null);
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
    if ($delivery.isError) {
      return {
        tone: "fail",
        text: errorMessage($delivery.error, "Couldn't check on your changes."),
      };
    }
    const data = $delivery.data;
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
    <h2 id="slack-panel-heading" class="slack-panel__heading">Slack</h2>
    <p class="slack-panel__subtitle">{statusText}</p>
  </div>

  <div class="slack-panel__channels">
    <ul class="slack-panel__rows">
      {#each rows as _row, i}
        <li class="slack-panel__row">
          <input
            class="slack-panel__input"
            type="text"
            autocomplete="off"
            placeholder="Channel ID"
            aria-label={`Channel ${i + 1}`}
            bind:value={rows[i]}
            disabled={frozen}
          />
          <button
            type="button"
            class="slack-panel__row-remove"
            aria-label={`Remove channel ${i + 1}`}
            disabled={frozen}
            onclick={() => handleRemoveRow(i)}
          >
            Remove
          </button>
        </li>
      {/each}
    </ul>

    <button
      type="button"
      class="slack-panel__add-row"
      disabled={frozen || rows.length >= MAX_CHANNELS}
      onclick={handleAddRow}
    >
      Add another channel
    </button>

    {#if saveError}
      <p class="slack-panel__error" role="alert">{saveError}</p>
    {/if}

    <button
      type="button"
      class="slack-panel__save"
      disabled={frozen || $saveChannels.isPending || !canSave}
      onclick={handleSaveChannels}
    >
      {$saveChannels.isPending ? "Saving…" : "Save"}
    </button>
  </div>

  {#if adminVisible}
    <div class="slack-panel__admin">
      {#if slack.app_id === null}
        <button
          type="button"
          class="slack-panel__connect"
          disabled={frozen || $createApp.isPending}
          onclick={handleCreateApp}
        >
          {$createApp.isPending ? "Connecting…" : "Connect to Slack"}
        </button>
        {#if createError}
          <p class="slack-panel__error" role="alert">{createError}</p>
        {/if}
      {:else}
        {@const appId = slack.app_id}
        <ol class="slack-panel__steps">
          <li class="slack-panel__step">
            <a
              class="slack-panel__step-title slack-panel__step-link"
              href={`https://api.slack.com/apps/${appId}`}
              target="_blank"
              rel="noopener noreferrer"
            >
              Install it to your workspace
              <span class="slack-panel__sr-only"> (opens Slack)</span>
            </a>
            <p class="slack-panel__step-help">
              Install the app, then copy the token that starts with xoxb-.
            </p>
          </li>
          <li class="slack-panel__step">
            <p class="slack-panel__step-title">Turn on its connection</p>
            <p class="slack-panel__step-help">
              In Basic Information → App-Level Tokens, create a token that can connect,
              then copy the one that starts with xapp-.
            </p>
          </li>
        </ol>

        <form
          class="slack-panel__form"
          onsubmit={(event) => {
            event.preventDefault();
            handleFinishSetup();
          }}
        >
          <div class="slack-panel__field">
            <label class="slack-panel__label" for="slack-panel-bot-token">Bot token</label>
            <input
              id="slack-panel-bot-token"
              class="slack-panel__input"
              type="password"
              autocomplete="off"
              bind:value={botToken}
              disabled={frozen}
            />
          </div>
          <div class="slack-panel__field">
            <label class="slack-panel__label" for="slack-panel-app-token">App-level token</label>
            <input
              id="slack-panel-app-token"
              class="slack-panel__input"
              type="password"
              autocomplete="off"
              bind:value={appToken}
              disabled={frozen}
            />
          </div>

          {#if connectError}
            <p class="slack-panel__error" role="alert">{connectError}</p>
          {/if}

          <button type="submit" class="slack-panel__finish" disabled={frozen || !canFinish}>
            {$connect.isPending ? "Finishing…" : "Finish setup"}
          </button>
        </form>
        <p class="slack-panel__note">You can add a picture for it later in Slack's settings.</p>
      {/if}
    </div>
  {/if}

  {#if $deliverySha !== null}
    <div
      class="slack-panel__delivery slack-panel__delivery--{deliveryView.tone}"
      role="status"
      aria-live="polite"
    >
      {#if deliveryView.tone === "pending"}
        <span class="slack-panel__delivery-dot pulse" aria-hidden="true"></span>
      {/if}
      <span>{deliveryView.text}</span>
    </div>
  {/if}
</section>

<style>
  .slack-panel {
    display: flex;
    flex-direction: column;
    gap: var(--space-md);
    padding: var(--space-lg);
    border: 1px solid var(--border);
    border-radius: var(--radius);
    background: var(--panel);
  }

  .slack-panel__sr-only {
    position: absolute;
    width: 1px;
    height: 1px;
    overflow: hidden;
    clip: rect(0, 0, 0, 0);
    white-space: nowrap;
  }

  .slack-panel__header {
    display: flex;
    flex-direction: column;
    gap: var(--space-2xs);
  }

  .slack-panel__heading {
    margin: 0;
    color: var(--muted);
    font-size: 0.8rem;
    font-weight: 600;
    letter-spacing: 0.06em;
    text-transform: uppercase;
  }

  .slack-panel__subtitle {
    margin: 0;
    color: var(--muted);
    font-size: 0.85rem;
  }

  .slack-panel__channels {
    display: flex;
    flex-direction: column;
    gap: var(--space-sm);
  }

  .slack-panel__rows {
    display: flex;
    flex-direction: column;
    gap: var(--space-xs);
    margin: 0;
    padding: 0;
    list-style: none;
  }

  .slack-panel__row {
    display: flex;
    align-items: center;
    gap: var(--space-xs);
  }

  .slack-panel__row-remove {
    flex-shrink: 0;
    padding: var(--space-2xs) var(--space-sm);
    border: 1px solid var(--border);
    border-radius: var(--radius-sm);
    background: var(--panel);
    color: var(--muted);
    cursor: pointer;
    white-space: nowrap;
  }

  .slack-panel__row-remove:hover:not(:disabled) {
    border-color: var(--fail);
    color: var(--fail);
  }

  .slack-panel__row-remove:disabled {
    opacity: 0.5;
    cursor: not-allowed;
  }

  .slack-panel__add-row {
    align-self: flex-start;
    padding: var(--space-2xs) var(--space-sm);
    border: 1px solid var(--border);
    border-radius: var(--radius-sm);
    background: var(--panel);
    color: var(--ink);
    font-weight: 600;
    cursor: pointer;
  }

  .slack-panel__add-row:hover:not(:disabled) {
    border-color: var(--accent);
    color: var(--accent);
  }

  .slack-panel__add-row:disabled {
    opacity: 0.5;
    cursor: not-allowed;
  }

  .slack-panel__connect {
    align-self: flex-start;
    padding: var(--space-xs) var(--space-md);
    border: none;
    border-radius: var(--radius-sm);
    background: var(--accent);
    color: var(--accent-ink);
    font-weight: 600;
    cursor: pointer;
  }

  .slack-panel__connect:hover:not(:disabled) {
    filter: brightness(1.05);
  }

  .slack-panel__connect:disabled {
    opacity: 0.5;
    cursor: not-allowed;
  }

  .slack-panel__save {
    align-self: flex-start;
    padding: var(--space-xs) var(--space-md);
    border: none;
    border-radius: var(--radius-sm);
    background: var(--accent);
    color: var(--accent-ink);
    font-weight: 600;
    cursor: pointer;
  }

  .slack-panel__save:hover:not(:disabled) {
    filter: brightness(1.05);
  }

  .slack-panel__save:disabled {
    opacity: 0.5;
    cursor: not-allowed;
  }

  .slack-panel__error {
    margin: 0;
    color: var(--fail);
    font-size: 0.85rem;
  }

  .slack-panel__admin {
    display: flex;
    flex-direction: column;
    gap: var(--space-md);
    padding-top: var(--space-md);
    border-top: 1px solid var(--border);
  }

  .slack-panel__steps {
    display: flex;
    flex-direction: column;
    gap: var(--space-md);
    margin: 0;
    padding-left: var(--space-lg);
  }

  .slack-panel__step {
    display: flex;
    flex-direction: column;
    gap: var(--space-2xs);
  }

  .slack-panel__step-title {
    margin: 0;
    color: var(--ink);
    font-weight: 600;
    font-size: 0.9rem;
  }

  .slack-panel__step-link {
    display: inline-flex;
    align-self: flex-start;
    color: var(--accent);
    text-decoration: none;
  }

  .slack-panel__step-link:hover {
    text-decoration: underline;
  }

  .slack-panel__step-help {
    margin: 0;
    color: var(--muted);
    font-size: 0.8rem;
  }

  .slack-panel__form {
    display: flex;
    flex-direction: column;
    gap: var(--space-sm);
  }

  .slack-panel__field {
    display: flex;
    flex-direction: column;
    gap: var(--space-2xs);
  }

  .slack-panel__label {
    color: var(--ink);
    font-weight: 600;
    font-size: 0.85rem;
  }

  .slack-panel__input {
    padding: var(--space-xs) var(--space-sm);
    border: 1px solid var(--border);
    border-radius: var(--radius-sm);
    background: var(--panel);
    color: var(--ink);
    font: inherit;
  }

  .slack-panel__input:focus-visible {
    outline: 2px solid var(--accent);
    outline-offset: 1px;
  }

  .slack-panel__finish {
    align-self: flex-start;
    padding: var(--space-xs) var(--space-md);
    border: none;
    border-radius: var(--radius-sm);
    background: var(--accent);
    color: var(--accent-ink);
    font-weight: 600;
    cursor: pointer;
  }

  .slack-panel__finish:hover:not(:disabled) {
    filter: brightness(1.05);
  }

  .slack-panel__finish:disabled {
    opacity: 0.5;
    cursor: not-allowed;
  }

  .slack-panel__note {
    margin: 0;
    color: var(--muted);
    font-size: 0.8rem;
  }

  .slack-panel__delivery {
    display: inline-flex;
    align-self: flex-start;
    align-items: center;
    gap: var(--space-xs);
    padding: var(--space-2xs) var(--space-sm);
    border: 1px solid var(--border);
    border-radius: 999px;
    color: var(--muted);
    font-size: 0.85rem;
  }

  .slack-panel__delivery--ok {
    border-color: var(--ok);
    color: var(--ok);
  }

  .slack-panel__delivery--fail {
    border-color: var(--fail);
    color: var(--fail);
  }

  .slack-panel__delivery-dot {
    width: 0.5rem;
    height: 0.5rem;
    border-radius: 50%;
    background: var(--pulse);
  }
</style>
