export function fmtDT(iso) {
  if (!iso) return "—";
  const d = new Date(iso);
  const p = (n) => String(n).padStart(2, "0");
  return `${d.getUTCFullYear()}-${p(d.getUTCMonth() + 1)}-${p(d.getUTCDate())} ${p(d.getUTCHours())}:${p(d.getUTCMinutes())}Z`;
}

export function fmtMin(m) {
  if (m == null) return "—";
  if (m >= 120) return `${(m / 60).toFixed(1)} 小时`;
  return `${Math.round(m)} 分钟`;
}

export function fmtShift(seconds) {
  if (seconds == null) return "—";
  const sign = seconds > 0 ? "+" : seconds < 0 ? "−" : "";
  const abs = Math.abs(seconds);
  if (abs >= 3600) return `${sign}${(abs / 3600).toFixed(1)} 小时`;
  return `${sign}${Math.round(abs / 60)} 分钟`;
}

export const STATUS_LABEL = {
  open: "处理中",
  waiting_customer: "等待客户",
  resolved: "已解决",
};
