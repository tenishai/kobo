<script lang="ts">
  // A live conversation with an already-ready agent. Always mounted inside
  // its own tabpanel (AgentDetail) regardless of the agent's status — the
  // server gates the actual chat mutation until `status === "ready"`
  // (a 409 otherwise), and AgentDetail disables the Chat tab TRIGGER for
  // every other non-frozen status so that gate is never reached by
  // clicking in from here. This component just owns the back-and-forth
  // once the agent is ready to have one.
  //
  // The transcript is local component state, not server state: there's no
  // persistence and no cache to invalidate (see chatMutation in queries.ts),
  // so a page refresh starts a fresh conversation. "First words" (the
  // agent's very first reply, shown in its own card by the caller) is never
  // folded into this transcript — the two are deliberately separate.
  //
  // Each send re-plays the *entire* transcript (all prior turns plus the new
  // message) to the API, since the mutation is stateless from the server's
  // point of view — the agent has no memory of past turns except what's
  // resent to it here.
  import { createMutation } from "@tanstack/svelte-query";
  import { ApiError, errorMessage, type ChatMessage } from "../lib/api";
  import { chatMutation } from "../lib/queries";
  import TeachModal from "./TeachModal.svelte";

  let { agentName, frozen = false }: { agentName: string; frozen?: boolean } = $props();

  // Client-side cap: keeps the transcript (and the payload re-sent on every
  // turn) bounded. Not a protocol limit — just a quiet trim once the
  // conversation runs long.
  const MAX_MESSAGES = 50;

  // Mirrors the server's per-message cap (CHAT_MESSAGE_TEXT_MAX_LEN in
  // kobo_api/main.py) so an over-length send is refused here instead of
  // round-tripping to a guaranteed 422. JS string length counts UTF-16 code
  // units, not the server's Python code points, so this is strictly
  // conservative — never lets through more than the server would accept.
  const MAX_MESSAGE_LEN = 4000;

  let transcript = $state<ChatMessage[]>([]);
  let input = $state("");
  let error = $state<string | null>(null);
  // Whether the current `error` is worth a "Try again" button. A 422 means
  // the payload itself was rejected — resending the same text can never
  // succeed, unlike a transient 502 — so that path turns this off.
  let retryable = $state(true);
  let trimmed = $state(false);
  let inputEl: HTMLTextAreaElement | undefined = $state();

  // `agentName` is read once here rather than tracked reactively: the caller
  // (AgentDetail, inside App.svelte's `{#key route.name}`) remounts this
  // component whenever the routed agent changes, so this component's
  // `agentName` prop is fixed for its entire lifetime — there's no live
  // update for a reactive reference to catch.
  // svelte-ignore state_referenced_locally
  const mutation = createMutation(() => (chatMutation(agentName)));

  const canSend = $derived(input.trim().length > 0 && !mutation.isPending);

  /** Drops the oldest messages once the transcript grows past the cap, and
   * flips on the "older messages were trimmed" notice the first time that
   * happens. */
  function capTranscript(messages: ChatMessage[]): ChatMessage[] {
    if (messages.length <= MAX_MESSAGES) {
      return messages;
    }
    trimmed = true;
    return messages.slice(messages.length - MAX_MESSAGES);
  }

  function focusInput(): void {
    inputEl?.focus();
  }

  // --- Teach this (Task 2): a quiet per-bubble action opens TeachModal -----
  // pre-filled with the clicked exchange PAIR. One modal instance is shared
  // across every bubble (not one per bubble) — `teachPrefill` is computed at
  // click time and handed to TeachModal, which reseeds its own draft from it
  // on every open (see TeachModal's own doc comment).

  let teachOpen = $state(false);
  let teachPrefill = $state("");

  /** The editable text TeachModal opens with for the bubble at `index`:
   * clicked user message + the agent reply right after it; clicked agent
   * reply + the user message right before it; a lone message (no adjacent
   * counterpart of the opposite role, e.g. a just-sent message still
   * awaiting its reply) just itself, unlabeled. */
  function pairText(index: number): string {
    const message = transcript[index];
    if (message.role === "user") {
      const next = transcript[index + 1];
      if (next && next.role === "agent") {
        return `You said: ${message.text}\n\n${agentName} replied: ${next.text}`;
      }
      return message.text;
    }
    const prev = transcript[index - 1];
    if (prev && prev.role === "user") {
      return `You said: ${prev.text}\n\n${agentName} replied: ${message.text}`;
    }
    return message.text;
  }

  function openTeach(index: number): void {
    teachPrefill = pairText(index);
    teachOpen = true;
  }

  function closeTeach(): void {
    teachOpen = false;
  }

  /** Sends `messages` (a full transcript snapshot) to the agent. Shared by
   * both the initial send and the retry affordance, so a retry re-plays
   * exactly what failed rather than re-deriving it. */
  function sendTranscript(messages: ChatMessage[]): void {
    error = null;
    mutation.mutate(messages, {
      onSuccess: (result) => {
        transcript = capTranscript([...transcript, { role: "agent", text: result.reply }]);
        focusInput();
      },
      onError: (err: unknown) => {
        if (err instanceof ApiError && err.status === 422) {
          // The payload itself was rejected (e.g. a message over the length
          // cap) — resending it unchanged would 422 forever. Hand the
          // poisoned bubble back to the user instead of wedging the
          // transcript: pull it out of the log and restore its text to the
          // input so it can be edited and resent.
          const last = messages[messages.length - 1];
          if (last.role === "user") {
            transcript = messages.slice(0, -1);
            input = last.text;
          }
          retryable = false;
        } else {
          retryable = true;
        }
        error = errorMessage(err, "Couldn't reach the agent. Try again.");
      },
    });
  }

  function handleSend(): void {
    const text = input.trim();
    if (text.length === 0 || mutation.isPending) {
      return;
    }
    input = "";
    const next = capTranscript([...transcript, { role: "user", text }]);
    transcript = next;
    sendTranscript(next);
  }

  function handleRetry(): void {
    if (mutation.isPending) {
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

<section class="chat-panel" aria-labelledby="chat-panel-heading">
  <h2 id="chat-panel-heading" class="chat-panel__heading">Chat</h2>

  <div class="chat-panel__log" role="log" aria-live="polite">
    {#if trimmed}
      <p class="chat-panel__notice">Older messages were trimmed to keep this short.</p>
    {/if}
    {#each transcript as message, i (i)}
      <div class="chat-bubble chat-bubble--{message.role}">
        <span class="chat-panel__sr-only"
          >{message.role === "user" ? "You" : agentName}:</span
        >
        <p class="chat-bubble__text">{message.text}</p>
        {#if !frozen}
          <button
            type="button"
            class="chat-bubble__teach"
            onclick={() => openTeach(i)}
          >
            Teach this
          </button>
        {/if}
      </div>
    {/each}
    {#if mutation.isPending}
      <div class="chat-bubble chat-bubble--agent chat-bubble--pending">
        <span class="chat-panel__sr-only">{agentName}:</span>
        <p class="chat-bubble__text">
          <span class="chat-panel__thinking-dot pulse" aria-hidden="true"></span>
          thinking…
        </p>
      </div>
    {/if}
  </div>

  {#if error}
    <div class="chat-panel__error" role="alert">
      <p class="chat-panel__error-text">{error}</p>
      {#if retryable}
        <button
          type="button"
          class="chat-panel__retry"
          disabled={frozen || mutation.isPending}
          onclick={handleRetry}
        >
          Try again
        </button>
      {/if}
    </div>
  {/if}

  <div class="chat-panel__form">
    <label class="chat-panel__label" for="chat-panel-input">Your message</label>
    <textarea
      id="chat-panel-input"
      class="chat-panel__input"
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
      class="chat-panel__send"
      disabled={frozen || !canSend}
      onclick={handleSend}
    >
      {mutation.isPending ? "Sending…" : "Send"}
    </button>
  </div>
</section>

<TeachModal {agentName} open={teachOpen} prefill={teachPrefill} onclose={closeTeach} />

<style>
  .chat-panel {
    display: flex;
    flex-direction: column;
    gap: var(--space-md);
    padding: var(--space-lg);
    border: 1px solid var(--border);
    border-radius: var(--radius);
    background: var(--panel);
  }

  .chat-panel__heading {
    margin: 0;
    color: var(--muted);
    font-size: 0.8rem;
    font-weight: 600;
    letter-spacing: 0.06em;
    text-transform: uppercase;
  }

  .chat-panel__sr-only {
    position: absolute;
    width: 1px;
    height: 1px;
    overflow: hidden;
    clip: rect(0, 0, 0, 0);
    white-space: nowrap;
  }

  .chat-panel__log {
    display: flex;
    flex-direction: column;
    gap: var(--space-sm);
  }

  .chat-panel__notice {
    margin: 0 0 var(--space-2xs);
    color: var(--muted);
    font-size: 0.8rem;
    text-align: center;
  }

  .chat-bubble {
    max-width: 80%;
    padding: var(--space-xs) var(--space-sm);
    border: 1px solid var(--border);
    border-radius: var(--radius-sm);
  }

  .chat-bubble--user {
    align-self: flex-end;
    border-color: var(--accent);
    background: var(--accent-soft);
  }

  .chat-bubble--agent {
    align-self: flex-start;
    background: var(--bg);
  }

  .chat-bubble__text {
    margin: 0;
    color: var(--ink);
    font-size: 0.95rem;
    line-height: 1.6;
    white-space: pre-wrap;
  }

  /* Quiet by design: no border, no background, small and muted — the
   * bubbles stay clean, this never competes with the message itself. */
  .chat-bubble__teach {
    display: block;
    margin-top: var(--space-2xs);
    padding: 0;
    border: none;
    background: none;
    color: var(--muted);
    font-size: 0.75rem;
    cursor: pointer;
  }

  .chat-bubble__teach:hover {
    color: var(--accent);
    text-decoration: underline;
  }

  .chat-bubble__teach:focus-visible {
    outline: 2px solid var(--accent);
    outline-offset: 1px;
  }

  .chat-panel__thinking-dot {
    display: inline-block;
    width: 0.5rem;
    height: 0.5rem;
    margin-right: var(--space-2xs);
    border-radius: 50%;
    background: var(--pulse);
  }

  .chat-panel__error {
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

  .chat-panel__error-text {
    margin: 0;
    color: var(--fail);
    font-size: 0.85rem;
  }

  .chat-panel__retry {
    padding: var(--space-2xs) var(--space-sm);
    border: 1px solid var(--fail);
    border-radius: var(--radius-sm);
    background: var(--panel);
    color: var(--fail);
    font-weight: 600;
    cursor: pointer;
    white-space: nowrap;
  }

  .chat-panel__retry:hover:not(:disabled) {
    filter: brightness(1.05);
  }

  .chat-panel__retry:disabled {
    opacity: 0.6;
    cursor: not-allowed;
  }

  .chat-panel__form {
    display: flex;
    flex-direction: column;
    gap: var(--space-2xs);
  }

  .chat-panel__label {
    color: var(--ink);
    font-weight: 600;
    font-size: 0.85rem;
  }

  .chat-panel__input {
    padding: var(--space-xs) var(--space-sm);
    border: 1px solid var(--border);
    border-radius: var(--radius-sm);
    background: var(--panel);
    color: var(--ink);
    font: inherit;
    resize: vertical;
  }

  .chat-panel__input:focus-visible {
    outline: 2px solid var(--accent);
    outline-offset: 1px;
  }

  .chat-panel__send {
    align-self: flex-end;
    padding: var(--space-xs) var(--space-md);
    border: none;
    border-radius: var(--radius-sm);
    background: var(--accent);
    color: var(--accent-ink);
    font-weight: 600;
    cursor: pointer;
  }

  .chat-panel__send:hover:not(:disabled) {
    filter: brightness(1.05);
  }

  .chat-panel__send:disabled {
    opacity: 0.5;
    cursor: not-allowed;
  }
</style>
