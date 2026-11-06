import assert from "node:assert/strict";
import { test } from "node:test";
import { MonitorClient, STALE_MS } from "../src/codex_cli_monitor/web_assets/client.mjs";

const flush = () => new Promise(resolve => setImmediate(resolve));
const session = { session_key: "one:1", server_id: "one", pid: 1, status: "运行中" };
const snapshot = (observed = 1, instance = "current", sessions = [session]) => ({
  instance_id: instance, observed_at: observed, sessions, servers: [{ id: "one", name: "One" }],
});

function setup(token = "read-secret") {
  let now = 0;
  let nextTimer = 0;
  const timers = new Map();
  const requests = [], sockets = [], displayed = [], states = [];
  const control = { reply: () => ({ ok: true, status: 200, json: async () => snapshot(now + 1) }) };
  const env = {
    performance: { now: () => now }, AbortController,
    setTimeout: (callback, delay) => { const id = ++nextTimer; timers.set(id, { callback, at: now + delay }); return id; },
    clearTimeout: id => timers.delete(id),
    fetch: async (url, options) => { requests.push({ url: String(url), options }); return control.reply(options); },
    WebSocket: class {
      constructor(url) { this.url = String(url); this.sent = []; this.closed = false; sockets.push(this); }
      send(message) { this.sent.push(JSON.parse(message)); }
      close() { this.closed = true; }
      message(value) { this.onmessage?.({ data: JSON.stringify(value) }); }
    },
  };
  const client = new MonitorClient({ token, origin: "https://monitor.example", env,
    onSnapshot: value => displayed.push(value), onState: value => states.push(value) });
  const advance = async ms => {
    const end = now + ms;
    for (;;) {
      const pending = [...timers.entries()].filter(([, timer]) => timer.at <= end).sort((a, b) => a[1].at - b[1].at)[0];
      if (!pending) break;
      now = pending[1].at;
      timers.delete(pending[0]);
      pending[1].callback();
      await flush();
    }
    now = end;
    await flush();
  };
  return { client, control, requests, sockets, displayed, states, advance, timers };
}

test("missing token never sends requests; credentials stay out of API and socket URLs", async () => {
  const missing = setup("");
  missing.client.start();
  assert.equal(missing.states.at(-1), "unauthorized");
  assert.equal(missing.requests.length, 0);
  const fixture = setup();
  fixture.client.start();
  await flush();
  assert.equal(fixture.requests[0].url, "https://monitor.example/api/sessions");
  assert.equal(fixture.requests[0].options.headers.Authorization, "Bearer read-secret");
  assert.equal(fixture.requests[0].options.referrerPolicy, "no-referrer");
  assert.equal(fixture.requests[0].options.redirect, "error");
  assert.equal(fixture.sockets[0].url, "wss://monitor.example/ws");
  fixture.sockets[0].onopen();
  assert.deepEqual(fixture.sockets[0].sent, [{ token: "read-secret" }]);
});

test("REST corrects a silent socket and accepts an empty snapshot immediately", async () => {
  const fixture = setup();
  fixture.client.start();
  await flush();
  fixture.sockets[0].message(snapshot(2));
  fixture.control.reply = () => ({ ok: true, status: 200, json: async () => snapshot(3, "current", []) });
  await fixture.advance(5000);
  assert.deepEqual(fixture.displayed.at(-1).sessions, []);
  assert.equal(fixture.requests.length, 2);
});

test("backend restart retires the old socket and rejects its delayed frames", async () => {
  const fixture = setup();
  fixture.client.start();
  await flush();
  const old = fixture.sockets[0];
  old.message(snapshot(2));
  fixture.control.reply = () => ({ ok: true, status: 200, json: async () => snapshot(3, "new", []) });
  await fixture.advance(5000);
  assert.equal(old.closed, true);
  old.message(snapshot(100));
  assert.equal(fixture.displayed.at(-1).instance_id, "new");
  assert.equal(fixture.displayed.at(-1).sessions.length, 0);
  const wrong = fixture.sockets.at(-1);
  wrong.message(snapshot(101));
  assert.equal(wrong.closed, true);
  assert.equal(fixture.displayed.at(-1).instance_id, "new");
});

test("older HTTP responses cannot overwrite newer socket state", async () => {
  const fixture = setup();
  fixture.client.start();
  await flush();
  let resolve;
  fixture.control.reply = () => new Promise(done => { resolve = done; });
  const request = fixture.client.refresh();
  const latest = snapshot(100, "current", [{ ...session, status: "成功" }]);
  fixture.sockets[0].message(latest);
  resolve({ ok: true, status: 200, json: async () => snapshot(2) });
  await request;
  assert.deepEqual(fixture.displayed.at(-1), latest);
});

test("loss of authoritative REST expires old rows even when the socket sends data", async () => {
  const fixture = setup();
  fixture.client.start();
  await flush();
  const socket = fixture.sockets[0];
  socket.message(snapshot(2));
  fixture.control.reply = () => { throw new Error("offline"); };
  await fixture.advance(STALE_MS - 1000);
  socket.message(snapshot(3));
  assert.notEqual(fixture.displayed.at(-1), null);
  await fixture.advance(1000);
  assert.equal(fixture.displayed.at(-1), null);
  assert.equal(fixture.states.at(-1), "stale");
  socket.message(snapshot(200));
  assert.equal(fixture.displayed.at(-1), null);
  fixture.control.reply = () => ({ ok: true, status: 200, json: async () => snapshot(200, "current", []) });
  await fixture.advance(5000);
  assert.deepEqual(fixture.displayed.at(-1).sessions, []);
});

test("revoked read token clears sessions, closes connections, and stops retrying", async () => {
  const fixture = setup();
  fixture.client.start();
  await flush();
  fixture.control.reply = () => ({ ok: false, status: 401 });
  await fixture.advance(5000);
  assert.equal(fixture.states.at(-1), "unauthorized");
  assert.equal(fixture.displayed.at(-1), null);
  assert.equal(fixture.sockets[0].closed, true);
  const count = fixture.requests.length;
  await fixture.advance(60000);
  assert.equal(fixture.requests.length, count);
});

test("blocked WebSocket falls back to polling and uses bounded retries", async () => {
  const fixture = setup();
  fixture.client.start();
  await flush();
  fixture.sockets[0].onerror();
  await fixture.advance(1000);
  assert.equal(fixture.sockets.length, 2);
  fixture.sockets[1].onerror();
  await fixture.advance(1000);
  assert.equal(fixture.sockets.length, 2);
  await fixture.advance(3000);
  assert.equal(fixture.requests.length, 2);
  assert.equal(fixture.states.at(-1), "polling");
});

test("sockets rotate after sixty seconds and hanging connects are bounded", async () => {
  const fixture = setup();
  fixture.client.start();
  await flush();
  const first = fixture.sockets[0];
  first.message(snapshot(2));
  await fixture.advance(60000);
  assert.equal(first.closed, true);
  assert.equal(fixture.sockets.length, 2);
  await fixture.advance(8000);
  assert.equal(fixture.sockets[1].closed, true);
});

test("malformed REST cannot renew freshness and never fabricates a session status", async () => {
  const fixture = setup();
  fixture.client.start();
  await flush();
  fixture.control.reply = () => ({ ok: true, status: 200, json: async () => snapshot(3, "current", [{ ...session, status: "unknown" }]) });
  await fixture.advance(STALE_MS);
  assert.equal(fixture.displayed.at(-1), null);
});

test("requests time out, never overlap, and late completion after stop is ignored", async () => {
  const fixture = setup();
  let resolve;
  fixture.control.reply = () => new Promise(done => { resolve = done; });
  fixture.client.start();
  await fixture.advance(8000);
  assert.equal(fixture.requests.length, 1);
  assert.equal(fixture.requests[0].options.signal.aborted, true);
  fixture.client.stop();
  resolve({ ok: true, status: 200, json: async () => snapshot() });
  await flush();
  assert.equal(fixture.displayed.length, 0);
  assert.equal(fixture.sockets.length, 0);
});
