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

async function openPage({ anonymous = false, width = 1440, role = "recruiting_admin", resetError = null, acceptInvite = false, authenticatedReset = false } = {}) {
  const context = await browser.newContext({ viewport: { width, height: 900 } });
  const requests = [];
  let resetAccepted = false;
  let meLookups = 0;
  await context.route("**/api/v1/**", async (route) => {
    const request = route.request();
    const path = new URL(request.url()).pathname;
    const problem = (code, status = 422) => route.fulfill({ status, contentType: "application/problem+json", body: JSON.stringify({ status, code }) });
    let data = [];
    if (path === "/api/v1/me") {
      meLookups++;
      if (anonymous || (authenticatedReset && resetAccepted)) return problem("authentication_required", 401);
      data = { id: "current", display_name: "Current", roles: [role] };
    } else if (path === "/api/v1/settings/users") {
      data = [
        { id: "active", display_name: "Active member", email: "active@example.test", roles: ["recruiter"], status: "active" },
        { id: "pending", display_name: "Pending member", roles: ["interviewer"], status: "invited" },
        { id: "disabled", display_name: "Disabled member", roles: ["interviewer"], status: "disabled" },
        { id: "current", display_name: "Current", roles: [role], status: "active" },
        { id: "admin", display_name: "Admin", roles: ["system_admin"], status: "active" },
      ];
    } else if (path === "/api/v1/settings/users/active/password-reset") {
      requests.push({ path, headers: request.headers(), body: request.postData() });
      data = { password_reset: { token: "reset+token/=&", expires_at: "2026-10-21T11:00:00Z" } };
    } else if (path === "/api/v1/auth/password-reset/accept") {
      requests.push({ path, body: request.postDataJSON() });
      if (resetError) return problem(resetError);
      resetAccepted = true;
      data = { email: "active@example.test", organization_slug: "test" };
    } else if (path === "/api/v1/auth/invitations/accept") {
      requests.push({ path, body: request.postDataJSON() });
      if (!acceptInvite) return problem("invitation_invalid_or_expired");
      data = { email: "active@example.test", organization_slug: "test" };
    } else if (path === "/api/v1/auth/feishu/authorize") {
      return problem("feishu_not_configured", 503);
    } else if (path === "/api/v1/auth/config") {
      data = { default_organization: { slug: "test", name: "Test" } };
    }
    return route.fulfill({ status: 200, contentType: "application/json", headers: { "X-CSRF-Token": "browser-csrf" }, body: JSON.stringify({ data }) });
  });
  const page = await context.newPage();
  page.setDefaultTimeout(7000);
  page.setDefaultNavigationTimeout(20000);
  return { context, page, requests, meLookups: () => meLookups };
}

for (const width of [1440, 390]) {
  test(`active member recovery link confirmation and password form at ${width}px`, { timeout: 25000 }, async () => {
    const admin = await openPage({ width });
    try {
      await admin.page.goto(`${baseUrl}settings/organization/members`);
      const resetButton = admin.page.getByRole("button", { name: "重置密码", exact: true });
      await resetButton.waitFor();
      assert.equal(await resetButton.count(), 1, "must exclude inactive, self and privileged targets");
      await resetButton.click();
      const dialog = admin.page.getByRole("dialog", { name: "重置成员密码", exact: true });
      assert.match(await dialog.textContent(), /1 小时/);
      assert.equal(admin.requests.length, 0, "opening confirmation cannot issue a reset");
      await dialog.getByRole("button", { name: "生成重置链接", exact: true }).click();
      const input = dialog.getByLabel("密码重置链接", { exact: true });
      await input.waitFor();
      assert.equal(admin.requests[0].headers["x-csrf-token"], "browser-csrf");
      const link = await input.inputValue();
      assert.equal(new URLSearchParams(new URL(link).hash.slice(1)).get("reset-password"), "reset+token/=&");
      assert.match(await dialog.textContent(), /原密码仍可使用/);
      assert.ok(await admin.page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth));
      const invitee = await openPage({ anonymous: true, width });
      try {
        await invitee.page.goto(link);
        await invitee.page.getByRole("heading", { name: "重置登录密码", exact: true }).waitFor();
        await invitee.page.getByLabel("新密码", { exact: true }).fill("new-password-123");
        await invitee.page.getByLabel("确认新密码", { exact: true }).fill("new-password-123");
        await invitee.page.getByRole("button", { name: "重置密码", exact: true }).click();
        await invitee.page.getByText("密码已重置", { exact: true }).waitFor();
        assert.deepEqual(invitee.requests[0].body, { token: "reset+token/=&", password: "new-password-123" });
        assert.equal(new URL(invitee.page.url()).hash, "", "consumed token must be removed before any navigation");
        await invitee.page.getByRole("button", { name: "前往登录", exact: true }).click();
        await invitee.page.getByRole("button", { name: "登录", exact: true }).waitFor();
        assert.equal(await invitee.page.getByLabel("工作邮箱", { exact: true }).inputValue(), "active@example.test");
      } finally { await invitee.context.close(); }
      await dialog.getByRole("button", { name: "完成", exact: true }).click();
      assert.equal(await admin.page.getByLabel("密码重置链接").count(), 0);
    } finally { await admin.context.close(); }
  });
}

test("used invitation provides login recovery without claiming the account is unactivated", async () => {
  const { context, page } = await openPage({ anonymous: true });
  try {
    await page.goto(`${baseUrl}#invite=already-consumed-token`);
    await page.getByLabel("新密码", { exact: true }).fill("new-password-123");
    await page.getByLabel("确认新密码", { exact: true }).fill("new-password-123");
    await page.getByRole("button", { name: "设置密码", exact: true }).click();
    await page.getByRole("alert").waitFor();
    assert.match(await page.getByRole("alert").textContent(), /已使用/);
    await page.getByRole("button", { name: "前往登录", exact: true }).click();
    await page.getByRole("button", { name: "登录", exact: true }).waitFor();
    assert.equal(new URL(page.url()).hash, "");
  } finally { await context.close(); }
});

test("expired reset stays on form with a safe error and a login exit", async () => {
  const { context, page } = await openPage({ anonymous: true, resetError: "password_reset_invalid_or_expired" });
  try {
    await page.goto(`${baseUrl}#reset-password=expired-reset-token`);
    await page.getByLabel("新密码", { exact: true }).fill("new-password-123");
    await page.getByLabel("确认新密码", { exact: true }).fill("new-password-123");
    await page.getByRole("button", { name: "重置密码", exact: true }).click();
    await page.getByRole("alert").waitFor();
    assert.match(await page.getByRole("alert").textContent(), /重新生成/);
    assert.equal(await page.getByRole("button", { name: "重置密码", exact: true }).isEnabled(), true);
    assert.equal(await page.getByRole("button", { name: "前往登录", exact: true }).isEnabled(), true);
  } finally { await context.close(); }
});

test("successful activation removes the invite token even when Feishu continuation fails", async () => {
  const { context, page, requests } = await openPage({ anonymous: true, acceptInvite: true });
  try {
    await page.goto(`${baseUrl}#invite=fresh-invitation-token`);
    await page.getByLabel("新密码", { exact: true }).fill("new-password-123");
    await page.getByLabel("确认新密码", { exact: true }).fill("new-password-123");
    await page.getByRole("button", { name: "设置密码", exact: true }).click();
    await page.getByRole("button", { name: "登录", exact: true }).waitFor();
    assert.equal(new URL(page.url()).hash, "");
    await page.reload();
    await page.getByRole("button", { name: "登录", exact: true }).waitFor();
    assert.equal(requests.filter(({ path }) => path.endsWith("invitations/accept")).length, 1);
  } finally { await context.close(); }
});

test("reset opened in an authenticated tab revalidates the revoked session before returning to login", async () => {
  const view = await openPage({ authenticatedReset: true });
  try {
    await view.page.goto(`${baseUrl}#reset-password=valid-authenticated-reset`);
    await view.page.getByLabel("新密码", { exact: true }).fill("new-password-123");
    await view.page.getByLabel("确认新密码", { exact: true }).fill("new-password-123");
    await view.page.getByRole("button", { name: "重置密码", exact: true }).click();
    await view.page.getByText("密码已重置", { exact: true }).waitFor();
    await view.page.getByRole("button", { name: "前往登录", exact: true }).click();
    await view.page.getByRole("button", { name: "登录", exact: true }).waitFor();
    assert.ok(view.meLookups() >= 2, "must bypass cached initial bootstrap after revoking the active session");
    assert.equal(new URL(view.page.url()).hash, "");
  } finally { await view.context.close(); }
});
