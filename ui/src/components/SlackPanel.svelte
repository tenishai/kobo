<script lang="ts">
  // Slack, per agent (first-run wave, 2026-09-23) -- the one-time ceremony
  // that creates the Slack app, and the channels the agent answers in.
  //
  // TWO LAYOUTS, not one panel with a drawer. Yesterday's cut put the
  // channels table first and hid the whole ceremony behind a "Set up the
  // Slack app" disclosure. Operators read that as optional: the manifest
  // was a block of text nobody thought to click, and the setup did not look
  // like the thing you must do before Slack works at all. So:
  //
  //   Not connected -> the panel IS the setup. A numbered stepper, nothing
  //   else on screen: make the app from the manifest, paste the three
  //   values back, say where it answers. One primary button leads: "Copy
  //   the manifest" while the paste fields are empty, "Connect" the moment
  //   anything has been pasted.
  //
  //   Connected (and waiting, and while connect is in flight) -> the
  //   channels table is the whole panel, with one Save. The ceremony lives
  //   behind a quiet "Reconnect the Slack app" disclosure at the bottom,
  //   shut, for the day a token has to be replaced.
  //
  // Both lanes read the same `slack` prop (`AgentDetail.slack`, refetched by
  // the caller's agentQuery -- see AgentDetail.svelte): `channels` and
  // `state` say what is saved and how wired-up it is. Which layout shows is
  // derived from `slack.state`, not from a phase flag of this component's
  // own, so a connect success (which invalidates the agent detail query)
  // moves the panel from the stepper to the table for free once the refetch
  // lands.
  //
  // WHAT THE BUTTON SENDS. The two calls behind this panel are unchanged:
  // PUT .../slack/channels with the cleaned channel list, and POST
  // .../slack/connect with the app id and both tokens. Connect and Save are
  // the same handler under two labels: each sends whichever of the two has
  // something to send, and sends the channels FIRST when it sends both --
  // connect refuses an agent with no channel, and a person who filled in
  // everything at once should not have to press the button twice to get
  // past a precondition they already satisfied.
  //
  // Edits are PENDING until that press, the same discipline as
  // SecretsPanel: adding a row, typing in one and removing one all change a
  // draft, and Cancel/Discard throws the draft away and puts the saved list
  // back.
  //
  // The one thing that *is* local state independent of `slack` is the
  // delivery chip's sha store (shared by both calls), and it's deliberately
  // NOT reset when `slack` changes shape -- a save that just fired keeps its
  // chip alive across the next refetch. See MemoryPanel's doc comment for
  // why this is a plain `writable`/`derived` pair rather than `$state` --
  // same @tanstack/svelte-query version quirk applies here (deliveryQuery
  // needs a *new* query per sha, and only a real Svelte store makes
  // createQuery re-derive its options after the first one).
  import { createMutation, createQuery, useQueryClient } from "@tanstack/svelte-query";
  import { errorMessage, type AgentSlack, type SlackWake } from "../lib/api";
  import {
    connectSlackMutation,
    deliveryQuery,
    saveSlackChannelsMutation,
    setSlackWakeMutation,
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
  // svelte-ignore state_referenced_locally
  const saveWake = createMutation(() => (setSlackWakeMutation(queryClient, agentName)));

  // --- How it wakes up (2026-10-06) ------------------------------------------
  //
  // Saved the moment it is picked, not on the panel's Save: on the first run
  // the manifest in step 1 has to carry whatever step 3 chose, and a choice
  // that waits behind a button is a manifest copied with the wrong events.
  // The server says whether the Slack app's event list changed, which only
  // a re-applied manifest can carry out; the note under the choices says so.
  const WAKE_OPTIONS: { value: SlackWake; label: string; help: string }[] = [
    {
      value: "mention",
      label: "Mentions only",
      help: "Only a message that @-mentions the agent reaches it.",
    },
    {
      value: "thread",
      label: "Mentions, then the thread",
      help: "Mention it once at the top of a thread and every reply there reaches it.",
    },
    {
      value: "channel",
      label: "Every message in the channel",
      help: "For an agent whose job is the channel. It hears everything.",
    },
  ];

  // svelte-ignore state_referenced_locally
  let wake = $state<SlackWake>(slack.wake);
  let wakeNote = $state<string | null>(null);
  let wakeError = $state<string | null>(null);

  async function handleWake(next: SlackWake): Promise<void> {
    if (frozen || next === wake) {
      return;
    }
    const previous = wake;
    wake = next;
    wakeNote = null;
    wakeError = null;
    try {
      const result = await saveWake.mutateAsync(next);
      if (result.manifest_changed && slack.state !== "idle") {
        wakeNote =
          "Saved. The Slack app's event list changed: copy the manifest again and paste it " +
          "over the old one at api.slack.com/apps, under App Manifest. No reinstall needed.";
      } else {
        wakeNote = "Saved.";
      }
    } catch (err: unknown) {
      wake = previous;
      wakeError = errorMessage(err, "Couldn't save how it wakes up. Try again.");
    }
  }

  // --- Which layout ---------------------------------------------------------

  /** The first run: no app yet, so the panel is the ceremony and nothing
   * else. The moment connect is in flight the panel moves on to the
   * connected layout, which is where the "Connecting" chip and the progress
   * line belong; a connect that fails comes straight back here with every
   * pasted value still in its field. */
  const firstRun = $derived(slack.state === "idle" && !connect.isPending);

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

  /** The one line under the heading while the app is on its way to the
   * agent. Null once it has arrived, when the plain connected lead shows
   * instead. */
  const stateNote = $derived.by((): string | null => {
    if (connect.isPending) {
      return "Sending the app details to the agent.";
    }
    if (slack.state === "waiting") {
      return "The app details are with the agent. It answers as soon as it picks them up.";
    }
    return null;
  });

  // --- The channels draft ---------------------------------------------------

  // Same client-side courtesy cap as the server's `MAX_CHANNELS`
  // (kobo_api.slack_state) — disables "+ Channel" before the round trip,
  // but the server is still the real gate: a request that somehow exceeds
  // this comes back 422 with its own "Eight channels at most for now."
  // copy, rendered verbatim through `saveError` below.
  const MAX_CHANNELS = 8;

  // The channel list is an ALLOWLIST, not a subscription: the runtime
  // ignores messages from channels that are not on it, and its Slack read
  // tool is restricted to them. The Slack invite gives membership, this
  // list gives permission, and both are needed — which is why the sentence
  // below asks for both.
  //
  // WHEN THE RUNTIME CHANGES: a proposal is out to make an empty list mean
  // "every channel the app is invited to". The day that lands, this
  // sentence stops being true for the empty case and becomes something like
  // "leave it empty and the agent answers wherever it is invited"; the
  // channel rows stay, as the way to narrow that down.
  const channelsNote = $derived(
    `Add each channel's ID, then invite @${agentName} to it in Slack. ` +
      `The agent answers only in channels listed here.`,
  );

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
  // pasted value can be checked before the button is pressed. Purely
  // client-side and pre-submit — it changes nothing about the
  // never-stored-by-the-room guarantee below, since the value never leaves
  // this input either way.
  let showBotToken = $state(false);
  let showAppToken = $state(false);

  /** Connect takes all three or none: two of the three is not a state the
   * server has anything to do with. */
  const setupPending = $derived(
    appId.trim().length > 0 && botToken.trim().length > 0 && appToken.trim().length > 0,
  );

  /** Anything at all pasted into step 2. This is what hands the primary
   * over from "Copy the manifest" to "Connect": somebody who has started
   * pasting is past the manifest, and one panel gets one filled button. */
  const setupTouched = $derived(
    appId.trim().length > 0 || botToken.trim().length > 0 || appToken.trim().length > 0,
  );

  const copyIsPrimary = $derived(firstRun && !setupTouched);

  /** Re-tokening, long after the first run. Shut by default: a connected
   * agent's panel is about its channels. */
  let reconnectOpen = $state(false);

  /** Where the two ceremony steps are on screen: the whole first-run panel,
   * or the reconnect disclosure. */
  const setupVisible = $derived(firstRun || reconnectOpen);

  // --- Saving ---------------------------------------------------------------

  const saving = $derived(saveChannels.isPending || connect.isPending);
  const hasPending = $derived(channelsPending || setupPending);
  const canSave = $derived(!frozen && !saving && hasPending);

  /** Connect takes the three values AND a channel: the server refuses an
   * agent with none (409 below), so the button says so by staying off
   * rather than by failing. */
  const canConnect = $derived(
    !frozen && !saving && setupPending && cleaned.length > 0,
  );

  // One press, one word for it: a first-run Connect that saves the channels
  // on its way to the connect call still reads "Connecting…", because that
  // is what the human asked for.
  const primaryLabel = $derived.by((): string => {
    if (firstRun) {
      return saving ? "Connecting…" : "Connect";
    }
    if (connect.isPending) {
      return "Connecting…";
    }
    if (saveChannels.isPending) {
      return "Saving…";
    }
    return "Save";
  });

  // The server's own copy for the missing-channels precondition (POST
  // .../slack/connect, 409 — see `kobo_api.main.connect_slack`), matched
  // verbatim so the swap below only fires for that exact case and never
  // masks a different server error under the wrong warning.
  const MISSING_CHANNELS_ERROR = "Add at least one channel first.";

  /** Sends whatever changed, through the same two calls as ever — the
   * handler behind Connect on the first run and behind Save after it. The
   * three setup fields are only cleared on success, once they're actually
   * spent — any server error (including the missing-channels 409) leaves
   * them exactly as pasted, since tokens are never persisted server-side
   * and re-pasting them is a real cost to the human. */
  async function handleSave(): Promise<void> {
    if (firstRun ? !canConnect : !canSave) {
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
        reconnectOpen = false;
        beginDelivery(result.sha);
      } catch (err: unknown) {
        const message = errorMessage(err, "Couldn't finish setting up Slack. Try again.");
        saveError =
          message === MISSING_CHANNELS_ERROR
            ? "Add at least one channel, then press Connect again. Your values are kept."
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

  // --- The manifest ---------------------------------------------------------
  //
  // Fetched as soon as step 1 is on screen, not on a click: the copy button
  // is the primary control of the first run, and a primary that has to be
  // armed by a second, quieter button first is the exact confusion this
  // wave is undoing. Still not fetched on mount of the *panel* — an agent
  // that is already connected asks for nothing until somebody opens the
  // reconnect disclosure, which is most visits.
  const manifest = createQuery(() => (slackManifestQuery(agentName, setupVisible)));

  /** The `<pre>`, for somebody who wants to read what they are pasting.
   * Shut by default: the copy button is the path, this is the reassurance. */
  let manifestShown = $state(false);

  // The exact pretty-printed string rendered in the `<pre>` below, kept as a
  // single source: the copy button hands this same value to the clipboard,
  // so there's never a second `JSON.stringify` call that could drift from
  // what the human actually sees.
  const manifestText = $derived(
    manifest.data ? JSON.stringify(manifest.data, null, 2) : undefined,
  );

  const COPIED_CLEAR_MS = 2000;

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

  // "Copied" is an answer, not a state to live in: it says the press landed
  // and then gets out of the way, so the button reads as pressable again
  // for the second copy.
  $effect(() => {
    if (copyState !== "copied") {
      return;
    }
    const timer = setTimeout(() => {
      copyState = "idle";
    }, COPIED_CLEAR_MS);
    return () => clearTimeout(timer);
  });

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

{#snippet manifestStep()}
  <li class="slack-panel__step">
    <h3 class="slack-panel__step-title">Create the Slack app from this manifest</h3>
    <div class="slack-panel__step-body">
      <p class="panel__note">
        At
        <a
          class="slack-panel__link"
          href="https://api.slack.com/apps"
          target="_blank"
          rel="noopener noreferrer"
        >
          api.slack.com/apps
          <span class="panel__sr"> (opens Slack)</span>
        </a>
        press Create New App, choose From a manifest, pick your workspace, paste this on the
        JSON tab.
      </p>
      <div class="slack-panel__step-actions">
        <button
          type="button"
          class="btn"
          class:btn--primary={copyIsPrimary}
          class:btn--quiet={!copyIsPrimary}
          disabled={frozen || manifestText === undefined}
          onclick={handleCopyManifest}
        >
          {copyState === "copied" ? "Copied" : "Copy the manifest"}
        </button>
        <button
          type="button"
          class="btn btn--quiet"
          aria-expanded={manifestShown}
          aria-controls="slack-panel-manifest"
          onclick={() => (manifestShown = !manifestShown)}
        >
          {manifestShown ? "Hide the manifest" : "Show the manifest"}
        </button>
      </div>
      {#if copyState === "failed"}
        <p class="panel__note panel__note--fail" role="alert">
          Copy failed. Select the text instead.
        </p>
      {/if}
      {#if manifestShown}
        <div id="slack-panel-manifest" class="slack-panel__manifest">
          {#if manifest.isPending}
            <p class="panel__note">Loading the manifest…</p>
          {:else if manifest.isError}
            <p class="panel__note panel__note--fail" role="alert">
              {errorMessage(manifest.error, "Couldn't load the manifest.")}
            </p>
          {:else if manifestText !== undefined}
            <pre class="slack-panel__manifest-block">{manifestText}</pre>
          {/if}
        </div>
      {/if}
    </div>
  </li>
{/snippet}

{#snippet valuesStep()}
  <li class="slack-panel__step">
    <h3 class="slack-panel__step-title">Paste the three values back</h3>
    <div class="slack-panel__step-body">
      <div class="slack-panel__fields">
        <div class="field slack-panel__field">
          <label class="field__label" for="slack-panel-app-id">App ID</label>
          <input
            id="slack-panel-app-id"
            class="field__input slack-panel__mono"
            type="text"
            autocomplete="off"
            spellcheck="false"
            placeholder="A0123456789"
            bind:value={appId}
            disabled={frozen}
          />
          <p class="field__help">Basic Information shows the App ID.</p>
        </div>
        <div class="field slack-panel__field">
          <label class="field__label" for="slack-panel-bot-token">Bot token</label>
          <div class="field__row">
            <input
              id="slack-panel-bot-token"
              class="field__input slack-panel__mono"
              type={showBotToken ? "text" : "password"}
              autocomplete="off"
              spellcheck="false"
              placeholder="xoxb-..."
              bind:value={botToken}
              disabled={frozen}
            />
            <button
              type="button"
              class="btn btn--quiet"
              disabled={frozen}
              aria-label={showBotToken ? "Hide bot token" : "Show bot token"}
              onclick={() => (showBotToken = !showBotToken)}
            >
              {showBotToken ? "Hide" : "Show"}
            </button>
          </div>
          <p class="field__help">Install App gives the bot token.</p>
        </div>
        <div class="field slack-panel__field">
          <label class="field__label" for="slack-panel-app-token">App-level token</label>
          <div class="field__row">
            <input
              id="slack-panel-app-token"
              class="field__input slack-panel__mono"
              type={showAppToken ? "text" : "password"}
              autocomplete="off"
              spellcheck="false"
              placeholder="xapp-..."
              bind:value={appToken}
              disabled={frozen}
            />
            <button
              type="button"
              class="btn btn--quiet"
              disabled={frozen}
              aria-label={showAppToken ? "Hide app-level token" : "Show app-level token"}
              onclick={() => (showAppToken = !showAppToken)}
            >
              {showAppToken ? "Hide" : "Show"}
            </button>
          </div>
          <p class="field__help">
            Generate one on Basic Information with the connections:write scope.
          </p>
        </div>
      </div>
      <p class="panel__note">
        For safety, the room never keeps these values. They go straight to the agent.
      </p>
    </div>
  </li>
{/snippet}

{#snippet wakeChoice()}
  <fieldset class="slack-panel__wake" disabled={frozen || saveWake.isPending}>
    <legend class="field__label">Wakes on</legend>
    {#each WAKE_OPTIONS as option (option.value)}
      <label class="slack-panel__wake-option">
        <input
          type="radio"
          name="slack-panel-wake"
          value={option.value}
          checked={wake === option.value}
          disabled={frozen || saveWake.isPending}
          onchange={() => handleWake(option.value)}
        />
        <span class="slack-panel__wake-text">
          <span class="slack-panel__wake-label">{option.label}</span>
          <span class="field__help">{option.help}</span>
        </span>
      </label>
    {/each}
    {#if wakeError}
      <p class="panel__note panel__note--fail" role="alert">{wakeError}</p>
    {:else if wakeNote}
      <p class="panel__note" role="status">{wakeNote}</p>
    {:else}
      <p class="field__help">A direct message always reaches the agent.</p>
    {/if}
  </fieldset>
{/snippet}

{#snippet channels()}
  <div class="slack-panel__channels">
    <p class="panel__note">{channelsNote}</p>
    <div class="panel__toolbar">
      <button
        type="button"
        class="btn"
        disabled={frozen || rows.length >= MAX_CHANNELS}
        onclick={addRow}
      >
        + Channel
      </button>
    </div>
    <table class="panel__table slack-panel__table">
      <thead>
        <tr>
          <th scope="col">Channel ID</th>
          <th scope="col" class="slack-panel__col-remove">
            <span class="panel__sr">Remove</span>
          </th>
        </tr>
      </thead>
      <tbody>
        {#if rows.length === 0}
          <tr>
            <td class="panel__empty" colspan="2">No channels yet.</td>
          </tr>
        {/if}
        {#each rows as row, index (row.id)}
          <tr>
            <td>
              <input
                class="slack-panel__cell-input"
                type="text"
                autocomplete="off"
                spellcheck="false"
                aria-label={`Channel ${index + 1}`}
                bind:value={row.value}
                disabled={frozen}
                use:focusOnMount={row.id === focusId}
              />
            </td>
            <td class="panel__cell--right">
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
  </div>
{/snippet}

<section class="panel" aria-labelledby="slack-panel-heading">
  <div class="panel__header">
    <div class="slack-panel__title">
      <h2 id="slack-panel-heading" class="panel__heading">Slack</h2>
      <span class="slack-panel__chip slack-panel__chip--{chipTone}">{chipText}</span>
    </div>
    {#if !firstRun}
      <p class="panel__lead">
        {stateNote ?? "The Slack app is connected to this agent."}
      </p>
    {/if}
  </div>

  {#if firstRun}
    <ol class="slack-panel__steps">
      {@render manifestStep()}
      {@render valuesStep()}
      <li class="slack-panel__step">
        <h3 class="slack-panel__step-title">Where it answers</h3>
        <div class="slack-panel__step-body">
          {@render channels()}
          {@render wakeChoice()}
        </div>
      </li>
    </ol>
  {:else}
    {@render channels()}
    {@render wakeChoice()}

    <div class="slack-panel__reconnect">
      <button
        type="button"
        class="btn btn--quiet"
        aria-expanded={reconnectOpen}
        aria-controls="slack-panel-reconnect"
        onclick={() => (reconnectOpen = !reconnectOpen)}
      >
        Reconnect the Slack app
      </button>
      {#if reconnectOpen}
        <ol class="slack-panel__steps" id="slack-panel-reconnect">
          {@render manifestStep()}
          {@render valuesStep()}
        </ol>
      {/if}
    </div>
  {/if}

  <div class="panel__footer">
    {#if saveError}
      <p class="panel__status panel__status--fail" role="alert">{saveError}</p>
    {:else if deliverySha !== null}
      <p
        class="panel__status slack-panel__status--{deliveryView.tone}"
        role="status"
        aria-live="polite"
      >
        {#if deliveryView.tone === "pending"}
          <span class="slack-panel__delivery-dot pulse" aria-hidden="true"></span>
        {/if}
        <span>{deliveryView.text}</span>
      </p>
    {/if}
    <div class="panel__actions">
      {#if firstRun}
        <button
          type="button"
          class="btn btn--quiet"
          disabled={frozen || saving || !hasPending}
          onclick={discard}
        >
          Cancel
        </button>
        <button
          type="button"
          class="btn"
          class:btn--primary={!copyIsPrimary}
          disabled={!canConnect}
          onclick={handleSave}
        >
          {primaryLabel}
        </button>
      {:else}
        {#if hasPending}
          <button
            type="button"
            class="btn btn--quiet"
            disabled={frozen || saving}
            onclick={discard}
          >
            Discard
          </button>
        {/if}
        <button
          type="button"
          class="btn btn--primary"
          disabled={!canSave}
          onclick={handleSave}
        >
          {primaryLabel}
        </button>
      {/if}
    </div>
  </div>
</section>

<style>
  .slack-panel__wake {
    display: flex;
    flex-direction: column;
    gap: var(--space-xs);
    margin: 0;
    padding: 0;
    border: 0;
    min-width: 0;
  }
  .slack-panel__wake-option {
    display: grid;
    grid-template-columns: auto 1fr;
    align-items: baseline;
    column-gap: var(--space-xs);
    cursor: pointer;
  }
  .slack-panel__wake-text {
    display: flex;
    flex-direction: column;
    min-width: 0;
  }
  .slack-panel__wake-label {
    color: var(--ink);
    font-size: 0.9rem;
  }
  .slack-panel__title {
    display: flex;
    flex-wrap: wrap;
    align-items: center;
    gap: var(--space-xs);
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

  /* The stepper. Numbered down the left, one step per row, so the order is
     the layout rather than something the copy has to claim. */
  .slack-panel__steps {
    display: flex;
    flex-direction: column;
    gap: var(--space-md);
    margin: 0;
    padding: 0;
    list-style: none;
    counter-reset: ceremony-step;
  }

  .slack-panel__step {
    display: grid;
    grid-template-columns: 1.5rem 1fr;
    align-items: baseline;
    column-gap: var(--space-xs);
    row-gap: var(--space-xs);
    counter-increment: ceremony-step;
  }

  .slack-panel__step::before {
    content: counter(ceremony-step);
    display: grid;
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

  .slack-panel__step-title {
    margin: 0;
    color: var(--ink);
    font-size: 0.9rem;
    font-weight: 600;
  }

  .slack-panel__step-body {
    display: flex;
    flex-direction: column;
    grid-column: 2;
    gap: var(--space-xs);
    min-width: 0;
  }

  .slack-panel__step-actions {
    display: flex;
    flex-wrap: wrap;
    align-items: center;
    gap: var(--space-xs);
  }

  .slack-panel__link {
    color: var(--link);
    text-decoration: none;
  }

  .slack-panel__link:hover {
    text-decoration: underline;
  }

  .slack-panel__manifest {
    display: flex;
    flex-direction: column;
    min-width: 0;
  }

  .slack-panel__manifest-block {
    max-width: 100%;
    max-height: 16rem;
    margin: 0;
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

  .slack-panel__fields {
    display: flex;
    flex-wrap: wrap;
    gap: var(--space-sm);
  }

  .slack-panel__field {
    flex: 1 1 12rem;
    min-width: 0;
  }

  .slack-panel__mono {
    font-family: var(--font-mono);
    font-size: 0.8rem;
  }

  .slack-panel__channels {
    display: flex;
    flex-direction: column;
    gap: var(--space-xs);
  }

  .slack-panel__table {
    table-layout: fixed;
  }

  .slack-panel__col-remove {
    width: 2.25rem;
  }

  /* A field that lives in a table cell: no frame of its own until it is
     reached for, so a list of eight reads as a list and not as a form. */
  .slack-panel__cell-input {
    width: 100%;
    padding: var(--space-2xs) var(--space-xs);
    border: 1px solid transparent;
    border-radius: var(--radius-sm);
    background: transparent;
    color: var(--ink);
    font-family: var(--font-mono);
    font-size: 0.8rem;
  }

  .slack-panel__cell-input:hover:not(:disabled) {
    border-color: var(--border);
  }

  .slack-panel__cell-input:focus-visible {
    border-color: transparent;
    background: var(--bg);
    outline: 2px solid var(--accent);
    outline-offset: -1px;
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

  .slack-panel__reconnect {
    display: flex;
    flex-direction: column;
    align-items: flex-start;
    gap: var(--space-sm);
    padding-top: var(--space-sm);
    border-top: 1px solid var(--border);
  }

  .slack-panel__reconnect .slack-panel__steps {
    width: 100%;
  }

  .slack-panel__status--ok {
    color: var(--ok);
  }

  .slack-panel__status--fail {
    color: var(--fail);
  }

  .slack-panel__delivery-dot {
    width: 0.5rem;
    height: 0.5rem;
    border-radius: 50%;
    background: var(--pulse);
  }
</style>
