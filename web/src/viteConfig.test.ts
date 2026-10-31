// @vitest-environment node
import { describe, expect, it } from "vitest";
import config from "../vite.config";

// Why 127.0.0.1 and not localhost: see vite.config.ts.
describe("the dev server", () => {
  it("listens on 127.0.0.1", () => {
    expect(config.server?.host).toBe("127.0.0.1");
  });

  it("sends /api to the backend on 127.0.0.1:8000", () => {
    expect(config.server?.proxy?.["/api"]).toBe("http://127.0.0.1:8000");
  });
});
