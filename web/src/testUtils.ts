/** Helpers for tests that fake the API. */

export function jsonResponse(status: number, body: unknown): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { "Content-Type": "application/json" },
  });
}

/** fetch rejects like the real one when the request's signal is aborted. */
export function rejectOnAbort(init: RequestInit | undefined, reject: (reason: unknown) => void) {
  init?.signal?.addEventListener("abort", () => reject(new DOMException("Aborted", "AbortError")));
}
