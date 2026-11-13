// codex-cli-monitor OpenCode plugin: surface prompts that are waiting on you.
//
// OpenCode holds pending permission and question prompts in memory only.  The
// SQLite database records granted approvals, not open asks, and the persisted
// event table carries no permission events at all, so a read-only observer
// cannot tell "the model is working" apart from "OpenCode stopped and is
// waiting for the user to choose".  This plugin closes that gap by appending
// tiny JSONL markers to a bounded local log the monitor already reads.
//
// It is deliberately observation-only:
//
//   * it subscribes to OpenCode's public event notification stream, and never
//     to a permission decision hook, so it cannot approve, reject, or
//     otherwise steer OpenCode;
//   * it records only structural identifiers (session id, request id) plus the
//     short permission category (for example `bash` or `edit`); prompt text,
//     command patterns, tool inputs, tool outputs, and metadata are never read;
//   * every failure is swallowed, so a full disk or a read-only home directory
//     can never surface an error inside OpenCode.
//
// Install it with `opencode-monitor-install-plugin`, which copies this file to
// `~/.config/opencode/plugin/`.  Removing that copy fully disables it; the
// OpenCode binary and its installed packages are never touched.

import fs from "node:fs"
import os from "node:os"
import path from "node:path"
import { Plugin } from "@opencode/plugin"

const SCHEMA_VERSION = 1
const MAX_LOG_BYTES = 1024 * 1024
const MAX_FIELD_LENGTH = 128

// Events that open a decision the user has to answer.
const ASK_EVENTS = {
  "permission.asked": "permission",
  "question.asked": "question",
  "form.created": "question",
}

// Events that answer one, whichever way the user went.
const REPLY_EVENTS = new Set([
  "permission.replied",
  "question.replied",
  "question.rejected",
  "form.replied",
  "form.cancelled",
])

function clean(value) {
  if (typeof value === "number" && Number.isFinite(value)) {
    value = String(value)
  }
  if (typeof value !== "string") {
    return null
  }
  let text = ""
  for (const character of value.trim()) {
    const code = character.codePointAt(0)
    if (code >= 0x20 && code !== 0x7f) {
      text += character
    }
    if (text.length >= MAX_FIELD_LENGTH) {
      break
    }
  }
  return text || null
}

function logPath() {
  const override = clean(process.env.OPENCODE_MONITOR_DECISION_LOG)
  if (override) {
    return override.startsWith("~")
      ? path.join(os.homedir(), override.slice(1))
      : override
  }
  const stateHome =
    clean(process.env.XDG_STATE_HOME) || path.join(os.homedir(), ".local", "state")
  return path.join(stateHome, "opencode-cli-monitor", "decisions.jsonl")
}

function append(record) {
  const line = JSON.stringify(record) + "\n"
  const file = logPath()
  fs.mkdirSync(path.dirname(file), { recursive: true, mode: 0o700 })
  let size = 0
  try {
    size = fs.statSync(file).size
  } catch {
    size = 0
  }
  if (size > 0 && size + Buffer.byteLength(line) > MAX_LOG_BYTES) {
    // One generation is enough: the monitor only ever needs decisions that are
    // still open, and it reads a bounded tail.
    try {
      fs.renameSync(file, file + ".1")
    } catch {
      // A rotation that loses the race just appends to the existing file.
    }
  }
  fs.appendFileSync(file, line, { mode: 0o600 })
}

function eventData(event) {
  if (event?.data && typeof event.data === "object") {
    return event.data
  }
  if (event?.properties && typeof event.properties === "object") {
    return event.properties
  }
  return {}
}

// V1 shaped some asks as a wrapper; V2 exposes the structural request directly
// in event.data. Both forms are accepted and anything unrecognized is dropped.
function askFields(type, data) {
  if (type === "form.created") {
    const form = data.form && typeof data.form === "object" ? data.form : {}
    return {
      request_id: clean(form.id) || clean(data.requestID) || clean(data.id),
      session_id: clean(form.sessionID) || clean(data.sessionID),
      category: "question",
    }
  }
  const nested =
    data.permission && typeof data.permission === "object"
      ? data.permission
      : data
  const category =
    clean(data.action) ||
    clean(typeof data.permission === "string" ? data.permission : null) ||
    clean(typeof nested.permission === "string" ? nested.permission : null) ||
    clean(nested.type)
  return {
    request_id: clean(nested.id) || clean(data.requestID) || clean(data.id),
    session_id: clean(nested.sessionID) || clean(data.sessionID),
    category,
  }
}

function replyFields(data) {
  return {
    request_id: clean(data.requestID) || clean(data.id),
    session_id: clean(data.sessionID),
    category: null,
  }
}

function record(directory, event) {
  const type = clean(event?.type)
  if (!type) {
    return
  }
  const asked = ASK_EVENTS[type]
  if (!asked && !REPLY_EVENTS.has(type)) {
    return
  }
  const data = eventData(event)
  const fields = asked ? askFields(type, data) : replyFields(data)
  if (!fields.request_id && !fields.session_id) {
    // Without an identifier the marker could never be paired with a reply, and
    // an unpairable ask would pin a row to "waiting" forever.
    return
  }
  append({
    schema_version: SCHEMA_VERSION,
    event: type,
    kind: asked || null,
    timestamp: Date.now() / 1000,
    pid: process.pid,
    directory,
    session_id: fields.session_id,
    request_id: fields.request_id,
    category: fields.category,
  })
}

const subscriptionTypes = [
  "permission.asked",
  "permission.replied",
  "form.created",
  "form.replied",
  "form.cancelled",
]

function subscribe(ctx, root) {
  const controller = new AbortController()
  void (async () => {
    try {
      for await (const event of ctx.event.subscribe({
        types: subscriptionTypes,
        signal: controller.signal,
      })) {
        try {
          record(root, event)
        } catch {
          // Monitoring must never change or interrupt an OpenCode session.
        }
      }
    } catch {
      // Plugin unload and event-stream failures are best-effort only.
    }
  })()
  return () => controller.abort()
}

const v2Definition = Plugin.define({
  id: "codex-cli-monitor.decisions",
  setup(ctx) {
    const root = clean(ctx?.location?.directory) || process.cwd()
    return subscribe(ctx, root)
  },
})

// The server entrypoint keeps one compatibility path for OpenCode 1 releases.
async function server({ directory, worktree } = {}) {
  const root = clean(directory) || clean(worktree) || process.cwd()
  return {
    event: async ({ event }) => {
      try {
        record(root, event)
      } catch {
        // Monitoring must never change or interrupt an OpenCode session.
      }
    },
  }
}

export default { ...v2Definition, server }
