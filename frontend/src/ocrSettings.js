import { apiClient } from "./apiClient.js";

const ERROR_MESSAGES = {
  api_key_required: "启用 OCR 前请先保存 API Key。",
  authentication_required: "登录状态已失效，请重新登录。",
  csrf_validation_failed: "当前会话已失效，请刷新页面后重试。",
  idempotency_conflict: "请求状态已变化，请刷新后重试。",
  ocr_key_unavailable: "已保存的 OCR API Key 暂时不可用，请联系系统管理员。",
  ocr_not_configured: "请先保存 OCR 服务配置和 API Key。",
  persistence_failed: "OCR 设置暂时无法保存，请稍后重试。",
  precondition_required: "配置版本已失效，请刷新后重试。",
  provider_address_forbidden: "Base URL 不能使用内网、回环或保留地址。",
  provider_auth_failed: "OCR 服务拒绝了 API Key，请检查 Key、地域和模型访问权限。",
  provider_model_not_found: "OCR 模型或服务不存在，请检查模型名称和 Base URL。",
  provider_port_forbidden: "Base URL 只能使用服务端允许的 HTTPS 端口。",
  provider_quota_or_rate_limited: "OCR 服务额度不足或请求过于频繁，请检查服务商额度后重试。",
  provider_request_rejected: "OCR 服务拒绝了测试请求，请检查模型是否支持图片识别。",
  provider_response_invalid: "OCR 服务返回了无法识别的响应，请检查接口兼容性。",
  provider_response_too_large: "OCR 服务返回内容过大，请调整模型或服务配置。",
  provider_unavailable: "OCR 服务当前无法连接，请稍后重试。",
  provider_url_forbidden: "Base URL 格式不安全，请填写标准 HTTPS 地址。",
  resource_not_found: "当前账号无权访问此设置。",
  service_unavailable: "OCR 服务暂时不可用，请稍后重试。",
  validation_failed: "OCR 设置内容无效，请检查后重试。",
};

let fallbackKeySequence = 0;

function createUniqueIdempotencyKey() {
  if (globalThis.crypto?.randomUUID) return globalThis.crypto.randomUUID();
  fallbackKeySequence += 1;
  return `ocr-${Date.now()}-${fallbackKeySequence}`;
}

function sanitizeConfig(value) {
  if (!value || typeof value !== "object") return null;
  const { api_key: ignoredApiKey, ...config } = value;
  return config;
}

function draftFromConfig(config) {
  return {
    provider_id: typeof config?.provider_id === "string" ? config.provider_id : "",
    base_url: typeof config?.base_url === "string" ? config.base_url : "",
    model: typeof config?.model === "string" ? config.model : "",
    enabled: config?.enabled === true,
  };
}

function draftChanged(draft, config) {
  const saved = draftFromConfig(config);
  return Object.keys(saved).some((key) => draft[key] !== saved[key]);
}

export function getOcrSettingsErrorMessage(error) {
  return ERROR_MESSAGES[error?.code] || "OCR 操作未完成，请稍后重试。";
}

export function getOcrTestDisabledReason(state) {
  if (["loading", "saving", "testing"].includes(state.status)) return "请等待当前操作完成。";
  if (state.dirty) return "请先保存当前修改，再测试已保存的配置。";
  if (!state.config?.configured) return "请先保存 OCR 服务配置。";
  if (state.config.key_configured !== true) return "请先保存 API Key。";
  return "";
}

export function releaseOcrSettingsSubscription(controller, unsubscribe) {
  unsubscribe();
  controller.cancelKeyReplacement();
}

export function createOcrSettingsController({
  client = apiClient,
  createIdempotencyKey = createUniqueIdempotencyKey,
} = {}) {
  let disposed = false;
  let requestSequence = 0;
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

  function installConfig(rawConfig, extra = {}) {
    const config = sanitizeConfig(rawConfig);
    publish({
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
      const response = await client.request("/api/v1/settings/ocr");
      if (disposed || requestId !== requestSequence) return false;
      installConfig(response?.data, { message: preserveMessage ? state.message : "" });
      return true;
    } catch (error) {
      if (disposed || requestId !== requestSequence) return false;
      publish({ status: "error", error: getOcrSettingsErrorMessage(error) });
      return false;
    }
  }

  function updateDraft(patch) {
    if (["loading", "saving", "testing"].includes(state.status)) return;
    const draft = { ...state.draft, ...patch };
    publish({ draft, dirty: draftChanged(draft, state.config) || Boolean(state.replacementKey), error: "", message: "" });
  }

  function startKeyReplacement() {
    publish({ replacingKey: true, replacementKey: "", error: "", message: "" });
  }

  function setReplacementKey(replacementKey) {
    if (!state.replacingKey) return;
    publish({ replacementKey, dirty: draftChanged(state.draft, state.config) || replacementKey.length > 0, error: "", message: "" });
  }

  function cancelKeyReplacement() {
    publish({ replacingKey: false, replacementKey: "", dirty: draftChanged(state.draft, state.config), error: "", message: "" });
  }

  function discardDraft() {
    if (state.config) installConfig(state.config);
  }

  async function save() {
    if (!state.config || ["saving", "testing"].includes(state.status)) return false;
    const requestId = ++requestSequence;
    const body = {
      provider_id: state.draft.provider_id.trim().toLowerCase(),
      base_url: state.draft.base_url.trim(),
      model: state.draft.model.trim(),
      enabled: state.draft.enabled,
    };
    if (state.replacingKey && state.replacementKey) body.api_key = state.replacementKey;
    publish({ status: "saving", error: "", message: "" });
    try {
      const response = await client.request("/api/v1/settings/ocr", {
        method: "PUT",
        body,
        ifMatch: `"${state.config.version}"`,
        idempotencyKey: createIdempotencyKey(),
      });
      if (disposed || requestId !== requestSequence) return false;
      installConfig(response?.data, { message: "OCR 设置已保存。" });
      return true;
    } catch (error) {
      if (disposed || requestId !== requestSequence) return false;
      if (error?.code === "resource_version_conflict") {
        publish({ replacingKey: false, replacementKey: "", dirty: false });
        await load();
        if (!disposed && state.status === "ready") publish({ message: "OCR 配置已被其他管理员更新。已加载最新设置，请检查后重试。" });
        return false;
      }
      publish({ status: "error", replacingKey: false, replacementKey: "", error: getOcrSettingsErrorMessage(error) });
      return false;
    }
  }

  async function testConnection() {
    if (getOcrTestDisabledReason(state)) return false;
    const requestId = ++requestSequence;
    publish({ status: "testing", error: "", message: "" });
    try {
      await client.request("/api/v1/settings/ocr/test", { method: "POST", idempotencyKey: createIdempotencyKey() });
      if (disposed || requestId !== requestSequence) return false;
      const loaded = await load();
      if (loaded) publish({ message: state.config?.last_test_status === "succeeded" ? "OCR 连接测试成功。" : "OCR 连接测试未通过。" });
      return loaded && state.config?.last_test_status === "succeeded";
    } catch (error) {
      if (disposed || requestId !== requestSequence) return false;
      const safeError = getOcrSettingsErrorMessage(error);
      await load();
      if (!disposed && state.config) publish({ status: "error", error: safeError });
      return false;
    }
  }

  return {
    getState: () => state,
    subscribe(listener) { listeners.add(listener); return () => listeners.delete(listener); },
    load,
    updateDraft,
    startKeyReplacement,
    setReplacementKey,
    cancelKeyReplacement,
    discardDraft,
    save,
    testConnection,
    dispose() {
      disposed = true;
      requestSequence += 1;
      state = { ...state, replacingKey: false, replacementKey: "" };
      listeners.clear();
    },
  };
}
