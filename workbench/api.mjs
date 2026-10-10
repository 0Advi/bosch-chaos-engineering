export class WorkbenchAPI {
  constructor(fetcher = globalThis.fetch.bind(globalThis)) {
    this.fetcher = fetcher;
  }
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
