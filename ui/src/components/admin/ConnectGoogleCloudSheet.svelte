<script lang="ts">
  // Connect Google Cloud (kobo#390, organization first since kobo#405). No key
  // and no role to create: the room's identity (whatever the permission service
  // reports) is granted the role packs once, on the organization; the admin then
  // picks which of the projects under it to connect (2026-11-09: nothing connects
  // by itself); one project is the other scope, and a project connected for its
  // Vertex AI models alone is held with nothing an agent could be granted. The sheet shows who to grant, the roles three ways (gcloud,
  // Terraform, by hand), then the form. Either scope may name the Vertex AI
  // project whose models the room offers: tick it and the room checks it can
  // list models there at the location before saving. Nothing is saved until
  // Google Cloud agrees.
  import { createQuery, keepPreviousData } from "@tanstack/svelte-query";
  import { adminGcpSetupQuery } from "../../lib/queries";

  let {
    mode = $bindable("parent"),
    project = $bindable(""),
    parentResource = $bindable(""),
    useVertex = $bindable(false),
    modelsOnly = $bindable(false),
    vertexProject = $bindable(""),
    location = $bindable("global"),
    integration = "gcp",
  }: {
    mode?: "project" | "parent";
    project?: string;
    parentResource?: string;
    useVertex?: boolean;
    modelsOnly?: boolean;
    vertexProject?: string;
    location?: string;
    integration?: string;
  } = $props();

  // The page re-renders with the id once it reads whole, so the snippets name the real
  // project or organization; a half-typed id keeps the placeholder rather than a fetch per key.
  const PROJECT_ID = /^[a-z][a-z0-9-]{4,28}[a-z0-9]$/;
  const PARENT = /^(folders|organizations)\/\d+$/;
  const typed = $derived(mode === "project" ? project.trim() : parentResource.trim());
  const target = $derived((mode === "project" ? PROJECT_ID : PARENT).test(typed) ? typed : "");
  // The name, the scope, the id and the tick all feed the query; the last page stays on screen
  // while the next one loads, so the form never blinks away under a typing hand.
  const setup = createQuery(() => ({
    ...adminGcpSetupQuery({
      packs: useVertex ? ["models"] : [],
      mode,
      integration,
      target: target || undefined,
    }),
    placeholderData: keepPreviousData,
  }));

  // On the project scope the Vertex AI project is the project itself until the person
  // names another; on the organization scope it is always typed.
  let vertexTyped = $state(false);
  $effect(() => {
    if (mode === "project" && !vertexTyped) vertexProject = project;
  });

  const SCOPES: { id: "project" | "parent"; name: string; what: string }[] = [
    { id: "parent", name: "An organization or folder", what: "Granted once. You then pick which of the projects under it to connect." },
    { id: "project", name: "A project", what: "One project: its resources, and its Vertex AI models if you tick them." },
  ];
  type Method = "gcloud" | "Terraform" | "By hand";
  const METHODS: { id: Method; what: string }[] = [
    { id: "gcloud", what: "Commands to run as someone who may set IAM there." },
    { id: "Terraform", what: "Resources for the pipeline that owns IAM there." },
    { id: "By hand", what: "The roles to add in the console." },
  ];
  let method = $state<Method>("gcloud");
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
</script>

{#snippet code(label: string, text: string, copyLabel = "Copy")}
  <div class="code">
    <pre>{text}</pre>
    <button type="button" class="code__copy" onclick={() => copy(label, text)}>{copied === label ? "Copied" : copyLabel}</button>
  </div>
{/snippet}

<div class="sheet">
  <div class="scopes" role="radiogroup" aria-label="What to connect">
    {#each SCOPES as sc (sc.id)}
      <button type="button" role="radio" class="choice" aria-checked={mode === sc.id} onclick={() => (mode = sc.id)}>
        <span class="choice__name">{sc.name}</span>
        <span class="choice__what">{sc.what}</span>
      </button>
    {/each}
  </div>

  {#if setup.isPending}
    <p class="hint">Reading the room's identity…</p>
  {:else if setup.isError || !setup.data}
    <p class="hint fail">Could not read the set-up details just now.</p>
  {:else}
    {@const s = setup.data}
    <p class="step__text">
      {#if mode === "parent"}
        Read-only in the projects you pick under the organization or folder: configuration and settings, never the data a service holds.
        Connect lists every project under it, the whole tree down, and you tick the ones agents may be granted.
      {:else}
        Read-only across the project: configuration and settings, never the data a service holds.
        Agents are granted services under Permissions and borrow an hour of access.
      {/if}
    </p>

    {#if s.identity}
      <dl class="facts">
        <div><dt>Grant</dt><dd><code>{s.identity.email}</code>{#if s.identity.subject}<span class="quiet"> (id {s.identity.subject})</span>{/if}</dd></div>
      </dl>
    {:else if s.identity_note}
      <p class="hint fail">{s.identity_note}</p>
    {/if}

    <div class="methods" role="radiogroup" aria-label="How you grant roles">
      {#each METHODS as m (m.id)}
        <button type="button" role="radio" class="choice" aria-checked={method === m.id} onclick={() => (method = m.id)}>
          <span class="choice__name">{m.id}</span>
          <span class="choice__what">{m.what}</span>
        </button>
      {/each}
    </div>

    <section class="block">
      <h3 class="step__title">{mode === "parent" ? "On the organization, once" : "On the project"}</h3>
      {#if method === "gcloud"}
        {@render code("gcloud", s.gcloud, "Copy commands")}
        <p class="hint">One binding per role{#if !s.target}; the id fills in as you type it below{/if}. Run them, then connect.</p>
      {:else if method === "Terraform"}
        {@render code("tf", s.terraform)}
        <p class="hint">Apply from the pipeline that owns IAM there, then connect.</p>
      {:else}
        <ul class="roles">
          {#each s.roles as role (role)}<li><code>{role}</code></li>{/each}
        </ul>
        <p class="hint">In the console, under IAM, add <code>{s.identity?.email ?? "the room's identity"}</code> as a principal with these roles.</p>
      {/if}
      <p class="packs">
        {#each s.packs.filter((p) => s.chosen.includes(p.id)) as p (p.id)}
          <span class="pack"><strong>{p.id}</strong> {p.purpose}</span>
        {/each}
      </p>
    </section>

    {#if s.agents.on}
      <section class="block">
        <h3 class="step__title">For the room, once: Google Cloud's logs name each agent</h3>
        {#if s.agents.group}
          <p class="step__text">
            Each new agent's account joins <code>{s.agents.group}</code>. Grant that group once on the room's identity, and
            Google Cloud's own logs record which agent a call was for. Not needed again for the next project or organization.
          </p>
        {:else}
          <p class="hint fail">
            This room has no agents group yet, so Google Cloud's logs do not name its agents. Make a Google group, let the
            room's own account add members to it, name it as <code>agents_group</code> in the room's settings, then grant it:
          </p>
        {/if}
        {@render code("agents", s.agents.gcloud, "Copy command")}
        <p class="hint">Until the grant is in place an agent's access works the same; the logs name only the room.</p>
      </section>
    {/if}

    {#if mode === "parent"}
      <label class="field">
        <span>Organization or folder</span>
        <input id="integration-parent_resource" type="text" bind:value={parentResource} autocomplete="off" spellcheck="false" placeholder="organizations/123456789012 or folders/123456789012" pattern="(folders|organizations)/[0-9]+" required />
      </label>
      <p class="hint">Connect saves it and opens the list of every project under it, the whole tree down, with the folder each sits in and whether the identity can read it. Tick the ones to connect; nothing connects by itself. Check projects opens the same list again later.</p>
    {:else}
      <label class="field">
        <span>Project id</span>
        <input id="integration-project" type="text" bind:value={project} autocomplete="off" spellcheck="false" placeholder="my-project-1234" pattern={"[a-z][a-z0-9-]{4,28}[a-z0-9]"} required />
      </label>
    {/if}

    <label class="check">
      <input id="integration-models" type="checkbox" bind:checked={useVertex} />
      <span>Include this project's Vertex AI models in the room</span>
    </label>
    {#if useVertex && mode === "project"}
      <label class="check check--inset">
        <input id="integration-models_only" type="checkbox" bind:checked={modelsOnly} />
        <span>Models only: this project serves the room's models and nothing else, so no agent is ever offered it</span>
      </label>
    {/if}
    {#if useVertex}
      <label class="field field--inset">
        <span>Vertex AI project</span>
        <input id="integration-models_project" type="text" bind:value={vertexProject} oninput={() => (vertexTyped = true)} autocomplete="off" spellcheck="false" placeholder="my-vertex-project" pattern={"[a-z][a-z0-9-]{4,28}[a-z0-9]"} required />
      </label>
      <label class="field field--inset">
        <span>Location</span>
        <input id="integration-models_location" type="text" bind:value={location} autocomplete="off" spellcheck="false" placeholder={s.vertex_default_location} pattern="global|[a-z]+-[a-z]+[0-9]+" />
      </label>
      <p class="hint hint--inset">
        {#if mode === "parent"}The project under the organization whose models the room offers.{:else}The project itself, unless another serves the models.{/if}
        Location: {s.vertex_default_location} unless you say otherwise, or a region such as us-east5. The models pack above grants the one role this needs.
      </p>
    {/if}
    <p class="hint">
      {#if s.checked}
        {#if mode === "parent"}
          Connect saves the organization{#if useVertex} after the permission service lists Vertex AI models in the project at the location{/if}; nothing is saved until Google Cloud agrees.
        {:else}
          Connect asks the permission service to read the project as its own identity{#if useVertex}, and to list Vertex AI models at the location{/if}; nothing is saved until Google Cloud agrees.
        {/if}
      {:else}
        Google Cloud connects through the permission service; turn Access on first.
      {/if}
    </p>
  {/if}
</div>

<style>
  .sheet { display: flex; flex-direction: column; gap: var(--space-md); }
  .block { display: flex; flex-direction: column; gap: var(--space-sm); }
  .scopes { display: grid; grid-template-columns: repeat(2, minmax(0, 1fr)); gap: var(--space-sm); }
  .methods { display: grid; grid-template-columns: repeat(3, minmax(0, 1fr)); gap: var(--space-sm); }
  .choice {
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
  .choice[aria-checked="true"] { border-color: var(--ink); box-shadow: inset 0 0 0 1px var(--ink); }
  .choice__name { font-weight: 600; }
  .choice__what { color: var(--ink); font-size: 0.85rem; }
  .quiet { color: var(--muted); }
  .step__title { margin: 0; font-size: 1rem; line-height: 1.5rem; color: var(--ink); }
  .step__text { margin: 0; color: var(--ink); line-height: 1.5; }
  .hint { margin: 0; color: var(--muted); font-size: 0.9rem; }
  .hint--inset { padding-left: var(--space-md); }
  .fail { color: var(--fail); }

  .facts { display: grid; grid-template-columns: max-content 1fr; gap: var(--space-2xs) var(--space-md); margin: 0; }
  .facts div { display: contents; }
  .facts dt { color: var(--muted); font-size: 0.9rem; }
  .facts dd { margin: 0; color: var(--ink); }
  .facts code { font-size: 0.9rem; }

  .roles {
    margin: 0;
    padding: var(--space-sm) var(--space-md);
    border: 1px solid var(--border);
    border-radius: var(--radius-sm);
    columns: 2;
    column-gap: var(--space-md);
    list-style: none;
    font-size: 0.85rem;
    line-height: 1.6;
  }
  .packs { display: flex; flex-direction: column; gap: var(--space-2xs); margin: 0; color: var(--ink); font-size: 0.85rem; }
  .pack strong { font-family: var(--font-mono); font-weight: 600; }

  .check { display: flex; align-items: center; gap: var(--space-sm); color: var(--ink); cursor: pointer; }
  .check input { margin: 0; }
  .check--inset { padding-left: var(--space-md); font-size: 0.9rem; }
  .field--inset { padding-left: var(--space-md); }

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
