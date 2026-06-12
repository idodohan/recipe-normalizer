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
