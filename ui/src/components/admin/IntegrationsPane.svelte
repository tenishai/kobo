<script lang="ts">
  // What agents reach out to. With the permission service on, a cloud is an
  // organization row (its hub role) with the accounts connected under it, and a
  // service vendor is one connection; agents are granted permissions per account
  // or connection under Permissions. Without it, a row is one credential served
  // through the gateway with a lane per agent under Connections.
  import { createMutation, createQuery, useQueryClient } from "@tanstack/svelte-query";
  import Modal from "../Modal.svelte";
  import AwsConnect from "./AwsConnect.svelte";
  import { errorMessage, type AdminIntegration, type OrgAccountsCheck } from "../../lib/api";
  import { toast } from "../../lib/toast.svelte";
  import {
    adminIntegrationsQuery,
    checkOrgAccountsMutation,
    connectAdminIntegrationMutation,
    disconnectAdminIntegrationMutation,
  } from "../../lib/queries";
  import { CATALOG, OTHER, catalogEntry, type CatalogEntry } from "../../lib/admin/catalog";

  let { item = null }: { item?: string | null } = $props();

  const queryClient = useQueryClient();
  const integrations = createQuery(() => (adminIntegrationsQuery()));
  const connect = createMutation(() => (connectAdminIntegrationMutation(queryClient)));
  const disconnect = createMutation(() => (disconnectAdminIntegrationMutation(queryClient)));
  // Check accounts (#275): the organization's accounts are probed through its hub and the
  // ones carrying the spoke role connect themselves. The last answer per organization is
  // kept so the accounts still waiting for the role show under it.
  const check = createMutation(() => (checkOrgAccountsMutation(queryClient)));
  let checked = $state<Record<string, OrgAccountsCheck>>({});
  let checking = $state<string | null>(null);

  let search = $state("");
  const all = $derived(integrations.data ?? []);
  const matches = $derived((i: AdminIntegration) => i.name.toLowerCase().includes(search.trim().toLowerCase()));
  // Top-level rows: organizations, flat roles and service connections. Accounts sit under their organization.
  const roots = $derived(all.filter((i) => i.spec.kind !== "account" && !i.spec.parent));
  const childrenOf = $derived((org: AdminIntegration) => all.filter((i) => i.spec.parent === org.id));
  const visibleRoots = $derived(roots.filter((r) => matches(r) || childrenOf(r).some(matches)));
  const connected = $derived(new Set(all.map((i) => i.vendor)));
  // A second AWS organization is a second row, so AWS stays addable.
  const addable = $derived(CATALOG.filter((c) => c.id === "aws" || !connected.has(c.id)));

  let catalogOpen = $state(false);
  let form = $state<{
    entry: CatalogEntry;
    mode: "role" | "org" | "other";
    parent: AdminIntegration | null;
    vendor: string;
    name: string;
    address: string;
    base_access: "read" | "write";
    trust: "trusted" | "untrusted";
    values: Record<string, string>;
  } | null>(null);
  let formError = $state<string | null>(null);
  let removing = $state<AdminIntegration | null>(null);

  // A deep link `#/admin/integrations/<vendor>` opens that vendor's form.
  $effect(() => {
    if (item && !form) {
      const preset = catalogEntry(item);
      if (preset) openConnect(preset);
    }
  });

  function slug(name: string): string {
    return name.toLowerCase().replace(/[^a-z0-9]+/g, "-").replace(/^-|-$/g, "");
  }

  function openConnect(preset: CatalogEntry | null): void {
    catalogOpen = false;
    formError = null;
    const entry = preset ?? OTHER;
    form = {
      entry,
      mode: entry.id === "aws" ? "org" : "other",
      parent: null,
      vendor: entry.id,
      name: entry.id === "aws" ? "" : entry.name,
      address: entry.address ?? "",
      base_access: "read",
      trust: "untrusted",
      // The AWS sheet binds the pasted ARN to `role_arn` whatever the catalogue field is called.
      values: entry.id === "aws" ? { role_arn: "" } : Object.fromEntries(entry.fields.map((f) => [f.key, ""])),
    };
  }

  function handleCheck(org: AdminIntegration): void {
    checking = org.id;
    check.mutate(org.id, {
      onSuccess: (out) => {
        checked = { ...checked, [org.id]: out };
        const n = out.connected.length;
        toast.success(
          n === 0 ? "No new accounts" : `${n} account${n === 1 ? "" : "s"} connected`,
          out.without_role.length
            ? `${out.without_role.length} still without the role. Deploy the template to them and check again.`
            : "Every account the hub can see carries the role.",
        );
      },
      onError: (err: unknown) => toast.error("Could not check the accounts", errorMessage(err, "Try again.")),
      onSettled: () => (checking = null),
    });
  }

  function submitConnect(event: SubmitEvent): void {
    event.preventDefault();
    if (!form) return;
    formError = null;
    const identity: Record<string, string> = {};
    const secrets: Record<string, string> = {};
    let auth: string = form.entry.auth;
    if (form.mode === "org") {
      auth = "aws_hub";
      identity.hub_role_arn = (form.values.role_arn ?? "").trim();
    } else {
      for (const f of form.entry.fields) {
        const v = (form.values[f.key] ?? "").trim();
        if (!v) continue;
        (f.secret ? secrets : identity)[f.key] = v;
      }
    }
    connect.mutate(
      {
        vendor: form.vendor || slug(form.name),
        name: form.name,
        address: form.address,
        base_access: form.base_access,
        trust: form.trust,
        auth: auth as never,
        identity,
        secrets,
      },
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
    return iso ? new Date(iso).toLocaleDateString(undefined, { month: "short", day: "numeric" }) : "";
  }
  function kindLabel(i: AdminIntegration): string {
    if (i.spec.kind === "org") return "organization";
    if (i.spec.kind === "account" || i.spec.parent) return "account";
    return i.vendor === "aws" ? "account" : "connection";
  }
  const formTitle = $derived(
    !form ? "" : form.mode === "org" ? "Connect an AWS organization" : `Add ${form.name || "an integration"}`,
  );
</script>

<section class="panel page" aria-labelledby="integrations-heading">
  <div>
    <h2 id="integrations-heading" class="page__title">Integrations</h2>
    <p class="page__lead">
      The vendors agents can reach. A cloud is an organization with the accounts connected under it;
      a service vendor is one connection. What each agent may do there is granted under Permissions.
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
      {#if all.length === 0}
        <p class="panel__empty">Nothing connected yet. Add an integration: an AWS organization, or a vendor the room holds one connection to.</p>
      {:else}
        <table class="panel__table">
          <thead>
            <tr><th>Name</th><th>Kind</th><th>Status</th><th>Added</th><th class="panel__cell--right"></th></tr>
          </thead>
          <tbody>
            {#each visibleRoots as i (i.id)}
              <tr>
                <td>
                  <span class="name">{i.name}</span>
                  {#if i.spec.address}<span class="quiet addr">{i.spec.address}</span>{/if}
                  {#if i.spec.source === "access" && i.spec.kind !== "org"}<span class="quiet addr">in the permission service{i.spec.ceiling?.length ? `, ${i.spec.ceiling.length} permission${i.spec.ceiling.length === 1 ? "" : "s"} agents may ask for` : ""}</span>{/if}
                </td>
                <td><span class="chip">{kindLabel(i)}</span></td>
                <td>
                  <span class="status status--{STATE[i.state]?.tone ?? "warn"}" title={i.state_detail ?? ""}>
                    <span class="status__dot" aria-hidden="true"></span>{STATE[i.state]?.label ?? i.state}
                  </span>
                </td>
                <td class="quiet">{when(i.created_at)}</td>
                <td class="panel__cell--right">
                  {#if i.spec.kind === "org"}
                    <button type="button" class="btn btn--quiet" disabled={checking !== null} onclick={() => handleCheck(i)}>
                      {checking === i.id ? "Checking…" : "Check accounts"}
                    </button>
                  {/if}
                  {#if i.spec.pull_request}
                    <a class="btn btn--quiet" href={i.spec.pull_request} target="_blank" rel="noreferrer">Pull request</a>
                  {/if}
                  <button type="button" class="btn btn--quiet" disabled={disconnect.isPending} onclick={() => (removing = i)}>Remove</button>
                </td>
              </tr>
              {#each childrenOf(i).filter((c) => !search.trim() || matches(c) || matches(i)) as c (c.id)}
                <tr class="child">
                  <td>
                    <span class="child__tick" aria-hidden="true">└</span>
                    <span class="name">{c.name}</span>
                    {#if c.spec.account_id}<span class="quiet addr">{c.spec.account_id}</span>{/if}
                    {#if c.spec.ceiling?.length}<span class="quiet addr">{c.spec.ceiling.length} permission{c.spec.ceiling.length === 1 ? "" : "s"} agents may ask for</span>{/if}
                  </td>
                  <td><span class="chip">account</span></td>
                  <td>
                    <span class="status status--{STATE[c.state]?.tone ?? "warn"}" title={c.state_detail ?? ""}>
                      <span class="status__dot" aria-hidden="true"></span>{STATE[c.state]?.label ?? c.state}
                    </span>
                  </td>
                  <td class="quiet">{when(c.created_at)}</td>
                  <td class="panel__cell--right">
                    <button type="button" class="btn btn--quiet" disabled={disconnect.isPending} onclick={() => (removing = c)}>Remove</button>
                  </td>
                </tr>
              {/each}
              {#each checked[i.id]?.without_role ?? [] as w (w.id)}
                <tr class="child child--waiting">
                  <td>
                    <span class="child__tick" aria-hidden="true">└</span>
                    <span class="name">{w.name}</span>
                    <span class="quiet addr">{w.id}</span>
                  </td>
                  <td><span class="chip">account</span></td>
                  <td>
                    <span class="status status--warn" title={w.reason ?? ""}>
                      <span class="status__dot" aria-hidden="true"></span>No role yet
                    </span>
                  </td>
                  <td class="quiet"></td>
                  <td class="panel__cell--right quiet">Deploy the template, then check again</td>
                </tr>
              {/each}
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

<Modal open={form !== null} title={formTitle} size={form?.vendor === "aws" ? "wide" : "default"} onclose={() => (form = null)}>
  {#if form && form.mode === "org"}
    <form id="integration-form" class="form" onsubmit={submitConnect}>
      <label class="field">
        <span>Name for this organization</span>
        <input id="integration-name" type="text" bind:value={form.name} placeholder="e.g. acme-aws" required autocomplete="off" />
      </label>
      <AwsConnect bind:roleArn={form.values.role_arn} mode="org" integration={slug(form.name) || "aws"} />
      {#if formError}<p class="page__foot fail">{formError}</p>{/if}
    </form>
  {:else if form}
    <form id="integration-form" class="form" onsubmit={submitConnect}>
      <label class="field">
        <span>Name</span>
        <input id="integration-name" type="text" bind:value={form.name} required />
      </label>
      {#each form.entry.fields as f (f.key)}
        <label class="field">
          <span>{f.label}{#if f.optional} <span class="quiet">(optional)</span>{/if}</span>
          <input
            id="integration-{f.key}"
            type={f.secret ? "password" : "text"}
            bind:value={form.values[f.key]}
            autocomplete="off"
            placeholder={f.placeholder ?? ""}
            required={!f.optional}
          />
        </label>
      {/each}
      {#if form.entry.note}<p class="page__foot">{form.entry.note}</p>{/if}
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
      {#if form.entry.fields.some((f) => f.secret)}
        <p class="page__foot">Secret values are stored in the room's secrets service and never shown again.</p>
      {/if}
    </form>
  {/if}
  {#snippet footer()}
    <button type="button" class="btn" onclick={() => (form = null)}>Cancel</button>
    <button type="submit" form="integration-form" class="btn btn--primary" disabled={connect.isPending}>{form?.vendor === "aws" ? (connect.isPending ? "Checking with AWS…" : "Connect") : "Add"}</button>
  {/snippet}
</Modal>

<Modal open={removing !== null} title={removing ? `Remove ${removing.name}?` : ""} size="narrow" onclose={() => (removing = null)}>
  <p class="page__lead">
    {#if removing?.spec.kind === "org"}
      The organization is removed from the permission service. Its accounts must be removed first.
    {:else}
      The connection is removed from the permission service and every grant on it is revoked; any credential is deleted from the secrets service and the vendor removed from the gateway.
    {/if}
  </p>
  {#snippet footer()}
    <button type="button" class="btn" onclick={() => (removing = null)}>Cancel</button>
    <button type="button" class="btn btn--danger" onclick={() => removing && handleDisconnect(removing)}>Remove</button>
  {/snippet}
</Modal>

<style>
  .child td:first-child { padding-left: 1.75rem; }
  .child--waiting .name { color: var(--muted); font-weight: 500; }
  .child__tick { margin-right: 0.35rem; color: var(--muted); }
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
