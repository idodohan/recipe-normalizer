/** Extract a human message from the backend error envelope:
 *  `{"error": {"code": "...", "message": "...", ...}}`. */
export function apiErrorMessage(
  body: unknown,
  fallback = "Something went wrong. Please try again.",
): string {
  if (body && typeof body === "object" && "error" in body) {
    const err = (body as { error: unknown }).error;
    if (err && typeof err === "object" && "message" in err) {
      const message = (err as { message: unknown }).message;
      if (typeof message === "string" && message.length > 0) return message;
    }
  }
  return fallback;
}

/** Parsed shape of the backend error envelope's `error` object. */
export type ApiErrorEnvelope = {
  code?: string;
  message?: string;
  existingId?: string;
  jobId?: string;
};

/** Pull the error envelope (code + message + extras) out of an openapi-fetch error. */
export function apiErrorEnvelope(body: unknown): ApiErrorEnvelope {
  const err =
    body && typeof body === "object" && "error" in body
      ? (body as { error: Record<string, unknown> }).error
      : undefined;
  if (!err || typeof err !== "object") return {};
  const code = typeof err["code"] === "string" ? err["code"] : undefined;
  const message = typeof err["message"] === "string" ? err["message"] : undefined;
  const existingId =
    typeof err["existing_id"] === "string" ? err["existing_id"] : undefined;
  const jobId = typeof err["job_id"] === "string" ? err["job_id"] : undefined;
  return { code, message, existingId, jobId };
}
