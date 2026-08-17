<script lang="ts">
  // Inline "create a new persona" form: swapped in for CreateModal's persona
  // search list when the user picks its "Build your own" card. Persona v3
  // ("describe the job, approve the soul" addendum): the room writes the
  // first draft now, not the operator — this is a two-step form, not the
  // v2 four-box builder it replaces.
  //
  // Step 1 ("draft"): ONE required textarea — "What should this agent do
  // for you?" — plus the existing optional Rules lines. "Draft persona"
  // posts both to `POST /api/personas/draft` (`draftPersonaMutation`, a
  // pure generator call with no cache invalidation of its own — nothing is
  // created yet) and gets back a name/who_they_are/what_they_do the room
  // came up with on its own.
  //
  // Step 2 ("preview"): the SAME four v2 fields (name/who they are/what
  // they do/rules), now prefilled from the draft response — "Here's their
  // draft — edit anything." Rules passes through verbatim from step 1 (the
  // same `rulesInput` state feeds both the draft call and this preview, so
  // there's nothing to re-type). Create persona itself is the untouched v2
  // flow: same `createPersonaMutation`, same validation, same 409 handling.
  // A quiet "start over" link drops back to step 1, clearing everything —
  // for when the draft missed the mark and a fresh job description is
  // easier than editing around it.
  //
  // Draft-in-flight disables the button ("Drafting…"); a genuine draft
  // *failure* (network/5xx — the endpoint itself always 200s with
  // something creatable, per `kobo_api.main.draft_persona_route`) still
  // lands on step 2, just with all four fields empty and an error message
  // up top — the classic four-field form as a fallback the operator can
  // fill in by hand rather than a dead end.
  //
  // This component owns only its own form fields plus the two mutations
  // that post them — it does NOT own the "which persona is selected" or
  // "am I showing the list or the builder" state, both of which stay in
  // CreateModal (this is the thing that swaps in and out, not the thing
  // deciding when to swap). No `team` prop (Persona v3 / teamless-create
  // addendum): the create modal's Team station is gone entirely, so there
  // is nothing to tag the new persona with — the server defaults the
  // recorded team to "shared" (`CreatePersonaRequest.team`) when the
  // create payload omits it, same as `createAgent`'s payload does now.
  //
  // `oncreated` hands back the full created CatalogEntry, not just its id
  // (create flow v2 "naming ceremony" addendum): CreateModal needs the
  // persona's `description` (== `who_they_are` verbatim — see
  // `kobo_api.personas`) and `name` right away to seed the naming station's
  // suggestion fetch, and reading them back off the persona search cache
  // would race that cache's own debounced refetch after this mutation's
  // `invalidateQueries` — the mutation result is already sitting right
  // here, so CreateModal just takes it directly instead.
  import { createMutation, useQueryClient } from "@tanstack/svelte-query";
  import {
    ApiError,
    errorMessage,
    type CatalogEntry,
    type PersonaForm,
  } from "../lib/api";
  import { createPersonaMutation, draftPersonaMutation } from "../lib/queries";
  import { splitLines } from "../lib/validate";

  let {
    oncancel,
    oncreated,
  }: {
    oncancel: () => void;
    oncreated: (persona: CatalogEntry) => void;
  } = $props();

  const queryClient = useQueryClient();
  const draftMutation = createMutation(draftPersonaMutation());
  const mutation = createMutation(createPersonaMutation(queryClient));

  let step = $state<"draft" | "preview">("draft");

  // --- Step 1: one plain question -----------------------------------------

  let job = $state("");
  let rulesInput = $state("");
  let draftError = $state<string | null>(null);

  const trimmedJob = $derived(job.trim());

  // Shown only once there's something to say about it, same convention
  // every other field-level error in this room uses.
  const jobError = $derived(
    trimmedJob.length > 500 ? "Keep it to 500 characters or fewer." : null,
  );

  const rules = $derived(splitLines(rulesInput));

  const rulesError = $derived(
    rules.length > 8 ? "Use up to 8, one per line." : null,
  );

  const canDraft = $derived(
    trimmedJob.length > 0 &&
      trimmedJob.length <= 500 &&
      rules.length <= 8 &&
      !$draftMutation.isPending,
  );

  // --- Step 2: the editable preview ----------------------------------------

  let name = $state("");
  let whoTheyAre = $state("");
  let whatTheyDo = $state("");
  let submitError = $state<string | null>(null);

  // Display-only cue (round 2 fix, carried over from v2): the server
  // slugifies `name` on its own (`persona_id()`, kobo_api/personas.py)
  // rather than rejecting an unexpected-charset name outright, so this
  // doesn't feed `canCreate` below — nothing here gates submission, same
  // as before. It exists purely so a name like "QAer" doesn't sail through
  // the field with zero feedback.
  const NAME_CHARSET_RE = /^[a-z0-9-]+$/;
  const nameHint = $derived(
    name.trim().length > 0 && !NAME_CHARSET_RE.test(name.trim())
      ? "Lowercase letters, numbers, and hyphens."
      : null,
  );

  const canCreate = $derived(
    name.trim().length > 0 &&
      whoTheyAre.trim().length > 0 &&
      whatTheyDo.trim().length > 0 &&
      rules.length <= 8 &&
      !$mutation.isPending,
  );

  function handleDraftSubmit(event: SubmitEvent): void {
    event.preventDefault();
    if (!canDraft) {
      return;
    }
    draftError = null;
    $draftMutation.mutate(
      { job: trimmedJob, rules },
      {
        onSuccess: (draft) => {
          name = draft.name;
          whoTheyAre = draft.who_they_are;
          whatTheyDo = draft.what_they_do;
          step = "preview";
        },
        onError: (error: unknown) => {
          // The draft endpoint itself always 200s — a rejection here means
          // a genuine network/5xx failure, not "the generator was down"
          // (that's handled server-side with its own fallback). Reveal the
          // classic four-field form empty so there's still a way forward.
          draftError = errorMessage(
            error,
            "Couldn't draft a persona right now. Fill it in yourself below.",
          );
          name = "";
          whoTheyAre = "";
          whatTheyDo = "";
          step = "preview";
        },
      },
    );
  }

  function startOver(): void {
    step = "draft";
    job = "";
    rulesInput = "";
    name = "";
    whoTheyAre = "";
    whatTheyDo = "";
    draftError = null;
    submitError = null;
  }

  function handleCreateSubmit(event: SubmitEvent): void {
    event.preventDefault();
    if (!canCreate) {
      return;
    }
    submitError = null;
    const body: PersonaForm = {
      name,
      who_they_are: whoTheyAre,
      what_they_do: whatTheyDo,
      rules,
    };
    $mutation.mutate(body, {
      onSuccess: (created) => {
        oncreated(created);
      },
      onError: (error: unknown) => {
        if (error instanceof ApiError && error.status === 409) {
          // The server may already have this persona from an earlier
          // attempt that landed but whose response never made it back here
          // (a dropped connection, a refresh mid-request, ...) — pointing
          // that out beats a bare "already exists" that leaves the user
          // wondering whether to go look for it.
          submitError =
            "A persona with that name already exists. It may already be in the list.";
        } else {
          submitError = errorMessage(error, "Something went wrong. Try again.");
        }
      },
    });
  }
</script>

{#if step === "draft"}
  <form class="persona-builder" onsubmit={handleDraftSubmit}>
    <div class="persona-builder__field">
      <label class="persona-builder__label" for="persona-builder-job">
        What should this agent do for you?
      </label>
      <textarea
        id="persona-builder-job"
        class="persona-builder__textarea"
        bind:value={job}
        rows="3"
        aria-describedby="persona-builder-job-status"
        aria-invalid={jobError !== null}
      ></textarea>
      <p class="persona-builder__help">A sentence or two, plain.</p>
      <div id="persona-builder-job-status">
        {#if jobError}
          <p class="persona-builder__status persona-builder__status--error" role="alert">
            {jobError}
          </p>
        {/if}
      </div>
    </div>

    <div class="persona-builder__field">
      <label class="persona-builder__label" for="persona-builder-rules">
        Rules (optional)
      </label>
      <textarea
        id="persona-builder-rules"
        class="persona-builder__textarea"
        bind:value={rulesInput}
        rows="3"
        placeholder="One per line: things they always or never do."
        aria-describedby="persona-builder-rules-status"
        aria-invalid={rulesError !== null}
      ></textarea>
      <p class="persona-builder__help">Up to 8, one per line.</p>
      <div id="persona-builder-rules-status">
        {#if rulesError}
          <p class="persona-builder__status persona-builder__status--error" role="alert">
            {rulesError}
          </p>
        {/if}
      </div>
    </div>

    <div class="persona-builder__actions">
      <button type="button" class="persona-builder__cancel" onclick={oncancel}>
        Cancel
      </button>
      <button type="submit" class="persona-builder__submit" disabled={!canDraft}>
        {$draftMutation.isPending ? "Drafting…" : "Draft persona"}
      </button>
    </div>
  </form>
{:else}
  <form class="persona-builder" onsubmit={handleCreateSubmit}>
    <p class="persona-builder__intro">Here's their draft. Edit anything.</p>

    {#if draftError}
      <p class="persona-builder__status persona-builder__status--error" role="alert">
        {draftError}
      </p>
    {/if}

    <div class="persona-builder__field">
      <label class="persona-builder__label" for="persona-builder-name">Name</label>
      <input
        id="persona-builder-name"
        class="persona-builder__input"
        type="text"
        bind:value={name}
        autocomplete="off"
        aria-describedby="persona-builder-name-hint"
      />
      <div id="persona-builder-name-hint">
        {#if nameHint}
          <p class="persona-builder__help">{nameHint}</p>
        {/if}
      </div>
    </div>

    <div class="persona-builder__field">
      <label class="persona-builder__label" for="persona-builder-who-they-are">
        Who they are
      </label>
      <input
        id="persona-builder-who-they-are"
        class="persona-builder__input"
        type="text"
        bind:value={whoTheyAre}
        placeholder="an unflappable network detective who's seen every outage twice"
        autocomplete="off"
      />
      <p class="persona-builder__help">One vivid line: who is this agent?</p>
    </div>

    <div class="persona-builder__field">
      <label class="persona-builder__label" for="persona-builder-what-they-do">
        What they do
      </label>
      <textarea
        id="persona-builder-what-they-do"
        class="persona-builder__textarea"
        bind:value={whatTheyDo}
        rows="3"
      ></textarea>
      <p class="persona-builder__help">2-4 sentences, plain prose.</p>
    </div>

    <div class="persona-builder__field">
      <label class="persona-builder__label" for="persona-builder-rules-preview">
        Rules (optional)
      </label>
      <textarea
        id="persona-builder-rules-preview"
        class="persona-builder__textarea"
        bind:value={rulesInput}
        rows="3"
        placeholder="One per line: things they always or never do."
        aria-describedby="persona-builder-rules-preview-status"
        aria-invalid={rulesError !== null}
      ></textarea>
      <p class="persona-builder__help">Up to 8, one per line.</p>
      <div id="persona-builder-rules-preview-status">
        {#if rulesError}
          <p class="persona-builder__status persona-builder__status--error" role="alert">
            {rulesError}
          </p>
        {/if}
      </div>
    </div>

    {#if submitError}
      <p class="persona-builder__status persona-builder__status--error" role="alert">
        {submitError}
      </p>
    {/if}

    <button type="button" class="persona-builder__start-over" onclick={startOver}>
      start over
    </button>

    <div class="persona-builder__actions">
      <button type="button" class="persona-builder__cancel" onclick={oncancel}>
        Cancel
      </button>
      <button type="submit" class="persona-builder__submit" disabled={!canCreate}>
        {$mutation.isPending ? "Creating…" : "Create persona"}
      </button>
    </div>
  </form>
{/if}

<style>
  .persona-builder {
    display: flex;
    flex-direction: column;
    gap: var(--space-md);
  }

  .persona-builder__intro {
    margin: 0;
    color: var(--muted);
    font-size: 0.9rem;
  }

  .persona-builder__field {
    display: flex;
    flex-direction: column;
    gap: var(--space-2xs);
  }

  .persona-builder__label {
    color: var(--ink);
    font-weight: 600;
    font-size: 0.9rem;
  }

  .persona-builder__input,
  .persona-builder__textarea {
    padding: var(--space-xs) var(--space-sm);
    border: 1px solid var(--border);
    border-radius: var(--radius-sm);
    background: var(--panel);
    color: var(--ink);
    font: inherit;
  }

  .persona-builder__textarea {
    resize: vertical;
  }

  .persona-builder__input:focus-visible,
  .persona-builder__textarea:focus-visible {
    outline: 2px solid var(--accent);
    outline-offset: 1px;
  }

  .persona-builder__help {
    margin: 0;
    color: var(--muted);
    font-size: 0.8rem;
  }

  .persona-builder__status {
    margin: 0;
    font-size: 0.8rem;
  }

  .persona-builder__status--error {
    color: var(--fail);
  }

  .persona-builder__start-over {
    align-self: flex-start;
    padding: 0;
    border: none;
    background: none;
    color: var(--muted);
    font-size: 0.8rem;
    text-decoration: underline;
    cursor: pointer;
  }

  .persona-builder__start-over:hover {
    color: var(--accent);
  }

  .persona-builder__actions {
    display: flex;
    justify-content: flex-end;
    gap: var(--space-sm);
  }

  .persona-builder__cancel {
    padding: var(--space-xs) var(--space-md);
    border: 1px solid var(--border);
    border-radius: var(--radius-sm);
    background: var(--panel);
    color: var(--ink);
    cursor: pointer;
  }

  .persona-builder__cancel:hover {
    border-color: var(--accent);
    color: var(--accent);
  }

  .persona-builder__submit {
    padding: var(--space-xs) var(--space-md);
    border: none;
    border-radius: var(--radius-sm);
    background: var(--accent);
    color: var(--accent-ink);
    font-weight: 600;
    cursor: pointer;
  }

  .persona-builder__submit:hover:not(:disabled) {
    filter: brightness(1.05);
  }

  .persona-builder__submit:disabled {
    opacity: 0.5;
    cursor: not-allowed;
  }
</style>
