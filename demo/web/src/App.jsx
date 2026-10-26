import { useCallback, useEffect, useState } from "react";
import { api } from "./api.js";

const FALLBACK_TASKS = [
  "review_pr", "publish_review", "account_brief", "update_stage",
  "followup_email", "draft_announcement", "publish_announcement",
  "publish_page", "exec_review_stats", "exec_digest",
  "remember_preference", "self_schedule", "daily_digest",
];
const SURFACES = ["channel", "dm", "thread", "ambient"];
const FALLBACK_USERS = ["ana", "ben", "cy", "dee"];

function IdentityChip({ identity }) {
  if (!identity) return <span className="chip chip-none">—</span>;
  const kind = identity.startsWith("service:")
    ? "service"
    : identity.startsWith("user:")
      ? "user"
      : "agent";
  return <span className={`chip chip-${kind}`}>{identity}</span>;
}

function RunTrace({ run, onApprove }) {
  if (!run) {
    return (
      <p className="empty">
        No run yet — pick an agent and a task, then Invoke. Try
        pipeline / followup_email from a shared channel to watch the
        mail tool refuse, or reviewer / publish_review to hit the
        approval gate.
      </p>
    );
  }
  return (
    <div className="run">
      <div className="run-head">
        <strong>{run.run_id}</strong>
        <span className="muted">
          {run.agent} · {run.total_tokens} tokens · $
          {Number(run.total_cost_usd).toFixed(4)}
        </span>
        <span className={`status status-${run.status}`}>{run.status}</span>
        {run.status === "awaiting_approval" && onApprove && (
          <button className="approve" onClick={() => onApprove(run)}>
            Approve {run.pending_approval?.target || "gated step"}
          </button>
        )}
      </div>
      <ol className="steps">
        {run.steps.map((s) => (
          <li key={s.seq} className={`step step-${s.status}`}>
            <span className="seq">{s.seq}</span>
            <span className="step-kind">
              {s.kind}
              {s.tool ? ` · ${s.tool}.${s.operation || ""}` : ""}
            </span>
            <span className={`step-status step-status-${s.status}`}>
              {s.status}
            </span>
            <IdentityChip identity={s.acting_identity} />
            {s.detail && <span className="step-detail">{s.detail}</span>}
            {s.output && <span className="step-output">{s.output}</span>}
          </li>
        ))}
      </ol>
      {run.output && <pre className="run-output">{run.output}</pre>}
    </div>
  );
}

function RunFeed({ runs }) {
  if (!runs.length) return <p className="empty">No runs this session yet.</p>;
  return (
    <div className="feed">
      {[...runs].reverse().map((r) => (
        <div key={r.run_id} className="feed-row">
          <strong>{r.run_id}</strong>
          <span>{r.agent}</span>
          <span className={`status status-${r.status}`}>{r.status}</span>
          <span className="muted">
            {r.steps.filter((s) => s.status === "blocked").length} blocked ·{" "}
            {r.total_tokens} tok
          </span>
        </div>
      ))}
    </div>
  );
}

export default function App() {
  const [data, setData] = useState(null);
  const [error, setError] = useState("");
  const [channel, setChannel] = useState("eng");
  const [messages, setMessages] = useState([]);

  const [postUser, setPostUser] = useState("ana");
  const [postText, setPostText] = useState("");

  const [agent, setAgent] = useState("reviewer");
  const [task, setTask] = useState("review_pr");
  const [user, setUser] = useState("ana");
  const [surface, setSurface] = useState("channel");
  const [param, setParam] = useState("PR-102");
  const [lastRun, setLastRun] = useState(null);
  const [busy, setBusy] = useState(false);

  const [rightTab, setRightTab] = useState("ambient");
  const [ticks, setTicks] = useState(30);
  const [ambient, setAmbient] = useState([]);
  const [runs, setRuns] = useState([]);
  const [audit, setAudit] = useState([]);
  const [memUser, setMemUser] = useState("ana");
  const [facts, setFacts] = useState([]);

  const users = data?.users?.map((u) => u.id) || FALLBACK_USERS;
  const tasks = data?.tasks || FALLBACK_TASKS;
  const agents = data?.agents || [];

  const refreshSide = useCallback(() => {
    api.runs().then(setRuns).catch(() => {});
    api.audit().then(setAudit).catch(() => {});
  }, []);

  const refreshMessages = useCallback(() => {
    api.messages(channel).then(setMessages).catch(() => setMessages([]));
  }, [channel]);

  useEffect(() => {
    api
      .state()
      .then((d) => {
        setData(d);
        setError("");
      })
      .catch((e) =>
        setError(
          `API unreachable (${e.message}). Start it with: uvicorn demo.backend.app:app`
        )
      );
    refreshSide();
  }, [refreshSide]);

  useEffect(refreshMessages, [refreshMessages]);

  useEffect(() => {
    api.memory(memUser).then(setFacts).catch(() => setFacts([]));
  }, [memUser, runs]);

  const afterAction = () => {
    api.state().then(setData).catch(() => {});
    refreshMessages();
    refreshSide();
  };

  const doPost = async (e) => {
    e.preventDefault();
    if (!postText.trim()) return;
    await api.post(channel, postUser, postText.trim());
    setPostText("");
    refreshMessages();
  };

  const doInvoke = async () => {
    setBusy(true);
    try {
      const params = param.trim()
        ? { pr: param.trim(), account: param.trim(), text: param.trim() }
        : {};
      const run = await api.invoke({
        agent,
        task,
        user: surface === "ambient" ? null : user,
        surface,
        channel_id: channel,
        params,
      });
      setLastRun(run);
      afterAction();
    } catch (e) {
      setError(`Invoke failed: ${e.message}`);
    } finally {
      setBusy(false);
    }
  };

  const doApprove = async (run) => {
    try {
      const updated = await api.approve(run.run_id, user);
      setLastRun(updated);
      afterAction();
    } catch (e) {
      setError(`Approve failed: ${e.message}`);
    }
  };

  const doTick = async () => {
    try {
      const results = await api.tick(Number(ticks) || 0);
      setAmbient(results);
      if (results.length) setLastRun(results[results.length - 1]);
      afterAction();
    } catch (e) {
      setError(`Tick failed: ${e.message}`);
    }
  };

  const doEvent = async () => {
    try {
      const results = await api.event("pull_request.opened", param.trim() || "PR-102");
      setAmbient(results);
      if (results.length) setLastRun(results[results.length - 1]);
      afterAction();
    } catch (e) {
      setError(`Event failed: ${e.message}`);
    }
  };

  return (
    <div className="app">
      <header>
        <h1>Open Company Harness</h1>
        <p>
          {data?.company || "Meridian Labs"} — declared agents living in team
          channels, acting under a brokered identity policy. Tick{" "}
          {data?.tick ?? "–"}.
        </p>
      </header>
      {error && <p className="error">{error}</p>}

      <div className="layout">
        <aside className="rail">
          <h2>Channels</h2>
          {(data?.channels || []).map((c) => (
            <button
              key={c.id}
              className={c.id === channel ? "rail-row active" : "rail-row"}
              onClick={() => setChannel(c.id)}
            >
              <span className="row-title">{c.name}</span>
              <span className="row-sub">{c.members.join(", ")}</span>
            </button>
          ))}

          <h2>People</h2>
          {users.map((u) => (
            <div key={u} className="rail-row static">
              <span className="row-title">@{u}</span>
            </div>
          ))}

          <h2>Agents</h2>
          {agents.map((a) => (
            <button
              key={a.name}
              className={a.name === agent ? "rail-row active" : "rail-row"}
              onClick={() => setAgent(a.name)}
            >
              <span className="row-title">{a.name}</span>
              <span className="row-sub">{a.role}</span>
              <span className="row-sub">
                {a.tools.join(", ")} · {a.provider}
              </span>
            </button>
          ))}

          <h2>Tools</h2>
          {(data?.tools || []).map((t) => (
            <div key={t.name} className="rail-row static">
              <span className="row-title">{t.name}</span>
              <span className="row-sub">
                {t.identity_mode} · {t.operations.join(", ")}
              </span>
            </div>
          ))}
        </aside>

        <main className="centre">
          <section className="panel">
            <h2>
              {(data?.channels || []).find((c) => c.id === channel)?.name ||
                channel}
            </h2>
            <div className="transcript">
              {messages.map((m) => (
                <div key={m.id} className="msg">
                  <span className="msg-author">{m.author}</span>
                  <span className="msg-text">{m.text}</span>
                  {m.thread_id && (
                    <span className="row-sub">thread {m.thread_id}</span>
                  )}
                </div>
              ))}
              {messages.length === 0 && (
                <p className="empty">No messages in this channel yet.</p>
              )}
            </div>
            <form className="postbox" onSubmit={doPost}>
              <select
                value={postUser}
                onChange={(e) => setPostUser(e.target.value)}
              >
                {users.map((u) => (
                  <option key={u} value={u}>
                    {u}
                  </option>
                ))}
              </select>
              <input
                value={postText}
                onChange={(e) => setPostText(e.target.value)}
                placeholder="Post a message (workspace only — does not invoke an agent)…"
              />
              <button type="submit">Post</button>
            </form>
          </section>

          <section className="panel">
            <h2>Invoke an agent</h2>
            <div className="invoke-grid">
              <label>
                Agent
                <select value={agent} onChange={(e) => setAgent(e.target.value)}>
                  {agents.map((a) => (
                    <option key={a.name} value={a.name}>
                      {a.name}
                    </option>
                  ))}
                </select>
              </label>
              <label>
                Task
                <select value={task} onChange={(e) => setTask(e.target.value)}>
                  {tasks.map((t) => (
                    <option key={t} value={t}>
                      {t}
                    </option>
                  ))}
                </select>
              </label>
              <label>
                User
                <select value={user} onChange={(e) => setUser(e.target.value)}>
                  {users.map((u) => (
                    <option key={u} value={u}>
                      {u}
                    </option>
                  ))}
                </select>
              </label>
              <label>
                Surface
                <select
                  value={surface}
                  onChange={(e) => setSurface(e.target.value)}
                >
                  {SURFACES.map((s) => (
                    <option key={s} value={s}>
                      {s}
                    </option>
                  ))}
                </select>
              </label>
              <label className="wide">
                Param (PR / account / text)
                <input
                  value={param}
                  onChange={(e) => setParam(e.target.value)}
                  placeholder="PR-102 or Acme Corp"
                />
              </label>
              <button className="invoke" onClick={doInvoke} disabled={busy}>
                {busy ? "Running…" : "Invoke"}
              </button>
            </div>
            <RunTrace run={lastRun} onApprove={doApprove} />
          </section>
        </main>

        <aside className="rail right">
          <nav className="tabs">
            {["ambient", "audit", "memory"].map((t) => (
              <button
                key={t}
                className={rightTab === t ? "tab active" : "tab"}
                onClick={() => setRightTab(t)}
              >
                {t[0].toUpperCase() + t.slice(1)}
              </button>
            ))}
          </nav>

          {rightTab === "ambient" && (
            <div>
              <div className="tickbox">
                <input
                  type="number"
                  min="0"
                  value={ticks}
                  onChange={(e) => setTicks(e.target.value)}
                />
                <button onClick={doTick}>Advance ticks</button>
              </div>
              <button className="event" onClick={doEvent}>
                Emit pull_request.opened ({param.trim() || "PR-102"})
              </button>
              <p className="muted small">
                A system schedule runs pipeline / daily_digest every 30
                ticks; the event trigger runs reviewer / review_pr under
                the codehost-bot service identity.
              </p>
              <h3>Latest ambient results</h3>
              <RunFeed runs={ambient} />
              <h3>All runs this session</h3>
              <RunFeed runs={runs} />
            </div>
          )}

          {rightTab === "audit" && (
            <table className="audit">
              <thead>
                <tr>
                  <th>Kind</th>
                  <th>Target</th>
                  <th>Decision</th>
                  <th>Acting identity</th>
                </tr>
              </thead>
              <tbody>
                {[...audit].reverse().map((e) => (
                  <tr key={e.seq} className={e.decision === "block" ? "blocked" : ""}>
                    <td>{e.kind}</td>
                    <td className="break">{e.target}</td>
                    <td>{e.decision}</td>
                    <td>
                      <IdentityChip identity={e.acting_identity} />
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          )}

          {rightTab === "memory" && (
            <div>
              <label className="memselect">
                Visible to
                <select
                  value={memUser}
                  onChange={(e) => setMemUser(e.target.value)}
                >
                  {users.map((u) => (
                    <option key={u} value={u}>
                      {u}
                    </option>
                  ))}
                </select>
              </label>
              {facts.map((f) => (
                <div key={f.id} className="fact">
                  <span className={`badge badge-${f.tier}`}>{f.tier}</span>
                  <span className="badge">{f.provenance.route}</span>
                  <div className="fact-text">{f.text}</div>
                  <div className="row-sub">
                    {f.id}
                    {f.owner ? ` · owner ${f.owner}` : ""} · via{" "}
                    {f.provenance.source_agent} · {f.provenance.surface}
                  </div>
                </div>
              ))}
              {facts.length === 0 && <p className="empty">No visible facts.</p>}
            </div>
          )}
        </aside>
      </div>
    </div>
  );
}
