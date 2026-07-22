import test, { after, before } from "node:test";
import assert from "node:assert/strict";
import { fileURLToPath } from "node:url";
import { chromium } from "playwright";
import { createServer } from "vite";

const prototypeRoot = fileURLToPath(new URL("../", import.meta.url));
const departments = [{ id: "00000000-0000-4000-8000-000000000201", name: "技术部", parent_id: null, member_count: 6, job_count: 3 }];
const users = [{ id: "user-1", display_name: "Admin", email: "admin@example.test", department_id: departments[0].id, department_name: "技术部", roles: ["recruiting_admin"], status: "active" }];
const notificationJobId = "10000000-0000-4000-8000-000000000001";
const standardWorkflowTemplate = { id: "50000000-0000-4000-8000-000000000001", name: "标准社招流程", rounds: ["一面"], status: "active", version: 1 };

function notificationCandidate(stage, index, name) {
  return {
    application_id: `20000000-0000-4000-8000-00000000000${index}`,
    candidate_id: `30000000-0000-4000-8000-00000000000${index}`,
    job_id: notificationJobId,
    display_name: name,
    current_title: "AI 工程师",
    location: "北京",
    source: "upload",
    stage,
    updated_at: "2026-07-17T03:00:00Z",
  };
}

function notificationWorkbench() {
  const review = notificationCandidate("review", 1, "陈曦");
  const reviewTask = {
    ...review,
    task_id: "40000000-0000-4000-8000-000000000001",
    ai_status: "succeeded",
    config_warning: false,
    candidate_link: `/candidates/${review.candidate_id}?tab=evidence&application=${review.application_id}&job=${review.job_id}`,
  };
  const passed = notificationCandidate("passed", 2, "李嘉杨");
  const notification = (item, version) => ({ ...item, notification_version: version.repeat(64) });
  const empty = { count: 0, items: [] };
  return {
    generated_at: "2026-07-17T04:00:00Z",
    jobs: [{
      id: notificationJobId,
      title: "AI 工程师",
      department_name: "技术部",
      status: "open",
      updated_at: "2026-07-17T03:30:00Z",
      active_count: 2,
      stages: { new: empty, review: { count: 1, items: [review] }, contact: empty, interview_pending: empty, interviewing: empty, decision: empty, passed: { count: 1, items: [passed] } },
    }],
    tasks: { review: { count: 1, items: [reviewTask] }, interview_pending: empty, decision: empty, passed: { count: 1, items: [passed] } },
    notifications: {
      review: { count: 1, items: [notification(reviewTask, "a")] },
      interview_pending: empty,
      decision: empty,
      passed: { count: 1, items: [notification(passed, "b")] },
    },
    interviews: { available: false, upcoming: [], pending_feedback: [] },
  };
}
let browser;
let vite;
let baseUrl;

before(async () => {
  vite = await createServer({ root: prototypeRoot, logLevel: "silent", server: { host: "127.0.0.1", port: 0 } });
  await vite.listen();
  baseUrl = vite.resolvedUrls.local[0];
  browser = await chromium.launch({ headless: true });
});

after(async () => {
  await browser?.close();
  await vite?.close();
});

async function openPage({ viewport = { width: 1280, height: 800 }, anonymous = false, roles = ["recruiting_admin"], workbench = null, candidates = null, onRequest } = {}) {
  const context = await browser.newContext({ viewport });
  let departmentDetail = {
    ...departments[0],
    status: "active",
    members: [{ id: users[0].id, name: users[0].display_name, roles: users[0].roles, status: "active" }],
    jobs: [{ id: "job-1", title: "AI 工程师", status: "open" }],
  };
  await context.route("**/api/v1/**", async (route) => {
    const request = route.request();
    const pathname = new URL(request.url()).pathname.replace(/\/$/, "");
    onRequest?.(pathname, request);
    if (pathname === "/api/v1/me") {
      if (anonymous) return route.fulfill({ status: 401, contentType: "application/problem+json", body: JSON.stringify({ status: 401 }) });
      return route.fulfill({ status: 200, contentType: "application/json", headers: { "x-csrf-token": "closure-csrf" }, body: JSON.stringify({ data: { ...users[0], roles, organization: { id: "org-1", slug: "acme", name: "星河科技" }, department: { id: departments[0].id, name: "技术部" } } }) });
    }
    if (pathname === "/api/v1/auth/config") return route.fulfill({ status: 200, contentType: "application/json", body: JSON.stringify({ data: { default_organization: { slug: "acme", name: "Acme" } } }) });
    if (pathname === "/api/v1/settings/departments") {
      if (request.method() === "POST") return route.fulfill({ status: 200, contentType: "application/json", body: JSON.stringify({ data: { id: "dep-new", ...request.postDataJSON(), member_count: 0, job_count: 0 } }) });
      return route.fulfill({ status: 200, contentType: "application/json", body: JSON.stringify({ data: departments }) });
    }
    if (pathname === `/api/v1/settings/departments/${departments[0].id}`) {
      if (request.method() === "PATCH") departmentDetail = { ...departmentDetail, ...request.postDataJSON() };
      return route.fulfill({ status: 200, contentType: "application/json", body: JSON.stringify({ data: departmentDetail }) });
    }
    if (pathname === "/api/v1/settings/users") {
      if (request.method() === "POST") return route.fulfill({ status: 200, contentType: "application/json", body: JSON.stringify({ data: { user: { id: "user-2", ...request.postDataJSON(), department_name: "技术部", roles: [request.postDataJSON().role], status: "invited" }, invitation: { token: "invite-once", expires_at: "2026-07-18T08:00:00Z" } } }) });
      return route.fulfill({ status: 200, contentType: "application/json", body: JSON.stringify({ data: users }) });
    }
    if (pathname === "/api/v1/settings/workflow-templates") return route.fulfill({ status: 200, contentType: "application/json", body: JSON.stringify({ data: [standardWorkflowTemplate] }) });
    if (pathname === "/api/v1/jobs") return route.fulfill({ status: 200, contentType: "application/json", body: JSON.stringify({ data: [], meta: { departments: [], owners: [], status_counts: {}, next_cursor: null } }) });
    if (pathname === "/api/v1/candidates" && candidates) return route.fulfill({ status: 200, contentType: "application/json", body: JSON.stringify({ data: candidates, meta: { limit: 50, next_cursor: null, owners: [] } }) });
    if (pathname === "/api/v1/workbench" && workbench) return route.fulfill({ status: 200, contentType: "application/json", body: JSON.stringify({ data: workbench }) });
    if (/^\/api\/v1\/notifications\/workbench\/[^/]+\/read$/.test(pathname) && request.method() === "PUT") return route.fulfill({ status: 200, contentType: "application/json", body: JSON.stringify({ data: { application_id: pathname.split("/")[5], version: request.postDataJSON().version, read_at: "2026-07-22T01:00:00Z" } }) });
    if (pathname === "/api/v1/auth/invitations/accept") return route.fulfill({ status: 200, contentType: "application/json", body: JSON.stringify({ data: { email: "invitee@example.test" } }) });
    if (pathname === "/api/v1/me/password") return route.fulfill({ status: 204, body: "" });
    return route.fulfill({ status: 503, contentType: "application/problem+json", body: JSON.stringify({ status: 503 }) });
  });
  const page = await context.newPage();
  return { context, page };
}

test("notification bell opens actionable tasks and candidate navigation", { timeout: 60_000 }, async () => {
  let readRequest;
  const { context, page } = await openPage({
    workbench: notificationWorkbench(),
    onRequest(pathname, request) {
      if (/^\/api\/v1\/notifications\/workbench\/[^/]+\/read$/.test(pathname)) readRequest = request;
    },
  });
  try {
    await page.goto(baseUrl);
    const trigger = page.getByRole("button", { name: "查看 2 项待处理事项", exact: true });
    await trigger.waitFor();
    await trigger.click();
    const panel = page.getByRole("dialog", { name: "待处理事项", exact: true });
    await panel.waitFor();
    const bounds = await panel.boundingBox();
    assert.ok(bounds && bounds.x >= 0 && bounds.y > 0);
    assert.ok(bounds.x + bounds.width <= 1280);
    assert.ok(bounds.y + bounds.height <= 800);
    await panel.getByRole("button", { name: /陈曦/ }).click();
    await page.waitForURL(/\/candidates\/30000000-0000-4000-8000-000000000001/);
    assert.equal(await panel.count(), 0);
    assert.equal(readRequest.method(), "PUT");
    assert.deepEqual(readRequest.postDataJSON(), { version: "a".repeat(64) });
    assert.equal(readRequest.headers()["x-csrf-token"], "closure-csrf");
  } finally { await context.close(); }
});

test("workbench board shows the five actionable stages while list and totals keep their behavior", { timeout: 60_000 }, async () => {
  const workbench = notificationWorkbench();
  workbench.jobs[0].stages.new = { count: 1, items: [notificationCandidate("new", 3, "王晨")] };
  workbench.jobs[0].stages.contact = { count: 1, items: [notificationCandidate("contact", 4, "赵敏")] };
  workbench.jobs[0].active_count = 4;
  const { context, page } = await openPage({ workbench });
  try {
    await page.goto(baseUrl);
    const board = page.getByLabel("候选人招聘阶段", { exact: true });
    await board.waitFor();
    assert.deepEqual(await board.locator(".stage > header > strong").allTextContents(), [
      "待用人经理评审",
      "待安排面试",
      "面试流程中",
      "待用人经理录用决策",
      "待录用确认",
    ]);
    assert.equal(await board.getByText("新简历", { exact: true }).count(), 0);
    assert.equal(await board.getByText("待确认候选人意向", { exact: true }).count(), 0);
    assert.equal(await board.locator(".candidate-card").count(), 2);
    assert.match(await page.locator(".dashboard-efficiency").textContent(), /当前流程候选人4覆盖 4 个阶段/);

    await page.getByRole("button", { name: "列表", exact: true }).click();
    const list = page.locator(".list-view");
    assert.equal(await list.locator(".list-row").count(), 4);
    assert.equal(await list.getByText("新简历", { exact: true }).count(), 1);
    assert.equal(await list.getByText("待确认候选人意向", { exact: true }).count(), 1);
    assert.equal(await list.getByText("待用人经理评审", { exact: true }).count(), 1);
    assert.equal(await list.getByText("待录用确认", { exact: true }).count(), 1);
    await list.getByRole("button", { name: /陈曦/ }).click();
    await page.waitForURL(/\/candidates\/30000000-0000-4000-8000-000000000001/);
  } finally { await context.close(); }
});

test("organization settings load real data, restrict invite roles, and show the one-time invitation link", { timeout: 60_000 }, async () => {
  let inviteRequest;
  const { context, page } = await openPage({ onRequest(pathname, request) { if (pathname === "/api/v1/settings/users" && request.method() === "POST") inviteRequest = request; } });
  try {
    await page.goto(baseUrl);
    await page.getByRole("button", { name: "设置", exact: true }).click();
    await page.getByRole("heading", { name: "组织与权限", exact: true }).waitFor();
    assert.equal(await page.locator(".organization-settings .users-table").getByText("Admin", { exact: true }).count(), 1);
    await page.getByRole("button", { name: "邀请成员", exact: true }).click();
    const drawer = page.getByRole("dialog", { name: "邀请成员", exact: true });
    await drawer.getByLabel("姓名", { exact: true }).fill("周宁");
    await drawer.getByLabel("工作邮箱", { exact: true }).fill("zhou@example.test");
    await drawer.getByLabel("部门").selectOption(departments[0].id);
    assert.deepEqual(await drawer.getByLabel("角色").locator("option").allTextContents(), ["HR 招聘专员", "用人经理", "面试官"]);
    await drawer.getByRole("button", { name: "发送邀请", exact: true }).click();
    const link = drawer.getByLabel("邀请链接", { exact: true });
    await link.waitFor();
    assert.match(await link.inputValue(), /#invite=invite-once$/);
    assert.match(await drawer.getByText(/48 小时/).textContent(), /48 小时/);
    assert.equal(inviteRequest.headers()["idempotency-key"].length > 0, true);
    assert.deepEqual(inviteRequest.postDataJSON(), { display_name: "周宁", email: "zhou@example.test", department_id: departments[0].id, role: "recruiter" });
  } finally { await context.close(); }
});

test("department cards open management details and support rename and reversible deactivation", { timeout: 60_000 }, async () => {
  const updates = [];
  const { context, page } = await openPage({
    onRequest(pathname, request) {
      if (pathname === `/api/v1/settings/departments/${departments[0].id}` && request.method() === "PATCH") updates.push(request.postDataJSON());
    },
  });
  try {
    await page.goto(`${baseUrl}settings/organization/departments`);
    await page.getByRole("button", { name: /技术部.*6 名成员.*3 个职位/ }).click();
    const drawer = page.getByRole("dialog", { name: "技术部", exact: true });
    await drawer.getByText("Admin", { exact: true }).waitFor();
    assert.match(await drawer.textContent(), /AI 工程师/);
    await drawer.getByLabel("部门名称", { exact: true }).fill("平台部");
    await drawer.getByRole("button", { name: "保存名称", exact: true }).click();
    const renamedDrawer = page.getByRole("dialog", { name: "平台部", exact: true });
    await renamedDrawer.getByRole("button", { name: "停用部门", exact: true }).click();
    await renamedDrawer.getByText("该部门已停用，历史成员和职位仍保留。", { exact: true }).waitFor();
    assert.deepEqual(updates, [{ name: "平台部" }, { status: "inactive" }]);
    await renamedDrawer.getByRole("button", { name: "重新启用", exact: true }).waitFor();
  } finally { await context.close(); }
});

test("anonymous invitation flow validates passwords, clears the hash, and prefills login email", { timeout: 60_000 }, async () => {
  let acceptPayload;
  const { context, page } = await openPage({ anonymous: true, onRequest(pathname, request) { if (pathname === "/api/v1/auth/invitations/accept") acceptPayload = request.postDataJSON(); } });
  try {
    await page.goto(`${baseUrl}#invite=invite-once`);
    await page.getByRole("heading", { name: "设置登录密码", exact: true }).waitFor();
    await page.getByLabel("新密码", { exact: true }).fill("short");
    await page.getByLabel("确认新密码", { exact: true }).fill("different");
    assert.equal(await page.getByRole("button", { name: "设置密码", exact: true }).isEnabled(), false);
    await page.getByLabel("新密码", { exact: true }).fill("secure-pass-123");
    await page.getByLabel("确认新密码", { exact: true }).fill("secure-pass-123");
    await page.getByRole("button", { name: "设置密码", exact: true }).click();
    await page.getByRole("heading", { name: "登录工作台", exact: true }).waitFor();
    assert.equal(new URL(page.url()).hash, "");
    assert.equal(await page.locator('input[name="email"]').inputValue(), "invitee@example.test");
    assert.deepEqual(acceptPayload, { token: "invite-once", password: "secure-pass-123" });
  } finally { await context.close(); }
});

test("profile settings are reachable on desktop and mobile, trap focus, close with Escape, and change password", { timeout: 60_000 }, async () => {
  for (const viewport of [{ width: 1280, height: 800 }, { width: 390, height: 844 }]) {
    const { context, page } = await openPage({ viewport, roles: ["interviewer"] });
    try {
      await page.goto(baseUrl);
      const trigger = page.getByRole("button", { name: "个人设置", exact: true }).filter({ visible: true });
      await trigger.click();
      const drawer = page.getByRole("dialog", { name: "个人设置", exact: true });
      await drawer.waitFor();
      assert.equal(await page.evaluate(() => getComputedStyle(document.body).fontSize), "16px");
      assert.equal(await page.evaluate(() => document.documentElement.scrollWidth === document.documentElement.clientWidth), true);
      assert.match(await drawer.textContent(), /Admin.*admin@example\.test.*星河科技.*技术部.*面试官/s);
      await drawer.getByRole("tab", { name: "账号安全", exact: true }).click();
      await drawer.getByLabel("当前密码", { exact: true }).fill("old-password");
      await drawer.getByLabel("新密码", { exact: true }).fill("new-password-123");
      await drawer.getByLabel("确认新密码", { exact: true }).fill("new-password-123");
      await drawer.getByRole("button", { name: "修改密码", exact: true }).click();
      await drawer.getByRole("status").filter({ hasText: "密码已修改" }).waitFor();
      await page.keyboard.press("Escape");
      await drawer.waitFor({ state: "hidden" });
      assert.equal(await trigger.evaluate((element) => element === document.activeElement), true);
    } finally { await context.close(); }
  }
});

test("new jobs default to the standard process and manage departments navigates to the department tab", { timeout: 60_000 }, async () => {
  const { context, page } = await openPage();
  try {
    await page.goto(baseUrl);
    await page.getByRole("button", { name: "职位", exact: true }).click();
    await page.getByRole("button", { name: "新建职位", exact: true }).click();
    await page.getByRole("heading", { name: "招聘配置", exact: true }).waitFor();
    const departmentSelect = page.getByLabel("所属部门", { exact: true });
    await departmentSelect.locator("option", { hasText: "技术部" }).waitFor({ state: "attached" });
    assert.deepEqual(await departmentSelect.locator("option").allTextContents(), ["未分配部门", "技术部"]);
    const process = page.getByLabel("流程模板", { exact: true });
    assert.equal(await process.evaluate((element) => element.tagName), "SELECT");
    assert.equal(await process.inputValue(), standardWorkflowTemplate.id);
    assert.match(await page.getByText("阶段摘要", { exact: true }).locator("..").textContent(), /新简历.*用人经理评审.*一面.*用人经理录用决策/s);
    assert.match(await page.getByText("AI 简历评估", { exact: true }).locator("..").textContent(), /LLM 生成评分、结论与依据/);
    await page.getByLabel("职位名称", { exact: true }).fill("平台工程师");
    await page.getByRole("button", { name: /管理部门/ }).click();
    await page.getByRole("heading", { name: "组织与权限", exact: true }).waitFor();
    assert.equal(new URL(page.url()).pathname, "/settings/organization/departments");
    assert.equal(await page.getByRole("tab", { name: "部门", exact: true }).getAttribute("aria-selected"), "true");
    await page.getByRole("button", { name: "返回职位编辑", exact: true }).click();
    await page.getByRole("heading", { name: "招聘配置", exact: true }).waitFor();
    assert.equal(new URL(page.url()).pathname, "/jobs/new");
    assert.equal(await page.getByLabel("职位名称", { exact: true }).inputValue(), "平台工程师");
  } finally { await context.close(); }
});

test("candidate deep links restore tabs and URL filters while browser history follows shell navigation", { timeout: 60_000 }, async () => {
  const { context, page } = await openPage();
  try {
    await page.goto(`${baseUrl}candidates/CAN-001?tab=timeline`);
    await page.getByRole("heading", { name: "候选人详情", exact: true }).waitFor();
    assert.equal(await page.getByRole("button", { name: "时间线", exact: true }).getAttribute("class"), "active");
    await page.getByRole("button", { name: "返回候选人列表", exact: true }).click();
    await page.getByRole("heading", { name: "候选人", exact: true, level: 1 }).waitFor();
    assert.equal(new URL(page.url()).pathname, "/candidates");
    await page.getByLabel("阶段筛选", { exact: true }).selectOption("待复核");
    assert.equal(new URL(page.url()).searchParams.get("stage"), "待复核");
    await page.getByRole("button", { name: "职位", exact: true }).click();
    await page.getByRole("heading", { name: "职位", exact: true }).waitFor();
    assert.equal(new URL(page.url()).pathname, "/jobs");
    await page.goBack();
    await page.getByRole("heading", { name: "候选人", exact: true, level: 1 }).waitFor();
    assert.equal(new URL(page.url()).searchParams.get("stage"), "待复核");
  } finally { await context.close(); }
});

test("candidate list keeps long stage labels separate from AI scores", { timeout: 60_000 }, async () => {
  const candidates = [{
    id: "30000000-0000-4000-8000-000000000099",
    display_name: "周雨晴",
    current_title: "高级算法工程师",
    location: "深圳",
    updated_at: "2026-07-22T03:00:00Z",
    application: {
      id: "20000000-0000-4000-8000-000000000099",
      job_id: notificationJobId,
      job_title: "AI 工程师",
      owner_id: users[0].id,
      owner_name: users[0].display_name,
      stage: "review",
      source: "本地上传",
      updated_at: "2026-07-22T03:00:00Z",
      ai_score: 78,
      ai_recommendation: "建议评审",
    },
  }];
  const { context, page } = await openPage({ viewport: { width: 1280, height: 800 }, candidates });
  try {
    await page.goto(`${baseUrl}candidates`);
    const row = page.locator(".candidate-table-row").filter({ hasText: "周雨晴" });
    await row.waitFor();
    const stageBox = await row.locator(".candidate-stage").boundingBox();
    const scoreBox = await row.locator(".candidate-score").boundingBox();
    assert.ok(stageBox && scoreBox);
    assert.ok(stageBox.x + stageBox.width + 12 <= scoreBox.x, `stage ends at ${stageBox.x + stageBox.width}, score starts at ${scoreBox.x}`);
    assert.equal(await page.evaluate(() => document.documentElement.scrollWidth === document.documentElement.clientWidth), true);
  } finally { await context.close(); }
});
