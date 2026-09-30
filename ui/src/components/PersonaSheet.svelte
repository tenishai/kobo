<script lang="ts">
  // The whole of one persona, read where you are choosing it.
  //
  // Personas-in-place wave (2026-09-30): the Personas page is gone. It was
  // a long list of truncated cards whose only actions were Move and
  // Archive, and the hover card's "open the full persona" link led to it
  // and stopped -- nowhere in the room showed a persona in full. This sheet
  // is swapped in over the create dialog's persona list, the way
  // PersonaBuilder is, and shows everything: who they are, all of what they
  // do, every rule, who made it. Back returns to the list with the
  // selection untouched.
  //
  // Manage sits at the bottom for the people who may: Move to another
  // group (the server says which personas the caller may re-file,
  // `movablePersonasQuery`), and Archive (a maker, a member of its group,
  // or an operator; built-ins and shared ones are the operator's alone).
  // The archive gate is the server's; the row shows for anyone the move
  // list names or who runs the room, and a refusal's own `{error}` copy
  // becomes the toast. There is no unarchive here: bringing one back is an
  // operator's API call, on purpose a rarer thing than the room's chrome
  // should carry.
  import { createMutation, createQuery, useQueryClient } from "@tanstack/svelte-query";
  import Skeleton from "./Skeleton.svelte";
  import { errorMessage } from "../lib/api";
  import {
    archivePersonaMutation,
    groupsQuery,
    movablePersonasQuery,
    movePersonaMutation,
    personaQuery,
    whoamiQuery,
  } from "../lib/queries";
  import { relativeTime } from "../lib/time";
  import { toast } from "../lib/toast.svelte";
  import { personaTeamOptions } from "../lib/validate";

  let {
    personaId,
    onback,
    onarchived = () => {},
  }: {
    personaId: string;
    /** Back to the list. */
    onback: () => void;
    /** The persona was archived: the caller drops it if it was selected. */
    onarchived?: (personaId: string) => void;
  } = $props();

  const queryClient = useQueryClient();
  const persona = createQuery(() => personaQuery(personaId));

  const SHARED_TEAM = "shared";
  const whoami = createQuery(() => whoamiQuery());
  const roomGroups = createQuery(() => ({
    ...groupsQuery(),
    enabled: whoami.data?.operator === true,
  }));
  const movable = createQuery(() => movablePersonasQuery());
  const canMove = $derived((movable.data ?? []).includes(personaId));
  const canManage = $derived(canMove || whoami.data?.operator === true);
  const teamOptions = $derived(personaTeamOptions(whoami.data, roomGroups.data));
  const moveChoices = $derived(
    [...teamOptions, SHARED_TEAM].filter((t) => t !== persona.data?.team),
  );

  const moveMutation = createMutation(() => movePersonaMutation(queryClient));
  const archiveMutation = createMutation(() => archivePersonaMutation(queryClient));

  function handleMove(select: HTMLSelectElement): void {
    const team = select.value;
    // The select is a menu, not a field: it always rests on "Move to...".
    select.value = "";
    if (team === "") {
      return;
    }
    const name = persona.data?.name ?? personaId;
    moveMutation.mutate(
      { id: personaId, team },
      {
        onSuccess: () => {
          toast.success(`Moved ${name} to ${team === SHARED_TEAM ? "everyone" : team}`);
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

  function handleArchive(): void {
    const name = persona.data?.name ?? personaId;
    archiveMutation.mutate(personaId, {
      onSuccess: () => {
        toast.success(`Archived ${name}`);
        onarchived(personaId);
        onback();
      },
      onError: (error: unknown) => {
        toast.error(
          "Couldn't archive that persona",
          errorMessage(error, "Something went wrong. Try again."),
        );
      },
    });
  }

  /** Plain paragraphs, never markdown: the text is prose somebody wrote. */
  const paragraphs = $derived(
    (persona.data?.what_they_do ?? "")
      .split(/\n\s*\n/)
      .map((part) => part.trim())
      .filter((part) => part.length > 0),
  );

  const footer = $derived.by(() => {
    const data = persona.data;
    if (!data?.created_by) {
      return null;
    }
    const when = data.created_at ? ` · ${relativeTime(data.created_at)}` : "";
    return `Made by ${data.created_by}${when}`;
  });
</script>

<div class="sheet">
  <button type="button" class="btn--link sheet__back" onclick={onback}>
    ← Back to personas
  </button>

  {#if persona.isPending}
    <Skeleton rows={3} />
  {:else if persona.isError}
    <p class="sheet__quiet">Could not load this persona.</p>
  {:else}
    <div class="sheet__head">
      <span class="sheet__name">{persona.data.name}</span>
      <span class="sheet__tags">
        <span class="sheet__tag sheet__tag--team">
          {persona.data.builtin ? "Built-in" : persona.data.team}
        </span>
        {#each [...new Set(persona.data.tags)] as tag (tag)}
          <span class="sheet__tag">{tag}</span>
        {/each}
      </span>
    </div>

    <p class="sheet__heading">Who they are</p>
    <p class="sheet__line">{persona.data.who_they_are}</p>

    <p class="sheet__heading">What they do</p>
    {#each paragraphs as para, index (index)}
      <p class="sheet__para">{para}</p>
    {/each}

    {#if persona.data.rules.length > 0}
      <p class="sheet__heading">Rules</p>
      <ul class="sheet__rules">
        {#each persona.data.rules as rule, index (index)}
          <li>{rule}</li>
        {/each}
      </ul>
    {/if}

    {#if footer}
      <p class="sheet__footer">{footer}</p>
    {/if}
  {/if}

  <!-- The sheet's own footer, in place of the dialog's while it is up
       (same idiom as PersonaBuilder). -->
  <div class="modal__footer modal__footer--inline">
    <div class="sheet__manage">
      {#if canManage && persona.data}
        {#if canMove && moveChoices.length > 0}
          <select
            class="field__input sheet__move"
            aria-label="Move to"
            disabled={moveMutation.isPending}
            onchange={(event) => handleMove(event.currentTarget)}
          >
            <option value="">Move to...</option>
            {#each moveChoices as team (team)}
              <option value={team}>{team === SHARED_TEAM ? "Everyone (shared)" : team}</option>
            {/each}
          </select>
        {/if}
        <button
          type="button"
          class="btn btn--quiet"
          disabled={archiveMutation.isPending}
          onclick={handleArchive}
        >
          Archive
        </button>
      {/if}
    </div>
    <button type="button" class="btn btn--quiet" onclick={onback}>Back</button>
  </div>
</div>

<style>
  .sheet {
    display: flex;
    flex-direction: column;
    gap: var(--space-2xs);
    font-size: 0.85rem;
  }

  .sheet__back {
    align-self: flex-start;
    margin-bottom: var(--space-xs);
  }

  .sheet__head {
    display: flex;
    align-items: center;
    flex-wrap: wrap;
    gap: var(--space-2xs);
  }

  .sheet__name {
    color: var(--ink);
    font-family: var(--font-mono);
    font-size: 0.95rem;
    font-weight: 600;
  }

  .sheet__tags {
    display: flex;
    flex-wrap: wrap;
    gap: var(--space-2xs);
  }

  .sheet__tag {
    padding: 0.1rem var(--space-2xs);
    border: 1px solid var(--border);
    border-radius: 999px;
    color: var(--muted);
    font-size: 0.7rem;
    white-space: nowrap;
  }

  .sheet__tag--team {
    border-color: transparent;
    background: var(--accent-soft);
    color: var(--ink);
  }

  .sheet__heading {
    margin: var(--space-sm) 0 0;
    color: var(--muted);
    font-size: 0.7rem;
    font-weight: 600;
    letter-spacing: 0.04em;
    text-transform: uppercase;
  }

  .sheet__line,
  .sheet__para {
    margin: 0 0 var(--space-2xs);
    color: var(--ink);
    white-space: pre-wrap;
  }

  .sheet__rules {
    margin: 0;
    padding-left: 1.1rem;
    color: var(--ink);
  }

  .sheet__quiet {
    margin: 0;
    color: var(--muted);
  }

  .sheet__footer {
    margin: var(--space-sm) 0 0;
    padding-top: var(--space-2xs);
    border-top: 1px solid var(--border);
    color: var(--muted);
    font-size: 0.75rem;
  }

  .sheet__manage {
    display: flex;
    flex: 1;
    align-items: center;
    gap: var(--space-xs);
  }

  .sheet__move {
    width: auto;
  }
</style>
