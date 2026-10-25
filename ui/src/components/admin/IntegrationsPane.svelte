<script lang="ts">
  // What agents reach out to: one base credential per vendor, held once by
  // the room and served through the gateway. A row is a vendor; the status
  // says how far the credential and the gateway change have gone.
  import { createMutation, createQuery, useQueryClient } from "@tanstack/svelte-query";
  import Modal from "../Modal.svelte";
  import { errorMessage, type AdminIntegration } from "../../lib/api";
  import { toast } from "../../lib/toast.svelte";
  import {
    adminIntegrationsQuery,
    connectAdminIntegrationMutation,
    disconnectAdminIntegrationMutation,
  } from "../../lib/queries";
  import { CATALOG, catalogEntry, type CatalogEntry } from "../../lib/admin/catalog";

  let { item = null }: { item?: string | null } = $props();

  const queryClient = useQueryClient();
  const integrations = createQuery(() => (adminIntegrationsQuery()));
  const connect = createMutation(() => (connectAdminIntegrationMutation(queryClient)));
  const disconnect = createMutation(() => (disconnectAdminIntegrationMutation(queryClient)));

  let search = $state("");
  const rows = $derived(
    (integrations.data ?? []).filter((i) => i.name.toLowerCase().includes(search.trim().toLowerCase())),
  );
  const connected = $derived(new Set((integrations.data ?? []).map((i) => i.vendor)));
  const addable = $derived(CATALOG.filter((c) => !connected.has(c.id)));

  let catalogOpen = $state(false);
  let form = $state<{ vendor: string; name: string; address: string; base_access: "read" | "write"; credential: string; trust: "trusted" | "untrusted"; credentialLabel: string } | null>(null);
  let formError = $state<string | null>(null);
  let removing = $state<AdminIntegration | null>(null);

  // A deep link `#/admin/integrations/<vendor>` opens that vendor's form.
  $effect(() => {
    if (item && !form) {
      const preset = catalogEntry(item);
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
      credentialLabel: preset?.credential ?? "Token or key",
    };
  }

  function submitConnect(event: SubmitEvent): void {
    event.preventDefault();
    if (!form) return;
    formError = null;
    const { credentialLabel: _label, ...body } = form;
    connect.mutate(
      { ...body, vendor: body.vendor || body.name.toLowerCase().replace(/[^a-z0-9]+/g, "-") },
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
    removing = null;
    disconnect.mutate(row.id, {
      onSuccess: () => toast.success(`${row.name} disconnected`),
      onError: (err: unknown) => toast.error("Could not disconnect", errorMessage(err, "Try again.")),
    });
  }

  const STATE: Record<string, { tone: "on" | "warn" | "fail"; label: string }> = {
    applied: { tone: "on", label: "Connected" },
    pending: { tone: "warn", label: "Pending" },
    failed: { tone: "fail", label: "Failed" },
  };

  function when(iso: string): string {
    return new Date(iso).toLocaleDateString(undefined, { month: "short", day: "numeric" });
  }
</script>

<section class="panel page" aria-labelledby="integrations-heading">
  <div>
    <h2 id="integrations-heading" class="page__title">Integrations</h2>
    <p class="page__lead">
      The vendors agents can reach. The room holds one credential per vendor; each agent gets its
      own lane to it under Connections.
    </p>
  </div>
  <div class="page__toolbar">
    <input class="field__input page__search" type="search" placeholder="Search integrations" bind:value={search} aria-label="Search integrations" />
    <button type="button" class="btn btn--primary" onclick={() => (catalogOpen = true)}>Add integration</button>
  </div>

  {#if integrations.isPending}
    <p class="page__foot">Reading what the room is connected to…</p>
  {:else if integrations.isError}
    <p class="page__foot">Could not read the integrations just now.</p>
  {:else}
    <div class="table-card">
      {#if (integrations.data ?? []).length === 0}
        <p class="panel__empty">Nothing connected yet. Add an integration to hold one vendor credential for the room.</p>
      {:else}
        <table class="panel__table">
          <thead>
            <tr><th>Name</th><th>Access</th><th>Status</th><th>Added</th><th class="panel__cell--right"></th></tr>
          </thead>
          <tbody>
            {#each rows as i (i.id)}
              <tr>
                <td>
                  <span class="name">{i.name}</span>
                  {#if i.spec.address}<span class="quiet addr">{i.spec.address}</span>{/if}
                </td>
                <td><span class="chip">{i.base_access === "write" ? "read and write" : "read-only"}</span></td>
                <td>
                  <span class="status status--{STATE[i.state]?.tone ?? "warn"}" title={i.state_detail ?? ""}>
                    <span class="status__dot" aria-hidden="true"></span>{STATE[i.state]?.label ?? i.state}
                  </span>
                </td>
                <td class="quiet">{when(i.created_at)}</td>
                <td class="panel__cell--right">
                  {#if i.spec.pull_request}
                    <a class="btn btn--quiet" href={i.spec.pull_request} target="_blank" rel="noreferrer">Pull request</a>
                  {/if}
                  <button type="button" class="btn btn--quiet" disabled={disconnect.isPending} onclick={() => (removing = i)}>Remove</button>
                </td>
              </tr>
            {:else}
              <tr><td colspan="5"><p class="panel__empty">No integration matches that.</p></td></tr>
            {/each}
          </tbody>
        </table>
      {/if}
    </div>
  {/if}
</section>

<Modal open={catalogOpen} title="Add an integration" onclose={() => (catalogOpen = false)}>
  <div class="table-card">
    {#each addable as c (c.id)}
      <div class="pick">
        <div>
          <span class="name">{c.name}</span>
          <p class="quiet pick__desc">{c.summary}</p>
        </div>
        <button type="button" class="btn" onclick={() => openConnect(c)}>Add</button>
      </div>
    {/each}
    <div class="pick">
      <div>
        <span class="name">Other</span>
        <p class="quiet pick__desc">A vendor not on this list.</p>
      </div>
      <button type="button" class="btn" onclick={() => openConnect(null)}>Add</button>
    </div>
  </div>
</Modal>

<Modal open={form !== null} title={form ? `Add ${form.name || "an integration"}` : ""} onclose={() => (form = null)}>
  {#if form}
    <form id="integration-form" class="form" onsubmit={submitConnect}>
      <label class="field">
        <span>Name</span>
        <input id="integration-name" type="text" bind:value={form.name} required />
      </label>
      <label class="field">
        <span>{form.credentialLabel}</span>
        <input id="integration-credential" type="password" bind:value={form.credential} autocomplete="off" required />
      </label>
      <label class="field">
        <span>Vendor's MCP address <span class="quiet">(optional)</span></span>
        <input id="integration-address" type="url" bind:value={form.address} placeholder="https://" />
      </label>
      <fieldset class="field">
        <legend>Agents may</legend>
        <label><input type="radio" name="base_access" value="read" bind:group={form.base_access} /> read</label>
        <label><input type="radio" name="base_access" value="write" bind:group={form.base_access} /> read and write</label>
      </fieldset>
      {#if formError}<p class="page__foot fail">{formError}</p>{/if}
      <p class="page__foot">The credential is stored in the room's secrets service and never shown again.</p>
    </form>
  {/if}
  {#snippet footer()}
    <button type="button" class="btn" onclick={() => (form = null)}>Cancel</button>
    <button type="submit" form="integration-form" class="btn btn--primary" disabled={connect.isPending}>Add</button>
  {/snippet}
</Modal>

<Modal open={removing !== null} title={removing ? `Remove ${removing.name}?` : ""} size="narrow" onclose={() => (removing = null)}>
  <p class="page__lead">
    The credential is deleted from the secrets service and the vendor is removed from the gateway.
    Agents with a lane to it lose it.
  </p>
  {#snippet footer()}
    <button type="button" class="btn" onclick={() => (removing = null)}>Cancel</button>
    <button type="button" class="btn btn--danger" onclick={() => removing && handleDisconnect(removing)}>Remove</button>
  {/snippet}
</Modal>

<style>
  .name { font-weight: 600; }
  .quiet { color: var(--muted); font-size: 0.85rem; }
  .fail { color: var(--fail); }
  .addr { display: block; font-size: 0.75rem; }
  .pick {
    display: flex;
    align-items: center;
    justify-content: space-between;
    gap: var(--space-md);
    padding: 12px var(--space-md);
    border-bottom: 1px solid var(--border);
  }
  .pick:last-child { border-bottom: none; }
  .pick__desc { margin: 2px 0 0; }
</style>
