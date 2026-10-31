/// <reference types="vitest/config" />
import { fileURLToPath, URL } from "node:url";
import { defineConfig } from "vite";
import tailwindcss from "@tailwindcss/vite";
import { svelte } from "@sveltejs/vite-plugin-svelte";
import { svelteTesting } from "@testing-library/svelte/vite";

export default defineConfig({
  plugins: [tailwindcss(), svelte(), svelteTesting()],
  resolve: {
    alias: {
      $lib: fileURLToPath(new URL("./src/lib", import.meta.url)),
    },
  },
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
