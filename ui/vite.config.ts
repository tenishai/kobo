/// <reference types="vitest/config" />
import { defineConfig } from "vite";
import { svelte } from "@sveltejs/vite-plugin-svelte";
import { svelteTesting } from "@testing-library/svelte/vite";

export default defineConfig({
  plugins: [svelte(), svelteTesting()],
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
