<script lang="ts">
  // Slack self-service (2026-08-17) retired the operator-only create-app
  // lane outright: the setup section below (manifest + instructions + the
  // three paste fields) now renders for every signed-in user, no gate —
  // there's no more `adminVisible` prop. Both the channels lane and the
  // setup section read the same `slack` prop (`AgentDetail.slack`, refetched
  // by the caller's agentQuery — see AgentDetail.svelte): `app_id` says
  // whether a connect has ever recorded one, `channels`/`state` say what's
  // saved and how wired-up it is. Neither lane holds a "which phase am I
  // in" flag of its own — they just render whatever `slack` says, so a
  // save/connect success (which invalidates the agent detail query)
  // advances the card for free once the refetch lands, same as MemoryPanel's
  // knowledge list advances after a save.
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
  // one). The manifest query below (`slackManifestQuery`) needs the exact
  // same treatment for its own `opened` flag.
  import { createMutation, createQuery, useQueryClient } from "@tanstack/svelte-query";
  import { derived, writable } from "svelte/store";
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

  // --- Setup section (Slack self-service, 2026-08-17: open to everyone,
  // no gate) — the manifest reveal, the numbered ceremony instructions, and
  // the three paste fields (App ID first) that finish connect. ------------

  let connectError = $state<string | null>(null);

  let appId = $state("");
  let botToken = $state("");
  let appToken = $state("");

  const canFinish = $derived(
    appId.trim().length > 0 &&
      botToken.trim().length > 0 &&
      appToken.trim().length > 0 &&
      !$connect.isPending,
  );

  /** Submits the app id plus both tokens (two-lane Slack, 2026-08-17: no
   * channel here — the channels lane above owns that; Slack self-service,
   * 2026-08-17 added the app id itself, pasted by the human from Slack's own
   * app site). All three are always cleared from local state afterward — on
   * success because they're spent, on failure because the human needs to
   * re-paste them (the error text says so). */
  function handleFinishSetup(): void {
    if (!canFinish) {
      return;
    }
    connectError = null;
    $connect.mutate(
      {
        app_id: appId.trim(),
        bot_token: botToken.trim(),
        app_token: appToken.trim(),
      },
      {
        onSuccess: (result) => {
          appId = "";
          botToken = "";
          appToken = "";
          beginDelivery(result.sha);
        },
        onError: (err: unknown) => {
          appId = "";
          botToken = "";
          appToken = "";
          connectError = `${errorMessage(err, "Couldn't finish setting up Slack. Try again.")} Paste them again.`;
        },
      },
    );
  }

  // --- Manifest reveal ------------------------------------------------------
  //
  // "Show the manifest" is a one-way reveal (the spec's own word), not a
  // toggle: clicking it flips `manifestOpened` true for good, which both
  // shows the block below AND (via the writable/derived pair, same version
  // constraint as `deliverySha` above) is the only thing that ever enables
  // `slackManifestQuery` — the fetch never fires on mount, only once a human
  // actually asks to see it.
  const manifestOpened = writable(false);
  const manifestOptions = derived(manifestOpened, ($opened) =>
    slackManifestQuery(agentName, $opened),
  );
  const manifest = createQuery(manifestOptions);

  function handleShowManifest(): void {
    manifestOpened.set(true);
  }

  // The exact pretty-printed string rendered in the `<pre>` below, kept as a
  // single source: the copy button hands this same value to the clipboard,
  // so there's never a second `JSON.stringify` call that could drift from
  // what the human actually sees.
  const manifestText = $derived(
    $manifest.data ? JSON.stringify($manifest.data, null, 2) : undefined,
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

  <div class="slack-panel__setup">
    <div class="slack-panel__manifest">
      <div class="slack-panel__manifest-actions">
        <button
          type="button"
          class="slack-panel__manifest-toggle"
          disabled={frozen}
          onclick={handleShowManifest}
        >
          Show the manifest
        </button>
        <button
          type="button"
          class="slack-panel__manifest-copy"
          disabled={frozen || manifestText === undefined}
          onclick={handleCopyManifest}
        >
          {copyState === "copied" ? "Copied." : "Copy the manifest"}
        </button>
      </div>
      {#if copyState === "failed"}
        <p class="slack-panel__error" role="alert">Copy failed. Select the text instead.</p>
      {/if}
      {#if $manifestOpened}
        {#if $manifest.isPending}
          <p class="slack-panel__manifest-status">Loading the manifest…</p>
        {:else if $manifest.isError}
          <p class="slack-panel__error" role="alert">
            {errorMessage($manifest.error, "Couldn't load the manifest.")}
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
            <span class="slack-panel__sr-only"> (opens Slack)</span>
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
        <p class="slack-panel__step-text">Install the app to your workspace.</p>
      </li>
      <li class="slack-panel__step">
        <p class="slack-panel__step-text">
          In Basic Information, under App-Level Tokens, create a token that can connect.
        </p>
      </li>
      <li class="slack-panel__step">
        <p class="slack-panel__step-text">
          Copy the bot token from the OAuth page and the App ID from Basic Information.
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
        <label class="slack-panel__label" for="slack-panel-app-id">App ID</label>
        <input
          id="slack-panel-app-id"
          class="slack-panel__input"
          type="text"
          autocomplete="off"
          bind:value={appId}
          disabled={frozen}
        />
        <p class="slack-panel__hint">Starts with A, like A0123456789.</p>
      </div>
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
        <p class="slack-panel__hint">Starts with xoxb-.</p>
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
        <p class="slack-panel__hint">Starts with xapp-.</p>
      </div>

      {#if connectError}
        <p class="slack-panel__error" role="alert">{connectError}</p>
      {/if}

      <button type="submit" class="slack-panel__finish" disabled={frozen || !canFinish}>
        {$connect.isPending ? "Finishing…" : "Finish setup"}
      </button>
    </form>
    <p class="slack-panel__note">You can add a picture for it later in Slack's settings.</p>
  </div>

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

  .slack-panel__setup {
    display: flex;
    flex-direction: column;
    gap: var(--space-md);
    padding-top: var(--space-md);
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

  .slack-panel__manifest-toggle,
  .slack-panel__manifest-copy {
    align-self: flex-start;
    padding: var(--space-2xs) var(--space-sm);
    border: 1px solid var(--border);
    border-radius: var(--radius-sm);
    background: var(--panel);
    color: var(--ink);
    font-weight: 600;
    cursor: pointer;
  }

  .slack-panel__manifest-toggle:hover:not(:disabled),
  .slack-panel__manifest-copy:hover:not(:disabled) {
    border-color: var(--accent);
    color: var(--accent);
  }

  .slack-panel__manifest-toggle:disabled,
  .slack-panel__manifest-copy:disabled {
    opacity: 0.5;
    cursor: not-allowed;
  }

  .slack-panel__manifest-status {
    margin: 0;
    color: var(--muted);
    font-size: 0.85rem;
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
    gap: var(--space-sm);
    margin: 0;
    padding-left: var(--space-lg);
  }

  .slack-panel__step {
    display: flex;
    flex-direction: column;
    gap: var(--space-2xs);
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

  .slack-panel__hint {
    margin: 0;
    color: var(--muted);
    font-size: 0.75rem;
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
