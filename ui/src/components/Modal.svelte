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
  //
  // One shape for every dialog in this room (modal idiom wave): a header row
  // (title in the page's h2 style, close button on the right), a body that
  // scrolls on its own, and a footer pinned under it whose actions are
  // right-aligned, so the button that finishes the job is on screen without
  // scrolling for it. Three widths and nothing in between -- `narrow` for a
  // confirm, the default for a form, `wide` for a picker. The classes live in
  // app.css, next to the panel idiom, so a dialog and a panel read from one
  // place rather than each carrying its own copy.
  import { tick, type Snippet } from "svelte";

  let {
    open,
    title,
    size = "default",
    onclose,
    children,
    footer,
  }: {
    open: boolean;
    title?: string;
    /** 28rem for a confirm, 36rem for a form, 44rem for a picker. */
    size?: "narrow" | "default" | "wide";
    onclose: () => void;
    children: Snippet;
    /** The pinned action row. Left out entirely by a dialog whose actions
     * belong to a form nested inside the body (see PersonaBuilder, which
     * draws its own sticky footer while it is swapped in). */
    footer?: Snippet;
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

  // A dialog can open over another (the destroy confirm over Edit details,
  // issue #368). The key is handled once, by the dialog that has it, so an
  // Escape in the confirm does not also close the editor behind it.
  function handleKeydown(event: KeyboardEvent): void {
    if (event.key === "Escape") {
      event.preventDefault();
      event.stopPropagation();
      onclose();
      return;
    }
    if (event.key !== "Tab") {
      return;
    }
    event.stopPropagation();

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
      class="modal modal--{size}"
      role="dialog"
      aria-modal="true"
      aria-labelledby={title ? "modal-title" : undefined}
      tabindex="-1"
      bind:this={dialogEl}
      onkeydown={handleKeydown}
    >
      <div class="modal__header">
        {#if title}
          <h2 id="modal-title" class="modal__title">{title}</h2>
        {/if}
        <button
          type="button"
          class="modal__close"
          aria-label="Close"
          onclick={onclose}
        >
          &times;
        </button>
      </div>
      <div class="modal__body">
        {@render children()}
      </div>
      {#if footer}
        <div class="modal__footer">
          {@render footer()}
        </div>
      {/if}
    </div>
  </div>
{/if}
