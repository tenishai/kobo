/// <reference types="vitest/config" />
import { defineConfig } from "vite";
import { svelte } from "@sveltejs/vite-plugin-svelte";
import { svelteTesting } from "@testing-library/svelte/vite";

// The admin-panel demo (2026-10-12) builds as a second page with relative
// asset paths, so `ADMIN_DEMO=1 npm run build` yields a dist/ that serves
// from any folder, no API behind it.
const adminDemo = process.env.ADMIN_DEMO === "1";

export default defineConfig({
  plugins: [svelte(), svelteTesting()],
  base: adminDemo ? "./" : "/",
  build: adminDemo
    ? { rollupOptions: { input: { "admin-demo": "admin-demo.html" } } }
    : undefined,
  server: {
    proxy: {
      "/api": "http://127.0.0.1:8100",
    },
  },
  test: {
    environment: "node",
    include: ["src/**/*.test.ts"],
    // A shared self-hosted runner is slower than a hosted one; a test that
    // renders sixty rows legitimately takes six seconds there.
    testTimeout: 20000,
  },
});
