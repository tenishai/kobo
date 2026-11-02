<script lang="ts">
  // Connect AWS. Pick how you create roles first; the sheet then shows only
  // that method (#276). An organization is one hub role in its management
  // account, trusting the room's identity (the permission service's, which
  // mints the hour-long leases), plus one spoke template every member account
  // gets with the organization's one external id; accounts that carry the
  // role register themselves (#275). A flat role is the pre-organization
  // shape, now also the way one account is connected on its own. The admin
  // pastes the role's ARN and the room checks it with AWS before saving.
  // An account (a flat role or a member account) may also serve the room's
  // Bedrock models (kobo#390): tick the box, pick the region, and the role's
  // snippets carry the Bedrock permissions block; the trust is unchanged.
  import { createQuery } from "@tanstack/svelte-query";
  import { adminAwsSetupQuery } from "../../lib/queries";

  let {
    roleArn = $bindable(""),
    mode = $bindable("role"),
    useBedrock = $bindable(false),
    bedrockRegion = $bindable("us-east-1"),
    pickShape = false,
    integration = "aws",
    parent = null,
    accountId = null,
  }: {
    roleArn?: string;
    mode?: "role" | "org" | "account";
    useBedrock?: boolean;
    bedrockRegion?: string;
    /** Offer the choice between an organization and one account at the top. */
    pickShape?: boolean;
    integration?: string;
    parent?: string | null;
    accountId?: string | null;
  } = $props();

  const ALL_PACKS = ["logs", "security-audit", "cost"];
  const bedrockHere = $derived(mode !== "org");
  const setup = createQuery(() => ({
    ...adminAwsSetupQuery({
      packs: ALL_PACKS, mode, integration, parent: parent ?? undefined, account_id: accountId ?? undefined,
      bedrock: bedrockHere && useBedrock,
    }),
    enabled: mode !== "account" || Boolean(parent && accountId),
  }));

  const SHAPES: { id: "org" | "role"; name: string; what: string }[] = [
    { id: "org", name: "An organization", what: "Its management account's hub role, then every account under it." },
    { id: "role", name: "One account", what: "A role in one account the room assumes directly; it can serve Bedrock models too." },
  ];

  type Method = "Terraform" | "CloudFormation" | "By hand";
  const METHODS: { id: Method; what: string }[] = [
    { id: "Terraform", what: "A module from the account's own pipeline." },
    { id: "CloudFormation", what: "A template, as a stack or a StackSet." },
    { id: "By hand", what: "Trust and permissions policies to paste into the console." },
  ];
  let method = $state<Method>("Terraform");
  let copied = $state<string | null>(null);

  async function copy(label: string, text: string): Promise<void> {
    try {
      await navigator.clipboard.writeText(text);
      copied = label;
      setTimeout(() => (copied = null), 1500);
    } catch {
      copied = null;
    }
  }

  const params = $derived(
    Object.entries(setup.data?.cloudformation.parameters ?? {}).map(([k, v]) => `${k} = ${v}`).join("\n"),
  );
</script>

{#snippet code(label: string, text: string, copyLabel = "Copy")}
  <div class="code">
    <pre>{text}</pre>
    <button type="button" class="code__copy" onclick={() => copy(label, text)}>{copied === label ? "Copied" : copyLabel}</button>
  </div>
{/snippet}

{#if pickShape && mode !== "account"}
  <div class="methods methods--two" role="radiogroup" aria-label="What you connect">
    {#each SHAPES as sh (sh.id)}
      <button type="button" role="radio" class="method" aria-checked={mode === sh.id} onclick={() => (mode = sh.id)}>
        <span class="method__name">{sh.name}</span>
        <span class="method__what">{sh.what}</span>
      </button>
    {/each}
  </div>
{/if}

{#if mode === "account" && !(parent && accountId)}
  <p class="hint">Pick the account first.</p>
{:else if setup.isPending}
  <p class="hint">Reading the room's identity…</p>
{:else if setup.isError || !setup.data}
  <p class="hint fail">Could not read the set-up details just now.</p>
{:else}
  {@const s = setup.data}
  <div class="sheet">
    {#if s.mode === "org"}
      <p class="step__text">
        One hub role in the management account: it assumes <code>{s.spoke_role_name}</code> in each member account,
        lists the accounts and reads the bill, nothing else. One spoke role in every member account, from one template
        with the organization's one external id. Accounts that carry the role connect themselves.
      </p>
    {:else if s.mode === "account"}
      <p class="step__text">
        Read-only in account <code>{s.account_id}</code>: configuration, never data. The role trusts the organization's
        hub with the organization's external id.
      </p>
    {:else}
      <p class="step__text">
        Read-only across the account: configuration, never data.
        {#if s.principal === "access"}Agents are granted services under Permissions and borrow an hour of access.{/if}
        {#if useBedrock}The role may also call the account's Bedrock models, for the room alone.{/if}
      </p>
    {/if}

    {#if s.identity}
      <dl class="facts">
        <div><dt>Trusts</dt><dd><code>{s.identity.subject}</code>{#if s.identity.email}<span class="quiet"> ({s.identity.email})</span>{/if}</dd></div>
        {#if s.mode === "org"}<div><dt>External id</dt><dd><code>{s.external_id}</code></dd></div>{/if}
      </dl>
    {:else if s.mode === "account"}
      <dl class="facts">
        <div><dt>Trusts</dt><dd><code>{s.hub_role_arn}</code></dd></div>
        <div><dt>External id</dt><dd><code>{s.external_id}</code></dd></div>
      </dl>
    {:else if s.identity_note}
      <p class="hint fail">{s.identity_note}</p>
    {/if}

    {#if bedrockHere}
      <label class="check">
        <input id="integration-models" type="checkbox" bind:checked={useBedrock} />
        <span>Use this account's Bedrock models in the room</span>
      </label>
      {#if useBedrock}
        <label class="field field--inset">
          <span>Region</span>
          <input id="integration-models_region" type="text" bind:value={bedrockRegion} autocomplete="off" spellcheck="false" placeholder={s.bedrock_default_region ?? "us-east-1"} pattern={"[a-z]{2}(-gov)?-[a-z]+-[0-9]"} />
        </label>
        <p class="hint hint--inset">The region the room calls Bedrock in, such as {s.bedrock_default_region ?? "us-east-1"}. The models the room's list names on AWS then appear under AWS when you pick a model.</p>
      {/if}
    {/if}

    <div class="methods" role="radiogroup" aria-label="How you create roles">
      {#each METHODS as m (m.id)}
        <button type="button" role="radio" class="method" aria-checked={method === m.id} onclick={() => (method = m.id)}>
          <span class="method__name">{m.id}</span>
          <span class="method__what">{m.what}</span>
        </button>
      {/each}
    </div>

    <section class="block">
      <h3 class="step__title">{s.mode === "org" ? "In the management account" : "In the account"}</h3>
      {#if method === "Terraform"}
        {@render code("tf", s.terraform)}
        <p class="hint">Apply, then paste the <code>{s.mode === "org" ? "kobo_hub_role_arn" : "kobo_role_arn"}</code> output below.{#if s.bedrock_policy} The second resource is the Bedrock permissions on the same role.{/if}</p>
      {:else if method === "CloudFormation"}
        {#if s.mode === "org"}
          {@render code("perm", s.permissions_policy ?? "", "Copy permissions")}
          <p class="hint">No template for the hub yet: create the role with the trust policy under By hand and this permissions policy.</p>
        {:else}
          {@render code("cfn", s.cloudformation.template, "Copy template")}
          <pre class="params">{params}</pre>
          <p class="hint">Create a stack from the template with these parameters, then paste its <code>RoleArn</code> output below.</p>
          {#if s.bedrock_policy}
            {@render code("bedrock", s.bedrock_policy, "Copy Bedrock policy")}
            <p class="hint">The template does not know Bedrock: attach this policy to the role once the stack has created it.</p>
          {/if}
        {/if}
      {:else}
        {@render code("trust", s.trust_policy)}
        {#if s.mode === "org" && s.permissions_policy}
          {@render code("perm", s.permissions_policy, "Copy permissions")}
          <p class="hint">A role named <code>{s.role_name}</code> with this trust policy and this permissions policy.</p>
        {:else}
          <p class="hint">A role named <code>{s.role_name}</code> with this trust policy, <code>ViewOnlyAccess</code> and <code>SecurityAudit</code>{#if s.bedrock_policy}, and this Bedrock policy{/if}.</p>
          {#if s.bedrock_policy}
            {@render code("bedrock", s.bedrock_policy, "Copy Bedrock policy")}
          {/if}
        {/if}
      {/if}
    </section>

    {#if s.mode === "org"}
      <section class="block">
        <h3 class="step__title">In every member account, once</h3>
        {#if method === "Terraform"}
          {@render code("spoke-tf", s.spoke_terraform ?? "")}
          <p class="hint">The same module in each account's pipeline. {#if !s.hub_role_arn}The hub ARN fills in after you connect; Check accounts then finds the accounts that have the role.{/if}</p>
        {:else if method === "CloudFormation"}
          {@render code("stackset", s.stackset ?? "", "Copy commands")}
          <p class="hint">One StackSet from the management account puts the role in every account. Template: the Copy template button above under the account method. {#if !s.hub_role_arn}The hub ARN fills in after you connect.{/if}</p>
        {:else}
          {@render code("spoke-trust", s.spoke_trust_policy ?? "")}
          <p class="hint">A role named <code>{s.spoke_role_name}</code> in each account with this trust policy, <code>ViewOnlyAccess</code> and <code>SecurityAudit</code>.</p>
        {/if}
      </section>
    {/if}

    <label class="field">
      <span>{s.mode === "org" ? "Hub role ARN" : "Role ARN"}</span>
      <input id="integration-role_arn" type="text" bind:value={roleArn} autocomplete="off" spellcheck="false" placeholder="arn:aws:iam::{s.mode === 'account' ? s.account_id : '123456789012'}:role/{s.role_name}" required />
    </label>
    <p class="hint">
      {#if s.mode === "account"}
        Connect asks the permission service to reach the role through the organization's hub once; nothing is saved until AWS agrees.
      {:else if s.checked}
        Connect asks {s.principal === "access" ? "the permission service" : "the gateway"} to assume the role once; nothing is saved until AWS agrees.{#if s.mode === "org"} Then its accounts are checked for the spoke role.{/if}{#if bedrockHere && useBedrock} Its Bedrock models in {bedrockRegion.trim() || s.bedrock_default_region || "us-east-1"} are then put on the room's model list and checked the first time an agent uses one.{/if}
      {:else}
        The room cannot check the role with AWS from here; it is saved as given.
      {/if}
    </p>
  </div>
{/if}

<style>
  .sheet { display: flex; flex-direction: column; gap: var(--space-md); }
  .block { display: flex; flex-direction: column; gap: var(--space-sm); }
  .methods { display: grid; grid-template-columns: repeat(3, minmax(0, 1fr)); gap: var(--space-sm); }
  .methods--two { grid-template-columns: repeat(2, minmax(0, 1fr)); margin-bottom: var(--space-md); }
  .check { display: flex; align-items: center; gap: var(--space-sm); color: var(--ink); cursor: pointer; }
  .check input { margin: 0; }
  .field--inset { padding-left: var(--space-md); }
  .hint--inset { padding-left: var(--space-md); }
  .method {
    display: flex;
    flex-direction: column;
    gap: 2px;
    padding: var(--space-sm) var(--space-md);
    border: 1px solid var(--border);
    border-radius: var(--radius-sm);
    background: none;
    color: var(--ink);
    font: inherit;
    text-align: left;
    cursor: pointer;
  }
  .method[aria-checked="true"] { border-color: var(--ink); box-shadow: inset 0 0 0 1px var(--ink); }
  .method__name { font-weight: 600; }
  .method__what { color: var(--muted); font-size: 0.85rem; }
  .params {
    margin: 0;
    padding: var(--space-sm) var(--space-md);
    border: 1px solid var(--border);
    border-radius: var(--radius-sm);
    font-family: var(--font-mono);
    font-size: 0.8rem;
    white-space: pre-wrap;
  }
  .quiet { color: var(--muted); }
  .step__title { margin: 0; font-size: 1rem; line-height: 1.5rem; color: var(--ink); }
  .step__text { margin: 0; color: var(--ink); line-height: 1.5; }
  .hint { margin: 0; color: var(--muted); font-size: 0.9rem; }
  .fail { color: var(--fail); }


  .facts { display: grid; grid-template-columns: max-content 1fr; gap: var(--space-2xs) var(--space-md); margin: 0; }
  .facts div { display: contents; }
  .facts dt { color: var(--muted); font-size: 0.9rem; }
  .facts dd { margin: 0; color: var(--ink); }
  .facts code { font-size: 0.9rem; }


  .code { position: relative; }
  .code pre {
    margin: 0;
    padding: var(--space-md);
    padding-right: 5.5rem;
    overflow: auto;
    max-height: 18rem;
    border-radius: var(--radius-sm);
    background: var(--ink);
    color: var(--bg);
    font-family: var(--font-mono);
    font-size: 0.8rem;
    line-height: 1.5;
    white-space: pre-wrap;
    word-break: break-all;
  }
  .code__copy {
    position: absolute;
    top: var(--space-xs);
    right: var(--space-xs);
    padding: 0.2rem var(--space-xs);
    border: 1px solid color-mix(in srgb, var(--bg) 35%, transparent);
    border-radius: var(--radius-sm);
    background: color-mix(in srgb, var(--bg) 12%, transparent);
    color: var(--bg);
    font: inherit;
    font-size: 0.8rem;
    cursor: pointer;
  }
  .code__copy:hover { background: color-mix(in srgb, var(--bg) 24%, transparent); }
</style>
