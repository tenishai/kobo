<script lang="ts">
  // What agents reach out to. With the permission service on, a cloud is an
  // organization row (its hub role) with the accounts connected under it, and a
  // service vendor is one connection; agents are granted permissions per account
  // or connection under Permissions. Without it, a row is one credential served
  // through the gateway with a lane per agent under Connections.
  import { untrack } from "svelte";
  import { createMutation, createQuery, useQueryClient } from "@tanstack/svelte-query";
  import Modal from "../Modal.svelte";
  import AwsConnect from "./AwsConnect.svelte";
  import {
    errorMessage,
    type AdminIntegration,
    type OrgAccountsCheck,
    type RoomServiceId,
    type RoomServiceStatus,
    type RoomServiceValues,
  } from "../../lib/api";
  import { toast } from "../../lib/toast.svelte";
  import {
    adminIntegrationsQuery,
    checkOrgAccountsMutation,
    connectAdminIntegrationMutation,
    disconnectAdminIntegrationMutation,
    putRoomServiceMutation,
    removeRoomServiceMutation,
    roomServicesQuery,
  } from "../../lib/queries";
  import { OTHER, catalogFor, type CatalogEntry } from "../../lib/admin/catalog";
  import { whenDay } from "../../lib/time";

  // `accessOn`: the permission service is on, so Datadog connects as a service account
  // the service mints scoped keys from (kobo#322) rather than a key pair for the gateway.
  let { item = null, accessOn = false }: { item?: string | null; accessOn?: boolean } = $props();
  const catalog = $derived(catalogFor(accessOn));

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

  // The room's own two services (#284, rows in this table since #304): the
  // Slack config tokens it creates agents' Slack apps with and the GitHub app
  // that opens their pull requests. Always listed, never "added"; pasted
  // once, read live; a value the deployment config carries reads Connected
  // too, with Replace to move it into the room (the room's value wins; an
  // older install's lane, not a fresh one's). Remove calls the room-services DELETE, never
  // a vendor disconnect.
  const roomServices = createQuery(() => (roomServicesQuery()));
  const putService = createMutation(() => (putRoomServiceMutation(queryClient)));
  const removeService = createMutation(() => (removeRoomServiceMutation(queryClient)));
  type ServiceField = { key: keyof RoomServiceValues; label: string; secret: boolean; multiline?: boolean; placeholder?: string };
  const ROOM_SERVICES: { id: RoomServiceId; name: string; summary: string; where: string; fields: ServiceField[] }[] = [
    {
      id: "slack",
      name: "Slack",
      summary: "Lets the room create agents' Slack apps.",
      where: "Both values come from api.slack.com/apps, under Your App Configuration Tokens: generate a token for your workspace and paste the pair. Every Slack app the room creates belongs to the account that generated the token, so use a dedicated kobo account rather than your own: create a Slack user for kobo in your workspace, sign in as it at api.slack.com/apps, generate the token there, and keep its sign-in with the room's operators. The token pair rotates on its own once pasted.",
      fields: [
        { key: "config_token", label: "App configuration token", secret: true, placeholder: "xoxe.xoxp-..." },
        { key: "refresh_token", label: "Refresh token", secret: true, placeholder: "xoxe-..." },
      ],
    },
    {
      id: "github",
      name: "GitHub",
      summary: "Opens agents' pull requests, scoped to the repositories you approve.",
      where: "App ID and the private key come from the app's settings page on GitHub (Developer settings, GitHub Apps); the installation ID is the number at the end of the installation's URL under Install App.",
      fields: [
        { key: "app_id", label: "App ID", secret: false, placeholder: "123456" },
        { key: "installation_id", label: "Installation ID", secret: false, placeholder: "12345678" },
        { key: "private_key", label: "Private key (PEM)", secret: true, multiline: true, placeholder: "-----BEGIN RSA PRIVATE KEY-----" },
      ],
    },
  ];
  const SERVICE_STATE: Record<RoomServiceStatus["source"], { tone: "on" | "warn"; label: string }> = {
    room: { tone: "on", label: "Set up" },
    deployment: { tone: "on", label: "Connected" },
    none: { tone: "warn", label: "Not set up" },
  };
  let serviceForm = $state<{ id: RoomServiceId; values: Record<string, string> } | null>(null);
  let serviceError = $state<string | null>(null);
  let removingService = $state<RoomServiceId | null>(null);
  const serviceDef = $derived((id: RoomServiceId) => ROOM_SERVICES.find((s) => s.id === id)!);
  const serviceStatus = $derived((id: RoomServiceId): RoomServiceStatus | null => roomServices.data?.services[id] ?? null);

  function openService(id: RoomServiceId): void {
    serviceError = null;
    serviceForm = { id, values: Object.fromEntries(serviceDef(id).fields.map((f) => [f.key, ""])) };
  }

  function submitService(event: SubmitEvent): void {
    event.preventDefault();
    if (!serviceForm) return;
    serviceError = null;
    const values: RoomServiceValues = {};
    for (const f of serviceDef(serviceForm.id).fields) values[f.key] = (serviceForm.values[f.key] ?? "").trim();
    const name = serviceDef(serviceForm.id).name;
    putService.mutate(
      { service: serviceForm.id, values },
      {
        onSuccess: () => {
          toast.success(`${name} set up`, "Agents can use it now.");
          serviceForm = null;
        },
        onError: (err: unknown) => (serviceError = errorMessage(err, "Could not store the values. Try again.")),
      },
    );
  }

  function handleRemoveService(id: RoomServiceId): void {
    removingService = null;
    const name = serviceDef(id).name;
    removeService.mutate(id, {
      onSuccess: () => toast.success(`${name} removed`),
      onError: (err: unknown) => toast.error(`Could not remove ${name}`, errorMessage(err, "Try again.")),
    });
  }

  function whenBy(s: RoomServiceStatus): string {
    if (s.source !== "room") return "";
    const at = whenDay(s.updated_at);
    return [at, s.updated_by ? `by ${s.updated_by}` : ""].filter(Boolean).join(" ");
  }

  let search = $state("");
  const all = $derived(integrations.data ?? []);
  const matches = $derived((i: AdminIntegration) => i.name.toLowerCase().includes(search.trim().toLowerCase()));
  // Top-level rows: organizations, flat roles and service connections. Accounts sit under their organization.
  const roots = $derived(all.filter((i) => i.spec.kind !== "account" && !i.spec.parent));
  const childrenOf = $derived((org: AdminIntegration) => all.filter((i) => i.spec.parent === org.id));
  const visibleRoots = $derived(roots.filter((r) => matches(r) || childrenOf(r).some(matches)));
  const visibleServices = $derived(ROOM_SERVICES.filter((s) => s.name.toLowerCase().includes(search.trim().toLowerCase())));
  const connected = $derived(new Set(all.map((i) => i.vendor)));
  // A second AWS organization is a second row, so AWS stays addable.
  const addable = $derived(catalog.filter((c) => c.id === "aws" || !connected.has(c.id)));

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

  // Datadog through the permission service (kobo#327): the gateway gets Datadog's
  // hosted MCP server as a target, so the address field stays and follows the Site
  // field until the person types their own.
  const DATADOG_AUTH = "datadog_service_account";
  const GITHUB_AUTH = "github_app";
  // Connections through the permission service: the keys go to it alone, the
  // vendor's MCP server becomes a gateway target, and the read/write choice does
  // not apply (the ceiling is the read permissions agents are granted).
  const SERVICE_AUTHS = new Set([DATADOG_AUTH, GITHUB_AUTH]);
  const VENDOR_OF_AUTH: Record<string, string> = { [DATADOG_AUTH]: "Datadog", [GITHUB_AUTH]: "GitHub" };
  function datadogMcpAddress(site: string): string {
    return `https://mcp.${site.trim() || "datadoghq.com"}/v1`;
  }
  let suggestedAddress = $state("");
  $effect(() => {
    if (!form || form.entry.auth !== DATADOG_AUTH) return;
    const next = datadogMcpAddress(form.values.site ?? "");
    untrack(() => {
      if (form && (form.address === "" || form.address === suggestedAddress)) form.address = next;
      suggestedAddress = next;
    });
  });

  // A deep link `#/admin/integrations/<vendor>` opens that vendor's form.
  $effect(() => {
    if (item && !form) {
      const preset = catalog.find((c) => c.id === item);
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

  const when = whenDay;
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
      The vendors agents can reach, and the two services the room itself uses: Slack and GitHub.
      A cloud is an organization with the accounts connected under it; a service vendor is one connection.
      What each agent may do there is granted under Permissions.
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
      <table class="panel__table">
        <thead>
          <tr><th>Name</th><th>Kind</th><th>Status</th><th>Added</th><th class="panel__cell--right"></th></tr>
        </thead>
        <tbody>
          {#each visibleServices as svc (svc.id)}
            {@const s = serviceStatus(svc.id)}
            <tr>
              <td>
                <span class="name">{svc.name}</span>
                <span class="quiet addr">{svc.summary}</span>
              </td>
              <td><span class="chip">service</span></td>
              <td>
                {#if s}
                  <span class="status status--{SERVICE_STATE[s.source].tone}">
                    <span class="status__dot" aria-hidden="true"></span>{SERVICE_STATE[s.source].label}
                  </span>
                {:else if roomServices.isError}
                  <span class="quiet">Could not read</span>
                {:else}
                  <span class="quiet">Reading…</span>
                {/if}
              </td>
              <td class="quiet">{s ? whenBy(s) : ""}</td>
              <td class="panel__cell--right">
                {#if s?.source === "deployment"}
                  <!-- The deployment config carries it (an older install). A value pasted here
                       is held by the room and wins, the way a fresh install does it. -->
                  <span class="quiet" title="Set in the deployment config. Replace to hold it in the room instead; the room's value wins.">set in the deployment config</span>
                  {#if roomServices.data?.can_store}
                    <button type="button" class="btn btn--quiet" onclick={() => openService(svc.id)}>Replace</button>
                  {/if}
                {:else if s && roomServices.data && !roomServices.data.can_store}
                  <span class="quiet" title={roomServices.data.note ?? ""}>nowhere to store it on this room</span>
                {:else if s?.source === "room"}
                  <button type="button" class="btn btn--quiet" onclick={() => openService(svc.id)}>Replace</button>
                  <button type="button" class="btn btn--quiet" disabled={removeService.isPending} onclick={() => (removingService = svc.id)}>Remove</button>
                {:else if s}
                  <button type="button" class="btn btn--quiet" onclick={() => openService(svc.id)}>Set up</button>
                {/if}
              </td>
            </tr>
          {/each}
          {#if all.length === 0 && !search.trim()}
            <tr><td colspan="5"><p class="panel__empty">Nothing connected yet. Add an integration: an AWS organization, or a vendor the room holds one connection to.</p></td></tr>
          {/if}
          {#each visibleRoots as i (i.id)}
            <tr>
              <td>
                <span class="name">{i.name}</span>
                {#if i.spec.address}<span class="quiet addr">{i.spec.address}</span>{/if}
                {#if i.spec.source === "access" && i.spec.kind !== "org"}<span class="quiet addr">in the permission service{i.spec.ceiling?.length ? `, ${i.spec.ceiling.length} permission${i.spec.ceiling.length === 1 ? "" : "s"} agents may ask for` : ""}</span>{/if}
              </td>
              <td><span class="chip">{kindLabel(i)}</span></td>
              <td>
                {#if disconnect.isPending && disconnect.variables === i.id}
                  <span class="status status--warn" title="Removing from the permission service, the secrets and the gateway; the gateway's pull request takes a moment.">
                    <span class="status__dot" aria-hidden="true"></span>Removing…
                  </span>
                {:else}
                  <span class="status status--{STATE[i.state]?.tone ?? "warn"}" title={i.state_detail ?? ""}>
                    <span class="status__dot" aria-hidden="true"></span>{STATE[i.state]?.label ?? i.state}
                  </span>
                {/if}
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
            {#if visibleServices.length === 0}
              <tr><td colspan="5"><p class="panel__empty">No integration matches that.</p></td></tr>
            {/if}
          {/each}
        </tbody>
      </table>
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
      {#if form.entry.auth === DATADOG_AUTH}
        <label class="field">
          <span>Datadog MCP server</span>
          <input id="integration-address" type="url" bind:value={form.address} placeholder="https://mcp.datadoghq.com/v1" />
        </label>
        <p class="page__foot">Agents reach it with their own leased key; change this only if your site differs.</p>
      {:else if form.entry.auth === GITHUB_AUTH}
        <label class="field">
          <span>GitHub MCP server <span class="quiet">(optional)</span></span>
          <input id="integration-address" type="url" bind:value={form.address} placeholder="https://api.githubcopilot.com" />
        </label>
        <p class="page__foot">Agents reach it with their own leased token, read-only; leave this blank for GitHub.com.</p>
      {:else}
        <label class="field">
          <span>Vendor's MCP address <span class="quiet">(optional)</span></span>
          <input id="integration-address" type="url" bind:value={form.address} placeholder="https://" />
        </label>
        <!-- A connection through the permission service has its ceiling in the read
             permissions agents are granted; the read/write choice does not apply. -->
        <fieldset class="field">
          <legend>Agents may</legend>
          <label><input type="radio" name="base_access" value="read" bind:group={form.base_access} /> read</label>
          <label><input type="radio" name="base_access" value="write" bind:group={form.base_access} /> read and write</label>
        </fieldset>
      {/if}
      {#if formError}<p class="page__foot fail">{formError}</p>{/if}
      {#if connect.isPending}
        <p class="page__foot busy" role="status"><span class="spin" aria-hidden="true"></span>
          {#if SERVICE_AUTHS.has(form.entry.auth)}Checking with {VENDOR_OF_AUTH[form.entry.auth]}, then adding its MCP server to the gateway. This can take up to a minute.
          {:else if form.vendor === "aws"}Checking the role with AWS.
          {:else}Storing the credential and adding the target to the gateway. This can take a moment.{/if}
        </p>
      {/if}
      {#if SERVICE_AUTHS.has(form.entry.auth)}
        <p class="page__foot">The keys are kept for the permission service alone and never shown again; it checks them with {VENDOR_OF_AUTH[form.entry.auth]} before anything is saved.</p>
      {:else if form.entry.fields.some((f) => f.secret)}
        <p class="page__foot">Secret values are stored in the room's secrets service and never shown again.</p>
      {/if}
    </form>
  {/if}
  {#snippet footer()}
    <button type="button" class="btn" onclick={() => (form = null)}>Cancel</button>
    <button type="submit" form="integration-form" class="btn btn--primary" disabled={connect.isPending}>
      {#if connect.isPending}<span class="spin spin--btn" aria-hidden="true"></span>{form?.vendor === "aws" ? "Checking with AWS…" : form && SERVICE_AUTHS.has(form.entry.auth) ? `Checking with ${VENDOR_OF_AUTH[form.entry.auth]}…` : "Adding…"}
      {:else}{form?.vendor === "aws" ? "Connect" : "Add"}{/if}
    </button>
  {/snippet}
</Modal>

<Modal open={serviceForm !== null} title={serviceForm ? `${serviceStatus(serviceForm.id)?.source === "room" ? "Replace" : "Set up"} ${serviceDef(serviceForm.id).name}` : ""} onclose={() => (serviceForm = null)}>
  {#if serviceForm}
    {@const def = serviceDef(serviceForm.id)}
    <form id="room-service-form" class="form" onsubmit={submitService}>
      <p class="page__lead">{def.where}</p>
      {#each def.fields as f (f.key)}
        <label class="field">
          <span>{f.label}</span>
          {#if f.multiline}
            <textarea id="room-service-{f.key}" rows="6" bind:value={serviceForm.values[f.key]} placeholder={f.placeholder ?? ""} autocomplete="off" spellcheck="false" required></textarea>
          {:else}
            <input id="room-service-{f.key}" type={f.secret ? "password" : "text"} bind:value={serviceForm.values[f.key]} placeholder={f.placeholder ?? ""} autocomplete="off" required />
          {/if}
        </label>
      {/each}
      {#if serviceError}<p class="page__foot fail">{serviceError}</p>{/if}
      <p class="page__foot">The values are kept in the room's own secret store and never shown again.</p>
    </form>
  {/if}
  {#snippet footer()}
    <button type="button" class="btn" onclick={() => (serviceForm = null)}>Cancel</button>
    <button type="submit" form="room-service-form" class="btn btn--primary" disabled={putService.isPending}>{putService.isPending ? "Saving…" : "Save"}</button>
  {/snippet}
</Modal>

<Modal open={removingService !== null} title={removingService ? `Remove ${serviceDef(removingService).name}?` : ""} size="narrow" onclose={() => (removingService = null)}>
  <p class="page__lead">
    {#if removingService === "slack"}
      The room forgets its Slack config tokens. Agents keep the Slack apps they already have, but no new app can be connected until a pair is set again, or the deployment config carries one; every agent's Slack tab greys out until then.
    {:else}
      The room forgets the GitHub app. Agents can't open pull requests until an app is set again, or the deployment config carries one; every agent's Repositories card greys out until then.
    {/if}
  </p>
  {#snippet footer()}
    <button type="button" class="btn" onclick={() => (removingService = null)}>Cancel</button>
    <button type="button" class="btn btn--danger" onclick={() => removingService && handleRemoveService(removingService)}>Remove</button>
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
  .busy { display: flex; align-items: center; gap: 0.5rem; }
  .spin { display: inline-block; width: 0.9em; height: 0.9em; border-radius: 50%; border: 2px solid currentColor; border-right-color: transparent; animation: spin 0.8s linear infinite; flex: none; }
  .spin--btn { margin-right: 0.4rem; vertical-align: -0.1em; }
  @keyframes spin { to { transform: rotate(360deg); } }

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
