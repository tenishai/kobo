<script lang="ts">
  // One pane rendered twice. Add-ons (kind "platform") are what the room
  // runs on: deploy platform, secrets, observability, switchboard, gateway,
  // memory, and add-ons other teams ship. Integrations (kind "vendor") are
  // what an agent reaches out to: one base credential per vendor, held once,
  // with agents scoped below it through Connections and Roles. Connect saves
  // as a pull request to the org config, so a card says "next boot" until
  // the room restarts.
  import { createQuery } from "@tanstack/svelte-query";
  import Modal from "../Modal.svelte";
  import { adminAddonsQuery } from "../../lib/queries";
  import { toast } from "../../lib/toast.svelte";
  import {
    CATALOG,
    addPlugin,
    admin,
    connectPlugin,
    disconnectPlugin,
    plugin,
    type Plugin,
  } from "../../lib/admin/store.svelte";

  let { kind, item = null }: { kind: "platform" | "vendor"; item?: string | null } = $props();

  const COPY = {
    platform: {
      id: "addons",
      heading: "Add-ons",
      lead: "What this room runs on. Each card is one part of the room's own plumbing, from this team or another; absent means off.",
    },
    vendor: {
      id: "integrations",
      heading: "Integrations",
      lead: "What agents reach out to. One base credential per vendor, held once by the room; an agent gets a scoped lane to it under Connections, never wider than the credential.",
    },
  } as const;
  const copy = $derived(COPY[kind]);
  // Add-ons are read from the server (one card per org-key group, state
  // derived there); integrations still come from the in-memory store until
  // their data lane lands (#173 step 3).
  const addons = createQuery(() => ({ ...adminAddonsQuery(), enabled: kind === "platform" }));
  const shown = $derived(kind === "vendor" ? admin.plugins.filter((p) => p.kind === "vendor") : []);
  const addable = $derived(CATALOG.filter((c) => c.kind === kind && !plugin(c.id)));

  let editing = $state<Plugin | null>(null);
  let values = $state<Record<string, string>>({});
  let catalogOpen = $state(false);

  // A deep link `#/admin/<addons|integrations>/<id>` opens that card's form.
  $effect(() => {
    if (item && plugin(item) && editing?.id !== item) {
      openConnect(plugin(item)!);
    }
  });

  function openConnect(p: Plugin): void {
    editing = p;
    values = Object.fromEntries(p.fields.map((f) => [f.key, f.secret ? "" : f.value]));
  }

  function closeConnect(): void {
    editing = null;
  }

  function submitConnect(event: SubmitEvent): void {
    event.preventDefault();
    if (!editing) return;
    const landed = connectPlugin(editing.id, values);
    toast.success(`${editing.name} connected`, `${landed}. Takes effect at the room's next boot.`);
    editing = null;
  }

  function handleDisconnect(p: Plugin): void {
    const landed = disconnectPlugin(p.id);
    toast.success(`${p.name} disconnected`, landed);
  }

  function handleAdd(id: string): void {
    const p = addPlugin(id);
    catalogOpen = false;
    if (p) openConnect(p);
  }

  function stateLabel(p: Plugin): string {
    return p.state === "connected" ? "Connected" : p.state === "error" ? "Error" : "Not configured";
  }
</script>

<section class="panel" aria-labelledby="{copy.id}-heading">
  <div class="panel__toolbar">
    <div class="panel__header">
      <h2 id="{copy.id}-heading" class="panel__heading">{copy.heading}</h2>
      <p class="panel__lead">{copy.lead}</p>
    </div>
    {#if kind === "vendor"}
      <button type="button" class="btn" onclick={() => (catalogOpen = true)} disabled={addable.length === 0}>
        Add integration
      </button>
    {/if}
  </div>

  {#if kind === "platform"}
    {#if addons.isPending}
      <p class="panel__note">Reading what the room runs on…</p>
    {:else if addons.isError}
      <p class="panel__note">Could not read the add-ons just now.</p>
    {:else}
      <div class="cards">
        {#each addons.data ?? [] as a (a.id)}
          <article class="card" aria-labelledby="addon-{a.id}">
            <div class="card__head">
              <h4 id="addon-{a.id}" class="card__name">{a.name}</h4>
              <span class="dot dot--{a.state}" title={a.state} aria-label={a.state}></span>
            </div>
            <p class="card__summary">{a.summary}</p>
            <p class="card__detail" class:card__detail--fail={a.state === "error"}>{a.detail}</p>
            <p class="card__fields">Set by {a.keys.join(", ")} · from the {a.owner}</p>
          </article>
        {/each}
      </div>
      <p class="panel__note">
        An add-on is turned on in the org config; this pane shows the result. Editing from here
        comes with Integrations.
      </p>
    {/if}
  {:else}
  <div class="cards">
    {#each shown as p (p.id)}
      <article class="card" aria-labelledby="plugin-{p.id}">
        <div class="card__head">
          <h4 id="plugin-{p.id}" class="card__name">{p.name}</h4>
          {#if p.baseline}<span class="list__tag">baseline</span>{/if}
          <span class="dot dot--{p.state}" title={stateLabel(p)} aria-label={stateLabel(p)}></span>
        </div>
        <p class="card__summary">{p.summary}</p>
        <p class="card__fields">
          {p.fields.map((f) => f.label).join(" · ")}
          {#if p.credential}
            · credential: {p.credential}
          {/if}
        </p>
        <p class="card__detail" class:card__detail--fail={p.state === "error"}>{p.detail}</p>
        <div class="card__actions">
          {#if p.required}
            <span class="card__note">Required for Create. Set in the deployment config.</span>
          {:else if p.state === "connected"}
            {#if kind === "vendor"}
              <span class="card__note">Lanes: {p.lanes.join(", ")}</span>
            {/if}
            <button type="button" class="btn btn--quiet" onclick={() => openConnect(p)}>Edit</button>
            {#if !p.baseline}
              <button type="button" class="btn btn--quiet btn--danger" onclick={() => handleDisconnect(p)}>Disconnect</button>
            {/if}
          {:else}
            <button type="button" class="btn btn--primary" onclick={() => openConnect(p)}>Connect</button>
          {/if}
        </div>
      </article>
    {/each}
  </div>
  {/if}
</section>

<Modal open={editing !== null} title={editing ? `Connect ${editing.name}` : ""} onclose={closeConnect}>
  {#if editing}
    <form id="plugin-form" class="form" onsubmit={submitConnect}>
      <p class="panel__lead">{editing.summary}</p>
      {#each editing.fields as f (f.key)}
        <label class="field">
          <span>{f.label}</span>
          <input
            id="plugin-{editing.id}-{f.key}"
            type={f.secret ? "password" : "text"}
            bind:value={values[f.key]}
            placeholder={f.secret && f.value ? "stored; leave blank to keep" : ""}
            autocomplete="off"
          />
        </label>
      {/each}
      <p class="panel__note">
        Saved as a pull request to the org config repository. Nothing here is written to the
        cluster; the room reads the new values at its next boot.
      </p>
    </form>
  {/if}
  {#snippet footer()}
    <button type="button" class="btn" onclick={closeConnect}>Cancel</button>
    <button type="submit" form="plugin-form" class="btn btn--primary">Save as pull request</button>
  {/snippet}
</Modal>

<Modal open={catalogOpen} title="Add an integration" onclose={() => (catalogOpen = false)}>
  <p class="panel__lead">One base credential per vendor. Agents are scoped below it.</p>
  <ul class="list">
    {#each addable as c (c.id)}
      <li class="list__row">
        <div>
          <span class="list__name">{c.name}</span>
          <p class="list__desc">{c.summary}</p>
        </div>
        <button type="button" class="btn" onclick={() => handleAdd(c.id)}>Add</button>
      </li>
    {/each}
  </ul>
</Modal>

<style>
  .cards {
    display: grid;
    grid-template-columns: repeat(auto-fill, minmax(17rem, 1fr));
    gap: var(--space-sm);
  }

  .card {
    display: flex;
    flex-direction: column;
    gap: var(--space-2xs);
    padding: var(--space-sm) var(--space-md);
    border: 1px solid var(--border);
    border-radius: var(--radius-sm);
  }

  .card__head {
    display: flex;
    align-items: center;
    gap: var(--space-xs);
  }

  .card__name {
    flex: 1;
    min-width: 0;
    margin: 0;
    font-size: 0.95rem;
  }

  .dot {
    width: 10px;
    height: 10px;
    border: 1px solid var(--muted);
    border-radius: 50%;
    flex-shrink: 0;
  }

  .dot--connected,
  .dot--on {
    border-color: var(--ok);
    background: var(--ok);
  }

  .dot--error,
  .dot--error {
    border-color: var(--fail);
    background: var(--fail);
  }

  .card__summary,
  .card__fields,
  .card__detail {
    margin: 0;
    font-size: 0.8rem;
  }

  .card__summary {
    color: var(--ink);
  }

  .card__fields {
    color: var(--muted);
  }

  .card__detail {
    color: var(--muted);
  }

  .card__detail--fail {
    color: var(--fail);
  }

  .card__actions {
    display: flex;
    flex-wrap: wrap;
    align-items: center;
    gap: var(--space-xs);
    margin-top: var(--space-2xs);
  }

  .card__note {
    flex: 1;
    min-width: 0;
    color: var(--muted);
    font-size: 0.75rem;
  }

  .form {
    display: flex;
    flex-direction: column;
    gap: var(--space-sm);
  }

  .field input {
    padding: var(--space-2xs) var(--space-xs);
    border: 1px solid var(--border);
    border-radius: var(--radius-sm);
    background: var(--bg);
    color: var(--ink);
    font: inherit;
  }

  .list__row {
    display: flex;
    align-items: center;
    justify-content: space-between;
    gap: var(--space-sm);
  }
</style>
