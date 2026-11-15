/**
 * Shared HTTP client for talking to randomgenerals.com's OpenAI-compatible
 * API, plus error handling tailored to the specific error shapes that
 * backend returns (see _api_error()/_api_user() in app.py).
 */
import axios, { AxiosError, type AxiosInstance } from "axios";
import { DEFAULT_BASE_URL, REQUEST_TIMEOUT_MS } from "./constants.js";
import type { ApiErrorBody } from "./types.js";

let client: AxiosInstance | undefined;

/** Lazily build the axios client so a missing API key fails at call time
 *  with a clear message rather than crashing the process at import time. */
export function getClient(): AxiosInstance {
  if (client) return client;

  const apiKey = process.env.RANDOMGENERALS_API_KEY;
  if (!apiKey) {
    throw new Error(
      "RANDOMGENERALS_API_KEY is not set. Create a key at " +
        "https://randomgenerals.com under Settings, then set it as an " +
        "environment variable for this MCP server.",
    );
  }

  client = axios.create({
    baseURL: process.env.RANDOMGENERALS_BASE_URL || DEFAULT_BASE_URL,
    timeout: REQUEST_TIMEOUT_MS,
    headers: {
      Authorization: `Bearer ${apiKey}`,
      "Content-Type": "application/json",
      Accept: "application/json",
    },
  });
  return client;
}

/** Turn any error from an API call into one clear, actionable sentence.
 *  Mirrors the specific error codes randomgenerals.com's /v1 routes emit
 *  (invalid_request_error, insufficient_quota, permission_error,
 *  server_error) rather than a generic "request failed". */
export function describeApiError(error: unknown): string {
  if (axios.isAxiosError(error)) {
    const err = error as AxiosError<ApiErrorBody>;
    const body = err.response?.data;
    const message = body?.error?.message;
    const status = err.response?.status;

    if (status === 401) {
      return `Error: ${message || "Invalid API key."} Set RANDOMGENERALS_API_KEY to a key generated at https://randomgenerals.com under Settings.`;
    }
    if (status === 402) {
      return `Error: ${message || "Out of credits."} Credits refill automatically, or upgrade to Pro at https://randomgenerals.com.`;
    }
    if (status === 403) {
      return `Error: ${message || "That model needs a Pro plan."}`;
    }
    if (status === 503) {
      return `Error: ${message || "No model is available right now."} Try again shortly, or omit the model parameter to let it pick automatically.`;
    }
    if (message) {
      return `Error: ${message}`;
    }
    if (err.code === "ECONNABORTED") {
      return "Error: The request timed out waiting for a reply. Try again, or use a shorter conversation.";
    }
    if (err.code === "ECONNREFUSED" || err.code === "ENOTFOUND") {
      return `Error: Could not reach ${err.config?.baseURL || "randomgenerals.com"}. Check RANDOMGENERALS_BASE_URL if you set it.`;
    }
    return `Error: randomgenerals.com request failed${status ? ` (HTTP ${status})` : ""}.`;
  }
  return `Error: Unexpected error occurred: ${error instanceof Error ? error.message : String(error)}`;
}
