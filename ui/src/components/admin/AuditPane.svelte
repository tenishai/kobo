<script lang="ts">
  // Who changed what from the panel, and whether it has landed. Every
  // change is a pull request or an admin-plane write, so this is the git
  // log read back, with the few applied-now rows beside it.
  import { admin } from "../../lib/admin/store.svelte";
</script>

<section class="panel" aria-labelledby="audit-heading">
  <div class="panel__header">
    <h2 id="audit-heading" class="panel__heading">Audit</h2>
    <p class="panel__lead">Who changed what, and whether it has landed yet.</p>
  </div>
  <table class="panel__table">
    <thead><tr><th>When</th><th>Who</th><th>Change</th><th>Landed</th></tr></thead>
    <tbody>
      {#each admin.audit as a (a.id)}
        <tr>
          <td class="when">{a.when}</td>
          <td>{a.who}</td>
          <td>{a.change}</td>
          <td class="landed landed--{a.state}">{a.landed}</td>
        </tr>
      {/each}
    </tbody>
  </table>
</section>

<style>
  .when {
    color: var(--muted);
    white-space: nowrap;
  }

  .landed {
    white-space: nowrap;
  }

  .landed--merged,
  .landed--applied {
    color: var(--ok);
  }

  .landed--open {
    color: var(--warn);
  }
</style>
