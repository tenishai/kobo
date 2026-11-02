<script lang="ts">
  // Which models New agent offers, and where each comes from (kobo#405).
  // One section per connected provider with the models the cloud lists;
  // a tick offers a model, an untick hides it. The room's own list seeds
  // the first ticks; after that this page is the choice. Nothing here
  // connects a cloud: that is Integrations.
  import { createMutation, createQuery, useQueryClient } from "@tanstack/svelte-query";
  import { errorMessage, type AdminDiscoveredModel } from "../../lib/api";
  import { toast } from "../../lib/toast.svelte";
  import { adminModelsQuery, putAdminModelsMutation } from "../../lib/queries";

  const queryClient = useQueryClient();
  const view = createQuery(() => (adminModelsQuery()));
  const put = createMutation(() => (putAdminModelsMutation(queryClient)));

  let search = $state("");
  // The ids whose tick is on its way, so the box reads as busy and not as undone.
  let pending = $state<string[]>([]);

  const needle = $derived(search.trim().toLowerCase());
  function matches(m: AdminDiscoveredModel): boolean {
    if (!needle) return true;
    return [m.label, m.model, m.publisher ?? "", m.via ?? ""].some((t) => t.toLowerCase().includes(needle));
  }
  const sections = $derived((view.data?.providers ?? []).map((p) => ({ ...p, shown: p.discovered.filter(matches) })));
  const notConnected = $derived((view.data?.not_connected ?? []).map((n) => n.tag));

  function toggle(model: AdminDiscoveredModel): void {
    const enabled = !model.enabled;
    pending = [...pending, model.id];
    put.mutate([{ id: model.id, enabled }], {
      onSuccess: (out) => {
        const note = out.gateway_result ? `On the model gateway: ${out.gateway_result}.` : undefined;
        toast.success(enabled ? `${model.label} is offered` : `${model.label} is hidden from New agent`, note);
      },
      onError: (err: unknown) => toast.error(`Could not change ${model.label}`, errorMessage(err, "Try again in a moment.")),
      onSettled: () => {
        pending = pending.filter((id) => id !== model.id);
      },
    });
  }

  function viaLabel(via: string | null): string {
    if (via === "inference-profile") return "profile";
    if (via === "on-demand") return "on demand";
    return via ?? "";
  }
</script>

<section class="panel page" aria-labelledby="models-heading">
  <div class="page__header">
    <h2 id="models-heading" class="page__title">Models</h2>
    <p class="page__lead">
      Which models New agent offers, and where each comes from. Tick a model to offer it; untick it to hide it. The
      list under each provider is what that cloud or endpoint has right now.
    </p>
  </div>

  <div class="page__toolbar">
    <input class="field__input page__search" type="search" placeholder="Search models" bind:value={search} aria-label="Search models" />
  </div>

  {#if view.isPending}
    <p class="page__foot">Reading the models…</p>
  {:else if view.isError}
    <p class="page__foot">Could not read the models just now.</p>
  {:else if sections.length === 0}
    <div class="table-card"><p class="panel__empty">{view.data.empty_state}</p></div>
  {:else}
    {#each sections as p (p.source)}
      <article class="provider" aria-label={`${p.tag} ${p.name}`}>
        <header class="provider__head">
          <span class="provider__title">
            <span class="chip">{p.tag}</span>
            <span class="provider__name">{p.name}</span>
            {#if p.region_or_location}<span class="provider__place">{p.region_or_location}</span>{/if}
          </span>
          <span class="provider__count">{p.discovered.length} discovered, {p.offered.length} offered</span>
        </header>
        {#if p.error}
          <p class="provider__error">Could not list this provider's models just now: {p.error}</p>
        {/if}
        <div class="table-card">
          <table class="panel__table">
            <thead>
              <tr><th class="col-tick"><span class="sr-only">Offer</span></th><th>Model</th><th>Publisher</th><th>Reached</th><th>In New agent</th></tr>
            </thead>
            <tbody>
              {#each p.shown as m (m.id)}
                <tr>
                  <td class="col-tick">
                    <input
                      type="checkbox"
                      aria-label={`Offer ${m.label}`}
                      checked={m.enabled}
                      disabled={pending.includes(m.id)}
                      onchange={() => toggle(m)}
                    />
                  </td>
                  <td>
                    <span class="model__label">{m.label}</span>
                    {#if m.model !== m.label}<span class="model__id">{m.model}</span>{/if}
                    {#if !m.discovered}<span class="model__note">Not in the provider's list right now</span>{/if}
                  </td>
                  <td class="model__meta">{m.publisher ?? ""}</td>
                  <td class="model__meta">{viaLabel(m.via)}</td>
                  <td>
                    {#if m.enabled}
                      <span class="status status--on"><span class="status__dot" aria-hidden="true"></span>Offered</span>
                    {:else}
                      <span class="status status--off"><span class="status__dot" aria-hidden="true"></span>Hidden from New agent</span>
                    {/if}
                  </td>
                </tr>
              {:else}
                <tr><td colspan="5"><p class="panel__empty">{p.discovered.length === 0 ? "This provider lists no models." : "No model matches that."}</p></td></tr>
              {/each}
            </tbody>
          </table>
        </div>
      </article>
    {/each}
  {/if}

  {#if view.data}
    {#if notConnected.length > 0}
      <p class="page__foot">Not connected: {notConnected.join(", ")}. Connect a cloud or a self-hosted endpoint under Integrations and its models appear here.</p>
    {/if}
    {#if view.data.org_list.length > 0}
      <p class="page__foot">
        The room's own list, which seeded the first ticks: {view.data.org_list.map((m) => (m.tier ? `${m.label} (${m.tier})` : m.label)).join(", ")}.
      </p>
    {/if}
    {#if !view.data.gateway}
      <p class="page__foot">The room is not set to reach models through the model gateway, so New agent offers the room's own list as it is.</p>
    {/if}
  {/if}
</section>

<style>
  .provider {
    display: flex;
    flex-direction: column;
    gap: var(--space-xs);
  }

  .provider__head {
    display: flex;
    flex-wrap: wrap;
    align-items: baseline;
    justify-content: space-between;
    gap: var(--space-sm);
  }

  .provider__title {
    display: inline-flex;
    flex-wrap: wrap;
    align-items: baseline;
    gap: var(--space-xs);
  }

  .provider__name {
    color: var(--ink);
    font-weight: 600;
  }

  .provider__place,
  .provider__count,
  .model__meta {
    color: var(--muted);
    font-size: 0.8rem;
  }

  .provider__error {
    margin: 0;
    color: var(--ink);
    font-size: 0.85rem;
  }

  .col-tick {
    width: 2.5rem;
  }

  .col-tick input {
    margin: 0;
  }

  .model__label {
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
  }

  .sr-only {
    position: absolute;
    width: 1px;
    height: 1px;
    overflow: hidden;
    clip: rect(0 0 0 0);
    white-space: nowrap;
  }
</style>
