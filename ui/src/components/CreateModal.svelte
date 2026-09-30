<script lang="ts">
  // The one-click moment, as one plain form (plain-form wave, 2026-09-23).
  //
  // What it replaced: three "stations" under a stepper line, a persona list
  // with a "Start from scratch" row wedged into the top of it, and a naming
  // station that asked a generator for three names and offered more on
  // request. An operator's reading of that dialog is the whole brief for
  // this one: "Start from scratch does not look good; the personas under it
  // expand the full length of the modal and the spacing is strange; don't
  // generate names on the fly, the same names always populate and they are
  // not custom to the persona, let them choose their own agent name; make
  // this section more intuitive, simple and easy to understand fully at a
  // glance."
  //
  // So: one column, no stepper, nothing gated behind anything else, and
  // everything visible without scrolling on a laptop --
  //   1. Persona: a search box and a list that scrolls inside a fixed box
  //      about five rows deep, so the catalog cannot set the height of the
  //      dialog. "Build your own persona" is a quiet link on the label row,
  //      not a row in the list; it still swaps `PersonaBuilder` in over the
  //      whole form, exactly as before.
  //   2. Name: a text input. The person types it. The suggestion cards, the
  //      "Offer more" round-tripping and the exclude-set bookkeeping that
  //      went with them are gone -- the route itself (`nameSuggestionsQuery`
  //      / `POST /api/agents/name-suggestions`) is untouched, this dialog
  //      just stopped calling it.
  //   3. Group, 4. Model: the same two selects as before, sharing one line
  //      so five fields still fit a 70vh body on a laptop without the
  //      dialog scrolling. Group takes the whole width on its own in a room
  //      with no configured groups.
  //   5. Purpose tags: folded away behind a disclosure, because most agents
  //      are created without any.
  //
  // Name availability is still the one field with a network round trip
  // attached: every keystroke gets instant client-side validation
  // (validate.ts) and only once that passes does a debounced (350ms)
  // `checkAvailability` call go out, so a name that's obviously wrong never
  // costs a request.
  //
  // Persona is a catalog *search*, not a whole catalog fetched once:
  // `personasSearchQuery` re-runs as the user types, debounced 250ms via the
  // same `debounced` helper the name field uses.
  //
  // Models come from `modelsQuery()`, prefetched by App.svelte the same
  // "warm modal" way. Org wiring status (`orgStatusQuery()`) decides whether
  // the persona section shows a real picker or an honest "not set up yet"
  // empty state -- see `personasUnwired` below.
  //
  // Team is not asked here at all (the "team vs group" ruling): the create
  // REQUEST simply doesn't send it, the server's existing default ("shared")
  // covers its absence, and it stays editable after the fact through Edit
  // details. Skills are picked post-create, in the Skills tab. No capability
  // field either -- that's earned after first contact.
  import { tick } from "svelte";
  import { createMutation, createQuery, useQueryClient } from "@tanstack/svelte-query";
  import EmptyState from "./EmptyState.svelte";
  import ErrorState from "./ErrorState.svelte";
  import Modal from "./Modal.svelte";
  import PersonaBuilder from "./PersonaBuilder.svelte";
  import PersonaHoverCard from "./PersonaHoverCard.svelte";
  import Skeleton from "./Skeleton.svelte";
  import {
    ApiError,
    checkAvailability,
    errorMessage,
    type CatalogEntry,
    type PersonaScope,
  } from "../lib/api";
  import { debounced } from "../lib/debounce";
  import {
    createAgentMutation,
    groupsQuery,
    modelsQuery,
    orgStatusQuery,
    personasSearchQuery,
    requestGroupMutation,
  } from "../lib/queries";
  import { toast } from "../lib/toast.svelte";
  import { validateName } from "../lib/validate";

  let { open, onclose }: { open: boolean; onclose: () => void } = $props();

  const queryClient = useQueryClient();
  const models = createQuery(() => (modelsQuery()));
  const groups = createQuery(() => (groupsQuery()));
  const orgStatus = createQuery(() => (orgStatusQuery()));
  const mutation = createMutation(() => (createAgentMutation(queryClient)));

  let name = $state("");
  // `null` means "no explicit choice yet" — the effective value falls back
  // to the catalog's default model, below (persona has no such fallback: a
  // search-driven list has no stable "first" item to default to, so a
  // persona must be explicitly picked).
  let personaOverride = $state<string | null>(null);
  let modelOverride = $state<string | null>(null);
  let submitError = $state<string | null>(null);

  const selectedPersona = $derived(personaOverride ?? "");
  const selectedModel = $derived(modelOverride ?? models.data?.default ?? "");

  // --- Name: client-side check, only rendered once there's something to
  // say about it (an empty field isn't "invalid", it's just empty) --------

  const nameError = $derived(name.length > 0 ? validateName(name) : null);
  const nameLocallyValid = $derived(name.length > 0 && nameError === null);

  type Availability =
    | { status: "idle" }
    | { status: "checking" }
    | { status: "available" }
    | { status: "unavailable"; reason: string };

  let availability = $state<Availability>({ status: "idle" });

  const checkName = debounced((value: string) => {
    checkAvailability(value)
      .then((result) => {
        if (value !== name) {
          return; // stale response — the field has moved on since
        }
        availability = result.available
          ? { status: "available" }
          : { status: "unavailable", reason: result.reason ?? "Taken" };
      })
      .catch(() => {
        if (value !== name) {
          return;
        }
        availability = {
          status: "unavailable",
          reason: "Couldn't check availability. Try again.",
        };
      });
  }, 350);

  // Drives the availability check off `name`, gated by local validity and
  // by the modal being open. The cleanup cancels the in-flight debounce
  // whenever this reruns — on every keystroke (harmless: `debounced`
  // already collapses those) and, more importantly, when the modal closes,
  // so a check started just before close can never resolve into a late
  // "available" flash on a field the user has already walked away from.
  $effect(() => {
    const value = name;
    if (!open || !nameLocallyValid) {
      availability = { status: "idle" };
      return;
    }
    availability = { status: "checking" };
    checkName(value);
    return () => {
      checkName.cancel();
    };
  });

  const nameSettled = $derived(nameLocallyValid && availability.status === "available");

  // --- Persona search / builder -------------------------------------------

  let personaQ = $state("");
  let personaSearchTerm = $state("");
  // Persona team scope: the list starts on the personas for this person's
  // own groups plus the shared ones ("mine"); the quiet toggle on the label
  // row widens it to every group ("all"). A filter for a shorter list, not
  // access control: each row still carries its team tag either way.
  let personaScope = $state<PersonaScope>("mine");
  const personas = createQuery(() =>
    personasSearchQuery(personaSearchTerm, null, personaScope),
  );

  // The API already sends built-ins first; these keep each group in the
  // order it arrived.
  const builtinPersonas = $derived((personas.data ?? []).filter((p) => p.builtin));
  const teamPersonas = $derived((personas.data ?? []).filter((p) => !p.builtin));

  const commitPersonaSearch = debounced((value: string) => {
    personaSearchTerm = value;
  }, 250);

  $effect(() => {
    const value = personaQ;
    if (!open) {
      return;
    }
    commitPersonaSearch(value);
    return () => {
      commitPersonaSearch.cancel();
    };
  });

  // --- The persona hover card ---------------------------------------------
  //
  // A row gives one truncated line, which is enough to recognise a persona
  // you already know and not enough to choose one you don't. Resting on a
  // row -- or tabbing onto its radio -- opens the whole persona beside the
  // list (PersonaHoverCard).
  //
  // The delay is the point: a pointer crossing the list on its way to the
  // name field would otherwise fire a request for every row it passed
  // over. Nothing is fetched until someone has actually stopped on one.
  const HOVER_DELAY_MS = 250;
  // Leaving the row does not close the card instantly: the card sits a few
  // pixels off the list, and crossing that gap to click "Open the full
  // persona" would otherwise dismiss the thing on the way. The card itself
  // cancels this the moment the pointer (or focus) arrives.
  const HOVER_GRACE_MS = 150;
  const HOVER_CARD_ID = "create-persona-card";

  let hoveredPersona = $state<string | null>(null);
  let hoveredRow = $state<HTMLElement | null>(null);
  let personaListEl = $state<HTMLElement | null>(null);

  /** Whether the persona box has rows below its bottom edge. Drives the fade
   * on the box's shell: a scrolling list that ends on a clean row edge looks
   * finished, and this room's four built-ins plus the group heading fill the
   * box to exactly that edge. Re-measured on scroll and whenever the rows
   * change (a search, the scope toggle, the catalog answering). */
  let personaMore = $state(false);
  function measurePersonaList(): void {
    const el = personaListEl;
    if (!el) {
      personaMore = false;
      return;
    }
    personaMore = el.scrollHeight - el.scrollTop - el.clientHeight > 1;
  }
  $effect(() => {
    // Read the rows so a change in them re-runs this, then measure after
    // the DOM has them.
    void builtinPersonas;
    void teamPersonas;
    void personaListEl;
    tick().then(measurePersonaList);
  });
  let showTimer: ReturnType<typeof setTimeout> | null = null;
  let hideTimer: ReturnType<typeof setTimeout> | null = null;

  /** The box the card is kept inside: the dialog's own scrolling body. */
  const cardBoundary = $derived(
    (personaListEl?.closest(".modal__body") as HTMLElement | null) ?? null,
  );

  function cancelHover(): void {
    if (showTimer !== null) {
      clearTimeout(showTimer);
      showTimer = null;
    }
    if (hideTimer !== null) {
      clearTimeout(hideTimer);
      hideTimer = null;
    }
  }

  function showCardAfterDelay(id: string, row: HTMLElement | null): void {
    cancelHover();
    showTimer = setTimeout(() => {
      showTimer = null;
      hoveredPersona = id;
      hoveredRow = row;
    }, HOVER_DELAY_MS);
  }

  /** Leaving the row or the radio: close, but not before the pointer has
   * had a moment to land on the card itself. */
  function releaseCard(): void {
    cancelHover();
    hideTimer = setTimeout(hideCard, HOVER_GRACE_MS);
  }

  function hideCard(): void {
    cancelHover();
    hoveredPersona = null;
    hoveredRow = null;
  }

  /** Escape takes the card first and the dialog second: while a card is up,
   * the keystroke is spent dismissing it and goes no further. */
  function handlePersonaKeydown(event: KeyboardEvent): void {
    if (event.key !== "Escape") {
      return;
    }
    const showing = hoveredPersona !== null;
    hideCard();
    if (showing) {
      event.stopPropagation();
    }
  }

  $effect(() => cancelHover);

  let builderOpen = $state(false);

  /** A builder-drafted persona arrives with the tags its draft step already
   * worked out, so those seed the chips editor and the disclosure opens to
   * show them -- otherwise they would be filed away behind a shut fold the
   * operator has no reason to open. A persona picked from search carries no
   * draft and so touches the tags not at all: switching persona must not
   * quietly wipe tags somebody typed by hand. */
  function handlePersonaCreated(persona: CatalogEntry, _draftedTags: string[]): void {
    personaOverride = persona.id;
    builderOpen = false;
    personaQ = "";
    personaSearchTerm = "";
    toast.success("Persona ready");
  }

  // --- Group (access-control Task B2, sectioned by the shared-group +
  // group-requests wave) -----------------------------------------------
  //
  // `groupsQuery` (`GET /api/catalog/groups`) is the ACCESS namespace this
  // agent belongs to -- DISTINCT from Team (the skills-catalog capability
  // axis, not asked here at all). The picker only renders at all once
  // `groups.data` has anything in it (`groupOptionsAvailable`) -- in
  // production that's ALWAYS true as of the shared-group wave (the commons
  // entry is always first, even in a room with no configured groups at
  // all), so this stays purely a defensive fallback for an empty/malformed
  // response, not a "room hasn't set up groups" branch any more. Once there
  // IS something to pick, picking one is required -- `canSubmit` below
  // gates on it, and the real enforcement is server-side
  // (`create_agent`'s own check against `cfg.groups`/`SHARED_GROUP`)
  // regardless of what this picker does.
  //
  // The select leads with the door out (group-menu-leads-with-the-door
  // ruling): "My group is not listed..." -- the escape hatch into the
  // mini-form below for a directory group `cfg.groups` doesn't know about
  // yet -- comes FIRST, right after the disabled "Choose a group"
  // placeholder and OUTSIDE any optgroup, so it's the first real choice a
  // person sees rather than something buried at the bottom. After it, the
  // select sections on `kind` (never on a hardcoded "shared" string,
  // matching the server's own "don't hardcode SHARED_GROUP's spelling
  // client-side" doc comment on `/api/catalog/groups`): a "Non-engineering
  // groups" optgroup next (create-modal non-engineering-wing ruling --
  // config-driven via `GroupChoice.kind` server-side, never a hardcoded
  // list of names here), then "Engineering groups", then a "Shared"
  // optgroup LAST (shared-last ruling, 2026-09-24: teams create under
  // their own group; the commons is not where the eye should land first).
  // Each section is omitted entirely
  // when it has no entries (same "don't render an empty optgroup" rule
  // already applied to every section).
  let groupValue = $state("");
  const groupOptionsAvailable = $derived((groups.data?.length ?? 0) > 0);
  const sharedGroupOptions = $derived(
    (groups.data ?? []).filter((g) => g.kind === "shared"),
  );
  const engineeringGroupOptions = $derived(
    (groups.data ?? []).filter((g) => g.kind === "engineering"),
  );
  const nonEngineeringGroupOptions = $derived(
    (groups.data ?? []).filter((g) => g.kind === "non-engineering"),
  );
  const selectedGroupKind = $derived(
    groups.data?.find((g) => g.name === groupValue)?.kind,
  );

  /** Sentinel `<option>` value for "My group is not listed..." -- never a
   * real group name (never sent as `group_name`; `canSubmit` below treats
   * it identically to no selection at all). */
  const GROUP_NOT_LISTED = "__group_not_listed__";
  const groupChosen = $derived(
    groupValue !== "" && groupValue !== GROUP_NOT_LISTED,
  );

  let groupRequestName = $state("");
  let groupRequestNote = $state("");
  let groupRequestError = $state<string | null>(null);
  /** True once a request has been filed successfully; drives the quiet
   * confirmation copy, which stays up even after `groupValue` itself is
   * reset back to "" (see `handleGroupRequestSubmit`'s `onSuccess`) --
   * cleared again the moment "My group is not listed..." is picked afresh
   * (`handleGroupSelect`), or by the modal's own "fresh form every open"
   * effect below. */
  let groupRequestConfirmed = $state(false);
  const requestGroup = createMutation(() => (requestGroupMutation()));

  function handleGroupSelect(value: string): void {
    groupValue = value;
    if (value === GROUP_NOT_LISTED) {
      groupRequestConfirmed = false;
      groupRequestError = null;
    }
  }

  function handleGroupRequestSubmit(): void {
    const requested = groupRequestName.trim();
    if (requested.length === 0 || requestGroup.isPending) {
      return;
    }
    groupRequestError = null;
    requestGroup.mutate(
      { groupName: requested, note: groupRequestNote.trim() || undefined },
      {
        onSuccess: () => {
          groupRequestConfirmed = true;
          groupValue = "";
          groupRequestName = "";
          groupRequestNote = "";
        },
        onError: (error: unknown) => {
          if (error instanceof ApiError && error.status === 409) {
            groupRequestError = "Already requested. A room admin will get to it.";
          } else {
            groupRequestError = errorMessage(
              error,
              "Couldn't send that request. Try again.",
            );
          }
        },
      },
    );
  }


  // --- Unwired / empty catalogs --------------------------------------------

  function checkFailed(checkId: string): boolean {
    return orgStatus.data?.checks.some((c) => c.id === checkId && !c.ok) ?? false;
  }

  // `undefined` (still loading, or the query hasn't resolved) never counts
  // as unwired on its own — that would flash the "not set up yet" empty
  // state on every open before org status has even loaded once. Only a
  // confirmed `wired === false`, or a confirmed failing check, does.
  const personasUnwired = $derived(
    orgStatus.data !== undefined &&
      (orgStatus.data.wired === false || checkFailed("personas-seeded")),
  );

  const UNWIRED_MESSAGE = "Catalogs aren't set up yet. Ask whoever runs your room.";

  // Fresh form every time the modal opens.
  $effect(() => {
    if (open) {
      name = "";
      personaOverride = null;
      modelOverride = null;
      submitError = null;
      personaQ = "";
      personaSearchTerm = "";
      personaScope = "mine";
      builderOpen = false;
      groupValue = "";
      groupRequestName = "";
      groupRequestNote = "";
      groupRequestError = null;
      groupRequestConfirmed = false;
      hideCard();
    }
  });

  const canSubmit = $derived(
    models.data !== undefined &&
      nameSettled &&
      !personasUnwired &&
      selectedPersona !== "" &&
      selectedModel !== "" &&
      (!groupOptionsAvailable || groupChosen) &&
      !mutation.isPending,
  );

  /** The one sentence above the footer, naming the FIRST thing still
   * missing and nothing else -- three things unanswered at once is still
   * one instruction, not three. `null` once the form is complete, and
   * while the catalogs are unwired (the empty state above already says
   * what's wrong, louder). */
  const missingStep = $derived(
    personasUnwired
      ? null
      : selectedPersona === ""
        ? "Pick a persona first."
        : !nameSettled
          ? "Give it a name."
          : groupOptionsAvailable && !groupChosen
            ? "Choose a group."
            : null,
  );

  /** Called by the form's own submit (Enter in a field) and by the footer's
   * Create button, which sits outside the form element the modal shell puts
   * in its body -- so it asks for the submit directly rather than relying on
   * a form association across that seam. */
  function handleSubmit(): void {
    if (!canSubmit) {
      return;
    }
    submitError = null;
    const createdName = name;
    mutation.mutate(
      {
        name: createdName,
        persona: selectedPersona,
        // Skills are added post-create, in the Skills tab -- the create
        // dialog no longer picks them.
        skills: [],
        model: selectedModel,
        group_name: groupOptionsAvailable ? groupValue : undefined,
      },
      {
        onSuccess: () => {
          toast.success("Agent created", `Assembling ${createdName}…`);
          onclose();
        },
        onError: (error: unknown) => {
          submitError = errorMessage(error, "Something went wrong. Try again.");
          if (error instanceof ApiError && error.status === 409) {
            // The debounced availability check said this name was free, but
            // the submit lost the race — someone else took it between the
            // check and now. Flip availability to match reality so the
            // "Available" line next to the field can't keep claiming it
            // right above an error that says otherwise.
            availability = { status: "unavailable", reason: "Taken" };
          }
        },
      },
    );
  }
</script>

{#snippet personaRow(persona: CatalogEntry)}
  <!-- The label IS the control here: it wraps the radio, so a
       click anywhere on the row picks that persona. The card's
       own keyboard route is the radio's focus/blur below, which
       is where a keyboard user actually lands. -->
  <!-- svelte-ignore a11y_no_noninteractive_element_interactions -->
  <label
    class="list__row"
    class:list__row--selected={persona.id === selectedPersona}
    onmouseenter={(event) =>
      showCardAfterDelay(persona.id, event.currentTarget)}
    onmouseleave={releaseCard}
    onkeydown={handlePersonaKeydown}
  >
    <input
      type="radio"
      name="persona"
      value={persona.id}
      checked={persona.id === selectedPersona}
      onchange={() => {
        personaOverride = persona.id;
        hideCard();
      }}
      onfocus={(event) =>
        showCardAfterDelay(
          persona.id,
          event.currentTarget.closest(".list__row"),
        )}
      onblur={releaseCard}
      aria-describedby={hoveredPersona === persona.id
        ? HOVER_CARD_ID
        : undefined}
    />
    <span class="list__name">{persona.name}</span>
    <!-- A built-in's heading already says Built-in; only a team
         persona carries a tag naming its team. -->
    {#if !persona.builtin}
      <span class="list__tag">{persona.team}</span>
    {/if}
    <!-- No `title`: the hover card is the answer now, and two
         tooltips over the same row fight each other. -->
    <span class="list__desc">
      {persona.description}
    </span>
  </label>
{/snippet}

<!-- The one filled button in this dialog, and the only one: the persona
     builder draws its own footer while it is swapped in (below), so the two
     are never on screen at once. -->
{#snippet createFooter()}
  <div class="modal__actions">
    <button type="button" class="btn btn--quiet" onclick={onclose}>Cancel</button>
    <button
      type="button"
      class="btn btn--primary"
      disabled={!canSubmit}
      onclick={handleSubmit}
    >
      {mutation.isPending ? "Creating…" : "Create agent"}
    </button>
  </div>
{/snippet}

<Modal {open} title="Create agent" {onclose} footer={builderOpen ? undefined : createFooter}>
  <form
    class="modal__form"
    onsubmit={(event) => {
      event.preventDefault();
      handleSubmit();
    }}
  >
    {#if personasUnwired}
      <EmptyState message={UNWIRED_MESSAGE} />
    {:else if builderOpen}
      <PersonaBuilder
        oncancel={() => (builderOpen = false)}
        oncreated={handlePersonaCreated}
        defaultTeam={groupChosen ? groupValue : null}
      />
    {:else}
      <!-- 1. Persona -->
      <div class="field">
        <div class="field__label-row">
          <span class="field__label" id="create-persona-label">Persona</span>
          <span class="field__label-actions">
            <button
              type="button"
              class="btn--link btn--link-quiet"
              aria-pressed={personaScope === "all"}
              onclick={() => (personaScope = personaScope === "all" ? "mine" : "all")}
            >
              {personaScope === "all" ? "Show mine" : "Show all groups"}
            </button>
            <button
              type="button"
              class="btn--link btn--link-strong"
              onclick={() => (builderOpen = true)}
            >
              Build your own persona
            </button>
          </span>
        </div>
        <input
          id="create-persona-search"
          class="field__input"
          type="text"
          bind:value={personaQ}
          placeholder="Search personas"
          aria-label="Search personas"
          autocomplete="off"
        />
        <!-- The shell only exists to carry the fade: a `::after` on the
             scrolling box itself would scroll away with the rows. -->
        <div class="list-scroll" class:list-scroll--more={personaMore}>
        <div
          class="list list--scroll"
          role="radiogroup"
          aria-labelledby="create-persona-label"
          bind:this={personaListEl}
          onscroll={measurePersonaList}
        >
          {#if personas.isPending}
            <div class="list__empty">
              <Skeleton rows={2} />
            </div>
          {:else if personas.isError}
            <div class="list__empty">
              <ErrorState
                message={errorMessage(personas.error, "Couldn't load personas.")}
                onretry={() => personas.refetch()}
              />
            </div>
          {:else if personas.data.length === 0}
            <p class="list__empty">No personas match.</p>
          {:else}
            <!-- Two groups (built-ins first ruling): the built-ins are the
                 main set and always lead; team personas sit under them.
                 A heading is a presentation row, never a radio, so arrow
                 keys move between personas and skip it. A group with
                 nothing matching drops its heading. The scope toggle only
                 ever changes the second group. -->
            {#if builtinPersonas.length > 0}
              <div class="list__group" role="presentation">Built-in</div>
              {#each builtinPersonas as persona (persona.id)}
                {@render personaRow(persona)}
              {/each}
            {/if}
            {#if teamPersonas.length > 0}
              <div class="list__group" role="presentation">
                {personaScope === "all" ? "From all groups" : "From your groups"}
              </div>
              {#each teamPersonas as persona (persona.id)}
                {@render personaRow(persona)}
              {/each}
            {/if}
          {/if}
        </div>
        </div>
        {#if hoveredPersona !== null}
          <PersonaHoverCard
            id={HOVER_CARD_ID}
            personaId={hoveredPersona}
            row={hoveredRow}
            boundary={cardBoundary}
            onkeep={cancelHover}
            onrelease={releaseCard}
          />
        {/if}
      </div>

      <!-- 2. Name -->
      <div class="field">
        <label class="field__label" for="create-name">Name</label>
        <input
          id="create-name"
          class="field__input"
          type="text"
          bind:value={name}
          placeholder="e.g. release-notes"
          autocomplete="off"
          spellcheck="false"
          aria-describedby="create-name-status"
          aria-invalid={nameError !== null || availability.status === "unavailable"}
        />
        <!-- The rule and the verdict on one line. A local error REPLACES the
             rule rather than sitting under it: the two say the same thing,
             and the error says it about what is actually in the field. -->
        <div class="field__help-row">
          <p
            class="field__help"
            class:field__help--error={nameError !== null}
            role={nameError ? "alert" : undefined}
          >
            {nameError ?? "Lowercase letters, numbers, and hyphens. 2 to 24 characters."}
          </p>
          <div
            id="create-name-status"
            role={availability.status === "unavailable"
              ? "alert"
              : availability.status === "checking" || availability.status === "available"
                ? "status"
                : undefined}
            aria-live={availability.status === "checking" ||
            availability.status === "available"
              ? "polite"
              : undefined}
          >
            {#if availability.status === "checking"}
              <p class="field__status">Checking…</p>
            {:else if availability.status === "available"}
              <p class="field__status field__status--ok">Available</p>
            {:else if availability.status === "unavailable"}
              <p class="field__status field__status--error">{availability.reason}</p>
            {/if}
          </div>
        </div>
      </div>

      <!-- 3. Group and 4. Model: two one-line answers, so they share a line.
           Stacked, the five fields ran past a 70vh body on a laptop and the
           dialog started scrolling for the sake of two selects. -->
      <div class="field-pair">
        {#if groupOptionsAvailable}
          <div class="field">
            <label class="field__label" for="create-group">Group</label>
            <select
              id="create-group"
              class="field__input"
              required
              value={groupValue}
              onchange={(event) =>
                handleGroupSelect((event.currentTarget as HTMLSelectElement).value)}
            >
              <option value="" disabled>Choose a group</option>
              <option value={GROUP_NOT_LISTED}>My group is not listed...</option>
              {#if nonEngineeringGroupOptions.length > 0}
                <optgroup label="Non-engineering groups">
                  {#each nonEngineeringGroupOptions as group (group.name)}
                    <option value={group.name}>{group.name}</option>
                  {/each}
                </optgroup>
              {/if}
              {#if engineeringGroupOptions.length > 0}
                <optgroup label="Engineering groups">
                  {#each engineeringGroupOptions as group (group.name)}
                    <option value={group.name}>{group.name}</option>
                  {/each}
                </optgroup>
              {/if}
              <!-- No Shared entry (2026-09-29): an agent belongs to a team.
                   The commons still exists for an operator to move an agent
                   into; it is not a place to create one. -->
            </select>
            <p class="field__help">Who can see and use this agent.</p>

            {#if groupRequestConfirmed}
              <p class="field__status field__status--ok" role="status">
                Requested. A room admin will add it, and you can create the
                agent once it is listed.
              </p>
            {:else if groupValue === GROUP_NOT_LISTED}
              <div class="create-form__request">
                <label class="field__label" for="group-request-name">
                  Your group's name
                </label>
                <input
                  id="group-request-name"
                  class="field__input"
                  type="text"
                  bind:value={groupRequestName}
                  autocomplete="off"
                />
                <label class="field__label" for="group-request-note">
                  Note (optional)
                </label>
                <input
                  id="group-request-note"
                  class="field__input"
                  type="text"
                  bind:value={groupRequestNote}
                  autocomplete="off"
                />
                {#if groupRequestError}
                  <p class="field__status field__status--error" role="alert">
                    {groupRequestError}
                  </p>
                {/if}
                <button
                  type="button"
                  class="btn"
                  disabled={groupRequestName.trim().length === 0 || requestGroup.isPending}
                  onclick={handleGroupRequestSubmit}
                >
                  {requestGroup.isPending ? "Requesting…" : "Request it"}
                </button>
              </div>
            {/if}
          </div>
        {/if}

        <div class="field">
          <label class="field__label" for="create-model">Model</label>
          {#if models.isPending}
            <Skeleton rows={1} />
          {:else if models.isError}
            <ErrorState
              message={errorMessage(models.error, "Couldn't load models.")}
              onretry={() => models.refetch()}
            />
          {:else}
            <select
              id="create-model"
              class="field__input"
              value={selectedModel}
              onchange={(event) =>
                (modelOverride = (event.currentTarget as HTMLSelectElement).value)}
            >
              {#each models.data.models ?? [] as model (model.id)}
                <option value={model.id}>{model.label}</option>
              {/each}
            </select>
          {/if}
        </div>
      </div>

      {#if submitError}
        <p class="field__status field__status--error" role="alert">
          {submitError}
        </p>
      {:else if missingStep}
        <p class="field__help">{missingStep}</p>
      {/if}
    {/if}
  </form>
</Modal>

<style>
  /* The "my group isn't listed" mini-form: the one thing in this dialog
     that is a form inside a field, so it says so with a rule around it. */
  .create-form__request {
    display: flex;
    flex-direction: column;
    align-items: flex-start;
    gap: var(--space-2xs);
    padding: var(--space-sm);
    border: 1px solid var(--border);
    border-radius: var(--radius-sm);
  }
</style>
