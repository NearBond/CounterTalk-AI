import tailwindcss from "@tailwindcss/vite";
import react from "@vitejs/plugin-react";
import { defineConfig } from "vitest/config";

export default defineConfig({
  plugins: [react(), tailwindcss()],
  server: {
    // 127.0.0.1, not localhost, both ways. On Windows, localhost tries ::1 first: a proxy aimed
    // at it waited about 5 s per connection, and a dev server on it wasn't reachable at 127.0.0.1.
    host: "127.0.0.1",
    proxy: { "/api": "http://127.0.0.1:8000" },
  },
  test: {
    environment: "jsdom",
    setupFiles: ["./src/setupTests.ts"],
    restoreMocks: true,
    unstubGlobals: true,
  },
});
