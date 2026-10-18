<script lang="ts">
  // Task 2's "Teach this" gesture: opened per-bubble by ChatPanel, pre-filled
  // with the clicked exchange pair (see ChatPanel's own `pairText`), editable
  // before it's posted to Task 1's teach route
  // (`POST /api/agents/{name}/teach`). Fully controlled by the caller
  // (`open`/`prefill`/`onclose`), same contract `Modal` itself uses — this
  // component owns only the draft textarea, the mutation, and the two
  // outcome views (success/error), never the "should it be open" decision.
  //
  // The draft is seeded from `prefill` on every FALSE -> TRUE transition of
  // `open`, tracked via the plain `wasOpen` variable (not `$state`) — the
  // same "read once at the moment that matters, not continuously" idiom
  // `Modal.svelte`'s own `opener` variable already uses. That means
  // reopening on a different bubble (or the very same one) always starts
  // from a clean slate: any unsaved edit, error, or a lingering "Saved to
  // memory" success view from a prior open is discarded, per the brief's
  // "modal resets fully between opens" — reading `prefill` only inside the
  // guarded branch also means a `prefill` change while already open (never
  // actually done by ChatPanel today, which sets both together) can't clobber
  // an in-progress edit.
  import { createMutation, useQueryClient } from "@tanstack/svelte-query";
  import Modal from "./Modal.svelte";
  import { errorMessage } from "../lib/api";
  import { teachMutation } from "../lib/queries";

  let {
    agentName,
    open,
    prefill,
    onclose,
  }: {
    agentName: string;
    open: boolean;
    prefill: string;
    onclose: () => void;
  } = $props();

  const queryClient = useQueryClient();
  // agentName is fixed for this component's lifetime (ChatPanel's own
  // agentName prop is fixed for its lifetime too, see ChatPanel's own doc
  // comment on why), same reasoning every other panel's mutation setup
  // documents.
  // svelte-ignore state_referenced_locally
  const mutation = createMutation(() => (teachMutation(queryClient, agentName)));

  let text = $state("");
  let error = $state<string | null>(null);
  let saved = $state(false);
  let mode = $state<"instant" | "archive">("archive");
  // The note's filename from the response, shown with the success copy so
  // "did anything happen?" has an answer (kobo#123 item 12).
  let savedAs = $state("");

  let wasOpen = false;

  $effect(() => {
    if (open && !wasOpen) {
      text = prefill;
      error = null;
      saved = false;
      mode = "archive";
      savedAs = "";
    }
    wasOpen = open;
  });

  const canSave = $derived(text.trim().length > 0 && !mutation.isPending);

  function handleSave(): void {
    if (!canSave) {
      return;
    }
    error = null;
    mutation.mutate(text, {
      onSuccess: (data) => {
        mode = data?.mode === "instant" ? "instant" : "archive";
        savedAs = typeof data?.filename === "string" ? data.filename : "";
        saved = true;
      },
      onError: (err: unknown) => {
        error = errorMessage(err, "Could not save. Try again.");
      },
    });
  }

  function handleClose(): void {
    onclose();
  }
</script>

<Modal {open} title="Teach this" onclose={handleClose}>
  {#if saved}
    <div class="teach-modal__success">
      <p class="teach-modal__success-text">
        {mode === "instant"
          ? "Learned. Filed to the repo in the background."
          : "Saved to memory. It arrives with the next delivery."}
      </p>
      {#if savedAs}
        <p class="teach-modal__success-file">Saved as intake/{savedAs}</p>
      {/if}
      <button type="button" class="teach-modal__done" onclick={handleClose}>
        Done
      </button>
    </div>
  {:else}
    <form
      class="teach-modal__form"
      onsubmit={(event) => {
        event.preventDefault();
        handleSave();
      }}
    >
      <label class="teach-modal__label" for="teach-modal-text">Teach {agentName}</label>
      <textarea
        id="teach-modal-text"
        class="teach-modal__textarea"
        bind:value={text}
        rows="8"
      ></textarea>
      {#if error}
        <p class="teach-modal__error" role="alert">{error}</p>
      {/if}
      <div class="teach-modal__actions">
        <button type="button" class="teach-modal__cancel" onclick={handleClose}>
          Cancel
        </button>
        <button type="submit" class="teach-modal__save" disabled={!canSave}>
          {mutation.isPending ? "Saving…" : "Save"}
        </button>
      </div>
    </form>
  {/if}
</Modal>

<style>
  .teach-modal__form {
    display: flex;
    flex-direction: column;
    gap: var(--space-md);
  }

  .teach-modal__label {
    color: var(--ink);
    font-weight: 600;
    font-size: 0.9rem;
  }

  .teach-modal__textarea {
    padding: var(--space-xs) var(--space-sm);
    border: 1px solid var(--border);
    border-radius: var(--radius-sm);
    background: var(--panel);
    color: var(--ink);
    font: inherit;
    resize: vertical;
  }

  .teach-modal__textarea:focus-visible {
    outline: 2px solid var(--accent);
    outline-offset: 1px;
  }

  .teach-modal__error {
    margin: 0;
    color: var(--fail);
    font-size: 0.85rem;
  }

  .teach-modal__actions {
    display: flex;
    justify-content: flex-end;
    gap: var(--space-sm);
  }

  .teach-modal__cancel {
    padding: var(--space-xs) var(--space-md);
    border: 1px solid var(--border);
    border-radius: var(--radius-sm);
    background: var(--panel);
    color: var(--ink);
    cursor: pointer;
  }

  .teach-modal__cancel:hover {
    border-color: var(--accent);
    color: var(--accent);
  }

  .teach-modal__save {
    padding: var(--space-xs) var(--space-md);
    border: none;
    border-radius: var(--radius-sm);
    background: var(--accent);
    color: var(--accent-ink);
    font-weight: 600;
    cursor: pointer;
  }

  .teach-modal__save:hover:not(:disabled) {
    filter: brightness(1.05);
  }

  .teach-modal__save:disabled {
    opacity: 0.5;
    cursor: not-allowed;
  }

  .teach-modal__success {
    display: flex;
    flex-direction: column;
    align-items: flex-start;
    gap: var(--space-md);
  }

  .teach-modal__success-text {
    margin: 0;
    color: var(--ink);
  }

  .teach-modal__success-file {
    margin: 0;
    color: var(--muted);
    font-size: 0.85rem;
  }

  .teach-modal__done {
    padding: var(--space-xs) var(--space-md);
    border: none;
    border-radius: var(--radius-sm);
    background: var(--accent);
    color: var(--accent-ink);
    font-weight: 600;
    cursor: pointer;
  }

  .teach-modal__done:hover {
    filter: brightness(1.05);
  }
</style>
