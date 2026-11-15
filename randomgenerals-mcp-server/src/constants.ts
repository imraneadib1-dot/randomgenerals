/**
 * Shared constants for the randomgenerals.com MCP server.
 */

/** Base URL for the OpenAI-compatible API surface, override for local dev
 *  (randomgenerals.com runs the same routes on http://127.0.0.1:5000/v1). */
export const DEFAULT_BASE_URL = "https://randomgenerals.com/v1";

/** randomgenerals.com's own tool-result cap (see tools.py/connectors.py in
 *  the backend) is 6000 chars; mirror that here so a very long completion
 *  doesn't blow past what a client expects to display. */
export const CHARACTER_LIMIT = 12000;

/** Network timeout for API calls. Non-streaming chat completions can take a
 *  while if the request fails over between providers (pip 1.0 -> OpenRouter ->
 *  Ollama), so this is generous rather than tight. */
export const REQUEST_TIMEOUT_MS = 60_000;
