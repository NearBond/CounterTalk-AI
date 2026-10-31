/**
 * The only place that calls fetch (CODING_GUIDELINES §4.3). Every failure becomes an ApiError,
 * so a caller has one thing to catch and always gets a status, a code and a readable message.
 */
import type { ErrorBody, FrameworkErrorCode, Health } from "../types/api";

const BASE_URL = "/api";

/** No answer arrived, or it broke off: offline, the server is down. Status 0. */
export const NETWORK_ERROR = "network_error";
/** The caller cancelled the request through its signal. Status 0. */
export const ABORTED = "aborted";
/** An answer the app can't read: an HTML error page, an empty body, or JSON in another shape. */
export const UNEXPECTED_RESPONSE = "unexpected_response";

export type ApiErrorCode =
  | FrameworkErrorCode
  | typeof NETWORK_ERROR
  | typeof ABORTED
  | typeof UNEXPECTED_RESPONSE
  | (string & {});

export class ApiError extends Error {
  /** The HTTP status, or 0 when no answer arrived. */
  readonly status: number;
  readonly code: ApiErrorCode;
  readonly details: ErrorBody["error"]["details"];
  /** The parsed JSON body, whatever its shape, so nothing the server said is lost (§9.6). */
  readonly body: unknown;

  constructor(
    status: number,
    code: ApiErrorCode,
    message: string,
    details?: ErrorBody["error"]["details"],
    body?: unknown,
  ) {
    super(message);
    this.name = "ApiError";
    this.status = status;
    this.code = code;
    this.details = details;
    this.body = body;
  }
}

export interface RequestOptions {
  method?: "GET" | "POST" | "PUT" | "PATCH" | "DELETE";
  body?: unknown;
  /** Aborting it cancels the request; the caller gets an ApiError with code `aborted`. */
  signal?: AbortSignal;
}

export async function apiRequest<T>(path: string, options: RequestOptions = {}): Promise<T> {
  const headers = new Headers({ Accept: "application/json" });
  if (options.body !== undefined) headers.set("Content-Type", "application/json");

  let response: Response;
  let text: string;
  try {
    response = await fetch(`${BASE_URL}${path}`, {
      method: options.method ?? "GET",
      headers,
      body: options.body === undefined ? undefined : JSON.stringify(options.body),
      signal: options.signal,
    });
    // Inside the same try: a body that breaks off part-way is a network failure too.
    text = await response.text();
  } catch {
    if (options.signal?.aborted) throw new ApiError(0, ABORTED, "The request was cancelled.");
    throw new ApiError(0, NETWORK_ERROR, "Can't reach the server. Check the connection and try again.");
  }

  const body = parseJson(text);
  if (!response.ok) throw errorFrom(response.status, body);
  if (response.status === 204) return undefined as T;
  if (body === NOT_JSON) {
    throw new ApiError(
      response.status,
      UNEXPECTED_RESPONSE,
      `The server's answer (HTTP ${response.status}) wasn't JSON.`,
    );
  }
  return body as T;
}

export async function getHealth(signal?: AbortSignal): Promise<Health> {
  try {
    return await apiRequest<Health>("/health", { signal });
  } catch (error) {
    // The health check answers 503 with its usual body when the database is down: that's an
    // answer, not a failure to get one.
    if (error instanceof ApiError && error.status === 503 && isHealth(error.body)) return error.body;
    throw error;
  }
}

const NOT_JSON = Symbol("not JSON");

function parseJson(text: string): unknown {
  if (text === "") return NOT_JSON;
  try {
    return JSON.parse(text) as unknown;
  } catch {
    return NOT_JSON;
  }
}

/** Keeps whatever the error body gets right, and falls back field by field (§9.6). */
function errorFrom(status: number, body: unknown): ApiError {
  const fallback = `The server answered with an error (HTTP ${status}).`;
  const kept = body === NOT_JSON ? undefined : body;
  const error = isRecord(body) && isRecord(body.error) ? body.error : undefined;
  if (error === undefined) return new ApiError(status, UNEXPECTED_RESPONSE, fallback, undefined, kept);
  return new ApiError(
    status,
    typeof error.code === "string" && error.code !== "" ? error.code : UNEXPECTED_RESPONSE,
    typeof error.message === "string" && error.message !== "" ? error.message : fallback,
    isRecord(error.details) ? error.details : undefined,
    kept,
  );
}

function isHealth(value: unknown): value is Health {
  const states = ["ok", "error"];
  return isRecord(value) && states.includes(value.status as string) && states.includes(value.database as string);
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}
