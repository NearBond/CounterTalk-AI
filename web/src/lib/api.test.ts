import { describe, expect, it, vi } from "vitest";
import { jsonResponse as json, rejectOnAbort } from "../testUtils";
import {
  ABORTED,
  ApiError,
  NETWORK_ERROR,
  UNEXPECTED_RESPONSE,
  apiRequest,
  getHealth,
  putSegment,
} from "./api";

type FetchArgs = [input: string | URL | Request, init?: RequestInit];

function stubFetch(answer: (...args: FetchArgs) => Promise<Response>) {
  const fake = vi.fn(answer);
  vi.stubGlobal("fetch", fake);
  return fake;
}

async function failure(call: Promise<unknown>): Promise<ApiError> {
  try {
    await call;
  } catch (error) {
    if (error instanceof ApiError) return error;
    throw error;
  }
  throw new Error("expected the call to fail");
}

describe("apiRequest", () => {
  it("returns the JSON of a successful response", async () => {
    const fake = stubFetch(async () => json(200, { status: "ok", database: "ok" }));

    await expect(getHealth()).resolves.toEqual({ status: "ok", database: "ok" });
    const [url, init] = fake.mock.calls[0] ?? [];
    expect(url).toBe("/api/health");
    expect(init?.method).toBe("GET");
    expect(new Headers(init?.headers).get("Accept")).toBe("application/json");
  });

  it("sends a body as JSON", async () => {
    const fake = stubFetch(async () => json(200, { id: "a" }));

    await apiRequest("/things", { method: "POST", body: { name: "A" } });

    const [, init] = fake.mock.calls[0] ?? [];
    expect(init?.body).toBe('{"name":"A"}');
    expect(new Headers(init?.headers).get("Content-Type")).toBe("application/json");
  });

  it.each([
    [404, "not_found", "Not Found"],
    [405, "method_not_allowed", "Method Not Allowed"],
    [409, "visit_not_recording", "The visit isn't recording"],
    [500, "internal_error", "Something went wrong on the server"],
  ])("turns a %i in the error shape into an ApiError with its code", async (status, code, message) => {
    stubFetch(async () => json(status, { error: { code, message } }));

    const error = await failure(getHealth());

    expect(error).toBeInstanceOf(Error);
    expect(error).toMatchObject({ status, code, message, details: undefined });
  });

  it("keeps the details of an error", async () => {
    const details = { errors: [{ loc: ["path", "item_id"], msg: "Not an integer", type: "int_parsing" }] };
    stubFetch(async () =>
      json(422, { error: { code: "invalid_request", message: "Wrong shape", details } }),
    );

    const error = await failure(getHealth());

    expect(error).toMatchObject({ status: 422, code: "invalid_request", details });
  });

  it("turns an error page that isn't JSON into an ApiError", async () => {
    stubFetch(async () => new Response("<html>Bad Gateway</html>", { status: 502 }));

    const error = await failure(getHealth());

    expect(error).toMatchObject({ status: 502, code: UNEXPECTED_RESPONSE });
    expect(error.message).toContain("502");
  });

  it("turns an empty error body into an ApiError", async () => {
    stubFetch(async () => new Response(null, { status: 503 }));

    expect(await failure(getHealth())).toMatchObject({ status: 503, code: UNEXPECTED_RESPONSE });
  });

  it("turns a JSON error body without the error shape into an ApiError", async () => {
    stubFetch(async () => json(503, { status: "error", database: "error" }));

    expect(await failure(apiRequest("/health"))).toMatchObject({
      status: 503,
      code: UNEXPECTED_RESPONSE,
    });
  });

  it("keeps the code of an error body that has no message", async () => {
    stubFetch(async () => json(409, { error: { code: "visit_not_recording" } }));

    const error = await failure(getHealth());

    expect(error.code).toBe("visit_not_recording");
    expect(error.message).toContain("409");
  });

  it("keeps the message of an error body whose code isn't a string", async () => {
    stubFetch(async () => json(409, { error: { code: 42, message: "Readable message" } }));

    expect(await failure(getHealth())).toMatchObject({
      code: UNEXPECTED_RESPONSE,
      message: "Readable message",
    });
  });

  it("ignores details that aren't an object, and fields it doesn't know", async () => {
    stubFetch(async () =>
      json(409, { error: { code: "c", message: "m", details: "text", extra: 1 }, other: true }),
    );

    expect(await failure(getHealth())).toMatchObject({ code: "c", message: "m", details: undefined });
  });

  it("turns a network failure into an ApiError with status 0", async () => {
    stubFetch(async () => {
      throw new TypeError("Failed to fetch");
    });

    expect(await failure(getHealth())).toMatchObject({ status: 0, code: NETWORK_ERROR });
  });

  it("refuses a successful response that isn't JSON", async () => {
    stubFetch(async () => new Response("<!doctype html><title>app</title>", { status: 200 }));

    expect(await failure(getHealth())).toMatchObject({ status: 200, code: UNEXPECTED_RESPONSE });
  });

  it("resolves with nothing for a 204 No Content", async () => {
    stubFetch(async () => new Response(null, { status: 204 }));

    await expect(apiRequest("/things/a", { method: "DELETE" })).resolves.toBeUndefined();
  });

  it("keeps the body of an error response that isn't in the error shape", async () => {
    stubFetch(async () => json(503, { status: "error", database: "error" }));

    expect((await failure(apiRequest("/health"))).body).toEqual({ status: "error", database: "error" });
  });

  it("turns a body that breaks off part-way into a network error", async () => {
    const broken = new ReadableStream({
      start(controller) {
        controller.error(new TypeError("network connection lost"));
      },
    });
    stubFetch(async () => new Response(broken, { status: 200 }));

    expect(await failure(getHealth())).toMatchObject({ status: 0, code: NETWORK_ERROR });
  });

  it("passes the signal on, and a cancelled request becomes an aborted ApiError", async () => {
    const fake = stubFetch((_url, init) => new Promise<Response>((_resolve, reject) => rejectOnAbort(init, reject)));
    const controller = new AbortController();

    const call = failure(getHealth(controller.signal));
    controller.abort();

    expect(fake.mock.calls[0]?.[1]?.signal).toBe(controller.signal);
    expect(await call).toMatchObject({ status: 0, code: ABORTED });
  });
});

describe("putSegment", () => {
  it("sends the segment's raw bytes with their SHA-256", async () => {
    const segment = { seq: 3, bytes: 5, sha256: "a".repeat(64), received_at: "2026-11-01T06:00:00Z" };
    const fake = stubFetch(async () => json(201, segment));
    const body = new Blob([new TextEncoder().encode("bytes")]);

    await expect(putSegment("v-1", 3, body, "a".repeat(64))).resolves.toEqual(segment);

    const [url, init] = fake.mock.calls[0] ?? [];
    expect(url).toBe("/api/visits/v-1/segments/3");
    expect(init?.method).toBe("PUT");
    expect(init?.body).toBe(body);
    const headers = new Headers(init?.headers);
    expect(headers.get("Content-Type")).toBe("application/octet-stream");
    expect(headers.get("X-Content-SHA256")).toBe("a".repeat(64));
  });
});

describe("getHealth", () => {
  it("returns the health body of a 503: the API is up and the database isn't", async () => {
    stubFetch(async () => json(503, { status: "error", database: "error" }));

    await expect(getHealth()).resolves.toEqual({ status: "error", database: "error" });
  });

  it("still fails on a 503 that isn't a health body", async () => {
    stubFetch(async () => new Response("<html>Service Unavailable</html>", { status: 503 }));

    expect(await failure(getHealth())).toMatchObject({ status: 503, code: UNEXPECTED_RESPONSE });
  });
});
