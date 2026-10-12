/** Client for the /api/workbench endpoints with a 10 s timeout and the ok/result envelope. */
export class WorkbenchAPI {
  /** Use the given fetch function (the browser's by default; tests pass a fake). */
  constructor(fetcher = globalThis.fetch.bind(globalThis)) {
    this.fetcher = fetcher;
  }
  /**
   * GET (no body) or POST JSON to a workbench path, with an optional idempotency key; returns the
   * result or throws.
   */
  async call(path, body, key) {
    const abort = new AbortController();
    const timer = setTimeout(() => abort.abort(), 10000);
    try {
      const fetcher = this.fetcher;
      const response = await fetcher("/api/workbench" + path, {
        method: body === undefined ? "GET" : "POST",
        signal: abort.signal,
        credentials: "same-origin",
        headers: {
          "Content-Type": "application/json",
          ...(key ? { "Idempotency-Key": key } : {}),
        },
        ...(body === undefined ? {} : { body: JSON.stringify(body) }),
      });
      const envelope = await response.json();
      if (!response.ok || envelope.ok !== true)
        throw Error(envelope.message || "Controller request failed.");
      return envelope.result;
    } finally {
      clearTimeout(timer);
    }
  }
}
