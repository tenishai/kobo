<script lang="ts">
  // The one-click moment, now staged as four stations at a bench (create
  // flow v2, "the naming ceremony" addendum; Persona v3 / teamless-create
  // addendum drops the Team station entirely and reorders what's left):
  // Persona -> The Naming -> Skills -> Create, each one unlocking once the
  // previous "commits". Fully controlled by the caller (`open` + `onclose`),
  // same contract as Modal itself — this component owns the form's own
  // state (name, selections, the availability check, submit errors) but
  // not whether it's visible.
  //
  // Persona v3 / teamless-create addendum first dropped Team as a
  // client-supplied field entirely; the persona-metadata plan (2026-08-18)
  // briefly brought it back as a real picker at station 4. The "team vs
  // group" ruling retires that picker for good: an operator asking "isn't
  // team and group basically the same thing?" surfaced that this room
  // already asks the one belonging question with Group (below). Team stays
  // what it reads as to a human — a derived marketplace fact (the
  // skills-catalog owner axis) — never a create-time choice. The create
  // REQUEST simply stops sending `team`; the server's existing default
  // ("shared") covers its absence exactly as it did before the picker ever
  // existed. Team is still editable after the fact through Edit details
  // (`MetadataDetails`, untouched by this ruling) — this modal just no
  // longer asks the question up front.
  //
  // "Unlocking" here is a visual/motivational cue only (reduced opacity +
  // a hollow station marker — see the `.station--locked` styling below),
  // never a hard functional lock: nothing is `disabled`/`inert` because a
  // later station is still muted. Persona leads the bench now (the one
  // station that's never itself muted — it has to stay usable from the
  // very first beat, the same role Team used to play before it was
  // removed). `canSubmit` below is the one thing that's actually gated —
  // unchanged from before this addendum, just fed by fields that now
  // render in a different order and without a team check.
  //
  // Name availability is the one field with a network round trip attached:
  // every keystroke gets instant client-side validation (validate.ts) and
  // only once that passes does a debounced (350ms) `checkAvailability` call
  // go out, so a name that's obviously wrong never costs a request. A name
  // can now arrive two ways — picking one of the naming station's five
  // suggested cards, or "name it yourself" — but either way it lands in
  // the same `name` state and runs through the same check.
  //
  // Persona and skills are catalog *searches* now (Task 10), not a whole
  // catalog fetched once: `personasSearchQuery`/`skillsSearchQuery` re-run
  // as the user types, each debounced 250ms via the same `debounced` helper
  // the name field uses. Because @tanstack/svelte-query's installed adapter
  // (5.90.2) only re-derives a query's options from an actual Svelte store
  // (see MemoryPanel/SlackPanel's delivery-chip doc comments for the same
  // constraint), the debounced search term for each section lives in a
  // plain `writable` store, fed by an `$effect` off the `$state` the input
  // is bound to — not a `$derived` object, which the adapter would freeze
  // at first render and never look at again. The naming station's
  // suggestion fetch (`nameSuggestionsQuery`) uses the exact same
  // writable-store idiom, just set once per persona commit rather than
  // per keystroke.
  //
  // Models come from `modelsQuery()`, prefetched by App.svelte the same
  // "warm modal" way the old whole-catalog fetch was. `teamsQuery()` (also
  // prefetched there) now feeds only the Skills section's search-by-team
  // filter — the "team vs group" ruling retired this station's own team
  // picker, so `teamOptionsQuery()` (`/api/teams`, the distinct skill-owner
  // pool + a server-named default) has no reader in this component any
  // more; it still backs Edit details' own team field (`MetadataDetails`).
  // Org wiring status (`orgStatusQuery()`) decides whether the persona/
  // skills sections show real pickers or an honest "not set up yet" empty
  // state — see `personasUnwired`/`skillsUnwired` below.
  //
  // Purpose chips are a plain local list, pre-filled once from the persona
  // draft step's own `purpose_tags` (`PersonaBuilder`'s `oncreated`) when
  // this agent's persona was actually drafted here; a search-picked existing
  // persona has no draft, so its chips start empty. A row of example chips
  // (below, in the markup) adds a starter tag on click through the same
  // list. No capability field at create — that's earned after first contact
  // (Overview/Edit details).
  import { createMutation, createQuery, useQueryClient } from "@tanstack/svelte-query";
  import { tick } from "svelte";
  import { derived, writable } from "svelte/store";
  import EmptyState from "./EmptyState.svelte";
  import ErrorState from "./ErrorState.svelte";
  import Modal from "./Modal.svelte";
  import PersonaBuilder from "./PersonaBuilder.svelte";
  import Skeleton from "./Skeleton.svelte";
  import {
    ApiError,
    checkAvailability,
    errorMessage,
    type CatalogEntry,
    type NameSuggestion,
  } from "../lib/api";
  import { debounced } from "../lib/debounce";
  import {
    createAgentMutation,
    groupsQuery,
    modelsQuery,
    nameSuggestionsQuery,
    orgStatusQuery,
    personasSearchQuery,
    requestGroupMutation,
    skillsSearchQuery,
    teamsQuery,
  } from "../lib/queries";
  import { toast } from "../lib/toast.svelte";
  import { validateName } from "../lib/validate";

  let { open, onclose }: { open: boolean; onclose: () => void } = $props();

  const queryClient = useQueryClient();
  const models = createQuery(modelsQuery());
  const teams = createQuery(teamsQuery());
  const groups = createQuery(groupsQuery());
  const orgStatus = createQuery(orgStatusQuery());
  const mutation = createMutation(createAgentMutation(queryClient));

  let name = $state("");
  // `null` means "no explicit choice yet" — the effective value falls back
  // to the catalog's default model, below (persona has no such fallback
  // any more: a search-driven list has no stable "first" item to default
  // to, so a persona must be explicitly picked).
  let personaOverride = $state<string | null>(null);
  let modelOverride = $state<string | null>(null);
  let submitError = $state<string | null>(null);

  const selectedPersona = $derived(personaOverride ?? "");
  const selectedModel = $derived(modelOverride ?? $models.data?.default ?? "");

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
          : { status: "unavailable", reason: result.reason ?? "Not available." };
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

  // --- Persona search / builder -------------------------------------------

  let personaQ = $state("");
  const personaSearchTerm = writable("");
  const personaSearchOptions = derived(personaSearchTerm, ($q) =>
    personasSearchQuery($q, null),
  );
  const personas = createQuery(personaSearchOptions);

  const commitPersonaSearch = debounced((value: string) => {
    personaSearchTerm.set(value);
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

  let builderOpen = $state(false);

  /** The committed persona's own text, kept alongside its id purely to seed
   * the naming station below (`description` doubles as `who_they_are`
   * verbatim — see `kobo_api.personas` — for every persona this room knows
   * about, catalog-shipped or user-authored). Set at the exact moment a
   * persona is picked (both branches below), never re-derived later by
   * looking the id back up in `$personas.data`: that list is itself a
   * debounced search result that can lag a beat behind a just-created
   * persona (createPersonaMutation's own cache invalidation hasn't
   * necessarily resolved yet), so re-deriving would risk racing it.
   *
   * `purposeTags` (persona-metadata plan) rides alongside for the same
   * reason: the moment a persona commits is also the moment the Create
   * station's chips editor should (re-)seed, so it travels in the same
   * state update rather than a second, separately-timed effect. */
  let selectedPersonaMeta = $state<
    { name: string; description: string; purposeTags: string[] } | null
  >(null);

  function selectPersona(persona: CatalogEntry, purposeTags: string[] = []): void {
    personaOverride = persona.id;
    selectedPersonaMeta = {
      name: persona.name,
      description: persona.description,
      purposeTags,
    };
  }

  function handlePersonaCreated(persona: CatalogEntry, purposeTags: string[]): void {
    selectPersona(persona, purposeTags);
    builderOpen = false;
    personaQ = "";
    personaSearchTerm.set("");
    toast.success("Persona ready");
  }

  // --- The Naming: five suggested cards once a persona is committed ------
  //
  // CatalogEntry (what both a search pick and a freshly-built persona hand
  // back) carries only `description`/`name` — no separate "what they do"
  // text survives into either shape. Disclosed call: `description` (==
  // `who_they_are`) becomes the generator's `who_they_are`, and the
  // persona's own `name` stands in for `what_they_do` — the best available
  // signal, not a literal match for the field name.

  let nameMode = $state<"cards" | "manual">("cards");
  let selectedCardName = $state<string | null>(null);
  // Set only by a "reroll" whose five names come back identical to the
  // five just shown — the fallback pool is deterministic per persona text,
  // so a generator outage can make that happen; copy must not promise
  // novelty it can't deliver (see `handleReroll`).
  let rerollNote = $state(false);
  let createNameInput: HTMLInputElement | undefined = $state();

  const nameSuggestionParams = writable<{ whoTheyAre: string; whatTheyDo: string }>({
    whoTheyAre: "",
    whatTheyDo: "",
  });
  const nameSuggestionsOptions = derived(nameSuggestionParams, ($p) =>
    nameSuggestionsQuery($p.whoTheyAre, $p.whatTheyDo),
  );
  const nameSuggestions = createQuery(nameSuggestionsOptions);

  // A persona commit — a first pick, a builder create, or picking a
  // *different* persona than before — feeds the naming query its who/what
  // text and, in the same beat, voids whatever the naming station had
  // going for the previous persona (or for no persona at all): a card pick
  // or a hand-typed name tied to one persona's suggestions has no business
  // surviving a switch to a different one. Purpose tags (persona-metadata
  // plan) re-seed here too, from the SAME commit's `purposeTags` — a
  // builder create carries its draft's tags, a search pick carries `[]`
  // (see `selectPersona`'s own default).
  $effect(() => {
    const meta = selectedPersonaMeta;
    if (!open) {
      return;
    }
    nameSuggestionParams.set({
      whoTheyAre: meta?.description ?? "",
      whatTheyDo: meta?.name ?? "",
    });
    nameMode = "cards";
    selectedCardName = null;
    name = "";
    rerollNote = false;
    purposeTags = meta?.purposeTags ?? [];
    purposeTagInput = "";
  });

  // A generator/route failure falls back to the classic input regardless of
  // which mode the user was in — the addendum's own error-state contract,
  // not something "name it yourself" alone should have to be clicked for.
  const showManualName = $derived(nameMode === "manual" || $nameSuggestions.isError);

  // Focus the name input when entering manual mode (a11y: keyboard/SR users
  // must land on the input, not dropped to body).
  $effect(() => {
    if (showManualName && nameMode === "manual" && createNameInput && open) {
      tick().then(() => createNameInput?.focus());
    }
  });

  function selectCard(card: NameSuggestion): void {
    selectedCardName = card.name;
    name = card.name;
  }

  /** "Offer more" (never "reroll" in the UI copy — the fallback pool is
   * deterministic per persona text, so promising novelty would sometimes be
   * a lie). Compares the five names before and after the refetch: if the
   * generator is down and the deterministic fallback hands back the exact
   * same five, `rerollNote` flips on so the station can say so plainly
   * instead of pretending something changed. Either way, the previous pick
   * doesn't survive a reroll — the offered set just changed out from under
   * it. */
  async function handleReroll(): Promise<void> {
    const before = ($nameSuggestions.data ?? []).map((s) => s.name);
    const result = await $nameSuggestions.refetch();
    const after = (result.data ?? []).map((s) => s.name);
    rerollNote =
      before.length > 0 &&
      after.length === before.length &&
      after.every((candidate, i) => candidate === before[i]);
    selectedCardName = null;
    name = "";
  }

  // --- Skills search -------------------------------------------------------

  let skillQ = $state("");
  // "" means "All teams" — a plain string (not `string | null`) because a
  // `<select>`'s DOM value is always a string; converted to `null` only at
  // the point the search params are committed, below.
  let skillTeamFilter = $state("");

  const skillSearchParams = writable<{ q: string; team: string | null }>({
    q: "",
    team: null,
  });
  const skillsSearchOptions = derived(skillSearchParams, ($params) =>
    skillsSearchQuery($params.q, $params.team),
  );
  const skills = createQuery(skillsSearchOptions);

  const commitSkillSearch = debounced(
    (value: { q: string; team: string | null }) => {
      skillSearchParams.set(value);
    },
    250,
  );

  // Caps the results list at 6 with a "show more" (create flow v2
  // addendum: "no wall of skills, ever") — reset to the capped view
  // whenever the search itself changes, so a fresh query never inherits an
  // earlier search's "show more" state.
  let skillShowAll = $state(false);

  $effect(() => {
    const value = { q: skillQ, team: skillTeamFilter === "" ? null : skillTeamFilter };
    skillShowAll = false;
    if (!open) {
      return;
    }
    commitSkillSearch(value);
    return () => {
      commitSkillSearch.cancel();
    };
  });

  const SKILLS_SHOWN_CAP = 6;

  const visibleSkills = $derived(
    $skills.data === undefined
      ? []
      : skillShowAll
        ? $skills.data
        : $skills.data.slice(0, SKILLS_SHOWN_CAP),
  );
  const skillsShowMoreCount = $derived(
    Math.max(0, ($skills.data?.length ?? 0) - SKILLS_SHOWN_CAP),
  );

  type SelectedSkill = { id: string; team: string; name: string };

  /** Keyed `team/id` (a skill id is only unique within its own team) so a
   * pick survives the visible results changing underneath it as the user
   * keeps searching. */
  let selectedSkills = $state<Map<string, SelectedSkill>>(new Map());

  function skillKey(id: string, skillTeam: string): string {
    return `${skillTeam}/${id}`;
  }

  function toggleSkill(entry: CatalogEntry): void {
    const key = skillKey(entry.id, entry.team);
    const next = new Map(selectedSkills);
    if (next.has(key)) {
      next.delete(key);
    } else {
      next.set(key, { id: entry.id, team: entry.team, name: entry.name });
    }
    selectedSkills = next;
  }

  function removeSkill(key: string): void {
    const next = new Map(selectedSkills);
    next.delete(key);
    selectedSkills = next;
  }

  // --- Group (access-control Task B2, sectioned by the shared-group +
  // group-requests wave) -----------------------------------------------
  //
  // `groupsQuery` (`GET /api/catalog/groups`) is the ACCESS namespace this
  // agent belongs to -- DISTINCT from Team above (the skills-catalog
  // capability axis). The picker only renders at all once `$groups.data`
  // has anything in it (`groupOptionsAvailable`) -- in production that's
  // ALWAYS true as of the shared-group wave (the commons entry is always
  // first, even in a room with no configured groups at all), so this stays
  // purely a defensive fallback for an empty/malformed response, not a
  // "room hasn't set up groups" branch any more. Once there IS something to
  // pick, picking one is required -- `canSubmit` below gates on it the same
  // way it gates on persona/model already being chosen, and the real
  // enforcement is server-side (`create_agent`'s own check against
  // `cfg.groups`/`SHARED_GROUP`) regardless of what this picker does.
  //
  // The select sections on `kind` (never on a hardcoded "shared" string,
  // matching the server's own "don't hardcode SHARED_GROUP's spelling
  // client-side" doc comment on `/api/catalog/groups`): a "Shared" optgroup
  // first, an "Engineering groups" optgroup next, then one final option
  // OUTSIDE both optgroups -- "My group is not listed..." -- the escape
  // hatch into the mini-form below for a directory group `cfg.groups`
  // doesn't know about yet.
  let groupValue = $state("");
  const groupOptionsAvailable = $derived(($groups.data?.length ?? 0) > 0);
  const sharedGroupOptions = $derived(
    ($groups.data ?? []).filter((g) => g.kind === "shared"),
  );
  const engineeringGroupOptions = $derived(
    ($groups.data ?? []).filter((g) => g.kind === "engineering"),
  );
  const selectedGroupKind = $derived(
    $groups.data?.find((g) => g.name === groupValue)?.kind,
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
  const requestGroup = createMutation(requestGroupMutation());

  function handleGroupSelect(value: string): void {
    groupValue = value;
    if (value === GROUP_NOT_LISTED) {
      groupRequestConfirmed = false;
      groupRequestError = null;
    }
  }

  function handleGroupRequestSubmit(): void {
    const name = groupRequestName.trim();
    if (name.length === 0 || $requestGroup.isPending) {
      return;
    }
    groupRequestError = null;
    $requestGroup.mutate(
      { groupName: name, note: groupRequestNote.trim() || undefined },
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

  // --- Purpose tags (persona-metadata plan, Create station) ---------------
  //
  // Free chips, pre-filled once per persona commit (see the effect above),
  // otherwise a plain add/remove list capped at 8 — same normalize-on-add
  // discipline `MetadataDetails`'s own chips editor uses. The example row
  // (markup, below) offers four starter tags that add themselves through
  // `addExampleTag`, the same normalize/cap-checked path just skinned as a
  // one-click chip instead of the type-and-press-Add flow.

  let purposeTags = $state<string[]>([]);
  let purposeTagInput = $state("");
  const PURPOSE_TAGS_CAP = 8;

  /** Picked to fit this room's own world (all four are names of real work
   * this room's agents actually do), not a generic SaaS example set. */
  const PURPOSE_TAG_EXAMPLES = ["networking", "incident-response", "gcp", "billing"];

  function addPurposeTag(): void {
    const value = purposeTagInput.trim().toLowerCase();
    if (value.length === 0) {
      return;
    }
    if (purposeTags.includes(value) || purposeTags.length >= PURPOSE_TAGS_CAP) {
      purposeTagInput = "";
      return;
    }
    purposeTags = [...purposeTags, value];
    purposeTagInput = "";
  }

  function addExampleTag(tag: string): void {
    if (purposeTags.includes(tag) || purposeTags.length >= PURPOSE_TAGS_CAP) {
      return;
    }
    purposeTags = [...purposeTags, tag];
  }

  function removePurposeTag(value: string): void {
    purposeTags = purposeTags.filter((t) => t !== value);
  }

  // --- Unwired / empty catalogs --------------------------------------------

  function checkFailed(checkId: string): boolean {
    return $orgStatus.data?.checks.some((c) => c.id === checkId && !c.ok) ?? false;
  }

  // `undefined` (still loading, or the query hasn't resolved) never counts
  // as unwired on its own — that would flash the "not set up yet" empty
  // state on every open before org status has even loaded once. Only a
  // confirmed `wired === false`, or a confirmed failing check, does.
  const personasUnwired = $derived(
    $orgStatus.data !== undefined &&
      ($orgStatus.data.wired === false || checkFailed("personas-seeded")),
  );
  const skillsUnwired = $derived(
    $orgStatus.data !== undefined &&
      ($orgStatus.data.wired === false || checkFailed("skills-index")),
  );

  const UNWIRED_MESSAGE = "Catalogs aren't set up yet. Ask whoever runs your room.";

  // --- Stations: which of the four read as active vs. muted --------------
  // Purely a visual cue (see the doc comment at the top of this file) —
  // none of these gate anything functionally; `canSubmit` below is the one
  // real gate, and it's unchanged.
  const personaCommitted = $derived(selectedPersona !== "");
  const namingCommitted = $derived(name.length > 0);

  // Fresh form every time the modal opens.
  $effect(() => {
    if (open) {
      name = "";
      personaOverride = null;
      modelOverride = null;
      submitError = null;
      personaQ = "";
      personaSearchTerm.set("");
      builderOpen = false;
      selectedPersonaMeta = null;
      nameMode = "cards";
      selectedCardName = null;
      rerollNote = false;
      nameSuggestionParams.set({ whoTheyAre: "", whatTheyDo: "" });
      skillQ = "";
      skillTeamFilter = "";
      skillSearchParams.set({ q: "", team: null });
      skillShowAll = false;
      selectedSkills = new Map();
      groupValue = "";
      groupRequestName = "";
      groupRequestNote = "";
      groupRequestError = null;
      groupRequestConfirmed = false;
      purposeTags = [];
      purposeTagInput = "";
    }
  });

  const canSubmit = $derived(
    $models.data !== undefined &&
      nameLocallyValid &&
      availability.status === "available" &&
      !personasUnwired &&
      !skillsUnwired &&
      selectedPersona !== "" &&
      selectedModel !== "" &&
      (!groupOptionsAvailable || groupChosen) &&
      !$mutation.isPending,
  );

  function handleSubmit(event: SubmitEvent): void {
    event.preventDefault();
    if (!canSubmit) {
      return;
    }
    submitError = null;
    const createdName = name;
    $mutation.mutate(
      {
        name: createdName,
        persona: selectedPersona,
        skills: [...selectedSkills.values()].map(({ id, team: skillTeam }) => ({
          id,
          team: skillTeam,
        })),
        model: selectedModel,
        purpose_tags: purposeTags,
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
            // check and now. Flip availability to match reality so the ✓
            // next to the field can't keep claiming "available" right above
            // an error that says otherwise.
            availability = { status: "unavailable", reason: "Name already taken." };
          }
        },
      },
    );
  }
</script>

{#snippet nameStatus()}
  <div
    id="create-name-status"
    role={nameError || availability.status === "unavailable"
      ? "alert"
      : availability.status === "checking" || availability.status === "available"
        ? "status"
        : undefined}
    aria-live={availability.status === "checking" ||
    availability.status === "available"
      ? "polite"
      : undefined}
  >
    {#if nameError}
      <p class="create-form__status create-form__status--error">{nameError}</p>
    {:else if availability.status === "checking"}
      <p class="create-form__status">Checking…</p>
    {:else if availability.status === "available"}
      <p class="create-form__status create-form__status--ok">✓ Available</p>
    {:else if availability.status === "unavailable"}
      <p class="create-form__status create-form__status--error">
        {availability.reason}
      </p>
    {/if}
  </div>
{/snippet}

<Modal {open} title="Create agent" {onclose}>
  <form class="create-form" onsubmit={handleSubmit}>
    <!-- Station 1: Persona — always active, never muted; it's the start of
         the bench now (Persona v3 / teamless-create addendum removed the
         Team station that used to lead it). -->
    <div class="station">
      <div class="station__head">
        <span class="station-marker station-marker--filled" aria-hidden="true">1</span>
        <h3 class="station__heading">Persona</h3>
      </div>

      <fieldset class="create-form__field create-form__fieldset">
        <legend class="sr-only">Persona</legend>
        {#if personasUnwired}
          <EmptyState message={UNWIRED_MESSAGE} />
        {:else if builderOpen}
          <PersonaBuilder
            oncancel={() => (builderOpen = false)}
            oncreated={handlePersonaCreated}
          />
        {:else}
          <button
            type="button"
            class="persona-build-cta"
            onclick={() => (builderOpen = true)}
          >
            Build your own persona
          </button>

          <div class="create-form__field">
            <label class="create-form__label" for="persona-search">Search personas</label>
            <input
              id="persona-search"
              class="create-form__input"
              type="text"
              bind:value={personaQ}
              placeholder="Search personas"
              autocomplete="off"
            />
          </div>

          {#if $personas.isPending}
            <Skeleton rows={2} />
          {:else if $personas.isError}
            <ErrorState
              message={errorMessage($personas.error, "Couldn't load personas.")}
              onretry={() => $personas.refetch()}
            />
          {:else}
            <div class="persona-list">
              {#if $personas.data.length === 0 && personaQ.length > 0}
                <p class="create-form__help">Nothing matches your search.</p>
              {/if}
              {#each $personas.data as persona (persona.id)}
                <label
                  class="persona-card"
                  class:persona-card--selected={persona.id === selectedPersona}
                >
                  <input
                    type="radio"
                    name="persona"
                    value={persona.id}
                    checked={persona.id === selectedPersona}
                    onchange={() => selectPersona(persona)}
                  />
                  <span class="persona-card__body">
                    <span class="persona-card__head">
                      <span class="persona-card__name">{persona.name}</span>
                      <span
                        class="catalog-badge"
                        class:catalog-badge--builtin={persona.builtin}
                      >
                        {persona.builtin ? "Built-in" : persona.team}
                      </span>
                    </span>
                    <span class="persona-card__description">{persona.description}</span>
                  </span>
                </label>
              {/each}
            </div>
          {/if}
        {/if}
      </fieldset>
    </div>

    <!-- Station 2: The Naming — muted until a persona is picked; there's
         nothing to suggest against before that. -->
    <div class="station" class:station--locked={!personaCommitted}>
      <div class="station__head">
        <span
          class="station-marker"
          class:station-marker--filled={personaCommitted}
          aria-hidden="true"
        >
          2
        </span>
        <h3 class="station__heading">The Naming</h3>
      </div>

      {#if !personaCommitted}
        <p class="station__hint">Pick a persona first.</p>
      {:else if showManualName}
        {#if $nameSuggestions.isError}
          <p class="create-form__help">
            Couldn't come up with name ideas right now. Name it yourself below.
          </p>
        {/if}
        <div class="create-form__field">
          <label class="create-form__label" for="create-name">Name</label>
          <input
            id="create-name"
            class="create-form__input"
            type="text"
            bind:value={name}
            bind:this={createNameInput}
            placeholder="e.g. weekly-digest"
            autocomplete="off"
            spellcheck="false"
            aria-describedby="create-name-status"
            aria-invalid={nameError !== null ||
              availability.status === "unavailable"}
          />
          <p class="create-form__help">
            Lowercase letters, numbers, and hyphens. 2-24 characters.
          </p>
          {@render nameStatus()}
          {#if nameMode === "manual" && $nameSuggestions.data !== undefined && !$nameSuggestions.isError}
            <button
              type="button"
              class="naming-actions__back"
              onclick={() => (nameMode = "cards")}
            >
              back to the suggestions
            </button>
          {/if}
        </div>
      {:else if $nameSuggestions.isPending}
        <div class="name-cards" aria-hidden="true">
          {#each { length: 5 } as _, i (i)}
            <div class="name-card name-card--skeleton">
              <span class="name-card__skeleton-bar name-card__skeleton-bar--name"
              ></span>
              <span class="name-card__skeleton-bar name-card__skeleton-bar--reason"
              ></span>
            </div>
          {/each}
        </div>
        <p class="sr-only" role="status">Loading name suggestions…</p>
      {:else}
        <div class="name-cards" role="radiogroup" aria-label="Suggested names">
          {#each $nameSuggestions.data as card (card.name)}
            <label
              class="name-card"
              class:name-card--selected={card.name === selectedCardName}
              class:name-card--dimmed={selectedCardName !== null &&
                card.name !== selectedCardName}
            >
              <input
                type="radio"
                name="suggested-name"
                value={card.name}
                checked={card.name === selectedCardName}
                onchange={() => selectCard(card)}
              />
              <span class="name-card__body">
                <span class="name-card__name">{card.name}</span>
                <span class="name-card__reason">{card.reason}</span>
              </span>
            </label>
          {/each}
        </div>

        {#if selectedCardName !== null}
          {@render nameStatus()}
        {/if}

        <div class="naming-actions">
          <button
            type="button"
            class="naming-actions__reroll"
            onclick={handleReroll}
            disabled={$nameSuggestions.isFetching}
          >
            {$nameSuggestions.isFetching ? "Asking again…" : "Offer more"}
          </button>
          <button
            type="button"
            class="naming-actions__manual"
            onclick={() => (nameMode = "manual")}
          >
            name it yourself
          </button>
        </div>
        {#if rerollNote}
          <p class="create-form__help">Same five as before. The workshop stands by them.</p>
        {/if}
      {/if}
    </div>

    <!-- Station 3: Skills — search-first, capped, muted until a name's
         landed (but always live — nothing about searching/picking skills
         actually needs one). -->
    <div class="station" class:station--locked={!namingCommitted}>
      <div class="station__head">
        <span
          class="station-marker"
          class:station-marker--filled={namingCommitted}
          aria-hidden="true"
        >
          3
        </span>
        <h3 class="station__heading">Skills</h3>
      </div>

      <fieldset class="create-form__field create-form__fieldset">
        <legend class="sr-only">Skills</legend>
        {#if skillsUnwired}
          <EmptyState message={UNWIRED_MESSAGE} />
        {:else}
          {#if selectedSkills.size > 0}
            <div class="chip-row">
              {#each [...selectedSkills.entries()] as [key, skill] (key)}
                <span class="chip">
                  {skill.name}
                  <button
                    type="button"
                    class="chip__remove"
                    aria-label={`Remove ${skill.name}`}
                    onclick={() => removeSkill(key)}
                  >
                    &times;
                  </button>
                </span>
              {/each}
            </div>
          {/if}

          <div class="create-form__field">
            <label class="create-form__label" for="skill-search">Search skills</label>
            <div class="catalog-search__row">
              <input
                id="skill-search"
                class="create-form__input"
                type="text"
                bind:value={skillQ}
                placeholder="Search skills"
                autocomplete="off"
              />
              <label class="sr-only" for="skill-team-filter">Filter skills by team</label>
              <select
                id="skill-team-filter"
                class="create-form__input"
                value={skillTeamFilter}
                onchange={(event) =>
                  (skillTeamFilter = (event.currentTarget as HTMLSelectElement).value)}
              >
                <option value="">All teams</option>
                {#if $teams.data}
                  {#each $teams.data as t (t)}
                    <option value={t}>{t}</option>
                  {/each}
                {/if}
              </select>
            </div>
          </div>

          {#if $skills.isPending}
            <Skeleton rows={2} />
          {:else if $skills.isError}
            <ErrorState
              message={errorMessage($skills.error, "Couldn't load skills.")}
              onretry={() => $skills.refetch()}
            />
          {:else if $skills.data.length === 0}
            <p class="create-form__help">
              {skillQ.length > 0 ? "Nothing matches your search." : "No skills in the catalog yet"}
            </p>
          {:else}
            <div class="skill-list">
              {#each visibleSkills as skill (skillKey(skill.id, skill.team))}
                <label class="skill-item">
                  <input
                    type="checkbox"
                    checked={selectedSkills.has(skillKey(skill.id, skill.team))}
                    onchange={() => toggleSkill(skill)}
                  />
                  <span class="skill-item__body">
                    <span class="skill-item__head">
                      <span class="skill-item__name">{skill.name}</span>
                      <span class="catalog-badge">{skill.team}</span>
                    </span>
                    <span class="skill-item__description">{skill.description}</span>
                  </span>
                </label>
              {/each}
            </div>
            {#if skillsShowMoreCount > 0}
              <button
                type="button"
                class="skills-show-more"
                onclick={() => (skillShowAll = true)}
              >
                Show {skillsShowMoreCount} more
              </button>
            {/if}
          {/if}
        {/if}
      </fieldset>
    </div>

    <!-- Station 4: Create — muted until a name's landed; Create itself
         stays gated by `canSubmit`, unchanged. -->
    <div class="station" class:station--locked={!namingCommitted}>
      <div class="station__head">
        <span
          class="station-marker"
          class:station-marker--filled={namingCommitted}
          aria-hidden="true"
        >
          4
        </span>
        <h3 class="station__heading">Create</h3>
      </div>

      {#if groupOptionsAvailable}
        <div class="create-form__field">
          <label class="create-form__label" for="create-group">Group</label>
          <select
            id="create-group"
            class="create-form__input"
            required
            value={groupValue}
            onchange={(event) =>
              handleGroupSelect((event.currentTarget as HTMLSelectElement).value)}
          >
            <option value="" disabled>Choose a group</option>
            {#if sharedGroupOptions.length > 0}
              <optgroup label="Shared">
                {#each sharedGroupOptions as group (group.name)}
                  <option value={group.name}>{group.name} (everyone in the company)</option>
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
            <option value={GROUP_NOT_LISTED}>My group is not listed...</option>
          </select>
          <p class="create-form__help">Who can see and use this agent.</p>
          {#if selectedGroupKind === "shared"}
            <p class="create-form__help">
              Shared agents are visible to everyone in the company.
            </p>
          {/if}

          {#if groupRequestConfirmed}
            <p class="create-form__status create-form__status--ok" role="status">
              Requested. A room admin will add it. Pick shared to create this
              agent now, or wait for your group.
            </p>
          {:else if groupValue === GROUP_NOT_LISTED}
            <div class="group-request">
              <label class="create-form__label" for="group-request-name">
                Your group's name
              </label>
              <input
                id="group-request-name"
                class="create-form__input"
                type="text"
                bind:value={groupRequestName}
                autocomplete="off"
              />
              <label class="create-form__label" for="group-request-note">
                Note (optional)
              </label>
              <input
                id="group-request-note"
                class="create-form__input"
                type="text"
                bind:value={groupRequestNote}
                autocomplete="off"
              />
              {#if groupRequestError}
                <p class="create-form__status create-form__status--error" role="alert">
                  {groupRequestError}
                </p>
              {/if}
              <button
                type="button"
                class="group-request__submit"
                disabled={groupRequestName.trim().length === 0 || $requestGroup.isPending}
                onclick={handleGroupRequestSubmit}
              >
                {$requestGroup.isPending ? "Requesting…" : "Request it"}
              </button>
            </div>
          {/if}
        </div>
      {/if}

      <div class="create-form__field">
        <label class="create-form__label" for="create-purpose-tag-input">Purpose tags</label>
        <p class="create-form__help">
          Short labels that help people and agents discover what this agent is for.
        </p>
        <div class="chip-row">
          {#each PURPOSE_TAG_EXAMPLES as example (example)}
            <button
              type="button"
              class="chip chip--example"
              disabled={purposeTags.includes(example) ||
                purposeTags.length >= PURPOSE_TAGS_CAP}
              onclick={() => addExampleTag(example)}
            >
              {example}
            </button>
          {/each}
        </div>
        {#if purposeTags.length > 0}
          <div class="chip-row">
            {#each purposeTags as tag (tag)}
              <span class="chip">
                {tag}
                <button
                  type="button"
                  class="chip__remove"
                  aria-label={`Remove ${tag}`}
                  onclick={() => removePurposeTag(tag)}
                >
                  &times;
                </button>
              </span>
            {/each}
          </div>
        {/if}
        <div class="catalog-search__row">
          <input
            id="create-purpose-tag-input"
            class="create-form__input"
            type="text"
            bind:value={purposeTagInput}
            placeholder="Add a tag"
            autocomplete="off"
            onkeydown={(event) => {
              if (event.key === "Enter") {
                event.preventDefault();
                addPurposeTag();
              }
            }}
          />
          <button
            type="button"
            class="skills-show-more"
            disabled={purposeTagInput.trim().length === 0 ||
              purposeTags.length >= PURPOSE_TAGS_CAP}
            onclick={addPurposeTag}
          >
            Add
          </button>
        </div>
        <p class="create-form__help">Up to 8 tags.</p>
      </div>

      <div class="create-form__field">
        <label class="create-form__label" for="create-model">Model</label>
        {#if $models.isPending}
          <Skeleton rows={1} />
        {:else if $models.isError}
          <ErrorState
            message={errorMessage($models.error, "Couldn't load models.")}
            onretry={() => $models.refetch()}
          />
        {:else}
          <select
            id="create-model"
            class="create-form__input"
            value={selectedModel}
            onchange={(event) =>
              (modelOverride = (event.currentTarget as HTMLSelectElement).value)}
          >
            {#each $models.data.models as model (model.id)}
              <option value={model.id}>{model.label}</option>
            {/each}
          </select>
        {/if}
      </div>

      {#if submitError}
        <p class="create-form__status create-form__status--error" role="alert">
          {submitError}
        </p>
      {/if}

      <div class="create-form__actions">
        <button type="button" class="create-form__cancel" onclick={onclose}>
          Cancel
        </button>
        <button type="submit" class="create-form__submit" disabled={!canSubmit}>
          {$mutation.isPending ? "Creating…" : "Create agent"}
        </button>
      </div>
    </div>
  </form>
</Modal>

<style>
  .create-form {
    display: flex;
    flex-direction: column;
    gap: var(--space-lg);
    /* Paper depth, not a flat card (addendum, "Visual direction"): a
       barely-there warm gradient plus a tiny tiled noise texture, both
       layered as plain background-images so they always paint behind
       every child — no pseudo-element/z-index bookkeeping needed. No
       padding of its own, so the tint fills exactly the space Modal.svelte
       already gives this slot (flush with its existing --space-lg inset)
       rather than reading as a second, nested card inside the dialog. */
    background-color: var(--bg);
    background-repeat: no-repeat, no-repeat, repeat;
    background-image:
      radial-gradient(
        140% 100% at 8% 0%,
        color-mix(in srgb, var(--accent) 6%, transparent) 0%,
        transparent 55%
      ),
      radial-gradient(
        120% 120% at 100% 100%,
        color-mix(in srgb, var(--accent) 4%, transparent) 0%,
        transparent 60%
      ),
      url("data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg'%3E%3Cfilter id='n'%3E%3CfeTurbulence type='fractalNoise' baseFrequency='0.85' numOctaves='2' stitchTiles='stitch'/%3E%3CfeColorMatrix type='saturate' values='0'/%3E%3C/filter%3E%3Crect width='100%25' height='100%25' filter='url(%23n)' opacity='0.05'/%3E%3C/svg%3E");
  }

  .create-form__field {
    display: flex;
    flex-direction: column;
    gap: var(--space-2xs);
  }

  .create-form__fieldset {
    margin: 0;
    padding: 0;
    border: none;
  }

  .create-form__label {
    padding: 0;
    color: var(--ink);
    font-weight: 600;
    font-size: 0.9rem;
  }

  .sr-only {
    position: absolute;
    width: 1px;
    height: 1px;
    overflow: hidden;
    clip: rect(0, 0, 0, 0);
    white-space: nowrap;
  }

  .create-form__input {
    padding: var(--space-xs) var(--space-sm);
    border: 1px solid var(--border);
    border-radius: var(--radius-sm);
    background: var(--panel);
    color: var(--ink);
    font: inherit;
  }

  .create-form__input:focus-visible {
    outline: 2px solid var(--accent);
    outline-offset: 1px;
  }

  .create-form__help {
    margin: 0;
    color: var(--muted);
    font-size: 0.8rem;
  }

  .create-form__status {
    margin: 0;
    color: var(--muted);
    font-size: 0.8rem;
  }

  /* Fixed floor so the idle state (no message rendered at all) reserves the
     same space a one-line status/error message would take — otherwise the
     field sits flush against the help text (a stray gap sliver) and then
     jumps down the moment a message appears. */
  #create-name-status {
    min-height: 1.25rem;
  }

  .create-form__status--ok {
    color: var(--ok);
  }

  .create-form__status--error {
    color: var(--fail);
  }

  /* --- Stations: numbered woodstamp markers + display-serif headings ---- */

  .station {
    display: flex;
    flex-direction: column;
    gap: var(--space-sm);
    transition: opacity 200ms ease-out;
  }

  .station--locked {
    opacity: 0.5;
  }

  .station__head {
    display: flex;
    align-items: center;
    gap: var(--space-xs);
  }

  .station-marker {
    display: inline-flex;
    flex-shrink: 0;
    align-items: center;
    justify-content: center;
    width: 1.6rem;
    height: 1.6rem;
    border: 1.5px solid var(--border);
    border-radius: 50%;
    color: var(--muted);
    font-family: var(--font-mono);
    font-size: 0.8rem;
    font-weight: 600;
  }

  .station-marker--filled {
    border-color: var(--accent);
    background: var(--accent);
    color: var(--accent-ink);
  }

  /* The one place the display serif appears (addendum, "Visual
     direction") — every other heading/label in this room keeps
     --font-sans/--font-mono. */
  .station__heading {
    margin: 0;
    color: var(--ink);
    font-family: var(--font-display);
    font-weight: 600;
    font-size: 1.15rem;
  }

  .station__hint {
    margin: 0;
    color: var(--muted);
    font-size: 0.85rem;
  }

  @media (prefers-reduced-motion: reduce) {
    .station {
      transition: none;
    }
  }

  .group-request {
    display: flex;
    flex-direction: column;
    gap: var(--space-2xs);
    padding: var(--space-sm);
    border: 1px solid var(--border);
    border-radius: var(--radius-sm);
    background: var(--panel);
  }

  .group-request__submit {
    align-self: flex-start;
    padding: var(--space-2xs) var(--space-sm);
    border: none;
    border-radius: var(--radius-sm);
    background: var(--accent);
    color: var(--accent-ink);
    font-weight: 600;
    cursor: pointer;
  }

  .group-request__submit:hover:not(:disabled) {
    filter: brightness(1.05);
  }

  .group-request__submit:disabled {
    opacity: 0.5;
    cursor: not-allowed;
  }

  .catalog-search__row {
    display: flex;
    gap: var(--space-xs);
  }

  .catalog-search__row .create-form__input:first-child {
    flex: 1;
  }

  .chip-row {
    display: flex;
    flex-wrap: wrap;
    gap: var(--space-2xs);
  }

  .chip {
    display: inline-flex;
    align-items: center;
    gap: var(--space-2xs);
    padding: 0.2rem var(--space-xs);
    border: 1px solid var(--border);
    border-radius: 999px;
    color: var(--ink);
    font-size: 0.8rem;
  }

  .chip__remove {
    padding: 0;
    border: none;
    background: none;
    color: var(--muted);
    font-size: 0.9rem;
    line-height: 1;
    cursor: pointer;
  }

  .chip__remove:hover {
    color: var(--fail);
  }

  .chip--example {
    background: none;
    font: inherit;
    cursor: pointer;
  }

  .chip--example:hover:not(:disabled) {
    border-color: var(--accent);
    color: var(--accent);
  }

  .chip--example:disabled {
    opacity: 0.5;
    cursor: not-allowed;
  }

  .catalog-badge {
    display: inline-flex;
    padding: 0.1rem var(--space-2xs);
    border: 1px solid var(--border);
    border-radius: 999px;
    color: var(--muted);
    font-size: 0.7rem;
    white-space: nowrap;
  }

  .catalog-badge--builtin {
    border-color: var(--accent);
    color: var(--accent);
  }

  .persona-list,
  .skill-list {
    display: flex;
    flex-direction: column;
    gap: var(--space-xs);
  }

  .persona-card,
  .skill-item {
    display: flex;
    align-items: flex-start;
    gap: var(--space-xs);
    padding: var(--space-sm);
    border: 1px solid var(--border);
    border-radius: var(--radius-sm);
    cursor: pointer;
  }

  /* Persona builder v2 (addendum): "Build your own persona" is a primary-
     styled action above the search results, not a text afterthought below
     them — same accent treatment as create-form__submit, but full-width
     since it now stands alone above the list rather than sitting inline
     next to a cancel button. */
  .persona-build-cta {
    width: 100%;
    padding: var(--space-sm) var(--space-md);
    border: none;
    border-radius: var(--radius-sm);
    background: var(--accent);
    color: var(--accent-ink);
    font-weight: 600;
    cursor: pointer;
  }

  .persona-build-cta:hover {
    filter: brightness(1.05);
  }

  .persona-card:hover,
  .skill-item:hover {
    border-color: var(--accent);
  }

  .persona-card--selected {
    border-color: var(--accent);
    background: color-mix(in srgb, var(--accent) 8%, var(--panel));
  }

  .persona-card input,
  .skill-item input {
    margin-top: 0.2rem;
  }

  .persona-card__body,
  .skill-item__body {
    display: flex;
    flex: 1;
    flex-direction: column;
    gap: 0.15rem;
  }

  .persona-card__head,
  .skill-item__head {
    display: flex;
    align-items: center;
    gap: var(--space-xs);
  }

  .persona-card__name,
  .skill-item__name {
    color: var(--ink);
    font-family: var(--font-mono);
    font-size: 0.85rem;
    font-weight: 600;
  }

  .persona-card__description,
  .skill-item__description {
    color: var(--muted);
    font-size: 0.8rem;
  }

  .skills-show-more {
    align-self: flex-start;
    padding: var(--space-2xs) var(--space-sm);
    border: 1px solid var(--border);
    border-radius: var(--radius-sm);
    background: var(--panel);
    color: var(--ink);
    font-size: 0.8rem;
    cursor: pointer;
  }

  .skills-show-more:hover {
    border-color: var(--accent);
    color: var(--accent);
  }

  /* --- The Naming: five cards, staggered entrance, quiet press-select --- */

  .name-cards {
    display: flex;
    flex-direction: column;
    gap: var(--space-xs);
  }

  .name-card {
    display: flex;
    align-items: flex-start;
    gap: var(--space-xs);
    padding: var(--space-sm);
    border: 1px solid var(--border);
    border-radius: var(--radius-sm);
    cursor: pointer;
    animation: name-card-rise 260ms ease-out both;
  }

  /* 60ms per-card stagger (addendum) — five tags settling onto the bench
     one after another. Purely nth-child so no per-card inline style/JS is
     needed for it. */
  .name-card:nth-child(1) {
    animation-delay: 0ms;
  }
  .name-card:nth-child(2) {
    animation-delay: 60ms;
  }
  .name-card:nth-child(3) {
    animation-delay: 120ms;
  }
  .name-card:nth-child(4) {
    animation-delay: 180ms;
  }
  .name-card:nth-child(5) {
    animation-delay: 240ms;
  }

  .name-card:hover {
    border-color: var(--accent);
  }

  .name-card--selected {
    border-color: var(--accent);
    background: color-mix(in srgb, var(--accent) 8%, var(--panel));
    animation: name-card-press 160ms ease-out;
  }

  .name-card--dimmed {
    opacity: 0.55;
  }

  .name-card input {
    margin-top: 0.2rem;
  }

  .name-card__body {
    display: flex;
    flex-direction: column;
    gap: 0.15rem;
  }

  .name-card__name {
    color: var(--ink);
    font-family: var(--font-mono);
    font-size: 0.95rem;
    font-weight: 600;
  }

  .name-card__reason {
    color: var(--muted);
    font-size: 0.8rem;
  }

  .name-card--skeleton {
    flex-direction: column;
    gap: var(--space-2xs);
    cursor: default;
  }

  .name-card__skeleton-bar {
    display: block;
    height: 0.85rem;
    border-radius: var(--radius-sm);
    background: linear-gradient(
      90deg,
      var(--border) 25%,
      var(--panel) 50%,
      var(--border) 75%
    );
    background-size: 200% 100%;
    animation: name-card-shimmer 1.6s ease-in-out infinite;
  }

  .name-card__skeleton-bar--name {
    width: 40%;
  }

  .name-card__skeleton-bar--reason {
    width: 70%;
    height: 0.7rem;
  }

  @keyframes name-card-rise {
    from {
      opacity: 0;
      transform: translateY(8px);
    }
    to {
      opacity: 1;
      transform: translateY(0);
    }
  }

  @keyframes name-card-press {
    from {
      transform: scale(0.98);
    }
    to {
      transform: scale(1);
    }
  }

  @keyframes name-card-shimmer {
    0% {
      background-position: 200% 0;
    }
    100% {
      background-position: -200% 0;
    }
  }

  @media (prefers-reduced-motion: reduce) {
    .name-card,
    .name-card--selected,
    .name-card__skeleton-bar {
      animation: none;
    }
  }

  .naming-actions {
    display: flex;
    align-items: center;
    gap: var(--space-sm);
  }

  .naming-actions__reroll {
    padding: var(--space-2xs) var(--space-sm);
    border: 1px solid var(--border);
    border-radius: var(--radius-sm);
    background: var(--panel);
    color: var(--ink);
    font-size: 0.85rem;
    cursor: pointer;
  }

  .naming-actions__reroll:hover:not(:disabled) {
    border-color: var(--accent);
    color: var(--accent);
  }

  .naming-actions__reroll:disabled {
    opacity: 0.6;
    cursor: not-allowed;
  }

  .naming-actions__manual {
    padding: 0;
    border: none;
    background: none;
    color: var(--muted);
    font-size: 0.85rem;
    text-decoration: underline;
    cursor: pointer;
  }

  .naming-actions__manual:hover {
    color: var(--accent);
  }

  .naming-actions__back {
    padding: 0;
    border: none;
    background: none;
    color: var(--muted);
    font-size: 0.8rem;
    text-decoration: underline;
    cursor: pointer;
  }

  .naming-actions__back:hover {
    color: var(--accent);
  }

  .create-form__actions {
    display: flex;
    justify-content: flex-end;
    gap: var(--space-sm);
  }

  .create-form__cancel {
    padding: var(--space-xs) var(--space-md);
    border: 1px solid var(--border);
    border-radius: var(--radius-sm);
    background: var(--panel);
    color: var(--ink);
    cursor: pointer;
  }

  .create-form__cancel:hover {
    border-color: var(--accent);
    color: var(--accent);
  }

  .create-form__submit {
    padding: var(--space-xs) var(--space-md);
    border: none;
    border-radius: var(--radius-sm);
    background: var(--accent);
    color: var(--accent-ink);
    font-weight: 600;
    cursor: pointer;
  }

  .create-form__submit:hover:not(:disabled) {
    filter: brightness(1.05);
  }

  .create-form__submit:disabled {
    opacity: 0.5;
    cursor: not-allowed;
  }
</style>
