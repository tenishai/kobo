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
      <a class="btn btn--link-quiet back" href={formatHash({ page: "admin", pane: "roles" })}>← Roles</a>
      <h2 id="role-heading" class="title">{draft.id ? draft.name : "Create a role"}</h2>
      <p class="page__lead">Define what the groups in this role can and cannot do.</p>
    </div>

    <div class="tabs" role="tablist" aria-label="Role sections">
      {#each TABS as t (t)}
        <button type="button" role="tab" class="tab" aria-selected={tab === t} onclick={() => (tab = t)}>{t}</button>
      {/each}
    </div>

    {#if tab === "Details"}
      <div class="form">
        <label class="field">
          <span>Role name</span>
          <input id="role-name" type="text" bind:value={draft.name} placeholder="ex: Data engineering" disabled={draft.builtin} />
        </label>
        <label class="field">
          <span>Description</span>
          <input id="role-desc" type="text" bind:value={draft.description} placeholder="One line on who this is for" />
        </label>
        <div class="field">
          <span>Groups</span>
          <p class="panel__note">Members of these groups get this role.</p>
          {#if draft.groups.length === 0}
            <p class="panel__note">No groups assigned.</p>
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
              <option value="">Add group…</option>
              {#each GROUPS.filter((g) => !draft!.groups.includes(g)) as g (g)}
                <option value={g}>{g}</option>
              {/each}
            </select>
            <button type="button" class="btn" onclick={addGroup} disabled={!groupPick}>Add</button>
          </div>
        </div>
      </div>
    {:else if tab === "Capabilities"}
      <ul class="perm">
        {#each CAPABILITIES as c (c.id)}
          <li class="perm__row">
            <label class="perm__label">
              <input type="checkbox" id="cap-{c.id}" checked={draft.capabilities.includes(c.id)} onchange={() => toggle(draft!.capabilities, c.id)} />
              <span>{c.label}</span>
            </label>
            <span class="perm__desc">{c.desc}</span>
          </li>
        {/each}
      </ul>
    {:else if tab === "Admin permissions"}
      <ul class="perm">
        {#each ADMIN_PERMISSIONS as p (p.id)}
          <li class="perm__row">
            <label class="perm__label">
              <input type="checkbox" id="adm-{p.id}" checked={draft.admin.includes(p.id)} onchange={() => toggle(draft!.admin, p.id)} />
              <span>{p.label}</span>
            </label>
            <span class="perm__desc">{p.desc}</span>
          </li>
        {/each}
      </ul>
    {:else if tab === "Connectors"}
      <p class="page__lead">The ceiling per vendor: the most an agent in this role may be granted under Connections.</p>
      <div class="table-card"><table class="panel__table">
        <thead>
          <tr><th>Vendor</th>{#each CEILINGS as c (c.id)}<th>{c.label}</th>{/each}</tr>
        </thead>
        <tbody>
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
      <p class="page__lead">Models agents in this role may be built with or switched to.</p>
      <ul class="perm">
        {#each MODELS as m (m.id)}
          <li class="perm__row">
            <label class="perm__label">
              <input type="checkbox" id="model-{m.id}" checked={draft.models.includes(m.id)} onchange={() => toggle(draft!.models, m.id)} />
              <span>{m.label}</span>
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

  .back {
    align-self: flex-start;
    padding: 0;
    border: none;
    background: none;
    color: var(--muted);
    font-size: 0.85rem;
    text-decoration: none;
  }

  .title {
    margin: 0;
    font-size: 1.3rem;
  }

  .tabs {
    display: flex;
    gap: var(--space-xs);
    border-bottom: 1px solid var(--border);
  }

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

  .tab[aria-selected="true"] {
    border-bottom-color: var(--accent);
    color: var(--ink);
  }

  .form {
    display: flex;
    flex-direction: column;
    gap: var(--space-sm);
    max-width: 36rem;
  }

  .field input,
  .field select,
  .add-group select {
    padding: var(--space-2xs) var(--space-xs);
    border: 1px solid var(--border);
    border-radius: var(--radius-sm);
    background: var(--bg);
    color: var(--ink);
    font: inherit;
  }

  .add-group {
    display: flex;
    gap: var(--space-xs);
  }

  .perm {
    display: flex;
    flex-direction: column;
    gap: var(--space-2xs);
    margin: 0;
    padding: 0;
    list-style: none;
  }

  .perm__row {
    display: grid;
    grid-template-columns: minmax(12rem, 16rem) 1fr;
    gap: var(--space-sm);
    align-items: center;
    padding: var(--space-2xs) 0;
    border-bottom: 1px solid var(--border);
  }

  .perm__label {
    display: flex;
    align-items: center;
    gap: var(--space-xs);
    font-weight: 600;
  }

  .perm__desc {
    color: var(--muted);
    font-size: 0.85rem;
  }

  @media (max-width: 40rem) {
    .perm__row {
      grid-template-columns: 1fr;
    }
  }
</style>
