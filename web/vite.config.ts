import tailwindcss from "@tailwindcss/vite";
import react from "@vitejs/plugin-react";
import { defineConfig } from "vitest/config";

export default defineConfig({
  plugins: [react(), tailwindcss()],
  server: {
    // 127.0.0.1, not localhost, both ways. On Windows, localhost tries ::1 first: a proxy aimed
    // at it waited about 5 s per connection, and a dev server on it wasn't reachable at 127.0.0.1.
    host: "127.0.0.1",
    // API_PORT: the browser checks (e2e/run.mjs) run their own API on another port.
    proxy: { "/api": `http://127.0.0.1:${process.env.API_PORT ?? "8000"}` },
  },
  test: {
    environment: "jsdom",
    setupFiles: ["./src/setupTests.ts"],
    restoreMocks: true,
    unstubGlobals: true,
  },
});
