import { apiClient } from "./apiClient.js";

const ERROR_MESSAGES = {
  api_key_required: "启用模型前请先保存 API Key。",
  authentication_required: "登录状态已失效，请重新登录。",
  csrf_validation_failed: "当前会话已失效，请刷新页面后重试。",
  idempotency_conflict: "请求状态已变化，请刷新后重试。",
  llm_key_unavailable: "已保存的 API Key 暂时不可用，请联系系统管理员。",
  llm_not_configured: "请先保存模型配置和 API Key。",
  persistence_failed: "设置暂时无法保存，请稍后重试。",
  precondition_required: "配置版本已失效，请刷新后重试。",
  provider_or_model_not_allowed: "所选 Provider 或模型不可用，请重新选择。",
  provider_already_exists: "该 Provider 标识已存在，请换一个标识。",
  provider_address_forbidden: "Provider 地址不能使用内网、回环或保留地址。",
  provider_port_forbidden: "Provider 地址只能使用标准 HTTPS 端口。",
  provider_url_forbidden: "Provider 地址格式不安全，请填写标准 HTTPS Base URL。",
  resource_not_found: "当前账号无权访问此设置。",
  service_unavailable: "服务暂时不可用，请稍后重试。",
  validation_failed: "设置内容无效，请检查后重试。",
};

let fallbackKeySequence = 0;

function createUniqueIdempotencyKey() {
  if (globalThis.crypto?.randomUUID) return globalThis.crypto.randomUUID();
  fallbackKeySequence += 1;
  return `llm-${Date.now()}-${fallbackKeySequence}`;
}

function sanitizeConfig(value) {
  if (!value || typeof value !== "object") return null;
  const { api_key: ignoredApiKey, ...config } = value;
  return config;
}

function draftFromConfig(config) {
  return {
    provider_id: typeof config?.provider_id === "string" ? config.provider_id : "",
    model: typeof config?.model === "string" ? config.model : "",
    enabled: config?.enabled === true,
  };
}

function draftChanged(draft, config) {
  const saved = draftFromConfig(config);
  return draft.provider_id !== saved.provider_id
    || draft.model !== saved.model
    || draft.enabled !== saved.enabled;
}

export function getLlmSettingsErrorMessage(error) {
  return ERROR_MESSAGES[error?.code] || "操作未完成，请稍后重试。";
}

export function getTestDisabledReason(state) {
  if (state.status === "loading" || state.status === "saving" || state.status === "testing" || state.status === "adding_provider") return "请等待当前操作完成。";
  if (state.dirty) return "请先保存当前修改，再测试已保存的配置。";
  if (!state.config?.configured) return "请先保存模型配置。";
  if (state.config.key_configured !== true) return "请先保存 API Key。";
  return "";
}

export function releaseLlmSettingsSubscription(controller, unsubscribe) {
  unsubscribe();
  controller.cancelKeyReplacement();
}

export function createLlmSettingsController({
  client = apiClient,
  createIdempotencyKey = createUniqueIdempotencyKey,
  now = () => new Date(),
} = {}) {
  let disposed = false;
  let requestSequence = 0;
  let replacementKeyRevision = 0;
  const listeners = new Set();
  let state = {
    status: "idle",
    config: null,
    draft: draftFromConfig(null),
    replacingKey: false,
    replacementKey: "",
    dirty: false,
    error: "",
    message: "",
  };

  function publish(patch) {
    if (disposed) return;
    state = { ...state, ...patch };
    listeners.forEach((listener) => listener(state));
  }

  function publishReplacementState(patch) {
    replacementKeyRevision += 1;
    publish(patch);
  }

  function clearSubmittedReplacementKey(submittedRevision) {
    if (submittedRevision === null || submittedRevision !== replacementKeyRevision) return;
    publishReplacementState({ replacingKey: false, replacementKey: "" });
  }

  function installConfig(rawConfig, extra = {}) {
    const config = sanitizeConfig(rawConfig);
    publishReplacementState({
      status: "ready",
      config,
      draft: draftFromConfig(config),
      replacingKey: false,
      replacementKey: "",
      dirty: false,
      error: "",
      message: "",
      ...extra,
    });
  }

  async function load({ preserveMessage = false } = {}) {
    const requestId = ++requestSequence;
    publish({ status: "loading", error: "", message: preserveMessage ? state.message : "" });
    try {
      const response = await client.request("/api/v1/settings/llm");
      if (disposed || requestId !== requestSequence) return;
      installConfig(response?.data);
    } catch (error) {
      if (disposed || requestId !== requestSequence) return;
      publish({ status: "error", error: getLlmSettingsErrorMessage(error) });
    }
  }

  function updateDraft(patch) {
    if (disposed || state.status === "loading" || state.status === "saving" || state.status === "testing") return;
    const draft = { ...state.draft, ...patch };
    publish({ draft, dirty: draftChanged(draft, state.config) || Boolean(state.replacementKey), error: "", message: "" });
  }

  function startKeyReplacement() {
    publishReplacementState({ replacingKey: true, replacementKey: "", error: "", message: "" });
  }

  function setReplacementKey(replacementKey) {
    if (!state.replacingKey) return;
    publishReplacementState({
      replacementKey,
      dirty: draftChanged(state.draft, state.config) || replacementKey.length > 0,
      error: "",
      message: "",
    });
  }

  function cancelKeyReplacement() {
    publishReplacementState({
      replacingKey: false,
      replacementKey: "",
      dirty: draftChanged(state.draft, state.config),
      error: "",
      message: "",
    });
  }

  function discardDraft() {
    if (!state.config) return;
    installConfig(state.config);
  }

  async function save() {
    if (!state.config || state.status === "saving" || state.status === "testing") return false;
    const requestId = ++requestSequence;
    const submittedReplacementRevision = state.replacingKey ? replacementKeyRevision : null;
    const body = {
      provider_id: state.draft.provider_id,
      model: state.draft.model,
      enabled: state.draft.enabled,
      allowed_job_ids: Array.isArray(state.config.allowed_job_ids) ? [...state.config.allowed_job_ids] : [],
    };
    if (state.replacingKey && state.replacementKey) body.api_key = state.replacementKey;
    publish({ status: "saving", error: "", message: "" });
    try {
      const response = await client.request("/api/v1/settings/llm", {
        method: "PUT",
        body,
        ifMatch: `"${state.config.version}"`,
        idempotencyKey: createIdempotencyKey(),
      });
      if (disposed) return false;
      if (requestId !== requestSequence) {
        clearSubmittedReplacementKey(submittedReplacementRevision);
        return false;
      }
      installConfig(response?.data, { message: "LLM 设置已保存。" });
      return true;
    } catch (error) {
      if (disposed) return false;
      if (requestId !== requestSequence) {
        clearSubmittedReplacementKey(submittedReplacementRevision);
        return false;
      }
      if (error?.code === "resource_version_conflict") {
        publishReplacementState({ replacingKey: false, replacementKey: "", dirty: false });
        await load();
        if (!disposed && state.status === "ready") {
          publish({ message: "配置已被其他管理员更新。已加载最新设置，请检查后重试。" });
        }
        return false;
      }
      publishReplacementState({
        status: "error",
        replacingKey: false,
        replacementKey: "",
        error: getLlmSettingsErrorMessage(error),
      });
      return false;
    }
  }

  async function testConnection() {
    if (getTestDisabledReason(state)) return false;
    const requestId = ++requestSequence;
    publish({ status: "testing", error: "", message: "" });
    try {
      const response = await client.request("/api/v1/settings/llm/test", {
        method: "POST",
        idempotencyKey: createIdempotencyKey(),
      });
      if (disposed || requestId !== requestSequence) return false;
      const result = response?.data || {};
      installConfig({
        ...state.config,
        last_test_status: result.status || null,
        last_test_error_code: result.safe_error_code || null,
        last_test_latency_ms: Number.isFinite(result.latency_ms) ? result.latency_ms : null,
        last_tested_at: now().toISOString(),
      }, { message: result.status === "succeeded" ? "连接测试成功。" : "连接测试未通过。" });
      return result.status === "succeeded";
    } catch (error) {
      if (disposed || requestId !== requestSequence) return false;
      const safeError = getLlmSettingsErrorMessage(error);
      await load();
      if (!disposed && state.status === "ready") publish({ status: "error", error: safeError });
      return false;
    }
  }

  async function createProvider(provider) {
    if (!state.config || state.dirty || ["loading", "saving", "testing", "adding_provider"].includes(state.status)) return false;
    const requestId = ++requestSequence;
    publish({ status: "adding_provider", error: "", message: "" });
    try {
      await client.request("/api/v1/settings/llm/providers", {
        method: "POST",
        body: provider,
        idempotencyKey: createIdempotencyKey(),
      });
      if (disposed || requestId !== requestSequence) return false;
      await load();
      if (!disposed && state.status === "ready") publish({ message: "Provider 已添加，可以继续配置模型和 API Key。" });
      return !disposed && state.status === "ready";
    } catch (error) {
      if (disposed || requestId !== requestSequence) return false;
      publish({ status: "error", error: getLlmSettingsErrorMessage(error) });
      return false;
    }
  }

  return {
    getState: () => state,
    subscribe(listener) {
      listeners.add(listener);
      return () => listeners.delete(listener);
    },
    load,
    updateDraft,
    startKeyReplacement,
    setReplacementKey,
    cancelKeyReplacement,
    discardDraft,
    save,
    testConnection,
    createProvider,
    dispose() {
      disposed = true;
      requestSequence += 1;
      replacementKeyRevision += 1;
      if (state.replacingKey || state.replacementKey) {
        state = { ...state, replacingKey: false, replacementKey: "" };
      }
      listeners.clear();
    },
  };
}
