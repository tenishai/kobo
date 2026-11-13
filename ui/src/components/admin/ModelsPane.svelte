<script lang="ts">
  // Which models New agent offers, and which cloud each one comes from
  // (kobo#405). The offered ones are first and always in view; under them is
  // every model the connected clouds list, one row per model with a tick
  // under each cloud that has it, folded by who publishes it. The page opens
  // on what the room already holds and each cloud's own list arrives on its
  // own, so a slow cloud holds up only its column. Nothing here connects a
  // cloud: that is Integrations.
  import { createMutation, createQueries, createQuery, useQueryClient } from "@tanstack/svelte-query";
  import { CaretDown, CaretRight } from "phosphor-svelte";
  import { errorMessage, type AdminDiscoveredModel, type AdminModelProvider } from "../../lib/api";
  import { groupRows, isOffered, modelRows, type ModelRow } from "../../lib/admin/models";
  import { adminModelListingQuery, adminModelsQuery, putAdminModelsMutation } from "../../lib/queries";
  import { relativeTime } from "../../lib/time";
  import { toast } from "../../lib/toast.svelte";

  // With this few models every publisher is open: there is nothing to fold away.
  const OPEN_ALL_UP_TO = 12;

  const queryClient = useQueryClient();
  const view = createQuery(() => adminModelsQuery());
  const put = createMutation(() => putAdminModelsMutation(queryClient));

  // The providers the first read holds no fresh list for: each is asked on its own.
  const owed = $derived((view.data?.providers ?? []).filter((p) => p.pending).map((p) => p.source));
  const listings = createQueries(() => ({ queries: owed.map((source) => adminModelListingQuery(source)) }));

  type Column = AdminModelProvider & { heading: string; reading: boolean; failed: boolean };
  const columns = $derived.by((): Column[] => {
    const held = view.data?.providers ?? [];
    return held.map((p) => {
      const asked = p.pending ? listings[owed.indexOf(p.source)] : undefined;
      const now = asked?.data?.providers.find((l) => l.source === p.source) ?? p;
      // Two connections of one cloud are told apart by their names.
      const heading = held.filter((o) => o.tag === p.tag).length > 1 ? `${p.tag} ${p.name}` : p.tag;
      return { ...now, heading, reading: asked !== undefined && asked.isPending, failed: asked !== undefined && asked.isError };
    });
  });

  let search = $state("");
  // The one cloud to show ("" is all of them).
  let cloud = $state("");
  // The publishers opened or folded by hand; the rest follow `OPEN_ALL_UP_TO`.
  let opened = $state<Record<string, boolean>>({});
  // The ids whose tick is on its way, so the box reads as busy and not as undone.
  let busy = $state<string[]>([]);

  const needle = $derived(search.trim().toLowerCase());
  function shown(row: ModelRow): boolean {
    if (cloud && !row.cells[cloud]) return false;
    if (!needle) return true;
    const cells = Object.values(row.cells).flatMap((m) => [m.label, m.model]);
    return [row.name, row.publisher, ...cells].some((t) => t.toLowerCase().includes(needle));
  }

  const offered = $derived(groupRows(modelRows(columns).filter(isOffered)).flatMap((g) => g.rows));
  // A row the room holds is not called unlisted while its cloud is still being read.
  const listed = $derived(
    modelRows(columns.map((c) => (c.reading ? { ...c, discovered: c.discovered.filter((m) => m.discovered) } : c))),
  );
  const offeredShown = $derived(offered.filter(shown));
  const groups = $derived(
    groupRows(listed)
      .map((g) => ({ publisher: g.publisher, total: g.rows.length, offered: g.rows.filter(isOffered).length, rows: g.rows.filter(shown) }))
      .filter((g) => g.rows.length > 0),
  );
  const notConnected = $derived((view.data?.not_connected ?? []).map((n) => n.tag));
  // "Reading the AWS and GCP models…" while a cloud's own list is on its way.
  const reading = $derived(columns.filter((c) => c.reading).map((c) => c.heading).join(" and "));
  const asOf = $derived(
    columns.filter((c) => !c.reading && c.fetched_at).map((c) => `${c.heading} read ${relativeTime(c.fetched_at!)}`),
  );

  function isOpen(publisher: string): boolean {
    if (needle) return true;
    return opened[publisher] ?? listed.length <= OPEN_ALL_UP_TO;
  }

  function toggle(model: AdminDiscoveredModel): void {
    const enabled = !model.enabled;
    busy = [...busy, model.id];
    put.mutate([{ id: model.id, enabled }], {
      onSuccess: (out) => {
        const note = out.gateway_result ? `On the model gateway: ${out.gateway_result}.` : undefined;
        toast.success(enabled ? `${model.label} is offered` : `${model.label} is hidden from New agent`, note);
      },
      onError: (err: unknown) => toast.error(`Could not change ${model.label}`, errorMessage(err, "Try again in a moment.")),
      onSettled: () => {
        busy = busy.filter((id) => id !== model.id);
      },
    });
  }

  /** The line under a model's name: the cloud's own id, or the name two clouds share. */
  function idLine(row: ModelRow): string {
    const cells = Object.values(row.cells);
    return cells.length === 1 ? cells[0].model : row.key.slice(row.key.indexOf("|") + 1);
  }
</script>

{#snippet modelRow(row: ModelRow)}
  <tr>
    <td>
      <span class="model__name">{row.name}</span>
      <span class="model__id">{idLine(row)}</span>
    </td>
    {#each columns as c (c.source)}
      {@const m = row.cells[c.source]}
      <td class="col-cloud">
        {#if m}
          <input
            type="checkbox"
            aria-label={`Offer ${row.name} from ${c.heading}`}
            title={m.model}
            checked={m.enabled}
            disabled={busy.includes(m.id)}
            onchange={() => toggle(m)}
          />
          {#if m.label !== row.name}<span class="model__note">as {m.label}</span>{/if}
          {#if !m.discovered && !c.reading}<span class="model__note">Not listed now</span>{/if}
        {/if}
      </td>
    {/each}
  </tr>
{/snippet}

{#snippet tableHead()}
  <thead>
    <tr>
      <th>Model</th>
      {#each columns as c (c.source)}
        <th class="col-cloud" title={[c.name, c.region_or_location].filter(Boolean).join(", ")}>{c.heading}</th>
      {/each}
    </tr>
  </thead>
{/snippet}

<section class="panel page" aria-labelledby="models-heading">
  <div class="page__header">
    <h2 id="models-heading" class="page__title">Models</h2>
    <p class="page__lead">
      Which models New agent offers, and which cloud each one comes from: tick a model under a cloud to offer it from
      there, untick it to hide it.
    </p>
  </div>

  {#if view.isPending}
    <p class="page__foot">Reading the models…</p>
  {:else if view.isError}
    <p class="page__foot">Could not read the models just now.</p>
  {:else if columns.length === 0}
    <div class="table-card"><p class="panel__empty">{view.data.empty_state}</p></div>
  {:else}
    <div class="page__toolbar">
      <input class="field__input page__search" type="search" placeholder="Search models" bind:value={search} aria-label="Search models" />
      {#if columns.length > 1}
        <div class="clouds" role="group" aria-label="Show models from">
          <button type="button" class="clouds__pick" aria-pressed={cloud === ""} onclick={() => (cloud = "")}>All</button>
          {#each columns as c (c.source)}
            <button type="button" class="clouds__pick" aria-pressed={cloud === c.source} onclick={() => (cloud = c.source)}>{c.heading}</button>
          {/each}
        </div>
      {/if}
    </div>

    <section class="models" aria-labelledby="models-offered">
      <h3 id="models-offered" class="panel__subheading">Offered in New agent <span class="models__count">{offered.length}</span></h3>
      <div class="table-card">
        {#if offeredShown.length > 0}
          <table class="panel__table">
            {@render tableHead()}
            <tbody>
              {#each offeredShown as row (row.key)}{@render modelRow(row)}{/each}
            </tbody>
          </table>
        {:else}
          <p class="panel__empty">{offered.length === 0 ? "No model is offered yet. Tick one below." : "No offered model matches that."}</p>
        {/if}
      </div>
    </section>

    <section class="models" aria-labelledby="models-all">
      <h3 id="models-all" class="panel__subheading">All models {#if listed.length > 0}<span class="models__count">{listed.length}</span>{/if}</h3>
      {#each columns as c (c.source)}
        {#if c.error}
          <p class="models__error">Could not list the {c.heading} models just now: {c.error}</p>
        {:else if c.failed}
          <p class="models__error">Could not list the {c.heading} models just now.</p>
        {/if}
      {/each}
      <div class="table-card">
        {#if groups.length > 0}
          <table class="panel__table">
            {@render tableHead()}
            <tbody>
              {#each groups as g (g.publisher)}
                <tr class="publisher">
                  <td colspan={columns.length + 1}>
                    <button type="button" class="publisher__fold" aria-expanded={isOpen(g.publisher)} onclick={() => (opened[g.publisher] = !isOpen(g.publisher))}>
                      {#if isOpen(g.publisher)}<CaretDown size={13} aria-hidden="true" />{:else}<CaretRight size={13} aria-hidden="true" />{/if}
                      <span class="publisher__name">{g.publisher}</span>
                      <span class="publisher__count">{g.total} {g.total === 1 ? "model" : "models"}, {g.offered} offered</span>
                    </button>
                  </td>
                </tr>
                {#if isOpen(g.publisher)}
                  {#each g.rows as row (row.key)}{@render modelRow(row)}{/each}
                {/if}
              {/each}
            </tbody>
          </table>
        {:else if reading}
          <p class="panel__empty" role="status">Reading the {reading} models…</p>
        {:else}
          <p class="panel__empty">{listed.length === 0 ? "No connected cloud lists a model." : "No model matches that."}</p>
        {/if}
      </div>
      {#if reading && groups.length > 0}<p class="page__foot" role="status">Reading the {reading} models…</p>{/if}
      {#if asOf.length > 0}<p class="page__foot">{asOf.join(". ")}.</p>{/if}
    </section>
  {/if}

  {#if view.data}
    {#if notConnected.length > 0}
      <p class="page__foot">Not connected: {notConnected.join(", ")}. Connect a cloud or a self-hosted endpoint under Integrations and its models appear here.</p>
    {/if}
    {#if !view.data.gateway}
      <p class="page__foot">The room is not set to reach models through the model gateway, so New agent offers the room's own list as it is.</p>
    {/if}
  {/if}
</section>

<style>
  .models {
    display: flex;
    flex-direction: column;
    gap: var(--space-xs);
  }

  .models__count {
    margin-left: var(--space-2xs);
    font-weight: 500;
  }

  .models__error {
    margin: 0;
    color: var(--ink);
    font-size: 0.85rem;
  }

  /* The cloud filter: the segmented control the audit log's tabs use. */
  .clouds {
    display: inline-flex;
    gap: 2px;
    padding: 3px;
    border-radius: calc(var(--radius-sm) + 3px);
    background: var(--accent-soft);
  }

  .clouds__pick {
    display: inline-flex;
    align-items: center;
    height: 1.75rem;
    padding: 0 var(--space-sm);
    border: none;
    border-radius: var(--radius-sm);
    background: none;
    color: var(--muted);
    font: inherit;
    font-size: 0.82rem;
    font-weight: 500;
    cursor: pointer;
  }

  .clouds__pick[aria-pressed="true"] {
    background: var(--panel);
    box-shadow: 0 1px 2px rgba(0, 0, 0, 0.06), 0 0 0 1px var(--border);
    color: var(--ink);
  }

  .col-cloud {
    width: 9rem;
  }

  .col-cloud input {
    display: block;
    margin: 0;
  }

  .model__name {
    display: block;
    color: var(--ink);
  }

  .model__id,
  .model__note {
    display: block;
    color: var(--muted);
    font-size: 0.75rem;
  }

  .model__id {
    font-family: var(--font-display-mono);
    overflow-wrap: anywhere;
  }

  .model__note {
    margin-top: 2px;
  }

  .publisher td {
    padding: 0;
    background: var(--accent-soft);
  }

  .publisher__fold {
    display: flex;
    align-items: center;
    gap: var(--space-xs);
    width: 100%;
    padding: var(--space-xs) var(--space-md);
    border: none;
    background: none;
    color: var(--ink);
    font: inherit;
    text-align: left;
    cursor: pointer;
  }

  .publisher__name {
    font-weight: 600;
  }

  .publisher__count {
    color: var(--muted);
    font-size: 0.8rem;
  }
</style>
