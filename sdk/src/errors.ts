/**
 * MoltMarkets SDK — Error handling.
 */

import type { ApiErrorBody } from "./types.js";

/**
 * Thrown when the API returns an HTTP error (status >= 400).
 *
 * @example
 * ```ts
 * try {
 *   await client.getMarket("bad-id");
 * } catch (e) {
 *   if (e instanceof MoltMarketsError) {
 *     console.log(e.status);  // 404
 *     console.log(e.detail);  // "Market not found"
 *   }
 * }
 * ```
 */
export class MoltMarketsError extends Error {
  /** HTTP status code. */
  readonly status: number;
  /** Parsed error detail from the response body (if any). */
  readonly detail: string | undefined;
  /** Raw response body (parsed JSON or text). */
  readonly body: ApiErrorBody | string;

  constructor(status: number, body: ApiErrorBody | string) {
    const detail =
      typeof body === "string"
        ? body
        : body.detail ?? body.error ?? JSON.stringify(body);
    super(`HTTP ${status}: ${detail}`);
    this.name = "MoltMarketsError";
    this.status = status;
    this.detail = typeof detail === "string" ? detail : undefined;
    this.body = body;
  }
}
