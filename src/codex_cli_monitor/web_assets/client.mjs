// REST establishes backend identity; only that instance may update the page.
export const POLL_MS = 5000;
export const STALE_MS = 15000;
const REQUEST_MS = 8000;
const SOCKET_MAX_MS = 60000;
export const STATUSES = { "运行中": "running", "待确认": "waiting", "成功": "success", "失败": "failure" };

function validSnapshot(value) {
  return value && typeof value.instance_id === "string" && value.instance_id.length > 0
    && Number.isFinite(value.observed_at) && Array.isArray(value.servers)
    && value.servers.every(server => server && typeof server.id === "string")
    && Array.isArray(value.sessions) && value.sessions.every(session => session
      && typeof session.session_key === "string" && typeof session.server_id === "string"
      && Object.hasOwn(STATUSES, session.status));
}

export class MonitorClient {
  constructor({ token, origin, onSnapshot, onState, env = globalThis }) {
    this.token = token;
    this.origin = origin;
    this.onSnapshot = onSnapshot;
    this.onState = onState;
    this.env = env;
    this.stopped = true;
    this.socket = null;
    this.request = null;
    this.instance = null;
    this.observed = -Infinity;
    this.lastRest = null;
    this.expired = true;
    this.restHealthy = false;
    this.nextPoll = 0;
    this.nextSocket = 0;
    this.retryDelay = 1000;
  }

  now() { return this.env.performance.now(); }

  state(value) {
    if (this.currentState !== value) {
      this.currentState = value;
      this.onState(value);
    }
  }

  fresh() { return this.lastRest !== null && this.now() - this.lastRest < STALE_MS; }

  start() {
    if (!this.stopped) return;
    if (!this.token) { this.onSnapshot(null); this.state("unauthorized"); return; }
    this.stopped = false;
    this.state("connecting");
    this.tick();
  }

  stop() {
    this.stopped = true;
    this.env.clearTimeout(this.timer);
    this.request?.abort();
    this.request = null;
    this.closeSocket();
  }

  unauthorized() {
    this.stop();
    this.onSnapshot(null);
    this.state("unauthorized");
  }

  expire() {
    if (this.lastRest !== null && !this.fresh() && !this.expired) {
      this.expired = true;
      this.closeSocket();
      this.onSnapshot(null);
      this.state("stale");
    }
  }

  tick() {
    if (this.stopped) return;
    this.expire();
    const now = this.now();
    if (now >= this.nextPoll) void this.refresh();
    if (this.socket && (now - this.socketStarted >= SOCKET_MAX_MS
      || (!this.socketReady && now - this.socketStarted >= REQUEST_MS))) {
      this.closeSocket();
    }
    if (!this.socket && this.fresh() && now >= this.nextSocket) this.connect();
    this.timer = this.env.setTimeout(() => this.tick(), 1000);
  }

  async refresh() {
    if (this.stopped || this.request) return;
    this.expire();
    const controller = new this.env.AbortController();
    this.request = controller;
    this.nextPoll = this.now() + POLL_MS;
    const timeout = this.env.setTimeout(() => controller.abort(), REQUEST_MS);
    try {
      const response = await this.env.fetch(new URL("/api/sessions", this.origin), {
        headers: { Authorization: `Bearer ${this.token}` },
        cache: "no-store", credentials: "omit", redirect: "error",
        referrerPolicy: "no-referrer", signal: controller.signal,
      });
      if (this.stopped || this.request !== controller) return;
      if (response.status === 401 || response.status === 403) { this.unauthorized(); return; }
      if (!response.ok) throw new Error("unavailable");
      const snapshot = await response.json();
      if (this.stopped || this.request !== controller) return;
      if (!validSnapshot(snapshot)) throw new Error("invalid_snapshot");
      if (snapshot.instance_id !== this.instance) {
        this.closeSocket();
        this.instance = snapshot.instance_id;
        this.observed = -Infinity;
        this.nextSocket = 0;
      }
      this.lastRest = this.now();
      this.restHealthy = true;
      // An expired display needs the full snapshot again even when unchanged.
      if (this.expired) this.observed = -Infinity;
      this.expired = false;
      this.accept(snapshot);
      this.state(this.socketReady ? "live" : "polling");
      if (!this.socket && this.now() >= this.nextSocket) this.connect();
    } catch {
      if (!this.stopped && this.request === controller) {
        this.restHealthy = false;
        this.expire();
        this.state(this.expired ? "stale" : "reconnecting");
      }
    } finally {
      this.env.clearTimeout(timeout);
      if (this.request === controller) this.request = null;
    }
  }

  accept(snapshot) {
    if (snapshot.observed_at < this.observed) return;
    this.observed = snapshot.observed_at;
    this.onSnapshot(snapshot);
  }

  closeSocket() {
    const socket = this.socket;
    this.socket = null;
    this.socketReady = false;
    if (socket) socket.close();
  }

  retrySocket() {
    this.closeSocket();
    this.nextSocket = this.now() + this.retryDelay;
    this.retryDelay = Math.min(30000, this.retryDelay * 2);
    if (this.restHealthy) this.state("polling");
  }

  connect() {
    const url = new URL("/ws", this.origin);
    url.protocol = url.protocol === "https:" ? "wss:" : "ws:";
    let socket;
    try { socket = new this.env.WebSocket(url); }
    catch { this.retrySocket(); return; }
    this.socket = socket;
    this.socketStarted = this.now();
    this.socketReady = false;
    socket.onopen = () => {
      if (this.socket === socket && !this.stopped) socket.send(JSON.stringify({ token: this.token }));
    };
    socket.onmessage = event => {
      if (this.stopped || this.socket !== socket) return;
      this.expire();
      if (!this.fresh()) return;
      let snapshot;
      try { snapshot = JSON.parse(event.data); }
      catch { this.retrySocket(); return; }
      if (snapshot?.ok === true) return;
      if (!validSnapshot(snapshot) || snapshot.instance_id !== this.instance) {
        this.retrySocket();
        // A stale socket cannot invalidate a token accepted by authoritative REST.
        void this.refresh();
        return;
      }
      this.socketReady = true;
      this.retryDelay = 1000;
      this.accept(snapshot);
      this.state(this.restHealthy ? "live" : "reconnecting");
    };
    socket.onerror = socket.onclose = () => {
      if (this.socket === socket && !this.stopped) this.retrySocket();
    };
  }
}
