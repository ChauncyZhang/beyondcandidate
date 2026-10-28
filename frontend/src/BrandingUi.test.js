import test, { before, after } from "node:test";
import assert from "node:assert/strict";
import { fileURLToPath } from "node:url";
import { mkdir } from "node:fs/promises";
import { chromium } from "playwright";
import { createServer } from "vite";

let browser, vite, baseUrl, servedConfig;
const received = [];
const custom = { name: "Example Hiring", short_name: "EH", logo_url: "/__branding-test/logo.svg" };
const asset = '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 48 48"><rect width="48" height="48" rx="8" fill="#16794d"/><text x="24" y="31" text-anchor="middle" font-family="Arial" font-size="20" fill="white">EH</text></svg>';
before(async () => {
  vite = await createServer({ root: fileURLToPath(new URL("../", import.meta.url)), logLevel: "silent", server: { host: "127.0.0.1", port: 0 }, plugins: [{
    name: "branding-runtime-fixture",
    configureServer(server) {
      server.middlewares.use((req, res, next) => {
        if (req.url === "/__branding-test/logo.svg") { res.setHeader("Content-Type", "image/svg+xml"); return res.end(asset); }
        if (req.url === "/__branding-test/missing.svg") { res.statusCode = 404; return res.end(); }
        if (req.url !== "/branding.json") return next();
        received.push({ cookie: req.headers.cookie, authorization: req.headers.authorization });
        if (servedConfig === undefined) return next();
        res.setHeader("Content-Type", "application/json");
        if (servedConfig === "failed") { res.statusCode = 503; return res.end("{}"); }
        if (servedConfig === "malformed") return res.end("{invalid");
        if (servedConfig === "slow") { const timer = setTimeout(() => res.end(JSON.stringify(custom)), 2200); res.on("close", () => clearTimeout(timer)); return; }
        res.end(JSON.stringify(servedConfig));
      });
    },
  }] });
  await vite.listen(); baseUrl = vite.resolvedUrls.local[0];
  browser = await chromium.launch({ headless: true });
  await mkdir(fileURLToPath(new URL("../../.tmp/branding/", import.meta.url)), { recursive: true });
});
after(async () => { await browser?.close(); await vite?.close(); });

async function openPage(width, authenticated = false) {
  const context = await browser.newContext({ viewport: { width, height: 900 } });
  await context.addCookies([{ name: "test-session", value: "not-for-branding", url: baseUrl }]);
  await context.route("**/api/v1/**", async (route) => {
    const path = new URL(route.request().url()).pathname;
    if (path === "/api/v1/me" && !authenticated) return route.fulfill({ status: 401, json: { code: "authentication_required" } });
    const data = path === "/api/v1/me" ? { id: "branding-user", display_name: "Recruiter", roles: ["recruiting_admin"] }
      : path === "/api/v1/auth/config" ? { default_organization: { slug: "example", name: "Unchanged Organization" } } : [];
    return route.fulfill({ json: { data } });
  });
  const page = await context.newPage();
  page.setDefaultTimeout(7000);
  return { context, page };
}

async function assertBrand(page, name, logo) {
  await page.getByRole("img", { name, exact: true }).waitFor();
  assert.equal(await page.title(), `${name} | 候选人全流程招聘平台`);
  if (logo) {
    const image = page.locator(".product-brand-mark img");
    assert.equal(await image.getAttribute("src"), logo);
    assert.equal(await image.evaluate((img) => img.complete && img.naturalWidth > 0), true);
    assert.equal(await page.locator('link[rel="icon"]').getAttribute("href"), logo);
  }
  assert.equal(await page.evaluate(() => document.documentElement.scrollWidth > innerWidth), false);
}

for (const width of [1440, 390]) {
  for (const variant of ["default", "custom"]) {
    test(`${variant} runtime branding on login, invite, reset and sidebar at ${width}px`, { timeout: 30000 }, async () => {
      servedConfig = variant === "default" ? undefined : custom;
      const name = variant === "default" ? "BeyondCandidate" : custom.name;
      const logo = variant === "default" ? "/favicon.svg" : custom.logo_url;
      const { context, page } = await openPage(width);
      try {
        await page.goto(baseUrl);
        await page.getByRole("heading", { name: "登录工作台" }).waitFor();
        await assertBrand(page, name, logo);
        assert.match(await page.locator(".login-heading p").textContent(), new RegExp(name));
        if (variant === "custom") assert.doesNotMatch(await page.locator("body").innerText(), /BeyondCandidate|\bBC\b/);
        await page.getByLabel("工作邮箱", { exact: true }).fill("member@example.test");
        await page.getByLabel("密码", { exact: true }).fill("preserved-login-password");
        await page.screenshot({ path: fileURLToPath(new URL(`../../.tmp/branding/${variant}-login-${width}.png`, import.meta.url)), fullPage: true });
        await page.goto(`${baseUrl}#invite=branding-test`);
        await page.getByRole("heading", { name: "设置登录密码" }).waitFor();
        await assertBrand(page, name, logo);
        await page.screenshot({ path: fileURLToPath(new URL(`../../.tmp/branding/${variant}-invite-${width}.png`, import.meta.url)), fullPage: true });
        assert.equal(await page.getByRole("button", { name: "设置密码", exact: true }).isEnabled(), false);
        await page.goto(`${baseUrl}#reset-password=branding-test`);
        await page.getByRole("heading", { name: "重置登录密码" }).waitFor();
        await assertBrand(page, name, logo);
        assert.ok(received.length > 0);
        assert.ok(received.every((req) => !req.cookie && !req.authorization));
      } finally { await context.close(); }
      const auth = await openPage(width, true);
      try {
        await auth.page.goto(`${baseUrl}candidates`);
        if (width === 390) await auth.page.getByRole("button", { name: "打开主导航" }).click();
        await auth.page.locator(".sidebar .brand").waitFor();
        await auth.page.waitForFunction(() => document.querySelector(".sidebar").getBoundingClientRect().left >= -1);
        await assertBrand(auth.page, name, logo);
        assert.equal(await auth.page.locator(".brand strong").textContent(), name);
        if (variant === "custom") assert.doesNotMatch(await auth.page.locator("body").innerText(), /BeyondCandidate|\bBC\b/);
        await auth.page.screenshot({ path: fileURLToPath(new URL(`../../.tmp/branding/${variant}-sidebar-${width}.png`, import.meta.url)), fullPage: true });
      } finally { await auth.context.close(); }
    });
  }
}

test("invalid, failed, slow and unsafe runtime config fall back without cached page state", { timeout: 30000 }, async () => {
  const { context, page } = await openPage(390);
  try {
    for (const config of [custom, "malformed", "failed", "slow", { ...custom, logo_url: "//unsafe.invalid/logo.svg" }, { ...custom, favicon_url: "/%2e%2e/secret" }, { ...custom, extra: "not-public" }, { ...custom, name: "Name\n" }, {}]) {
      servedConfig = config;
      await page.goto(baseUrl);
      await page.getByRole("heading", { name: "登录工作台" }).waitFor();
      const valid = config === custom;
      await assertBrand(page, valid ? custom.name : "BeyondCandidate", valid ? custom.logo_url : "/favicon.svg");
      if (config === "slow") {
        await page.waitForTimeout(850);
        assert.equal(await page.locator(".login-heading p").textContent(), "BeyondCandidate · 候选人全流程招聘平台");
      }
    }
  } finally { await context.close(); }
});

for (const width of [1440, 390]) {
  test(`maximum length brand name and monogram fit the sidebar at ${width}px`, async () => {
    servedConfig = { name: "LongName".repeat(10), short_name: "ABC12345", logo_url: null };
    const { context, page } = await openPage(width, true);
    try {
      await page.goto(`${baseUrl}candidates`);
      if (width === 390) await page.getByRole("button", { name: "打开主导航" }).click();
      await page.waitForFunction(() => document.querySelector(".sidebar")?.getBoundingClientRect().left >= -1);
      const label = page.locator(".brand strong");
      await label.waitFor();
      const fit = await label.evaluate((el) => el.scrollWidth <= el.clientWidth + 1 && el.scrollHeight <= el.clientHeight + 1);
      assert.equal(fit, true);
      const contained = await label.evaluate((el) => {
        const label = el.getBoundingClientRect();
        const row = el.closest(".brand").getBoundingClientRect();
        return label.top >= row.top && label.bottom <= row.bottom && label.left >= row.left && label.right <= row.right;
      });
      assert.equal(contained, true);
      assert.equal(await page.locator(".product-brand-monogram").innerText(), "ABC12345");
      assert.equal(await page.evaluate(() => document.documentElement.scrollWidth > innerWidth), false);
      await page.screenshot({ path: fileURLToPath(new URL(`../../.tmp/branding/long-name-sidebar-${width}.png`, import.meta.url)), fullPage: true });
    } finally { await context.close(); }
  });
}

test("null and broken logos use readable monograms, safe title text and generated favicons", { timeout: 30000 }, async () => {
  const { context, page } = await openPage(390);
  try {
    for (const logo_url of [null, "/__branding-test/missing.svg"]) {
      const name = '<img src=x onerror="window.brandInjected=true">';
      servedConfig = { name, short_name: "ABC12345", logo_url };
      await page.goto(baseUrl);
      const monogram = page.locator(".product-brand-monogram");
      await monogram.waitFor();
      assert.equal(await monogram.innerText(), "ABC12345");
      assert.equal(await monogram.getAttribute("aria-label"), name);
      assert.equal(await page.title(), `${name} | 候选人全流程招聘平台`);
      assert.equal(await page.locator(".login-heading p img").count(), 0);
      assert.equal(await page.evaluate(() => window.brandInjected), undefined);
      assert.match(await page.locator('link[rel="icon"]').getAttribute("href"), /^data:image\/svg\+xml,/);
      assert.equal(await page.evaluate(() => document.documentElement.scrollWidth > innerWidth), false);
      const fits = await monogram.evaluate((el) => el.scrollWidth <= el.clientWidth && el.scrollHeight <= el.clientHeight);
      assert.equal(fits, true);
    }
    await page.screenshot({ path: fileURLToPath(new URL("../../.tmp/branding/monogram-fallback-390.png", import.meta.url)), fullPage: true });
    servedConfig = { ...custom, favicon_url: "/favicon.svg" };
    await page.goto(baseUrl);
    await page.getByRole("heading", { name: "登录工作台" }).waitFor();
    assert.equal(await page.locator('link[rel="icon"]').getAttribute("href"), "/favicon.svg");
  } finally { await context.close(); }
});
