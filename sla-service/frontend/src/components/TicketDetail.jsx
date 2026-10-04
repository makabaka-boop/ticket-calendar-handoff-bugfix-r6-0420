import { useState } from "react";
import { api } from "../api.js";
import { fmtDT, fmtMin, fmtShift, STATUS_LABEL } from "../format.js";
import Timeline from "./Timeline.jsx";

function DeadlineCard({ label, value, hint }) {
  return (
    <div className="deadline-card">
      <div className="muted small">{label}</div>
      <div className="deadline-value">{fmtDT(value)}</div>
      {hint && <div className="muted small">{hint}</div>}
    </div>
  );
}

function IntervalTable({ title, intervals, empty }) {
  return (
    <div className="interval-table">
      <h4>
        {title} <span className="muted small">({intervals.length})</span>
      </h4>
      {intervals.length === 0 ? (
        <div className="muted small">{empty}</div>
      ) : (
        <table>
          <tbody>
            {intervals.map(([s, e], i) => (
              <tr key={i}>
                <td className="mono">{fmtDT(s)}</td>
                <td className="mono">{e ? fmtDT(e) : "至今"}</td>
              </tr>
            ))}
          </tbody>
        </table>
      )}
    </div>
  );
}

function Adjudication({ a }) {
  const [open, setOpen] = useState(false);
  const b = a.basis;
  return (
    <li className={`adjudication ${a.kind}`}>
      <div className="row">
        <span className={`badge ${a.kind === "escalation" ? "esc" : "warn"}`}>
          {a.kind === "escalation" ? "升级" : "警告"}
        </span>
        <span className="mono">{fmtDT(a.adjudicated_at)}</span>
        <span className="muted small">
          策略 {b.policy?.name} v{b.policy?.version}
        </span>
        <button className="link" onClick={() => setOpen(!open)}>
          {open ? "收起依据" : "裁决依据"}
        </button>
      </div>
      <div className="muted small">
        裁决时累计 {fmtMin(a.accumulated_minutes)} ≥ 阈值{" "}
        {fmtMin(a.threshold_minutes)}
      </div>
      {open && (
        <div className="basis">
          <p className="small">
            规则：<code>{b.rule}</code>（{fmtDT(b.as_of)} 时点计算）
          </p>
          <p className="small">
            计入区间 {b.counted_intervals.length} 段、暂停区间{" "}
            {b.paused_intervals.length} 段，明细如下：
          </p>
          <pre>{JSON.stringify(b, null, 2)}</pre>
        </div>
      )}
    </li>
  );
}

function DiffTable({ diff }) {
  const rows = [
    [
      "警告阈值",
      fmtMin(diff.from.warn_minutes),
      fmtMin(diff.to.warn_minutes),
      fmtShift(diff.delta.warn_minutes * 60),
    ],
    [
      "升级阈值",
      fmtMin(diff.from.escalate_minutes),
      fmtMin(diff.to.escalate_minutes),
      fmtShift(diff.delta.escalate_minutes * 60),
    ],
    [
      "已累计有效分钟",
      fmtMin(diff.from.accumulated_minutes),
      fmtMin(diff.to.accumulated_minutes),
      fmtShift(diff.delta.accumulated_minutes * 60),
    ],
    [
      "警告截止",
      fmtDT(diff.from.warn_deadline),
      fmtDT(diff.to.warn_deadline),
      fmtShift(diff.delta.warn_deadline_shift_seconds),
    ],
    [
      "升级截止",
      fmtDT(diff.from.escalate_deadline),
      fmtDT(diff.to.escalate_deadline),
      fmtShift(diff.delta.escalate_deadline_shift_seconds),
    ],
  ];
  return (
    <table className="diff-table">
      <thead>
        <tr>
          <th>指标</th>
          <th>v{diff.from.version}（当前）</th>
          <th>v{diff.to.version}（目标）</th>
          <th>差异</th>
        </tr>
      </thead>
      <tbody>
        {rows.map(([k, a, b, d]) => (
          <tr key={k}>
            <td>{k}</td>
            <td className="mono">{a}</td>
            <td className="mono">{b}</td>
            <td className="mono">{d}</td>
          </tr>
        ))}
      </tbody>
    </table>
  );
}

export default function TicketDetail({ detail, policies, run, onChanged }) {
  const { timing } = detail;
  const [targetVersion, setTargetVersion] = useState("");
  const [preview, setPreview] = useState(null);

  const policy = policies.find((p) => p.policy_id === detail.policy.policy_id);
  const otherVersions = (policy?.versions || []).filter(
    (v) => v.policy_version_id !== detail.policy.policy_version_id,
  );

  async function action(act) {
    const ok = await run(() => api.setStatus(detail.id, act, detail.revision));
    if (ok) await onChanged();
  }

  async function doPreview() {
    if (!targetVersion) return;
    const p = await run(() =>
      api.migrationPreview(detail.id, Number(targetVersion)),
    );
    setPreview(p);
  }

  async function doMigrate() {
    const ok = await run(() =>
      api.migrate(detail.id, Number(targetVersion), detail.revision),
    );
    if (ok) {
      setPreview(null);
      setTargetVersion("");
      await onChanged();
    }
  }

  const paused = detail.status === "waiting_customer";
  const resolved = detail.status === "resolved";

  return (
    <div className="detail">
      <div className="detail-header">
        <h2>
          #{detail.id} {detail.title}
        </h2>
        <span className={`badge status-${detail.status}`}>
          {STATUS_LABEL[detail.status]}
        </span>
        <span className="muted small">
          revision {detail.revision} · 固定策略 {detail.policy.name} v
          {detail.policy.version}
        </span>
      </div>

      <div className="actions">
        {detail.status === "open" && (
          <>
            <button onClick={() => action("wait")}>等待客户（暂停）</button>
            <button className="primary" onClick={() => action("resolve")}>
              解决
            </button>
          </>
        )}
        {paused && (
          <>
            <button className="primary" onClick={() => action("resume")}>
              恢复计时
            </button>
            <button onClick={() => action("resolve")}>解决</button>
          </>
        )}
        {resolved && (
          <span className="muted">
            已于 {fmtDT(detail.resolved_at)} 解决，计时终止。
          </span>
        )}
      </div>

      <div className="deadline-grid">
        <div className="deadline-card accumulated">
          <div className="muted small">已累计有效工作分钟</div>
          <div className="deadline-value">
            {fmtMin(timing.accumulated_minutes)}
          </div>
          <div className="muted small">
            警告 {fmtMin(detail.policy.warn_minutes)} / 升级{" "}
            {fmtMin(detail.policy.escalate_minutes)}
          </div>
        </div>
        {resolved ? (
          <DeadlineCard label="状态" value={null} hint="已解决，无截止时刻" />
        ) : paused ? (
          <>
            <DeadlineCard
              label="警告截止（预计，若现在恢复）"
              value={timing.projected_warn_deadline}
              hint="暂停中，时钟冻结"
            />
            <DeadlineCard
              label="升级截止（预计，若现在恢复）"
              value={timing.projected_escalate_deadline}
              hint="暂停中，时钟冻结"
            />
          </>
        ) : (
          <>
            <DeadlineCard label="警告截止" value={timing.warn_deadline} />
            <DeadlineCard label="升级截止" value={timing.escalate_deadline} />
          </>
        )}
      </div>

      <h3>
        计时时轴 <span className="muted small">截至 {fmtDT(timing.as_of)}</span>
      </h3>
      <Timeline
        counted={timing.counted_intervals}
        paused={timing.paused_intervals}
        asOf={timing.as_of}
      />

      <div className="interval-grid">
        <IntervalTable
          title="已计入区间"
          intervals={timing.counted_intervals}
          empty="尚未计入任何工作时间"
        />
        <IntervalTable
          title="暂停区间"
          intervals={timing.paused_intervals}
          empty="无暂停"
        />
      </div>

      <h3>
        升级记录{" "}
        <span className="muted small">({detail.adjudications.length})</span>
      </h3>
      {detail.adjudications.length === 0 ? (
        <div className="muted small">尚未触发警告或升级</div>
      ) : (
        <ul className="adjudication-list">
          {detail.adjudications.map((a) => (
            <Adjudication key={a.id} a={a} />
          ))}
        </ul>
      )}

      <h3>队列接力</h3>
      <button
        onClick={async () => {
          const version = Number(window.prompt("目标策略版本 ID"));
          if (Number.isInteger(version) && version > 0) {
            const ok = await run(() =>
              api.handoff(detail.id, version, detail.revision),
            );
            if (ok) await onChanged();
          }
        }}
        disabled={detail.status === "resolved"}
      >
        转派并保留累计工作分钟
      </button>
      <pre>{JSON.stringify(detail.handoffs || [], null, 2)}</pre>
      <h3>策略迁移</h3>
      {otherVersions.length === 0 ? (
        <div className="muted small">
          策略 {detail.policy.name} 暂无其他版本可迁移
        </div>
      ) : (
        <div className="migration">
          <div className="row">
            <select
              value={targetVersion}
              onChange={(e) => {
                setTargetVersion(e.target.value);
                setPreview(null);
              }}
            >
              <option value="">选择目标版本…</option>
              {otherVersions.map((v) => (
                <option key={v.policy_version_id} value={v.policy_version_id}>
                  v{v.version}（警告 {fmtMin(v.warn_minutes)} / 升级{" "}
                  {fmtMin(v.escalate_minutes)}）
                </option>
              ))}
            </select>
            <button onClick={doPreview} disabled={!targetVersion}>
              预览差异
            </button>
            {preview && (
              <button className="primary" onClick={doMigrate}>
                确认迁移并保存证据
              </button>
            )}
          </div>
          {preview && (
            <div className="preview">
              <p className="muted small">
                迁移不会改写历史裁决；以下为 {fmtDT(preview.as_of)}{" "}
                时点的旧/新计时差异：
              </p>
              <DiffTable diff={preview} />
            </div>
          )}
        </div>
      )}

      {detail.migrations.length > 0 && (
        <>
          <h4>
            迁移证据{" "}
            <span className="muted small">({detail.migrations.length})</span>
          </h4>
          <ul className="migration-list">
            {detail.migrations.map((m) => (
              <li key={m.id}>
                <div className="row">
                  <span className="mono">{fmtDT(m.migrated_at)}</span>
                  <span>
                    v{m.diff.from.version} → v{m.diff.to.version}
                  </span>
                  <span className="muted small">操作人 {m.actor || "—"}</span>
                </div>
                <DiffTable diff={m.diff} />
              </li>
            ))}
          </ul>
        </>
      )}
    </div>
  );
}
