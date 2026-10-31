<script lang="ts">
  // Roles: a named set of permissions a group holds. Four built-ins and any
  // number of custom roles from the same lists, all from the server; the
  // editor has the five tabs an org-settings screen has. Saving writes the
  // row and its audit event (#173 step 3).
  import { createMutation, createQuery, useQueryClient } from "@tanstack/svelte-query";
  import { toast } from "../../lib/toast.svelte";
  import { errorMessage, type AdminRole } from "../../lib/api";
  import { formatHash, navigate } from "../../lib/router.svelte";
  import { adminRolesQuery, deleteAdminRoleMutation, putAdminRoleMutation } from "../../lib/queries";

  let { item = null }: { item?: string | null } = $props();

  const TABS = ["Details", "Capabilities", "Admin permissions", "Connectors", "Models"] as const;
  type Tab = (typeof TABS)[number];
  type Ceiling = "none" | "read" | "write" | "approve";
  type Role = AdminRole;
  const CEILINGS: { id: Ceiling; label: string; desc: string }[] = [
    { id: "none", label: "None", desc: "Agents in this role cannot be granted this vendor." },
    { id: "read", label: "Read", desc: "Read lanes only." },
    { id: "write", label: "Write", desc: "Read and write lanes." },
    { id: "approve", label: "Approve", desc: "Write lanes, and this role approves requests for it." },
  ];

  const queryClient = useQueryClient();
  const rolesQuery = createQuery(() => (adminRolesQuery()));
  const save = createMutation(() => (putAdminRoleMutation(queryClient)));
  const remove = createMutation(() => (deleteAdminRoleMutation(queryClient)));

  const roles = $derived(rolesQuery.data?.roles ?? []);
  const CAPABILITIES = $derived(rolesQuery.data?.catalogue.capabilities ?? []);
  const ADMIN_PERMISSIONS = $derived(rolesQuery.data?.catalogue.admin ?? []);
  const MODELS = $derived(rolesQuery.data?.catalogue.models ?? []);
  const GROUPS = $derived(rolesQuery.data?.catalogue.groups ?? []);
  const vendorIds = $derived((rolesQuery.data?.catalogue.connectors ?? []).map((c) => c.id));
  function pluginName(id: string): string {
    return rolesQuery.data?.catalogue.connectors.find((c) => c.id === id)?.name ?? id;
  }
  function findRole(id: string): Role | undefined {
    return roles.find((r) => r.id === id);
  }
  function blankRole(): Role {
    return {
      id: "",
      name: "",
      description: "",
      builtin: false,
      groups: [],
      capabilities: ["chat_teach"],
      admin: [],
      connectors: vendorIds.map((plugin) => ({ plugin, ceiling: "none" as Ceiling })),
      models: MODELS.map((m) => m.id),
      updated_at: "",
    };
  }

  let draft = $state<Role | null>(null);
  let tab = $state<Tab>("Details");
  let groupPick = $state("");

  // `#/admin/roles/new` opens a blank editor; `#/admin/roles/<id>` edits one.
  $effect(() => {
    if (rolesQuery.isPending) return;
    if (item === "new") {
      if (!draft || draft.id !== "") {
        draft = blankRole();
        tab = "Details";
      }
    } else if (item && findRole(item)) {
      if (draft?.id !== item) {
        draft = structuredClone($state.snapshot(findRole(item)!));
        tab = "Details";
      }
    } else {
      draft = null;
    }
  });

  function toggle(list: string[], id: string): void {
    const i = list.indexOf(id);
    if (i >= 0) list.splice(i, 1);
    else list.push(id);
  }

  function addGroup(): void {
    if (!draft || !groupPick || draft.groups.includes(groupPick)) return;
    draft.groups.push(groupPick);
    groupPick = "";
  }

  function slug(text: string): string {
    return text.trim().toLowerCase().replace(/[^a-z0-9]+/g, "-").replace(/^-|-$/g, "");
  }

  function handleSave(): void {
    if (!draft || draft.name.trim() === "") return;
    const id = draft.id || slug(draft.name);
    const { name, description, groups, capabilities, admin, connectors, models } = $state.snapshot(draft);
    save.mutate(
      { id, body: { name, description, groups, capabilities, admin, connectors, models } },
      {
        onSuccess: (saved) => {
          toast.success(`Role ${saved.name} saved`);
          navigate({ page: "admin", pane: "roles" });
        },
        onError: (err: unknown) => toast.error("Could not save the role", errorMessage(err, "Try again.")),
      },
    );
  }

  function handleDelete(r: Role): void {
    remove.mutate(r.id, {
      onSuccess: () => {
        toast.success(`Role ${r.name} deleted`);
        navigate({ page: "admin", pane: "roles" });
      },
      onError: (err: unknown) => toast.error("Could not delete the role", errorMessage(err, "Try again.")),
    });
  }

  function ceilingOf(r: Role, pluginId: string): Ceiling {
    return r.connectors.find((c) => c.plugin === pluginId)?.ceiling ?? "none";
  }

  function setCeiling(r: Role, pluginId: string, ceiling: Ceiling): void {
    const c = r.connectors.find((x) => x.plugin === pluginId);
    if (c) c.ceiling = ceiling;
    else r.connectors.push({ plugin: pluginId, ceiling });
  }
</script>

{#if draft}
  <section class="panel page" aria-labelledby="role-heading">
    <div class="page__header">
      <nav class="crumbs" aria-label="Breadcrumb">
        <a class="back" href={formatHash({ page: "admin", pane: "roles" })}>Roles</a>
        <span aria-hidden="true">›</span>
        <span aria-current="page">{draft.id ? draft.name : "New role"}</span>
      </nav>
      <h2 id="role-heading" class="title">{draft.id ? draft.name : "Create a role"}</h2>
      <p class="page__lead">Define what the groups in this role can and cannot do.</p>
    </div>

    <div class="tabs" role="tablist" aria-label="Role sections">
      {#each TABS as t (t)}
        <button type="button" role="tab" class="tab" aria-selected={tab === t} onclick={() => (tab = t)}>{t}</button>
      {/each}
    </div>

    {#if tab === "Details"}
      <!-- Settings rows: what each field is on the left, the field on the
           right. -->
      <div class="rows">
        <div class="row">
          <div class="row__head">
            <label class="row__title" for="role-name">Role name</label>
            <p class="row__help">How the role shows up across the room.</p>
          </div>
          <input id="role-name" type="text" bind:value={draft.name} placeholder="Data engineering" disabled={draft.builtin} />
        </div>
        <div class="row">
          <div class="row__head">
            <label class="row__title" for="role-desc">Description</label>
            <p class="row__help">One line on who this role is for.</p>
          </div>
          <input id="role-desc" type="text" bind:value={draft.description} placeholder="Engineers who build data pipelines" />
        </div>
        <div class="row">
          <div class="row__head">
            <label class="row__title" for="role-group">Groups</label>
            <p class="row__help">Members of these groups get this role.</p>
          </div>
          <div class="groups">
            {#if draft.groups.length === 0}
              <p class="groups__empty">No groups assigned yet.</p>
            {:else}
              <div class="chip-row">
                {#each draft.groups as g (g)}
                  <span class="chip">{g}
                    <button type="button" class="chip__remove" aria-label="Remove {g}" onclick={() => toggle(draft!.groups, g)}>×</button>
                  </span>
                {/each}
              </div>
            {/if}
            <div class="add-group">
              <select id="role-group" bind:value={groupPick}>
                <option value="">Choose a group</option>
                {#each GROUPS.filter((g) => !draft!.groups.includes(g)) as g (g)}
                  <option value={g}>{g}</option>
                {/each}
              </select>
              <button type="button" class="btn" onclick={addGroup} disabled={!groupPick}>Add group</button>
            </div>
          </div>
        </div>
      </div>
    {:else if tab === "Capabilities"}
      <p class="tab-lead">What members of this role can do with agents.</p>
      <ul class="perm">
        {#each CAPABILITIES as c (c.id)}
          <li>
            <label class="perm__row" class:perm__row--on={draft.capabilities.includes(c.id)}>
              <input type="checkbox" id="cap-{c.id}" checked={draft.capabilities.includes(c.id)} onchange={() => toggle(draft!.capabilities, c.id)} />
              <span class="perm__text">
                <span class="perm__title">{c.label}</span>
                <span class="perm__desc">{c.desc}</span>
              </span>
            </label>
          </li>
        {/each}
      </ul>
    {:else if tab === "Admin permissions"}
      <p class="tab-lead">Which parts of the admin panel members of this role can change.</p>
      <ul class="perm">
        {#each ADMIN_PERMISSIONS as p (p.id)}
          <li>
            <label class="perm__row" class:perm__row--on={draft.admin.includes(p.id)}>
              <input type="checkbox" id="adm-{p.id}" checked={draft.admin.includes(p.id)} onchange={() => toggle(draft!.admin, p.id)} />
              <span class="perm__text">
                <span class="perm__title">{p.label}</span>
                <span class="perm__desc">{p.desc}</span>
              </span>
            </label>
          </li>
        {/each}
      </ul>
    {:else if tab === "Connectors"}
      <p class="tab-lead">The ceiling per vendor: the most an agent in this role may be granted under Connections.</p>
      <div class="table-card"><table class="panel__table">
        <thead>
          <tr><th>Vendor</th>{#each CEILINGS as c (c.id)}<th>{c.label}</th>{/each}</tr>
        </thead>
        <tbody>
          {#if vendorIds.length === 0}
            <tr><td class="panel__empty" colspan={CEILINGS.length + 1}>No vendors connected yet. Add one under Integrations, then set its ceiling here.</td></tr>
          {/if}
          {#each vendorIds as v (v)}
            <tr>
              <td>{pluginName(v)}</td>
              {#each CEILINGS as c (c.id)}
                <td>
                  <input type="radio" id="ceil-{v}-{c.id}" name="ceil-{v}" value={c.id} checked={ceilingOf(draft, v) === c.id}
                    onchange={() => setCeiling(draft!, v, c.id)} aria-label="{pluginName(v)}: {c.label}" title={c.desc} />
                </td>
              {/each}
            </tr>
          {/each}
        </tbody>
      </table></div>
    {:else}
      <p class="tab-lead">Models agents in this role may be built with or switched to.</p>
      <ul class="perm">
        {#each MODELS as m (m.id)}
          <li>
            <label class="perm__row" class:perm__row--on={draft.models.includes(m.id)}>
              <input type="checkbox" id="model-{m.id}" checked={draft.models.includes(m.id)} onchange={() => toggle(draft!.models, m.id)} />
              <span class="perm__text">
                <span class="perm__title">{m.label}</span>
              </span>
            </label>
          </li>
        {/each}
      </ul>
    {/if}

    <div class="panel__footer">
      <p class="panel__status">Saved to the room; the audit log records it.</p>
      <div class="panel__actions">
        {#if draft.id && !draft.builtin}
          <button type="button" class="btn btn--danger" disabled={remove.isPending} onclick={() => handleDelete(draft!)}>Delete</button>
        {/if}
        <a class="btn" href={formatHash({ page: "admin", pane: "roles" })}>Cancel</a>
        <button type="button" class="btn btn--primary" onclick={handleSave} disabled={draft.name.trim() === "" || save.isPending}>Save</button>
      </div>
    </div>
  </section>
{:else}
  <section class="panel page" aria-labelledby="roles-heading">
    <div class="panel__toolbar">
      <div class="page__header">
        <h2 id="roles-heading" class="page__title">Roles</h2>
        <p class="page__lead">What a group may do. Assign a role to groups; their members and agents get its permissions.</p>
      </div>
      <a class="btn btn--primary" href={formatHash({ page: "admin", pane: "roles", item: "new" })}>Add role</a>
    </div>
    {#if rolesQuery.isPending}
      <p class="panel__note">Reading the roles…</p>
    {:else if rolesQuery.isError}
      <p class="panel__note">Could not read the roles just now.</p>
    {:else}
    <div class="table-card"><table class="panel__table">
      <thead>
        <tr><th>Name</th><th>Groups</th><th>Admin access</th><th>Vendors</th><th class="panel__cell--right"></th></tr>
      </thead>
      <tbody>
        {#each roles as r (r.id)}
          <tr>
            <td>
              <span class="role-name">{r.name}</span>
              {#if r.builtin}<span class="chip chip--muted">built in</span>{/if}
            </td>
            <td>
              {#each r.groups.slice(0, 2) as g (g)}<span class="chip">{g}</span>{/each}
              {#if r.groups.length > 2}<span class="chip chip--muted">+{r.groups.length - 2}</span>{/if}
              {#if r.groups.length === 0}<span class="quiet">—</span>{/if}
            </td>
            <td class="quiet">{r.admin.length === 0 ? "—" : r.admin.length === ADMIN_PERMISSIONS.length ? "All" : `${r.admin.length} of ${ADMIN_PERMISSIONS.length}`}</td>
            <td class="quiet">{r.connectors.filter((c) => c.ceiling !== "none").map((c) => `${pluginName(c.plugin)} · ${c.ceiling}`).join(", ") || "—"}</td>
            <td class="panel__cell--right">
              <a class="btn btn--quiet" href={formatHash({ page: "admin", pane: "roles", item: r.id })}>Edit ›</a>
            </td>
          </tr>
        {/each}
      </tbody>
    </table></div>
    {/if}
  </section>
{/if}

<style>
  .role-name { margin-right: var(--space-xs); font-weight: 600; }
  .quiet { color: var(--muted); font-size: 0.85rem; }

  .crumbs {
    display: flex;
    align-items: center;
    gap: var(--space-xs);
    margin-bottom: var(--space-xs);
    color: var(--muted);
    font-family: var(--font-display-mono);
    font-size: 0.75rem;
  }

  .crumbs [aria-current="page"] {
    color: var(--ink);
  }

  .back {
    color: var(--muted);
    text-decoration: none;
  }

  .back:hover {
    color: var(--ink);
    text-decoration: underline;
  }

  .title {
    margin: 0;
    font-size: 1.35rem;
    letter-spacing: -0.01em;
  }

  /* Underline tabs, the same as the agent page's. */
  .tabs {
    display: flex;
    gap: var(--space-lg);
    margin-top: var(--space-sm);
    border-bottom: 1px solid var(--border);
  }

  .tab {
    position: relative;
    height: 2.75rem;
    padding: 0 var(--space-2xs);
    border: none;
    background: none;
    color: var(--muted);
    font: inherit;
    font-size: 0.875rem;
    font-weight: 500;
    cursor: pointer;
  }

  .tab:hover {
    color: var(--ink);
  }

  .tab::after {
    content: "";
    position: absolute;
    right: 0;
    bottom: -1px;
    left: 0;
    height: 2px;
    background: transparent;
  }

  .tab[aria-selected="true"] {
    color: var(--ink);
  }

  .tab[aria-selected="true"]::after {
    background: var(--accent);
  }

  .rows {
    display: flex;
    flex-direction: column;
  }

  .row {
    display: grid;
    grid-template-columns: minmax(0, 15rem) minmax(0, 32rem);
    gap: var(--space-xl);
    align-items: start;
    padding: var(--space-lg) 0;
    border-bottom: 1px solid var(--border);
  }

  .row:last-child {
    border-bottom: none;
  }

  .row__head {
    display: flex;
    flex-direction: column;
    gap: var(--space-2xs);
  }

  .row__title {
    color: var(--ink);
    font-size: 0.9rem;
    font-weight: 600;
  }

  .row__help {
    margin: 0;
    color: var(--muted);
    font-size: 0.82rem;
    line-height: 1.5;
  }

  .row input {
    width: 100%;
  }

  .groups {
    display: flex;
    flex-direction: column;
    gap: var(--space-sm);
  }

  .groups__empty {
    margin: 0;
    color: var(--muted);
    font-size: 0.85rem;
  }

  .add-group {
    display: flex;
    gap: var(--space-xs);
  }

  .add-group select {
    flex: 1;
    min-width: 0;
  }

  @media (max-width: 52rem) {
    .row {
      grid-template-columns: minmax(0, 1fr);
      gap: var(--space-sm);
    }
  }

  .tab-lead {
    margin: var(--space-lg) 0 var(--space-md);
    color: var(--muted);
    font-size: 0.875rem;
  }

  /* A permission list: one bordered card, a row per permission, the whole
     row clickable, checked rows tinted. */
  .perm {
    display: flex;
    flex-direction: column;
    margin: 0;
    padding: 0;
    overflow: hidden;
    border: 1px solid var(--border);
    border-radius: var(--radius-sm);
    list-style: none;
  }

  .perm li + li {
    border-top: 1px solid var(--border);
  }

  .perm__row {
    display: flex;
    align-items: flex-start;
    gap: var(--space-md);
    padding: var(--space-md) var(--space-lg);
    cursor: pointer;
    transition: background 0.15s ease;
  }

  .perm__row:hover {
    background: color-mix(in oklab, var(--accent-soft) 60%, var(--panel));
  }

  .perm__row--on {
    background: var(--accent-soft);
  }

  .perm__row input {
    flex-shrink: 0;
    width: 1rem;
    height: 1rem;
    margin: 0.15rem 0 0;
  }

  .perm__text {
    display: flex;
    min-width: 0;
    flex-direction: column;
    gap: 0.15rem;
  }

  .perm__title {
    color: var(--ink);
    font-size: 0.9rem;
    font-weight: 600;
  }

  .perm__desc {
    color: var(--muted);
    font-size: 0.82rem;
    line-height: 1.5;
  }

  /* The footer under its own rule, with room above it. */
  .page :global(.panel__footer) {
    margin-top: var(--space-lg);
    padding-top: var(--space-lg);
    border-top: 1px solid var(--border);
  }
</style>
