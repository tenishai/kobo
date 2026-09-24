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
  // Modal idiom wave (2026-09-23): the fields read from the shared `.field`
  // classes and both steps end in one action row -- "Start with a blank
  // form" (step 1) and "Start over" (step 2) are the quiet button on its
  // left, Cancel and the step's own finishing button on its right. The row
  // is sticky inside the create dialog's scrolling body rather than handed
  // to the shell's footer: this form is swapped in for the persona list,
  // inside that body, and while it is up the dialog's own footer is not --
  // so the one filled button in the dialog is this form's (Continue at step
  // 1, since a persona somebody already wrote is the primary path; Create
  // persona at step 2).
  //
  // This component owns only its own form fields plus the mutations that
  // post them — it does NOT own the "which persona is selected" or "am I
  // showing the list or the builder" state, both of which stay in
  // CreateModal (this is the thing that swaps in and out, not the thing
  // deciding when to swap).
  //
  // "Who is this persona for?" (persona team scope, operator ruling: "most
  // personas are created under the shared tag; they should be scoped to
  // the group that created them"): a select of the caller's own groups
  // (whoami's `groups`, the config names the create form's Group select
  // also lists) plus "Everyone (shared)", sent as `team` on create. An
  // operator sees every group the room has named (`groupsQuery`). The
  // default is the Group already picked on the create form when the caller
  // is in it (`defaultTeam`), else their only group when they have exactly
  // one, else everyone. The server's team gate is still the real check; a
  // refusal from it reads as one plain sentence.
  //
  // `oncreated` hands back the full created CatalogEntry, not just its id
  // (create flow v2 "naming ceremony" addendum): CreateModal selects this
  // persona the moment it lands, and reading it back off the persona
  // search cache would race that cache's own debounced refetch after this
  // mutation's `invalidateQueries` — the mutation result is already sitting
  // right here, so CreateModal just takes it directly instead.
  //
  // `oncreated`'s second argument (persona-metadata plan, 2026-08-18) is
  // whatever purpose tags the draft/distill step came up with
  // (`draft.purpose_tags`/`distilled.purpose_tags`) — captured at that time
  // and carried through step 2 untouched, so CreateModal can pre-fill its
  // own chips editor the moment this persona is actually created. `[]` when
  // neither happened, or either one failed (the classic empty fallback
  // form).
  //
  // There used to be a third argument, and a name-suggestions prefetch at
  // the "Create persona" click to go with it: the create dialog's old
  // naming station asked a generator for three names, and this was the
  // earliest moment its who/what pair was final. The plain-form wave
  // retired that station -- a person types the name now -- so both are
  // gone, and a persona create no longer starts a generator call nobody
  // will read the result of.
  import { createMutation, createQuery, useQueryClient } from "@tanstack/svelte-query";
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
    groupsQuery,
    whoamiQuery,
  } from "../lib/queries";
  import {
    mergeDistillFields,
    mergeDraftFields,
    personaTeamOptions,
    splitLines,
  } from "../lib/validate";

  let {
    oncancel,
    oncreated,
    defaultTeam = null,
  }: {
    oncancel: () => void;
    oncreated: (persona: CatalogEntry, purposeTags: string[]) => void;
    /** The Group already chosen on the create form, if any. */
    defaultTeam?: string | null;
  } = $props();

  const queryClient = useQueryClient();
  const draftMutation = createMutation(() => (draftPersonaMutation()));
  // Persona-first station addendum: fired in the background the moment the
  // operator hands over a ready-made persona document (paste or upload) and
  // clicks Continue - never blocks the advance to step 2, see
  // `continueWithPersonaBody` below.
  const distillMutation = createMutation(() => (distillPersonaMutation()));
  const mutation = createMutation(() => (createPersonaMutation(queryClient)));

  let step = $state<"draft" | "preview">("draft");

  // --- Who the persona is for ----------------------------------------------

  const SHARED_TEAM = "shared";
  const whoami = createQuery(() => whoamiQuery());
  const isOperator = $derived(whoami.data?.operator === true);
  const roomGroups = createQuery(() => ({ ...groupsQuery(), enabled: isOperator }));

  const myGroups = $derived(
    (whoami.data?.groups ?? []).filter((g) => g !== SHARED_TEAM),
  );
  const teamOptions = $derived(personaTeamOptions(whoami.data, roomGroups.data));
  const suggestedTeam = $derived(
    defaultTeam && (defaultTeam === SHARED_TEAM || teamOptions.includes(defaultTeam))
      ? defaultTeam
      : myGroups.length === 1
        ? myGroups[0]
        : SHARED_TEAM,
  );
  let teamOverride = $state<string | null>(null);
  const selectedTeam = $derived(teamOverride ?? suggestedTeam);

  const TEAM_REFUSED =
    "You are not in that group, so the persona can't be filed there. Pick one of your groups, or Everyone.";

  // Tracks whether step 2 was reached via a successful draft, as opposed to
  // a bring-your-own-persona upload/paste or a draft that failed - purely a
  // copy switch for step 2's intro line below, so it never describes a
  // draft that doesn't exist.
  let draftSucceeded = $state(false);

  // Rules count cap (upload/limits addendum): mirrors PersonaFields.rules'
  // own Field(max_length=150) (kobo_api/personas.py) — the per-rule length
  // cap that used to live beside it is gone (the merged persona.md's own
  // 200-line/49152-byte gate is the real ceiling now, checked server-side
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
      !draftMutation.isPending,
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
   * `distillMutation.isError` in the markup below) is the only signal -
   * no blocking error banner, since the operator's persona body is already
   * safely sitting in the form either way.
   */
  function continueWithPersonaBody(): void {
    draftError = null;
    draftSucceeded = false;
    draftedPurposeTags = [];
    step = "preview";
    distillMutation.mutate(whatTheyDo, {
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
      !mutation.isPending,
  );

  function handleDraftSubmit(event: SubmitEvent): void {
    event.preventDefault();
    if (!canDraft) {
      return;
    }
    draftError = null;
    draftMutation.mutate(
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
    distillMutation.reset();
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
      purpose_tags: draftedPurposeTags,
      team: selectedTeam,
    };
    mutation.mutate(body, {
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
        } else if (error instanceof ApiError && error.status === 403) {
          submitError = TEAM_REFUSED;
        } else {
          submitError = errorMessage(error, "Something went wrong. Try again.");
        }
      },
    });
  }
</script>


{#if step === "draft"}
  <form class="modal__form" onsubmit={handleDraftSubmit}>
    <div class="modal__section">
      <p class="field__label">Their persona</p>
      <div class="field">
        <label class="field__label" for="persona-builder-persona-body">
          Paste their persona
        </label>
        <textarea
          id="persona-builder-persona-body"
          class="field__input"
          bind:value={whatTheyDo}
          rows="6"
          placeholder="Paste the full document here."
        ></textarea>
      </div>
      <div class="persona-builder__upload">
        <label class="field__help" for="persona-builder-own-persona-file">
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
            <p class="field__status field__status--error" role="alert">
              {personaFileError}
            </p>
          {/if}
        </div>
      </div>
    </div>

    <div class="field">
      <label class="field__label" for="persona-builder-rules">
        Rules (optional)
      </label>
      <textarea
        id="persona-builder-rules"
        class="field__input"
        bind:value={rulesInput}
        rows="3"
        placeholder="One per line: things they always or never do."
        aria-describedby="persona-builder-rules-status"
        aria-invalid={rulesError !== null}
      ></textarea>
      <div class="persona-builder__upload">
        <label class="field__help" for="persona-builder-rules-file">
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
            <p class="field__status field__status--error" role="alert">
              {rulesFileError}
            </p>
          {/if}
        </div>
      </div>
      <p class="field__help">Up to {RULES_MAX}, one per line.</p>
      <div id="persona-builder-rules-status">
        {#if rulesError}
          <p class="field__status field__status--error" role="alert">
            {rulesError}
          </p>
        {/if}
      </div>
    </div>

    <div class="modal__section">
      <p class="field__help">
        No persona yet? Describe the job and Kobo drafts one.
      </p>
      <div class="field">
        <label class="field__label" for="persona-builder-job">
          What should this agent do for you?
        </label>
        <textarea
          id="persona-builder-job"
          class="field__input"
          bind:value={job}
          rows="3"
          aria-describedby="persona-builder-job-status"
          aria-invalid={jobError !== null}
        ></textarea>
        <p class="field__help">A sentence or two, plain.</p>
        <div id="persona-builder-job-status">
          {#if jobError}
            <p class="field__status field__status--error" role="alert">
              {jobError}
            </p>
          {/if}
        </div>
      </div>
    </div>

    <!-- The builder's own footer. It is swapped in for the persona list
         INSIDE the create dialog's scrolling body, which is why it is
         sticky here rather than handed to the shell: while it is up, the
         dialog's own footer is not, so this is the one action row and
         Continue is the one filled button. -->
    <div class="modal__footer modal__footer--inline">
      <button type="button" class="btn btn--quiet" onclick={startFromOwnText}>
        Start with a blank form
      </button>
      <div class="modal__actions">
        <button type="button" class="btn btn--quiet" onclick={oncancel}>Cancel</button>
        <button type="submit" class="btn" disabled={!canDraft}>
          {draftMutation.isPending ? "Drafting…" : "Draft persona"}
        </button>
        <button
          type="button"
          class="btn btn--primary"
          disabled={whatTheyDo.trim().length === 0}
          onclick={handleContinueClick}
        >
          Continue
        </button>
      </div>
    </div>
  </form>
{:else}
  <form class="modal__form" onsubmit={handleCreateSubmit}>
    <p class="field__help">
      {draftSucceeded
        ? "Here's their draft. Edit anything."
        : "Fill in anything that's missing, then create."}
    </p>

    {#if draftError}
      <p class="field__status field__status--error" role="alert">{draftError}</p>
    {/if}

    {#if distillMutation.isPending}
      <p class="field__status">Reading their persona…</p>
    {:else if distillMutation.isError}
      <p class="field__status">Could not read the persona. Fill these in yourself.</p>
    {/if}

    <div class="field">
      <label class="field__label" for="persona-builder-name">Name</label>
      <input
        id="persona-builder-name"
        class="field__input"
        type="text"
        maxlength="60"
        bind:value={name}
        autocomplete="off"
        aria-describedby="persona-builder-name-hint"
      />
      <p class="field__help">
        A name is a name: one or two words, the shape of a craftsman's
        nickname, like "sentinel" or "ledger-keeper". Never a hyphenated
        list of traits; the description line below is where the traits
        live, so the name can just be remembered.
      </p>
      <div id="persona-builder-name-hint">
        {#if nameHint}
          <p class="field__help">{nameHint}</p>
        {/if}
      </div>
    </div>

    <div class="field">
      <label class="field__label" for="persona-builder-who-they-are">
        Who they are
      </label>
      <input
        id="persona-builder-who-they-are"
        class="field__input"
        type="text"
        maxlength="200"
        bind:value={whoTheyAre}
        placeholder="an unflappable network detective who's seen every outage twice"
        autocomplete="off"
      />
      <p class="field__help">
        One vivid line: who is this agent?{#if whoTheyAre.length > 170}
          {" "}({200 - whoTheyAre.length} characters left){/if}
      </p>
    </div>

    <div class="field">
      <label class="field__label" for="persona-builder-what-they-do">
        What they do
      </label>
      <textarea
        id="persona-builder-what-they-do"
        class="field__input"
        bind:value={whatTheyDo}
        rows="3"
      ></textarea>
      <div class="persona-builder__upload">
        <label class="field__help" for="persona-builder-what-they-do-file">
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
            <p class="field__status field__status--error" role="alert">
              {whatTheyDoFileError}
            </p>
          {/if}
        </div>
      </div>
      <p class="field__help">2-4 sentences, plain prose.</p>
    </div>

    <div class="field">
      <label class="field__label" for="persona-builder-rules-preview">
        Rules (optional)
      </label>
      <textarea
        id="persona-builder-rules-preview"
        class="field__input"
        bind:value={rulesInput}
        rows="3"
        placeholder="One per line: things they always or never do."
        aria-describedby="persona-builder-rules-preview-status"
        aria-invalid={rulesError !== null}
      ></textarea>
      <div class="persona-builder__upload">
        <label class="field__help" for="persona-builder-rules-preview-file">
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
            <p class="field__status field__status--error" role="alert">
              {rulesFileError}
            </p>
          {/if}
        </div>
      </div>
      <p class="field__help">Up to {RULES_MAX}, one per line.</p>
      <div id="persona-builder-rules-preview-status">
        {#if rulesError}
          <p class="field__status field__status--error" role="alert">
            {rulesError}
          </p>
        {/if}
      </div>
    </div>

    <div class="field">
      <label class="field__label" for="persona-builder-team">
        Who is this persona for?
      </label>
      <select
        id="persona-builder-team"
        class="field__input"
        value={selectedTeam}
        onchange={(event) =>
          (teamOverride = (event.currentTarget as HTMLSelectElement).value)}
      >
        {#each teamOptions as team (team)}
          <option value={team}>{team}</option>
        {/each}
        <option value={SHARED_TEAM}>Everyone (shared)</option>
      </select>
      <p class="field__help">
        People in this group see it in their persona list. Anyone can still
        find it with Show all groups.
      </p>
    </div>

    {#if submitError}
      <p class="field__status field__status--error" role="alert">{submitError}</p>
    {/if}

    <div class="modal__footer modal__footer--inline">
      <button type="button" class="btn btn--quiet" onclick={startOver}>
        Start over
      </button>
      <div class="modal__actions">
        <button type="button" class="btn btn--quiet" onclick={oncancel}>Cancel</button>
        <button type="submit" class="btn btn--primary" disabled={!canCreate}>
          {mutation.isPending ? "Creating…" : "Create persona"}
        </button>
      </div>
    </div>
  </form>
{/if}

<style>
  /* A file input and the quiet label that names it, on one line. */
  .persona-builder__upload {
    display: flex;
    align-items: center;
    gap: var(--space-xs);
    font-size: 0.8rem;
  }
</style>
