<script lang="ts">
  // The sidebar's account menu: the signed-in email as a button, and a small
  // list above it when opened. The guide is not here: it has its own entry
  // in the rail. Admin shows only when the room has the admin
  // panel on and the server says this caller is an admin; Log out shows only
  // when the deployment names a sign-out path. Both are the server's call
  // (`/api/whoami`), never derived here.
  import { CaretUpDown, Gear, SignOut } from "phosphor-svelte";
  import type { Whoami } from "../lib/api";
  import { formatHash } from "../lib/router.svelte";

  let { whoami, active }: { whoami: Whoami; active: string } = $props();

  let open = $state(false);
  const showAdmin = $derived(Boolean(whoami.operator && whoami.admin_panel));

  function close(): void {
    open = false;
  }

  function onKeydown(event: KeyboardEvent): void {
    if (event.key === "Escape") {
      close();
    }
  }
</script>

<svelte:window onkeydown={onKeydown} />

<div class="account">
  {#if open}
    <button type="button" class="account__scrim" aria-label="Close menu" onclick={close}></button>
    <div class="account__menu" role="menu" aria-label="Account">
      <p class="account__email" title={whoami.email ?? ""}>{whoami.email}</p>
      <ul class="account__list">
        {#if showAdmin}
          <li>
            <a
              role="menuitem"
              class="account__item"
              href={formatHash({ page: "admin", pane: "overview" })}
              aria-current={active === "admin" ? "page" : undefined}
              onclick={close}
            >
              <Gear class="account__glyph" size={16} aria-hidden="true" />
              Admin
              {#if whoami.pending_group_requests}
                <span class="account__badge">{whoami.pending_group_requests}</span>
              {/if}
            </a>
          </li>
        {/if}
        {#if whoami.logout_url}
          <li class="account__divider">
            <a role="menuitem" class="account__item" href={whoami.logout_url}>
              <SignOut class="account__glyph" size={16} aria-hidden="true" />
              Log out
            </a>
          </li>
        {/if}
      </ul>
    </div>
  {/if}
  <button
    type="button"
    class="account__trigger"
    aria-haspopup="menu"
    aria-expanded={open}
    aria-label="Account menu"
    title={whoami.email ?? ""}
    onclick={() => (open = !open)}
  >
    <span class="account__avatar" aria-hidden="true">{(whoami.email ?? "?").slice(0, 1).toUpperCase()}</span>
    <span class="account__trigger-email">{whoami.email}</span>
    <CaretUpDown class="account__chevron" size={14} aria-hidden="true" />
  </button>
</div>

<style>
  .account {
    position: relative;
    margin: auto 0 0 0;
    padding-top: var(--space-sm);
    border-top: 1px solid var(--border);
  }

  .account__trigger {
    display: flex;
    align-items: center;
    gap: var(--space-xs);
    width: 100%;
    padding: var(--space-xs);
    border: none;
    border-radius: var(--radius-sm);
    background: none;
    color: var(--muted);
    font: inherit;
    font-size: 0.8rem;
    text-align: left;
    cursor: pointer;
  }

  .account__trigger:hover,
  .account__trigger[aria-expanded="true"] {
    background: color-mix(in oklab, var(--rail-raised) 60%, transparent);
    color: var(--ink);
  }

  .account__avatar {
    display: inline-flex;
    align-items: center;
    justify-content: center;
    width: 1.75rem;
    height: 1.75rem;
    border-radius: 50%;
    background: var(--accent);
    color: var(--accent-ink);
    font-size: 0.7rem;
    font-weight: 600;
    flex-shrink: 0;
  }

  .account__trigger-email {
    flex: 1;
    min-width: 0;
    overflow: hidden;
    text-overflow: ellipsis;
    white-space: nowrap;
  }

  .account :global(.account__chevron) {
    flex-shrink: 0;
    opacity: 0.7;
  }

  .account__scrim {
    position: fixed;
    inset: 0;
    border: none;
    background: transparent;
    cursor: default;
  }

  .account__menu {
    position: absolute;
    bottom: calc(100% + var(--space-2xs));
    left: 0;
    right: 0;
    z-index: 2;
    padding: var(--space-xs) 0;
    border: 1px solid var(--border);
    border-radius: var(--radius-sm);
    background: var(--bg);
    box-shadow: 0 6px 24px rgba(0, 0, 0, 0.12);
  }

  .account__email {
    margin: 0;
    padding: var(--space-2xs) var(--space-sm) var(--space-xs);
    border-bottom: 1px solid var(--border);
    color: var(--muted);
    font-size: 0.75rem;
    overflow: hidden;
    text-overflow: ellipsis;
    white-space: nowrap;
  }

  .account__list {
    margin: 0;
    padding: var(--space-2xs) 0 0;
    list-style: none;
  }

  .account__item {
    display: flex;
    align-items: center;
    gap: var(--space-xs);
    padding: var(--space-2xs) var(--space-sm);
    color: var(--ink);
    font-size: 0.85rem;
    text-decoration: none;
  }

  .account__item:hover,
  .account__item[aria-current="page"] {
    background: var(--surface-2, rgba(127, 127, 127, 0.08));
  }

  .account :global(.account__glyph) {
    flex-shrink: 0;
    color: var(--muted);
  }

  .account__badge {
    margin-left: auto;
    padding: 0 0.4rem;
    border-radius: var(--radius-pill);
    background: var(--fail);
    color: var(--bg);
    font-size: 0.7rem;
    font-weight: 600;
  }

  .account__divider {
    margin-top: var(--space-2xs);
    padding-top: var(--space-2xs);
    border-top: 1px solid var(--border);
  }

  @media (max-width: 40rem) {
    .account__trigger-email,
    .account :global(.account__chevron) {
      display: none;
    }
  }
</style>
