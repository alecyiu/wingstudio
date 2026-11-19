/* Wing Studio - HTTP and event-stream access.
   The mock mode (?mock=1) swaps AS.transport for an in-page server; nothing
   else in the UI knows the difference. */
(function () {
  'use strict';
  const AS = window.AS;

  AS.transport = {
    fetch: (url, options) => window.fetch(url, options),
    EventSource: window.EventSource,
  };

  async function request(method, path, body) {
    let response;
    try {
      response = await AS.transport.fetch(path, {
        method,
        headers: body !== undefined ? { 'Content-Type': 'application/json' } : {},
        body: body !== undefined ? JSON.stringify(body) : undefined,
      });
    } catch (e) {
      throw new Error('The studio server is not reachable');
    }
    const text = await response.text();
    let data = null;
    try { data = text ? JSON.parse(text) : null; } catch (e) { /* non-JSON error page */ }
    if (!response.ok) {
      const detail = data && (data.error || data.detail);
      const error = new Error(typeof detail === 'string' ? detail : `${method} ${path} failed (${response.status})`);
      error.status = response.status;
      throw error;
    }
    return data;
  }

  AS.api = {
    get: (path) => request('GET', path),
    post: (path, body) => request('POST', path, body === undefined ? {} : body),
    patch: (path, body) => request('PATCH', path, body),
  };

  /* One EventSource per open study. Reconnects by itself and always asks the
     server to replay from the last sequence number it has seen, so no event
     is lost or delivered twice. */
  AS.EventStream = class {
    constructor(studyId, after, onEvent, onState) {
      this.studyId = studyId;
      this.lastSeq = after || 0;
      this.onEvent = onEvent;
      this.onState = onState;
      this.closed = false;
      this.retries = 0;
      this.source = null;
      this.timer = null;
      this.open();
    }
    open() {
      if (this.closed) return;
      this.onState(this.retries ? 'reconnecting' : 'connecting');
      const source = new AS.transport.EventSource(`/api/studies/${this.studyId}/events?after=${this.lastSeq}`);
      this.source = source;
      source.onopen = () => { this.retries = 0; this.onState('live'); };
      source.onmessage = (message) => {
        let event;
        try { event = JSON.parse(message.data); } catch (e) { return; }
        if (!event || typeof event.seq !== 'number' || event.seq <= this.lastSeq) return;   // duplicate after a reconnect
        this.lastSeq = event.seq;
        this.onEvent(event);
      };
      source.onerror = () => {
        // Close and reopen ourselves: the browser's own retry would reuse the
        // original URL and replay from the wrong sequence number.
        source.close();
        if (this.closed) return;
        this.retries += 1;
        this.onState('reconnecting');
        clearTimeout(this.timer);
        this.timer = setTimeout(() => this.open(), Math.min(5000, 500 * this.retries));
      };
    }
    close() {
      this.closed = true;
      clearTimeout(this.timer);
      if (this.source) this.source.close();
      this.onState('idle');
    }
  };
})();
