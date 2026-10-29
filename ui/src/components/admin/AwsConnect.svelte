<script lang="ts">
  // Connect AWS: the account owner creates one read-only role that trusts
  // the room's identity, the admin pastes its ARN, the room checks it with
  // AWS before saving. With the permission service on, the identity is its
  // (it mints the hour-long leases) and what each agent may do inside the
  // role is what it is granted under Permissions; otherwise the identity is
  // the gateway's and each agent gets a lane under Connections.
  import { createQuery } from "@tanstack/svelte-query";
  import { adminAwsSetupQuery } from "../../lib/queries";

  let { roleArn = $bindable("") }: { roleArn?: string } = $props();

  const ALL_PACKS = ["logs", "security-audit", "cost"];
  const setup = createQuery(() => (adminAwsSetupQuery(ALL_PACKS)));

  const TABS = ["Terraform", "CloudFormation", "By hand"] as const;
  let tab = $state<(typeof TABS)[number]>("Terraform");
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

{#if setup.isPending}
  <p class="hint">Reading the room's identity…</p>
{:else if setup.isError || !setup.data}
  <p class="hint fail">Could not read the set-up details just now.</p>
{:else}
  {@const s = setup.data}
  <ol class="steps">
    <li class="step">
      <h3 class="step__title">What the role allows</h3>
      <p class="step__text">
        Read-only across the account: configuration, never data.
        {#if s.principal === "access"}
          Agents hold nothing by default; each is granted the services it needs under Permissions and
          borrows an hour of access scoped to them.
        {:else}
          Each agent gets its own narrower profile inside it under Connections.
        {/if}
      </p>
      <ul class="packs">
        {#each s.packs as p (p.id)}
          <li class="pack"><span class="pack__name">{p.id}</span><span class="pack__what">{p.purpose}</span></li>
        {/each}
      </ul>
    </li>

    <li class="step">
      <h3 class="step__title">Create the role in the account</h3>
      {#if s.identity}
        <dl class="facts">
          <div><dt>Trusts</dt><dd><code>{s.identity.subject}</code></dd></div>
          {#if s.identity.email}<div><dt>Which is</dt><dd>{s.identity.email}{s.principal === "access" ? ", the permission service that mints the leases" : ", the tool gateway"}</dd></div>{/if}
          <div><dt>Named</dt><dd>{s.role_name}</dd></div>
        </dl>
      {:else if s.identity_note}
        <p class="hint fail">{s.identity_note}</p>
      {/if}
      <div class="tabs" role="tablist" aria-label="How to create the role">
        {#each TABS as t (t)}
          <button type="button" role="tab" class="tab" aria-selected={tab === t} onclick={() => (tab = t)}>{t}</button>
        {/each}
      </div>
      {#if tab === "Terraform"}
        <div class="code">
          <pre>{s.terraform}</pre>
          <button type="button" class="code__copy" onclick={() => copy("tf", s.terraform)}>{copied === "tf" ? "Copied" : "Copy"}</button>
        </div>
        <p class="step__text">Apply it from the account's own pipeline, then paste the <code>kobo_role_arn</code> output below.</p>
      {:else if tab === "CloudFormation"}
        <div class="code">
          <pre>{params}</pre>
          <button type="button" class="code__copy" onclick={() => copy("cfn", s.cloudformation.template)}>{copied === "cfn" ? "Copied" : "Copy template"}</button>
        </div>
        <p class="step__text">Create a stack from the copied template with these parameters, or a StackSet across the organisation, then paste the stack's <code>RoleArn</code> output below.</p>
      {:else}
        <div class="code">
          <pre>{s.trust_policy}</pre>
          <button type="button" class="code__copy" onclick={() => copy("trust", s.trust_policy)}>{copied === "trust" ? "Copied" : "Copy"}</button>
        </div>
        <p class="step__text">Create a role with this trust policy and attach <code>ViewOnlyAccess</code> and <code>SecurityAudit</code>; the cost and logs reads are the inline statements in the template.</p>
      {/if}
    </li>

    <li class="step">
      <h3 class="step__title">Paste the role</h3>
      <label class="field">
        <span>Role ARN</span>
        <input id="integration-role_arn" type="text" bind:value={roleArn} autocomplete="off" spellcheck="false" placeholder="arn:aws:iam::123456789012:role/{s.role_name}" required />
      </label>
      <p class="hint">{s.checked ? `Connect asks ${s.principal === "access" ? "the permission service" : "the gateway"} to assume the role once; nothing is saved until AWS agrees.` : "The room cannot check the role with AWS from here; it is saved as given."}</p>
    </li>
  </ol>
{/if}

<style>
  .steps {
    display: flex;
    flex-direction: column;
    gap: var(--space-lg);
    margin: 0;
    padding: 0;
    list-style: none;
    counter-reset: step;
  }
  .step {
    display: flex;
    flex-direction: column;
    gap: var(--space-sm);
    padding-left: 2.25rem;
    position: relative;
    counter-increment: step;
  }
  .step::before {
    content: counter(step);
    position: absolute;
    left: 0;
    top: 0;
    width: 1.5rem;
    height: 1.5rem;
    border-radius: 999px;
    background: var(--accent);
    color: var(--accent-ink);
    font-size: 0.8rem;
    font-weight: 700;
    line-height: 1.5rem;
    text-align: center;
  }
  .step__title { margin: 0; font-size: 1rem; line-height: 1.5rem; color: var(--ink); }
  .step__text { margin: 0; color: var(--ink); line-height: 1.5; }
  .hint { margin: 0; color: var(--muted); font-size: 0.9rem; }
  .fail { color: var(--fail); }

  .packs {
    display: grid;
    grid-template-columns: max-content 1fr;
    gap: var(--space-2xs) var(--space-md);
    margin: 0;
    padding: var(--space-sm) var(--space-md);
    border: 1px solid var(--border);
    border-radius: var(--radius-sm);
    list-style: none;
  }
  .pack { display: contents; }
  .pack__name { font-family: var(--font-mono); font-size: 0.85rem; font-weight: 600; color: var(--ink); }
  .pack__what { color: var(--muted); font-size: 0.9rem; }

  .facts { display: grid; grid-template-columns: max-content 1fr; gap: var(--space-2xs) var(--space-md); margin: 0; }
  .facts div { display: contents; }
  .facts dt { color: var(--muted); font-size: 0.9rem; }
  .facts dd { margin: 0; color: var(--ink); }
  .facts code { font-size: 0.9rem; }

  .tabs { display: flex; gap: var(--space-md); border-bottom: 1px solid var(--border); }
  .tab {
    padding: var(--space-xs) 0;
    border: none;
    border-bottom: 2px solid transparent;
    margin-bottom: -1px;
    background: none;
    color: var(--muted);
    font: inherit;
    font-weight: 600;
    cursor: pointer;
  }
  .tab:hover { color: var(--ink); }
  .tab[aria-selected="true"] { border-bottom-color: var(--ink); color: var(--ink); }

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
