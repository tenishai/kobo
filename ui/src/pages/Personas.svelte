<script lang="ts">
  // The persona catalog (hardening wave): the naming law made visible. Lists
  // active personas via the existing unfiltered search (`personasSearchQuery`,
  // the same query CreateModal warms) as cards showing the DISPLAY
  // HIERARCHY the naming law is about -- the persona's NAME is the primary
  // line, `who_they_are`/`description` the grey secondary line directly
  // beneath, team and tags as chips, a builtin badge where true.
  //
  // Archived personas are already excluded from that search server-side
  // (the catalog ingest drops them -- see api.ts's own doc comment on
  // `searchPersonas`), so the active list needs no client-side filtering at
  // all. A "Show archived" toggle reveals a second section fed by
  // `archivedPersonasQuery`, fetched lazily behind that toggle -- the same
  // "don't fetch until a human actually asks to see it" idiom
  // `GroupRequestsCard`'s own expand/collapse toggle and `slackManifestQuery`'s
  // `opened` flag both already use in this room. Its cards carry Unarchive
  // instead of Archive.
  //
  // The archive/unarchive gate (builtin or team "shared": operator only; any
  // other team: a member of that team, or an operator) is enforced entirely
  // server-side -- every card shows both kinds of controls to everyone; a
  // refusal's exact `{error}` copy (naming the actual requirement) surfaces
  // as an error toast, the house idiom (toast.svelte.ts) AgentDetail's own
  // retry/avatar failures already use.
  //
  // "Move to..." (persona team scope): a persona the caller may re-file --
  // they made it, they share its team, or they run the room; the server
  // says which (`movablePersonasQuery`, never recomputed here) -- carries a
  // small select of the caller's groups plus "Everyone (shared)", minus the
  // team it already sits under. Picking one PATCHes just the team. This page
  // lists every group (scope "all"): it is the whole catalog, where the
  // create dialog's list is the short one. Built-ins lead under their own
  // heading, with every team persona under "From all groups" beneath them.
  import { createMutation, createQuery, useQueryClient } from "@tanstack/svelte-query";
  import EmptyState from "../components/EmptyState.svelte";
  import ErrorState from "../components/ErrorState.svelte";
  import Skeleton from "../components/Skeleton.svelte";
  import { errorMessage, type ArchivedPersona, type CatalogEntry } from "../lib/api";
  import {
    archivePersonaMutation,
    archivedPersonasQuery,
    groupsQuery,
    movablePersonasQuery,
    movePersonaMutation,
    personasSearchQuery,
    unarchivePersonaMutation,
    whoamiQuery,
  } from "../lib/queries";
  import { toast } from "../lib/toast.svelte";
  import { personaTeamOptions } from "../lib/validate";

  const queryClient = useQueryClient();
  const active = createQuery(() => personasSearchQuery("", null, "all"));

  const SHARED_TEAM = "shared";
  const whoami = createQuery(() => whoamiQuery());
  const roomGroups = createQuery(() => ({
    ...groupsQuery(),
    enabled: whoami.data?.operator === true,
  }));
  const movable = createQuery(() => movablePersonasQuery());
  const movableIds = $derived(new Set(movable.data ?? []));
  const teamOptions = $derived(personaTeamOptions(whoami.data, roomGroups.data));
  const moveMutation = createMutation(() => movePersonaMutation(queryClient));

  /** Where this persona could go: the caller's groups, then everyone,
   * minus where it already is. */
  function moveChoicesFor(persona: CatalogEntry): string[] {
    return [...teamOptions, SHARED_TEAM].filter((t) => t !== persona.team);
  }

  function handleMove(persona: CatalogEntry, select: HTMLSelectElement): void {
    const team = select.value;
    // The select is a menu, not a field: it always rests on "Move to...".
    select.value = "";
    if (team === "") {
      return;
    }
    moveMutation.mutate(
      { id: persona.id, team },
      {
        onSuccess: () => {
          toast.success(
            `Moved ${persona.name} to ${team === SHARED_TEAM ? "everyone" : team}`,
          );
        },
        onError: (error: unknown) => {
          toast.error(
            "Couldn't move that persona",
            errorMessage(error, "Something went wrong. Try again."),
          );
        },
      },
    );
  }

  // The API sends built-ins first; the split keeps each group in that order.
  const personaGroups = $derived(
    [
      { heading: "Built-in", personas: (active.data ?? []).filter((p) => p.builtin) },
      {
        heading: "From all groups",
        personas: (active.data ?? []).filter((p) => !p.builtin),
      },
    ].filter((group) => group.personas.length > 0),
  );

  let showArchived = $state(false);
  const archived = createQuery(() => (archivedPersonasQuery(showArchived)));

  function toggleArchived(): void {
    showArchived = !showArchived;
  }

  const archiveMutation = createMutation(() => (archivePersonaMutation(queryClient)));
  const unarchiveMutation = createMutation(() => (unarchivePersonaMutation(queryClient)));

  function handleArchive(id: string): void {
    archiveMutation.mutate(id, {
      onError: (error: unknown) => {
        toast.error(
          "Couldn't archive that persona",
          errorMessage(error, "Something went wrong. Try again."),
        );
      },
    });
  }

  function handleUnarchive(id: string): void {
    unarchiveMutation.mutate(id, {
      onError: (error: unknown) => {
        toast.error(
          "Couldn't unarchive that persona",
          errorMessage(error, "Something went wrong. Try again."),
        );
      },
    });
  }
</script>

<div class="personas-page">
  <div class="personas-page__header">
    <h1 class="personas-page__title">Personas</h1>
    <button
      type="button"
      class="personas-page__toggle"
      onclick={toggleArchived}
      aria-expanded={showArchived}
    >
      {showArchived ? "Hide archived" : "Show archived"}
    </button>
  </div>

  {#if active.isPending}
    <Skeleton rows={3} />
  {:else if active.isError}
    <ErrorState
      message={errorMessage(active.error, "Couldn't load personas.")}
      onretry={() => active.refetch()}
    />
  {:else if active.data.length === 0}
    <EmptyState message="No personas yet." />
  {:else}
    <!-- Built-ins first (operator ruling): they are the main set, and the
         team personas sit under them. A group with nothing in it drops
         its heading. -->
    {#each personaGroups as group (group.heading)}
      <section class="personas-page__section">
        <h2 class="personas-page__group">{group.heading}</h2>
        <ul class="persona-list">
          {#each group.personas as persona (persona.id)}
            {@render personaCard(persona, "archive")}
          {/each}
        </ul>
      </section>
    {/each}
  {/if}

  {#if showArchived}
    <div class="personas-page__archived">
      <h2 class="personas-page__archived-title">Archived</h2>
      {#if archived.isPending}
        <Skeleton rows={2} />
      {:else if archived.isError}
        <ErrorState
          message={errorMessage(archived.error, "Couldn't load archived personas.")}
          onretry={() => archived.refetch()}
        />
      {:else if archived.data.length === 0}
        <EmptyState message="No archived personas." />
      {:else}
        <ul class="persona-list">
          {#each archived.data as persona (persona.id)}
            {@render personaCard(persona, "unarchive")}
          {/each}
        </ul>
      {/if}
    </div>
  {/if}
</div>

{#snippet personaCard(persona: CatalogEntry | ArchivedPersona, mode: "archive" | "unarchive")}
  <li class="persona-card">
    <div class="persona-card__body">
      <div class="persona-card__head">
        <span class="persona-card__name">{persona.name}</span>
        {#if persona.builtin}
          <span class="persona-card__badge persona-card__badge--builtin">builtin</span>
        {/if}
      </div>
      <p class="persona-card__description">{persona.description}</p>
      <div class="persona-card__chips">
        <span class="persona-card__chip">{persona.team}</span>
        <!-- Set, not the raw list: the chips are keyed by the tag string, so
             one repeated tag from the server (as the pre-naming-law
             audit-check persona proved live, 2026-08-28) would throw
             each_key_duplicate and take the WHOLE list down with it. The
             server now dedupes at ingest and read; this guard keeps a render
             from ever dying on data anyway. -->
        {#each [...new Set(persona.tags)] as tag (tag)}
          <span class="persona-card__chip">{tag}</span>
        {/each}
      </div>
    </div>
    {#if mode === "archive"}
      {@const moveChoices = moveChoicesFor(persona)}
      <!-- Never on a built-in, whatever the movable list says: built-ins
           belong to everyone and stay where they are. -->
      {#if !persona.builtin && movableIds.has(persona.id) && moveChoices.length > 0}
        <select
          class="persona-card__action persona-card__move"
          aria-label={`Move ${persona.name} to`}
          value=""
          disabled={moveMutation.isPending}
          onchange={(event) => handleMove(persona, event.currentTarget)}
        >
          <option value="" disabled>Move to...</option>
          {#each moveChoices as team (team)}
            <option value={team}>
              {team === SHARED_TEAM ? "Everyone (shared)" : team}
            </option>
          {/each}
        </select>
      {/if}
      <button
        type="button"
        class="persona-card__action"
        disabled={archiveMutation.isPending}
        onclick={() => handleArchive(persona.id)}
      >
        Archive
      </button>
    {:else}
      <button
        type="button"
        class="persona-card__action"
        disabled={unarchiveMutation.isPending}
        onclick={() => handleUnarchive(persona.id)}
      >
        Unarchive
      </button>
    {/if}
  </li>
{/snippet}

<style>
  .personas-page {
    display: flex;
    flex-direction: column;
    gap: var(--space-lg);
  }

  .personas-page__header {
    display: flex;
    align-items: center;
    justify-content: space-between;
    gap: var(--space-sm);
  }

  .personas-page__title {
    margin: 0;
    color: var(--ink);
  }

  .personas-page__toggle {
    padding: var(--space-2xs) var(--space-sm);
    border: 1px solid var(--border);
    border-radius: var(--radius-sm);
    background: var(--panel);
    color: var(--ink);
    font-size: 0.85rem;
    cursor: pointer;
  }

  .personas-page__toggle:hover {
    border-color: var(--accent);
    color: var(--accent);
  }

  .personas-page__section {
    display: flex;
    flex-direction: column;
    gap: var(--space-xs);
  }

  .personas-page__group {
    margin: 0;
    color: var(--muted);
    font-size: 0.8rem;
    font-weight: 600;
  }

  .personas-page__archived {
    display: flex;
    flex-direction: column;
    gap: var(--space-sm);
    padding-top: var(--space-md);
    border-top: 1px solid var(--border);
  }

  .personas-page__archived-title {
    margin: 0;
    color: var(--muted);
    font-size: 0.9rem;
  }

  .persona-list {
    display: flex;
    flex-direction: column;
    gap: var(--space-sm);
    margin: 0;
    padding: 0;
    list-style: none;
  }

  .persona-card {
    display: flex;
    align-items: flex-start;
    gap: var(--space-sm);
    padding: var(--space-md);
    border: 1px solid var(--border);
    border-radius: var(--radius);
    background: var(--panel);
  }

  .persona-card__body {
    display: flex;
    flex: 1;
    flex-direction: column;
    gap: 0.2rem;
    min-width: 0;
  }

  .persona-card__head {
    display: flex;
    align-items: center;
    gap: var(--space-xs);
  }

  /* The naming law's display hierarchy: the NAME is the primary line here
     (ink-toned, bold) -- `.persona-card__description` right beneath it is
     the grey secondary line, never a co-equal title. */
  .persona-card__name {
    color: var(--ink);
    font-weight: 600;
    font-size: 0.95rem;
  }

  .persona-card__description {
    margin: 0;
    color: var(--muted);
    font-size: 0.85rem;
  }

  .persona-card__chips {
    display: flex;
    flex-wrap: wrap;
    gap: var(--space-2xs);
    margin-top: var(--space-2xs);
  }

  .persona-card__chip,
  .persona-card__badge {
    display: inline-flex;
    padding: 0.1rem var(--space-2xs);
    border: 1px solid var(--border);
    border-radius: 999px;
    color: var(--muted);
    font-size: 0.7rem;
    white-space: nowrap;
  }

  .persona-card__badge--builtin {
    border-color: transparent;
    background: var(--accent-soft);
    color: var(--ink);
  }

  .persona-card__action {
    flex-shrink: 0;
    padding: var(--space-2xs) var(--space-sm);
    border: 1px solid var(--border);
    border-radius: var(--radius-sm);
    background: var(--panel);
    color: var(--ink);
    font-weight: 600;
    cursor: pointer;
    white-space: nowrap;
  }

  .persona-card__action:hover:not(:disabled) {
    border-color: var(--accent);
    color: var(--accent);
  }

  /* The move menu reads as a quiet button, not a form field. */
  .persona-card__move {
    font-weight: 500;
    color: var(--muted);
  }

  .persona-card__action:disabled {
    opacity: 0.6;
    cursor: not-allowed;
  }
</style>
