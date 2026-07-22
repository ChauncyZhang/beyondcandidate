function safeString(value) {
  return typeof value === "string" ? value : "";
}

export function buildFeishuConfigPayload(draft) {
  const payload = {
    app_id: safeString(draft?.app_id).trim(),
    redirect_uri: safeString(draft?.redirect_uri).trim(),
    calendar_id: safeString(draft?.calendar_id).trim() || "primary",
    enabled: draft?.enabled === true,
  };
  for (const key of ["app_secret", "verification_token", "encrypt_key"]) {
    const value = safeString(draft?.[key]);
    if (value) payload[key] = value;
  }
  return payload;
}

export function normalizeFeishuConfig(value = {}) {
  return {
    configured: value.configured === true,
    appId: safeString(value.app_id),
    redirectUri: safeString(value.redirect_uri),
    calendarId: safeString(value.calendar_id),
    enabled: value.enabled === true,
    appSecretConfigured: value.app_secret_configured === true,
    verificationTokenConfigured: value.verification_token_configured === true,
    encryptKeyConfigured: value.encrypt_key_configured === true,
    version: Number.isInteger(value.version) ? value.version : 0,
    lastTestStatus: safeString(value.last_test_status),
    lastTestedAt: safeString(value.last_tested_at),
    lastTestErrorCode: safeString(value.last_test_error_code),
  };
}

export function normalizeFeishuBinding(value = {}) {
  if (value.bound !== true) return { bound: false, unionId: "", openId: "" };
  return { bound: true, unionId: safeString(value.union_id), openId: safeString(value.open_id) };
}

export function getFeishuLoginErrorMessage(error) {
  if (error?.code === "feishu_disabled") {
    return "当前组织尚未启用飞书登录，请联系管理员前往“设置 → 飞书集成”完成配置并启用。";
  }
  if (error?.code === "feishu_account_not_invited_or_bound") {
    return "当前飞书账号尚未绑定。已有账号请先使用密码登录，再到“个人设置 → 飞书账号”完成绑定；新用户请联系管理员邀请。";
  }
  return "飞书登录服务暂时不可用，请稍后重试。";
}

export function getFeishuCallbackErrorCode(search = globalThis.location?.search || "") {
  return safeString(new URLSearchParams(search).get("feishu_error")).trim();
}

export function getFeishuConfigErrorMessage(error) {
  if (error?.status === 422 || error?.code === "feishu_secret_required") {
    return "配置格式不正确，请检查必填项后重试。当前输入已保留。";
  }
  if (error?.status === 403 || error?.code === "forbidden") {
    return "当前账号没有管理飞书集成的权限。当前输入已保留。";
  }
  return "飞书配置暂时无法保存，请稍后重试。当前输入已保留。";
}

export async function startFeishuAuthorization(authorize, navigate = (url) => window.location.assign(url)) {
  const result = await authorize();
  const authorizationUrl = new URL(result?.authorization_url || "");
  if (authorizationUrl.protocol !== "https:" || authorizationUrl.hostname !== "accounts.feishu.cn") {
    throw new Error("invalid_feishu_authorization_url");
  }
  navigate(authorizationUrl.toString());
  return authorizationUrl.toString();
}
