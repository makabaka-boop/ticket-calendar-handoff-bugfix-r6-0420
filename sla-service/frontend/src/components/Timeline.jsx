import { fmtDT } from "../format.js";

/** 计入区间（绿）与暂停区间（灰）的时间轴可视化。 */
export default function Timeline({ counted, paused, asOf }) {
  const segs = [
    ...counted.map(([s, e]) => ({ s, e, type: "counted" })),
    ...paused.map(([s, e]) => ({ s, e: e || asOf, type: "paused", open: !e })),
  ].filter((x) => x.s && x.e);
  if (!segs.length) return <div className="muted small">暂无计时段</div>;

  const min = Math.min(...segs.map((x) => Date.parse(x.s)));
  const max = Math.max(...segs.map((x) => Date.parse(x.e)), Date.parse(asOf));
  const span = Math.max(max - min, 1);

  return (
    <div className="timeline">
      <div className="timeline-track">
        {segs.map((x, i) => {
          const left = ((Date.parse(x.s) - min) / span) * 100;
          const width = Math.max(
            ((Date.parse(x.e) - Date.parse(x.s)) / span) * 100,
            0.6,
          );
          return (
            <span
              key={i}
              className={`seg ${x.type}${x.open ? " open" : ""}`}
              style={{ left: `${left}%`, width: `${width}%` }}
              title={`${x.type === "counted" ? "计入" : "暂停"} ${fmtDT(x.s)} ~ ${x.open ? "至今" : fmtDT(x.e)}`}
            />
          );
        })}
        <span
          className="now-marker"
          style={{
            left: `${Math.min(((Date.parse(asOf) - min) / span) * 100, 100)}%`,
          }}
          title={`现在 ${fmtDT(asOf)}`}
        />
      </div>
      <div className="timeline-axis muted small">
        <span>{fmtDT(new Date(min).toISOString())}</span>
        <span>{fmtDT(new Date(max).toISOString())}</span>
      </div>
      <div className="legend small">
        <span>
          <i className="dot counted" /> 已计入
        </span>
        <span>
          <i className="dot paused" /> 暂停
        </span>
        <span>
          <i className="dot now" /> 现在
        </span>
      </div>
    </div>
  );
}
