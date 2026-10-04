import { useState } from "react";
import { api } from "../api.js";
import { fmtDT, fmtMin } from "../format.js";

/** 策略版本管理：发布新版本不影响已固定旧版本的工单。 */
export default function Policies({ policies, onChanged, run }) {
  const [form, setForm] = useState({
    warn: 180,
    esc: 360,
    work: "",
    holiday: "",
  });
  const [target, setTarget] = useState(null);

  function parseIntervals(text) {
    return text
      .split("\n")
      .map((l) => l.trim())
      .filter(Boolean)
      .map((l) => l.split(/[,，\s]+/).filter(Boolean))
      .map(([s, e]) => [s, e]);
  }

  async function submit(e) {
    e.preventDefault();
    if (!target) return;
    const body = {
      warn_minutes: Number(form.warn),
      escalate_minutes: Number(form.esc),
      work_intervals: parseIntervals(form.work),
      holiday_intervals: parseIntervals(form.holiday),
    };
    const ok = await run(() => api.addPolicyVersion(target, body));
    if (ok) await onChanged();
  }

  return (
    <div className="policies">
      <h2>策略与版本</h2>
      <p className="muted small">
        工单创建时固定当时的最新版本；发布新版本不会改变既有工单时限，需在工单详情页显式迁移。
      </p>
      {policies.map((p) => (
        <div key={p.policy_id} className="policy-card">
          <h3>{p.name}</h3>
          <table>
            <thead>
              <tr>
                <th>版本</th>
                <th>警告阈值</th>
                <th>升级阈值</th>
                <th>工作区间</th>
                <th>假日切口</th>
                <th>发布时间</th>
              </tr>
            </thead>
            <tbody>
              {p.versions.map((v) => (
                <tr key={v.policy_version_id}>
                  <td>v{v.version}</td>
                  <td>{fmtMin(v.warn_minutes)}</td>
                  <td>{fmtMin(v.escalate_minutes)}</td>
                  <td>{v.work_intervals.length} 段</td>
                  <td>{v.holiday_intervals.length} 段</td>
                  <td className="mono">{fmtDT(v.created_at)}</td>
                </tr>
              ))}
            </tbody>
          </table>

          <details>
            <summary>发布新版本</summary>
            <form className="version-form" onSubmit={submit}>
              <label>
                警告阈值（分钟）
                <input
                  type="number"
                  value={form.warn}
                  onChange={(e) => setForm({ ...form, warn: e.target.value })}
                />
              </label>
              <label>
                升级阈值（分钟）
                <input
                  type="number"
                  value={form.esc}
                  onChange={(e) => setForm({ ...form, esc: e.target.value })}
                />
              </label>
              <label>
                工作区间（每行一段：开始, 结束，UTC ISO）
                <textarea
                  rows="4"
                  placeholder={"2026-10-05T09:00:00Z, 2026-10-05T17:00:00Z"}
                  value={form.work}
                  onChange={(e) => setForm({ ...form, work: e.target.value })}
                />
              </label>
              <label>
                假日切口（每行一段，可空）
                <textarea
                  rows="2"
                  placeholder={"2026-10-01T00:00:00Z, 2026-10-03T00:00:00Z"}
                  value={form.holiday}
                  onChange={(e) =>
                    setForm({ ...form, holiday: e.target.value })
                  }
                />
              </label>
              <button
                type="submit"
                className="primary"
                onClick={() => setTarget(p.policy_id)}
              >
                发布新版本
              </button>
            </form>
          </details>
        </div>
      ))}
    </div>
  );
}
