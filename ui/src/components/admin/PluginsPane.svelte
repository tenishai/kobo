<script lang="ts">
  // One pane rendered twice. Add-ons (kind "platform") are what the room
  // runs on, read from the server. Integrations (kind "vendor") are what an
  // agent reaches out to: one base credential per vendor, held once by the
  // room in the secrets add-on and served through the gateway. Connect
  // stores the credential, opens the gateway pull request and records the
  // row; the card shows how far that has gone (#173 step 3).
  import { createMutation, createQuery, useQueryClient } from "@tanstack/svelte-query";
  import Modal from "../Modal.svelte";
  import { errorMessage } from "../../lib/api";
  import { toast } from "../../lib/toast.svelte";
  import {
    adminAddonsQuery,
    adminIntegrationsQuery,
    connectAdminIntegrationMutation,
    disconnectAdminIntegrationMutation,
  } from "../../lib/queries";
  import type { AdminIntegration } from "../../lib/api";
  import { CATALOG, type CatalogEntry } from "../../lib/admin/store.svelte";

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

  const queryClient = useQueryClient();
  const addons = createQuery(() => ({ ...adminAddonsQuery(), enabled: kind === "platform" }));
  const integrations = createQuery(() => ({ ...adminIntegrationsQuery(), enabled: kind === "vendor" }));
  const connect = createMutation(() => (connectAdminIntegrationMutation(queryClient)));
  const disconnect = createMutation(() => (disconnectAdminIntegrationMutation(queryClient)));

  const connected = $derived(new Set((integrations.data ?? []).map((i) => i.id)));
  // The catalog is the list of vendors we know how to describe; a vendor not
  // on it is still connectable under "Other".
  const addable = $derived(CATALOG.filter((c) => c.kind === "vendor" && !connected.has(c.id)));

  let catalogOpen = $state(false);
  let form = $state<{ vendor: string; name: string; address: string; base_access: "read" | "write"; credential: string; trust: "trusted" | "untrusted" } | null>(null);
  let formError = $state<string | null>(null);

  // A deep link `#/admin/integrations/<id>` opens that vendor's form.
  $effect(() => {
    if (kind === "vendor" && item && !form) {
      const preset = CATALOG.find((c) => c.id === item);
      if (preset) openConnect(preset);
    }
  });

  function openConnect(preset: CatalogEntry | null): void {
    catalogOpen = false;
    formError = null;
    form = {
      vendor: preset?.id ?? "",
      name: preset?.name ?? "",
      address: preset?.address ?? "",
      base_access: "read",
      credential: "",
      trust: "untrusted",
    };
  }

  function submitConnect(event: SubmitEvent): void {
    event.preventDefault();
    if (!form) return;
    formError = null;
    connect.mutate(
      { ...form, vendor: form.vendor || form.name.toLowerCase().replace(/[^a-z0-9]+/g, "-") },
      {
        onSuccess: (row) => {
          toast.success(`${row.name} connected`, row.state_detail ?? undefined);
          form = null;
        },
        onError: (err: unknown) => {
          formError = errorMessage(err, "Could not connect. Try again.");
        },
      },
    );
  }

  function handleDisconnect(row: AdminIntegration): void {
    disconnect.mutate(row.id, {
      onSuccess: (outcome) => toast.success(`${row.name} disconnected`, `Credential ${outcome.secret}; gateway: ${outcome.gateway}.`),
      onError: (err: unknown) => toast.error("Could not disconnect", errorMessage(err, "Try again.")),
    });
  }

  const STATE: Record<string, string> = { pending: "Pending", applied: "Connected", failed: "Failed" };
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
    {#if integrations.isPending}
      <p class="panel__note">Reading what the room is connected to…</p>
    {:else if integrations.isError}
      <p class="panel__note">Could not read the integrations just now.</p>
    {:else if (integrations.data ?? []).length === 0}
      <p class="panel__empty">Nothing connected yet. Add an integration to hold one vendor credential for the room.</p>
    {:else}
      <div class="cards">
        {#each integrations.data ?? [] as i (i.id)}
          <article class="card" aria-labelledby="integration-{i.id}">
            <div class="card__head">
              <h4 id="integration-{i.id}" class="card__name">{i.name}</h4>
              <span class="list__tag">{i.base_access === "write" ? "read and write" : "read-only"}</span>
              <span class="dot dot--{i.state}" title={STATE[i.state] ?? i.state} aria-label={STATE[i.state] ?? i.state}></span>
            </div>
            {#if i.spec.address}<p class="card__fields">{i.spec.address}</p>{/if}
            <p class="card__detail" class:card__detail--fail={i.state === "failed"}>{i.state_detail ?? STATE[i.state]}</p>
            <div class="card__actions">
              {#if i.spec.pull_request}
                <a class="btn btn--quiet" href={i.spec.pull_request} target="_blank" rel="noreferrer">Pull request</a>
              {/if}
              <button type="button" class="btn btn--quiet btn--danger" disabled={disconnect.isPending} onclick={() => handleDisconnect(i)}>Disconnect</button>
            </div>
          </article>
        {/each}
      </div>
    {/if}
    <p class="panel__note">Connecting stores the credential in the secrets add-on and opens a pull request on the gateway's config; the card says how far that has gone.</p>
  {/if}
</section>

<Modal open={form !== null} title={form ? `Connect ${form.name || "an integration"}` : ""} onclose={() => (form = null)}>
  {#if form}
    <form id="integration-form" class="form" onsubmit={submitConnect}>
      <label class="field">
        <span>Name</span>
        <input id="integration-name" type="text" bind:value={form.name} required />
      </label>
      <label class="field">
        <span>Address of the vendor's MCP service</span>
        <input id="integration-address" type="url" bind:value={form.address} placeholder="https://" />
      </label>
      <label class="field">
        <span>Token or key</span>
        <input id="integration-credential" type="password" bind:value={form.credential} autocomplete="off" required />
      </label>
      <fieldset class="field">
        <legend>The room holds it as</legend>
        <label><input type="radio" name="base_access" value="read" bind:group={form.base_access} /> read-only</label>
        <label><input type="radio" name="base_access" value="write" bind:group={form.base_access} /> read and write</label>
      </fieldset>
      {#if formError}<p class="panel__note card__detail--fail">{formError}</p>{/if}
      <p class="panel__note">The credential goes to the secrets add-on, never to this page again. Agents get a lane to it under Connections.</p>
    </form>
  {/if}
  {#snippet footer()}
    <button type="button" class="btn" onclick={() => (form = null)}>Cancel</button>
    <button type="submit" form="integration-form" class="btn btn--primary" disabled={connect.isPending}>Connect</button>
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
        <button type="button" class="btn" onclick={() => openConnect(c)}>Add</button>
      </li>
    {/each}
    <li class="list__row">
      <div>
        <span class="list__name">Other</span>
        <p class="list__desc">A vendor not on this list.</p>
      </div>
      <button type="button" class="btn" onclick={() => openConnect(null)}>Add</button>
    </li>
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
  .dot--on,
  .dot--applied {
    border-color: var(--ok);
    background: var(--ok);
  }

  .dot--error,
  .dot--failed {
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

  .dot--pending {
    border-color: var(--warn);
    background: var(--warn);
  }

  fieldset.field {
    display: flex;
    gap: var(--space-sm);
    padding: 0;
    border: none;
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
