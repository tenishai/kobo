<script lang="ts">
  // Focus-trapped dialog shell. Fully controlled by the caller (`open` +
  // `onclose`) — this component owns none of the "should it be open" state,
  // only the accessibility mechanics of *being* a dialog:
  //   - role="dialog" / aria-modal so assistive tech treats it as one
  //   - focus moves into the dialog on open, and back to whatever had focus
  //     beforehand when it closes
  //   - Escape and a backdrop click both call `onclose`
  //   - Tab/Shift+Tab cycle within the dialog's focusable elements instead
  //     of escaping to the page behind it
  //   - background scroll is locked while open
  import { tick, type Snippet } from "svelte";

  let {
    open,
    title,
    onclose,
    children,
  }: {
    open: boolean;
    title?: string;
    onclose: () => void;
    children: Snippet;
  } = $props();

  let dialogEl: HTMLDivElement | undefined = $state();
  let opener: HTMLElement | null = null;

  const FOCUSABLE_SELECTOR = [
    "a[href]",
    "button:not([disabled])",
    "textarea:not([disabled])",
    "input:not([disabled])",
    "select:not([disabled])",
    '[tabindex]:not([tabindex="-1"])',
  ].join(", ");

  function focusables(): HTMLElement[] {
    if (!dialogEl) {
      return [];
    }
    return Array.from(dialogEl.querySelectorAll<HTMLElement>(FOCUSABLE_SELECTOR));
  }

  function handleKeydown(event: KeyboardEvent): void {
    if (event.key === "Escape") {
      event.preventDefault();
      onclose();
      return;
    }
    if (event.key !== "Tab") {
      return;
    }

    const focusable = focusables();
    if (focusable.length === 0) {
      event.preventDefault();
      return;
    }

    const first = focusable[0];
    const last = focusable[focusable.length - 1];
    const active = document.activeElement;

    if (event.shiftKey && active === first) {
      event.preventDefault();
      last.focus();
    } else if (!event.shiftKey && active === last) {
      event.preventDefault();
      first.focus();
    }
  }

  function handleBackdropMousedown(event: MouseEvent): void {
    if (event.target === event.currentTarget) {
      onclose();
    }
  }

  $effect(() => {
    if (!open) {
      return;
    }

    opener = document.activeElement as HTMLElement | null;
    const previousOverflow = document.body.style.overflow;
    document.body.style.overflow = "hidden";

    // Wait for the {#if open} block below to mount before looking for
    // something inside it to focus.
    tick().then(() => {
      const [first] = focusables();
      (first ?? dialogEl)?.focus();
    });

    return () => {
      document.body.style.overflow = previousOverflow;
      opener?.focus();
      opener = null;
    };
  });
</script>

{#if open}
  <!-- Backdrop click-to-close is a convenience on top of Escape and the
       close button below, both of which are fully keyboard-accessible, so
       the backdrop itself doesn't need its own keyboard interaction. -->
  <!-- svelte-ignore a11y_no_static_element_interactions -->
  <div class="modal-backdrop" onmousedown={handleBackdropMousedown}>
    <div
      class="modal"
      role="dialog"
      aria-modal="true"
      aria-labelledby={title ? "modal-title" : undefined}
      tabindex="-1"
      bind:this={dialogEl}
      onkeydown={handleKeydown}
    >
      <button
        type="button"
        class="modal__close"
        aria-label="Close"
        onclick={onclose}
      >
        &times;
      </button>
      {#if title}
        <h2 id="modal-title" class="modal__title">{title}</h2>
      {/if}
      <div class="modal__content">
        {@render children()}
      </div>
    </div>
  </div>
{/if}

<style>
  .modal-backdrop {
    position: fixed;
    inset: 0;
    display: flex;
    align-items: center;
    justify-content: center;
    padding: var(--space-lg);
    background: rgba(0, 0, 0, 0.4);
    z-index: 100;
  }

  .modal {
    position: relative;
    width: 100%;
    max-width: 32rem;
    max-height: 85vh;
    overflow-y: auto;
    padding: var(--space-lg);
    border: 1px solid var(--border);
    border-radius: var(--radius);
    background: var(--panel);
    box-shadow: 0 12px 40px rgba(0, 0, 0, 0.25);
  }

  .modal__close {
    position: absolute;
    top: var(--space-sm);
    right: var(--space-sm);
    padding: var(--space-2xs) var(--space-xs);
    border: none;
    background: none;
    color: var(--muted);
    font-size: 1.25rem;
    line-height: 1;
    cursor: pointer;
  }

  .modal__close:hover {
    color: var(--ink);
  }

  .modal__title {
    margin: 0 0 var(--space-md);
    padding-right: var(--space-xl);
    color: var(--ink);
  }
</style>
