// Global toast queue. `toast.success`/`toast.error` push a notification onto
// `toasts`; each one auto-dismisses after 5s, or can be dismissed early via
// `dismiss(id)` (wired up by Toasts.svelte's dismiss button). Presentation
// lives entirely in Toasts.svelte — this module only owns the queue.

export type ToastKind = "success" | "error";

export type Toast = {
  id: number;
  kind: ToastKind;
  title: string;
  body?: string;
};

const AUTO_DISMISS_MS = 5000;

let nextId = 1;

/** The live toast queue, oldest first. Render it; don't mutate it directly. */
export const toasts: Toast[] = $state([]);

/**
 * Pending auto-dismiss timer handles, by toast id. Tracked so a manual
 * dismiss (the toast's own dismiss button) can cancel the toast's timer
 * instead of leaving it to fire later into an id that's already gone —
 * harmless today since `dismiss` is idempotent, but it's a dangling handle
 * for however long the timer keeps waiting, and it'd stop being harmless the
 * moment an id were ever reused or dismiss grew a side effect.
 */
const timers = new Map<number, ReturnType<typeof setTimeout>>();

/** Remove a toast by id. Safe to call for an id that's already gone. */
export function dismiss(id: number): void {
  const index = toasts.findIndex((item) => item.id === id);
  if (index !== -1) {
    toasts.splice(index, 1);
  }
  const handle = timers.get(id);
  if (handle !== undefined) {
    clearTimeout(handle);
    timers.delete(id);
  }
}

function push(kind: ToastKind, title: string, body?: string): void {
  const id = nextId++;
  toasts.push({ id, kind, title, body });
  timers.set(
    id,
    setTimeout(() => dismiss(id), AUTO_DISMISS_MS),
  );
}

export const toast = {
  success(title: string, body?: string): void {
    push("success", title, body);
  },
  error(title: string, body?: string): void {
    push("error", title, body);
  },
};
