<script lang="ts">
  // What this room is wired to. Platforms are the room's own plumbing;
  // vendors are one base credential each, held once, with agents scoped
  // below it through connections and roles. Connect saves as a pull request
  // to the org config, so a card says "next boot" until the room restarts.
  import Modal from "../Modal.svelte";
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

  let { item = null }: { item?: string | null } = $props();

  const platforms = $derived(admin.plugins.filter((p) => p.kind === "platform"));
  const vendors = $derived(admin.plugins.filter((p) => p.kind === "vendor"));
  const addable = $derived(CATALOG.filter((c) => !plugin(c.id)));

  let editing = $state<Plugin | null>(null);
  let values = $state<Record<string, string>>({});
  let catalogOpen = $state(false);

  // A deep link `#/admin/plugins/<id>` opens that card's form.
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

<section class="panel" aria-labelledby="plugins-heading">
  <div class="panel__header">
    <h2 id="plugins-heading" class="panel__heading">Plugins</h2>
    <p class="panel__lead">
      What this room is wired to. A vendor is one base credential, held once; agents get a scoped
      lane to it under Connections. Absent means off.
    </p>
  </div>

  <h3 class="panel__subheading">Platforms</h3>
  <div class="cards">
    {#each platforms as p (p.id)}
      <article class="card" aria-labelledby="plugin-{p.id}">
        <div class="card__head">
          <h4 id="plugin-{p.id}" class="card__name">{p.name}</h4>
          <span class="dot dot--{p.state}" title={stateLabel(p)} aria-label={stateLabel(p)}></span>
        </div>
        <p class="card__summary">{p.summary}</p>
        <p class="card__fields">{p.fields.map((f) => f.label).join(" · ")}</p>
        <p class="card__detail" class:card__detail--fail={p.state === "error"}>{p.detail}</p>
        <div class="card__actions">
          {#if p.required}
            <span class="card__note">Required for Create. Set in the deployment config.</span>
          {:else if p.state === "connected"}
            <button type="button" class="btn btn--quiet" onclick={() => openConnect(p)}>Edit</button>
            <button type="button" class="btn btn--quiet btn--danger" onclick={() => handleDisconnect(p)}>Disconnect</button>
          {:else}
            <button type="button" class="btn btn--primary" onclick={() => openConnect(p)}>Connect</button>
          {/if}
        </div>
      </article>
    {/each}
  </div>

  <div class="panel__toolbar">
    <h3 class="panel__subheading">Vendors</h3>
    <button type="button" class="btn" onclick={() => (catalogOpen = true)} disabled={addable.length === 0}>
      Add plugin
    </button>
  </div>
  <div class="cards">
    {#each vendors as p (p.id)}
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
          {#if p.state === "connected"}
            <span class="card__note">Lanes: {p.lanes.join(", ")}</span>
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

<Modal open={catalogOpen} title="Add a plugin" onclose={() => (catalogOpen = false)}>
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

  .dot--connected {
    border-color: var(--ok);
    background: var(--ok);
  }

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
