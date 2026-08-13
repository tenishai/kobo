<script lang="ts">
  // One-shot uploads for an agent to learn from once (`bundle/intake/*.md`
  // or `*.txt` in its repo) — a runtime seeder ingests these into the
  // agent's actual memory exactly once, at boot (a later task); this panel
  // is only the upload/list/remove plumbing, plus showing each file's
  // pending/ingested status (derived fresh from git ancestry by the server
  // on every list call — see `kobo_api.intake.list_intake`'s docstring).
  // Same rebuild-the-user-never-names convention as MemoryPanel/SkillsPanel:
  // copy speaks of learning/uploading/removing, never "commit"/"deploy"/
  // "repo"/"sha"/"pipeline".
  //
  // Unlike MemoryPanel/SkillsPanel, there's no edit affordance here — an
  // intake file is either not-yet-uploaded or already sitting in the repo
  // waiting on the seeder; "editing" it would just be a second upload under
  // the same name. So the upload form is a plain inline form at the top of
  // the panel (closer to SlackPanel's inline steps than MemoryPanel's
  // modal-driven add), and the shared `Modal` is used for exactly one
  // thing: confirming a remove.
  //
  // Delivery-chip reactivity: copied verbatim from SkillsPanel/MemoryPanel
  // (see MemoryPanel's doc comment for why `deliverySha` is a plain
  // `writable`/`derived` pair rather than `$state`/`$derived`). Only the
  // upload mutation feeds it — upload is the one intake action whose
  // response carries a `sha` this room can track; delete's response instead
  // carries a `note`, rendered as its own plain-language success flash
  // rather than routed through the delivery chip.
  import { createMutation, createQuery, useQueryClient } from "@tanstack/svelte-query";
  import { derived, writable } from "svelte/store";
  import EmptyState from "./EmptyState.svelte";
  import ErrorState from "./ErrorState.svelte";
  import Modal from "./Modal.svelte";
  import Skeleton from "./Skeleton.svelte";
  import { errorMessage, type IntakeFile } from "../lib/api";
  import { deleteIntakeMutation, deliveryQuery, intakeQuery, uploadIntakeMutation } from "../lib/queries";

  let { agentName }: { agentName: string } = $props();

  const queryClient = useQueryClient();
  // `agentName` is read once here rather than tracked reactively: same
  // reasoning as MemoryPanel/SkillsPanel — AgentDetail (the only caller)
  // remounts this component via `{#key route.name}` whenever the routed
  // agent changes, so this prop is fixed for the component's entire
  // lifetime.
  // svelte-ignore state_referenced_locally
  const intake = createQuery(intakeQuery(agentName));
  // svelte-ignore state_referenced_locally
  const uploadMutation = createMutation(uploadIntakeMutation(queryClient, agentName));
  // svelte-ignore state_referenced_locally
  const deleteMutation = createMutation(deleteIntakeMutation(queryClient, agentName));

  // --- Delivery chip (copied pattern from SkillsPanel/MemoryPanel; see
  // MemoryPanel's doc comment and the module comment above for the full
  // rationale) -------------------------------------------------------------

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
          "Something went wrong. Try uploading again.",
        ),
      };
    }
    // "building" | "rolling", or any state this code doesn't recognize yet —
    // treated the same as still-in-progress, never claiming success or
    // failure it can't back up.
    return { tone: "pending", text: "Delivering changes…" };
  });

  // --- Upload form ----------------------------------------------------------

  // Matches the server's `INTAKE_MAX_BYTES` (`kobo_api.intake`) exactly —
  // this is a client-side guard so an oversized paste is caught before the
  // round trip, not a separate rule of its own.
  const INTAKE_MAX_BYTES = 262_144;

  // Same shape as `kobo_api.intake.INTAKE_FILENAME_RE`: a lowercase letter,
  // then lowercase/digit/hyphen (40 chars max), ending in .md or .txt.
  const INTAKE_FILENAME_RE = /^[a-z][a-z0-9-]{0,39}\.(md|txt)$/;

  let filename = $state("");
  let content = $state("");
  let uploadError = $state<string | null>(null);

  const contentBytes = $derived(new TextEncoder().encode(content).length);
  const contentTooLarge = $derived(contentBytes > INTAKE_MAX_BYTES);

  const filenameError = $derived.by((): string | null => {
    if (filename.length === 0) {
      return null; // nothing to say about an empty field yet
    }
    if (!INTAKE_FILENAME_RE.test(filename)) {
      return "Use lowercase letters, numbers, and hyphens, ending in .md or .txt.";
    }
    return null;
  });

  const filenameMatch = $derived.by((): IntakeFile | null => {
    if (filename.length === 0 || !INTAKE_FILENAME_RE.test(filename)) {
      return null;
    }
    const match = $intake.data?.find((file) => file.name === filename);
    return match ?? null;
  });

  const canUpload = $derived.by((): boolean => {
    if ($uploadMutation.isPending) {
      return false;
    }
    if (filename.length === 0 || filenameError !== null) {
      return false;
    }
    if (content.trim().length === 0 || contentTooLarge) {
      return false;
    }
    return true;
  });

  /** Human-readable size for a row's `content_bytes` — plain "1.2 KB"/"48 B"
   * style, no jargon, matching the plain tone of the rest of this panel's
   * copy. */
  function humanSize(bytes: number): string {
    if (bytes < 1024) {
      return `${bytes} B`;
    }
    const kb = bytes / 1024;
    if (kb < 1024) {
      return `${kb.toFixed(kb < 10 ? 1 : 0)} KB`;
    }
    return `${(kb / 1024).toFixed(1)} MB`;
  }

  function handleUpload(event: SubmitEvent): void {
    event.preventDefault();
    if (!canUpload) {
      return;
    }
    uploadError = null;
    $uploadMutation.mutate(
      { filename, content },
      {
        onSuccess: (result) => {
          beginDelivery(result.sha);
          filename = "";
          content = "";
        },
        onError: (err: unknown) => {
          uploadError = errorMessage(err, "Couldn't upload this file. Try again.");
        },
      },
    );
  }

  // --- Remove: confirm modal + success flash --------------------------------

  let removeTarget = $state<IntakeFile | null>(null);
  let removeError = $state<string | null>(null);
  let removeNote = $state<string | null>(null);
  let removeNoteTimer: ReturnType<typeof setTimeout> | undefined;

  function openRemove(file: IntakeFile): void {
    if (file.status !== "ingested") {
      return; // Remove is only ever enabled once a file is ingested
    }
    removeTarget = file;
    removeError = null;
  }

  function closeRemove(): void {
    removeTarget = null;
    removeError = null;
  }

  function handleRemove(): void {
    if (removeTarget === null) {
      return;
    }
    const targetName = removeTarget.name;
    removeError = null;
    $deleteMutation.mutate(targetName, {
      onSuccess: (result) => {
        if (removeNoteTimer !== undefined) {
          clearTimeout(removeNoteTimer);
        }
        removeNote = result.note;
        removeNoteTimer = setTimeout(() => {
          removeNote = null;
          removeNoteTimer = undefined;
        }, DELIVERED_CLEAR_MS);
        closeRemove();
      },
      onError: (err: unknown) => {
        removeError = errorMessage(err, "Couldn't remove it. Try again.");
      },
    });
  }
</script>

<section class="intake-panel" aria-labelledby="intake-panel-heading">
  <div class="intake-panel__header">
    <h2 id="intake-panel-heading" class="intake-panel__heading">Intake</h2>
    <p class="intake-panel__subtitle">
      Drop in notes or docs for your agent to learn once — the file can go away afterward, the
      learning stays.
    </p>
  </div>

  <form class="intake-form" onsubmit={handleUpload}>
    <div class="intake-form__field">
      <label class="intake-form__label" for="intake-filename">File name</label>
      <input
        id="intake-filename"
        class="intake-form__input"
        type="text"
        bind:value={filename}
        autocomplete="off"
        spellcheck="false"
        aria-describedby="intake-filename-status"
        aria-invalid={filenameError !== null}
      />
      <p class="intake-form__help">lowercase-with-dashes, .md or .txt</p>
      <div id="intake-filename-status">
        {#if filenameError}
          <p class="intake-form__status intake-form__status--error" role="alert">
            {filenameError}
          </p>
        {:else if filenameMatch}
          <p class="intake-form__help">
            {filenameMatch.status === "ingested"
              ? "Re-uploading under this name teaches your agent again from the new content — what it already learned stays."
              : "This will replace the waiting file before it's learned."}
          </p>
        {/if}
      </div>
    </div>

    <div class="intake-form__field">
      <label class="intake-form__label" for="intake-content">Content</label>
      <textarea
        id="intake-content"
        class="intake-form__textarea"
        bind:value={content}
        rows="8"
        aria-describedby="intake-content-status"
      ></textarea>
      <div id="intake-content-status">
        {#if contentTooLarge}
          <p class="intake-form__status intake-form__status--error" role="alert">
            That's too much text — keep it under 256KB (262,144 bytes).
          </p>
        {:else if content.trim().length === 0}
          <p class="intake-form__help">Add a bit of text before uploading.</p>
        {/if}
      </div>
    </div>

    {#if uploadError}
      <p class="intake-form__status intake-form__status--error" role="alert">{uploadError}</p>
    {/if}

    <button type="submit" class="intake-form__submit" disabled={!canUpload}>
      {$uploadMutation.isPending ? "Uploading…" : "Upload"}
    </button>
  </form>

  {#if $intake.isPending}
    <Skeleton rows={3} />
  {:else if $intake.isError}
    <ErrorState
      message={errorMessage($intake.error, "Couldn't load this agent's files.")}
      onretry={() => $intake.refetch()}
    />
  {:else if $intake.data.length === 0}
    <EmptyState message="Nothing here yet — upload a file for your agent to learn from." />
  {:else}
    <ul class="intake-panel__list">
      {#each $intake.data as file (file.name)}
        <li class="intake-row">
          <span class="intake-row__body">
            <span class="intake-row__name">{file.name}</span>
            <span class="intake-row__meta">{humanSize(file.content_bytes)}</span>
          </span>
          <span class="intake-row__status intake-row__status--{file.status}">
            {file.status === "ingested" ? "Ingested" : "Pending"}
          </span>
          <button
            type="button"
            class="intake-row__remove"
            disabled={file.status !== "ingested"}
            title={file.status !== "ingested"
              ? "Wait for this file to be ingested before removing it."
              : undefined}
            aria-label={`Remove ${file.name}`}
            onclick={() => openRemove(file)}
          >
            Remove
          </button>
        </li>
      {/each}
    </ul>
  {/if}

  {#if removeNote}
    <p class="intake-panel__note" role="status">{removeNote}</p>
  {/if}

  {#if $deliverySha !== null}
    <div
      class="intake-panel__delivery intake-panel__delivery--{deliveryView.tone}"
      role="status"
      aria-live="polite"
    >
      {#if deliveryView.tone === "pending"}
        <span class="intake-panel__delivery-dot pulse" aria-hidden="true"></span>
      {/if}
      <span>{deliveryView.text}</span>
    </div>
  {/if}
</section>

<Modal
  open={removeTarget !== null}
  title={removeTarget ? `Remove ${removeTarget.name}` : undefined}
  onclose={closeRemove}
>
  {#if removeTarget}
    <p class="intake-modal__confirm-text">
      The file will be removed. Everything the agent learned from it stays.
    </p>
    {#if removeError}
      <p class="intake-modal__error" role="alert">{removeError}</p>
    {/if}
    <div class="intake-modal__actions">
      <button type="button" class="intake-modal__cancel" onclick={closeRemove}>Cancel</button>
      <button
        type="button"
        class="intake-modal__danger"
        disabled={$deleteMutation.isPending}
        onclick={handleRemove}
      >
        {$deleteMutation.isPending ? "Removing…" : "Remove"}
      </button>
    </div>
  {/if}
</Modal>

<style>
  .intake-panel {
    display: flex;
    flex-direction: column;
    gap: var(--space-md);
    padding: var(--space-lg);
    border: 1px solid var(--border);
    border-radius: var(--radius);
    background: var(--panel);
  }

  .intake-panel__header {
    display: flex;
    flex-direction: column;
    gap: var(--space-2xs);
  }

  .intake-panel__heading {
    margin: 0;
    color: var(--muted);
    font-size: 0.8rem;
    font-weight: 600;
    letter-spacing: 0.06em;
    text-transform: uppercase;
  }

  .intake-panel__subtitle {
    margin: 0;
    color: var(--muted);
    font-size: 0.85rem;
  }

  .intake-form {
    display: flex;
    flex-direction: column;
    gap: var(--space-md);
    padding: var(--space-md);
    border: 1px solid var(--border);
    border-radius: var(--radius-sm);
    background: var(--bg);
  }

  .intake-form__field {
    display: flex;
    flex-direction: column;
    gap: var(--space-2xs);
  }

  .intake-form__label {
    color: var(--ink);
    font-weight: 600;
    font-size: 0.9rem;
  }

  .intake-form__input,
  .intake-form__textarea {
    padding: var(--space-xs) var(--space-sm);
    border: 1px solid var(--border);
    border-radius: var(--radius-sm);
    background: var(--panel);
    color: var(--ink);
    font: inherit;
  }

  .intake-form__textarea {
    font-family: var(--font-mono);
    font-size: 0.85rem;
    resize: vertical;
  }

  .intake-form__input:focus-visible,
  .intake-form__textarea:focus-visible {
    outline: 2px solid var(--accent);
    outline-offset: 1px;
  }

  .intake-form__help {
    margin: 0;
    color: var(--muted);
    font-size: 0.8rem;
  }

  .intake-form__status {
    margin: 0;
    font-size: 0.8rem;
  }

  .intake-form__status--error {
    color: var(--fail);
  }

  .intake-form__submit {
    align-self: flex-start;
    padding: var(--space-xs) var(--space-md);
    border: none;
    border-radius: var(--radius-sm);
    background: var(--accent);
    color: var(--accent-ink);
    font-weight: 600;
    cursor: pointer;
  }

  .intake-form__submit:hover:not(:disabled) {
    filter: brightness(1.05);
  }

  .intake-form__submit:disabled {
    opacity: 0.5;
    cursor: not-allowed;
  }

  .intake-panel__list {
    display: flex;
    flex-direction: column;
    gap: var(--space-xs);
    margin: 0;
    padding: 0;
    list-style: none;
  }

  .intake-row {
    display: flex;
    align-items: center;
    gap: var(--space-xs);
    padding: var(--space-sm);
    border: 1px solid var(--border);
    border-radius: var(--radius-sm);
    background: var(--bg);
  }

  .intake-row__body {
    display: flex;
    flex: 1;
    flex-direction: column;
    gap: 0.15rem;
    min-width: 0;
  }

  .intake-row__name {
    overflow-wrap: anywhere;
    color: var(--ink);
    font-family: var(--font-mono);
    font-size: 0.9rem;
    font-weight: 600;
  }

  .intake-row__meta {
    color: var(--muted);
    font-size: 0.8rem;
  }

  .intake-row__status {
    display: inline-flex;
    flex-shrink: 0;
    padding: 0.15rem var(--space-xs);
    border: 1px solid var(--border);
    border-radius: 999px;
    font-size: 0.75rem;
    font-weight: 600;
    white-space: nowrap;
  }

  .intake-row__status--pending {
    border-color: var(--warn);
    color: var(--warn);
    background: color-mix(in srgb, var(--warn) 12%, transparent);
  }

  .intake-row__status--ingested {
    border-color: var(--ok);
    color: var(--ok);
    background: color-mix(in srgb, var(--ok) 12%, transparent);
  }

  .intake-row__remove {
    flex-shrink: 0;
    padding: var(--space-2xs) var(--space-sm);
    border: 1px solid var(--border);
    border-radius: var(--radius-sm);
    background: var(--panel);
    color: var(--muted);
    cursor: pointer;
    white-space: nowrap;
  }

  .intake-row__remove:hover:not(:disabled) {
    border-color: var(--fail);
    color: var(--fail);
  }

  .intake-row__remove:disabled {
    opacity: 0.5;
    cursor: not-allowed;
  }

  .intake-panel__note {
    margin: 0;
    color: var(--muted);
    font-size: 0.85rem;
  }

  .intake-panel__delivery {
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

  .intake-panel__delivery--ok {
    border-color: var(--ok);
    color: var(--ok);
  }

  .intake-panel__delivery--fail {
    border-color: var(--fail);
    color: var(--fail);
  }

  .intake-panel__delivery-dot {
    width: 0.5rem;
    height: 0.5rem;
    border-radius: 50%;
    background: var(--pulse);
  }

  .intake-modal__confirm-text {
    margin: 0;
    color: var(--ink);
  }

  .intake-modal__error {
    margin: 0;
    color: var(--fail);
    font-size: 0.85rem;
  }

  .intake-modal__actions {
    display: flex;
    justify-content: flex-end;
    gap: var(--space-sm);
  }

  .intake-modal__cancel {
    padding: var(--space-xs) var(--space-md);
    border: 1px solid var(--border);
    border-radius: var(--radius-sm);
    background: var(--panel);
    color: var(--ink);
    cursor: pointer;
  }

  .intake-modal__cancel:hover:not(:disabled) {
    border-color: var(--accent);
    color: var(--accent);
  }

  .intake-modal__cancel:disabled {
    opacity: 0.6;
    cursor: not-allowed;
  }

  .intake-modal__danger {
    padding: var(--space-xs) var(--space-md);
    border: none;
    border-radius: var(--radius-sm);
    background: var(--fail);
    color: var(--accent-ink);
    font-weight: 600;
    cursor: pointer;
  }

  .intake-modal__danger:hover:not(:disabled) {
    filter: brightness(1.05);
  }

  .intake-modal__danger:disabled {
    opacity: 0.6;
    cursor: not-allowed;
  }
</style>
