import assert from "node:assert/strict";
import test, { after, before } from "node:test";
import { fileURLToPath } from "node:url";
import { chromium } from "playwright";
import { createServer } from "vite";

let browser;
let vite;
let baseUrl;
const prototypeRoot = fileURLToPath(new URL("../", import.meta.url));

before(async () => {
  vite = await createServer({
    root: prototypeRoot,
    logLevel: "silent",
    server: { host: "127.0.0.1", port: 0 },
  });
  await vite.listen();
  baseUrl = vite.resolvedUrls.local[0];
  browser = await chromium.launch({ headless: true });
});

async function openAuthenticatedPage(viewport) {
  const context = await browser.newContext({ viewport });
  await context.route("**/api/v1/**", async (route) => {
    const pathname = new URL(route.request().url()).pathname.replace(/\/$/, "");
    if (pathname === "/api/v1/me") {
      await route.fulfill({
        status: 200,
        contentType: "application/json",
        headers: { "x-csrf-token": "settings-regression" },
        body: JSON.stringify({ data: { id: "user-1", display_name: "Admin", roles: ["recruiting_admin"] } }),
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
    if (pathname === "/api/v1/settings/departments") {
      await route.fulfill({ status: 200, contentType: "application/json", body: JSON.stringify({ data: [{ id: "dep-1", name: "技术部", parent_id: null, member_count: 1, job_count: 1 }] }) });
      return;
    }
    if (pathname === "/api/v1/settings/users") {
      await route.fulfill({ status: 200, contentType: "application/json", body: JSON.stringify({ data: [{ id: "user-1", display_name: "Admin", email: "admin@example.test", department_id: "dep-1", department_name: "技术部", roles: ["recruiting_admin"], status: "active" }] }) });
      return;
    }
    if (pathname === "/api/v1/settings/integrations/feishu") {
      await route.fulfill({ status: 200, contentType: "application/json", body: JSON.stringify({ data: { enabled: false, configured: false, app_id: "", redirect_uri: "", calendar_id: "" } }) });
      return;
    }
    await route.fulfill({ status: 503, contentType: "application/problem+json", body: JSON.stringify({ status: 503 }) });
  });
  const page = await context.newPage();
  await page.goto(baseUrl, { waitUntil: "domcontentloaded" });
  await page.getByRole("heading", { name: "工作台", exact: true }).waitFor();
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
    await drawer.getByLabel("部门").selectOption("dep-1");
    assert.equal(await drawer.getByRole("button", { name: "发送邀请", exact: true }).isEnabled(), true);
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
