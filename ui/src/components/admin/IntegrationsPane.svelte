<script lang="ts">
  // What agents reach out to. With the permission service on, a cloud is an
  // organization row (its hub role) with the accounts connected under it, or a
  // Google Cloud project, or a folder with its projects under it (kobo#390), and a
  // service vendor is one connection; agents are granted permissions per account,
  // project or connection under Permissions. Without it, a row is one credential
  // served through the gateway with a lane per agent under Connections.
  import { untrack } from "svelte";
  import { createMutation, createQuery, useQueryClient } from "@tanstack/svelte-query";
  import Modal from "../Modal.svelte";
  import VendorMark from "../VendorMark.svelte";
  import AwsConnect from "./AwsConnect.svelte";
  import ConnectGoogleCloudSheet from "./ConnectGoogleCloudSheet.svelte";
  import {
    errorMessage,
    type AdminIntegration,
    type OrgAccountsCheck,
    type SelfHostedModel,
  } from "../../lib/api";
  import { toast } from "../../lib/toast.svelte";
  import {
    adminIntegrationsQuery,
    checkOrgAccountsMutation,
    checkSelfHostedModelsMutation,
    connectAdminIntegrationMutation,
    disconnectAdminIntegrationMutation,
  } from "../../lib/queries";
  import { OTHER, ROOM_VENDORS, SELF_HOSTED, SELF_HOSTED_AUTH, SELF_HOSTED_SUMMARY, catalogFor, type CatalogEntry } from "../../lib/admin/catalog";
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
  // Check models (kobo#390): a Self-hosted row's connect probe again, one short answer
  // asked of each named model; the row's verdict per model and the gateway follow.
  const checkModels = createMutation(() => (checkSelfHostedModelsMutation(queryClient)));
  let checkingModels = $state<string | null>(null);

  // Slack and GitHub are rows like the rest (kobo#377): added through the catalog,
  // replaced by an Add with new values, removed like any connection. The room's own
  // needs read their credential from the row. An older install whose deployment config
  // still carries one shows it as a row marked so, with Replace and no Remove.
  let search = $state("");
  const all = $derived(integrations.data ?? []);
  const matches = $derived((i: AdminIntegration) => i.name.toLowerCase().includes(search.trim().toLowerCase()));
  // Top-level rows: organizations, flat roles and service connections. Accounts sit under their organization.
  const roots = $derived(all.filter((i) => i.spec.kind !== "account" && !i.spec.parent));
  const childrenOf = $derived((org: AdminIntegration) => all.filter((i) => i.spec.parent === org.id));
  const visibleRoots = $derived(roots.filter((r) => matches(r) || childrenOf(r).some(matches)));
  const connected = $derived(new Set(all.map((i) => i.vendor)));
  // A second AWS organization, a second Google Cloud project or a second self-hosted
  // endpoint is a second row, so those stay addable.
  const addable = $derived(
    catalog
      .filter((c) => c.id === "aws" || c.id === "gcp" || c.id === SELF_HOSTED || !connected.has(c.id))
      .slice()
      .sort((a, b) => a.name.localeCompare(b.name, undefined, { sensitivity: "base" })),
  );

  let catalogOpen = $state(false);
  let form = $state<{
    entry: CatalogEntry;
    mode: "role" | "org" | "gcp" | "other" | "replace";
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
  // The Google Cloud sheet's own choices (kobo#390): a project or a folder, and whether
  // the project's Vertex AI models serve the room.
  let gcpMode = $state<"project" | "parent">("project");
  let useVertex = $state(false);
  const VERTEX_DEFAULT_LOCATION = "us-east5";
  // The AWS sheet's own choices (kobo#390): an organization (its hub role) or one account
  // (a role the room assumes directly), and whether that account's Bedrock models serve
  // the room. Bedrock is per account, so the box is not offered on an organization.
  let awsMode = $state<"org" | "role" | "account">("org");
  let useBedrock = $state(false);
  const BEDROCK_DEFAULT_REGION = "us-east-1";

  // Datadog through the permission service (kobo#327): the gateway gets Datadog's
  // hosted MCP server as a target, so the address field stays and follows the Site
  // field until the person types their own.
  const DATADOG_AUTH = "datadog_service_account";
  const GITHUB_AUTH = "github_app";
  const SLACK_AUTH = "slack_config_token";
  // Connections through the permission service: the keys go to it alone, the
  // vendor's MCP server becomes a gateway target, and the read/write choice does
  // not apply (the ceiling is the read permissions agents are granted).
  const SERVICE_AUTHS = new Set([DATADOG_AUTH, GITHUB_AUTH]);
  const VENDOR_OF_AUTH: Record<string, string> = { [DATADOG_AUTH]: "Datadog", [GITHUB_AUTH]: "GitHub" };
  // The room's own two (kobo#377): the values are held by the room itself, no read/write choice.
  const ROOM_AUTHS = new Set([GITHUB_AUTH, SLACK_AUTH]);
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

  function openConnect(preset: CatalogEntry | null, replacing: AdminIntegration | null = null): void {
    catalogOpen = false;
    formError = null;
    const entry = preset ?? OTHER;
    gcpMode = "project";
    useVertex = false;
    awsMode = "org";
    useBedrock = false;
    form = {
      entry,
      mode: replacing ? "replace" : entry.id === "aws" ? "org" : entry.id === "gcp" ? "gcp" : "other",
      parent: null,
      vendor: entry.id,
      name: replacing?.name || (entry.id === "aws" || entry.id === "gcp" || entry.id === SELF_HOSTED ? "" : entry.name),
      address: entry.address ?? "",
      base_access: "read",
      trust: "untrusted",
      // The AWS sheet binds the pasted ARN to `role_arn` whatever the catalogue field is called;
      // the Google Cloud sheet owns its fields the same way.
      values: entry.id === "aws" ? { role_arn: "", models_region: BEDROCK_DEFAULT_REGION }
        : entry.id === "gcp" ? { project: "", service_account: "", parent_resource: "", models_location: VERTEX_DEFAULT_LOCATION }
        : Object.fromEntries(entry.fields.map((f) => [f.key, ""])),
    };
  }

  // The noun under a cloud's parent row: AWS accounts, Google Cloud projects.
  const childNoun = (i: AdminIntegration): string => (i.vendor === "gcp" ? "project" : "account");

  function handleCheck(org: AdminIntegration): void {
    checking = org.id;
    const noun = childNoun(org);
    check.mutate(org.id, {
      onSuccess: (out) => {
        checked = { ...checked, [org.id]: out };
        const n = out.connected.length;
        toast.success(
          n === 0 ? `No new ${noun}s` : `${n} ${noun}${n === 1 ? "" : "s"} connected`,
          out.without_role.length
            ? (org.vendor === "gcp"
              ? `${out.without_role.length} the identity cannot read yet. Grant it the roles there and check again.`
              : `${out.without_role.length} still without the role. Deploy the template to them and check again.`)
            : (org.vendor === "gcp" ? "The identity can read every project under it." : "Every account the hub can see carries the role."),
        );
      },
      onError: (err: unknown) => toast.error(`Could not check the ${noun}s`, errorMessage(err, "Try again.")),
      onSettled: () => (checking = null),
    });
  }

  function handleCheckModels(row: AdminIntegration): void {
    checkingModels = row.id;
    checkModels.mutate(row.id, {
      onSuccess: (out) => {
        const failed = out.models.filter((m) => !m.ok);
        toast[out.answering ? "success" : "error"](
          `${out.answering} of ${out.total} model${out.total === 1 ? "" : "s"} answer`,
          failed.length ? failed.map((m) => `${m.name} failed: ${m.reason ?? "no answer"}`).join("; ") : "Every model you named answered.",
        );
      },
      onError: (err: unknown) => toast.error("Could not check the models", errorMessage(err, "Try again.")),
      onSettled: () => (checkingModels = null),
    });
  }

  function submitConnect(event: SubmitEvent): void {
    event.preventDefault();
    if (!form) return;
    formError = null;
    const replacing = form.mode === "replace";
    const identity: Record<string, string> = {};
    const secrets: Record<string, string> = {};
    let auth: string = form.entry.auth;
    if (form.mode === "org" && awsMode === "role") {
      auth = "aws_role";
      identity.role_arn = (form.values.role_arn ?? "").trim();
      if (useBedrock) {
        identity.models_provider = "bedrock";
        identity.models_region = (form.values.models_region ?? "").trim() || BEDROCK_DEFAULT_REGION;
      }
    } else if (form.mode === "org") {
      auth = "aws_hub";
      identity.hub_role_arn = (form.values.role_arn ?? "").trim();
    } else if (form.mode === "gcp" && gcpMode === "parent") {
      auth = "gcp_parent";
      identity.parent_resource = (form.values.parent_resource ?? "").trim();
    } else if (form.mode === "gcp") {
      auth = "gcp_identity";
      identity.project = (form.values.project ?? "").trim();
      const sa = (form.values.service_account ?? "").trim();
      if (sa) identity.service_account = sa;
      if (useVertex) {
        identity.models_provider = "vertex_ai";
        identity.models_location = (form.values.models_location ?? "").trim() || VERTEX_DEFAULT_LOCATION;
      }
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
          toast.success(replacing ? `${row.name} replaced` : `${row.name} connected`, row.state_detail ?? undefined);
          form = null;
        },
        onError: (err: unknown) => {
          formError = errorMessage(err, "Could not connect. Try again.");
        },
      },
    );
  }

  /** Replace (kobo#377): the same Add, with new values, over the row the room holds
   * or the one the deployment config stands behind; the room's value wins. */
  function openReplace(row: AdminIntegration): void {
    const entry = catalog.find((c) => c.id === row.vendor);
    if (entry) openConnect(entry, row);
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
    if (i.spec.kind === "org") {
      if (i.vendor !== "gcp") return "organization";
      return i.spec.parent_resource?.startsWith("organizations/") ? "organization" : "folder";
    }
    if (i.spec.kind === "account" || i.spec.parent) return childNoun(i);
    if (i.vendor === SELF_HOSTED) return "models";
    return i.vendor === "aws" || i.vendor === "gcp" ? childNoun(i) : "connection";
  }
  const PROVIDER_NAMES: Record<string, string> = { vertex_ai: "Vertex AI", bedrock: "Bedrock" };
  const modelsLabel = (i: AdminIntegration): string | null =>
    i.spec.models && !Array.isArray(i.spec.models)
      ? `Models: ${PROVIDER_NAMES[i.spec.models.provider] ?? i.spec.models.provider}, ${i.spec.models.location ?? i.spec.models.region ?? ""}`
      : null;
  // A Self-hosted row (kobo#390): the host alone (never the key, never the path), how many
  // of its models answer, and the ones that did not, each with the server's sentence.
  const selfHosted = (i: AdminIntegration): boolean => i.spec.auth === SELF_HOSTED_AUTH;
  const ownModels = (i: AdminIntegration): SelfHostedModel[] => (Array.isArray(i.spec.models) ? i.spec.models : []);
  function hostOf(url: string | undefined): string {
    try {
      return url ? new URL(url).host : "";
    } catch {
      return url ?? "";
    }
  }
  function modelsCount(i: AdminIntegration): string {
    const models = ownModels(i);
    const ok = models.filter((m) => m.ok).length;
    const noun = `model${models.length === 1 ? "" : "s"}`;
    return ok === models.length ? `${models.length} ${noun}` : `${ok} of ${models.length} ${noun} answer`;
  }
  const formTitle = $derived(
    !form ? "" : form.mode === "org" ? (awsMode === "role" ? "Connect an AWS account" : "Connect an AWS organization") : form.mode === "gcp" ? "Connect Google Cloud" : form.mode === "replace" ? `Replace ${form.name}` : form.entry.id === SELF_HOSTED ? "Connect self-hosted models" : `Add ${form.name || "an integration"}`,
  );
  const fromDeployment = (i: AdminIntegration): boolean => i.spec.source === "deployment";
  const roomOwn = (i: AdminIntegration): boolean => ROOM_VENDORS.has(i.vendor) && i.spec.source !== "access";
</script>

<section class="panel page" aria-labelledby="integrations-heading">
  <div>
    <h2 id="integrations-heading" class="page__title">Integrations</h2>
    <p class="page__lead">
      The vendors agents can reach. A cloud is an organization with the accounts connected under it,
      or a project, or a folder with its projects under it; a service vendor is one connection. Slack
      and GitHub connect the same way, and the room's own needs ride the same credential. What each
      agent may do there is granted under Permissions.
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
          {#if all.length === 0 && !search.trim()}
            <tr><td colspan="5"><p class="panel__empty">Nothing connected yet. Add an integration: Slack so the room can make agents' Slack apps, GitHub for the room's own pull requests, an AWS organization, a Google Cloud project, or a vendor the room holds one connection to.</p></td></tr>
          {/if}
          {#each visibleRoots as i (i.id)}
            <tr>
              <td>
                <div class="who">
                  <VendorMark id={i.vendor} name={i.name} size={16} />
                  <div class="who__text">
                    <span class="name">{i.name}</span>
                    {#if i.spec.address}<span class="quiet addr">{i.spec.address}</span>{/if}
                    {#if i.spec.project}<span class="quiet addr">{i.spec.project}</span>{/if}
                    {#if i.spec.parent_resource}<span class="quiet addr">{i.spec.parent_resource}</span>{/if}
                    {#if modelsLabel(i)}<span class="addr">{modelsLabel(i)}</span>{/if}
                    {#if selfHosted(i)}
                      <span class="addr">{SELF_HOSTED_SUMMARY}</span>
                      <span class="quiet addr">{hostOf(i.spec.base_url)}</span>
                      <span class="addr">{modelsCount(i)}</span>
                      {#each ownModels(i).filter((m) => !m.ok) as m (m.name)}
                        <span class="addr fail">{m.name} failed{m.reason ? `: ${m.reason}` : ""}</span>
                      {/each}
                    {/if}
                    {#if i.vendor === "slack"}<span class="quiet addr">Lets the room create agents' Slack apps.</span>{/if}
                    {#if i.vendor === "github" && i.spec.source !== "access"}<span class="quiet addr">The room's own pull requests and agent repositories{i.spec.room_only ? "" : "; what each agent may reach is picked on its Access tab"}.</span>{/if}
                    {#if i.spec.source === "access" && i.spec.kind !== "org"}<span class="quiet addr">in the permission service{i.spec.ceiling?.length ? `, ${i.spec.ceiling.length} permission${i.spec.ceiling.length === 1 ? "" : "s"} agents may ask for` : ""}</span>{/if}
                  </div>
                </div>
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
                    {checking === i.id ? "Checking…" : `Check ${childNoun(i)}s`}
                  </button>
                {/if}
                {#if selfHosted(i)}
                  <button type="button" class="btn btn--quiet" disabled={checkingModels !== null} onclick={() => handleCheckModels(i)}>
                    {checkingModels === i.id ? "Checking…" : "Check models"}
                  </button>
                {/if}
                {#if i.spec.pull_request}
                  <a class="btn btn--quiet" href={i.spec.pull_request} target="_blank" rel="noreferrer">Pull request</a>
                {/if}
                {#if fromDeployment(i)}
                  <!-- The deployment config carries it (an older install). A value pasted here
                       is held by the room and wins, the way a fresh install does it. -->
                  <span class="quiet" title="Set in the deployment config. Replace to hold it in the room instead; the room's value wins.">set in the deployment config</span>
                  <button type="button" class="btn btn--quiet" onclick={() => openReplace(i)}>Replace</button>
                {:else}
                  {#if roomOwn(i)}
                    <button type="button" class="btn btn--quiet" onclick={() => openReplace(i)}>Replace</button>
                  {/if}
                  <button type="button" class="btn btn--quiet" disabled={disconnect.isPending} onclick={() => (removing = i)}>Remove</button>
                {/if}
              </td>
            </tr>
            {#each childrenOf(i).filter((c) => !search.trim() || matches(c) || matches(i)) as c (c.id)}
              <tr class="child">
                <td>
                  <div class="who">
                    <span class="child__tick" aria-hidden="true">└</span>
                    <VendorMark id={c.vendor || i.vendor} name={c.name} size={14} />
                    <div class="who__text">
                      <span class="name">{c.name}</span>
                      {#if c.spec.account_id}<span class="quiet addr">{c.spec.account_id}</span>{/if}
                      {#if c.spec.project}<span class="quiet addr">{c.spec.project}</span>{/if}
                      {#if modelsLabel(c)}<span class="addr">{modelsLabel(c)}</span>{/if}
                      {#if c.spec.ceiling?.length}<span class="quiet addr">{c.spec.ceiling.length} permission{c.spec.ceiling.length === 1 ? "" : "s"} agents may ask for</span>{/if}
                    </div>
                  </div>
                </td>
                <td><span class="chip">{childNoun(c)}</span></td>
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
                  <div class="who">
                    <span class="child__tick" aria-hidden="true">└</span>
                    <VendorMark id={i.vendor} name={w.name} size={14} />
                    <div class="who__text">
                      <span class="name">{w.name}</span>
                      <span class="quiet addr">{w.id}</span>
                    </div>
                  </div>
                </td>
                <td><span class="chip">{childNoun(i)}</span></td>
                <td>
                  <span class="status status--warn" title={w.reason ?? ""}>
                    <span class="status__dot" aria-hidden="true"></span>{i.vendor === "gcp" ? "Not granted yet" : "No role yet"}
                  </span>
                </td>
                <td class="quiet"></td>
                <td class="panel__cell--right quiet">{i.vendor === "gcp" ? "Grant the identity the roles, then check again" : "Deploy the template, then check again"}</td>
              </tr>
            {/each}
          {:else}
            {#if search.trim()}
              <tr><td colspan="5"><p class="panel__empty">No integration matches that.</p></td></tr>
            {/if}
          {/each}
        </tbody>
      </table>
    </div>
  {/if}
</section>

<Modal open={catalogOpen} title="Add an integration" size="wide" onclose={() => (catalogOpen = false)}>
  <p class="catalog__lead">Pick a vendor. The room holds one connection to it and lends agents only what you grant under Permissions. Self-hosted models is the one that is only models: name an endpoint you run and its models join the pickers.</p>
  <div class="catalog">
    {#each addable as c (c.id)}
      <div class="catalog__card">
        <VendorMark id={c.id} name={c.name} size={22} />
        <div class="catalog__text">
          <span class="catalog__name">{c.name}</span>
          <p class="catalog__desc">{c.summary}</p>
        </div>
        <button type="button" class="btn catalog__add" aria-label={`Add ${c.name}`} onclick={() => openConnect(c)}>Add</button>
      </div>
    {/each}
  </div>
</Modal>

<Modal open={form !== null} title={formTitle} size={form?.vendor === "aws" || form?.vendor === "gcp" ? "wide" : "default"} onclose={() => (form = null)}>
  {#if form && form.mode === "org"}
    <form id="integration-form" class="form" onsubmit={submitConnect}>
      <label class="field">
        <span>{awsMode === "role" ? "Name for this account" : "Name for this organization"}</span>
        <input id="integration-name" type="text" bind:value={form.name} placeholder={awsMode === "role" ? "e.g. acme-models" : "e.g. acme-aws"} required autocomplete="off" />
      </label>
      <AwsConnect
        bind:roleArn={form.values.role_arn}
        bind:mode={awsMode}
        bind:useBedrock
        bind:bedrockRegion={form.values.models_region}
        pickShape
        integration={slug(form.name) || "aws"}
      />
      {#if formError}<p class="page__foot fail">{formError}</p>{/if}
    </form>
  {:else if form && form.mode === "gcp"}
    <form id="integration-form" class="form" onsubmit={submitConnect}>
      <label class="field">
        <span>Name for this connection</span>
        <input id="integration-name" type="text" bind:value={form.name} placeholder={gcpMode === "parent" ? "e.g. example-platform" : "e.g. example-prod"} required autocomplete="off" />
      </label>
      <ConnectGoogleCloudSheet
        bind:mode={gcpMode}
        bind:project={form.values.project}
        bind:serviceAccount={form.values.service_account}
        bind:parentResource={form.values.parent_resource}
        bind:useVertex
        bind:location={form.values.models_location}
        integration={slug(form.name) || "gcp"}
      />
      {#if formError}<p class="page__foot fail">{formError}</p>{/if}
      {#if connect.isPending}
        <p class="page__foot busy" role="status"><span class="spin" aria-hidden="true"></span>
          {gcpMode === "parent" ? "Saving the folder, then checking its projects." : "Checking the project with Google Cloud."}
        </p>
      {/if}
    </form>
  {:else if form}
    <form id="integration-form" class="form" onsubmit={submitConnect}>
      <label class="field">
        <span>{form.entry.id === SELF_HOSTED ? "Name for this endpoint" : "Name"}</span>
        <input id="integration-name" type="text" bind:value={form.name} placeholder={form.entry.id === SELF_HOSTED ? "e.g. lab-gpu-pool" : ""} required autocomplete="off" />
      </label>
      {#each form.entry.fields as f (f.key)}
        <label class="field">
          <span>{f.label}{#if f.optional} <span class="quiet">(optional)</span>{/if}</span>
          {#if f.multiline}
            <textarea id="integration-{f.key}" rows="6" bind:value={form.values[f.key]} placeholder={f.placeholder ?? ""} autocomplete="off" spellcheck="false" required={!f.optional}></textarea>
          {:else}
            <input
              id="integration-{f.key}"
              type={f.secret ? "password" : "text"}
              bind:value={form.values[f.key]}
              autocomplete="off"
              placeholder={f.placeholder ?? ""}
              required={!f.optional}
            />
          {/if}
        </label>
      {/each}
      {#if form.entry.note}<p class="page__foot">{form.entry.note}</p>{/if}
      {#if form.entry.auth === SLACK_AUTH}
        <!-- Nothing for the gateway and no read/write choice: the pair is the room's own. -->
      {:else if form.entry.auth === SELF_HOSTED_AUTH}
        <!-- Only models: nothing is leased, so no address for the gateway and no read/write choice. -->
      {:else if form.entry.auth === DATADOG_AUTH}
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
          {#if form.entry.auth === SLACK_AUTH}Checking the room can keep the pair, then saving it.
          {:else if form.entry.auth === SELF_HOSTED_AUTH}Listing the models at the address, then asking each one you named for a short answer.
          {:else if form.entry.auth === GITHUB_AUTH && !accessOn}Saving the app for the room.
          {:else if SERVICE_AUTHS.has(form.entry.auth)}Checking with {VENDOR_OF_AUTH[form.entry.auth]}, then adding its MCP server to the gateway. This can take up to a minute.
          {:else if form.vendor === "aws"}Checking the role with AWS.
          {:else}Storing the credential and adding the target to the gateway. This can take a moment.{/if}
        </p>
      {/if}
      {#if form.entry.auth === SELF_HOSTED_AUTH}
        <p class="page__foot">The key, if you give one, is kept by the room itself and never shown again. The row connects as long as one model answers; the ones that do not are marked on it, and Check models asks again.</p>
      {:else if ROOM_AUTHS.has(form.entry.auth)}
        <p class="page__foot">The values are kept by the room itself and never shown again{#if form.entry.auth === GITHUB_AUTH && accessOn}; the permission service checks the app with GitHub before anything is saved{/if}.</p>
      {:else if SERVICE_AUTHS.has(form.entry.auth)}
        <p class="page__foot">The keys are kept for the permission service alone and never shown again; it checks them with {VENDOR_OF_AUTH[form.entry.auth]} before anything is saved.</p>
      {:else if form.entry.fields.some((f) => f.secret)}
        <p class="page__foot">Secret values are stored in the room's secrets service and never shown again.</p>
      {/if}
    </form>
  {/if}
  {#snippet footer()}
    <button type="button" class="btn" onclick={() => (form = null)}>Cancel</button>
    <button type="submit" form="integration-form" class="btn btn--primary" disabled={connect.isPending}>
      {#if connect.isPending}<span class="spin spin--btn" aria-hidden="true"></span>{form?.vendor === "aws" ? "Checking with AWS…" : form?.vendor === "gcp" ? "Checking with Google Cloud…" : form?.vendor === SELF_HOSTED ? "Checking the models…" : form && SERVICE_AUTHS.has(form.entry.auth) && (form.entry.auth !== GITHUB_AUTH || accessOn) ? `Checking with ${VENDOR_OF_AUTH[form.entry.auth]}…` : "Saving…"}
      {:else}{form?.vendor === "aws" || form?.vendor === "gcp" || form?.vendor === SELF_HOSTED ? "Connect" : form?.mode === "replace" ? "Replace" : "Add"}{/if}
    </button>
  {/snippet}
</Modal>

<Modal open={removing !== null} title={removing ? `Remove ${removing.name}?` : ""} size="narrow" onclose={() => (removing = null)}>
  <p class="page__lead">
    {#if removing?.spec.kind === "org" && removing.vendor === "gcp"}
      The folder or organization is removed from the permission service. Its projects must be removed first.
    {:else if removing?.spec.kind === "org"}
      The organization is removed from the permission service. Its accounts must be removed first.
    {:else if removing && selfHosted(removing)}
      Its models come off the New agent and Change model lists and off the model gateway; agents already on one of them keep their setting until their model is changed. The key, if there was one, is forgotten.
    {:else if removing && modelsLabel(removing)}
      The connection is removed from the permission service and every grant on it is revoked. Its models come off the New agent and Change model lists and off the model gateway; agents already on one of them keep their setting until their model is changed.
    {:else if removing?.vendor === "slack"}
      The room forgets its Slack config tokens. Agents keep the Slack apps they already have, but no new app can be connected until Slack is added again, or the deployment config carries a pair; every agent's Slack row greys out until then.
    {:else if removing?.vendor === "github" && removing.spec.source !== "access"}
      The room forgets the GitHub app. Its own pull requests (skills, tooling) stop until GitHub is added again, or the deployment config carries an app{#if !removing.spec.room_only}; the connection is removed from the permission service and every grant on it is revoked, and GitHub's tools come off the gateway{/if}.
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

  .child td:first-child { padding-left: 1.25rem; }
  /* The mark beside the name, the name and its line under it to the right. */
  .who { display: flex; align-items: center; gap: var(--space-sm); min-width: 0; }
  .who__text { display: flex; flex-direction: column; min-width: 0; }
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

  /* The vendor catalog: a two-column grid of cards. */
  .catalog__lead {
    margin: 0 0 var(--space-md);
    color: var(--ink);
    font-size: 0.875rem;
  }

  .catalog {
    display: grid;
    grid-template-columns: repeat(auto-fill, minmax(17rem, 1fr));
    gap: var(--space-sm);
  }

  .catalog__card {
    display: grid;
    grid-template-columns: auto minmax(0, 1fr) auto;
    align-items: center;
    gap: var(--space-sm);
    padding: var(--space-md);
    border: 1px solid var(--border);
    border-radius: var(--radius-sm);
    background: var(--panel);
    transition: border-color 0.15s ease, background 0.15s ease;
  }

  .catalog__card:hover {
    border-color: var(--ring);
    background: color-mix(in oklab, var(--accent-soft) 50%, var(--panel));
  }


  .catalog__text {
    min-width: 0;
  }

  .catalog__name {
    color: var(--ink);
    font-size: 0.92rem;
    font-weight: 600;
  }

  .catalog__desc {
    margin: 0.15rem 0 0;
    color: var(--ink);
    font-size: 0.8rem;
    line-height: 1.45;
  }

  .catalog__add {
    align-self: center;
  }
</style>
