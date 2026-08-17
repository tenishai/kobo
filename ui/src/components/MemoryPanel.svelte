<script lang="ts">
  // What a deployed agent knows from the start (`bundle/knowledge/*.md` in
  // its repo, P2b), plus — in the "Teach from a file" subsection below —
  // one-shot uploads for the agent to learn from once (`bundle/intake/*.md`
  // or `*.txt` in its repo): a runtime seeder ingests these into the
  // agent's actual memory exactly once, at boot (a later task); this
  // subsection is only the upload/list/remove plumbing, plus showing each
  // file's pending/ingested status (derived fresh from git ancestry by the
  // server on every list call — see `kobo_api.intake.list_intake`'s
  // docstring). Both halves are always mounted inside their own tabpanel
  // (AgentDetail) regardless of the agent's status — the server gates the
  // actual mutations until `status === "ready"` (a 409 otherwise), and
  // AgentDetail disables the Memory tab TRIGGER for every other non-frozen
  // status so that gate is never reached by clicking in from here; the
  // `frozen` prop (destroying/destroy_failed) disables this panel's own
  // controls instead, since destruction leaves the tab itself clickable.
  // Every save/delete/upload here is delivered through a rebuild the user
  // never sees or names: copy speaks only of saving/teaching changes and
  // the agent picking them up, never
  // "commit"/"deploy"/"build"/"repo"/"sha"/"pipeline".
  //
  // One shared `Modal` instance drives edit, add, and delete for knowledge
  // files — which of the three (and which file, for edit/delete) lives in
  // `target`; the textarea/filename fields are separate top-level state so
  // "Back to editing" can return to them without losing what was typed, and
  // so the add-mode name field survives the review round trip.
  //
  // Unlike the knowledge list, there's no edit affordance for an intake
  // file — it's either not-yet-uploaded or already sitting in the repo
  // waiting on the seeder, so "editing" it would just be a second upload
  // under the same name. So the upload form is a plain inline form (closer
  // to SlackPanel's inline steps than the knowledge modal), and the intake
  // subsection gets its own, second `Modal` instance for exactly one thing:
  // confirming a remove.
  //
  // Delivery-chip reactivity: `deliveryQuery(name, sha)` needs a *new* query
  // (not just new data on the same one) every time `sha` changes, so a save
  // that lands while an earlier one is still "delivering" doesn't keep
  // polling the stale target. The installed @tanstack/svelte-query adapter
  // (5.90.2, see node_modules/@tanstack/svelte-query/dist/createBaseQuery.js)
  // only re-derives a query's options from an actual Svelte store — it
  // checks `isSvelteStore(options)` and otherwise freezes whatever was
  // passed at mount time, so a `$derived`-computed options object (a plain
  // value, not a store) would never refresh after the first sha. A classic
  // `writable`/`derived` pair is the version-correct equivalent of "local
  // reactive state driving the query" here: `deliverySha` is still owned by
  // this component and still read with the same `$name` syntax as a rune
  // would be in the markup below, it just comes from svelte/store instead
  // of `$state` so `createQuery` actually notices when it changes.
  //
  // There's exactly one chip in this panel, shared by every mutation below
  // — knowledge save, knowledge delete, and intake upload alike, the same
  // pattern SkillsPanel uses across its own three mutations: `deliveryQuery`
  // is sha-generic, so whichever flow's mutation resolves most recently just
  // calls `beginDelivery(result.sha)` and the chip tracks that sha exactly
  // as if this panel only ever did one kind of thing.
  import { createMutation, createQuery, useQueryClient } from "@tanstack/svelte-query";
  import { derived, writable } from "svelte/store";
  import EmptyState from "./EmptyState.svelte";
  import ErrorState from "./ErrorState.svelte";
  import Modal from "./Modal.svelte";
  import Skeleton from "./Skeleton.svelte";
  import { errorMessage, type IntakeFile, type KnowledgeFile } from "../lib/api";
  import { lineDiff, type DiffLine } from "../lib/diff";
  import {
    deleteIntakeMutation,
    deleteKnowledgeMutation,
    deliveryQuery,
    intakeQuery,
    knowledgeQuery,
    saveKnowledgeMutation,
    uploadIntakeMutation,
  } from "../lib/queries";

  let { agentName, frozen = false }: { agentName: string; frozen?: boolean } = $props();

  const queryClient = useQueryClient();
  // `agentName` is read once here rather than tracked reactively: same
  // reasoning as ChatPanel's `chatMutation(agentName)` — AgentDetail (the
  // only caller) remounts this component via `{#key route.name}` whenever
  // the routed agent changes, so this prop is fixed for the component's
  // entire lifetime.
  // svelte-ignore state_referenced_locally
  const knowledge = createQuery(knowledgeQuery(agentName));
  // svelte-ignore state_referenced_locally
  const saveMutation = createMutation(saveKnowledgeMutation(queryClient, agentName));
  // svelte-ignore state_referenced_locally
  const deleteMutation = createMutation(deleteKnowledgeMutation(queryClient, agentName));
  // svelte-ignore state_referenced_locally
  const intake = createQuery(intakeQuery(agentName));
  // svelte-ignore state_referenced_locally
  const uploadMutation = createMutation(uploadIntakeMutation(queryClient, agentName));
  // svelte-ignore state_referenced_locally
  const intakeDeleteMutation = createMutation(deleteIntakeMutation(queryClient, agentName));

  // --- Delivery chip (shared by knowledge save/delete and intake upload) --

  // How long the "Changes are in place" chip stays up before it clears on
  // its own — see the module doc comment above for why this is a plain
  // store rather than `$state`.
  const DELIVERED_CLEAR_MS = 5000;

  const deliverySha = writable<string | null>(null);
  const deliveryOptions = derived(deliverySha, ($sha) => deliveryQuery(agentName, $sha));
  const delivery = createQuery(deliveryOptions);

  // Handle of the pending "clear the chip" timeout, if any. Not reactive
  // state on purpose — it's bookkeeping for `beginDelivery`/the effect
  // below, not something the template renders.
  let clearTimer: ReturnType<typeof setTimeout> | undefined;

  /** Start tracking a new save/delete/upload's delivery, replacing whatever
   * was being tracked before. Clears any pending "fade the chip" timer first
   * so a change that lands while an earlier one's chip is winding down
   * can't have its own fresh chip yanked away by a timer that belonged to
   * the old sha. */
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
    // failure it can't back up (mirrors status.ts's agentTone/stepTone
    // fallback convention).
    return { tone: "pending", text: "Delivering changes…" };
  });

  // --- Modal: edit / add / delete (knowledge files) -----------------------

  type Target =
    | { kind: "edit"; file: KnowledgeFile }
    | { kind: "add" }
    | { kind: "delete"; file: KnowledgeFile };

  let target = $state<Target | null>(null);
  let content = $state("");
  let filename = $state("");
  let reviewing = $state(false);
  let modalError = $state<string | null>(null);

  // Above this many (before-lines × after-lines) cells, the LCS table in
  // diff.ts's O(n·m) algorithm gets expensive enough that it's not worth
  // paying for a preview the user is about to skim past anyway.
  const DIFF_LINE_PRODUCT_LIMIT = 1_000_000;

  /** Line count under diff.ts's own convention: an empty string is zero
   * lines, not one (see diff.ts's `splitLines`). This only estimates the
   * cost of a diff for the guard below — it doesn't compute one. */
  function lineCount(text: string): number {
    return text === "" ? 0 : text.split("\n").length;
  }

  function displayName(fullName: string): string {
    return fullName.endsWith(".md") ? fullName.slice(0, -3) : fullName;
  }

  function firstLine(fileContent: string): string {
    const newlineIndex = fileContent.indexOf("\n");
    const line = newlineIndex === -1 ? fileContent : fileContent.slice(0, newlineIndex);
    return line.length > 0 ? line : "No preview yet.";
  }

  function diffMarker(kind: DiffLine["kind"]): string {
    switch (kind) {
      case "added":
        return "+ ";
      case "removed":
        return "− ";
      default:
        return "  ";
    }
  }

  function openEdit(file: KnowledgeFile): void {
    target = { kind: "edit", file };
    content = file.content;
    filename = "";
    reviewing = false;
    modalError = null;
  }

  function openAdd(): void {
    target = { kind: "add" };
    content = "";
    filename = "";
    reviewing = false;
    modalError = null;
  }

  function openDelete(file: KnowledgeFile): void {
    target = { kind: "delete", file };
    modalError = null;
  }

  function closeModal(): void {
    target = null;
    content = "";
    filename = "";
    reviewing = false;
    modalError = null;
  }

  function backToEditing(): void {
    reviewing = false;
    modalError = null;
  }

  const originalContent = $derived(target?.kind === "edit" ? target.file.content : "");
  const contentUnchanged = $derived(content === originalContent);

  const filenameError = $derived.by((): string | null => {
    if (target?.kind !== "add") {
      return null;
    }
    if (filename.length === 0) {
      return null; // nothing to say about an empty field yet
    }
    if (filename.length > 40) {
      return "Use 1-40 characters.";
    }
    if (!/^[a-z0-9-]+$/.test(filename)) {
      return "Use lowercase letters, numbers, and hyphens only.";
    }
    if (!/^[a-z]/.test(filename)) {
      return "Must start with a letter.";
    }
    if ($knowledge.data?.some((file) => file.name === `${filename}.md`)) {
      return "There's already a memory file with this name.";
    }
    return null;
  });

  const canReview = $derived.by((): boolean => {
    if (target === null || target.kind === "delete") {
      return false;
    }
    if (content.trim().length === 0 || contentUnchanged) {
      return false;
    }
    if (target.kind === "add" && (filename.length === 0 || filenameError !== null)) {
      return false;
    }
    return true;
  });

  const diffTooLarge = $derived(
    lineCount(originalContent) * lineCount(content) > DIFF_LINE_PRODUCT_LIMIT,
  );

  const diffLines = $derived.by((): DiffLine[] => {
    if (!reviewing || diffTooLarge) {
      return [];
    }
    return lineDiff(originalContent, content);
  });

  const modalTitle = $derived.by((): string | undefined => {
    if (target === null) {
      return undefined;
    }
    if (target.kind === "add") {
      return "Add memory";
    }
    if (target.kind === "delete") {
      return `Remove ${displayName(target.file.name)}`;
    }
    return `Edit ${displayName(target.file.name)}`;
  });

  function handleSave(): void {
    if (target === null || target.kind === "delete") {
      return;
    }
    const targetFilename = target.kind === "edit" ? target.file.name : `${filename}.md`;
    modalError = null;
    $saveMutation.mutate(
      { filename: targetFilename, content },
      {
        onSuccess: (result) => {
          beginDelivery(result.sha);
          closeModal();
        },
        onError: (err: unknown) => {
          modalError = errorMessage(err, "Couldn't save. Try again.");
        },
      },
    );
  }

  function handleDelete(): void {
    if (target?.kind !== "delete") {
      return;
    }
    const targetFilename = target.file.name;
    modalError = null;
    $deleteMutation.mutate(targetFilename, {
      onSuccess: (result) => {
        beginDelivery(result.sha);
        closeModal();
      },
      onError: (err: unknown) => {
        modalError = errorMessage(err, "Couldn't remove it. Try again.");
      },
    });
  }

  // --- Teach from a file: upload form --------------------------------------

  // Matches the server's `INTAKE_MAX_BYTES` (`kobo_api.intake`) exactly —
  // this is a client-side guard so an oversized paste is caught before the
  // round trip, not a separate rule of its own.
  const INTAKE_MAX_BYTES = 262_144;

  // Same shape as `kobo_api.intake.INTAKE_FILENAME_RE`: a lowercase letter,
  // then lowercase/digit/hyphen (40 chars max), ending in .md or .txt.
  const INTAKE_FILENAME_RE = /^[a-z][a-z0-9-]{0,39}\.(md|txt)$/;

  let intakeFilename = $state("");
  let intakeContent = $state("");
  let uploadError = $state<string | null>(null);
  let fileReadError = $state<string | null>(null);

  const intakeContentBytes = $derived(new TextEncoder().encode(intakeContent).length);
  const intakeContentTooLarge = $derived(intakeContentBytes > INTAKE_MAX_BYTES);

  const intakeFilenameError = $derived.by((): string | null => {
    if (intakeFilename.length === 0) {
      return null; // nothing to say about an empty field yet
    }
    if (!INTAKE_FILENAME_RE.test(intakeFilename)) {
      return "Use lowercase letters, numbers, and hyphens, ending in .md or .txt.";
    }
    return null;
  });

  const intakeFilenameMatch = $derived.by((): IntakeFile | null => {
    if (intakeFilename.length === 0 || !INTAKE_FILENAME_RE.test(intakeFilename)) {
      return null;
    }
    const match = $intake.data?.find((file) => file.name === intakeFilename);
    return match ?? null;
  });

  const canUpload = $derived.by((): boolean => {
    if ($uploadMutation.isPending) {
      return false;
    }
    if (intakeFilename.length === 0 || intakeFilenameError !== null) {
      return false;
    }
    if (intakeContent.trim().length === 0 || intakeContentTooLarge) {
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

  function handleFileSelect(event: Event): void {
    const input = event.currentTarget as HTMLInputElement;
    const file = input.files?.[0];
    if (!file) {
      return;
    }
    fileReadError = null;
    const reader = new FileReader();
    reader.onload = (e) => {
      try {
        const text = e.target?.result as string;
        intakeContent = text;
        // Extract lowercased basename without extension, then append original extension
        const withoutExt = file.name.lastIndexOf(".")
          ? file.name.slice(0, file.name.lastIndexOf("."))
          : file.name;
        const ext = file.name.slice(withoutExt.length);
        intakeFilename = (withoutExt + ext).toLowerCase();
      } catch {
        fileReadError = "Couldn't read this file. Try another.";
      }
    };
    reader.onerror = () => {
      fileReadError = "Couldn't read this file. Try another.";
    };
    reader.readAsText(file);
  }

  function handleUpload(event: SubmitEvent): void {
    event.preventDefault();
    if (!canUpload) {
      return;
    }
    uploadError = null;
    $uploadMutation.mutate(
      { filename: intakeFilename, content: intakeContent },
      {
        onSuccess: (result) => {
          beginDelivery(result.sha);
          intakeFilename = "";
          intakeContent = "";
        },
        onError: (err: unknown) => {
          uploadError = errorMessage(err, "Couldn't upload this file. Try again.");
        },
      },
    );
  }

  // --- Teach from a file: remove (confirm modal + success flash) ----------

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
    $intakeDeleteMutation.mutate(targetName, {
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

<section class="memory-panel" aria-labelledby="memory-panel-heading">
  <div class="memory-panel__header">
    <div>
      <h2 id="memory-panel-heading" class="memory-panel__heading">Memory</h2>
      <p class="memory-panel__subtitle">What your agent knows from the start.</p>
    </div>
    <button type="button" class="memory-panel__add" disabled={frozen} onclick={openAdd}>Add memory</button>
  </div>

  {#if $knowledge.isPending}
    <Skeleton rows={3} />
  {:else if $knowledge.isError}
    <ErrorState
      message={errorMessage($knowledge.error, "Couldn't load what this agent knows.")}
      onretry={() => $knowledge.refetch()}
    />
  {:else if $knowledge.data.length === 0}
    <EmptyState message="Nothing here yet. Add the first thing your agent should know." />
  {:else}
    <ul class="memory-panel__list">
      {#each $knowledge.data as file (file.name)}
        <li class="memory-row">
          <button type="button" class="memory-row__open" disabled={frozen} onclick={() => openEdit(file)}>
            <span class="memory-panel__sr-only">Edit </span>
            <span class="memory-row__name">{displayName(file.name)}</span>
            <span class="memory-row__preview">{firstLine(file.content)}</span>
          </button>
          <button
            type="button"
            class="memory-row__remove"
            disabled={frozen}
            aria-label={`Remove ${displayName(file.name)}`}
            onclick={() => openDelete(file)}
          >
            Remove
          </button>
        </li>
      {/each}
    </ul>
  {/if}

  <section class="intake-panel" aria-labelledby="intake-panel-heading">
    <div class="intake-panel__header">
      <h3 id="intake-panel-heading" class="intake-panel__heading">Teach from a file</h3>
      <p class="intake-panel__subtitle">
        Drop in notes or docs for your agent to learn once. The file can go away afterward, the
        learning stays.
      </p>
    </div>

    <form class="intake-form" onsubmit={handleUpload}>
      <div class="intake-form__field">
        <label class="intake-form__label" for="intake-file">Choose file</label>
        <div class="intake-form__file-row">
          <input
            id="intake-file"
            class="intake-form__file-input"
            type="file"
            accept=".md,.txt"
            onchange={handleFileSelect}
            aria-describedby="intake-file-status"
            disabled={frozen}
          />
          <div id="intake-file-status">
            {#if fileReadError}
              <p class="intake-form__status intake-form__status--error" role="alert">
                {fileReadError}
              </p>
            {/if}
          </div>
        </div>
      </div>

      <div class="intake-form__field">
        <label class="intake-form__label" for="intake-filename">File name</label>
        <input
          id="intake-filename"
          class="intake-form__input"
          type="text"
          bind:value={intakeFilename}
          autocomplete="off"
          spellcheck="false"
          aria-describedby="intake-filename-status"
          aria-invalid={intakeFilenameError !== null}
          disabled={frozen}
        />
        <p class="intake-form__help">lowercase-with-dashes, .md or .txt</p>
        <div id="intake-filename-status">
          {#if intakeFilenameError}
            <p class="intake-form__status intake-form__status--error" role="alert">
              {intakeFilenameError}
            </p>
          {:else if intakeFilenameMatch}
            <p class="intake-form__help">
              {intakeFilenameMatch.status === "ingested"
                ? "Re-uploading under this name teaches your agent again from the new content. What it already learned stays."
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
          bind:value={intakeContent}
          rows="8"
          aria-describedby="intake-content-status"
          disabled={frozen}
        ></textarea>
        <div id="intake-content-status">
          {#if intakeContentTooLarge}
            <p class="intake-form__status intake-form__status--error" role="alert">
              That's too much text. Keep it under 256KB (262,144 bytes).
            </p>
          {:else if intakeContent.trim().length === 0}
            <p class="intake-form__help">Add a bit of text before uploading.</p>
          {/if}
        </div>
      </div>

      {#if uploadError}
        <p class="intake-form__status intake-form__status--error" role="alert">{uploadError}</p>
      {/if}

      <button type="submit" class="intake-form__submit" disabled={frozen || !canUpload}>
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
      <EmptyState message="Nothing here yet. Upload a file for your agent to learn from." />
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
              disabled={frozen || file.status !== "ingested"}
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
  </section>

  {#if $deliverySha !== null}
    <div
      class="memory-panel__delivery memory-panel__delivery--{deliveryView.tone}"
      role="status"
      aria-live="polite"
    >
      {#if deliveryView.tone === "pending"}
        <span class="memory-panel__delivery-dot pulse" aria-hidden="true"></span>
      {/if}
      <span>{deliveryView.text}</span>
    </div>
  {/if}
</section>

<Modal open={target !== null} title={modalTitle} onclose={closeModal}>
  {#if target}
    {@const t = target}
    {#if t.kind === "delete"}
      <p class="memory-modal__confirm-text">
        This removes it from what {agentName} knows. Remove?
      </p>
      {#if modalError}
        <p class="memory-modal__error" role="alert">{modalError}</p>
      {/if}
      <div class="memory-modal__actions">
        <button type="button" class="memory-modal__cancel" disabled={frozen} onclick={closeModal}>Cancel</button>
        <button
          type="button"
          class="memory-modal__danger"
          disabled={frozen || $deleteMutation.isPending}
          onclick={handleDelete}
        >
          {$deleteMutation.isPending ? "Removing…" : "Remove"}
        </button>
      </div>
    {:else if reviewing}
      {#if diffTooLarge}
        <p class="memory-modal__diff-notice">
          Too much changed to preview line by line. Review your text, then save.
        </p>
      {:else}
        <div class="memory-modal__diff">
          {#each diffLines as line, i (i)}
            <p class="memory-diff-line memory-diff-line--{line.kind}">{diffMarker(
                line.kind,
              )}{line.text}</p>
          {/each}
        </div>
      {/if}
      {#if modalError}
        <p class="memory-modal__error" role="alert">{modalError}</p>
      {/if}
      <div class="memory-modal__actions">
        <button type="button" class="memory-modal__cancel" disabled={frozen} onclick={backToEditing}>
          Back to editing
        </button>
        <button
          type="button"
          class="memory-modal__primary"
          disabled={frozen || $saveMutation.isPending}
          onclick={handleSave}
        >
          {$saveMutation.isPending ? "Saving…" : "Save"}
        </button>
      </div>
    {:else}
      <form
        class="memory-form"
        onsubmit={(event) => {
          event.preventDefault();
          if (canReview) {
            reviewing = true;
          }
        }}
      >
        {#if t.kind === "add"}
          <div class="memory-form__field">
            <label class="memory-form__label" for="memory-filename">Name</label>
            <input
              id="memory-filename"
              class="memory-form__input"
              type="text"
              bind:value={filename}
              autocomplete="off"
              spellcheck="false"
              aria-describedby="memory-filename-status"
              aria-invalid={filenameError !== null}
              disabled={frozen}
            />
            <p class="memory-form__help">
              Lowercase letters, numbers, and hyphens. 1-40 characters.
            </p>
            <div id="memory-filename-status">
              {#if filenameError}
                <p class="memory-form__status memory-form__status--error" role="alert">
                  {filenameError}
                </p>
              {/if}
            </div>
          </div>
        {:else}
          <p class="memory-form__filename">{displayName(t.file.name)}</p>
        {/if}

        <div class="memory-form__field">
          <label class="memory-form__label" for="memory-content">What it knows</label>
          <textarea
            id="memory-content"
            class="memory-form__textarea"
            bind:value={content}
            rows="10"
            disabled={frozen}
          ></textarea>
          {#if content.trim().length === 0}
            <p class="memory-form__help">Add a bit of text before saving.</p>
          {/if}
        </div>

        <div class="memory-modal__actions">
          <button type="button" class="memory-modal__cancel" disabled={frozen} onclick={closeModal}>Cancel</button>
          <button type="submit" class="memory-modal__primary" disabled={frozen || !canReview}>
            {contentUnchanged ? "No changes yet" : "Review changes"}
          </button>
        </div>
      </form>
    {/if}
  {/if}
</Modal>

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
      <button type="button" class="intake-modal__cancel" disabled={frozen} onclick={closeRemove}>Cancel</button>
      <button
        type="button"
        class="intake-modal__danger"
        disabled={frozen || $intakeDeleteMutation.isPending}
        onclick={handleRemove}
      >
        {$intakeDeleteMutation.isPending ? "Removing…" : "Remove"}
      </button>
    </div>
  {/if}
</Modal>

<style>
  .memory-panel {
    display: flex;
    flex-direction: column;
    gap: var(--space-md);
    padding: var(--space-lg);
    border: 1px solid var(--border);
    border-radius: var(--radius);
    background: var(--panel);
  }

  .memory-panel__sr-only {
    position: absolute;
    width: 1px;
    height: 1px;
    overflow: hidden;
    clip: rect(0, 0, 0, 0);
    white-space: nowrap;
  }

  .memory-panel__header {
    display: flex;
    align-items: flex-start;
    justify-content: space-between;
    gap: var(--space-md);
  }

  .memory-panel__heading {
    margin: 0;
    color: var(--muted);
    font-size: 0.8rem;
    font-weight: 600;
    letter-spacing: 0.06em;
    text-transform: uppercase;
  }

  .memory-panel__subtitle {
    margin: var(--space-2xs) 0 0;
    color: var(--muted);
    font-size: 0.85rem;
  }

  .memory-panel__add {
    padding: var(--space-2xs) var(--space-sm);
    border: 1px solid var(--border);
    border-radius: var(--radius-sm);
    background: var(--panel);
    color: var(--ink);
    font-weight: 600;
    cursor: pointer;
    white-space: nowrap;
  }

  .memory-panel__add:hover {
    border-color: var(--accent);
    color: var(--accent);
  }

  .memory-panel__list {
    display: flex;
    flex-direction: column;
    gap: var(--space-xs);
    margin: 0;
    padding: 0;
    list-style: none;
  }

  .memory-row {
    display: flex;
    align-items: center;
    gap: var(--space-xs);
  }

  .memory-row__open {
    display: flex;
    flex: 1;
    flex-direction: column;
    align-items: flex-start;
    gap: 0.15rem;
    min-width: 0;
    padding: var(--space-sm);
    border: 1px solid var(--border);
    border-radius: var(--radius-sm);
    background: var(--bg);
    color: inherit;
    font: inherit;
    text-align: left;
    cursor: pointer;
  }

  .memory-row__open:hover {
    border-color: var(--accent);
  }

  .memory-row__open:focus-visible {
    outline: 2px solid var(--accent);
    outline-offset: 1px;
  }

  .memory-row__name {
    color: var(--ink);
    font-family: var(--font-mono);
    font-size: 0.9rem;
    font-weight: 600;
  }

  .memory-row__preview {
    overflow: hidden;
    max-width: 100%;
    color: var(--muted);
    font-size: 0.8rem;
    text-overflow: ellipsis;
    white-space: nowrap;
  }

  .memory-row__remove {
    padding: var(--space-2xs) var(--space-sm);
    border: 1px solid var(--border);
    border-radius: var(--radius-sm);
    background: var(--panel);
    color: var(--muted);
    cursor: pointer;
    white-space: nowrap;
  }

  .memory-row__remove:hover {
    border-color: var(--fail);
    color: var(--fail);
  }

  .memory-panel__delivery {
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

  .memory-panel__delivery--ok {
    border-color: var(--ok);
    color: var(--ok);
  }

  .memory-panel__delivery--fail {
    border-color: var(--fail);
    color: var(--fail);
  }

  .memory-panel__delivery-dot {
    width: 0.5rem;
    height: 0.5rem;
    border-radius: 50%;
    background: var(--pulse);
  }

  .memory-form {
    display: flex;
    flex-direction: column;
    gap: var(--space-lg);
  }

  .memory-form__field {
    display: flex;
    flex-direction: column;
    gap: var(--space-2xs);
  }

  .memory-form__label {
    color: var(--ink);
    font-weight: 600;
    font-size: 0.9rem;
  }

  .memory-form__filename {
    margin: 0;
    color: var(--ink);
    font-family: var(--font-mono);
    font-weight: 600;
    font-size: 0.95rem;
  }

  .memory-form__input,
  .memory-form__textarea {
    padding: var(--space-xs) var(--space-sm);
    border: 1px solid var(--border);
    border-radius: var(--radius-sm);
    background: var(--panel);
    color: var(--ink);
    font: inherit;
  }

  .memory-form__textarea {
    font-family: var(--font-mono);
    font-size: 0.85rem;
    resize: vertical;
  }

  .memory-form__input:focus-visible,
  .memory-form__textarea:focus-visible {
    outline: 2px solid var(--accent);
    outline-offset: 1px;
  }

  .memory-form__help {
    margin: 0;
    color: var(--muted);
    font-size: 0.8rem;
  }

  .memory-form__status {
    margin: 0;
    font-size: 0.8rem;
  }

  .memory-form__status--error {
    color: var(--fail);
  }

  .memory-modal__confirm-text {
    margin: 0;
    color: var(--ink);
  }

  .memory-modal__diff {
    display: flex;
    flex-direction: column;
    max-height: 24rem;
    overflow-y: auto;
    border: 1px solid var(--border);
    border-radius: var(--radius-sm);
  }

  .memory-diff-line {
    margin: 0;
    padding: 0.1rem var(--space-xs);
    overflow-wrap: anywhere;
    font-family: var(--font-mono);
    font-size: 0.85rem;
    white-space: pre-wrap;
  }

  .memory-diff-line--added {
    background: color-mix(in srgb, var(--ok) 12%, transparent);
    color: var(--ok);
  }

  .memory-diff-line--removed {
    background: color-mix(in srgb, var(--fail) 12%, transparent);
    color: var(--fail);
  }

  .memory-diff-line--same {
    color: var(--muted);
  }

  .memory-modal__diff-notice {
    margin: 0;
    padding: var(--space-sm);
    border: 1px solid var(--border);
    border-radius: var(--radius-sm);
    background: var(--bg);
    color: var(--muted);
    font-size: 0.85rem;
  }

  .memory-modal__error {
    margin: 0;
    color: var(--fail);
    font-size: 0.85rem;
  }

  .memory-modal__actions {
    display: flex;
    justify-content: flex-end;
    gap: var(--space-sm);
  }

  .memory-modal__cancel {
    padding: var(--space-xs) var(--space-md);
    border: 1px solid var(--border);
    border-radius: var(--radius-sm);
    background: var(--panel);
    color: var(--ink);
    cursor: pointer;
  }

  .memory-modal__cancel:hover:not(:disabled) {
    border-color: var(--accent);
    color: var(--accent);
  }

  .memory-modal__cancel:disabled {
    opacity: 0.6;
    cursor: not-allowed;
  }

  .memory-modal__primary {
    padding: var(--space-xs) var(--space-md);
    border: none;
    border-radius: var(--radius-sm);
    background: var(--accent);
    color: var(--accent-ink);
    font-weight: 600;
    cursor: pointer;
  }

  .memory-modal__primary:hover:not(:disabled) {
    filter: brightness(1.05);
  }

  .memory-modal__primary:disabled {
    opacity: 0.5;
    cursor: not-allowed;
  }

  .memory-modal__danger {
    padding: var(--space-xs) var(--space-md);
    border: none;
    border-radius: var(--radius-sm);
    background: var(--fail);
    color: var(--accent-ink);
    font-weight: 600;
    cursor: pointer;
  }

  .memory-modal__danger:hover:not(:disabled) {
    filter: brightness(1.05);
  }

  .memory-modal__danger:disabled {
    opacity: 0.6;
    cursor: not-allowed;
  }

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

  .intake-form__file-row {
    display: flex;
    flex-direction: column;
    gap: var(--space-2xs);
  }

  .intake-form__file-input {
    padding: var(--space-2xs) var(--space-sm);
    border: 1px solid var(--border);
    border-radius: var(--radius-sm);
    background: var(--panel);
    color: var(--ink);
    font-weight: 600;
    cursor: pointer;
    font-size: 0.9rem;
  }

  .intake-form__file-input:hover {
    border-color: var(--accent);
    color: var(--accent);
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
