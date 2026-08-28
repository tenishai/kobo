<script lang="ts">
  // The thread surface (threads arc, Task 10): start a shared, multi-agent
  // conversation from THIS agent's own chat context, loop in further
  // participants the caller can see, and drive it one turn at a time.
  // Always mounted inside the Chat tab's own tabpanel (AgentDetail),
  // alongside ChatPanel and ColleagueConversations -- same "gated on ready
  // by the tab trigger, not by this component" posture ChatPanel's own doc
  // comment documents; `frozen` (destroying/destroy_failed) disables every
  // control here the same way it does there.
  //
  // A thread carries no persisted message log server-side -- `GET /api/
  // threads/{id}` (kobo_api.main._thread_repr) returns only id/
  // initiator_email/created_at/participants, never a transcript -- so, same
  // as ChatPanel's own single-agent chat, the turn-by-turn exchange below is
  // local component state: `sendTranscript` resends the FULL transcript so
  // far on every turn, and a page refresh starts a fresh thread. `thread`
  // itself (the id + participant list) is likewise never fetched back after
  // creation -- `createThread`/`addThreadParticipant` both already return the
  // fresh shape, so there is nothing left to re-GET for this surface.
  //
  // The "suggest a colleague" behavior (`api/kobo_api/composer.py`'s own
  // "name a colleague whose capability fits" instruction) is plain text in
  // an agent's own reply -- there is no structured suggestion on the wire for
  // this component to parse. What this panel offers instead, per the
  // brief's own ruling, is a plain "Add participant" picker of every OTHER
  // ready agent the caller can see: reads the exact same `agentsQuery()` the
  // dashboard and RequestAccessCard already use (server-filtered to
  // visibility, access-control Task B2), filtered here, client-side, to drop
  // whoever's already seated -- purely a display nicety, since the server's
  // own `_add_thread_participant` is the real gate either way.
  //
  // Add-participant refusals (not-ready, already seated, no shared consent,
  // not-found) surface as a toast, not an inline form error -- the brief's
  // own call: adding a participant is a quiet aside to the conversation, not
  // the main flow a blocking form error would interrupt. A start-thread or
  // turn-send failure, by contrast, blocks the thing the caller is actually
  // trying to do right now, so those stay inline (same posture ChatPanel's
  // own chat-panel__error already takes for a failed send).
  import { createMutation, createQuery } from "@tanstack/svelte-query";
  import { ApiError, errorMessage, type ChatMessage, type Thread } from "../lib/api";
  import {
    addThreadParticipantMutation,
    agentsQuery,
    createThreadMutation,
    postThreadTurnMutation,
  } from "../lib/queries";
  import { toast } from "../lib/toast.svelte";

  let { agentName, frozen = false }: { agentName: string; frozen?: boolean } = $props();

  // Client-side cap, same rationale and same numbers as ChatPanel's own
  // MAX_MESSAGES/MAX_MESSAGE_LEN: bounds the transcript re-sent on every
  // turn, and mirrors the server's per-message length cap
  // (CHAT_MESSAGE_TEXT_MAX_LEN, shared by chat and thread turns alike) so an
  // over-length send is refused here instead of round-tripping to a
  // guaranteed 422.
  const MAX_MESSAGES = 50;
  const MAX_MESSAGE_LEN = 4000;

  type ThreadEntry = { role: "user" | "agent"; text: string; agent?: string };

  let thread = $state<Thread | null>(null);
  let transcript = $state<ThreadEntry[]>([]);
  let input = $state("");
  let error = $state<string | null>(null);
  // Same "a 422 can never succeed unchanged" distinction ChatPanel's own
  // `retryable` documents -- shared across both phases (starting the thread,
  // sending a turn) since they never overlap: `thread` is null for the
  // first, set for the second.
  let retryable = $state(true);
  let trimmed = $state(false);
  let inputEl: HTMLTextAreaElement | undefined = $state();

  // svelte-ignore state_referenced_locally
  const startMutation = createMutation(() => (createThreadMutation()));
  // svelte-ignore state_referenced_locally
  const addParticipantMutation = createMutation(() => (addThreadParticipantMutation()));
  // svelte-ignore state_referenced_locally
  const turnMutation = createMutation(() => (postThreadTurnMutation()));
  // svelte-ignore state_referenced_locally
  const agents = createQuery(() => (agentsQuery()));

  const canSend = $derived(
    thread !== null && input.trim().length > 0 && !turnMutation.isPending,
  );

  const eligibleAgents = $derived.by(() => {
    const seated = new Set(thread?.participants ?? []);
    return (agents.data?.agents ?? []).filter(
      (agent) => agent.status === "ready" && !seated.has(agent.name),
    );
  });

  let selectedParticipant = $state<string | null>(null);

  // Keeps the picker's selection pointed at a real option -- same
  // seed-then-reseed shape RequestAccessCard's own `selectedFromAgent`
  // effect documents, so a stale pick (the list changed underneath it, or
  // the just-picked agent got seated) never lingers as a phantom selection.
  $effect(() => {
    if (eligibleAgents.length === 0) {
      selectedParticipant = null;
      return;
    }
    if (
      selectedParticipant === null ||
      !eligibleAgents.some((agent) => agent.name === selectedParticipant)
    ) {
      selectedParticipant = eligibleAgents[0].name;
    }
  });

  function focusInput(): void {
    inputEl?.focus();
  }

  function capTranscript(entries: ThreadEntry[]): ThreadEntry[] {
    if (entries.length <= MAX_MESSAGES) {
      return entries;
    }
    trimmed = true;
    return entries.slice(entries.length - MAX_MESSAGES);
  }

  function toChatMessages(entries: ThreadEntry[]): ChatMessage[] {
    return entries.map((entry) => ({ role: entry.role, text: entry.text }));
  }

  function handleStart(): void {
    if (startMutation.isPending) {
      return;
    }
    error = null;
    startMutation.mutate(agentName, {
      onSuccess: (result) => {
        thread = result;
      },
      onError: (err: unknown) => {
        error = errorMessage(err, "Couldn't start a thread. Try again.");
      },
    });
  }

  function handleAddParticipant(event: SubmitEvent): void {
    event.preventDefault();
    if (thread === null || selectedParticipant === null || addParticipantMutation.isPending) {
      return;
    }
    const threadId = thread.id;
    const agent = selectedParticipant;
    addParticipantMutation.mutate(
      { threadId, agent },
      {
        onSuccess: (result) => {
          thread = result;
        },
        onError: (err: unknown) => {
          toast.error(
            `Couldn't add ${agent}`,
            errorMessage(err, "Something went wrong."),
          );
        },
      },
    );
  }

  /** Sends `entries` (a full transcript snapshot) as one turn. Shared by the
   * initial send and the retry affordance, same reasoning ChatPanel's own
   * `sendTranscript` documents: a retry re-plays exactly what failed. */
  function sendTranscript(entries: ThreadEntry[]): void {
    if (thread === null) {
      return;
    }
    const threadId = thread.id;
    error = null;
    turnMutation.mutate(
      { threadId, messages: toChatMessages(entries) },
      {
        onSuccess: (result) => {
          const replies: ThreadEntry[] = result.replies.map((reply) => ({
            role: "agent",
            text: reply.text,
            agent: reply.agent,
          }));
          transcript = capTranscript([...transcript, ...replies]);
          focusInput();
        },
        onError: (err: unknown) => {
          if (err instanceof ApiError && err.status === 422) {
            // Same poisoned-bubble recovery ChatPanel's own onError takes:
            // the payload itself was rejected, so resending it unchanged
            // would 422 forever. Pull it back out of the transcript and
            // into the input so it can be edited and resent.
            const last = entries[entries.length - 1];
            if (last.role === "user") {
              transcript = entries.slice(0, -1);
              input = last.text;
            }
            retryable = false;
          } else {
            retryable = true;
          }
          error = errorMessage(err, "The thread didn't get a reply. Try again.");
        },
      },
    );
  }

  function handleSend(): void {
    const text = input.trim();
    if (text.length === 0 || turnMutation.isPending || thread === null) {
      return;
    }
    input = "";
    const next = capTranscript([...transcript, { role: "user", text }]);
    transcript = next;
    sendTranscript(next);
  }

  function handleRetry(): void {
    if (turnMutation.isPending) {
      return;
    }
    sendTranscript(transcript);
  }

  function handleKeydown(event: KeyboardEvent): void {
    if (event.key === "Enter" && !event.shiftKey) {
      event.preventDefault();
      handleSend();
    }
  }
</script>

<section class="thread-panel" aria-labelledby="thread-panel-heading">
  <h2 id="thread-panel-heading" class="thread-panel__heading">Shared thread</h2>

  {#if thread === null}
    <p class="thread-panel__explainer">
      Loop other agents into this conversation. Everyone in the thread sees the same messages,
      one turn at a time.
    </p>
    {#if error}
      <p class="thread-panel__error" role="alert">{error}</p>
    {/if}
    <button
      type="button"
      class="thread-panel__start"
      disabled={frozen || startMutation.isPending}
      onclick={handleStart}
    >
      {startMutation.isPending ? "Starting…" : "Start a shared thread"}
    </button>
  {:else}
    <ul class="thread-panel__participants" aria-label="Thread participants">
      {#each thread.participants as name (name)}
        <li class="thread-panel__chip">{name}</li>
      {/each}
    </ul>

    <form class="thread-panel__add-form" onsubmit={handleAddParticipant}>
      <label class="thread-panel__add-label" for="thread-add-participant">Add participant</label>
      {#if agents.isPending}
        <p class="thread-panel__hint">Loading agents…</p>
      {:else if eligibleAgents.length === 0}
        <p class="thread-panel__hint">No other agents to add right now.</p>
      {:else}
        <div class="thread-panel__add-row">
          <select
            id="thread-add-participant"
            class="thread-panel__add-select"
            bind:value={selectedParticipant}
            disabled={frozen}
          >
            {#each eligibleAgents as agent (agent.name)}
              <option value={agent.name}>{agent.name}</option>
            {/each}
          </select>
          <button
            type="submit"
            class="thread-panel__add-submit"
            disabled={frozen || selectedParticipant === null || addParticipantMutation.isPending}
          >
            {addParticipantMutation.isPending ? "Adding…" : "Add"}
          </button>
        </div>
      {/if}
    </form>

    <div class="thread-panel__log" role="log" aria-live="polite">
      {#if trimmed}
        <p class="thread-panel__notice">Older messages were trimmed to keep this short.</p>
      {/if}
      {#each transcript as entry, i (i)}
        <div class="thread-bubble thread-bubble--{entry.role}">
          <span class="thread-panel__sr-only"
            >{entry.role === "user" ? "You" : (entry.agent ?? "Agent")}:</span
          >
          {#if entry.role === "agent"}
            <p class="thread-bubble__attribution" aria-hidden="true">{entry.agent}</p>
          {/if}
          <p class="thread-bubble__text">{entry.text}</p>
        </div>
      {/each}
      {#if turnMutation.isPending}
        <div class="thread-bubble thread-bubble--agent thread-bubble--pending">
          <p class="thread-bubble__text">
            <span class="thread-panel__thinking-dot pulse" aria-hidden="true"></span>
            thinking…
          </p>
        </div>
      {/if}
    </div>

    {#if error}
      <div class="thread-panel__turn-error" role="alert">
        <p class="thread-panel__turn-error-text">{error}</p>
        {#if retryable}
          <button
            type="button"
            class="thread-panel__retry"
            disabled={frozen || turnMutation.isPending}
            onclick={handleRetry}
          >
            Try again
          </button>
        {/if}
      </div>
    {/if}

    <div class="thread-panel__form">
      <label class="thread-panel__label" for="thread-panel-input">Message the thread</label>
      <textarea
        id="thread-panel-input"
        class="thread-panel__input"
        bind:this={inputEl}
        bind:value={input}
        onkeydown={handleKeydown}
        placeholder="Say something…"
        rows="2"
        maxlength={MAX_MESSAGE_LEN}
        disabled={frozen}
      ></textarea>
      <button
        type="button"
        class="thread-panel__send"
        disabled={frozen || !canSend}
        onclick={handleSend}
      >
        {turnMutation.isPending ? "Sending…" : "Send"}
      </button>
    </div>
  {/if}
</section>

<style>
  .thread-panel {
    display: flex;
    flex-direction: column;
    gap: var(--space-md);
    padding: var(--space-lg);
    border: 1px solid var(--border);
    border-radius: var(--radius);
    background: var(--panel);
  }

  .thread-panel__heading {
    margin: 0;
    color: var(--muted);
    font-size: 0.8rem;
    font-weight: 600;
    letter-spacing: 0.06em;
    text-transform: uppercase;
  }

  .thread-panel__sr-only {
    position: absolute;
    width: 1px;
    height: 1px;
    overflow: hidden;
    clip: rect(0, 0, 0, 0);
    white-space: nowrap;
  }

  .thread-panel__explainer {
    margin: 0;
    color: var(--muted);
    font-size: 0.85rem;
  }

  .thread-panel__error {
    margin: 0;
    color: var(--fail);
    font-size: 0.85rem;
  }

  .thread-panel__start {
    align-self: flex-start;
    padding: var(--space-xs) var(--space-md);
    border: none;
    border-radius: var(--radius-sm);
    background: var(--accent);
    color: var(--accent-ink);
    font-weight: 600;
    cursor: pointer;
  }

  .thread-panel__start:hover:not(:disabled) {
    filter: brightness(1.05);
  }

  .thread-panel__start:disabled {
    opacity: 0.5;
    cursor: not-allowed;
  }

  .thread-panel__participants {
    display: flex;
    flex-wrap: wrap;
    gap: var(--space-2xs);
    margin: 0;
    padding: 0;
    list-style: none;
  }

  .thread-panel__chip {
    padding: 0.15rem var(--space-sm);
    border: 1px solid var(--border);
    border-radius: 999px;
    color: var(--ink);
    font-size: 0.8rem;
    font-weight: 600;
  }

  .thread-panel__add-form {
    display: flex;
    flex-direction: column;
    gap: var(--space-2xs);
  }

  .thread-panel__add-label {
    color: var(--ink);
    font-weight: 600;
    font-size: 0.85rem;
  }

  .thread-panel__hint {
    margin: 0;
    color: var(--muted);
    font-size: 0.8rem;
  }

  .thread-panel__add-row {
    display: flex;
    gap: var(--space-sm);
  }

  .thread-panel__add-select {
    flex: 1;
    padding: var(--space-xs) var(--space-sm);
    border: 1px solid var(--border);
    border-radius: var(--radius-sm);
    background: var(--panel);
    color: var(--ink);
    font: inherit;
  }

  .thread-panel__add-submit {
    padding: var(--space-xs) var(--space-md);
    border: 1px solid var(--border);
    border-radius: var(--radius-sm);
    background: var(--panel);
    color: var(--ink);
    font-weight: 600;
    cursor: pointer;
    white-space: nowrap;
  }

  .thread-panel__add-submit:hover:not(:disabled) {
    border-color: var(--accent);
    color: var(--accent);
  }

  .thread-panel__add-submit:disabled {
    opacity: 0.5;
    cursor: not-allowed;
  }

  .thread-panel__log {
    display: flex;
    flex-direction: column;
    gap: var(--space-sm);
  }

  .thread-panel__notice {
    margin: 0 0 var(--space-2xs);
    color: var(--muted);
    font-size: 0.8rem;
    text-align: center;
  }

  .thread-bubble {
    max-width: 80%;
    padding: var(--space-xs) var(--space-sm);
    border: 1px solid var(--border);
    border-radius: var(--radius-sm);
  }

  .thread-bubble--user {
    align-self: flex-end;
    border-color: var(--accent);
    background: color-mix(in srgb, var(--accent) 10%, var(--panel));
  }

  .thread-bubble--agent {
    align-self: flex-start;
    background: var(--bg);
  }

  .thread-bubble__attribution {
    margin: 0 0 0.15rem;
    color: var(--muted);
    font-size: 0.75rem;
    font-weight: 600;
    text-transform: uppercase;
    letter-spacing: 0.04em;
  }

  .thread-bubble__text {
    margin: 0;
    color: var(--ink);
    font-size: 0.95rem;
    line-height: 1.6;
    white-space: pre-wrap;
  }

  .thread-panel__thinking-dot {
    display: inline-block;
    width: 0.5rem;
    height: 0.5rem;
    margin-right: var(--space-2xs);
    border-radius: 50%;
    background: var(--pulse);
  }

  .thread-panel__turn-error {
    display: flex;
    flex-wrap: wrap;
    align-items: center;
    justify-content: space-between;
    gap: var(--space-sm);
    padding: var(--space-sm) var(--space-md);
    border: 1px solid var(--fail);
    border-radius: var(--radius-sm);
    background: color-mix(in srgb, var(--fail) 8%, var(--panel));
  }

  .thread-panel__turn-error-text {
    margin: 0;
    color: var(--fail);
    font-size: 0.85rem;
  }

  .thread-panel__retry {
    padding: var(--space-2xs) var(--space-sm);
    border: 1px solid var(--fail);
    border-radius: var(--radius-sm);
    background: var(--panel);
    color: var(--fail);
    font-weight: 600;
    cursor: pointer;
    white-space: nowrap;
  }

  .thread-panel__retry:hover:not(:disabled) {
    filter: brightness(1.05);
  }

  .thread-panel__retry:disabled {
    opacity: 0.6;
    cursor: not-allowed;
  }

  .thread-panel__form {
    display: flex;
    flex-direction: column;
    gap: var(--space-2xs);
  }

  .thread-panel__label {
    color: var(--ink);
    font-weight: 600;
    font-size: 0.85rem;
  }

  .thread-panel__input {
    padding: var(--space-xs) var(--space-sm);
    border: 1px solid var(--border);
    border-radius: var(--radius-sm);
    background: var(--panel);
    color: var(--ink);
    font: inherit;
    resize: vertical;
  }

  .thread-panel__input:focus-visible {
    outline: 2px solid var(--accent);
    outline-offset: 1px;
  }

  .thread-panel__send {
    align-self: flex-end;
    padding: var(--space-xs) var(--space-md);
    border: none;
    border-radius: var(--radius-sm);
    background: var(--accent);
    color: var(--accent-ink);
    font-weight: 600;
    cursor: pointer;
  }

  .thread-panel__send:hover:not(:disabled) {
    filter: brightness(1.05);
  }

  .thread-panel__send:disabled {
    opacity: 0.5;
    cursor: not-allowed;
  }
</style>
