<script lang="ts">
  // What the room runs on, one row each, read from the org keys that turn
  // each part on. Nothing here is edited; the org config is. Reloading that
  // config is the one action, behind a confirmation that says what it does.
  import { createMutation, createQuery, useQueryClient } from "@tanstack/svelte-query";
  import Modal from "../Modal.svelte";
  import { errorMessage } from "../../lib/api";
  import { toast } from "../../lib/toast.svelte";
  import { adminAddonsQuery, reloadAdminOrgMutation } from "../../lib/queries";

  const queryClient = useQueryClient();
  const addons = createQuery(() => (adminAddonsQuery()));
  const reload = createMutation(() => (reloadAdminOrgMutation(queryClient)));
  let confirming = $state(false);

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

<section class="panel page" aria-labelledby="platform-heading">
  <div class="page__toolbar">
    <div>
      <h2 id="platform-heading" class="page__title">Platform</h2>
      <p class="page__lead">The services this room runs on. Each is turned on in the org config.</p>
    </div>
    <button type="button" class="btn" onclick={() => (confirming = true)} disabled={reload.isPending}>
      Reload org config
    </button>
  </div>

  {#if addons.isPending}
    <p class="page__foot">Reading what the room runs on…</p>
  {:else if addons.isError}
    <p class="page__foot">Could not read the platform just now.</p>
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

<style>
  .name { font-weight: 600; }
  .quiet { color: var(--muted); font-size: 0.85rem; }
  .fail { color: var(--fail); }
</style>
