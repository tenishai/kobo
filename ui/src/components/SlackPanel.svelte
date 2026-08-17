<script lang="ts">
  // Hooks a ready agent up to Slack. Three states, driven entirely by the
  // `slack` prop (`AgentDetail.slack`, refetched by the caller's agentQuery
  // — see AgentDetail.svelte): no app yet, an app that's waiting on the
  // human to finish install in Slack itself, or a fully wired channel. This
  // component holds no "which state am I in" flag of its own — it just
  // renders whatever `slack` says, so a create/connect success (which
  // invalidates the agent detail query) advances the card for free once the
  // refetch lands, the same way MemoryPanel's knowledge list advances after
  // a save.
  //
  // The one thing that *is* local state here is the delivery chip's sha
  // store, and it's deliberately NOT reset when `slack` changes shape (2→3):
  // a connect that just fired keeps its chip alive across that flip, per the
  // brief. See MemoryPanel's doc comment for why this is a plain
  // `writable`/`derived` pair rather than `$state` — same
  // @tanstack/svelte-query version quirk applies here (deliveryQuery needs a
  // *new* query per sha, and only a real Svelte store makes createQuery
  // re-derive its options after the first one).
  import { createMutation, createQuery, useQueryClient } from "@tanstack/svelte-query";
  import { derived, writable } from "svelte/store";
  import { errorMessage, type AgentDetail } from "../lib/api";
  import { connectSlackMutation, createSlackAppMutation, deliveryQuery } from "../lib/queries";

  let {
    agentName,
    slack,
    frozen = false,
  }: { agentName: string; slack: AgentDetail["slack"]; frozen?: boolean } = $props();

  const queryClient = useQueryClient();
  // `agentName` is read once here rather than tracked reactively: AgentDetail
  // (the only caller) remounts this component via `{#key route.name}`
  // whenever the routed agent changes, so this prop is fixed for the
  // component's entire lifetime — same reasoning as ChatPanel/MemoryPanel.
  // svelte-ignore state_referenced_locally
  const createApp = createMutation(createSlackAppMutation(queryClient, agentName));
  // svelte-ignore state_referenced_locally
  const connect = createMutation(connectSlackMutation(queryClient, agentName));

  let createError = $state<string | null>(null);
  let connectError = $state<string | null>(null);

  let channelInput = $state("");
  let botToken = $state("");
  let appToken = $state("");

  const canFinish = $derived(
    channelInput.trim().length > 0 &&
      botToken.trim().length > 0 &&
      appToken.trim().length > 0 &&
      !$connect.isPending,
  );

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

  // --- Actions -------------------------------------------------------------

  function handleCreateApp(): void {
    createError = null;
    $createApp.mutate(undefined, {
      onError: (err: unknown) => {
        createError = errorMessage(err, "Couldn't set up Slack. Try again.");
      },
    });
  }

  /** Submits the channel + both tokens. Tokens are always cleared from local
   * state afterward — on success because they're spent, on failure because
   * the human needs to re-paste them (the error text says so). The channel
   * value is left alone either way so a failed submit doesn't make the user
   * retype it. */
  function handleFinishSetup(): void {
    if (!canFinish) {
      return;
    }
    connectError = null;
    $connect.mutate(
      {
        channel: channelInput.trim(),
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
</script>

<section class="slack-panel" aria-labelledby="slack-panel-heading">
  <div class="slack-panel__header">
    <h2 id="slack-panel-heading" class="slack-panel__heading">Slack</h2>
    {#if slack === null}
      <p class="slack-panel__subtitle">Let people talk to {agentName} in Slack.</p>
    {:else if slack.channel === null}
      <p class="slack-panel__subtitle">Almost there. Finish setup in Slack.</p>
    {:else}
      <p class="slack-panel__subtitle">In Slack</p>
    {/if}
  </div>

  {#if slack === null}
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
  {:else if slack.channel === null}
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
      <li class="slack-panel__step">
        <p class="slack-panel__step-title">Add the channel and tokens</p>
        <form
          class="slack-panel__form"
          onsubmit={(event) => {
            event.preventDefault();
            handleFinishSetup();
          }}
        >
          <div class="slack-panel__field">
            <label class="slack-panel__label" for="slack-panel-channel">Channel ID</label>
            <input
              id="slack-panel-channel"
              class="slack-panel__input"
              type="text"
              autocomplete="off"
              bind:value={channelInput}
              disabled={frozen}
            />
            <p class="slack-panel__step-help">
              Open the channel's details in Slack and copy its ID.
            </p>
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
      </li>
    </ol>
    <p class="slack-panel__note">You can add a picture for it later in Slack's settings.</p>
  {:else}
    {@const channelId = slack.channel}
    <p class="slack-panel__channel">
      <span class="slack-panel__channel-label">Channel</span>
      <code class="slack-panel__channel-code">{channelId}</code>
    </p>
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

  .slack-panel__error {
    margin: 0;
    color: var(--fail);
    font-size: 0.85rem;
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
    margin-top: var(--space-2xs);
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

  .slack-panel__channel {
    display: flex;
    align-items: center;
    gap: var(--space-xs);
    margin: 0;
  }

  .slack-panel__channel-label {
    color: var(--muted);
    font-size: 0.75rem;
    letter-spacing: 0.04em;
    text-transform: uppercase;
  }

  .slack-panel__channel-code {
    padding: 0.15rem var(--space-2xs);
    border: 1px solid var(--border);
    border-radius: var(--radius-sm);
    background: var(--bg);
    color: var(--ink);
    font-size: 0.85rem;
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
