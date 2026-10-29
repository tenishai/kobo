<script lang="ts">
  // What the room runs on, one row each, read from the org keys that turn
  // each part on. Nothing here is edited; the org config is. Two actions,
  // each behind a confirmation that says what it does: reloading that
  // config, and sweeping the switchboard's directory (issue #274), whose
  // confirm lists exactly what will be retired before anything is.
  import { createMutation, createQuery, useQueryClient } from "@tanstack/svelte-query";
  import Modal from "../Modal.svelte";
  import { errorMessage } from "../../lib/api";
  import { toast } from "../../lib/toast.svelte";
  import {
    adminAddonsQuery,
    directorySweepPreviewQuery,
    reloadAdminOrgMutation,
    sweepDirectoryMutation,
  } from "../../lib/queries";

  const queryClient = useQueryClient();
  const addons = createQuery(() => (adminAddonsQuery()));
  const reload = createMutation(() => (reloadAdminOrgMutation(queryClient)));
  let confirming = $state(false);

  let sweeping = $state(false);
  // The preview runs only while the sweep confirm is open: what it lists is
  // the switchboard's answer right now, and the Retire button is disabled
  // until that answer is in (and when there is nothing to retire).
  const preview = createQuery(() => ({ ...directorySweepPreviewQuery(), enabled: sweeping }));
  const sweep = createMutation(() => (sweepDirectoryMutation(queryClient)));
  const switchboardOn = $derived((addons.data ?? []).some((a) => a.id === "switchboard" && a.state === "on"));

  function handleSweep(): void {
    sweep.mutate(undefined, {
      onSuccess: (r) => {
        sweeping = false;
        const n = r.retired.length;
        toast.success(
          n ? `Retired ${n} director${n === 1 ? "y entry" : "y entries"}` : "Directory already clean",
          r.failed.length ? `The switchboard refused ${r.failed.join(", ")}.` : n ? r.retired.join(", ") : undefined,
        );
      },
      onError: (err: unknown) => toast.error("Could not sweep the directory", errorMessage(err, "Try again.")),
    });
  }

  const STATE: Record<string, { tone: "on" | "off" | "fail"; label: string }> = {
    on: { tone: "on", label: "On" },
    off: { tone: "off", label: "Off" },
    error: { tone: "fail", label: "Not answering" },
  };

  function handleReload(): void {
    confirming = false;
    reload.mutate(undefined, {
      onSuccess: (r) => toast.success(r.wired ? "Org config reloaded" : "Org config not wired", r.wired ? r.note : r.detail),
      onError: (err: unknown) => toast.error("Could not reload", errorMessage(err, "Try again.")),
    });
  }
</script>

<section class="panel page" aria-labelledby="services-heading">
  <div class="page__toolbar">
    <div>
      <h2 id="services-heading" class="page__title">Services</h2>
      <p class="page__lead">What this room runs on. Each service is turned on in the org config.</p>
    </div>
    <div class="page__actions">
      {#if switchboardOn}
        <button type="button" class="btn" onclick={() => (sweeping = true)} disabled={sweep.isPending}>
          Sweep directory
        </button>
      {/if}
      <button type="button" class="btn" onclick={() => (confirming = true)} disabled={reload.isPending}>
        Reload org config
      </button>
    </div>
  </div>

  {#if addons.isPending}
    <p class="page__foot">Reading what the room runs on…</p>
  {:else if addons.isError}
    <p class="page__foot">Could not read the services just now.</p>
  {:else}
    <div class="table-card">
      <table class="panel__table">
        <thead>
          <tr><th>Service</th><th>What it does</th><th>Status</th><th>Detail</th></tr>
        </thead>
        <tbody>
          {#each addons.data ?? [] as a (a.id)}
            <tr>
              <td><span class="name">{a.name}</span></td>
              <td class="quiet">{a.summary}</td>
              <td>
                <span class="status status--{STATE[a.state]?.tone ?? "off"}" title={a.state}>
                  <span class="status__dot" aria-hidden="true"></span>{STATE[a.state]?.label ?? a.state}
                </span>
              </td>
              <td class="quiet" class:fail={a.state === "error"}>{a.detail}</td>
            </tr>
          {/each}
        </tbody>
      </table>
    </div>
  {/if}
</section>

<Modal open={confirming} title="Reload the org config?" size="narrow" onclose={() => (confirming = false)}>
  <p class="page__lead">
    The room re-reads org.yaml from its config repository now: models, groups and the people lists
    change without a restart. Nothing restarts and no agent is touched. Service addresses need a
    restart to change and are left as they are.
  </p>
  {#snippet footer()}
    <button type="button" class="btn" onclick={() => (confirming = false)}>Cancel</button>
    <button type="button" class="btn btn--primary" onclick={handleReload}>Reload</button>
  {/snippet}
</Modal>

<Modal open={sweeping} title="Sweep the directory?" size="narrow" onclose={() => (sweeping = false)}>
  <p class="page__lead">
    The switchboard's directory is compared with this room's agents. Entries the room registered
    for agents it no longer has are retired. Entries registered by hand are left alone.
  </p>
  {#if preview.isPending}
    <p class="page__foot">Reading the directory…</p>
  {:else if preview.isError}
    <p class="page__foot fail">{errorMessage(preview.error, "Could not read the directory.")}</p>
  {:else if preview.data}
    {#if preview.data.retired.length}
      <p class="sweep__heading">Will be retired</p>
      <ul class="sweep__list">
        {#each preview.data.retired as name (name)}<li>{name}</li>{/each}
      </ul>
    {:else}
      <p class="page__foot">Nothing to retire. Every entry the room registered has an agent behind it.</p>
    {/if}
    <p class="page__foot">
      {preview.data.kept.length} kept{preview.data.unknown.length
        ? `, ${preview.data.unknown.length} not the room's (${preview.data.unknown.join(", ")})`
        : ""}.
    </p>
  {/if}
  {#snippet footer()}
    <button type="button" class="btn" onclick={() => (sweeping = false)}>Cancel</button>
    <button
      type="button"
      class="btn btn--primary"
      onclick={handleSweep}
      disabled={sweep.isPending || !preview.data || preview.data.retired.length === 0}
    >
      Retire {preview.data?.retired.length ?? ""}
    </button>
  {/snippet}
</Modal>

<style>
  .name { font-weight: 600; }
  .page__actions { display: flex; gap: var(--space-xs); }
  .sweep__heading { margin: var(--space-sm) 0 var(--space-2xs); font-weight: 600; }
  .sweep__list { margin: 0 0 var(--space-sm); padding-left: 1.25rem; }
  .quiet { color: var(--muted); font-size: 0.85rem; }
  .fail { color: var(--fail); }
</style>
