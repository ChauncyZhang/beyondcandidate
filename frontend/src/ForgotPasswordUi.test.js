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

async function openPage({ width = 1440, manualOrganization = false, failure = null, barrier } = {}) {
  const context = await browser.newContext({ viewport: { width, height: 900 } });
  const requests = [];
  await context.route("**/api/v1/**", async (route) => {
    const request = route.request();
    const path = new URL(request.url()).pathname;
    if (path === "/api/v1/me") return route.fulfill({ status: 401, contentType: "application/problem+json", body: JSON.stringify({ code: "authentication_required" }) });
    let data = [];
    if (path === "/api/v1/auth/config") data = { default_organization: manualOrganization ? null : { slug: "acme", name: "Acme" } };
    if (path === "/api/v1/auth/password-reset/request") {
      requests.push(request.postDataJSON());
      await barrier;
      if (failure) return route.fulfill({ status: failure.status, contentType: "application/problem+json", body: JSON.stringify(failure) });
      return route.fulfill({ status: 202, contentType: "application/json", body: JSON.stringify({ data: { requested: true } }) });
    }
    return route.fulfill({ status: 200, contentType: "application/json", body: JSON.stringify({ data }) });
  });
  const page = await context.newPage();
  page.setDefaultTimeout(7000);
  page.setDefaultNavigationTimeout(20000);
  await page.goto(baseUrl);
  return { context, page, requests };
}

for (const width of [1440, 390]) {
  test(`login self-service recovery preserves email, hides password and prevents duplicate requests at ${width}px`, { timeout: 25000 }, async () => {
    let release;
    const barrier = new Promise((resolve) => { release = resolve; });
    const { context, page, requests } = await openPage({ width, barrier });
    try {
      await page.getByLabel("工作邮箱", { exact: true }).fill("member@example.test");
      await page.getByLabel("密码", { exact: true }).fill("never-transmit-this-password");
      await page.getByRole("button", { name: "忘记密码", exact: true }).click();
      await page.getByRole("heading", { name: "找回密码", exact: true }).waitFor();
      assert.equal(await page.getByLabel("工作邮箱", { exact: true }).inputValue(), "member@example.test");
      assert.equal(await page.getByLabel("密码", { exact: true }).count(), 0);
      await page.getByRole("button", { name: "发送重置邮件", exact: true }).click();
      await page.getByRole("button", { name: "正在发送…", exact: true }).waitFor();
      assert.equal(await page.getByRole("button", { name: "正在发送…", exact: true }).isEnabled(), false);
      assert.deepEqual(requests, [{ organization_slug: "acme", email: "member@example.test" }]);
      release();
      await page.getByText(/如果该邮箱对应可用账号/).waitFor();
      assert.match(await page.getByRole("status").textContent(), /1 小时/);
      assert.equal(await page.getByRole("button", { name: /重新发送/ }).isEnabled(), false);
      assert.ok(await page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth));
      const bounds = await page.locator(".login-card").boundingBox();
      assert.ok(bounds.x >= 0 && bounds.x + bounds.width <= width + 1);
      await page.screenshot({ path: fileURLToPath(new URL(`../.tmp/forgot-password-${width}.png`, import.meta.url)), fullPage: true });
      await page.getByRole("button", { name: "返回登录", exact: true }).click();
      await page.getByRole("heading", { name: "登录工作台", exact: true }).waitFor();
      assert.equal(await page.getByLabel("密码", { exact: true }).inputValue(), "", "opening recovery must clear any typed password");
      assert.equal(await page.getByLabel("工作邮箱", { exact: true }).inputValue(), "member@example.test");
      await page.screenshot({ path: fileURLToPath(new URL(`../.tmp/forgot-password-login-${width}.png`, import.meta.url)), fullPage: true });
      await page.getByRole("button", { name: "忘记密码", exact: true }).click();
      assert.equal(await page.getByRole("button", { name: /重新发送/ }).isEnabled(), false, "returning to login must not bypass cooldown");
    } finally { release(); await context.close(); }
  });
}

test("manual organization recovery validates email and supports keyboard submit without a password", { timeout: 25000 }, async () => {
  const { context, page, requests } = await openPage({ manualOrganization: true });
  try {
    await page.getByRole("button", { name: "忘记密码", exact: true }).click();
    await page.getByLabel("组织标识", { exact: true }).fill("other");
    await page.getByLabel("工作邮箱", { exact: true }).fill("bad-address");
    await page.getByRole("button", { name: "发送重置邮件", exact: true }).click();
    assert.equal(requests.length, 0);
    await page.getByLabel("工作邮箱", { exact: true }).fill("unknown@example.test");
    await page.getByLabel("工作邮箱", { exact: true }).press("Enter");
    await page.getByText(/如果该邮箱对应可用账号/).waitFor();
    assert.deepEqual(requests, [{ organization_slug: "other", email: "unknown@example.test" }]);
    assert.equal(await page.getByRole("alert").count(), 0);
  } finally { await context.close(); }
});

test("recovery transport failure shows safe retry and leaves login available", { timeout: 25000 }, async () => {
  const { context, page } = await openPage({ failure: { status: 503, code: "service_unavailable", detail: "sensitive server detail" } });
  try {
    await page.getByRole("button", { name: "忘记密码", exact: true }).click();
    await page.getByLabel("工作邮箱", { exact: true }).fill("member@example.test");
    await page.getByRole("button", { name: "发送重置邮件", exact: true }).click();
    await page.getByRole("alert").waitFor();
    assert.match(await page.getByRole("alert").textContent(), /稍后重试/);
    assert.doesNotMatch(await page.getByRole("alert").textContent(), /sensitive|账号不存在/);
    assert.equal(await page.getByRole("button", { name: "发送重置邮件", exact: true }).isEnabled(), true);
    await page.getByRole("button", { name: "返回登录", exact: true }).click();
    await page.getByRole("button", { name: "登录", exact: true }).waitFor();
  } finally { await context.close(); }
});
