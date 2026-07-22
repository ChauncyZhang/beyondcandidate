const STATE_CHANGING_METHODS = new Set(["POST", "PUT", "PATCH", "DELETE"]);

function safeString(value, fallback = "") {
  return typeof value === "string" ? value : fallback;
}

function safeRetryAfterSeconds(value) {
  return Number.isInteger(value) && value > 0 && value <= 86_400 ? value : null;
}

export class ApiError extends Error {
  constructor({ status = 0, code = "request_failed", title = "请求失败", detail = "", kind = "request", retryAfterSeconds = null } = {}) {
    super(kind === "unavailable" ? "服务暂时不可用" : "请求未能完成");
    this.name = "ApiError";
    this.status = Number.isInteger(status) ? status : 0;
    this.code = safeString(code, "request_failed");
    this.title = safeString(title, "请求失败");
    this.detail = safeString(detail);
    this.kind = kind;
    this.retryAfterSeconds = safeRetryAfterSeconds(retryAfterSeconds);
  }
}

async function parseResponse(response) {
  if (response.status === 204) return null;
  const contentType = response.headers.get("Content-Type") || "";
  if (!contentType.toLowerCase().includes("json")) return null;
  try {
    return await response.json();
  } catch {
    throw new ApiError({ kind: "unavailable", code: "invalid_response" });
  }
}

export function createApiClient({ fetchImpl = globalThis.fetch } = {}) {
  let csrfToken = null;
  let unauthorizedHandler = null;
  let authEpoch = 0;

  function notifyUnauthorized(requestEpoch) {
    try {
      unauthorizedHandler?.(requestEpoch);
    } catch {
      // Session notifications cannot replace the response's safe ApiError.
    }
  }

  async function parsePayload(response, requestEpoch, notifySession) {
    try {
      return await parseResponse(response);
    } catch (error) {
      if (notifySession) notifyUnauthorized(requestEpoch);
      throw error;
    }
  }

  async function fetchResponse(path, options = {}) {
    const requestEpoch = authEpoch;
    const method = (options.method || "GET").toUpperCase();
    const isLogin = path === "/api/v1/auth/login";
    const refreshesCsrf = isLogin || path === "/api/v1/me";
    const headers = new Headers(options.headers || {});

    if (isLogin) csrfToken = null;
    if (STATE_CHANGING_METHODS.has(method) && !isLogin && csrfToken) {
      headers.set("X-CSRF-Token", csrfToken);
    }
    if (options.idempotencyKey) headers.set("Idempotency-Key", options.idempotencyKey);
    if (options.ifMatch) headers.set("If-Match", options.ifMatch);

    let body = options.body;
    if (body !== undefined && body !== null && !(body instanceof FormData)) {
      headers.set("Content-Type", "application/json");
      body = JSON.stringify(body);
    }

    let response;
    try {
      response = await fetchImpl(path, { method, headers, body, credentials: "include", signal: options.signal });
    } catch (error) {
      if (error?.name === "AbortError") throw error;
      throw new ApiError({ kind: "unavailable", code: "service_unavailable" });
    }

    if (refreshesCsrf && response.ok) {
      const responseCsrf = response.headers.get("X-CSRF-Token");
      if (responseCsrf) csrfToken = responseCsrf;
    } else if (response.status === 401 && requestEpoch === authEpoch) {
      csrfToken = null;
    }

    return { response, requestEpoch, notifySession: response.status === 401 && !refreshesCsrf };
  }

  async function request(path, options = {}) {
    const { response, requestEpoch, notifySession } = await fetchResponse(path, options);
    const payload = await parsePayload(response, requestEpoch, notifySession);
    if (!response.ok) {
      const isProblem = (response.headers.get("Content-Type") || "").toLowerCase().includes("application/problem+json");
      const error = new ApiError({
        status: response.status,
        code: isProblem ? safeString(payload?.code, "request_failed") : "request_failed",
        title: isProblem ? safeString(payload?.title, "请求失败") : "请求失败",
        detail: isProblem ? safeString(payload?.detail) : "",
        kind: response.status >= 500 ? "unavailable" : "request",
        retryAfterSeconds: isProblem ? payload?.retry_after_seconds : null,
      });
      if (notifySession) notifyUnauthorized(requestEpoch);
      throw error;
    }
    return payload;
  }

  async function download(path, options = {}) {
    const { response, requestEpoch, notifySession } = await fetchResponse(path, options);
    if (!response.ok) {
      const payload = await parsePayload(response, requestEpoch, notifySession);
      const isProblem = (response.headers.get("Content-Type") || "").toLowerCase().includes("application/problem+json");
      const error = new ApiError({
        status: response.status,
        code: isProblem ? safeString(payload?.code, "request_failed") : "request_failed",
        title: isProblem ? safeString(payload?.title, "请求失败") : "请求失败",
        detail: isProblem ? safeString(payload?.detail) : "",
        kind: response.status >= 500 ? "unavailable" : "request",
      });
      if (notifySession) notifyUnauthorized(requestEpoch);
      throw error;
    }
    const disposition = response.headers.get("Content-Disposition") || "";
    const encoded = disposition.match(/filename\*=UTF-8''([^;]+)/i)?.[1];
    const quoted = disposition.match(/filename="([^"]+)"/i)?.[1];
    let filename = "resume";
    try {
      filename = encoded ? decodeURIComponent(encoded) : quoted || filename;
    } catch {
      filename = "resume";
    }
    return { blob: await response.blob(), filename };
  }

  return {
    request,
    download,
    async login(credentials) {
      const response = await request("/api/v1/auth/login", { method: "POST", body: credentials });
      return response?.data ?? null;
    },
    async getAuthContext() {
      const response = await request("/api/v1/auth/config");
      const organization = response?.data?.default_organization;
      return organization ? { organization_slug: organization.slug, organization_name: organization.name } : null;
    },
    async getMe() {
      const response = await request("/api/v1/me");
      return response?.data ?? null;
    },
    async logout() {
      return request("/api/v1/auth/logout", { method: "POST" });
    },
    async listDepartments({ signal } = {}) {
      const response = await request("/api/v1/settings/departments", signal ? { signal } : {});
      return Array.isArray(response?.data) ? response.data : [];
    },
    async createDepartment(body) {
      const response = await request("/api/v1/settings/departments", { method: "POST", body });
      return response?.data ?? null;
    },
    async getDepartment(id) {
      const response = await request(`/api/v1/settings/departments/${encodeURIComponent(id)}`);
      return response?.data ?? null;
    },
    async updateDepartment(id, body) {
      const response = await request(`/api/v1/settings/departments/${encodeURIComponent(id)}`, { method: "PATCH", body });
      return response?.data ?? null;
    },
    async listUsers() {
      const response = await request("/api/v1/settings/users");
      return Array.isArray(response?.data) ? response.data : [];
    },
    async inviteUser(body, { idempotencyKey } = {}) {
      const response = await request("/api/v1/settings/users", { method: "POST", body, idempotencyKey });
      return response?.data ?? null;
    },
    async acceptInvitation(body) {
      const response = await request("/api/v1/auth/invitations/accept", { method: "POST", body });
      return response?.data ?? null;
    },
    async changePassword(body) {
      return request("/api/v1/me/password", { method: "POST", body });
    },
    async markNotificationRead(applicationId, version) {
      const response = await request(`/api/v1/notifications/workbench/${encodeURIComponent(applicationId)}/read`, {
        method: "PUT",
        body: { version },
      });
      return response?.data ?? null;
    },
    async getFeishuConfig() { const response = await request("/api/v1/settings/integrations/feishu"); return response?.data ?? null; },
    async saveFeishuConfig(body) { const response = await request("/api/v1/settings/integrations/feishu", { method: "PUT", body }); return response?.data ?? null; },
    async testFeishuConnection() { const response = await request("/api/v1/settings/integrations/feishu/test", { method: "POST" }); return response?.data ?? null; },
    async authorizeFeishuLogin(organizationSlug) { const response = await request("/api/v1/auth/feishu/authorize", { method: "POST", body: { organization_slug: organizationSlug } }); return response?.data ?? null; },
    async authorizeFeishuBinding() { const response = await request("/api/v1/me/integrations/feishu/authorize", { method: "POST" }); return response?.data ?? null; },
    async getFeishuBinding() { const response = await request("/api/v1/me/integrations/feishu"); return response?.data ?? null; },
    async unbindFeishu() { return request("/api/v1/me/integrations/feishu", { method: "DELETE" }); },
    clearCsrf() {
      csrfToken = null;
    },
    getAuthEpoch() {
      return authEpoch;
    },
    advanceAuthEpoch() {
      authEpoch += 1;
      return authEpoch;
    },
    setUnauthorizedHandler(handler) {
      const registeredHandler = typeof handler === "function" ? handler : null;
      unauthorizedHandler = registeredHandler;
      return () => {
        if (unauthorizedHandler === registeredHandler) unauthorizedHandler = null;
      };
    },
  };
}

export const apiClient = createApiClient();
