// Component-test harness: mounts a component inside a QueryClientProvider
// (panels and DangerZone all call createQuery/createMutation, which throw
// without one) and stubs fetch so mounting never hits the network.
import { QueryClient } from "@tanstack/svelte-query";
import { render } from "@testing-library/svelte";
import { vi } from "vitest";
import Harness from "./Harness.svelte";

// Not auto-reset between tests: callers own `vi.unstubAllGlobals()` in their own `beforeEach`.
export function stubFetch(routes: Record<string, unknown>): void {
  vi.stubGlobal(
    "fetch",
    vi.fn(async (input: RequestInfo | URL) => {
      const url = String(input);
      for (const [needle, payload] of Object.entries(routes)) {
        if (url.includes(needle)) {
          return new Response(JSON.stringify(payload), {
            status: 200,
            headers: { "Content-Type": "application/json" },
          });
        }
      }
      throw new Error(`unstubbed fetch: ${url}`);
    }),
  );
}

export function renderWithClient(
  component: unknown,
  props: Record<string, unknown> = {},
) {
  const client = new QueryClient({
    defaultOptions: { queries: { retry: false } },
  });
  return render(Harness, { props: { component, componentProps: props, client } });
}
