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
//   * it subscribes to the `event` hook, which is a notification stream, and
//     never to `permission.ask` or any other decision hook, so it cannot
//     approve, reject, or otherwise steer OpenCode;
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

const SCHEMA_VERSION = 1
const MAX_LOG_BYTES = 1024 * 1024
const MAX_FIELD_LENGTH = 128

// Events that open a decision the user has to answer.
const ASK_EVENTS = {
  "permission.asked": "permission",
  "question.asked": "question",
}

// Events that answer one, whichever way the user went.
const REPLY_EVENTS = new Set([
  "permission.replied",
  "question.replied",
  "question.rejected",
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

// The ask payload has been shaped both as the request itself and as a wrapper
// carrying it, so both are accepted and anything unrecognized is dropped.
function askFields(properties) {
  const nested =
    properties.permission && typeof properties.permission === "object"
      ? properties.permission
      : properties
  const category =
    clean(typeof properties.permission === "string" ? properties.permission : null) ||
    clean(typeof nested.permission === "string" ? nested.permission : null) ||
    clean(nested.type)
  return {
    request_id: clean(nested.id) || clean(properties.requestID) || clean(properties.id),
    session_id: clean(nested.sessionID) || clean(properties.sessionID),
    category,
  }
}

function replyFields(properties) {
  return {
    request_id: clean(properties.requestID) || clean(properties.id),
    session_id: clean(properties.sessionID),
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
  const properties =
    event.properties && typeof event.properties === "object" ? event.properties : {}
  const fields = asked ? askFields(properties) : replyFields(properties)
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

export const CodexMonitorDecisions = async ({ directory, worktree }) => {
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
