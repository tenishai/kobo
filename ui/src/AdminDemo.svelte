<script lang="ts">
  // The admin panel inside the room's shell, standalone. The Sidebar's own
  // queries fail quietly with no API behind them; the demo only needs the
  // rail to look like the room.
  import { QueryClient, QueryClientProvider } from "@tanstack/svelte-query";
  import Sidebar from "./components/Sidebar.svelte";
  import Toasts from "./components/Toasts.svelte";
  import AdminPanel from "./pages/AdminPanel.svelte";
  import { navigate, route } from "./lib/router.svelte";

  const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } });

  // Any hash that is not the admin panel lands on its Overview.
  $effect(() => {
    if (route.page !== "admin") {
      navigate({ page: "admin", pane: "overview" });
    }
  });
</script>

<QueryClientProvider client={queryClient}>
  <div class="shell">
    <Sidebar active="admin" showAdmin onnewagent={() => {}} onprefetch={() => {}} />
    <main class="shell__main">
      <p class="demo-banner">Demo: every change here lives in this page only. Reload to reset.</p>
      <AdminPanel />
    </main>
  </div>
</QueryClientProvider>
<Toasts />

<style>
  .shell {
    display: flex;
    flex-direction: row;
    gap: var(--space-xl);
    min-height: 100vh;
  }

  .shell__main {
    flex: 1;
    padding: var(--space-xl);
    min-width: 0;
  }

  .demo-banner {
    margin: 0 0 var(--space-md);
    padding: var(--space-2xs) var(--space-sm);
    border: 1px dashed var(--border);
    border-radius: var(--radius-sm);
    color: var(--muted);
    font-size: 0.8rem;
  }
</style>
