<script lang="ts">
  // Connect AWS: pick what the agents may read, create the role one of three
  // ways, paste its ARN. The room checks the role with AWS before it is saved.
  import { createQuery } from "@tanstack/svelte-query";
  import { adminAwsSetupQuery } from "../../lib/queries";

  let { roleArn = $bindable("") }: { roleArn?: string } = $props();

  let packs = $state<string[]>(["logs"]);
  const setup = createQuery(() => (adminAwsSetupQuery(packs)));

  const TABS = ["Terraform", "CloudFormation", "By hand"] as const;
  let tab = $state<(typeof TABS)[number]>("Terraform");
  let copied = $state<string | null>(null);

  function togglePack(id: string, on: boolean): void {
    packs = on ? [...packs, id] : packs.filter((p) => p !== id);
  }

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
    Object.entries(setup.data?.cloudformation.parameters ?? {}).map(([k, v]) => `${k}=${v}`).join("\n"),
  );
</script>

{#if setup.isPending}
  <p class="page__foot">Reading what the gateway is…</p>
{:else if setup.isError || !setup.data}
  <p class="page__foot fail">Could not read the set-up details just now.</p>
{:else}
  {@const s = setup.data}
  <section class="step">
    <h3 class="step__title">1. What agents may read</h3>
    <div class="packs">
      {#each s.packs as p (p.id)}
        <label class="pack">
          <input type="checkbox" checked={p.required || packs.includes(p.id)} disabled={p.required} onchange={(e) => togglePack(p.id, e.currentTarget.checked)} />
          <span><span class="pack__name">{p.id}</span> <span class="quiet">{p.purpose}</span></span>
        </label>
      {/each}
    </div>
    <p class="page__foot">Read-only, configuration not data. The role is named {s.role_name}; the owner of the account can narrow it further.</p>
  </section>

  <section class="step">
    <h3 class="step__title">2. Create the role in the account</h3>
    {#if s.identity}
      <p class="page__foot">The role trusts the gateway's identity <code>{s.identity.subject}</code>{#if s.identity.email} ({s.identity.email}){/if}, nothing else. No key is created.</p>
    {:else if s.identity_note}
      <p class="page__foot fail">{s.identity_note}</p>
    {/if}
    <div class="tabs" role="tablist" aria-label="How to create the role">
      {#each TABS as t (t)}
        <button type="button" role="tab" class="tab" aria-selected={tab === t} onclick={() => (tab = t)}>{t}</button>
      {/each}
    </div>
    {#if tab === "Terraform"}
      <div class="code">
        <pre>{s.terraform}</pre>
        <button type="button" class="btn btn--quiet code__copy" onclick={() => copy("tf", s.terraform)}>{copied === "tf" ? "Copied" : "Copy"}</button>
      </div>
      <p class="page__foot">Apply it with the account's own pipeline, then paste the <code>kobo_role_arn</code> output below.</p>
    {:else if tab === "CloudFormation"}
      <div class="code">
        <pre>{params}</pre>
        <button type="button" class="btn btn--quiet code__copy" onclick={() => copy("cfn", s.cloudformation.template)}>{copied === "cfn" ? "Copied" : "Copy template"}</button>
      </div>
      <p class="page__foot">Create a stack from the copied template with these parameters, or a StackSet across the organisation, then paste the stack's <code>RoleArn</code> output below.</p>
    {:else}
      <div class="code">
        <pre>{s.trust_policy}</pre>
        <button type="button" class="btn btn--quiet code__copy" onclick={() => copy("trust", s.trust_policy)}>{copied === "trust" ? "Copied" : "Copy"}</button>
      </div>
      <p class="page__foot">Create a role with this trust policy and attach ViewOnlyAccess{#if packs.includes("security-audit")} and SecurityAudit{/if}; the other packs are inline statements in the template.</p>
    {/if}
  </section>

  <section class="step">
    <h3 class="step__title">3. Paste the role</h3>
    <label class="field">
      <span>Role ARN</span>
      <input id="integration-role_arn" type="text" bind:value={roleArn} autocomplete="off" placeholder="arn:aws:iam::123456789012:role/{s.role_name}" required />
    </label>
    <p class="page__foot">{s.checked ? "The room asks the gateway to assume the role once before saving it." : "The room cannot check the role with AWS from here; it is saved as given."}</p>
  </section>
{/if}

<style>
  .step { display: flex; flex-direction: column; gap: var(--space-xs); }
  .step__title { margin: var(--space-sm) 0 0; font-size: 0.95rem; }
  .packs { display: flex; flex-direction: column; gap: var(--space-2xs); }
  .pack { display: flex; align-items: baseline; gap: var(--space-xs); }
  .pack__name { font-weight: 600; }
  .quiet { color: var(--muted); font-size: 0.85rem; }
  .fail { color: var(--fail); }
  .tabs { display: flex; gap: var(--space-xs); border-bottom: 1px solid var(--border); }
  .tab {
    padding: var(--space-xs) var(--space-sm);
    border: none;
    border-bottom: 2px solid transparent;
    background: none;
    color: var(--muted);
    font: inherit;
    font-weight: 600;
    cursor: pointer;
  }
  .tab[aria-selected="true"] { border-bottom-color: var(--accent); color: var(--ink); }
  .code { position: relative; }
  .code pre {
    margin: 0;
    padding: var(--space-sm);
    overflow: auto;
    max-height: 16rem;
    border: 1px solid var(--border);
    border-radius: var(--radius-sm);
    background: var(--panel);
    font-size: 0.78rem;
    user-select: all;
  }
  .code__copy { position: absolute; top: var(--space-2xs); right: var(--space-2xs); }
</style>
