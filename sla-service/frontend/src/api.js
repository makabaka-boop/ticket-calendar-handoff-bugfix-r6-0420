const base = "/api";

async function req(path, options = {}) {
  const res = await fetch(base + path, {
    headers: { "Content-Type": "application/json" },
    ...options,
  });
  if (!res.ok) {
    const body = await res.json().catch(() => ({}));
    const err = new Error(body.error || `HTTP ${res.status}`);
    err.status = res.status;
    err.body = body;
    throw err;
  }
  return res.json();
}

export const api = {
  handoff: (id, to_version_id, expected_revision) =>
    fetch(`/api/tickets/${id}/handoff`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ to_version_id, expected_revision }),
    }).then(async (r) => {
      const data = await r.json();
      if (!r.ok) throw new Error(data.error);
      return data;
    }),
  meta: () => req("/meta"),
  listTickets: () => req("/tickets"),
  getTicket: (id) => req(`/tickets/${id}`),
  createTicket: (title) =>
    req("/tickets", { method: "POST", body: JSON.stringify({ title }) }),
  setStatus: (id, action, expected_revision) =>
    req(`/tickets/${id}/status`, {
      method: "POST",
      body: JSON.stringify({ action, expected_revision }),
    }),
  listPolicies: () => req("/policies"),
  addPolicyVersion: (policyId, body) =>
    req(`/policies/${policyId}/versions`, {
      method: "POST",
      body: JSON.stringify(body),
    }),
  migrationPreview: (id, toVersionId) =>
    req(`/tickets/${id}/migration-preview?to_version_id=${toVersionId}`),
  migrate: (id, to_version_id, expected_revision) =>
    req(`/tickets/${id}/migrate`, {
      method: "POST",
      body: JSON.stringify({
        to_version_id,
        expected_revision,
        actor: "web-user",
      }),
    }),
  scan: () => req("/scan", { method: "POST" }),
};
