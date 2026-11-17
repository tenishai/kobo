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
  //   1. Persona: a search box, a row of group pills, and a list that
  //      scrolls inside a fixed box about five rows deep, so the catalog
  //      cannot set the height of the dialog. The pills sit above the list
  //      where they are seen without scrolling; one is always pressed
  //      (Built-in to begin with) and the list is that group's personas. A
  //      search puts the pills away and lists every match under its
  //      group's name. "Build your own persona" is a button on the label
  //      row, not a row in the list; it swaps `PersonaBuilder` in over the
  //      whole form.
  //   2. Name: a text input. The person types it. The suggestion cards, the
  //      "Offer more" round-tripping and the exclude-set bookkeeping that
  //      went with them are gone -- the route itself (`nameSuggestionsQuery`
  //      / `POST /api/agents/name-suggestions`) is untouched, this dialog
  //      just stopped calling it.
  //   3. Group, 4. Model: each the whole width, one under the other (the
  //      restyle of 2026-11-01 undid the shared line: the model list needs
  //      the room to say why a row is off, and squeezed into half a dialog
  //      it wrapped three deep). The body scrolls when it has to.
  //
  // The dialog is the room's shell (Modal.svelte) with the shared field
  // primitives inside it: a title with one sentence under it, a body that
  // scrolls, Cancel and Create agent on the right of the footer. Focus
  // lands on the persona search; Enter in Name creates when the form is
  // complete; Escape closes.
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
  import { Button } from "$lib/components/ui/button/index.js";
  import { Input } from "$lib/components/ui/input/index.js";
  import { Label } from "$lib/components/ui/label/index.js";
  import EmptyState from "./EmptyState.svelte";
  import ErrorState from "./ErrorState.svelte";
  import Modal from "./Modal.svelte";
  import ModelPicker from "./ModelPicker.svelte";
  import PersonaBuilder from "./PersonaBuilder.svelte";
  import PersonaSheet from "./PersonaSheet.svelte";
  import PersonaHoverCard from "./PersonaHoverCard.svelte";
  import Skeleton from "./Skeleton.svelte";
  import {
    ApiError,
    checkAvailability,
    errorMessage,
    type CatalogEntry,
  } from "../lib/api";
  import { debounced } from "../lib/debounce";
  import {
    createAgentMutation,
    groupsQuery,
    modelsQuery,
    orgStatusQuery,
    personasSearchQuery,
    requestGroupMutation,
    whoamiQuery,
  } from "../lib/queries";
  import { toast } from "../lib/toast.svelte";
  import { BUILTIN_GROUP, personaGroups, validateName } from "../lib/validate";

  let { open, onclose }: { open: boolean; onclose: () => void } = $props();

  const queryClient = useQueryClient();
  const models = createQuery(() => (modelsQuery()));
  const groups = createQuery(() => (groupsQuery()));
  const orgStatus = createQuery(() => (orgStatusQuery()));
  // Only to link the no-model sentence for a room admin; never a gate.
  const whoami = createQuery(() => (whoamiQuery()));
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

  // --- Models (kobo#390) ----------------------------------------------------
  // The list comes from the connected clouds when the room's model gateway
  // is on, and `ModelPicker` groups and greys it. With no model available
  // at all, New agent is disabled with one sentence naming the admin page.
  const gatewayOn = $derived(models.data?.gateway === true);
  const noModelAvailable = $derived(
    gatewayOn && !(models.data?.models ?? []).some((m) => m.available !== false),
  );
  const isAdmin = $derived(Boolean(whoami.data?.operator && whoami.data?.admin_panel));

  // --- Persona search / builder -------------------------------------------

  let personaQ = $state("");
  let personaSearchTerm = $state("");
  // One query over the whole catalog, drawn a group at a time
  // (2026-11-17). The first cut of this list (2026-09-29) led with the
  // built-ins and put the group pills under them, inside the scrolling
  // box: they were below the fold until someone scrolled, a search
  // narrowed the pills and still showed no row until one was pressed, and
  // a persona made a moment ago was selected but nowhere on screen. Its
  // maker took it for lost, made it again, and was told the name was
  // taken. So the pills are above the list, one is always pressed, a
  // search shows its matches outright, and a new persona's group is
  // pressed for it.
  const personas = createQuery(() => personasSearchQuery(personaSearchTerm, null, "all"));
  const searching = $derived(personaSearchTerm.trim() !== "");
  /** A persona made in this dialog, kept on the list until the catalog's
   * own refetch brings it back. */
  let justCreated = $state<CatalogEntry | null>(null);
  const allPersonas = $derived.by(() => {
    const rows = personas.data ?? [];
    const made = justCreated;
    if (made === null || searching || rows.some((p) => p.id === made.id)) {
      return rows;
    }
    return [...rows, made];
  });
  const personaGroupList = $derived(personaGroups(allPersonas, whoami.data?.groups ?? []));
  /** The pill somebody pressed; until then, the first group (Built-in). */
  let pillOverride = $state<string | null>(null);
  const activeGroup = $derived(
    personaGroupList.find((g) => g.key === pillOverride) ?? personaGroupList[0] ?? null,
  );
  const visibleGroups = $derived(
    searching ? personaGroupList : activeGroup ? [activeGroup] : [],
  );
  /** The picked persona's own row, so the form can still name it when the
   * list has moved on to another group or a search. */
  let selectedEntry = $state<CatalogEntry | null>(null);
  const selectedOffList = $derived(
    selectedEntry !== null &&
      selectedEntry.id === selectedPersona &&
      !personas.isPending &&
      !visibleGroups.some((g) => g.personas.some((p) => p.id === selectedPersona)),
  );

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
    void visibleGroups;
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
  /** The persona the builder is editing, when it was opened from a sheet. */
  let editingPersona = $state<string | null>(null);

  /** The persona being read in full, swapped in over the list the way the
   * builder is (personas-in-place wave, 2026-09-30). */
  let readingPersona = $state<string | null>(null);

  function readPersona(id: string): void {
    hideCard();
    readingPersona = id;
  }

  /** Edit is reached from the sheet and goes back to it, saved or not. */
  function editPersona(id: string): void {
    editingPersona = id;
    readingPersona = null;
    builderOpen = true;
  }

  function closeBuilder(): void {
    if (editingPersona !== null) {
      readingPersona = editingPersona;
      editingPersona = null;
    }
    builderOpen = false;
  }

  function handlePersonaSaved(): void {
    closeBuilder();
    toast.success("Persona saved");
  }

  /** An archived persona leaves the list; a selection of it goes with it. */
  function handlePersonaArchived(id: string): void {
    if (selectedPersona === id) {
      personaOverride = null;
      selectedEntry = null;
    }
    if (justCreated?.id === id) {
      justCreated = null;
    }
  }

  /** A new persona is picked for the agent, and shown: its group's pill is
   * pressed and the search is cleared, so the row is on screen with its
   * radio on. Its maker should never have to go looking for it. */
  function handlePersonaCreated(persona: CatalogEntry, _draftedTags: string[]): void {
    personaOverride = persona.id;
    selectedEntry = persona;
    justCreated = persona;
    pillOverride = persona.team;
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

  /** The group a new persona is offered first: the agent's Group when one
   * is chosen, else the group whose personas are on screen. The builder
   * only takes it when its maker may file there. */
  const builderTeam = $derived(
    groupChosen
      ? groupValue
      : activeGroup && activeGroup.key !== BUILTIN_GROUP
        ? activeGroup.key
        : null,
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
      pillOverride = null;
      justCreated = null;
      selectedEntry = null;
      builderOpen = false;
      editingPersona = null;
      readingPersona = null;
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
      !noModelAvailable &&
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

  /** Enter in the Name field creates the agent once the form is complete.
   * The form has two text fields and its submit button sits in the footer,
   * outside it, so the browser's own implicit submission never fires here;
   * this is the one key that stands in for it. */
  function handleNameKeydown(event: KeyboardEvent): void {
    if (event.key !== "Enter") {
      return;
    }
    event.preventDefault();
    handleSubmit();
  }

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
        selectedEntry = persona;
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
    <!-- The name in ink, then the one line about it, cut off rather than
         wrapped. The pressed pill, or the heading over the run in a
         search, already says whose it is, so the row carries no tag. -->
    <span class="list__name">{persona.name}</span>
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
    <Button type="button" variant="ghost" onclick={onclose}>Cancel</Button>
    <Button type="button" disabled={!canSubmit} onclick={handleSubmit}>
      {mutation.isPending ? "Creating…" : "Create agent"}
    </Button>
  </div>
{/snippet}

<!-- The builder and the sheet are swapped in for the whole form, not
     drawn inside it: the builder is a form of its own, and a form may not
     hold another. The title says which of the three is up. -->
<Modal
  {open}
  title={!builderOpen ? "Create agent" : editingPersona !== null ? "Edit persona" : "Build your own persona"}
  description={!builderOpen
    ? "Pick who it is, name it, and choose the group that may use it."
    : editingPersona !== null
      ? "Changes reach agents made from now on. An agent already made keeps the persona it was made with."
      : "A persona is who an agent is: what it is for, how it works, and the rules it keeps."}
  size="wide"
  {onclose}
  footer={builderOpen || readingPersona !== null ? undefined : createFooter}
>
  {#if personasUnwired}
    <EmptyState message={UNWIRED_MESSAGE} />
  {:else if readingPersona !== null}
    <PersonaSheet
      personaId={readingPersona}
      onback={() => (readingPersona = null)}
      onedit={editPersona}
      onarchived={handlePersonaArchived}
    />
  {:else if builderOpen}
    <PersonaBuilder
      oncancel={closeBuilder}
      oncreated={handlePersonaCreated}
      onsaved={handlePersonaSaved}
      defaultTeam={builderTeam}
      editId={editingPersona}
    />
  {:else}
  <form
    class="modal__form create-form"
    onsubmit={(event) => {
      event.preventDefault();
      handleSubmit();
    }}
  >
      <!-- 1. Persona -->
      <div class="field">
        <div class="field__label-row">
          <span class="field__label" id="create-persona-label">Persona</span>
          <Button
            type="button"
            variant="secondary"
            size="sm"
            onclick={() => (builderOpen = true)}
          >
            Build your own persona
          </Button>
        </div>
        <Input
          id="create-persona-search"
          type="text"
          bind:value={personaQ}
          placeholder="Search personas"
          aria-label="Search personas"
          autocomplete="off"
          data-autofocus
        />
        <!-- One pill a group, one always pressed. Above the list and
             outside its scroll, so every group is in view the moment the
             dialog opens. A search lists every match and has no use for
             them. -->
        {#if !searching && personaGroupList.length > 1}
          <div class="chip-row" role="group" aria-label="Persona groups">
            {#each personaGroupList as group (group.key)}
              <button
                type="button"
                class="chip chip--action"
                aria-pressed={group.key === activeGroup?.key}
                onclick={() => (pillOverride = group.key)}
              >
                {group.label}
              </button>
            {/each}
          </div>
        {/if}
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
          {:else if visibleGroups.length === 0}
            <p class="list__empty">{searching ? "No personas match." : "No personas yet."}</p>
          {:else}
            <!-- The pressed group's personas, or in a search every match
                 under its group's name. A heading is a presentation row,
                 never a radio, so arrow keys move between personas and
                 skip it. -->
            {#each visibleGroups as group (group.key)}
              {#if searching}
                <div class="list__group" role="presentation">{group.label}</div>
              {/if}
              {#each group.personas as persona (persona.id)}
                {@render personaRow(persona)}
              {/each}
            {/each}
          {/if}
        </div>
        </div>
        {#if selectedOffList && selectedEntry}
          <p class="field__help">
            Selected: <span class="create-form__selected">{selectedEntry.name}</span>
          </p>
        {/if}
        {#if hoveredPersona !== null}
          <PersonaHoverCard
            id={HOVER_CARD_ID}
            personaId={hoveredPersona}
            row={hoveredRow}
            boundary={cardBoundary}
            onkeep={cancelHover}
            onrelease={releaseCard}
            onopen={readPersona}
          />
        {/if}
      </div>

      <!-- 2. Name -->
      <div class="field">
        <Label class="field__label" for="create-name">Name</Label>
        <Input
          id="create-name"
          type="text"
          bind:value={name}
          placeholder="e.g. release-notes"
          autocomplete="off"
          spellcheck="false"
          aria-describedby="create-name-status"
          aria-invalid={nameError !== null || availability.status === "unavailable"}
          onkeydown={handleNameKeydown}
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

      <!-- 3. Group -->
      {#if groupOptionsAvailable}
        <div class="field">
          <Label class="field__label" for="create-group">Group</Label>
          <select
            id="create-group"
            class="create-form__select"
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
              <Label class="field__label" for="group-request-name">
                Your group's name
              </Label>
              <Input
                id="group-request-name"
                type="text"
                bind:value={groupRequestName}
                autocomplete="off"
              />
              <Label class="field__label" for="group-request-note">
                Note (optional)
              </Label>
              <Input
                id="group-request-note"
                type="text"
                bind:value={groupRequestNote}
                autocomplete="off"
              />
              {#if groupRequestError}
                <p class="field__status field__status--error" role="alert">
                  {groupRequestError}
                </p>
              {/if}
              <Button
                type="button"
                variant="outline"
                size="sm"
                disabled={groupRequestName.trim().length === 0 || requestGroup.isPending}
                onclick={handleGroupRequestSubmit}
              >
                {requestGroup.isPending ? "Requesting…" : "Request it"}
              </Button>
            </div>
          {/if}
        </div>
      {/if}

      <!-- 4. Model -->
      <div class="field">
        <span class="field__label" id="create-model-label">Model</span>
        {#if models.isPending}
          <Skeleton rows={1} />
        {:else if models.isError}
          <ErrorState
            message={errorMessage(models.error, "Couldn't load models.")}
            onretry={() => models.refetch()}
          />
        {:else}
          <ModelPicker
            models={models.data.models ?? []}
            value={selectedModel}
            name="create-model"
            onchange={(id) => (modelOverride = id)}
          />
          <!-- The gate sits under the list it is about. Only a room admin
               gets the link; the sentence is the same for everyone. -->
          {#if noModelAvailable}
            <p class="field__help field__help--warn" role="alert">
              {#if isAdmin}
                Connect a cloud with models under <a href="#/admin/integrations">Admin, Integrations</a>, before creating an agent.
              {:else}
                Connect a cloud with models under Admin, Integrations, before creating an agent.
              {/if}
            </p>
          {/if}
        {/if}
      </div>

      {#if submitError}
        <p class="field__status field__status--error" role="alert">
          {submitError}
        </p>
      {:else if missingStep}
        <p class="field__help">{missingStep}</p>
      {/if}
  </form>
  {/if}
</Modal>

<style>
  /* The group select is the one native control left in this form; the
     room's shared control look dresses it, this only gives it the width
     the fields beside it have. */
  .create-form__select {
    width: 100%;
  }

  /* The picked persona's name, in ink beside its grey label, for when its
     row is not among the ones on screen. */
  .create-form__selected {
    color: var(--ink);
    font-weight: 500;
  }

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
