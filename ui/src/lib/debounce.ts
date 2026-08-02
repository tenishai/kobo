// Drives the availability check: as the user types a candidate name, callers
// wrap the "hit the API" callback in `debounced` so only the last keystroke
// in a quiet spell actually fires a request.

/** A debounced function, with a `cancel` handle for pending calls. */
export type Debounced<T> = ((value: T) => void) & { cancel: () => void };

/**
 * Wrap `fn` so that calling the returned function repeatedly only invokes
 * `fn` once, `ms` after the last call — with the value from that last call
 * (rapid calls collapse; latest value wins). `cancel()` drops any call
 * still waiting out its quiet period without invoking `fn`.
 */
export function debounced<T>(fn: (value: T) => void, ms = 350): Debounced<T> {
  let timer: ReturnType<typeof setTimeout> | undefined;

  const call = ((value: T) => {
    if (timer !== undefined) {
      clearTimeout(timer);
    }
    timer = setTimeout(() => {
      timer = undefined;
      fn(value);
    }, ms);
  }) as Debounced<T>;

  call.cancel = () => {
    if (timer !== undefined) {
      clearTimeout(timer);
      timer = undefined;
    }
  };

  return call;
}
