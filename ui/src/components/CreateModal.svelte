<script lang="ts">
  // The one-click moment, now staged as three stations at a bench (create
  // flow v2, "the naming ceremony" addendum; Persona v3 / teamless-create
  // addendum drops the Team station entirely and reorders what's left; the
  // UI-simplification wave then drops Skills too -- skills are picked
  // post-create, in the Skills tab, not at the bench):
  // Persona -> The Naming -> Create, each one unlocking once the
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
  // Modal idiom wave (2026-09-23): the three stations are three sections of
  // one scrolling body now, under a single compact stepper line -- the
  // woodstamp circles, the display-serif headings and the dimmed
  // "locked" sections are gone, and so are the tall persona cards (a dense
  // radio list instead; "Start from scratch" is the quiet first row of it,
  // not a filled bar above it). Create and Cancel live in the shell's
  // pinned footer, so the finishing button is on screen from the first
  // beat. Progress is still a cue and nothing more: nothing is
  // `disabled`/`inert` because a later step has not been reached, and
  // `canSubmit` below is the one thing that's actually gated -- unchanged,
  // just fed by fields that render in one column now.
  //
  // Name availability is the one field with a network round trip attached:
  // every keystroke gets instant client-side validation (validate.ts) and
  // only once that passes does a debounced (350ms) `checkAvailability` call
  // go out, so a name that's obviously wrong never costs a request. A name
  // can now arrive two ways — picking one of the three suggested rows, or
  // "name it yourself" — but either way it lands in the same `name` state
  // and runs through the same check.
  //
  // Persona is a catalog *search* now (Task 10), not a whole catalog
  // fetched once: `personasSearchQuery` re-runs as the user types, debounced
  // 250ms via the same `debounced` helper the name field uses. Because
  // @tanstack/svelte-query's installed adapter (5.90.2) only re-derives a
  // query's options from an actual Svelte store (see MemoryPanel/SlackPanel's
  // delivery-chip doc comments for the same constraint), the debounced
  // search term lives in a plain `writable` store, fed by an `$effect` off
  // the `$state` the input is bound to -- not a `$derived` object, which the
  // adapter would freeze at first render and never look at again. The
  // naming station's suggestion fetch (`nameSuggestionsQuery`) uses the
  // exact same writable-store idiom, just set once per persona commit
  // rather than per keystroke.
  //
  // Models come from `modelsQuery()`, prefetched by App.svelte the same
  // "warm modal" way the old whole-catalog fetch was. `teamOptionsQuery()`
  // (`/api/teams`, the distinct skill-owner pool + a server-named default)
  // has no reader in this component -- it backs Edit details' own team field
  // (`MetadataDetails`) instead. Org wiring status (`orgStatusQuery()`)
  // decides whether the persona section shows a real picker or an honest
  // "not set up yet" empty state -- see `personasUnwired` below.
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
  // to the catalog's default model, below (persona has no such fallback
  // any more: a search-driven list has no stable "first" item to default
  // to, so a persona must be explicitly picked).
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
  let personaSearchTerm = $state("");
  const personas = createQuery(() => (personasSearchQuery(personaSearchTerm, null)));

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

  let builderOpen = $state(false);

  /** The committed persona's own text, kept alongside its id purely to seed
   * the naming station below (`description` doubles as `who_they_are`
   * verbatim — see `kobo_api.personas` — for every persona this room knows
   * about, catalog-shipped or user-authored). Set at the exact moment a
   * persona is picked (both branches below), never re-derived later by
   * looking the id back up in `personas.data`: that list is itself a
   * debounced search result that can lag a beat behind a just-created
   * persona (createPersonaMutation's own cache invalidation hasn't
   * necessarily resolved yet), so re-deriving would risk racing it.
   *
   * `personaId`/`whatTheyDo` (create-identity, Task 4) are what let the
   * naming station tell the two lanes apart: a search pick has a real,
   * already-stored id, so `personaId` carries it and `whatTheyDo` stays
   * `null` — the server does the real-body lookup (see
   * `NameSuggestionsRequest` in api.ts). A builder create has no id yet at
   * the moment its own "Create persona" click fires the warm-up prefetch
   * (nothing's been written yet), so `PersonaBuilder` hands back its
   * drafted body directly through `oncreated`'s third argument instead;
   * `personaId` stays `null` for that lane so the naming query's key never
   * drifts from the one that prefetch already warmed.
   *
   * `purposeTags` (persona-metadata plan) rides alongside for the same
   * reason: the moment a persona commits is also the moment the Create
   * station's chips editor should (re-)seed, so it travels in the same
   * state update rather than a second, separately-timed effect. */
  let selectedPersonaMeta = $state<
    {
      whoTheyAre: string;
      personaId: string | null;
      whatTheyDo: string | null;
      purposeTags: string[];
    } | null
  >(null);

  function selectPersona(
    persona: CatalogEntry,
    purposeTags: string[] = [],
    builderWhatTheyDo: string | null = null,
  ): void {
    personaOverride = persona.id;
    selectedPersonaMeta = {
      whoTheyAre: persona.description,
      personaId: builderWhatTheyDo === null ? persona.id : null,
      whatTheyDo: builderWhatTheyDo,
      purposeTags,
    };
  }

  /** Warms the naming station's suggestion cache the instant an existing
   * persona is picked from search (naming station perf fix), since
   * `description` is already sitting right here in the search result with
   * no network round trip needed to learn it, so this is the earliest
   * possible moment to start the Vertex call, well before the operator's
   * eye even reaches station 2. Sends `persona_id` (create-identity, Task
   * 4) so the server does the real-body lookup rather than this thin
   * search-result text; `who_they_are` doubles as the required
   * `what_they_do` placeholder since a resolved `persona_id` makes the
   * server ignore it anyway (see `NameSuggestionsRequest` in api.ts) — it
   * must still be some non-empty string, and it must not be the persona's
   * bare label, which is exactly what this avoids. Same params the
   * persona-commit effect below will feed `nameSuggestionParams` (`exclude:
   * []`/`round: 0`, since picking any persona resets both), so a warm cache
   * entry here means that reactive query never has to wait on a second
   * request of its own — a mismatched key here would just warm a dead one.
   *
   * Deliberately NOT folded into `selectPersona` itself: that's also
   * called from `handlePersonaCreated` below, for the builder path, which
   * already warms this same cache earlier still, at `PersonaBuilder`'s own
   * "Create persona" click, before its create-persona POST even resolves.
   * Adding a second prefetch here for that path would just be a redundant
   * (if harmless) extra call once the mutation lands. */
  function prefetchNameSuggestionsForPick(persona: CatalogEntry): void {
    void queryClient.prefetchQuery(
      nameSuggestionsQuery({
        whoTheyAre: persona.description,
        whatTheyDo: persona.description,
        personaId: persona.id,
        exclude: [],
        round: 0,
      }),
    );
  }

  function handlePersonaCreated(
    persona: CatalogEntry,
    purposeTags: string[],
    whatTheyDo: string,
  ): void {
    selectPersona(persona, purposeTags, whatTheyDo);
    builderOpen = false;
    personaQ = "";
    personaSearchTerm = "";
    toast.success("Persona ready");
  }

  // --- The Naming: three suggested cards once a persona is committed -----
  //
  // create-identity Task 4 rewrite: a search-picked persona already has a
  // real, stored id, so its lane sends `persona_id` and lets the SERVER
  // pull the persona's own real body + tags (`kobo_api.main`'s
  // name-suggestions route, Task 3) — `who_they_are` (== `description`
  // verbatim) doubles as the required `what_they_do` placeholder, since a
  // resolved `persona_id` makes the server ignore that field anyway. A
  // builder-drafted persona has no id yet at the moment its own warm-up
  // prefetch fires (see `PersonaBuilder`'s "Create persona" click), so that
  // lane sends the drafted body's own first 2000 chars as `what_they_do`
  // directly, with no `persona_id` at all — see `selectedPersonaMeta`'s own
  // doc comment above for how the two lanes are told apart.

  let nameMode = $state<"cards" | "manual">("cards");
  let selectedCardName = $state<string | null>(null);
  // Set only by an "Offer more" whose names come back identical to the set
  // already on screen — the fallback pool is deterministic per persona
  // text, so a generator outage can make that happen; copy must not
  // promise novelty it can't deliver (see `handleOfferMore`).
  let rerollNote = $state(false);
  let createNameInput: HTMLInputElement | undefined = $state();

  /** The live query's actual params, INCLUDING `exclude`/`round` — a frozen
   * snapshot for whichever round is currently on screen (create-identity,
   * Task 4). Deliberately NOT a live derived read of the ever-growing
   * `offeredNames` below: `exclude` has to be part of the query key (a
   * different exclude set is a genuinely different request), but if the
   * key for the round ALREADY ON SCREEN kept changing every time
   * `offeredNames` grew, the station would chase its own tail — the
   * moment round 0's three names settle, the accrual effect (below) folds
   * them into `offeredNames`, which would immediately read as a new
   * `exclude` value for the SAME round if this object read it live,
   * firing a second fetch under a new key, whose own three names would
   * grow `offeredNames` again, firing a third... This object only ever
   * changes at an explicit round boundary (a persona commit, or an "Offer
   * more" click in `handleOfferMore`), so it stays stable for as long as
   * one round's three cards are actually on screen. */
  let nameSuggestionParams = $state<{
    whoTheyAre: string;
    whatTheyDo: string;
    personaId: string | null;
    exclude: string[];
    round: number;
  }>({
    whoTheyAre: "",
    whatTheyDo: "",
    personaId: null,
    exclude: [],
    round: 0,
  });

  /** Every name this persona's naming station has shown across every round
   * so far (create-identity, Task 4) — pure bookkeeping, consulted only
   * when `handleOfferMore` builds the NEXT round's `exclude` set, never fed
   * directly into the live query above (see that state's own doc comment
   * for why). Capped at 20 (dropping the oldest beyond that, same cap the
   * route itself enforces server-side) — generous well past the three a
   * single round ever shows, but bounded so an operator who mashes "Offer
   * more" all afternoon doesn't grow the request body without limit. Reset
   * to `[]` whenever the persona changes (see the persona-commit effect
   * below) — a prior persona's offered names have no bearing on a new
   * one's. */
  let offeredNames = $state<string[]>([]);
  const OFFERED_NAMES_CAP = 20;

  const nameSuggestions = createQuery(() => (
    nameSuggestionsQuery({
      whoTheyAre: nameSuggestionParams.whoTheyAre,
      whatTheyDo: nameSuggestionParams.whatTheyDo,
      personaId: nameSuggestionParams.personaId,
      exclude: nameSuggestionParams.exclude,
      round: nameSuggestionParams.round,
    })
  ));

  // Accumulates every name a settled response has actually shown into
  // `offeredNames` (create-identity, Task 4) — the ONE place that list
  // grows, so the next "Offer more" round's exclude set reflects every
  // card the operator has genuinely seen for this persona, not just the
  // immediately-prior round. Reads/writes `offeredNames` only, never
  // `nameSuggestionParams` — see that state's own doc comment for why
  // folding this back into the live query's own params would loop.
  $effect(() => {
    const data = nameSuggestions.data;
    if (data === undefined || data.length === 0) {
      return;
    }
    const fresh = data.map((s) => s.name).filter((n) => !offeredNames.includes(n));
    if (fresh.length === 0) {
      return;
    }
    const merged = [...offeredNames, ...fresh];
    offeredNames =
      merged.length > OFFERED_NAMES_CAP
        ? merged.slice(merged.length - OFFERED_NAMES_CAP)
        : merged;
  });

  // A persona commit — a first pick, a builder create, or picking a
  // *different* persona than before — feeds the naming query its who/what
  // text and, in the same beat, voids whatever the naming station had
  // going for the previous persona (or for no persona at all): a card pick
  // or a hand-typed name tied to one persona's suggestions has no business
  // surviving a switch to a different one, and neither does an earlier
  // persona's offered-names history or round count. Purpose tags
  // (persona-metadata plan) re-seed here too, from the SAME commit's
  // `purposeTags` — a builder create carries its draft's tags, a search
  // pick carries `[]` (see `selectPersona`'s own default).
  $effect(() => {
    const meta = selectedPersonaMeta;
    if (!open) {
      return;
    }
    nameSuggestionParams = {
      whoTheyAre: meta?.whoTheyAre ?? "",
      whatTheyDo: meta?.whatTheyDo ?? meta?.whoTheyAre ?? "",
      personaId: meta?.personaId ?? null,
      exclude: [],
      round: 0,
    };
    offeredNames = [];
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
  const showManualName = $derived(nameMode === "manual" || nameSuggestions.isError);

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
   * deterministic per persona text, so promising total novelty would
   * sometimes be a lie). create-identity Task 4: this used to be a bare
   * `nameSuggestions.refetch()`, which re-runs the SAME query key and so
   * can only ever hand back the SAME three names a generator failure
   * already served — "Offer more" that can't actually offer more. Bumping
   * `round` (and carrying every name `offeredNames` has accumulated so far
   * as `exclude`) instead addresses a genuinely new cache key, so a fresh
   * request actually goes out and the route's own exclude-set enforcement
   * (`kobo_api.namegen.finalize_suggestions`) keeps every name already
   * shown off the table. `queryClient.fetchQuery` (not the reactive
   * `nameSuggestions` query itself) is what actually issues that request
   * and lets this function await its result directly, for the
   * before/after comparison below — the reactive query picks up the exact
   * same cache entry once Svelte re-renders with the reassigned
   * `nameSuggestionParams`, so nothing fetches twice.
   *
   * Compares the names on screen right before the click against the ones
   * that come back: if the generator is down and the deterministic
   * fallback hands back the exact same set, `rerollNote` flips on so the
   * station can say so plainly instead of pretending something changed —
   * count-agnostic, since it compares whatever came back to whatever was
   * shown rather than assuming a fixed size. Either way, the previous pick
   * doesn't survive an "Offer more" — the offered set just changed out
   * from under it. */
  async function handleOfferMore(): Promise<void> {
    const before = (nameSuggestions.data ?? []).map((s) => s.name);
    const params = {
      whoTheyAre: nameSuggestionParams.whoTheyAre,
      whatTheyDo: nameSuggestionParams.whatTheyDo,
      personaId: nameSuggestionParams.personaId,
      exclude: offeredNames,
      round: nameSuggestionParams.round + 1,
    };
    nameSuggestionParams = params;
    const result = await queryClient.fetchQuery(nameSuggestionsQuery(params));
    const after = result.map((s) => s.name);
    rerollNote =
      before.length > 0 &&
      after.length === before.length &&
      after.every((candidate, i) => candidate === before[i]);
    selectedCardName = null;
    name = "";
  }

  // --- Group (access-control Task B2, sectioned by the shared-group +
  // group-requests wave) -----------------------------------------------
  //
  // `groupsQuery` (`GET /api/catalog/groups`) is the ACCESS namespace this
  // agent belongs to -- DISTINCT from Team above (the skills-catalog
  // capability axis). The picker only renders at all once `groups.data`
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
  // The select leads with the door out (group-menu-leads-with-the-door
  // ruling): "My group is not listed..." -- the escape hatch into the
  // mini-form below for a directory group `cfg.groups` doesn't know about
  // yet -- comes FIRST, right after the disabled "Choose a group"
  // placeholder and OUTSIDE any optgroup, so it's the first real choice a
  // person sees rather than something buried at the bottom. After it, the
  // select sections on `kind` (never on a hardcoded "shared" string,
  // matching the server's own "don't hardcode SHARED_GROUP's spelling
  // client-side" doc comment on `/api/catalog/groups`): a "Shared" optgroup
  // next, a "Non-engineering groups" optgroup after that (create-modal
  // non-engineering-wing ruling -- config-driven via `GroupChoice.kind`
  // server-side, never a hardcoded list of names here), then an
  // "Engineering groups" optgroup last. Each section is omitted entirely
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
    const name = groupRequestName.trim();
    if (name.length === 0 || requestGroup.isPending) {
      return;
    }
    groupRequestError = null;
    requestGroup.mutate(
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

  // --- Stations: which of the three read as active vs. muted -------------
  // Purely a visual cue (see the doc comment at the top of this file) —
  // none of these gate anything functionally; `canSubmit` below is the one
  // real gate, and it's unchanged.
  const personaCommitted = $derived(selectedPersona !== "");
  const namingCommitted = $derived(name.length > 0);

  /** Which of the three the stepper line puts in ink (modal idiom wave).
   * The same reading of progress the woodstamp markers used to draw, minus
   * the serif headings and the dimmed sections -- and just as much a cue
   * and nothing more: `canSubmit` below is still the one real gate. */
  const currentStep = $derived(!personaCommitted ? 1 : !namingCommitted ? 2 : 3);
  const STEPS = ["Persona", "Name", "Create"];

  // Fresh form every time the modal opens.
  $effect(() => {
    if (open) {
      name = "";
      personaOverride = null;
      modelOverride = null;
      submitError = null;
      personaQ = "";
      personaSearchTerm = "";
      builderOpen = false;
      selectedPersonaMeta = null;
      nameMode = "cards";
      selectedCardName = null;
      rerollNote = false;
      nameSuggestionParams = {
        whoTheyAre: "",
        whatTheyDo: "",
        personaId: null,
        exclude: [],
        round: 0,
      };
      offeredNames = [];
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
    models.data !== undefined &&
      nameLocallyValid &&
      availability.status === "available" &&
      !personasUnwired &&
      selectedPersona !== "" &&
      selectedModel !== "" &&
      (!groupOptionsAvailable || groupChosen) &&
      !mutation.isPending,
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
        // Skills are added post-create, in the Skills tab (UI-simplification
        // wave) -- the bench no longer picks them.
        skills: [],
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
      <p class="field__status field__status--error">{nameError}</p>
    {:else if availability.status === "checking"}
      <p class="field__status">Checking…</p>
    {:else if availability.status === "available"}
      <p class="field__status field__status--ok">✓ Available</p>
    {:else if availability.status === "unavailable"}
      <p class="field__status field__status--error">
        {availability.reason}
      </p>
    {/if}
  </div>
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

<Modal
  {open}
  title="Create agent"
  size="wide"
  {onclose}
  footer={builderOpen ? undefined : createFooter}
>
  <form
    class="modal__form"
    onsubmit={(event) => {
      event.preventDefault();
      handleSubmit();
    }}
  >
    <!-- Three steps on one line. The markers used to be woodstamp circles
         with display-serif headings under them, which cost a third of the
         dialog's height before anybody had picked anything. -->
    <ol class="modal__steps" aria-label="Steps">
      {#each STEPS as label, index (label)}
        <li
          class="modal__step"
          class:modal__step--current={currentStep === index + 1}
          aria-current={currentStep === index + 1 ? "step" : undefined}
        >
          <span class="modal__step-number">{index + 1}</span>
          {label}
        </li>
      {/each}
    </ol>

    <!-- Step 1: Persona -->
    <div class="modal__section">
      {#if personasUnwired}
        <EmptyState message={UNWIRED_MESSAGE} />
      {:else if builderOpen}
        <PersonaBuilder
          oncancel={() => (builderOpen = false)}
          oncreated={handlePersonaCreated}
        />
      {:else}
        <fieldset class="field field--group">
          <legend class="field__sr">Persona</legend>
          <label class="field__label" for="persona-search">Search personas</label>
          <input
            id="persona-search"
            class="field__input"
            type="text"
            bind:value={personaQ}
            placeholder="Search personas"
            autocomplete="off"
          />

          <div class="list">
            <!-- Writing one yourself is an alternative to the list, so it
                 sits at the top of it as a quiet row rather than a filled
                 bar above it. Outside the loading/error branches below: it
                 is the one row that never depends on the search coming
                 back, and a room whose catalog is slow or broken still has
                 this way through. -->
            <button
              type="button"
              class="list__row list__row--action"
              onclick={() => (builderOpen = true)}
            >
              Start from scratch
            </button>
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
            {:else}
              {#each personas.data as persona (persona.id)}
                <label
                  class="list__row"
                  class:list__row--selected={persona.id === selectedPersona}
                >
                  <input
                    type="radio"
                    name="persona"
                    value={persona.id}
                    checked={persona.id === selectedPersona}
                    onchange={() => {
                      prefetchNameSuggestionsForPick(persona);
                      selectPersona(persona);
                    }}
                  />
                  <span class="list__name">{persona.name}</span>
                  <span class="list__tag" class:list__tag--accent={persona.builtin}>
                    {persona.builtin ? "Built-in" : persona.team}
                  </span>
                  <span class="list__desc" title={persona.description}>
                    {persona.description}
                  </span>
                </label>
              {/each}
              {#if personas.data.length === 0 && personaQ.length > 0}
                <p class="list__empty">Nothing matches your search.</p>
              {/if}
            {/if}
          </div>
        </fieldset>
      {/if}
    </div>

    {#if !builderOpen}
      <!-- Step 2: the name -->
      <div class="modal__section">
        {#if !personaCommitted}
          <p class="modal__hint">Pick a persona first.</p>
        {:else if showManualName}
          {#if nameSuggestions.isError}
            <p class="field__help">
              Couldn't come up with name ideas right now. Name it yourself below.
            </p>
          {/if}
          <div class="field">
            <label class="field__label" for="create-name">Name</label>
            <input
              id="create-name"
              class="field__input"
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
            <p class="field__help">
              Lowercase letters, numbers, and hyphens. 2-24 characters.
            </p>
            {@render nameStatus()}
            {#if nameMode === "manual" && nameSuggestions.data !== undefined && !nameSuggestions.isError}
              <button
                type="button"
                class="btn btn--quiet"
                onclick={() => (nameMode = "cards")}
              >
                Back to the suggestions
              </button>
            {/if}
          </div>
        {:else if nameSuggestions.isPending}
          <Skeleton rows={3} />
          <p class="field__sr" role="status">Loading name suggestions…</p>
        {:else}
          <div class="list" role="radiogroup" aria-label="Suggested names">
            {#each nameSuggestions.data as card (card.name)}
              <label
                class="list__row"
                class:list__row--selected={card.name === selectedCardName}
              >
                <input
                  type="radio"
                  name="suggested-name"
                  value={card.name}
                  checked={card.name === selectedCardName}
                  onchange={() => selectCard(card)}
                />
                <span class="list__name">{card.name}</span>
                <span class="list__desc" title={card.reason}>{card.reason}</span>
              </label>
            {/each}
          </div>

          {#if selectedCardName !== null}
            {@render nameStatus()}
          {/if}

          <div class="field__row">
            <button
              type="button"
              class="btn"
              onclick={handleOfferMore}
              disabled={nameSuggestions.isFetching}
            >
              {nameSuggestions.isFetching ? "Asking again…" : "Offer more"}
            </button>
            <button
              type="button"
              class="btn btn--quiet"
              onclick={() => (nameMode = "manual")}
            >
              name it yourself
            </button>
          </div>
          {#if rerollNote}
            <p class="field__help">Same names as before. The workshop stands by them.</p>
          {/if}
        {/if}
      </div>

      <!-- Step 3: the rest of what the room needs -->
      <div class="modal__section">
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
              {#if sharedGroupOptions.length > 0}
                <optgroup label="Shared">
                  {#each sharedGroupOptions as group (group.name)}
                    <option value={group.name}>{group.name} (everyone in the company)</option>
                  {/each}
                </optgroup>
              {/if}
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
            </select>
            <p class="field__help">Who can see and use this agent.</p>
            {#if selectedGroupKind === "shared"}
              <p class="field__help">
                Shared agents are visible to everyone in the company.
              </p>
            {/if}

            {#if groupRequestConfirmed}
              <p class="field__status field__status--ok" role="status">
                Requested. A room admin will add it. Pick shared to create this
                agent now, or wait for your group.
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
          <label class="field__label" for="create-purpose-tag-input">Purpose tags</label>
          <p class="field__help">
            Short labels that help people and agents discover what this agent is for.
          </p>
          <div class="chip-row">
            {#each PURPOSE_TAG_EXAMPLES as example (example)}
              <button
                type="button"
                class="chip chip--action"
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
          <div class="field__row">
            <input
              id="create-purpose-tag-input"
              class="field__input"
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
              class="btn"
              disabled={purposeTagInput.trim().length === 0 ||
                purposeTags.length >= PURPOSE_TAGS_CAP}
              onclick={addPurposeTag}
            >
              Add
            </button>
          </div>
          <p class="field__help">Up to 8 tags.</p>
        </div>

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

        {#if submitError}
          <p class="field__status field__status--error" role="alert">
            {submitError}
          </p>
        {/if}
      </div>
    {/if}
  </form>
</Modal>

<style>
  /* Fixed floor so the idle state (no message rendered at all) reserves the
     same space a one-line status/error message would take — otherwise the
     field sits flush against the help text (a stray gap sliver) and then
     jumps down the moment a message appears. */
  :global(#create-name-status) {
    min-height: 1.25rem;
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
