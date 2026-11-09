<script lang="ts">
  // The projects under a Google Cloud folder or organization, picked rather than
  // connected wholesale (2026-11-09). The permission service lists the whole tree
  // and says which projects its identity can read; the admin searches, ticks, and
  // connects the ticked ones. A connected project is shown and cannot be ticked
  // again; one the identity cannot read yet is shown greyed with the reason, so
  // the admin knows where to grant. A hundred projects stay one scrollable list
  // with a search box, never a hundred rows on the Integrations page.
  import { createMutation, useQueryClient } from "@tanstack/svelte-query";
  import Modal from "../Modal.svelte";
  import { errorMessage, type GcpProject, type GcpProjectsCheck } from "../../lib/api";
  import { checkOrgAccountsMutation, connectGcpProjectsMutation } from "../../lib/queries";
  import { toast } from "../../lib/toast.svelte";

  let {
    integrationId,
    name,
    onclose,
  }: {
    /** The parent row (kind org) whose projects are listed. */
    integrationId: string;
    name: string;
    onclose: () => void;
  } = $props();

  const queryClient = useQueryClient();
  const list = createMutation(() => checkOrgAccountsMutation(queryClient));
  const connect = createMutation(() => connectGcpProjectsMutation(queryClient));

  let projects = $state<GcpProject[]>([]);
  let loadError = $state<string | null>(null);
  let search = $state("");
  let picked = $state<Set<string>>(new Set());

  function load(): void {
    loadError = null;
    list.mutate(integrationId, {
      onSuccess: (out) => {
        projects = (out as GcpProjectsCheck).accounts ?? [];
      },
      onError: (err: unknown) => {
        loadError = errorMessage(err, "Could not list the projects. Try again.");
      },
    });
  }
  $effect(() => {
    void integrationId;
    load();
  });

  const shown = $derived.by(() => {
    const q = search.trim().toLowerCase();
    if (!q) return projects;
    return projects.filter((p) =>
      p.id.toLowerCase().includes(q) || p.name.toLowerCase().includes(q) || p.folder_path.join(" / ").toLowerCase().includes(q));
  });
  const pickable = (p: GcpProject): boolean => !p.connected && p.reachable === true;
  /** The id, then the folders between the parent and the project, as one line. */
  const where = (p: GcpProject): string => (p.folder_path.length ? `${p.id} · ${p.folder_path.join(" / ")}` : p.id);
  const counts = $derived({
    connected: projects.filter((p) => p.connected).length,
    readable: projects.filter((p) => pickable(p)).length,
    waiting: projects.filter((p) => !p.connected && p.reachable !== true).length,
  });

  function toggle(id: string): void {
    const next = new Set(picked);
    if (next.has(id)) next.delete(id);
    else next.add(id);
    picked = next;
  }

  function submit(): void {
    const ids = [...picked];
    if (!ids.length) return;
    connect.mutate({ integrationId, projects: ids }, {
      onSuccess: (out) => {
        const n = out.connected.length;
        toast[n ? "success" : "error"](
          n ? `${n} project${n === 1 ? "" : "s"} connected` : "No project connected",
          out.refused.length ? out.refused.map((r) => `${r.name}: ${r.reason ?? "refused"}`).join("; ") : undefined,
        );
        picked = new Set();
        if (out.refused.length) load();
        else onclose();
      },
      onError: (err: unknown) => toast.error("Could not connect the projects", errorMessage(err, "Try again.")),
    });
  }

  function rowState(p: GcpProject): { tone: "on" | "warn" | "off"; label: string; title: string } {
    if (p.connected) return { tone: "on", label: "Connected", title: "Agents may be granted its read permissions." };
    if (p.reachable === true) return { tone: "off", label: "Can read", title: "The identity can read it; tick it to connect." };
    return { tone: "warn", label: "Not granted yet", title: p.reason ?? "The identity cannot read it yet; grant it the roles there." };
  }
</script>

<Modal open={true} title={`Projects under ${name}`} size="wide" {onclose}
  description="Every project under it, the whole tree down. Tick the ones agents may be granted; the rest stay out of the room.">
  {#if list.isPending && !projects.length}
    <p class="page__lead busy" role="status"><span class="spin" aria-hidden="true"></span> Listing the projects and checking which the identity can read…</p>
  {:else if loadError}
    <p class="page__lead fail">{loadError}</p>
  {:else}
    <div class="picker">
      <div class="picker__head">
        <input class="field__input page__search" type="search" placeholder="Search by id, name or folder" bind:value={search} aria-label="Search projects" />
        <span class="quiet counts">{counts.connected} connected · {counts.readable} can be picked · {counts.waiting} not granted yet</span>
      </div>
      {#if !shown.length}
        <p class="page__lead quiet">{projects.length ? "No project matches that." : "No project under it yet."}</p>
      {:else}
        <ul class="list" aria-label="Projects">
          {#each shown as p (p.id)}
            {@const st = rowState(p)}
            <li class="row" class:row--off={!pickable(p)}>
              <label class="row__pick">
                <input type="checkbox" checked={picked.has(p.id) || p.connected} disabled={!pickable(p)} onchange={() => toggle(p.id)} aria-label={`Connect ${p.name}`} />
                <span class="row__text">
                  <span class="row__name">{p.name}</span>
                  <span class="quiet row__id">{where(p)}</span>
                </span>
              </label>
              <span class="status status--{st.tone}" title={st.title}><span class="status__dot" aria-hidden="true"></span>{st.label}</span>
            </li>
          {/each}
        </ul>
      {/if}
    </div>
  {/if}
  {#snippet footer()}
    <button type="button" class="btn" onclick={onclose}>Close</button>
    <button type="button" class="btn btn--primary" disabled={!picked.size || connect.isPending} onclick={submit}>
      {#if connect.isPending}<span class="spin spin--btn" aria-hidden="true"></span>Connecting…{:else}Connect {picked.size ? `${picked.size} ` : ""}selected{/if}
    </button>
  {/snippet}
</Modal>

<style>
  .picker { display: flex; flex-direction: column; gap: var(--space-sm); }
  .picker__head { display: flex; flex-direction: column; gap: var(--space-2xs); }
  /* `.page__search` sizes itself with a flex-basis meant for a row; in this column it became the
     box's height (2026-11-09), so here it is one input-high and as wide as the dialog. */
  .picker__head .page__search { flex: none; width: 100%; min-width: 0; }
  .counts { font-size: 0.85rem; }
  .quiet { color: var(--muted); }
  .fail { color: var(--fail); }
  .busy { display: flex; align-items: center; gap: 0.5rem; }
  .spin { display: inline-block; width: 0.9em; height: 0.9em; border-radius: 50%; border: 2px solid currentColor; border-right-color: transparent; animation: spin 0.8s linear infinite; flex: none; }
  .spin--btn { margin-right: 0.4rem; }
  @keyframes spin { to { transform: rotate(360deg); } }
  .list { list-style: none; margin: 0; padding: 0; display: flex; flex-direction: column; border: 1px solid var(--border); border-radius: var(--radius-sm); max-height: 28rem; overflow: auto; }
  .row { display: flex; align-items: center; justify-content: space-between; gap: var(--space-md); padding: var(--space-xs) var(--space-md); border-bottom: 1px solid var(--border); }
  .row:last-child { border-bottom: 0; }
  .row--off .row__name { color: var(--muted); }
  .row__pick { display: flex; align-items: center; gap: var(--space-sm); cursor: pointer; min-width: 0; }
  .row__pick input { margin: 0; flex: none; }
  .row__text { display: flex; flex-direction: column; min-width: 0; }
  .row__name { color: var(--ink); }
  .row__id { font-size: 0.85rem; font-family: var(--font-mono); overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
  .status { display: inline-flex; align-items: center; gap: 0.35rem; white-space: nowrap; font-size: 0.85rem; }
  .status--on { color: var(--ok, var(--ink)); }
  .status--warn { color: var(--warn, var(--ink)); }
  .status--off { color: var(--muted); }
  .status__dot { width: 0.5rem; height: 0.5rem; border-radius: 50%; background: currentColor; }
</style>
