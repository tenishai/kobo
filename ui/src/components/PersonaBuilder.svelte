<script lang="ts">
  // Inline "create a new persona" form: swapped in for CreateModal's persona
  // search list when the user picks its "Build your own" card. Persona-first
  // station addendum: the PRIMARY path is now "the operator already has a
  // full persona document" (pasted or uploaded) - describing a job and
  // letting the room draft one is the secondary path, below it. Still a
  // two-step form: step 1 gathers a starting point one way or another, step
  // 2 is the same editable preview regardless of which way it arrived.
  //
  // Step 1 ("draft" - the state name predates this addendum, the primary
  // path inside it isn't drafting at all any more):
  //   - PRIMARY, top: "Their persona" - a paste textarea plus the existing
  //     .md/.txt upload, both writing straight into `whatTheyDo` (the
  //     document itself already IS the persona body - there's nothing to
  //     draft for that field). "Continue" (enabled once there's a body)
  //     advances to step 2 IMMEDIATELY, then fires `POST
  //     /api/personas/distill` (`distillPersonaMutation`) in the
  //     background to pull discovery metadata - name/who_they_are/
  //     purpose_tags - out of the document; see `continueWithPersonaBody`.
  //     A quiet "start with a blank form" link skips straight to step 2
  //     with nothing filled at all, for an operator who wants to type
  //     everything by hand.
  //   - Rules (optional) sits between the two paths - rules are constraints
  //     on the agent, orthogonal to how its persona body arrives, so both
  //     paths share the same `rulesInput`.
  //   - SECONDARY, bottom: "No persona yet? Describe the job and kobo
  //     drafts one." - the original ONE required textarea, unchanged.
  //     "Draft persona" posts it (+ rules) to `POST /api/personas/draft`
  //     (`draftPersonaMutation`, a pure generator call with no cache
  //     invalidation of its own — nothing is created yet) and gets back a
  //     name/who_they_are/what_they_do the room came up with on its own.
  //
  // Step 2 ("preview"): the SAME four v2 fields (name/who they are/what
  // they do/rules). Reads correctly for any of three arrivals: job-drafted
  // ("Here's their draft. Edit anything.", `draftSucceeded`), a persona
  // document that's mid-distill or just failed to distill (a quiet inline
  // note near Name/Who they are - "Reading their persona…" while
  // `distillMutation` is in flight, "Could not read the persona. Fill
  // these in yourself." on its error, nothing once it lands), or a blank
  // start (the same generic "Fill in anything that's missing" intro every
  // non-drafted arrival already used). No-clobber either way: a drafted or
  // distilled field only ever fills one that's still empty
  // (`mergeDraftFields`/`mergeDistillFields`, ../lib/validate). Rules
  // passes through verbatim from step 1 (the same `rulesInput` state feeds
  // every path and this preview, so there's nothing to re-type). Create
  // persona itself is the untouched v2 flow: same `createPersonaMutation`,
  // same validation, same 409 handling. A quiet "start over" link drops
  // back to step 1, clearing everything.
  //
  // Draft-in-flight disables the "Draft persona" button ("Drafting…"); a
  // genuine draft *failure* (network/5xx — the endpoint itself always 200s
  // with something creatable, per `kobo_api.main.draft_persona_route`)
  // still lands on step 2, just with all four fields empty and an error
  // message up top — the classic four-field form as a fallback the
  // operator can fill in by hand rather than a dead end. A distill
  // *failure* (same "endpoint always 200s" contract,
  // `kobo_api.main.distill_persona_route`) is quieter still: no blocking
  // error banner, just the inline note above, since the operator already
  // has their persona body in hand either way — only the two extra fields
  // stay empty for them to type.
  //
  // This component owns only its own form fields plus the mutations that
  // post them — it does NOT own the "which persona is selected" or "am I
  // showing the list or the builder" state, both of which stay in
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
  //
  // `oncreated`'s second argument (persona-metadata plan, 2026-08-18) is
  // whatever purpose tags the draft/distill step came up with
  // (`draft.purpose_tags`/`distilled.purpose_tags`) — captured at that time
  // and carried through step 2 untouched, so CreateModal can pre-fill the
  // Create station's chips editor the moment this persona is actually
  // created. `[]` when neither happened, or either one failed (the classic
  // empty fallback form).
  import { createMutation, useQueryClient } from "@tanstack/svelte-query";
  import {
    ApiError,
    errorMessage,
    type CatalogEntry,
    type PersonaForm,
  } from "../lib/api";
  import {
    createPersonaMutation,
    distillPersonaMutation,
    draftPersonaMutation,
    nameSuggestionsQuery,
  } from "../lib/queries";
  import { mergeDistillFields, mergeDraftFields, splitLines } from "../lib/validate";

  let {
    oncancel,
    oncreated,
  }: {
    oncancel: () => void;
    oncreated: (persona: CatalogEntry, purposeTags: string[]) => void;
  } = $props();

  const queryClient = useQueryClient();
  const draftMutation = createMutation(draftPersonaMutation());
  // Persona-first station addendum: fired in the background the moment the
  // operator hands over a ready-made persona document (paste or upload) and
  // clicks Continue - never blocks the advance to step 2, see
  // `continueWithPersonaBody` below.
  const distillMutation = createMutation(distillPersonaMutation());
  const mutation = createMutation(createPersonaMutation(queryClient));

  let step = $state<"draft" | "preview">("draft");

  // Tracks whether step 2 was reached via a successful draft, as opposed to
  // a bring-your-own-persona upload/paste or a draft that failed - purely a
  // copy switch for step 2's intro line below, so it never describes a
  // draft that doesn't exist.
  let draftSucceeded = $state(false);

  // Rules count cap (upload/limits addendum): mirrors PersonaFields.rules'
  // own Field(max_length=150) (kobo_api/personas.py) — the per-rule length
  // cap that used to live beside it is gone (the merged persona.md's own
  // 150-line/32768-byte gate is the real ceiling now, checked server-side
  // against the actual rendered file, not a per-field lookalike), so this
  // is the only client-side rules number left worth mirroring.
  const RULES_MAX = 150;

  // --- Step 1: one plain question -----------------------------------------

  let job = $state("");
  let rulesInput = $state("");
  let draftError = $state<string | null>(null);
  let rulesFileError = $state<string | null>(null);

  const trimmedJob = $derived(job.trim());

  // Shown only once there's something to say about it, same convention
  // every other field-level error in this room uses.
  const jobError = $derived(
    trimmedJob.length > 500 ? "Keep it to 500 characters or fewer." : null,
  );

  const rules = $derived(splitLines(rulesInput));

  const rulesError = $derived(
    rules.length > RULES_MAX ? `Use up to ${RULES_MAX}, one per line.` : null,
  );

  const canDraft = $derived(
    trimmedJob.length > 0 &&
      trimmedJob.length <= 500 &&
      rules.length <= RULES_MAX &&
      !$draftMutation.isPending,
  );

  /** Mirrors MemoryPanel.svelte's own `handleFileSelect` idiom exactly: read
   * the chosen file as text, drop it straight into `rulesInput` (an upload
   * is an explicit act - replacing whatever was typed there is expected),
   * and surface the same plain failure copy on a read error. */
  function handleRulesFileSelect(event: Event): void {
    const input = event.currentTarget as HTMLInputElement;
    const file = input.files?.[0];
    if (!file) {
      return;
    }
    rulesFileError = null;
    const reader = new FileReader();
    reader.onload = (e) => {
      try {
        rulesInput = e.target?.result as string;
      } catch {
        rulesFileError = "Couldn't read this file. Try another.";
      }
    };
    reader.onerror = () => {
      rulesFileError = "Couldn't read this file. Try another.";
    };
    reader.readAsText(file);
  }

  // --- Step 2: the editable preview ----------------------------------------

  let name = $state("");
  let whoTheyAre = $state("");
  let whatTheyDo = $state("");
  let submitError = $state<string | null>(null);
  let whatTheyDoFileError = $state<string | null>(null);

  /** Same read-as-text idiom as `handleRulesFileSelect` above, for the
   * persona body textarea. */
  function handleWhatTheyDoFileSelect(event: Event): void {
    const input = event.currentTarget as HTMLInputElement;
    const file = input.files?.[0];
    if (!file) {
      return;
    }
    whatTheyDoFileError = null;
    const reader = new FileReader();
    reader.onload = (e) => {
      try {
        whatTheyDo = e.target?.result as string;
      } catch {
        whatTheyDoFileError = "Couldn't read this file. Try another.";
      }
    };
    reader.onerror = () => {
      whatTheyDoFileError = "Couldn't read this file. Try another.";
    };
    reader.readAsText(file);
  }

  // --- Their persona (step 1, PRIMARY - persona-first station addendum) ----
  //
  // An operator who arrives with a persona document already written (paste
  // or upload, both writing into the very same `whatTheyDo` state step 2's
  // textarea binds to) never wants the job-drafting step at all - this is
  // the first thing they see, at the top of step 1. Both paths below skip
  // `draftPersonaMutation` entirely, but - unlike the old "just advance"
  // behavior this addendum replaces - Continue fires `distillMutation` in
  // the background instead of leaving name/who-they-are for the operator to
  // fill in unassisted; see `continueWithPersonaBody` below.

  let personaFileError = $state<string | null>(null);

  /** Advance to step 2 IMMEDIATELY (never wait on the network), then fire
   * the distill mutation against whatever `whatTheyDo` holds right now, in
   * the background. On success, fills ONLY the fields still empty
   * (`mergeDistillFields`'s no-clobber rule - an operator who started
   * typing Name or Who they are during the brief window before this lands
   * never gets overwritten) and carries the distilled purpose tags through
   * to `oncreated`, same as a job-drafted persona's tags. On error, the
   * quiet inline note near those two fields (driven directly by
   * `$distillMutation.isError` in the markup below) is the only signal -
   * no blocking error banner, since the operator's persona body is already
   * safely sitting in the form either way.
   */
  function continueWithPersonaBody(): void {
    draftError = null;
    draftSucceeded = false;
    draftedPurposeTags = [];
    step = "preview";
    $distillMutation.mutate(whatTheyDo, {
      onSuccess: (distilled) => {
        const merged = mergeDistillFields({ name, whoTheyAre }, distilled);
        name = merged.name;
        whoTheyAre = merged.whoTheyAre;
        draftedPurposeTags = distilled.purpose_tags;
      },
    });
  }

  /** The paste/Continue half of the primary path: does nothing while
   * `whatTheyDo` is still empty (mirrors every other submit guard in this
   * component - the button itself is also disabled the same way, this is
   * defense-in-depth). */
  function handleContinueClick(): void {
    if (whatTheyDo.trim().length === 0) {
      return;
    }
    continueWithPersonaBody();
  }

  /** Same read-as-text idiom as `handleWhatTheyDoFileSelect` above (same
   * failure copy too), but triggered from step 1: it fills the very same
   * `whatTheyDo` state, then continues exactly like a Continue click on
   * the pasted-text path (advance immediately, distill in the background)
   * - upload and paste are two doors into the one primary flow. */
  function handlePersonaFileSelect(event: Event): void {
    const input = event.currentTarget as HTMLInputElement;
    const file = input.files?.[0];
    if (!file) {
      return;
    }
    personaFileError = null;
    const reader = new FileReader();
    reader.onload = (e) => {
      try {
        whatTheyDo = e.target?.result as string;
        continueWithPersonaBody();
      } catch {
        personaFileError = "Couldn't read this file. Try another.";
      }
    };
    reader.onerror = () => {
      personaFileError = "Couldn't read this file. Try another.";
    };
    reader.readAsText(file);
  }

  /** The blank-start escape hatch: jump straight to the editable preview
   * with every field exactly as it is - typically empty, aside from
   * whatever rules were already typed or uploaded above - for an operator
   * who wants to type everything by hand rather than paste/upload or
   * describe a job. No draft, no distill. */
  function startFromOwnText(): void {
    draftSucceeded = false;
    step = "preview";
  }

  // Persona-metadata plan: whatever the draft step's model call (or its
  // deterministic fallback) came up with for purpose tags, carried through
  // step 2 untouched and handed to `oncreated` once Create persona lands.
  let draftedPurposeTags = $state<string[]>([]);

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
      rules.length <= RULES_MAX &&
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
          // No-clobber: operator text - typed, pasted, or uploaded - is
          // never overwritten by a draft; a draft only ever fills in
          // whatever field is still empty (mergeDraftFields, ../lib/validate,
          // judges each field on its own).
          const merged = mergeDraftFields({ name, whoTheyAre, whatTheyDo }, draft);
          name = merged.name;
          whoTheyAre = merged.whoTheyAre;
          whatTheyDo = merged.whatTheyDo;
          draftedPurposeTags = draft.purpose_tags;
          draftSucceeded = true;
          step = "preview";
        },
        onError: (error: unknown) => {
          // The draft endpoint itself always 200s — a rejection here means
          // a genuine network/5xx failure, not "the generator was down"
          // (that's handled server-side with its own fallback). Reveal the
          // classic four-field form so there's still a way forward - but
          // never blank out anything the operator already typed, pasted,
          // or uploaded, same no-clobber rule as the success path above.
          draftError = errorMessage(
            error,
            "Couldn't draft a persona right now. Fill it in yourself below.",
          );
          draftedPurposeTags = [];
          draftSucceeded = false;
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
    draftedPurposeTags = [];
    draftSucceeded = false;
    draftError = null;
    submitError = null;
    // Clears a lingering "Reading their persona…"/"Could not read the
    // persona" note from a prior visit to step 2 - without this, a distill
    // that errored (or is still somehow pending) before "start over" was
    // clicked would otherwise bleed its stale status into whichever path
    // the operator picks next.
    $distillMutation.reset();
  }

  function handleCreateSubmit(event: SubmitEvent): void {
    event.preventDefault();
    if (!canCreate) {
      return;
    }
    submitError = null;
    // Naming station perf fix: warms the suggestion cache the moment this
    // persona's who/what pair is FINAL, at this click, not at any keystroke
    // above (`canCreate` already guarantees both fields are non-empty).
    // Fired here rather than waiting for `oncreated` (CreateModal's own
    // `selectPersona`, called once the mutation below actually succeeds)
    // so the naming station's Vertex call runs IN PARALLEL with the
    // create-persona POST, instead of only starting once that round trip
    // lands. Second argument is `name`, not `whatTheyDo`, since
    // CreateModal's own persona-commit effect stands the created persona's
    // `name` in for `what_they_do` (see CreateModal.svelte's "The Naming"
    // doc comment), so `name` is the key the naming station will actually
    // read once this persona lands there; prefetching under `whatTheyDo`
    // instead would warm a cache entry nothing ever looks at.
    void queryClient.prefetchQuery(nameSuggestionsQuery(whoTheyAre, name));
    const body: PersonaForm = {
      name,
      who_they_are: whoTheyAre,
      what_they_do: whatTheyDo,
      rules,
    };
    $mutation.mutate(body, {
      onSuccess: (created) => {
        oncreated(created, draftedPurposeTags);
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
    <div class="persona-builder__own-persona">
      <p class="persona-builder__own-persona-lead">Their persona</p>
      <div class="persona-builder__field">
        <label class="persona-builder__label" for="persona-builder-persona-body">
          Paste their persona
        </label>
        <textarea
          id="persona-builder-persona-body"
          class="persona-builder__textarea"
          bind:value={whatTheyDo}
          rows="6"
          placeholder="Paste the full document here."
        ></textarea>
      </div>
      <div class="persona-builder__upload">
        <label class="persona-builder__upload-label" for="persona-builder-own-persona-file">
          Upload persona file
        </label>
        <input
          id="persona-builder-own-persona-file"
          type="file"
          accept=".md,.txt"
          onchange={handlePersonaFileSelect}
          aria-describedby="persona-builder-own-persona-file-status"
        />
        <div id="persona-builder-own-persona-file-status">
          {#if personaFileError}
            <p class="persona-builder__status persona-builder__status--error" role="alert">
              {personaFileError}
            </p>
          {/if}
        </div>
      </div>
      <div class="persona-builder__actions">
        <button
          type="button"
          class="persona-builder__submit"
          disabled={whatTheyDo.trim().length === 0}
          onclick={handleContinueClick}
        >
          Continue
        </button>
      </div>
      <button
        type="button"
        class="persona-builder__start-over"
        onclick={startFromOwnText}
      >
        Start with a blank form
      </button>
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
      <div class="persona-builder__upload">
        <label class="persona-builder__upload-label" for="persona-builder-rules-file">
          Upload rules file
        </label>
        <input
          id="persona-builder-rules-file"
          type="file"
          accept=".md,.txt"
          onchange={handleRulesFileSelect}
          aria-describedby="persona-builder-rules-file-status"
        />
        <div id="persona-builder-rules-file-status">
          {#if rulesFileError}
            <p class="persona-builder__status persona-builder__status--error" role="alert">
              {rulesFileError}
            </p>
          {/if}
        </div>
      </div>
      <p class="persona-builder__help">Up to {RULES_MAX}, one per line.</p>
      <div id="persona-builder-rules-status">
        {#if rulesError}
          <p class="persona-builder__status persona-builder__status--error" role="alert">
            {rulesError}
          </p>
        {/if}
      </div>
    </div>

    <div class="persona-builder__secondary">
      <p class="persona-builder__secondary-lead">
        No persona yet? Describe the job and Kobo drafts one.
      </p>
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
    <p class="persona-builder__intro">
      {draftSucceeded
        ? "Here's their draft. Edit anything."
        : "Fill in anything that's missing, then create."}
    </p>

    {#if draftError}
      <p class="persona-builder__status persona-builder__status--error" role="alert">
        {draftError}
      </p>
    {/if}

    {#if $distillMutation.isPending}
      <p class="persona-builder__status persona-builder__status--quiet">
        Reading their persona…
      </p>
    {:else if $distillMutation.isError}
      <p class="persona-builder__status persona-builder__status--quiet">
        Could not read the persona. Fill these in yourself.
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
      <div class="persona-builder__upload">
        <label class="persona-builder__upload-label" for="persona-builder-what-they-do-file">
          Upload body file
        </label>
        <input
          id="persona-builder-what-they-do-file"
          type="file"
          accept=".md,.txt"
          onchange={handleWhatTheyDoFileSelect}
          aria-describedby="persona-builder-what-they-do-file-status"
        />
        <div id="persona-builder-what-they-do-file-status">
          {#if whatTheyDoFileError}
            <p class="persona-builder__status persona-builder__status--error" role="alert">
              {whatTheyDoFileError}
            </p>
          {/if}
        </div>
      </div>
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
      <div class="persona-builder__upload">
        <label class="persona-builder__upload-label" for="persona-builder-rules-preview-file">
          Upload rules file
        </label>
        <input
          id="persona-builder-rules-preview-file"
          type="file"
          accept=".md,.txt"
          onchange={handleRulesFileSelect}
          aria-describedby="persona-builder-rules-preview-file-status"
        />
        <div id="persona-builder-rules-preview-file-status">
          {#if rulesFileError}
            <p class="persona-builder__status persona-builder__status--error" role="alert">
              {rulesFileError}
            </p>
          {/if}
        </div>
      </div>
      <p class="persona-builder__help">Up to {RULES_MAX}, one per line.</p>
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

  .persona-builder__upload {
    display: flex;
    align-items: center;
    gap: var(--space-xs);
  }

  .persona-builder__upload-label {
    color: var(--muted);
    font-size: 0.8rem;
  }

  .persona-builder__own-persona {
    display: flex;
    flex-direction: column;
    gap: var(--space-sm);
    padding-bottom: var(--space-sm);
    border-bottom: 1px solid var(--border);
  }

  .persona-builder__own-persona-lead {
    margin: 0;
    color: var(--ink);
    font-weight: 600;
    font-size: 0.9rem;
  }

  .persona-builder__secondary {
    display: flex;
    flex-direction: column;
    gap: var(--space-md);
    padding-top: var(--space-sm);
    border-top: 1px solid var(--border);
  }

  .persona-builder__secondary-lead {
    margin: 0;
    color: var(--muted);
    font-size: 0.85rem;
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

  .persona-builder__status--quiet {
    color: var(--muted);
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
