// API base: same-origin /api by default (Vite proxies it in dev; on
// Render set VITE_API_URL at build time to the API service URL).
const BASE = import.meta.env.VITE_API_URL || "";

async function get(path) {
  const resp = await fetch(`${BASE}${path}`);
  if (!resp.ok) throw new Error(`${path}: ${resp.status}`);
  return resp.json();
}

async function post(path, body) {
  const resp = await fetch(`${BASE}${path}`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  });
  if (!resp.ok) {
    let detail = `${resp.status}`;
    try {
      detail = (await resp.json()).detail || detail;
    } catch {
      /* keep status */
    }
    throw new Error(`${path}: ${detail}`);
  }
  return resp.json();
}

export const api = {
  state: () => get("/api/state"),
  messages: (channel) =>
    get(`/api/messages?channel=${encodeURIComponent(channel)}`),
  audit: () => get("/api/audit"),
  runs: () => get("/api/runs"),
  memory: (user) => get(`/api/memory?user=${encodeURIComponent(user)}`),
  post: (channel_id, user, text) => post("/api/post", { channel_id, user, text }),
  invoke: (payload) => post("/api/invoke", payload),
  approve: (run_id, approver) => post("/api/approve", { run_id, approver }),
  tick: (ticks) => post("/api/tick", { ticks }),
  event: (event_type, pr) => post("/api/event", { event_type, pr }),
};
