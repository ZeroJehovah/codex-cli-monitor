import { MonitorClient, STATUSES } from "/web/client.mjs";

const byId = id => document.getElementById(id);
const clientNames = { codex: "Codex", opencode: "OpenCode", claude: "Claude Code", qoder: "Qoder" };
const connectionLabels = {
  connecting: "正在连接", live: "实时同步", polling: "自动刷新",
  reconnecting: "正在重连", stale: "连接中断", unauthorized: "鉴权失败",
};
let currentSnapshot = null;
let displaySignature = null;
let selectedSession = null;
let connectionState = "connecting";

function element(tag, className, text) {
  const node = document.createElement(tag);
  if (className) node.className = className;
  if (text !== undefined) node.textContent = text;
  return node;
}

function cliName(session) { return clientNames[session.cli_type] || session.cli_type || "Codex"; }
function statusText(session) {
  return session.status === "待确认" && session.waiting_reason
    ? `${session.status} · ${session.waiting_reason}` : session.status;
}
function startTime(session) {
  return Number.isFinite(session.started_at)
    ? new Date(session.started_at * 1000).toLocaleString() : "未知";
}
function details(session) {
  byId("detail-title").textContent = cliName(session);
  const fields = [
    ["状态", statusText(session)], ["服务器", session.server_name || session.server_id],
    ["目录", session.directory || "未知"], ["PID", session.pid], ["启动时间", startTime(session)],
  ];
  byId("detail-fields").replaceChildren(...fields.flatMap(([key, value]) => [
    element("dt", "", key), element("dd", "", String(value)),
  ]));
}

function render(snapshot) {
  currentSnapshot = snapshot;
  const sessions = snapshot?.sessions || [];
  const servers = snapshot?.servers || [];
  byId("summary").textContent = snapshot ? `${servers.length} 台服务器 · ${sessions.length} 个会话` : "会话状态";
  for (const [status, className] of Object.entries(STATUSES)) {
    byId(`count-${className}`).textContent = sessions.filter(session => session.status === status).length;
  }
  if (snapshot) {
    const updated = new Date(snapshot.observed_at * 1000);
    byId("updated").textContent = updated.toLocaleTimeString([], { hour12: false });
    byId("updated").dateTime = updated.toISOString();
  } else {
    byId("updated").textContent = "";
    byId("updated").removeAttribute("datetime");
  }
  if (selectedSession) {
    const selected = sessions.find(session => session.session_key === selectedSession);
    if (selected) details(selected);
    else { byId("details").close(); selectedSession = null; }
  }

  // Ignore timestamps in the rendering key so polling preserves focus and glow.
  const signature = JSON.stringify([
    servers.map(server => [server.id, server.name]),
    sessions.map(session => [session.session_key, session.server_id, session.server_name,
      session.directory, session.cli_type, session.pid, session.started_at, session.status, session.waiting_reason]),
  ]);
  if (signature !== displaySignature) {
    displaySignature = signature;
    const focusedKey = document.activeElement?.dataset.sessionKey;
    const ordered = [...servers].sort((a, b) => (a.name || a.id).localeCompare(b.name || b.id) || a.id.localeCompare(b.id));
    const sections = ordered.map((server, index) => {
      const section = element("section", `server color-${index % 5}`);
      const rows = new Map();
      const local = sessions.filter(session => session.server_id === server.id)
        .sort((a, b) => (a.started_at ?? Infinity) - (b.started_at ?? Infinity) || a.pid - b.pid);
      const header = element("header", "server-heading");
      header.append(element("h2", "", server.name || server.id), element("span", "server-count", `${local.length} 个会话`));
      section.append(header);
      for (const session of local) {
        const directory = session.directory || "未知目录";
        if (!rows.has(directory)) rows.set(directory, []);
        rows.get(directory).push(session);
      }
      for (const [directory, group] of rows) {
        const row = element("div", "directory-row");
        const label = element("div");
        const normalized = directory.replaceAll("\\", "/").replace(/\/$/, "");
        const name = normalized.split("/").pop() || directory;
        label.append(element("p", "directory-name", name), element("p", "directory-path", directory));
        label.title = directory;
        const dots = element("div", "session-dots");
        for (const session of group) {
          const button = element("button", `session ${STATUSES[session.status]}`);
          button.type = "button";
          button.dataset.sessionKey = session.session_key;
          button.title = `${cliName(session)} · ${statusText(session)}\n${session.directory || "未知目录"}\nPID ${session.pid} · ${startTime(session)}`;
          button.setAttribute("aria-label", `${cliName(session)} · ${statusText(session)} · PID ${session.pid} · ${directory}`);
          button.setAttribute("aria-haspopup", "dialog");
          button.append(element("span", "dot"));
          button.addEventListener("click", () => {
            const latest = currentSnapshot?.sessions.find(item => item.session_key === session.session_key);
            if (!latest) return;
            selectedSession = latest.session_key;
            details(latest);
            byId("details").showModal();
          });
          dots.append(button);
        }
        row.append(label, dots);
        section.append(row);
      }
      if (!local.length) section.append(element("p", "server-empty", "暂无会话"));
      return section;
    });
    byId("servers").replaceChildren(...sections);
    if (focusedKey) {
      [...document.querySelectorAll(".session")].find(button => button.dataset.sessionKey === focusedKey)?.focus({ preventScroll: true });
    }
  }
  renderConnection();
}

function renderConnection() {
  byId("connection").textContent = connectionLabels[connectionState];
  byId("connection").dataset.state = connectionState;
  const messages = {
    unauthorized: "访问凭证已失效，请使用带有有效 token 的链接重新打开。",
    reconnecting: "连接暂时中断，正在重试。下方为最近一次同步的会话。",
    stale: "暂时无法获取最新会话，恢复连接后会自动刷新。",
  };
  byId("notice").textContent = messages[connectionState] || "";
  byId("notice").hidden = !messages[connectionState];
  byId("empty").hidden = Boolean(currentSnapshot?.servers.length);
  byId("empty-title").textContent = currentSnapshot ? "暂无会话" : connectionLabels[connectionState];
  byId("empty-message").textContent = currentSnapshot ? "提交任务后，会话将自动显示在这里。"
    : connectionState === "connecting" ? "正在获取会话状态…" : "";
  byId("refresh").disabled = connectionState === "unauthorized";
}

const tokens = new URL(location.href).searchParams.getAll("token");
const client = new MonitorClient({
  token: tokens.length === 1 ? tokens[0] : "",
  origin: location.origin,
  onSnapshot: render,
  onState: state => { connectionState = state; renderConnection(); },
});
byId("refresh").addEventListener("click", () => void client.refresh());
byId("close-details").addEventListener("click", () => byId("details").close());
byId("details").addEventListener("close", () => { selectedSession = null; });
document.addEventListener("visibilitychange", () => { if (!document.hidden) void client.refresh(); });
window.addEventListener("online", () => void client.refresh());
window.addEventListener("pagehide", () => { client.stop(); render(null); });
window.addEventListener("pageshow", () => { client.start(); void client.refresh(); });
client.start();
