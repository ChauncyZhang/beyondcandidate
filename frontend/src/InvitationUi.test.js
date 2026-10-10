import test, { before, after } from "node:test";
import assert from "node:assert/strict";
import { fileURLToPath } from "node:url";
import { chromium } from "playwright";
import { createServer } from "vite";

let browser, vite, baseUrl;
before(async () => {
  vite = await createServer({ root: fileURLToPath(new URL("../", import.meta.url)), logLevel: "silent", server: { host: "127.0.0.1", port: 0 } });
  await vite.listen();
  baseUrl = vite.resolvedUrls.local[0];
  browser = await chromium.launch({ headless: true });
});
after(async () => { await browser?.close(); await vite?.close(); });

async function openPage({ role = "recruiting_admin", viewport = { width: 1440, height: 900 }, anonymous = false, reinviteError = null, acceptError = null, reinviteResponseBarrier } = {}) {
  const context = await browser.newContext({ viewport });
  const requests = [];
  await context.route("**/api/v1/**", async (route) => {
    const request = route.request();
    const path = new URL(request.url()).pathname;
    let data = [];
    const problem = (error) => route.fulfill({ status: error.status, contentType: "application/problem+json", body: JSON.stringify(error) });
    if (path === "/api/v1/me") {
      if (anonymous) return problem({ status: 401, code: "authentication_required" });
      data = { id: "current", display_name: "Current", roles: [role] };
    } else if (path === "/api/v1/settings/users") {
      if (request.method() === "POST") return problem({ status: 409, code: "user_email_already_exists" });
      data = [
        { id: "pending", display_name: "Pending", email: "pending@example.test", roles: ["recruiter"], status: "invited" },
        { id: "admin-pending", display_name: "Pending admin", email: "admin@example.test", roles: ["system_admin"], status: "invited" },
        { id: "active", display_name: "Active", roles: ["recruiter"], status: "active" },
        { id: "disabled", display_name: "Disabled", roles: ["recruiter"], status: "disabled" },
      ];
    } else if (path === "/api/v1/settings/departments") {
      data = [{ id: "dep", name: "Test department", status: "active" }];
    } else if (path === "/api/v1/settings/users/pending/invitation") {
      requests.push({ method: request.method(), headers: request.headers() });
      await reinviteResponseBarrier;
      if (reinviteError) return problem(reinviteError);
      data = { invitation: { token: "fresh+token/=&", expires_at: "2026-10-02T10:00:00Z" } };
    } else if (path === "/api/v1/auth/invitations/accept") {
      requests.push(request.postDataJSON());
      return problem(acceptError || { status: 422, code: "invitation_invalid_or_expired" });
    } else if (path === "/api/v1/auth/config") {
      data = { default_organization: { slug: "test", name: "Test" } };
    }
    await route.fulfill({ status: 200, contentType: "application/json", headers: { "X-CSRF-Token": "browser-csrf" }, body: JSON.stringify({ data }) });
  });
  const page = await context.newPage();
  return { context, page, requests };
}

for (const viewport of [{ width: 1440, height: 900 }, { width: 390, height: 844 }]) {
  test(`reinvite confirmation, CSRF, fresh URL round-trip and layout at ${viewport.width}px`, { timeout: 30_000 }, async () => {
    let releaseResponse;
    const reinviteResponseBarrier = new Promise((resolve) => { releaseResponse = resolve; });
    const { context, page, requests } = await openPage({ viewport, reinviteResponseBarrier });
    try {
      await page.goto(`${baseUrl}settings/organization/members`);
      const row = page.locator(".settings-table-row").filter({ has: page.getByText("Pending", { exact: true }) });
      const button = row.getByRole("button", { name: "重新邀请", exact: true });
      await button.waitFor();
      assert.equal(await page.getByRole("button", { name: "重新邀请", exact: true }).count(), 1);
      const actionsContained = await page.locator(".users-table .member-row-action button").evaluateAll((buttons) => buttons.every((button) => {
        const bounds = button.getBoundingClientRect();
        const cell = button.parentElement.getBoundingClientRect();
        const table = button.closest(".users-table").getBoundingClientRect();
        const text = document.createRange();
        text.selectNodeContents(button);
        const textBounds = text.getBoundingClientRect();
        return bounds.left >= Math.max(cell.left, table.left, 0) - 1
          && bounds.right <= Math.min(cell.right, table.right, window.innerWidth) + 1
          && textBounds.left >= bounds.left - 1 && textBounds.right <= bounds.right + 1
          && textBounds.top >= bounds.top - 1 && textBounds.bottom <= bounds.bottom + 1
          && button.scrollWidth <= button.clientWidth;
      }));
      assert.equal(actionsContained, true, "member action buttons and text must fit their cells and visible table");
      await page.screenshot({ path: fileURLToPath(new URL(`../.tmp/invitation-frontend-20260930/members-${viewport.width}.png`, import.meta.url)), fullPage: true });
      await button.click();
      const dialog = page.getByRole("dialog", { name: "重新邀请成员", exact: true });
      assert.match(await dialog.textContent(), /48 小时.*旧邀请链接将失效/);
      assert.equal(requests.length, 0);
      const submit = dialog.getByRole("button", { name: "生成新邀请", exact: true });
      const requestStarted = page.waitForRequest((request) => new URL(request.url()).pathname === "/api/v1/settings/users/pending/invitation");
      await submit.click();
      await requestStarted;
      assert.equal(await dialog.getByRole("button", { name: "正在生成…" }).isDisabled(), true);
      releaseResponse();
      const link = dialog.getByLabel("邀请链接", { exact: true });
      await link.waitFor();
      assert.equal(requests.length, 1);
      assert.equal(requests[0].method, "POST");
      assert.equal(requests[0].headers["x-csrf-token"], "browser-csrf");
      const url = await link.inputValue();
      assert.equal(new URLSearchParams(new URL(url).hash.slice(1)).get("invite"), "fresh+token/=&");
      assert.match(await dialog.textContent(), /旧链接会在重新邀请后失效/);
      assert.ok(await page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth));
      const bounds = await dialog.boundingBox();
      assert.ok(bounds.x >= 0 && bounds.x + bounds.width <= viewport.width + 1);
      await page.screenshot({ path: fileURLToPath(new URL(`../.tmp/invitation-frontend-20260930/dialog-${viewport.width}.png`, import.meta.url)), fullPage: true });
      await page.keyboard.press("Escape");
      await dialog.waitFor({ state: "detached" });
      assert.equal(await button.evaluate((element) => element === document.activeElement), true);
      const invitee = await openPage({ anonymous: true, viewport });
      try {
        await invitee.page.goto(url);
        await invitee.page.getByRole("heading", { name: "设置登录密码", exact: true }).waitFor();
        await invitee.page.getByLabel("新密码", { exact: true }).fill("secure-pass-123");
        await invitee.page.getByLabel("确认新密码", { exact: true }).fill("secure-pass-123");
        await invitee.page.getByRole("button", { name: "设置密码", exact: true }).click();
        await invitee.page.getByRole("alert").waitFor();
        assert.deepEqual(invitee.requests[0], { token: "fresh+token/=&", password: "secure-pass-123" });
        assert.match(await invitee.page.getByRole("alert").textContent(), /过期.*最新链接/);
      } finally { await invitee.context.close(); }
    } finally { releaseResponse(); await context.close(); }
  });
}

test("system admin can reinvite admin targets; read-only HR sees no reinvite", { timeout: 30_000 }, async () => {
  for (const [role, count] of [["system_admin", 2], ["recruiter", 0]]) {
    const { context, page } = await openPage({ role });
    try {
      await page.goto(`${baseUrl}settings/organization/members`);
      await page.getByText("Pending", { exact: true }).waitFor();
      assert.equal(await page.getByRole("button", { name: "重新邀请", exact: true }).count(), count);
    } finally { await context.close(); }
  }
});

test("stale invited status produces an actionable dialog error without showing a token", { timeout: 30_000 }, async () => {
  const { context, page } = await openPage({ reinviteError: { status: 409, code: "user_not_invited" } });
  try {
    await page.goto(`${baseUrl}settings/organization/members`);
    await page.getByRole("button", { name: "重新邀请", exact: true }).click();
    const dialog = page.getByRole("dialog", { name: "重新邀请成员", exact: true });
    await dialog.getByRole("button", { name: "生成新邀请", exact: true }).click();
    await dialog.getByRole("alert").waitFor();
    assert.match(await dialog.getByRole("alert").textContent(), /刷新成员列表/);
    assert.equal(await dialog.getByLabel("邀请链接", { exact: true }).count(), 0);
    assert.equal(await dialog.getByRole("button", { name: "生成新邀请", exact: true }).isEnabled(), true);
  } finally { await context.close(); }
});

test("acceptance validation failure is not mislabeled as an expired invitation", { timeout: 30_000 }, async () => {
  const { context, page } = await openPage({ anonymous: true, acceptError: { status: 422, code: "validation_error" } });
  try {
    await page.goto(`${baseUrl}#invite=valid-token`);
    await page.getByLabel("新密码", { exact: true }).fill("secure-pass-123");
    await page.getByLabel("确认新密码", { exact: true }).fill("secure-pass-123");
    await page.getByRole("button", { name: "设置密码", exact: true }).click();
    await page.getByRole("alert").waitFor();
    assert.match(await page.getByRole("alert").textContent(), /密码设置未通过验证/);
    assert.doesNotMatch(await page.getByRole("alert").textContent(), /过期/);
  } finally { await context.close(); }
});

test("duplicate-create guidance leads back to the existing pending member row", { timeout: 30_000 }, async () => {
  const { context, page } = await openPage();
  try {
    await page.goto(`${baseUrl}settings/organization/members`);
    await page.getByRole("button", { name: "邀请成员", exact: true }).click();
    const dialog = page.getByRole("dialog", { name: "邀请成员", exact: true });
    await dialog.getByLabel("姓名", { exact: true }).fill("Pending");
    await dialog.getByLabel("工作邮箱", { exact: true }).fill("pending@example.test");
    await dialog.getByLabel("所属部门", { exact: true }).selectOption("dep");
    await dialog.getByRole("button", { name: "发送邀请", exact: true }).click();
    await dialog.getByRole("alert").waitFor();
    assert.match(await dialog.getByRole("alert").textContent(), /关闭邀请窗口.*搜索.*重新邀请/);
    await dialog.getByRole("button", { name: "取消", exact: true }).click();
    await page.getByLabel("搜索成员", { exact: true }).fill("pending@example.test");
    await page.getByRole("button", { name: "重新邀请", exact: true }).click();
    await page.getByRole("dialog", { name: "重新邀请成员", exact: true }).waitFor();
  } finally { await context.close(); }
});
