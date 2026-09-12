export function friendlyMessage(message: string): string {
  const normalized = message.toLowerCase().trim();
  
  if (normalized.includes("extraction failed")) {
    return "We couldn't extract a recipe from this source.";
  }
  if (normalized.includes("download failed")) {
    return "We couldn't download the content from this link.";
  }
  if (normalized.includes("invalid url") || normalized.includes("unsupported_url")) {
    return "This link doesn't look right or isn't supported.";
  }
  if (normalized.includes("timeout")) {
    return "The extraction took too long and timed out.";
  }
  if (normalized.includes("rate limit")) {
    return "We're receiving too many requests. Please try again later.";
  }
  if (normalized.includes("payload too large")) {
    return "The file or text is too large to process.";
  }
  if (normalized.includes("not found")) {
    return "We couldn't find what you were looking for.";
  }
  
  return message;
}

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
      if (typeof message === "string" && message.length > 0) return friendlyMessage(message);
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
  /** `extra.scale_endpoint` on a transform's 422 `use_scale_feature` — see
   *  `ai.service.transform_recipe`'s scaling boundary. */
  scaleEndpoint?: string;
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
  const scaleEndpoint =
    typeof err["scale_endpoint"] === "string" ? err["scale_endpoint"] : undefined;
  return { code, message, existingId, jobId, scaleEndpoint };
}
