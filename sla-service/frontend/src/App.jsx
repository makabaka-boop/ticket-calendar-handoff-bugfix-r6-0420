import { useCallback, useEffect, useState } from "react";
import { api } from "./api.js";
import { fmtDT, fmtMin, STATUS_LABEL } from "./format.js";
import TicketDetail from "./components/TicketDetail.jsx";
import Policies from "./components/Policies.jsx";

export default function App() {
  const [tickets, setTickets] = useState([]);
  const [selectedId, setSelectedId] = useState(null);
  const [detail, setDetail] = useState(null);
  const [policies, setPolicies] = useState([]);
  const [conflict, setConflict] = useState(null);
  const [error, setError] = useState(null);
  const [view, setView] = useState("tickets");
  const [newTitle, setNewTitle] = useState("");
  const [serverTime, setServerTime] = useState(null);

  const run = useCallback(async (fn) => {
    setError(null);
    try {
      return await fn();
    } catch (e) {
      if (e.status === 409) {
        setConflict(
          `修订号已过期（当前 revision=${e.body?.current_revision}），已为你刷新最新状态。`,
        );
        await refresh();
      } else {
        setError(e.message);
      }
      return null;
    }
  }, []);

  const refresh = useCallback(async () => {
    const [list, meta] = await Promise.all([api.listTickets(), api.meta()]);
    setTickets(list);
    setServerTime(meta.server_time);
    return list;
  }, []);

  const loadDetail = useCallback(async (id) => {
    if (id == null) return setDetail(null);
    setDetail(await api.getTicket(id));
  }, []);

  useEffect(() => {
    refresh();
    api.listPolicies().then(setPolicies);
  }, [refresh]);

  useEffect(() => {
    loadDetail(selectedId);
  }, [selectedId, loadDetail]);

  // 后台扫描器在服务端周期运行，前端定时刷新以反映最新裁决
  useEffect(() => {
    const t = setInterval(() => {
      refresh();
      if (selectedId != null) loadDetail(selectedId);
    }, 10000);
    return () => clearInterval(t);
  }, [refresh, loadDetail, selectedId]);

  async function createTicket(e) {
    e.preventDefault();
    if (!newTitle.trim()) return;
    const created = await run(() => api.createTicket(newTitle.trim()));
    if (created) {
      setNewTitle("");
      await refresh();
      setSelectedId(created.id);
    }
  }

  async function scanNow() {
    await run(() => api.scan());
    await refresh();
    if (selectedId != null) loadDetail(selectedId);
  }

  return (
    <div className="app">
      <header>
        <h1>工单时限计时服务</h1>
        <div className="header-right">
          <span className="muted">服务器时间 {fmtDT(serverTime)}</span>
          <button onClick={scanNow}>立即扫描</button>
          <button
            className={view === "policies" ? "active" : ""}
            onClick={() =>
              setView(view === "policies" ? "tickets" : "policies")
            }
          >
            {view === "policies" ? "返回工单" : "策略管理"}
          </button>
        </div>
      </header>

      {conflict && (
        <div className="banner conflict" onClick={() => setConflict(null)}>
          ⚠ 提交冲突：{conflict}（点击关闭）
        </div>
      )}
      {error && (
        <div className="banner error" onClick={() => setError(null)}>
          ✕ {error}（点击关闭）
        </div>
      )}

      {view === "policies" ? (
        <Policies
          policies={policies}
          onChanged={async () => {
            setPolicies(await api.listPolicies());
            await refresh();
          }}
          run={run}
        />
      ) : (
        <main>
          <section className="list-pane">
            <form onSubmit={createTicket} className="new-ticket">
              <input
                value={newTitle}
                onChange={(e) => setNewTitle(e.target.value)}
                placeholder="新工单标题…"
              />
              <button type="submit">创建</button>
            </form>
            <ul className="ticket-list">
              {tickets.map((t) => (
                <li
                  key={t.id}
                  className={t.id === selectedId ? "selected" : ""}
                  onClick={() => {
                    setConflict(null);
                    setSelectedId(t.id);
                  }}
                >
                  <div className="row">
                    <span className="title">
                      #{t.id} {t.title}
                    </span>
                    <span className={`badge status-${t.status}`}>
                      {STATUS_LABEL[t.status]}
                    </span>
                  </div>
                  <div className="row muted small">
                    <span>
                      {t.policy_version} · 已计 {fmtMin(t.accumulated_minutes)}
                    </span>
                    <span>
                      {t.escalated && <span className="badge esc">已升级</span>}
                      {!t.escalated && t.warned && (
                        <span className="badge warn">已警告</span>
                      )}
                    </span>
                  </div>
                  <div className="row muted small">
                    <span>升级截止 {fmtDT(t.escalate_deadline)}</span>
                  </div>
                </li>
              ))}
            </ul>
          </section>

          <section className="detail-pane">
            {detail ? (
              <TicketDetail
                key={`${detail.id}-${detail.revision}`}
                detail={detail}
                policies={policies}
                run={run}
                onChanged={async () => {
                  await refresh();
                  await loadDetail(detail.id);
                }}
              />
            ) : (
              <div className="empty">选择左侧工单查看计时详情</div>
            )}
          </section>
        </main>
      )}
    </div>
  );
}
