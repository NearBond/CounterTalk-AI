/**
 * The API's types, written by hand to mirror the backend's schemas (CODING_GUIDELINES §4.4).
 * Field names are snake_case, exactly as the API sends them.
 */

/**
 * GET /api/health: countertalk/api/health.py, HealthOut. Sent with 200 when everything is ok,
 * and with 503 when the database can't be reached.
 */
export interface Health {
  status: "ok" | "error";
  database: "ok" | "error";
}

/** The framework's own error codes (countertalk/main.py). Domain errors bring their own. */
export type FrameworkErrorCode =
  | "not_found"
  | "method_not_allowed"
  | "invalid_request"
  | "internal_error";

/** The details of an invalid_request (422): where and why, never the values sent. */
export interface InvalidRequestDetails {
  errors: Array<{ loc: Array<string | number>; msg: string; type: string }>;
}

/** Every error response (§2.10). */
export interface ErrorBody {
  error: {
    code: FrameworkErrorCode | (string & {});
    message: string;
    details?: InvalidRequestDetails | Record<string, unknown>;
  };
}
