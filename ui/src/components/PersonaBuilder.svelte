<script lang="ts">
  // The persona form: one screen, for making a persona and for editing one.
  //
  // Rebuilt 2026-11-17. What it replaced was two steps with three ways in
  // (paste a document, describe a job, start blank), two upload buttons and
  // a preview nobody could tell from the first step. People could not see
  // at a glance what to add or how much, and what they made did not read
  // like the room's own personas: a pasted document went in as it was, and
  // a one-line job description became a one-line persona.
  //
  // So the form is the persona. Four parts, top to bottom, the same four a
  // built-in has, each with one line saying what goes there and how much:
  //   Name          one or two words
  //   Who they are  one line
  //   What they do  the body, in the room's shape
  //   Rules         one per line
  // and the group it is filed under.
  //
  // Rules only where an agent keeps its rules in its persona (2026-11-20). With
  // guardrails on, each agent's rules are its own Guardrails, so people writing
  // a persona are not asked for any and a draft's are not used: writing them
  // here only to change them again on each agent was the same work twice. An
  // edit still shows the box while the persona has rules, so nothing in it is
  // hidden; the built-ins keep theirs, which start a new agent's Guardrails.
  //
  // Above them, when making one, a single optional box: describe the job or
  // paste what you already have, press Draft persona, and the room writes
  // the parts in that shape (`POST /api/personas/draft`). It is a way
  // to fill the form, not a step. A draft fills what is empty and refreshes
  // what an earlier draft wrote; anything a person typed stays
  // (`mergeDraftFields`). A draft that fails says so and leaves the form
  // alone.
  //
  // The room keeps a floor so a persona is enough for an agent to work
  // from (`kobo_api.personas.check_substance`). `personaMissing` mirrors it
  // and names the next thing the form needs in one sentence above the
  // footer, the way the create form does. The server is still the check.
  //
  // Group: a new persona is filed under its maker's group, never the
  // commons by default. The Group already picked on the create form, or
  // the pill pressed in the persona list, when the maker may file there;
  // else their one group; someone in several chooses. Everyone is last,
  // and the default only for someone in no group at all. The server's gate
  // is the real check, and a refusal reads as one sentence.
  //
  // Editing (`editId`) is the same form over a stored persona: the text is
  // read whole, the name and the group stay as they are, and Save changes
  // goes to `PUT /api/personas/{id}`.
  //
  // This is drawn inside the create dialog's body in place of the persona
  // list, so it brings its own action row, sticky at the bottom of that
  // body; while it is up the dialog's own footer is not. Which persona is
  // selected, and whether the list or this form is showing, stay with
  // CreateModal. `oncreated` hands back the created entry itself, since
  // reading it off the list's cache would race that cache's refetch.
  import { onMount } from "svelte";
  import { createMutation, createQuery, useQueryClient } from "@tanstack/svelte-query";
  import { Button } from "$lib/components/ui/button/index.js";
  import { Input } from "$lib/components/ui/input/index.js";
  import { Label } from "$lib/components/ui/label/index.js";
  import ErrorState from "./ErrorState.svelte";
  import Skeleton from "./Skeleton.svelte";
  import { ApiError, errorMessage, type CatalogEntry } from "../lib/api";
  import {
    createPersonaMutation,
    draftPersonaMutation,
    groupsQuery,
    personaFullQuery,
    updatePersonaMutation,
    whoamiQuery,
  } from "../lib/queries";
  import {
    EMPTY_PERSONA_TEXT,
    mergeDraftFields,
    personaGroupLabel,
    personaMissing,
    personaTeamOptions,
    splitLines,
    type PersonaText,
  } from "../lib/validate";

  let {
    oncancel,
    oncreated = () => {},
    onsaved = () => {},
    defaultTeam = null,
    editId = null,
  }: {
    oncancel: () => void;
    /** A persona was made. The tags are the draft's, `[]` without one. */
    oncreated?: (persona: CatalogEntry, purposeTags: string[]) => void;
    /** An edit was saved. */
    onsaved?: (persona: CatalogEntry) => void;
    /** The group to offer first: the create form's, or the pressed pill's. */
    defaultTeam?: string | null;
    /** Edit this persona instead of making one. */
    editId?: string | null;
  } = $props();

  const queryClient = useQueryClient();
  const draftMutation = createMutation(() => draftPersonaMutation());
  const createMut = createMutation(() => createPersonaMutation(queryClient));
  const saveMut = createMutation(() => updatePersonaMutation(queryClient));

  const editing = $derived(editId !== null);
  const stored = createQuery(() => personaFullQuery(editId));

  let text = $state<PersonaText>({ ...EMPTY_PERSONA_TEXT });
  /** What each field last received from a draft; see `mergeDraftFields`. */
  let drafted = $state<PersonaText>({ ...EMPTY_PERSONA_TEXT });
  let draftedTags = $state<string[]>([]);

  // The stored text seeds the form once. A refetch behind an open form
  // must not write over what is being typed.
  let seeded = false;
  $effect(() => {
    const data = stored.data;
    if (editId === null || data === undefined || seeded) {
      return;
    }
    seeded = true;
    text = {
      name: data.name,
      whoTheyAre: data.who_they_are,
      whatTheyDo: data.what_they_do,
      rules: data.rules.join("\n"),
    };
  });

  // --- The group it is filed under -----------------------------------------

  const SHARED_TEAM = "shared";
  const whoami = createQuery(() => whoamiQuery());
  const guardrailsOn = $derived(whoami.data?.guardrails === true);
  /** The rules box: always without guardrails; with them, only on an edit of
   * a persona that still has rules. */
  const showRules = $derived(
    !guardrailsOn || (editing && (stored.data?.rules.length ?? 0) > 0),
  );
  const isOperator = $derived(whoami.data?.operator === true);
  const roomGroups = createQuery(() => ({ ...groupsQuery(), enabled: isOperator }));
  const myGroups = $derived((whoami.data?.groups ?? []).filter((g) => g !== SHARED_TEAM));
  const teamOptions = $derived(personaTeamOptions(whoami.data, roomGroups.data));
  /** `null` asks the person to choose. */
  const suggestedTeam = $derived(
    whoami.isPending
      ? null
      : defaultTeam && teamOptions.includes(defaultTeam)
        ? defaultTeam
        : myGroups.length === 1
          ? myGroups[0]
          : teamOptions.length === 0
            ? SHARED_TEAM
            : null,
  );
  let teamOverride = $state<string | null>(null);
  const selectedTeam = $derived(teamOverride ?? suggestedTeam);

  const TEAM_REFUSED =
    "You are not in that group, so the persona can't be filed there. Pick one of your groups, or Everyone.";

  // --- Start from a description --------------------------------------------

  // The persona size ceiling, which is also the most the draft route reads.
  const SOURCE_MAX = 49152;
  const READ_FAILED = "Couldn't read this file. Try another.";

  let source = $state("");
  let sourceEl: HTMLTextAreaElement | undefined = $state();
  let fileInput: HTMLInputElement | undefined = $state();
  let draftNote = $state<{ ok: boolean; message: string } | null>(null);
  const DRAFT_FAILED = "Couldn't draft a persona right now. Fill in the parts yourself below.";

  const canDraft = $derived(source.trim().length > 0 && !draftMutation.isPending);

  function handleDraft(): void {
    const job = source.trim();
    if (job.length === 0 || draftMutation.isPending) {
      return;
    }
    if (job.length > SOURCE_MAX) {
      draftNote = { ok: false, message: "That is too long to draft from. Cut it down and try again." };
      return;
    }
    draftNote = null;
    // Rules the person typed are sent for the draft to keep. Rules an
    // earlier draft wrote are not: they would come back as if hand-typed.
    // With no rules box there are none to keep.
    const ownRules = !showRules || text.rules === drafted.rules ? [] : splitLines(text.rules);
    draftMutation.mutate(
      { job, rules: ownRules },
      {
        onSuccess: (draft) => {
          // The model had no say: what came back is a stand-in cut from the
          // source, not a persona in the house shape. Leave the form alone.
          if (draft.generated === false) {
            draftNote = { ok: false, message: DRAFT_FAILED };
            return;
          }
          const merged = mergeDraftFields(text, drafted, draft);
          text = merged.fields;
          drafted = merged.drafted;
          draftedTags = Array.isArray(draft.purpose_tags) ? draft.purpose_tags : [];
          draftNote = { ok: true, message: "Drafted. Read it through and change anything." };
        },
        onError: () => {
          draftNote = { ok: false, message: DRAFT_FAILED };
        },
      },
    );
  }

  /** A file is another way to fill the box: its text lands there, and
   * Draft persona is still the person's to press. */
  function handleFile(event: Event): void {
    const input = event.currentTarget as HTMLInputElement;
    const file = input.files?.[0];
    if (!file) {
      return;
    }
    draftNote = null;
    const reader = new FileReader();
    reader.onload = () => {
      if (typeof reader.result === "string") {
        source = reader.result;
      } else {
        draftNote = { ok: false, message: READ_FAILED };
      }
    };
    reader.onerror = () => {
      draftNote = { ok: false, message: READ_FAILED };
    };
    reader.readAsText(file);
    // So picking the same file again still fires a change.
    input.value = "";
  }

  onMount(() => {
    sourceEl?.focus();
  });

  // --- Create / save -------------------------------------------------------

  let submitError = $state<string | null>(null);
  const pending = $derived(createMut.isPending || saveMut.isPending);
  const missing = $derived(
    personaMissing(
      text,
      editing ? (stored.data?.team ?? SHARED_TEAM) : selectedTeam,
      editing,
      !guardrailsOn,
    ),
  );
  const canSubmit = $derived(
    missing === null && !pending && (!editing || stored.data !== undefined),
  );

  function nameTaken(body: unknown): string {
    const where = (body ?? {}) as { team?: unknown; archived?: unknown };
    if (where.archived === true) {
      return "A persona with that name was archived. Pick another name, or ask a room admin to bring it back.";
    }
    if (typeof where.team === "string" && where.team.length > 0) {
      return `A persona with that name already exists. It is under ${personaGroupLabel(where.team)} in the persona list.`;
    }
    return "A persona with that name already exists.";
  }

  function handleSubmit(event: SubmitEvent): void {
    event.preventDefault();
    if (!canSubmit) {
      return;
    }
    submitError = null;
    // A draft may have filled the rules behind a box that is not shown;
    // those are never sent.
    const rules = showRules ? splitLines(text.rules) : [];
    if (editId !== null) {
      saveMut.mutate(
        {
          id: editId,
          body: { who_they_are: text.whoTheyAre, what_they_do: text.whatTheyDo, rules },
        },
        {
          onSuccess: (saved) => onsaved(saved),
          onError: (error: unknown) => {
            submitError = errorMessage(error, "Something went wrong. Try again.");
          },
        },
      );
      return;
    }
    const tags = draftedTags;
    createMut.mutate(
      {
        name: text.name,
        who_they_are: text.whoTheyAre,
        what_they_do: text.whatTheyDo,
        rules,
        purpose_tags: tags,
        team: selectedTeam ?? SHARED_TEAM,
      },
      {
        onSuccess: (created) => oncreated(created, tags),
        onError: (error: unknown) => {
          if (error instanceof ApiError && error.status === 409) {
            submitError = nameTaken(error.body);
          } else if (error instanceof ApiError && error.status === 403) {
            submitError = TEAM_REFUSED;
          } else {
            submitError = errorMessage(error, "Something went wrong. Try again.");
          }
        },
      },
    );
  }

  const WHAT_PLACEHOLDER = [
    "The work and who it is for, in a short paragraph.",
    "",
    "Your ground: what you own, and what you leave to others.",
    "",
    "1. **A kind of request.** How you handle it.",
    "2. **Another kind.** How you handle it.",
    "",
    "Tiebreaker: what wins when two of these pull apart.",
  ].join("\n");

  const RULES_PLACEHOLDER = [
    "Never guess a number. Say what you checked.",
    "Always name where a fact came from.",
    "Ask before changing anything you were not asked to change.",
  ].join("\n");
</script>

<form class="modal__form" onsubmit={handleSubmit}>
  {#if editing && stored.isPending}
    <Skeleton rows={4} />
  {:else if editing && stored.isError}
    <ErrorState
      message={errorMessage(stored.error, "Couldn't load this persona.")}
      onretry={() => stored.refetch()}
    />
  {:else}
    {#if !editing}
      <div class="field persona-builder__start">
        <div class="field__label-row">
          <label class="field__label" for="persona-builder-source">
            Start from a description
            <span class="persona-builder__optional">Optional</span>
          </label>
          <span class="field__label-actions">
            <!-- The guide's own section, in a new tab so the form keeps
                 what has been typed. -->
            <a
              class="btn--link"
              href="#/getting-started/personas"
              target="_blank"
              rel="noreferrer"
            >
              How personas work
            </a>
          </span>
        </div>
        <textarea
          id="persona-builder-source"
          bind:this={sourceEl}
          bind:value={source}
          rows="3"
          placeholder="Describe the job in a few sentences, or paste notes or a document you already have."
          aria-describedby="persona-builder-source-help"
        ></textarea>
        <div class="persona-builder__start-row">
          <Button
            type="button"
            variant="secondary"
            size="sm"
            disabled={!canDraft}
            onclick={handleDraft}
          >
            {draftMutation.isPending ? "Drafting…" : "Draft persona"}
          </Button>
          <Button type="button" variant="ghost" size="sm" onclick={() => fileInput?.click()}>
            Upload a file
          </Button>
          <input
            bind:this={fileInput}
            type="file"
            accept=".md,.txt"
            hidden
            tabindex="-1"
            onchange={handleFile}
          />
        </div>
        {#if draftNote}
          <p
            class="field__status"
            class:field__status--ok={draftNote.ok}
            class:field__status--error={!draftNote.ok}
            role={draftNote.ok ? "status" : "alert"}
          >
            {draftNote.message}
          </p>
        {:else}
          <p class="field__help" id="persona-builder-source-help">
            Kobo writes the parts below from it. Edit anything after.
          </p>
        {/if}
      </div>
    {/if}

    <div class="field">
      <Label class="field__label" for="persona-builder-name">Name</Label>
      <Input
        id="persona-builder-name"
        type="text"
        bind:value={text.name}
        maxlength={60}
        placeholder="e.g. ledger-keeper"
        autocomplete="off"
        spellcheck="false"
        disabled={editing}
      />
      <p class="field__help">
        {#if editing}
          A persona keeps its name.
        {:else}
          One or two words, like sentinel or ledger-keeper. The agent gets its
          own name later.
        {/if}
      </p>
    </div>

    <div class="field">
      <Label class="field__label" for="persona-builder-who">Who they are</Label>
      <Input
        id="persona-builder-who"
        type="text"
        bind:value={text.whoTheyAre}
        maxlength={200}
        placeholder="e.g. a careful keeper of the team's release notes"
        autocomplete="off"
      />
      <p class="field__help">
        One line. It finishes the sentence "You are [the agent's name], ..."
      </p>
    </div>

    <div class="field">
      <Label class="field__label" for="persona-builder-what">What they do</Label>
      <textarea
        id="persona-builder-what"
        bind:value={text.whatTheyDo}
        rows="12"
        placeholder={WHAT_PLACEHOLDER}
      ></textarea>
      <p class="field__help">
        The work, the ground it owns, each kind of request and how to handle
        it, and what to do when unsure. Most personas run 25 to 45 lines.
      </p>
    </div>

    {#if showRules}
      <div class="field">
        <Label class="field__label" for="persona-builder-rules">
          {guardrailsOn ? "Starter guardrails" : "Rules"}
        </Label>
        <textarea
          id="persona-builder-rules"
          bind:value={text.rules}
          rows="6"
          placeholder={RULES_PLACEHOLDER}
        ></textarea>
        <p class="field__help">
          {#if guardrailsOn}
            One per line. Each new agent made from it starts its Guardrails with these,
            and its owners change them there.
          {:else}
            One per line: things it always or never does. At least 3, most personas have 8
            to 12.
          {/if}
        </p>
      </div>
    {/if}

    {#if !editing}
      <div class="field">
        <Label class="field__label" for="persona-builder-team">Group</Label>
        <select
          id="persona-builder-team"
          class="persona-builder__select"
          value={selectedTeam ?? ""}
          onchange={(event) =>
            (teamOverride = (event.currentTarget as HTMLSelectElement).value)}
        >
          <option value="" disabled>Choose a group</option>
          {#each teamOptions as team (team)}
            <option value={team}>{team}</option>
          {/each}
          <option value={SHARED_TEAM}>{personaGroupLabel(SHARED_TEAM)}</option>
        </select>
        <p class="field__help">
          The persona is filed under this group in the persona list, and the
          people in it can edit it.
        </p>
      </div>
    {/if}
  {/if}

  <!-- What is missing, or why the save was refused, sits in the footer so
       it is on screen wherever the form is scrolled to. -->
  <div class="modal__footer modal__footer--inline">
    {#if submitError}
      <p class="field__status field__status--error persona-builder__state" role="alert">
        {submitError}
      </p>
    {:else if missing && !(editing && stored.data === undefined)}
      <p class="field__help persona-builder__state">{missing}</p>
    {/if}
    <div class="modal__actions">
      <Button type="button" variant="ghost" onclick={oncancel}>Cancel</Button>
      <Button type="submit" disabled={!canSubmit}>
        {#if editing}
          {saveMut.isPending ? "Saving…" : "Save changes"}
        {:else}
          {createMut.isPending ? "Creating…" : "Create persona"}
        {/if}
      </Button>
    </div>
  </div>
</form>

<style>
  /* The one optional thing on the form, set apart from the four parts it
     fills: a ruled box, the way the create form sets apart its own form
     inside a field. */
  .persona-builder__start {
    padding: var(--space-sm);
    border: 1px solid var(--border);
    border-radius: var(--radius-sm);
  }

  .persona-builder__optional {
    margin-left: var(--space-2xs);
    color: var(--muted);
    font-size: 0.75rem;
    font-weight: 400;
  }

  .persona-builder__start-row {
    display: flex;
    align-items: center;
    gap: var(--space-xs);
  }

  .persona-builder__select {
    width: 100%;
  }

  .persona-builder__state {
    flex: 1 1 14rem;
    min-width: 0;
    margin: 0;
  }
</style>
