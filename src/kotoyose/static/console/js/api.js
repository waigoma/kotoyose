// Thin wrapper over the existing admin API. Sends same-origin session cookies and CSRF header.

export class ApiError extends Error {
  constructor(status, message) {
    super(message);
    this.status = status;
  }
}

function enc(value) {
  return encodeURIComponent(value);
}

function describeDetail(status, data) {
  const detail = data && data.detail;
  if (typeof detail === "string") return detail;
  if (Array.isArray(detail)) {
    return detail
      .map((item) => {
        const field = (item.loc || []).slice(1).join(".");
        return field ? `${field}: ${item.msg}` : item.msg;
      })
      .join("; ");
  }
  if (data && data.error && typeof data.error.message === "string") {
    return data.error.message;
  }
  return `Request failed (${status})`;
}

function redirectToLogin() {
  const next = `${location.pathname}${location.search}${location.hash}`;
  location.assign(`/admin/login?next=${encodeURIComponent(next)}`);
}

async function request(method, path, body, options = {}) {
  const init = {
    method,
    credentials: "same-origin",
    headers: { Accept: "application/json", "X-Kotoyose-Admin": "1" },
  };
  if (options.signal) init.signal = options.signal;
  if (body !== undefined) {
    init.headers["Content-Type"] = "application/json";
    init.body = JSON.stringify(body);
  }
  let response;
  try {
    response = await fetch(path, init);
  } catch (cause) {
    if (cause && cause.name === "AbortError") throw cause;
    throw new ApiError(0, "Could not reach the server.");
  }
  if (response.status === 401) {
    redirectToLogin();
    throw new ApiError(401, "Authentication required");
  }
  const text = await response.text();
  let data = null;
  if (text) {
    try {
      data = JSON.parse(text);
    } catch (cause) {
      data = null;
    }
  }
  if (!response.ok) throw new ApiError(response.status, describeDetail(response.status, data));
  return data;
}

export const api = {
  session: () => request("GET", "/admin/session"),
  logout: () => request("POST", "/admin/logout", {}),
  reloadConfig: () => request("POST", "/admin/reload"),
  browse: (path) => request("GET", path ? `/admin/browse?path=${enc(path)}` : "/admin/browse"),
  settings: () => request("GET", "/admin/settings"),
  saveSettings: (body) => request("PUT", "/admin/settings", body),
  config: () => request("GET", "/admin/config"),
  saveProviderOrder: (providerOrder) => request("PUT", "/admin/settings/provider-order", {
    provider_order: providerOrder,
  }),
  tools: () => request("GET", "/admin/tools"),
  installTool: (profile) => request("POST", `/admin/tools/${enc(profile)}/install`, {}),
  machine: () => request("GET", "/admin/machine"),
  poolState: () => request("GET", "/admin/pools/state"),
  apiKeys: () => request("GET", "/admin/api-keys"),
  createApiKey: (name, client) => request("POST", "/admin/api-keys", { name, client }),
  revokeApiKey: (name) => request("POST", `/admin/api-keys/${enc(name)}/revoke`, {}),
  deleteApiKey: (name) => request("DELETE", `/admin/api-keys/${enc(name)}`),
  clients: () => request("GET", "/admin/clients"),
  putClient: (name, body) => request("PUT", `/admin/clients/${enc(name)}`, body),
  deleteClient: (name) => request("DELETE", `/admin/clients/${enc(name)}`),
  interactions: (limit) => request("GET", `/admin/interactions?limit=${Number(limit)}`),
  interaction: (id) => request("GET", `/admin/interactions/${enc(id)}`),
  interactionEvents: (id, afterSeq = -1) =>
    request("GET", `/admin/interactions/${enc(id)}/events?after_seq=${Number(afterSeq)}`),
  sessions: (limit = 100, offset = 0) =>
    request("GET", `/admin/sessions?limit=${Number(limit)}&offset=${Number(offset)}`),
  sessionDetail(conversationKey, { runtimeSessionId = null, provider = null } = {}) {
    const params = new URLSearchParams();
    if (runtimeSessionId) params.set("runtime_session_id", runtimeSessionId);
    if (provider) params.set("provider", provider);
    const suffix = params.toString() ? `?${params.toString()}` : "";
    return request("GET", `/admin/sessions/${enc(conversationKey)}${suffix}`);
  },
  stats: () => request("GET", "/admin/stats"),
  hourlyStats: (hours = 24) => request("GET", `/admin/stats/hourly?hours=${Number(hours)}`),
  providerModels(provider, params = {}) {
    const query = new URLSearchParams(
      Object.entries(params).filter(([, value]) => value),
    ).toString();
    const suffix = query ? `?${query}` : "";
    return request("GET", `/admin/providers/${enc(provider)}/models${suffix}`);
  },
  createConnection: (id, body) => request("POST", `/admin/connections/${enc(id)}`, body),
  updateConnection: (id, body) => request("PUT", `/admin/connections/${enc(id)}`, body),
  deleteConnection: (id) => request("DELETE", `/admin/connections/${enc(id)}`),
  connections: () => request("GET", "/admin/connections"),
  probeConnection: (id) => request("POST", `/admin/connections/${enc(id)}/probe`, {}),
  refreshConnection: (id) => request("POST", `/admin/connections/${enc(id)}/refresh`, {}),
  startConnectionLogin: (id) => request("POST", `/admin/connections/${enc(id)}/login`, {}),
  pollConnectionLogin: (id, sessionId) => request("GET", `/admin/connections/${enc(id)}/login/${enc(sessionId)}`),
  cancelConnectionLogin: (id, sessionId) => request("DELETE", `/admin/connections/${enc(id)}/login/${enc(sessionId)}`),
  startClaudeLogin: (id) => request("POST", `/admin/connections/${enc(id)}/claude-login`, {}),
  pollClaudeLogin: (id, sessionId) => request("GET", `/admin/connections/${enc(id)}/claude-login/${enc(sessionId)}`),
  submitClaudeLoginCode: (id, sessionId, code) =>
    request("POST", `/admin/connections/${enc(id)}/claude-login/${enc(sessionId)}/code`, { code }),
  cancelClaudeLogin: (id, sessionId) => request("DELETE", `/admin/connections/${enc(id)}/claude-login/${enc(sessionId)}`),
  startClaudeSetupToken: (id) => request("POST", `/admin/connections/${enc(id)}/claude-setup-token`, {}),
  pollClaudeSetupToken: (id, sessionId) => request("GET", `/admin/connections/${enc(id)}/claude-setup-token/${enc(sessionId)}`),
  submitClaudeSetupTokenCode: (id, sessionId, code) =>
    request("POST", `/admin/connections/${enc(id)}/claude-setup-token/${enc(sessionId)}/code`, { code }),
  cancelClaudeSetupToken: (id, sessionId) => request("DELETE", `/admin/connections/${enc(id)}/claude-setup-token/${enc(sessionId)}`),
  startCliLogin: (id) => request("POST", `/admin/connections/${enc(id)}/cli-login`, {}),
  pollCliLogin: (id, sessionId) => request("GET", `/admin/connections/${enc(id)}/cli-login/${enc(sessionId)}`),
  submitCliLoginCode: (id, sessionId, code) =>
    request("POST", `/admin/connections/${enc(id)}/cli-login/${enc(sessionId)}/code`, { code }),
  cancelCliLogin: (id, sessionId) => request("DELETE", `/admin/connections/${enc(id)}/cli-login/${enc(sessionId)}`),
  createRoute: (name, body) => request("POST", `/admin/routes/${enc(name)}`, body),
  updateRoute: (name, body) => request("PUT", `/admin/routes/${enc(name)}`, body),
  deleteRoute: (name) => request("DELETE", `/admin/routes/${enc(name)}`),
  renameRoute: (name, newName) => request("POST", `/admin/routes/${enc(name)}/rename`, { new_name: newName }),
  ackRoute: (name) => request("POST", `/admin/routes/${enc(name)}/ack`, {}),
  revokeRouteAck: (name) => request("DELETE", `/admin/routes/${enc(name)}/ack`),
  createPool: (name, body) => request("POST", `/admin/pools/${enc(name)}`, body),
  updatePool: (name, body) => request("PUT", `/admin/pools/${enc(name)}`, body),
  deletePool: (name) => request("DELETE", `/admin/pools/${enc(name)}`),
  createModel: (modelId, preset) => request("POST", "/admin/models", { model_id: modelId, preset }),
  updateModel: (modelId, preset) => request("PUT", `/admin/models/${enc(modelId)}`, preset),
  renameModel: (modelId, newName, preset) => request("POST", `/admin/models/${enc(modelId)}/rename`, { new_name: newName, preset }),
  deleteModel: (modelId) => request("DELETE", `/admin/models/${enc(modelId)}`),
  nativeOptions: () => request("GET", "/admin/native/options"),
  createPromptFile: (name, body) => request("POST", `/admin/prompt-files/${enc(name)}`, body),
  updatePromptFile: (name, body) => request("PUT", `/admin/prompt-files/${enc(name)}`, body),
  renamePromptFile: (name, newName, body) => request("POST", `/admin/prompt-files/${enc(name)}/rename`, { new_name: newName, ...body }),
  deletePromptFile: (name) => request("DELETE", `/admin/prompt-files/${enc(name)}`),
  promptFileContent: (name) => request("GET", `/admin/prompt-files/${enc(name)}/content`),
  updatePromptFileContent: (name, content) => request("PUT", `/admin/prompt-files/${enc(name)}/content`, { content }),
  createSkill: (name, body) => request("POST", `/admin/skills/${enc(name)}`, body),
  updateSkill: (name, body) => request("PUT", `/admin/skills/${enc(name)}`, body),
  deleteSkill: (name) => request("DELETE", `/admin/skills/${enc(name)}`),
  createMcp: (name, body) => request("POST", `/admin/mcps/${enc(name)}`, body),
  updateMcp: (name, body) => request("PUT", `/admin/mcps/${enc(name)}`, body),
  renameMcp: (name, newName, body) => request("POST", `/admin/mcps/${enc(name)}/rename`, { new_name: newName, ...body }),
  deleteMcp: (name) => request("DELETE", `/admin/mcps/${enc(name)}`),
  probeMcp: (name, options = {}) => request("POST", `/admin/mcps/${enc(name)}/probe`, {}, options),
  probeMcpDraft: (server, options = {}) => request("POST", "/admin/mcp-probe", { server }, options),
  secrets: () => request("GET", "/admin/secrets"),
  putSecret: (name, value, purpose = "") => request("PUT", `/admin/secrets/${enc(name)}`, { value, purpose }),
  deleteSecret: (name) => request("DELETE", `/admin/secrets/${enc(name)}`),
};
