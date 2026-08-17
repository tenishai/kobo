<script lang="ts">
  import { QueryClientProvider, type QueryClient } from "@tanstack/svelte-query";
  let { component, componentProps, client }: {
    component: any;
    componentProps: Record<string, unknown>;
    client: QueryClient;
  } = $props();
  // component is fixed for this harness instance's lifetime (a fresh Harness
  // is rendered per test), same reasoning DangerZone.svelte documents for its
  // own svelte-ignore.
  // svelte-ignore state_referenced_locally
  const Component = component;
</script>

<QueryClientProvider {client}>
  <Component {...componentProps} />
</QueryClientProvider>
