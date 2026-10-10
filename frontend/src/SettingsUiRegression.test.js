import assert from "node:assert/strict";
import test, { after, before } from "node:test";
import { fileURLToPath } from "node:url";
import { chromium } from "playwright";
import { createServer } from "vite";
import { FEISHU_ONBOARDING_FIELDS } from "./feishuIntegration.js";

let browser;
let vite;
let baseUrl;
const prototypeRoot = fileURLToPath(new URL("../", import.meta.url));

before(async () => {
  vite = await createServer({
    root: prototypeRoot,
    cacheDir: fileURLToPath(new URL(`../.tmp/vite-settings-regression-${process.pid}/`, import.meta.url)),
    logLevel: "silent",
    server: { host: "127.0.0.1", port: 0 },
  });
  await vite.listen();
  baseUrl = vite.resolvedUrls.local[0];
  browser = await chromium.launch({ headless: true });
});

async function openAuthenticatedPage(viewport, {
  currentRole = "recruiting_admin",
  initialDepartments = [{ id: "dep-1", name: "技术部", parent_id: null, member_count: 1, job_count: 1, status: "active" }],
  onboardingConfig = { enabled: false, approval_code: "", field_mapping: {}, department_mappings: [], validation_status: "unvalidated", version: 1 },
  departmentCreateError = null,
  feishuDepartments = [
    { open_department_id: "od-existing", name: "技术部" },
    { open_department_id: "od-product", name: "产品部" },
  ],
  feishuDepartmentError = null,
  feishuDepartmentDelayMs = 0,
  approvalRequests = [],
  approvalSaveError = null,
  approvalValidationError = null,
  approvalSaveDelayMs = 0,
  approvalLoadDelayMs = 0,
  approvalRefreshError = false,
  approverOptions = { nodes: [], members: [] },
  approverOptionsHandler = null,
} = {}) {
  if (onboardingConfig.validation_status === "valid" && onboardingConfig.approval_nodes === undefined) {
    onboardingConfig = { ...onboardingConfig, approval_nodes: [] };
  }
  const context = await browser.newContext({ viewport });
  let departments = [...initialDepartments];
  let users = [
    { id: "user-1", display_name: "Admin", email: "admin@example.test", department_id: "dep-1", department_name: "技术部", roles: ["recruiting_admin"], status: "active", recruiting_scope_type: "organization", recruiting_department_ids: [] },
    { id: "user-2", display_name: "林岚", email: "lin@example.test", department_id: "dep-1", department_name: "技术部", roles: ["recruiter"], status: "active", recruiting_scope_type: "departments", recruiting_department_ids: ["dep-1"] },
    { id: "user-3", display_name: "周宁", email: "zhou@example.test", department_id: "dep-1", department_name: "技术部", roles: ["hiring_manager"], status: "active", recruiting_scope_type: "organization", recruiting_department_ids: [] },
  ];
  await context.route("**/api/v1/**", async (route) => {
    const pathname = new URL(route.request().url()).pathname.replace(/\/$/, "");
    if (pathname === "/api/v1/me") {
      await route.fulfill({
        status: 200,
        contentType: "application/json",
        headers: { "x-csrf-token": "settings-regression" },
        body: JSON.stringify({ data: { id: "current-user", display_name: "Current User", roles: [currentRole] } }),
      });
      return;
    }
    if (pathname === "/api/v1/jobs") {
      await route.fulfill({
        status: 200,
        contentType: "application/json",
        body: JSON.stringify({ data: [{ id: "00000000-0000-4000-8000-000000000101", title: "AI 工程师", status: "open" }], meta: { next_cursor: null } }),
      });
      return;
    }
    if (pathname === "/api/v1/settings/departments" && route.request().method() === "POST") {
      const body = route.request().postDataJSON();
      assert.deepEqual(body, { name: "产品部", parent_id: null });
      if (departmentCreateError) {
        await route.fulfill({
          status: departmentCreateError.status,
          contentType: "application/problem+json",
          body: JSON.stringify({ status: departmentCreateError.status, code: departmentCreateError.code }),
        });
        return;
      }
      const department = { id: "dep-2", name: body.name, parent_id: null, member_count: 0, job_count: 0, status: "active" };
      departments = [...departments, department];
      await route.fulfill({ status: 201, contentType: "application/json", body: JSON.stringify({ data: department }) });
      return;
    }
    if (pathname === "/api/v1/settings/departments") {
      await route.fulfill({ status: 200, contentType: "application/json", body: JSON.stringify({ data: departments }) });
      return;
    }
    if (pathname === "/api/v1/settings/users" && route.request().method() === "GET") {
      await route.fulfill({ status: 200, contentType: "application/json", body: JSON.stringify({ data: users }) });
      return;
    }
    if (pathname === "/api/v1/settings/users/user-2/recruiting-scope" && route.request().method() === "PATCH") {
      const body = route.request().postDataJSON();
      assert.deepEqual(body, { recruiting_scope_type: "organization", recruiting_department_ids: [] });
      users = users.map((user) => user.id === "user-2" ? { ...user, ...body } : user);
      await route.fulfill({ status: 200, contentType: "application/json", body: JSON.stringify({ data: users.find((user) => user.id === "user-2") }) });
      return;
    }
    if (pathname === "/api/v1/settings/users/user-1/recruiting-scope" && route.request().method() === "PATCH") {
      const body = route.request().postDataJSON();
      assert.deepEqual(body, { role: "recruiter", recruiting_scope_type: "departments", recruiting_department_ids: ["dep-1"] });
      users = users.map((user) => user.id === "user-1" ? { ...user, ...body, roles: [body.role] } : user);
      await route.fulfill({ status: 200, contentType: "application/json", body: JSON.stringify({ data: users.find((user) => user.id === "user-1") }) });
      return;
    }
    if (pathname === "/api/v1/settings/integrations/feishu") {
      await route.fulfill({ status: 200, contentType: "application/json", body: JSON.stringify({ data: { enabled: false, configured: false, app_id: "", redirect_uri: "", calendar_id: "" } }) });
      return;
    }
    if (pathname === "/api/v1/settings/integrations/feishu/departments") {
      if (feishuDepartmentDelayMs) await new Promise((resolve) => setTimeout(resolve, feishuDepartmentDelayMs));
      if (feishuDepartmentError) {
        await route.fulfill({
          status: feishuDepartmentError.status,
          contentType: "application/problem+json",
          body: JSON.stringify(feishuDepartmentError),
        });
        return;
      }
      await route.fulfill({ status: 200, contentType: "application/json", body: JSON.stringify({ data: feishuDepartments }) });
      return;
    }
    if (pathname === "/api/v1/settings/integrations/feishu/onboarding-approval/approver-options") {
      if (approverOptionsHandler) return approverOptionsHandler(route);
      await route.fulfill({ status: 200, contentType: "application/json", body: JSON.stringify({ data: approverOptions }) });
      return;
    }
    if (pathname === "/api/v1/settings/integrations/feishu/onboarding-approval" && route.request().method() === "GET") {
      if (approvalLoadDelayMs) await new Promise((resolve) => setTimeout(resolve, approvalLoadDelayMs));
      if (approvalRefreshError && approvalRequests.some((item) => item.action === "validate")) {
        await route.fulfill({ status: 503, contentType: "application/problem+json", body: JSON.stringify({ status: 503 }) });
        return;
      }
      await route.fulfill({ status: 200, contentType: "application/json", body: JSON.stringify({ data: onboardingConfig }) });
      return;
    }
    if (pathname === "/api/v1/settings/integrations/feishu/onboarding-approval/validate" && route.request().method() === "POST") {
      approvalRequests.push({ action: "validate", version: route.request().headers()["if-match"] });
      if (approvalValidationError) {
        onboardingConfig = { ...onboardingConfig, enabled: false, validation_status: "invalid", version: onboardingConfig.version + 1 };
        await route.fulfill({ status: 422, contentType: "application/problem+json", body: JSON.stringify({ status: 422, code: approvalValidationError }) });
        return;
      }
      onboardingConfig = { ...onboardingConfig, validation_status: "valid", approval_nodes: approverOptions.nodes, version: onboardingConfig.version + 1 };
      await route.fulfill({ status: 200, contentType: "application/json", body: JSON.stringify({ data: onboardingConfig }) });
      return;
    }
    if (pathname === "/api/v1/settings/integrations/feishu/onboarding-approval" && route.request().method() === "PUT") {
      approvalRequests.push({ action: "save", version: route.request().headers()["if-match"], body: route.request().postDataJSON() });
      if (approvalSaveDelayMs) await new Promise((resolve) => setTimeout(resolve, approvalSaveDelayMs));
      if (approvalSaveError) {
        await route.fulfill({ status: 503, contentType: "application/problem+json", body: JSON.stringify({ status: 503, code: approvalSaveError }) });
        return;
      }
      onboardingConfig = { ...onboardingConfig, ...route.request().postDataJSON(), version: onboardingConfig.version + 1 };
      await route.fulfill({ status: 200, contentType: "application/json", body: JSON.stringify({ data: onboardingConfig }) });
      return;
    }
    await route.fulfill({ status: 503, contentType: "application/problem+json", body: JSON.stringify({ status: 503 }) });
  });
  const page = await context.newPage();
  await page.goto(baseUrl, { waitUntil: "domcontentloaded" });
  await page.getByRole("heading", { name: currentRole === "system_admin" ? "设置" : "工作台", exact: true }).waitFor();
  return { context, page };
}

async function openDeletionApprovalPage() {
  const context = await browser.newContext({ viewport: { width: 1280, height: 800 } });
  let request = {
    id: "3137e3e8-dcbd-4ea2-bab3-7e753b5b2952",
    status: "requested",
    version: 1,
    reason_code: "administrator_request",
    requested_at: "2026-07-22T05:42:40Z",
    approved_at: null,
    safe_error_code: null,
    active_application_count: 1,
    impact: {
      schema_version: 1,
      candidate_ref: "11111111-1111-4111-8111-111111111111",
      candidate_version: 1,
      policy_version: 1,
      counts: { contacts: 0, resumes: 1, applications: 1, screening_records: 2, interviews: 0, feedback_records: 0, talent_memberships: 0, resume_objects: 1, temporary_exports: 0 },
      backup_window_ends_at: "2026-10-20T05:42:40Z",
    },
  };
  await context.route("**/api/v1/**", async (route) => {
    const pathname = new URL(route.request().url()).pathname.replace(/\/$/, "");
    if (pathname === "/api/v1/me") {
      await route.fulfill({ status: 200, contentType: "application/json", headers: { "x-csrf-token": "approval-regression" }, body: JSON.stringify({ data: { id: "system-admin-1", display_name: "System Admin", roles: ["system_admin"] } }) });
      return;
    }
    if (pathname === `/api/v1/deletion-requests/${request.id}/transitions` && route.request().method() === "POST") {
      assert.deepEqual(route.request().postDataJSON(), { target_status: "approved", terminate_active_applications: true });
      request = { ...request, status: "approved", version: 2, approved_at: "2026-07-22T06:00:00Z", active_application_count: 0 };
      await route.fulfill({ status: 200, contentType: "application/json", body: JSON.stringify({ data: request }) });
      return;
    }
    if (pathname === `/api/v1/deletion-requests/${request.id}`) {
      await route.fulfill({ status: 200, contentType: "application/json", body: JSON.stringify({ data: request }) });
      return;
    }
    if (pathname === "/api/v1/deletion-requests") {
      await route.fulfill({ status: 200, contentType: "application/json", body: JSON.stringify({ data: [request], meta: { next_cursor: null, limit: 50 } }) });
      return;
    }
    await route.fulfill({ status: 503, contentType: "application/problem+json", body: JSON.stringify({ status: 503 }) });
  });
  const page = await context.newPage();
  await page.goto(`${baseUrl}settings/governance`, { waitUntil: "domcontentloaded" });
  await page.getByRole("heading", { name: "删除请求审批", exact: true }).waitFor();
  return { context, page, requestId: request.id };
}

async function assertNoHorizontalOverflow(page, label) {
  const widths = await page.evaluate(() => ({
    body: [document.body.clientWidth, document.body.scrollWidth],
    root: [document.documentElement.clientWidth, document.documentElement.scrollWidth],
  }));
  assert.equal(widths.body[0], widths.body[1], `${label} body overflow: ${JSON.stringify(widths)}`);
  assert.equal(widths.root[0], widths.root[1], `${label} root overflow: ${JSON.stringify(widths)}`);
}

after(async () => {
  await browser?.close();
  await vite?.close();
});

test("dedicated deployment hides the organization field and submits its configured slug", { timeout: 60_000 }, async () => {
  const context = await browser.newContext({ viewport: { width: 1280, height: 800 } });
  let loginPayload;
  await context.route("**/api/v1/**", async (route) => {
    const pathname = new URL(route.request().url()).pathname.replace(/\/$/, "");
    if (pathname === "/api/v1/me") {
      await route.fulfill({ status: 401, contentType: "application/problem+json", body: JSON.stringify({ status: 401 }) });
      return;
    }
    if (pathname === "/api/v1/auth/config") {
      await route.fulfill({
        status: 200,
        contentType: "application/json",
        body: JSON.stringify({ data: { default_organization: { slug: "acme", name: "Acme" } } }),
      });
      return;
    }
    if (pathname === "/api/v1/auth/login") {
      loginPayload = route.request().postDataJSON();
      await route.fulfill({ status: 401, contentType: "application/problem+json", body: JSON.stringify({ status: 401 }) });
      return;
    }
    await route.fulfill({ status: 503, contentType: "application/problem+json", body: JSON.stringify({ status: 503 }) });
  });
  const page = await context.newPage();
  try {
    const contextResponse = page.waitForResponse((response) => new URL(response.url()).pathname === "/api/v1/auth/config");
    await page.goto(baseUrl, { waitUntil: "domcontentloaded" });
    await page.getByRole("heading", { name: "登录工作台", exact: true }).waitFor();
    await contextResponse;
    assert.equal(await page.locator('input[name="organization_slug"]').count(), 0);
    await page.locator('input[name="email"]').fill("hr@example.test");
    await page.locator('input[name="password"]').fill("secret");
    const loginResponse = page.waitForResponse((response) => new URL(response.url()).pathname === "/api/v1/auth/login");
    await page.getByRole("button", { name: "登录", exact: true }).click();
    await loginResponse;
    assert.deepEqual(loginPayload, {
      organization_slug: "acme",
      email: "hr@example.test",
      password: "secret",
    });
  } finally {
    await context.close();
  }
});

test("successful password login replaces a stale settings route with the role default workbench", { timeout: 60_000 }, async () => {
  const context = await browser.newContext({ viewport: { width: 1280, height: 800 } });
  let authenticated = false;
  const emptyGroup = { count: 0, items: [] };
  await context.route("**/api/v1/**", async (route) => {
    const pathname = new URL(route.request().url()).pathname.replace(/\/$/, "");
    if (pathname === "/api/v1/me") {
      await route.fulfill(authenticated ? {
        status: 200,
        contentType: "application/json",
        headers: { "x-csrf-token": "post-login-csrf" },
        body: JSON.stringify({ data: { id: "user-1", display_name: "Admin", roles: ["recruiting_admin"] } }),
      } : { status: 401, contentType: "application/problem+json", body: JSON.stringify({ status: 401 }) });
      return;
    }
    if (pathname === "/api/v1/auth/config") {
      await route.fulfill({ status: 200, contentType: "application/json", body: JSON.stringify({ data: { default_organization: { slug: "acme", name: "Acme" } } }) });
      return;
    }
    if (pathname === "/api/v1/auth/login") {
      authenticated = true;
      await route.fulfill({ status: 204, body: "" });
      return;
    }
    if (pathname === "/api/v1/workbench") {
      await route.fulfill({
        status: 200,
        contentType: "application/json",
        body: JSON.stringify({
          data: {
            generated_at: "2026-07-22T05:00:00Z",
            jobs: [],
            tasks: { review: emptyGroup, interview_pending: emptyGroup, decision: emptyGroup, passed: emptyGroup },
            notifications: { review: emptyGroup, interview_pending: emptyGroup, decision: emptyGroup, passed: emptyGroup },
            interviews: { available: false, upcoming: [], pending_feedback: [] },
          },
        }),
      });
      return;
    }
    await route.fulfill({ status: 503, contentType: "application/problem+json", body: JSON.stringify({ status: 503 }) });
  });
  const page = await context.newPage();
  try {
    await page.goto(new URL("/settings/ai", baseUrl).toString(), { waitUntil: "domcontentloaded" });
    await page.getByRole("heading", { name: "登录工作台", exact: true }).waitFor();
    await page.locator('input[name="email"]').fill("hr@example.test");
    await page.locator('input[name="password"]').fill("secret");
    await page.getByRole("button", { name: "登录", exact: true }).click();

    await page.waitForURL((url) => url.pathname === "/workbench");
    await page.getByRole("heading", { name: "工作台", exact: true }).waitFor();
  } finally {
    await context.close();
  }
});

test("locked login shows the server countdown and switching accounts restores submit", { timeout: 60_000 }, async () => {
  const context = await browser.newContext({ viewport: { width: 1280, height: 800 } });
  await context.route("**/api/v1/**", async (route) => {
    const pathname = new URL(route.request().url()).pathname.replace(/\/$/, "");
    if (pathname === "/api/v1/me") {
      await route.fulfill({ status: 401, contentType: "application/problem+json", body: JSON.stringify({ status: 401 }) });
      return;
    }
    if (pathname === "/api/v1/auth/config") {
      await route.fulfill({ status: 200, contentType: "application/json", body: JSON.stringify({ data: { default_organization: { slug: "acme", name: "Acme" } } }) });
      return;
    }
    if (pathname === "/api/v1/auth/login") {
      await route.fulfill({
        status: 429,
        contentType: "application/problem+json",
        headers: { "Retry-After": "5" },
        body: JSON.stringify({ status: 429, code: "account_temporarily_locked", detail: "Too many failed login attempts.", retry_after_seconds: 5 }),
      });
      return;
    }
    await route.fulfill({ status: 503, contentType: "application/problem+json", body: JSON.stringify({ status: 503 }) });
  });
  const page = await context.newPage();
  try {
    await page.goto(baseUrl, { waitUntil: "domcontentloaded" });
    await page.getByRole("heading", { name: "登录工作台", exact: true }).waitFor();
    await page.locator('input[name="email"]').fill("locked@example.test");
    await page.locator('input[name="password"]').fill("incorrect");
    await page.getByRole("button", { name: "登录", exact: true }).click();

    const notice = page.locator(".login-lockout-message");
    await notice.waitFor();
    assert.match(await notice.innerText(), /账号已临时锁定/);
    assert.match(await notice.innerText(), /请于 \d{2}:\d{2}:\d{2} 后重试/);
    assert.match(await notice.innerText(), /剩余 [1-5] 秒/);
    assert.equal(await page.getByRole("button", { name: /请等待/ }).isDisabled(), true);

    await page.locator('input[name="email"]').fill("other@example.test");
    assert.equal(await notice.count(), 0);
    assert.equal(await page.getByRole("button", { name: "登录", exact: true }).isEnabled(), true);
  } finally {
    await context.close();
  }
});

test("invite member opens a usable server-backed invitation drawer", { timeout: 60_000 }, async () => {
  const { context, page } = await openAuthenticatedPage({ width: 1280, height: 800 });
  try {
    await page.getByRole("button", { name: "设置", exact: true }).click();
    await page.getByRole("button", { name: "邀请成员", exact: true }).click();

    const drawer = page.getByRole("dialog", { name: "邀请成员", exact: true });
    await drawer.waitFor();
    await drawer.getByLabel("姓名", { exact: true }).fill("林岚");
    await drawer.getByLabel("工作邮箱", { exact: true }).fill("linlan@example.test");
    await drawer.getByLabel("所属部门", { exact: true }).selectOption("dep-1");
    assert.equal(await drawer.getByRole("button", { name: "发送邀请", exact: true }).isEnabled(), true);
  } finally {
    await context.close();
  }
});

function completeApprovalConfig(overrides = {}) {
  return {
    enabled: false, approval_code: "approval-saved", version: 3, validation_status: "unvalidated",
    field_mapping: Object.fromEntries(FEISHU_ONBOARDING_FIELDS.map((field) => [field.key, {
      control_id: `${field.key}-control`, type: field.defaultType,
      ...(field.key === "gender" ? { options: { male: "male-option", female: "female-option" } } : {}),
    }])),
    department_mappings: [{ department_id: "dep-1", feishu_department_id: "od-existing" }],
    ...overrides,
  };
}

async function openApprovalForm(page) {
  await page.getByRole("button", { name: "设置", exact: true }).click();
  await page.getByRole("button", { name: "飞书集成", exact: true }).click();
  const form = page.locator(".feishu-onboarding-form");
  await form.waitFor();
  await form.getByText(/完成同名自动匹配检查/).waitFor();
  if ((await form.locator(".feishu-approval-code input").inputValue()).trim()) {
    await form.locator(".feishu-fixed-approvers").waitFor();
  }
  return form;
}

const approverIds = ["00000000-0000-4000-8000-000000000001", "00000000-0000-4000-8000-000000000002", "00000000-0000-4000-8000-000000000003"];
const approvalNodeId = "b3a3a075c6399c41e83e922d46af58c8";
function fixedApproverOptions() {
  return {
    nodes: [
      { node_id: "start", name: "开始", need_approver: false, allow_multiple: true },
      { node_id: approvalNodeId, name: "审批", need_approver: true, allow_multiple: true },
      { node_id: "single", name: "复核", need_approver: true, allow_multiple: false },
      { node_id: "end", name: "结束", need_approver: false, allow_multiple: true },
    ],
    members: [
      { user_id: approverIds[0], display_name: "林岚", available: true, unavailable_reason: null },
      { user_id: approverIds[1], display_name: "周宁", available: true, unavailable_reason: null },
      { user_id: approverIds[2], display_name: "未绑定成员", available: false, unavailable_reason: "feishu_approver_unbound" },
    ],
  };
}

test("legacy validated config without node metadata requires revalidation and permits only disabling", { timeout: 60_000 }, async () => {
  for (const enabled of [false, true]) {
    const requests = [];
    const { context, page } = await openAuthenticatedPage({ width: 1280, height: 800 }, {
      onboardingConfig: completeApprovalConfig({ enabled, validation_status: "valid", approval_nodes: null }),
      approvalRequests: requests,
    });
    try {
      const form = await openApprovalForm(page);
      assert.equal(await form.locator(".feishu-validation-status").innerText(), "待重新校验");
      await form.getByRole("status").filter({ hasText: /尚未校验审批节点.*重新校验/ }).waitFor();
      const toggle = form.getByLabel("启用飞书入职审批");
      assert.equal(await toggle.isChecked(), enabled);
      if (enabled) {
        assert.equal(await toggle.isEnabled(), true, "legacy enabled configuration must remain switchable off");
        await toggle.uncheck();
        assert.equal(await toggle.isDisabled(), true);
        await form.getByRole("button", { name: "保存入职审批配置", exact: true }).click();
        await form.getByText("配置已保存并通过校验，但尚未启用入职审批。", { exact: true }).waitFor();
        assert.equal(requests[0].body.enabled, false);
      }
      assert.equal(await toggle.isDisabled(), true, "reading options alone must not authorize enablement");
      await form.getByRole("button", { name: "校验审批模板", exact: true }).click();
      await form.getByText("审批模板校验通过，但入职审批尚未启用。", { exact: true }).waitFor();
      assert.equal(await form.locator(".feishu-validation-status").innerText(), "已校验 · 未启用");
      assert.equal(await toggle.isEnabled(), true);
      assert.equal(await toggle.isChecked(), false);
      assert.equal(requests.at(-1).action, "validate");
    } finally { await context.close(); }
  }
});

test("fixed approvers block incomplete validation, save draft, serialize multi and single choices and require revalidation", { timeout: 60_000 }, async () => {
  const requests = [];
  const { context, page } = await openAuthenticatedPage({ width: 1280, height: 800 }, {
    onboardingConfig: completeApprovalConfig({ approval_nodes: null, node_approvers: [] }),
    approverOptions: fixedApproverOptions(), approvalRequests: requests,
  });
  try {
    const form = await openApprovalForm(page);
    const multi = form.getByRole("group", { name: "审批（可多选）", exact: true });
    const single = form.getByRole("group", { name: "复核（单选）", exact: true });
    const validate = () => form.getByRole("button", { name: /^(保存并)?校验审批模板$/ });
    assert.equal(await form.getByRole("group").count(), 2);
    assert.equal(await validate().isDisabled(), true);
    assert.match(await form.locator("#feishu-validation-action-reason").innerText(), /审批.*选择固定审批人/);
    assert.equal(await multi.getByRole("checkbox", { name: /未绑定成员/ }).isDisabled(), true);
    assert.match(await multi.innerText(), /未绑定飞书账号/);
    await multi.getByRole("checkbox", { name: "林岚", exact: true }).focus();
    await page.keyboard.press("Space");
    assert.equal(await multi.getByRole("checkbox", { name: "林岚", exact: true }).isChecked(), true);
    await multi.getByRole("checkbox", { name: "周宁", exact: true }).check();
    assert.equal(await validate().isDisabled(), true);
    await form.getByRole("button", { name: "保存入职审批配置", exact: true }).click();
    await form.getByText("入职审批配置已保存，尚未校验、未启用。", { exact: true }).waitFor();
    assert.deepEqual(requests.map((item) => item.action), ["save"]);
    assert.deepEqual(requests[0].body.node_approvers, [{ node_id: approvalNodeId, user_ids: approverIds.slice(0, 2) }]);
    await single.getByRole("radio", { name: "林岚", exact: true }).check();
    await single.getByRole("radio", { name: "周宁", exact: true }).check();
    assert.equal(await single.getByRole("radio", { name: "林岚", exact: true }).isChecked(), false);
    assert.equal(await form.getByLabel("启用飞书入职审批").isDisabled(), true);
    await validate().click();
    await form.getByText("审批模板校验通过，但入职审批尚未启用。", { exact: true }).waitFor();
    assert.deepEqual(requests.map((item) => item.action), ["save", "save", "validate"]);
    assert.deepEqual(requests[1].body.node_approvers, [{ node_id: approvalNodeId, user_ids: approverIds.slice(0, 2) }, { node_id: "single", user_ids: [approverIds[1]] }]);
    assert.equal(requests[1].body.enabled, false);
    assert.equal(await form.getByLabel("启用飞书入职审批").isEnabled(), true);
    await form.getByLabel("启用飞书入职审批").check();
    await multi.getByRole("checkbox", { name: "林岚", exact: true }).uncheck();
    assert.equal(await form.getByLabel("启用飞书入职审批").isChecked(), false);
    assert.equal(await form.getByLabel("启用飞书入职审批").isDisabled(), true);
  } finally { await context.close(); }
});

test("fixed approvers allow removal of stale nodes, missing members and selected unbound members", { timeout: 60_000 }, async () => {
  const requests = [];
  const { context, page } = await openAuthenticatedPage({ width: 1280, height: 800 }, {
    onboardingConfig: completeApprovalConfig({ node_approvers: [
      { node_id: "obsolete", user_ids: [approverIds[0]] },
      { node_id: approvalNodeId, user_ids: ["00000000-0000-4000-8000-000000000099", approverIds[2]] },
      { node_id: "single", user_ids: [approverIds[0]] },
    ] }), approverOptions: fixedApproverOptions(), approvalRequests: requests,
  });
  try {
    const form = await openApprovalForm(page);
    assert.match(await form.locator("#feishu-validation-action-reason").innerText(), /节点已变更/);
    await form.getByRole("button", { name: "清除失效节点" }).click();
    await form.getByRole("button", { name: "移除不可用成员" }).click();
    await form.getByRole("button", { name: "移除未绑定成员", exact: true }).click();
    const multi = form.getByRole("group", { name: "审批（可多选）", exact: true });
    assert.equal(await multi.getByRole("checkbox", { name: /未绑定成员/ }).isChecked(), false);
    await multi.getByRole("checkbox", { name: "林岚", exact: true }).check();
    await form.getByRole("button", { name: "保存入职审批配置", exact: true }).click();
    await form.getByText("入职审批配置已保存，尚未校验、未启用。", { exact: true }).waitFor();
    assert.deepEqual(requests[0].body.node_approvers, [{ node_id: approvalNodeId, user_ids: [approverIds[0]] }, { node_id: "single", user_ids: [approverIds[0]] }]);
  } finally { await context.close(); }
});

test("fixed approver options ignore a late old-code response and manual refresh uses the latest draft", { timeout: 60_000 }, async () => {
  const codes = [];
  let releaseOld;
  const oldGate = new Promise((resolve) => { releaseOld = resolve; });
  const { context, page } = await openAuthenticatedPage({ width: 1280, height: 800 }, {
    onboardingConfig: completeApprovalConfig(),
    approverOptionsHandler: async (route) => {
      const code = new URL(route.request().url()).searchParams.get("approval_code");
      codes.push(code);
      if (code === "old-code") await oldGate;
      await route.fulfill({ status: 200, contentType: "application/json", body: JSON.stringify({ data: { nodes: [{ node_id: code, name: code, need_approver: true, allow_multiple: true }], members: fixedApproverOptions().members } }) });
    },
  });
  try {
    const form = await openApprovalForm(page);
    const input = form.locator(".feishu-approval-code input");
    await page.evaluate(() => {
      const readJson = Response.prototype.json;
      Response.prototype.json = async function (...args) {
        const data = await readJson.apply(this, args);
        if (new URL(this.url).searchParams.get("approval_code") === "old-code") {
          window.oldApproverBodyConsumed = true;
        }
        return data;
      };
    });
    const oldRequest = page.waitForRequest((request) => request.url().includes("approval_code=old-code"));
    await input.fill("old-code");
    await oldRequest;
    assert.equal(await form.getByRole("button", { name: /^(保存并)?校验审批模板$/ }).isDisabled(), true);
    await input.fill("latest-code");
    await form.getByRole("group", { name: "latest-code（可多选）", exact: true }).waitFor();
    const oldResponse = page.waitForResponse((response) => response.url().includes("approval_code=old-code"));
    releaseOld();
    const completedOldResponse = await oldResponse;
    await completedOldResponse.finished();
    await page.waitForFunction(() => window.oldApproverBodyConsumed === true);
    await page.evaluate(() => new Promise((resolve) => requestAnimationFrame(() => requestAnimationFrame(resolve))));
    assert.equal(await form.getByRole("group", { name: "old-code（可多选）", exact: true }).count(), 0);
    assert.equal(await form.getByRole("group", { name: "latest-code（可多选）", exact: true }).count(), 1);
    const refreshResponse = page.waitForResponse((response) => response.url().includes("approval_code=latest-code"));
    await form.getByRole("button", { name: "读取审批节点和成员", exact: true }).click();
    await refreshResponse;
    await form.getByRole("group", { name: "latest-code（可多选）", exact: true }).waitFor();
    assert.deepEqual(codes, ["approval-saved", "old-code", "latest-code", "latest-code"]);
  } finally { releaseOld(); await context.close(); }
});

test("fixed approvers fit desktop and mobile with long labels and produce a screenshot", { timeout: 60_000 }, async () => {
  const options = fixedApproverOptions();
  options.members[0].display_name = "跨区域人力资源审批负责人".repeat(5);
  const { context, page } = await openAuthenticatedPage({ width: 1280, height: 800 }, { onboardingConfig: completeApprovalConfig(), approverOptions: options });
  try {
    const form = await openApprovalForm(page);
    for (const width of [1280, 390]) {
      await page.setViewportSize({ width, height: 844 });
      await assertNoHorizontalOverflow(page, `${width}px fixed approvers`);
      const boxes = await form.locator(".feishu-approver-node, .feishu-approver-members label").evaluateAll((elements) => elements.map((element) => {
        const box = element.getBoundingClientRect();
        return { left: box.left, right: box.right, width: box.width, overflow: element.scrollWidth - element.clientWidth };
      }));
      assert.ok(boxes.every((box) => box.width > 0 && box.left >= 0 && box.right <= width && box.overflow <= 1), JSON.stringify(boxes));
    }
    const screenshot = await form.getByRole("region", { name: "固定审批人", exact: true }).screenshot({ path: fileURLToPath(new URL("../.tmp/fixed-approvers-mobile.png", import.meta.url)) });
    assert.ok(screenshot.length > 1000);
  } finally { await context.close(); }
});

test("Feishu save-and-validate persists auto and manual mappings before validating the returned version without enabling", { timeout: 60_000 }, async () => {
  for (const manual of [false, true]) {
    const requests = [];
    const { context, page } = await openAuthenticatedPage({ width: 1280, height: 800 }, {
      onboardingConfig: completeApprovalConfig({ department_mappings: [] }), approvalRequests: requests, approvalSaveDelayMs: 500,
    });
    try {
      const form = await openApprovalForm(page);
      if (manual) {
        await form.getByLabel("姓名控件 ID", { exact: true }).fill("manual-control");
        await form.getByLabel("技术部对应飞书部门").selectOption("od-product");
      }
      const action = form.getByRole("button", { name: "保存并校验审批模板", exact: true });
      assert.equal(await action.isEnabled(), true);
      await action.focus();
      await page.keyboard.press("Enter");
      await form.getByText("正在保存入职审批配置，请稍候。", { exact: true }).waitFor();
      assert.equal(await action.isDisabled(), true);
      await form.getByText("审批模板校验通过，但入职审批尚未启用。", { exact: true }).waitFor();
      assert.deepEqual(requests.map(({ action, version }) => ({ action, version })), [{ action: "save", version: '"3"' }, { action: "validate", version: '"4"' }]);
      assert.equal(requests[0].body.enabled, false);
      assert.equal(requests[0].body.department_mappings[0].feishu_department_id, manual ? "od-product" : "od-existing");
      if (manual) assert.equal(requests[0].body.field_mapping.candidate_name.control_id, "manual-control");
      assert.equal(await form.getByLabel("启用飞书入职审批").isChecked(), false);
      await form.getByText(/如需启用入职审批，请打开/).waitFor();
      await form.getByLabel("启用飞书入职审批").check();
      await form.getByRole("button", { name: "保存并校验审批模板" }).click();
      await form.getByText("审批模板校验通过，但入职审批尚未启用。", { exact: true }).waitFor();
      assert.equal(requests[2].body.enabled, false, "validation action must not save a pending enable toggle");
      assert.equal(await form.getByLabel("启用飞书入职审批").isChecked(), false);
      await page.setViewportSize({ width: 390, height: 844 });
      await assertNoHorizontalOverflow(page, "390px save-and-validate");
    } finally { await context.close(); }
  }
});

test("Feishu save failure preserves dirty draft and never validates", { timeout: 60_000 }, async () => {
  const requests = [];
  const { context, page } = await openAuthenticatedPage({ width: 1280, height: 800 }, {
    onboardingConfig: completeApprovalConfig(), approvalRequests: requests, approvalSaveError: "unavailable",
  });
  try {
    const form = await openApprovalForm(page);
    await form.getByLabel("姓名控件 ID", { exact: true }).fill("unsaved-control");
    await form.getByRole("button", { name: "保存并校验审批模板" }).click();
    await form.locator(".settings-error").waitFor();
    assert.deepEqual(requests.map((item) => item.action), ["save"]);
    assert.equal(await form.getByLabel("姓名控件 ID", { exact: true }).inputValue(), "unsaved-control");
    assert.equal(await form.getByRole("button", { name: "保存并校验审批模板" }).isEnabled(), true);
  } finally { await context.close(); }
});

test("Feishu validation failure refreshes the version for retry and preserves inputs", { timeout: 60_000 }, async () => {
  const requests = [];
  const { context, page } = await openAuthenticatedPage({ width: 1280, height: 800 }, {
    onboardingConfig: completeApprovalConfig(), approvalRequests: requests, approvalValidationError: "feishu_approval_definition_invalid",
  });
  try {
    const form = await openApprovalForm(page);
    await form.getByLabel("姓名控件 ID", { exact: true }).fill("edited-control");
    await form.getByRole("button", { name: "保存并校验审批模板" }).click();
    await form.getByText(/Approval Code 或控件映射与飞书审批模板不一致/).waitFor();
    const retryResponse = page.waitForResponse((response) => response.url().endsWith("/onboarding-approval/validate"));
    await form.getByRole("button", { name: "校验审批模板", exact: true }).click();
    await retryResponse;
    assert.deepEqual(requests.map((item) => item.version), ['"3"', '"4"', '"5"']);
    assert.equal(await form.getByLabel("姓名控件 ID", { exact: true }).inputValue(), "edited-control");
    assert.equal(await form.getByLabel("启用飞书入职审批").isChecked(), false);
  } finally { await context.close(); }
});

test("Feishu failed revalidation reflects server disablement without losing mappings", { timeout: 60_000 }, async () => {
  const { context, page } = await openAuthenticatedPage({ width: 1280, height: 800 }, {
    onboardingConfig: completeApprovalConfig({ enabled: true, validation_status: "valid" }), approvalValidationError: "feishu_approval_definition_invalid",
  });
  try {
    const form = await openApprovalForm(page);
    await form.getByRole("button", { name: "校验审批模板", exact: true }).click();
    await form.getByText("校验未通过", { exact: true }).waitFor();
    assert.equal(await form.getByLabel("启用飞书入职审批").isChecked(), false);
    assert.equal(await form.getByLabel("姓名控件 ID", { exact: true }).inputValue(), "candidate_name-control");
  } finally { await context.close(); }
});

test("Feishu validation refresh failure blocks stale writes and offers an explicit refresh", { timeout: 60_000 }, async () => {
  const { context, page } = await openAuthenticatedPage({ width: 1280, height: 800 }, {
    onboardingConfig: completeApprovalConfig(), approvalValidationError: "feishu_approval_definition_invalid", approvalRefreshError: true,
  });
  try {
    const form = await openApprovalForm(page);
    await form.getByRole("button", { name: "校验审批模板", exact: true }).click();
    await form.getByRole("button", { name: "重试刷新配置版本" }).waitFor();
    await form.getByLabel("姓名控件 ID", { exact: true }).fill("preserved-after-failure");
    assert.equal(await form.getByRole("button", { name: "保存并校验审批模板" }).isDisabled(), true);
    assert.equal(await form.getByRole("button", { name: "保存入职审批配置" }).isDisabled(), true);
    assert.match(await form.locator("#feishu-validation-action-reason").innerText(), /当前输入已保留/);
    await page.route("**/onboarding-approval", async (route) => {
      assert.equal(route.request().method(), "GET");
      await new Promise((resolve) => setTimeout(resolve, 300));
      await route.fulfill({ status: 200, contentType: "application/json", body: JSON.stringify({ data: completeApprovalConfig({ version: 10, validation_status: "invalid" }) }) });
    });
    await form.getByRole("button", { name: "重试刷新配置版本" }).click();
    await form.getByText("正在刷新配置版本，请稍候。", { exact: true }).waitFor();
    await form.getByRole("button", { name: "重试刷新配置版本" }).waitFor({ state: "detached" });
    assert.equal(await form.getByLabel("姓名控件 ID", { exact: true }).inputValue(), "preserved-after-failure");
    assert.equal(await form.getByRole("button", { name: "保存并校验审批模板" }).isEnabled(), true);
  } finally { await context.close(); }
});

test("Feishu validation exposes loading and precise missing-field reasons", { timeout: 60_000 }, async () => {
  const { context, page } = await openAuthenticatedPage({ width: 1280, height: 800 }, {
    onboardingConfig: completeApprovalConfig(), approvalLoadDelayMs: 500, feishuDepartmentDelayMs: 500,
  });
  try {
    await page.getByRole("button", { name: "设置", exact: true }).click();
    await page.getByRole("button", { name: "飞书集成", exact: true }).click();
    await page.getByText("正在加载入职审批配置…", { exact: true }).waitFor();
    const form = page.locator(".feishu-onboarding-form");
    await form.getByText("正在读取并匹配飞书部门，请稍候。", { exact: true }).waitFor();
    await form.getByText(/完成同名自动匹配检查/).waitFor();
    const reason = form.locator("#feishu-validation-action-reason");
    await form.locator(".feishu-approval-code input").fill("  ");
    assert.equal(await reason.innerText(), "请先填写 Approval Code。");
    assert.equal(await form.getByRole("button", { name: "保存并校验审批模板" }).isDisabled(), true);
    await form.locator(".feishu-approval-code input").fill("approval-saved");
    await form.getByLabel("姓名控件 ID", { exact: true }).fill("");
    assert.equal(await reason.innerText(), "请填写以下字段的控件 ID：姓名。");
    await form.getByLabel("姓名控件 ID", { exact: true }).fill("name");
    await form.getByLabel("性别选项男", { exact: true }).fill(" ");
    assert.equal(await reason.innerText(), "请填写性别选项男、女对应的值。");
    await form.getByLabel("性别选项男", { exact: true }).fill("female-option");
    assert.equal(await reason.innerText(), "男、女必须填写不同的性别选项值。");
  } finally { await context.close(); }
});

test("Feishu onboarding distinguishes saved, validated and enabled configuration", { timeout: 60_000 }, async () => {
  const fields = ["candidate_name", "gender", "department", "job_title", "phone", "email", "home_address"];
  const { context, page } = await openAuthenticatedPage({ width: 1280, height: 800 }, {
    onboardingConfig: {
      enabled: false,
      approval_code: "approval-saved",
      field_mapping: Object.fromEntries(fields.map((key) => [key, {
        control_id: `${key}-control`, type: key === "gender" ? "radio" : key === "department" ? "department" : key === "phone" ? "telephone" : key === "home_address" ? "textarea" : "input",
        ...(key === "gender" ? { options: { male: "male-option", female: "female-option" } } : {}),
      }])),
      department_mappings: [{ department_id: "dep-1", feishu_department_id: "od-existing" }],
      validation_status: "unvalidated", version: 3,
    },
  });
  try {
    await page.getByRole("button", { name: "设置", exact: true }).click();
    await page.getByRole("button", { name: "飞书集成", exact: true }).click();
    const form = page.locator(".feishu-onboarding-form");
    await form.waitFor();
    assert.equal(await form.locator(".feishu-validation-status").innerText(), "已保存 · 待校验");
    assert.equal(await page.getByLabel("启用飞书入职审批", { exact: true }).isDisabled(), true);
    await form.getByRole("button", { name: "校验审批模板", exact: true }).click();
    await form.getByText("审批模板校验通过，但入职审批尚未启用。", { exact: true }).waitFor();
    assert.equal(await form.locator(".feishu-validation-status").innerText(), "已校验 · 未启用");
    assert.equal(await page.getByLabel("启用飞书入职审批", { exact: true }).isChecked(), false);
    await page.getByLabel("启用飞书入职审批", { exact: true }).check();
    await form.getByRole("button", { name: "保存入职审批配置", exact: true }).click();
    await form.getByText("入职审批已启用。", { exact: true }).waitFor();
    assert.equal(await form.locator(".feishu-validation-status").innerText(), "已启用");
  } finally {
    await context.close();
  }
});

test("Feishu onboarding mapping can create a department without leaving the current draft", { timeout: 60_000 }, async () => {
  const { context, page } = await openAuthenticatedPage({ width: 1280, height: 800 }, {
    onboardingConfig: {
      enabled: true,
      approval_code: "approval-original",
      field_mapping: { candidate_name: { control_id: "candidate-control", type: "input" } },
      department_mappings: [{ department_id: "dep-1", feishu_department_id: "od-existing" }],
      validation_status: "valid",
      version: 3,
    },
  });
  try {
    await page.getByRole("button", { name: "设置", exact: true }).click();
    await page.getByRole("button", { name: "飞书集成", exact: true }).click();
    await page.getByRole("heading", { name: "入职审批", exact: true }).waitFor();
    await page.getByRole("button", { name: "新增本地部门", exact: true }).click();
    await page.getByLabel("新增部门名称", { exact: true }).fill("产品部");
    await page.getByRole("button", { name: "确认新增", exact: true }).click();
    await page.getByLabel("产品部对应飞书部门", { exact: true }).waitFor();
    assert.equal(await page.getByLabel("启用飞书入职审批", { exact: true }).isChecked(), false);
    assert.equal(await page.getByLabel("姓名控件 ID", { exact: true }).inputValue(), "candidate-control");
    assert.equal(await page.getByLabel("技术部对应飞书部门", { exact: true }).inputValue(), "od-existing");
    assert.equal(await page.getByLabel("产品部对应飞书部门", { exact: true }).inputValue(), "od-product");
    assert.equal(await page.locator(".feishu-approval-code input").inputValue(), "approval-original");
    assert.match(await page.locator(".feishu-onboarding-form").innerText(), /部门已创建，并已匹配同名飞书部门/);
    await page.setViewportSize({ width: 390, height: 844 });
    await assertNoHorizontalOverflow(page, "390px Feishu department quick create");
  } finally {
    await context.close();
  }
});

test("Feishu onboarding mapping explains inline creation when no department exists", { timeout: 60_000 }, async () => {
  const { context, page } = await openAuthenticatedPage({ width: 1280, height: 800 }, { initialDepartments: [] });
  try {
    await page.getByRole("button", { name: "设置", exact: true }).click();
    await page.getByRole("button", { name: "飞书集成", exact: true }).click();
    const emptyState = page.locator(".feishu-mapping-empty");
    await emptyState.waitFor();
    assert.match(await emptyState.innerText(), /点击“新增本地部门”直接创建/);
    assert.equal(await page.getByRole("button", { name: "新增本地部门", exact: true }).isEnabled(), true);
  } finally {
    await context.close();
  }
});

test("Feishu directory failure preserves the saved onboarding draft", { timeout: 60_000 }, async () => {
  const { context, page } = await openAuthenticatedPage({ width: 1280, height: 800 }, {
    onboardingConfig: {
      enabled: true,
      approval_code: "approval-existing",
      field_mapping: { candidate_name: { control_id: "candidate-control", type: "input" } },
      department_mappings: [{ department_id: "dep-1", feishu_department_id: "od-existing" }],
      validation_status: "valid",
      version: 3,
    },
    feishuDepartmentError: { status: 502, code: "feishu_department_directory_unavailable" },
  });
  try {
    await page.getByRole("button", { name: "设置", exact: true }).click();
    await page.getByRole("button", { name: "飞书集成", exact: true }).click();
    await page.getByRole("alert").filter({ hasText: /无法读取飞书通讯录部门/ }).waitFor();
    assert.equal(await page.locator(".feishu-approval-code input").inputValue(), "approval-existing");
    assert.equal(await page.getByLabel("姓名控件 ID", { exact: true }).inputValue(), "candidate-control");
    assert.equal(await page.getByLabel("技术部对应飞书部门", { exact: true }).inputValue(), "od-existing");
  } finally {
    await context.close();
  }
});

test("late Feishu directory matching merges into the latest edited onboarding draft", { timeout: 60_000 }, async () => {
  const { context, page } = await openAuthenticatedPage({ width: 1280, height: 800 }, {
    onboardingConfig: {
      enabled: false,
      approval_code: "approval-original",
      field_mapping: {},
      department_mappings: [],
      validation_status: "unvalidated",
      version: 1,
    },
    feishuDepartmentDelayMs: 2_000,
  });
  try {
    await page.getByRole("button", { name: "设置", exact: true }).click();
    await page.getByRole("button", { name: "飞书集成", exact: true }).click();
    await page.locator(".feishu-approval-code input").fill("approval-edited-before-directory-returned");
    await page.getByRole("status").filter({ hasText: /完成同名自动匹配检查/ }).waitFor();
    assert.equal(await page.locator(".feishu-approval-code input").inputValue(), "approval-edited-before-directory-returned");
    assert.equal(await page.getByLabel("技术部对应飞书部门", { exact: true }).inputValue(), "od-existing");
  } finally {
    await context.close();
  }
});

test("Feishu onboarding department creation keeps input and explains server failures", { timeout: 60_000 }, async () => {
  const scenarios = [
    [{ status: 409, code: "department_already_exists" }, /该部门已存在/],
    [{ status: 403, code: "forbidden" }, /没有新增部门的权限/],
    [{ status: 503, code: "service_unavailable" }, /部门创建失败，请稍后重试/],
  ];
  for (const [departmentCreateError, expectedMessage] of scenarios) {
    const { context, page } = await openAuthenticatedPage({ width: 1280, height: 800 }, { departmentCreateError });
    try {
      await page.getByRole("button", { name: "设置", exact: true }).click();
      await page.getByRole("button", { name: "飞书集成", exact: true }).click();
      await page.getByRole("button", { name: "新增本地部门", exact: true }).click();
      await page.getByLabel("新增部门名称", { exact: true }).fill("产品部");
      await page.getByRole("button", { name: "确认新增", exact: true }).click();
      await page.getByRole("status").filter({ hasText: expectedMessage }).waitFor();
      assert.equal(await page.getByLabel("新增部门名称", { exact: true }).inputValue(), "产品部");
      assert.equal(await page.getByRole("button", { name: "确认新增", exact: true }).isEnabled(), true);
    } finally {
      await context.close();
    }
  }
});

test("recruiting scope renders conditionally, validates departments, and refreshes after save", { timeout: 60_000 }, async () => {
  const { context, page } = await openAuthenticatedPage({ width: 1280, height: 800 });
  try {
    await page.getByRole("button", { name: "设置", exact: true }).click();
    const rows = page.locator(".users-table .settings-table-row");
    await rows.filter({ hasText: "林岚" }).waitFor();
    assert.match(await rows.filter({ hasText: "Admin" }).innerText(), /全公司/);
    assert.match(await rows.filter({ hasText: "林岚" }).innerText(), /1 个部门/);
    assert.match(await rows.filter({ hasText: "周宁" }).innerText(), /全公司/);
    assert.equal(await page.getByRole("button", { name: "配置负责范围", exact: true }).count(), 2);

    await page.getByRole("button", { name: "邀请成员", exact: true }).click();
    const inviteDrawer = page.getByRole("dialog", { name: "邀请成员", exact: true });
    const scopeGroup = inviteDrawer.getByRole("group", { name: "负责范围", exact: true });
    await scopeGroup.waitFor();
    await inviteDrawer.getByLabel("姓名", { exact: true }).fill("新专员");
    await inviteDrawer.getByLabel("工作邮箱", { exact: true }).fill("new@example.test");
    await inviteDrawer.getByLabel("所属部门", { exact: true }).selectOption("dep-1");
    await inviteDrawer.getByLabel(/^指定部门/).check();
    assert.equal(await inviteDrawer.getByRole("button", { name: "发送邀请", exact: true }).isDisabled(), true);
    assert.match(await inviteDrawer.getByRole("alert").innerText(), /至少选择一个负责部门/);
    await inviteDrawer.getByLabel("技术部", { exact: true }).check();
    assert.equal(await inviteDrawer.getByRole("button", { name: "发送邀请", exact: true }).isEnabled(), true);
    await inviteDrawer.getByLabel("角色", { exact: true }).selectOption("interviewer");
    assert.equal(await inviteDrawer.getByRole("group", { name: "负责范围", exact: true }).count(), 0);
    await inviteDrawer.getByLabel("角色", { exact: true }).selectOption("hiring_manager");
    assert.equal(await inviteDrawer.getByRole("group", { name: "负责范围", exact: true }).count(), 1);
    await inviteDrawer.getByRole("button", { name: "取消", exact: true }).click();

    await rows.filter({ hasText: "林岚" }).getByRole("button", { name: "配置负责范围", exact: true }).click();
    const scopeDrawer = page.getByRole("dialog", { name: "配置负责范围", exact: true });
    await scopeDrawer.getByLabel(/^全公司/).check();
    await scopeDrawer.getByRole("button", { name: "保存负责范围", exact: true }).click();
    await scopeDrawer.waitFor({ state: "detached" });
    assert.match(await rows.filter({ hasText: "林岚" }).innerText(), /全公司/);
  } finally {
    await context.close();
  }
});

test("system administrator can configure an existing member role and recruiting scope", { timeout: 60_000 }, async () => {
  const { context, page } = await openAuthenticatedPage({ width: 1280, height: 800 }, { currentRole: "system_admin" });
  try {
    const adminRow = page.locator(".users-table .settings-table-row").filter({ hasText: "Admin" });
    await adminRow.waitFor();
    await adminRow.getByRole("button", { name: "配置成员", exact: true }).click();
    const drawer = page.getByRole("dialog", { name: "配置成员", exact: true });
    await drawer.getByLabel("成员角色", { exact: true }).selectOption("recruiter");
    await drawer.getByLabel(/^指定部门/).check();
    await drawer.getByLabel("技术部", { exact: true }).check();
    await drawer.getByRole("button", { name: "保存成员配置", exact: true }).click();
    await drawer.waitFor({ state: "detached" });
    assert.match(await adminRow.innerText(), /HR 招聘专员/);
    assert.match(await adminRow.innerText(), /1 个部门/);
  } finally {
    await context.close();
  }
});

test("member recruiting scope cards stay within a narrow mobile viewport", { timeout: 60_000 }, async () => {
  const { context, page } = await openAuthenticatedPage({ width: 390, height: 844 });
  try {
    await page.getByRole("button", { name: "打开主导航", exact: true }).click();
    await page.getByRole("button", { name: "设置", exact: true }).click();
    await page.locator(".users-table .settings-table-row").first().waitFor();
    await assertNoHorizontalOverflow(page, "390px recruiting scope members");
    const boxes = await page.locator(".users-table .settings-table-row").evaluateAll((elements) => elements.map((element) => {
      const box = element.getBoundingClientRect();
      return { left: box.left, right: box.right, width: box.width };
    }));
    assert.ok(boxes.every((box) => box.left >= 0 && box.right <= 390 && box.width > 0), JSON.stringify(boxes));
    const gridAreas = await page.locator(".users-table .settings-table-row").first().evaluate((element) => getComputedStyle(element).gridTemplateAreas);
    assert.match(gridAreas, /scope/);
    assert.match(gridAreas, /action/);
  } finally {
    await context.close();
  }
});

test("audit controls align at desktop width and stack without overflow on narrow screens", { timeout: 60_000 }, async () => {
  for (const viewport of [{ width: 1280, height: 800 }, { width: 390, height: 844 }]) {
    const { context, page } = await openAuthenticatedPage(viewport);
    try {
      if (viewport.width <= 840) await page.getByRole("button", { name: "打开主导航", exact: true }).click();
      await page.getByRole("button", { name: "设置", exact: true }).click();
      await page.getByRole("button", { name: "审计与数据治理", exact: true }).click();
      const toolbar = page.locator(".audit-toolbar.governance-filters");
      await toolbar.waitFor();
      const approvalQueue = page.locator(".deletion-queue");
      await approvalQueue.waitFor();
      const [approvalBox, toolbarBox] = await Promise.all([approvalQueue.boundingBox(), toolbar.boundingBox()]);
      assert.ok(approvalBox && toolbarBox && approvalBox.y < toolbarBox.y, `deletion approvals must appear before audit logs: ${JSON.stringify({ approvalBox, toolbarBox })}`);
      const controls = toolbar.locator("input, select, button");
      const boxes = await controls.evaluateAll((elements) => elements.map((element) => {
        const box = element.getBoundingClientRect();
        return { top: box.top, right: box.right, bottom: box.bottom, left: box.left, width: box.width };
      }));
      if (viewport.width === 1280) {
        const bottoms = boxes.map((box) => box.bottom);
        assert.ok(Math.max(...bottoms) - Math.min(...bottoms) <= 1, `desktop audit control bottoms differ: ${JSON.stringify(boxes)}`);
      } else {
        const labelHeights = await toolbar.locator("label").evaluateAll((elements) => elements.map((element) => element.getBoundingClientRect().height));
        assert.ok(labelHeights.every((height) => height <= 70), `mobile audit labels are excessively tall: ${JSON.stringify(labelHeights)}`);
        for (let index = 1; index < boxes.length; index += 1) {
          assert.ok(boxes[index].top > boxes[index - 1].bottom, `mobile audit controls overlap: ${JSON.stringify(boxes)}`);
        }
        assert.ok(boxes.every((box) => box.left >= 0 && box.right <= viewport.width), `mobile audit control leaves viewport: ${JSON.stringify(boxes)}`);
      }
      await assertNoHorizontalOverflow(page, `${viewport.width}px audit`);
    } finally {
      await context.close();
    }
  }
});

test("successful deletion approval closes its drawers and cannot be approved twice", { timeout: 60_000 }, async () => {
  const { context, page, requestId } = await openDeletionApprovalPage();
  try {
    await page.getByRole("button", { name: new RegExp(requestId) }).click();
    const detail = page.getByRole("dialog", { name: "删除请求详情", exact: true });
    await detail.waitFor();
    await detail.getByRole("button", { name: "批准删除", exact: true }).click();
    const confirmation = page.getByRole("dialog", { name: "确认终止申请并删除候选人", exact: true });
    await confirmation.getByRole("button", { name: "终止 1 项申请并批准删除", exact: true }).click();
    await detail.waitFor({ state: "detached" });
    await confirmation.waitFor({ state: "detached" });

    const approvedRow = page.getByRole("button", { name: new RegExp(`${requestId}.*已批准，等待执行`) });
    await approvedRow.click();
    const approvedDetail = page.getByRole("dialog", { name: "删除请求详情", exact: true });
    await approvedDetail.waitFor();
    assert.equal(await approvedDetail.getByRole("button", { name: "批准删除", exact: true }).count(), 0);
  } finally {
    await context.close();
  }
});

test("Feishu enable control stays compact and aligned with responsive actions", { timeout: 60_000 }, async () => {
  for (const viewport of [{ width: 1280, height: 800 }, { width: 390, height: 844 }]) {
    const { context, page } = await openAuthenticatedPage(viewport);
    try {
      if (viewport.width <= 840) await page.getByRole("button", { name: "打开主导航", exact: true }).click();
      await page.getByRole("button", { name: "设置", exact: true }).click();
      await page.getByRole("button", { name: "飞书集成", exact: true }).click();
      const footer = page.locator(".feishu-form-footer");
      await footer.waitFor();
      const geometry = await footer.evaluate((element) => {
        const control = element.querySelector(".feishu-enabled-control").getBoundingClientRect();
        const checkbox = element.querySelector('input[type="checkbox"]').getBoundingClientRect();
        const actions = element.querySelector(".feishu-form-actions").getBoundingClientRect();
        const footerBox = element.getBoundingClientRect();
        return {
          control: { top: control.top, bottom: control.bottom, left: control.left, right: control.right },
          checkbox: { width: checkbox.width, height: checkbox.height, top: checkbox.top, bottom: checkbox.bottom },
          actions: { top: actions.top, bottom: actions.bottom, left: actions.left, right: actions.right },
          footer: { top: footerBox.top, bottom: footerBox.bottom, left: footerBox.left, right: footerBox.right },
        };
      });
      assert.ok(geometry.checkbox.width <= 18 && geometry.checkbox.height <= 18, `checkbox is oversized: ${JSON.stringify(geometry)}`);
      assert.ok(geometry.checkbox.top >= geometry.control.top && geometry.checkbox.bottom <= geometry.control.bottom, `checkbox leaves its label: ${JSON.stringify(geometry)}`);
      if (viewport.width === 1280) {
        assert.ok(geometry.actions.left > geometry.control.right, `desktop footer items overlap: ${JSON.stringify(geometry)}`);
        assert.ok(geometry.actions.top < geometry.control.bottom && geometry.actions.bottom > geometry.control.top, `desktop footer items are not aligned: ${JSON.stringify(geometry)}`);
      } else {
        assert.ok(geometry.actions.top > geometry.control.bottom, `mobile footer did not stack: ${JSON.stringify(geometry)}`);
      }
      assert.ok(geometry.control.left >= geometry.footer.left && geometry.actions.right <= geometry.footer.right, `footer content overflows: ${JSON.stringify(geometry)}`);
      await assertNoHorizontalOverflow(page, `${viewport.width}px Feishu settings`);
    } finally {
      await context.close();
    }
  }
});

test("import wizard steps and paired fields are geometrically aligned", { timeout: 60_000 }, async () => {
  for (const viewport of [{ width: 1280, height: 800 }, { width: 390, height: 844 }]) {
    const { context, page } = await openAuthenticatedPage(viewport);
    try {
      await page.getByRole("button", { name: "导入简历", exact: true }).click();
      const modal = page.getByRole("dialog", { name: "导入并筛选简历", exact: true });
      await modal.waitFor();
      const modalBox = await modal.boundingBox();
      assert.ok(modalBox && modalBox.x >= 0 && modalBox.y >= 0 && modalBox.x + modalBox.width <= viewport.width && modalBox.y + modalBox.height <= viewport.height, `modal leaves ${viewport.width}px viewport: ${JSON.stringify(modalBox)}`);

      const stepAlignment = await modal.locator(".wizard-steps > div").evaluateAll((steps) => steps.map((step) => {
        const stepBox = step.getBoundingClientRect();
        const markerBox = step.querySelector("span").getBoundingClientRect();
        return Math.abs((stepBox.left + stepBox.width / 2) - (markerBox.left + markerBox.width / 2));
      }));
      assert.ok(stepAlignment.every((delta) => delta <= 46), `step markers are not centered with their labels: ${JSON.stringify(stepAlignment)}`);

      const selects = await modal.locator(".wizard-grid select").evaluateAll((elements) => elements.map((element) => {
        const box = element.getBoundingClientRect();
        return { top: box.top, left: box.left, right: box.right, width: box.width };
      }));
      if (viewport.width === 1280) {
        assert.ok(Math.abs(selects[0].top - selects[1].top) <= 1, `paired fields have different tops: ${JSON.stringify(selects)}`);
      } else {
        assert.ok(selects[1].top > selects[0].top, `narrow fields did not stack: ${JSON.stringify(selects)}`);
        assert.ok(selects[1].top - selects[0].top <= 85, `narrow fields have excessive vertical spacing: ${JSON.stringify(selects)}`);
        assert.ok(selects.every((box) => box.left >= 0 && box.right <= viewport.width), `narrow field leaves viewport: ${JSON.stringify(selects)}`);
      }
      await assertNoHorizontalOverflow(page, `${viewport.width}px import`);
    } finally {
      await context.close();
    }
  }
});
