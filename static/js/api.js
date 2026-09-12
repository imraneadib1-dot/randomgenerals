/* ----------------------------------------------------------------
   One way to call the server.

   Sixty-odd fetch() calls, each with its own idea of what a failure
   looks like: some checked res.ok, some read .error, some had a catch,
   and some had none - so a failed thread creation was a rejected
   promise nobody caught, and the message simply never sent, with one
   red line in the console.

   request() turns every failure into an ApiError carrying the status
   and the body, and explain() turns that into a sentence for a person.
   A 429 says how long to wait; a 401 says sign in; a network failure
   says the server could not be reached; anything else uses the
   server's own `error` field, which every route here sets.
   ---------------------------------------------------------------- */

export class ApiError extends Error {
  /**
   * @param {number} status 0 when the request never got an answer
   * @param {any} body the parsed JSON body, or {}
   */
  constructor(status, body, message) {
    super(message);
    this.name = "ApiError";
    this.status = status;
    this.body = body || {};
  }
}

/**
 * @param {string} method
 * @param {string} url
 * @param {any} [body] JSON-encoded when given
 * @param {{signal?: AbortSignal}} [opts]
 * @returns {Promise<any>} the parsed JSON body
 */
export async function request(method, url, body, opts = {}) {
  const init = { method, headers: {}, signal: opts.signal };
  if (body !== undefined) {
    init.headers["Content-Type"] = "application/json";
    init.body = JSON.stringify(body);
  }
  let res;
  try {
    res = await fetch(url, init);
  } catch (e) {
    if (e && e.name === "AbortError") throw e;
    throw new ApiError(0, null, "Could not reach the server.");
  }
  const data = await res.json().catch(() => ({}));
  if (!res.ok) {
    throw new ApiError(res.status, data,
      (data && data.error) || `The server answered ${res.status}.`);
  }
  return data;
}

export const getJSON = (url, opts) => request("GET", url, undefined, opts);
export const postJSON = (url, body, opts) => request("POST", url, body ?? {}, opts);
export const patchJSON = (url, body, opts) => request("PATCH", url, body, opts);
export const del = (url, opts) => request("DELETE", url, undefined, opts);

/** A sentence for a person, from any error request() can throw. */
export function explain(err, fallback = "Something went wrong.") {
  if (!(err instanceof ApiError)) return (err && err.message) || fallback;
  if (err.status === 0) return "Could not reach the server.";
  if (err.status === 401) return "Sign in first.";
  if (err.status === 429) {
    const s = Number(err.body.retry_after) || 0;
    return s ? `Too many requests - try again in about ${s}s.` : "Too many requests - try again in a moment.";
  }
  return err.message || fallback;
}
