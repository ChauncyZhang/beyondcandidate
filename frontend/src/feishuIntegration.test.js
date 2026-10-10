import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import test from "node:test";

import { createApiClient } from "./apiClient.js";
import { getFeishuApprovalValidationDisabledReason, getFeishuApproverSelectionError } from "./feishuIntegration.js";
import { autoMatchFeishuDepartmentMappings, buildFeishuConfigPayload, buildFeishuOnboardingApprovalPayload, FEISHU_APPROVAL_CONTROL_TYPES, FEISHU_ONBOARDING_FIELDS, getFeishuCallbackErrorCode, getFeishuConfigErrorMessage, getFeishuConnectionTestErrorMessage, getFeishuLoginErrorMessage, getFeishuOnboardingApprovalErrorMessage, normalizeFeishuConfig, normalizeFeishuDepartmentDirectory, normalizeFeishuOnboardingApprovalConfig, normalizeFeishuBinding, startFeishuAuthorization } from "./feishuIntegration.js";

function response(body, status = 200, headers = {}) {
  return new Response(body == null ? null : JSON.stringify(body), { status, headers: { "Content-Type": "application/json", ...headers } });
}

test("fixed approvers preserve local UUIDs and unknown validation metadata through draft serialization", () => {
  const ids = ["00000000-0000-4000-8000-000000000002", "00000000-0000-4000-8000-000000000001"];
  const input = { approval_nodes: null, node_approvers: [{ node_id: "node", user_ids: ids }] };
  const draft = normalizeFeishuOnboardingApprovalConfig(input);
  assert.equal(draft.approvalNodes, null);
  assert.deepEqual(draft.nodeApprovers, [{ nodeId: "node", userIds: ids }]);
  const payload = buildFeishuOnboardingApprovalPayload(draft);
  assert.deepEqual(payload.node_approvers, [{ node_id: "node", user_ids: [...ids].sort() }]);
  assert.deepEqual(input.node_approvers[0].user_ids, ids);
  assert.equal(Object.hasOwn(payload, "approval_nodes"), false);
  assert.deepEqual(buildFeishuOnboardingApprovalPayload({ nodeApprovers: [{ nodeId: "node", userIds: [] }] }).node_approvers, []);
});

test("fixed approver validation distinguishes missing, single, unavailable, removed and valid selections", () => {
  const options = { nodes: [{ node_id: "node", name: "人事", need_approver: true, allow_multiple: false }], members: [{ user_id: "one", available: true }, { user_id: "two", available: true }, { user_id: "unbound", available: false }] };
  const draft = (ids, nodeId = "node") => ({ nodeApprovers: [{ nodeId, userIds: ids }] });
  assert.match(getFeishuApproverSelectionError(draft([]), options), /人事.*选择固定审批人/);
  assert.match(getFeishuApproverSelectionError(draft(["one", "two"]), options), /只能选择一名/);
  for (const id of ["unbound", "removed"]) assert.match(getFeishuApproverSelectionError(draft([id]), options), /不可用/);
  assert.match(getFeishuApproverSelectionError(draft(["one"], "obsolete"), options), /节点已变更/);
  assert.equal(getFeishuApproverSelectionError(draft(["one"]), options), "");
  assert.equal(getFeishuApproverSelectionError(draft(["one", "two"]), { ...options, nodes: [{ ...options.nodes[0], allow_multiple: true }] }), "");
  assert.equal(getFeishuApproverSelectionError(draft([]), { ...options, nodes: [{ ...options.nodes[0], need_approver: false }] }), "");
});

test("approval validation reasons cover pending states and trimmed required values", () => {
  const draft = normalizeFeishuOnboardingApprovalConfig({ approval_code: "code", field_mapping: Object.fromEntries(FEISHU_ONBOARDING_FIELDS.map((field) => [field.key, { control_id: field.key }])) });
  assert.equal(getFeishuApprovalValidationDisabledReason(draft, "ready", "ready"), "");
  for (const [status, expected] of [["loading", "加载"], ["saving", "保存"], ["validating", "校验"], ["refreshing", "刷新"], ["creating_department", "创建部门"]]) {
    assert.match(getFeishuApprovalValidationDisabledReason(draft, status, "ready"), new RegExp(expected));
  }
  assert.match(getFeishuApprovalValidationDisabledReason(draft, "ready", "loading"), /读取并匹配/);
  assert.match(getFeishuApprovalValidationDisabledReason({ ...draft, approvalCode: " " }, "ready", "ready"), /Approval Code/);
  draft.fieldMapping.email.controlId = " ";
  assert.match(getFeishuApprovalValidationDisabledReason(draft, "ready", "ready"), /邮箱/);
});

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
  await client.listFeishuDepartments();
  await client.getFeishuOnboardingApprovalConfig();
  await client.saveFeishuOnboardingApprovalConfig({ enabled: true }, { version: 2 });
  await client.validateFeishuOnboardingApprovalConfig({ version: 3 });
  await client.authorizeFeishuLogin("acme");
  await client.authorizeFeishuBinding();
  await client.getFeishuBinding();
  await client.unbindFeishu();

  assert.deepEqual(calls.map(([path, options]) => [path, options.method]), [
    ["/api/v1/settings/integrations/feishu", "GET"],
    ["/api/v1/settings/integrations/feishu", "PUT"],
    ["/api/v1/settings/integrations/feishu/test", "POST"],
    ["/api/v1/settings/integrations/feishu/departments", "GET"],
    ["/api/v1/settings/integrations/feishu/onboarding-approval", "GET"],
    ["/api/v1/settings/integrations/feishu/onboarding-approval", "PUT"],
    ["/api/v1/settings/integrations/feishu/onboarding-approval/validate", "POST"],
    ["/api/v1/auth/feishu/authorize", "POST"],
    ["/api/v1/me/integrations/feishu/authorize", "POST"],
    ["/api/v1/me/integrations/feishu", "GET"],
    ["/api/v1/me/integrations/feishu", "DELETE"],
  ]);
});

test("Feishu department directory normalizes safe fields and uniquely auto-matches names", () => {
  const directory = normalizeFeishuDepartmentDirectory([
    { open_department_id: "od-engineering", name: " 研发部 ", members: ["private"] },
    { open_department_id: "od-product-a", name: "产品部" },
    { open_department_id: "od-product-b", name: "产品部" },
    { open_department_id: "invalid", name: "无效" },
  ]);
  assert.deepEqual(directory, [
    { openDepartmentId: "od-product-a", name: "产品部" },
    { openDepartmentId: "od-product-b", name: "产品部" },
    { openDepartmentId: "od-engineering", name: "研发部" },
  ]);

  const result = autoMatchFeishuDepartmentMappings([
    { departmentId: "local-1", departmentName: "研发部", feishuDepartmentId: "" },
    { departmentId: "local-2", departmentName: "产品部", feishuDepartmentId: "" },
    { departmentId: "local-3", departmentName: "其他", feishuDepartmentId: "od-existing" },
  ], directory);
  assert.equal(result.matchedCount, 1);
  assert.equal(result.mappings[0].feishuDepartmentId, "od-engineering");
  assert.equal(result.mappings[1].feishuDepartmentId, "");
  assert.equal(result.mappings[2].feishuDepartmentId, "od-existing");
  assert.deepEqual(buildFeishuOnboardingApprovalPayload({
    departmentMappings: result.mappings,
  }).department_mappings, [
    { department_id: "local-1", feishu_department_id: "od-engineering" },
    { department_id: "local-3", feishu_department_id: "od-existing" },
  ]);
});

test("Feishu onboarding config serializes the seven fields sent to Feishu and keeps the start date internal", () => {
  const config = normalizeFeishuOnboardingApprovalConfig({
    enabled: true,
    approval_code: "APPROVAL-1",
    field_mapping: {
      candidate_name: { control_id: "name-id", type: "input" },
      gender: { control_id: "gender-id", type: "radio", options: { male: "先生", female: "女士" } },
    },
    department_mappings: [{ department_id: "dep-1", department_name: "研发部", feishu_department_id: "od-1" }],
    validation_status: "valid",
    validated_at: "2026-08-18T10:00:00Z",
    version: 4,
  });
  assert.equal(FEISHU_ONBOARDING_FIELDS.length, 7);
  assert.equal(FEISHU_ONBOARDING_FIELDS.some((field) => field.key === "expected_start_date"), false);
  assert.equal(FEISHU_APPROVAL_CONTROL_TYPES.some((type) => type.value === "radioV2"), true);
  assert.equal(config.fieldMapping.candidate_name.controlId, "name-id");
  assert.equal(config.fieldMapping.phone.type, "telephone");
  assert.equal(config.fieldMapping.gender.options.female, "女士");
  assert.equal(config.departmentMappings[0].feishuDepartmentId, "od-1");
  const payload = buildFeishuOnboardingApprovalPayload(config);
  assert.equal(payload.approval_code, "APPROVAL-1");
  assert.equal(Object.keys(payload.field_mapping).length, 7);
  assert.deepEqual(payload.field_mapping.gender.options, { male: "先生", female: "女士" });
  assert.deepEqual(payload.department_mappings, [{ department_id: "dep-1", feishu_department_id: "od-1" }]);
});

test("Feishu onboarding errors remain Chinese and do not expose provider details", () => {
  assert.match(getFeishuOnboardingApprovalErrorMessage("feishu_approval_control_unsupported"), /不支持/);
  assert.match(getFeishuOnboardingApprovalErrorMessage("feishu_department_unmapped"), /部门/);
  assert.doesNotMatch(getFeishuOnboardingApprovalErrorMessage({ code: "unknown", detail: "provider token" }), /provider token/);
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

test("Feishu message tests distinguish account binding from bot permission failures", () => {
  assert.match(getFeishuConnectionTestErrorMessage({ code: "feishu_test_user_unbound" }), /个人设置.*飞书账号/);
  assert.match(getFeishuConnectionTestErrorMessage({ code: "feishu_request_failed" }), /机器人能力.*以应用身份发消息/);
  assert.doesNotMatch(getFeishuConnectionTestErrorMessage({ code: "unknown", detail: "private" }), /private/);
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

test("Feishu settings expose onboarding approval mapping and validation without a new navigation item", async () => {
  const [settings, styles] = await Promise.all([
    readFile(new URL("./FeishuIntegrationSettings.jsx", import.meta.url), "utf8"),
    readFile(new URL("./product-theme-admin.css", import.meta.url), "utf8"),
  ]);
  assert.match(settings, /入职审批/);
  assert.match(settings, /Approval Code/);
  assert.match(settings, /FEISHU_ONBOARDING_FIELDS\.map/);
  assert.match(settings, /部门映射/);
  assert.match(settings, /新增本地部门/);
  assert.match(settings, /刷新飞书部门/);
  assert.match(settings, /listFeishuDepartments/);
  assert.match(settings, /client\.createDepartment\(\{ name, parent_id: null \}\)/);
  assert.match(settings, /部门已创建并加入飞书映射/);
  assert.match(settings, /校验审批模板/);
  assert.match(settings, /getFeishuOnboardingApprovalConfig/);
  assert.match(settings, /saveFeishuOnboardingApprovalConfig/);
  assert.match(settings, /validateFeishuOnboardingApprovalConfig/);
  assert.match(styles, /\.settings-page \.feishu-field-mapping/);
  assert.match(styles, /\.settings-page \.feishu-department-create/);
  assert.match(styles, /@media \(max-width: 640px\)/);
});

test("Feishu connection test reads the persisted test status instead of a missing ok field", async () => {
  const settings = await readFile(new URL("./FeishuIntegrationSettings.jsx", import.meta.url), "utf8");
  assert.match(settings, /next\.lastTestStatus === "succeeded"/);
  assert.doesNotMatch(settings, /result\?\.ok/);
});
