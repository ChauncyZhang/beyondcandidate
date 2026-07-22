import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import test from "node:test";

import { createApiClient } from "./apiClient.js";
import { buildFeishuConfigPayload, getFeishuCallbackErrorCode, getFeishuConfigErrorMessage, getFeishuLoginErrorMessage, normalizeFeishuConfig, normalizeFeishuBinding, startFeishuAuthorization } from "./feishuIntegration.js";

function response(body, status = 200, headers = {}) {
  return new Response(body == null ? null : JSON.stringify(body), { status, headers: { "Content-Type": "application/json", ...headers } });
}

test("Feishu API methods follow the organization login and binding contracts", async () => {
  const calls = [];
  const client = createApiClient({ fetchImpl: async (path, options) => {
    calls.push([path, options]);
    if (path.endsWith("/authorize")) return response({ data: { authorization_url: "https://accounts.feishu.cn/auth", state: "opaque" } });
    if (options.method === "DELETE") return response(null, 204);
    return response({ data: { configured: false, enabled: false, bound: false } });
  } });

  await client.getFeishuConfig();
  await client.saveFeishuConfig({ app_id: "cli", enabled: false });
  await client.testFeishuConnection();
  await client.authorizeFeishuLogin("acme");
  await client.authorizeFeishuBinding();
  await client.getFeishuBinding();
  await client.unbindFeishu();

  assert.deepEqual(calls.map(([path, options]) => [path, options.method]), [
    ["/api/v1/settings/integrations/feishu", "GET"],
    ["/api/v1/settings/integrations/feishu", "PUT"],
    ["/api/v1/settings/integrations/feishu/test", "POST"],
    ["/api/v1/auth/feishu/authorize", "POST"],
    ["/api/v1/me/integrations/feishu/authorize", "POST"],
    ["/api/v1/me/integrations/feishu", "GET"],
    ["/api/v1/me/integrations/feishu", "DELETE"],
  ]);
});

test("Feishu projections keep only safe configuration and binding fields", () => {
  const config = normalizeFeishuConfig({
    configured: true, app_id: "cli", redirect_uri: "https://example.test/callback", calendar_id: "primary", enabled: true,
    app_secret_configured: true, verification_token_configured: true, encrypt_key_configured: true, version: 2,
    app_secret: "must-not-survive", verification_token: "must-not-survive", encrypt_key: "must-not-survive",
  });
  assert.equal(config.appSecretConfigured, true);
  assert.equal(JSON.stringify(config).includes("must-not-survive"), false);
  assert.deepEqual(normalizeFeishuBinding({ bound: true, union_id: "on_1", open_id: "ou_1", access_token: "no" }), { bound: true, unionId: "on_1", openId: "ou_1" });
});

test("blank optional Calendar ID falls back to the primary calendar", () => {
  assert.deepEqual(buildFeishuConfigPayload({
    app_id: " cli_app ",
    app_secret: "secret-value",
    redirect_uri: " https://hr.example.com/api/v1/auth/feishu/callback ",
    calendar_id: "   ",
    verification_token: "",
    encrypt_key: "",
    enabled: true,
  }), {
    app_id: "cli_app",
    app_secret: "secret-value",
    redirect_uri: "https://hr.example.com/api/v1/auth/feishu/callback",
    calendar_id: "primary",
    enabled: true,
  });
});

test("Feishu configuration errors explain validation failures without exposing server details", () => {
  assert.equal(
    getFeishuConfigErrorMessage({ status: 422, code: "request_failed", detail: "private validation internals" }),
    "配置格式不正确，请检查必填项后重试。当前输入已保留。",
  );
  assert.equal(
    getFeishuConfigErrorMessage({ status: 503, code: "unexpected", detail: "database password" }),
    "飞书配置暂时无法保存，请稍后重试。当前输入已保留。",
  );
});

test("Feishu authorization navigates only to an HTTPS Feishu URL", async () => {
  const destinations = [];
  await startFeishuAuthorization(async () => ({ authorization_url: "https://accounts.feishu.cn/open-apis/authen/v1/authorize" }), (url) => destinations.push(url));
  assert.deepEqual(destinations, ["https://accounts.feishu.cn/open-apis/authen/v1/authorize"]);
  await assert.rejects(() => startFeishuAuthorization(async () => ({ authorization_url: "javascript:alert(1)" }), () => {}));
});

test("Feishu login errors explain when an administrator must enable the integration", () => {
  assert.equal(
    getFeishuLoginErrorMessage({ code: "feishu_disabled" }),
    "当前组织尚未启用飞书登录，请联系管理员前往“设置 → 飞书集成”完成配置并启用。",
  );
  assert.equal(getFeishuLoginErrorMessage({ code: "service_unavailable" }), "飞书登录服务暂时不可用，请稍后重试。");
});

test("Feishu callback errors guide unbound users back to a safe account-linking flow", () => {
  assert.equal(
    getFeishuCallbackErrorCode("?feishu_error=feishu_account_not_invited_or_bound"),
    "feishu_account_not_invited_or_bound",
  );
  assert.equal(
    getFeishuLoginErrorMessage({ code: "feishu_account_not_invited_or_bound" }),
    "当前飞书账号尚未绑定。已有账号请先使用密码登录，再到“个人设置 → 飞书账号”完成绑定；新用户请联系管理员邀请。",
  );
  assert.equal(getFeishuCallbackErrorCode("?other=value"), "");
});

test("Login Settings and Profile expose only the requested Feishu entry points", async () => {
  const [login, inviteAccept, settings, profile, interviews] = await Promise.all([
    readFile(new URL("./LoginView.jsx", import.meta.url), "utf8"),
    readFile(new URL("./InviteAcceptView.jsx", import.meta.url), "utf8"),
    readFile(new URL("./SettingsViews.jsx", import.meta.url), "utf8"),
    readFile(new URL("./ProfileSettings.jsx", import.meta.url), "utf8"),
    readFile(new URL("./InterviewViews.jsx", import.meta.url), "utf8"),
  ]);
  assert.match(login, /飞书登录/);
  assert.match(login, /getFeishuCallbackErrorCode/);
  assert.match(inviteAccept, /startFeishuAuthorization\(\(\) => client\.authorizeFeishuLogin\(result\?\.organization_slug/);
  assert.match(inviteAccept, /onAccepted\(result\?\.email/);
  assert.match(settings, /飞书集成/);
  assert.match(profile, /飞书账号/);
  assert.doesNotMatch(interviews, /Feishu|飞书/);
});

test("Feishu settings keep the enable control and actions in one responsive footer", async () => {
  const [settings, styles] = await Promise.all([
    readFile(new URL("./FeishuIntegrationSettings.jsx", import.meta.url), "utf8"),
    readFile(new URL("./product-theme-admin.css", import.meta.url), "utf8"),
  ]);
  assert.match(settings, /className="feishu-form-footer"/);
  assert.match(settings, /className="feishu-enabled-control"/);
  assert.match(settings, /className="feishu-form-actions"/);
  assert.match(styles, /\.settings-page \.feishu-form-footer\s*\{/);
  assert.match(styles, /\.feishu-enabled-control input\[type="checkbox"\]/);
});

test("Feishu connection test reads the persisted test status instead of a missing ok field", async () => {
  const settings = await readFile(new URL("./FeishuIntegrationSettings.jsx", import.meta.url), "utf8");
  assert.match(settings, /next\.lastTestStatus === "succeeded"/);
  assert.doesNotMatch(settings, /result\?\.ok/);
});
