import test, { after, before } from "node:test";
import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import { fileURLToPath } from "node:url";
import { chromium } from "playwright";
import { createServer } from "vite";

const labels = {
  workbench: "工作台",
  jobs: "职位",
  screening: "筛选任务",
  candidates: "候选人",
  interviews: "面试",
  talent: "人才库",
  reports: "报表",
  settings: "设置",
  importResume: "导入简历",
};

const recruitingNav = [labels.workbench, labels.jobs, labels.screening, labels.candidates, labels.interviews, labels.talent, labels.reports, labels.settings];
const interviewerNav = [labels.workbench, labels.interviews];
const prototypeRoot = fileURLToPath(new URL("../", import.meta.url));

let browser;
let vite;
let baseUrl;

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

after(async () => {
  await browser?.close();
  await vite?.close();
});

async function openAuthenticatedPage(viewport, roles) {
  const context = await browser.newContext({ viewport });
  await context.route("**/api/v1/**", async (route) => {
    const pathname = new URL(route.request().url()).pathname.replace(/\/$/, "");
    if (pathname === "/api/v1/me") {
      await route.fulfill({
        status: 200,
        contentType: "application/json",
        headers: { "x-csrf-token": "shell-navigation-audit" },
        body: JSON.stringify({
          data: {
            id: "00000000-0000-4000-8000-000000000001",
            display_name: "Navigation audit",
            roles,
          },
        }),
      });
      return;
    }
    await route.fulfill({
      status: 503,
      contentType: "application/problem+json",
      body: JSON.stringify({ type: "about:blank", title: "Unavailable", status: 503 }),
    });
  });
  const page = await context.newPage();
  await page.goto(baseUrl, { waitUntil: "domcontentloaded" });
  await page.getByRole("heading", { name: labels.workbench, exact: true }).waitFor();
  return { context, page };
}

async function computed(locator) {
  return locator.evaluate((element) => {
    const style = getComputedStyle(element);
    return {
      display: style.display,
      visibility: style.visibility,
      position: style.position,
      overflowX: style.overflowX,
    };
  });
}

async function assertInsideViewport(locator, viewport, message) {
  await locator.scrollIntoViewIfNeeded();
  const box = await locator.boundingBox();
  assert.ok(box, `${message} has no rendered bounding box`);
  assert.ok(box.x >= 0 && box.y >= 0, `${message} begins outside the viewport: ${JSON.stringify(box)}`);
  assert.ok(box.x + box.width <= viewport.width, `${message} exceeds viewport width: ${JSON.stringify(box)}`);
  assert.ok(box.y + box.height <= viewport.height, `${message} exceeds viewport height: ${JSON.stringify(box)}`);
}

async function assertNoBodyOverflow(page, message) {
  const widths = await page.evaluate(() => ({
    bodyClient: document.body.clientWidth,
    bodyScroll: document.body.scrollWidth,
    rootClient: document.documentElement.clientWidth,
    rootScroll: document.documentElement.scrollWidth,
  }));
  assert.equal(widths.bodyScroll, widths.bodyClient, `${message} body overflows horizontally: ${JSON.stringify(widths)}`);
  assert.equal(widths.rootScroll, widths.rootClient, `${message} root overflows horizontally: ${JSON.stringify(widths)}`);
}

async function assertNavLabels(nav, expected) {
  assert.equal(await nav.getAttribute("aria-label"), "主导航");
  assert.deepEqual(await nav.getByRole("button", { includeHidden: true }).allTextContents(), expected);
}

test("application shell uses BrowserRouter and URL-derived route state", async () => {
  const [mainSource, appSource] = await Promise.all([
    readFile(new URL("./main.jsx", import.meta.url), "utf8"),
    readFile(new URL("./App.jsx", import.meta.url), "utf8"),
  ]);
  assert.match(mainSource, /BrowserRouter/);
  assert.match(appSource, /useLocation/);
  assert.match(appSource, /useNavigate/);
  assert.match(appSource, /parseAppRoute\(location\)/);
  assert.match(appSource, /clearJobCreateDraft/);
  assert.match(appSource, /interviewController\.get\(route\.id/);
  assert.match(appSource, /inert=\{drawerViewport && !menuOpen\}/);
  assert.doesNotMatch(appSource, /inert=\{drawerViewport && !menuOpen \? "" : undefined\}/);
  assert.doesNotMatch(appSource, /createAppHistory/);
});

test("390px keeps the navigation drawer and primary action reachable", { timeout: 60_000 }, async () => {
  const viewport = { width: 390, height: 844 };
  const { context, page } = await openAuthenticatedPage(viewport, ["recruiting_admin"]);
  try {
    const sidebar = page.locator(".sidebar");
    const nav = page.locator("#primary-navigation");
    const menu = page.locator(".mobile-menu");
    assert.deepEqual(await computed(sidebar), { display: "flex", visibility: "hidden", position: "fixed", overflowX: "visible" });
    assert.notEqual((await computed(menu)).display, "none");
    await assertNavLabels(nav, recruitingNav);
    assert.equal(await nav.getByRole("button", { name: labels.workbench, exact: true, includeHidden: true }).getAttribute("aria-current"), "page");

    await menu.click();
    await page.waitForFunction(() => document.activeElement?.textContent?.trim() === "工作台");
    for (const name of recruitingNav) {
      await assertInsideViewport(nav.getByRole("button", { name, exact: true }), viewport, `390px ${name}`);
    }
    await assertInsideViewport(page.getByRole("button", { name: labels.importResume, exact: true }), viewport, "390px import resume");
    await page.keyboard.press("Escape");
    await sidebar.waitFor({ state: "hidden" });
    await assertNoBodyOverflow(page, "390px");
  } finally {
    await context.close();
  }
});

test("768px drawer traps focus and restores it for every close path", { timeout: 60_000 }, async () => {
  const viewport = { width: 768, height: 844 };
  const { context, page } = await openAuthenticatedPage(viewport, ["recruiting_admin"]);
  try {
    const sidebar = page.locator(".sidebar");
    const nav = page.locator("#primary-navigation");
    const navButtons = nav.getByRole("button", { includeHidden: true });
    const first = navButtons.first();
    const last = navButtons.last();
    const menu = page.locator(".mobile-menu");

    assert.equal((await computed(sidebar)).visibility, "hidden");
    await assertNavLabels(nav, recruitingNav);
    assert.equal(await first.isVisible(), false);
    await menu.focus();
    await page.keyboard.press("Tab");
    assert.equal(await nav.evaluate((element) => element.contains(document.activeElement)), false, "closed drawer entered the tab order");

    await menu.click();
    await page.waitForFunction(() => document.activeElement?.textContent?.trim() === "工作台");
    assert.equal(await menu.getAttribute("aria-expanded"), "true");
    assert.equal(await sidebar.getAttribute("role"), "dialog");
    assert.equal(await sidebar.getAttribute("aria-modal"), "true");
    assert.equal((await computed(sidebar)).visibility, "visible");
    for (const name of recruitingNav) {
      await assertInsideViewport(nav.getByRole("button", { name, exact: true }), viewport, `768px ${name}`);
    }

    await page.keyboard.press("Shift+Tab");
    assert.equal(await last.evaluate((element) => element === document.activeElement), true, "Shift+Tab did not wrap to the last navigation item");
    await page.keyboard.press("Tab");
    assert.equal(await first.evaluate((element) => element === document.activeElement), true, "Tab did not wrap to the first navigation item");
    for (let index = 0; index < recruitingNav.length + 2; index += 1) {
      await page.keyboard.press("Tab");
      assert.equal(await nav.evaluate((element) => element.contains(document.activeElement)), true, "drawer focus escaped to main content");
    }

    await page.keyboard.press("Escape");
    await page.waitForFunction(() => document.activeElement?.classList.contains("mobile-menu"));
    assert.equal(await menu.getAttribute("aria-expanded"), "false");
    await sidebar.waitFor({ state: "hidden" });
    assert.equal((await computed(sidebar)).visibility, "hidden");

    await menu.click();
    await page.waitForFunction(() => document.activeElement?.textContent?.trim() === "工作台");
    await page.getByRole("button", { name: "关闭菜单", exact: true }).click();
    await page.waitForFunction(() => document.activeElement?.classList.contains("mobile-menu"));
    await sidebar.waitFor({ state: "hidden" });
    assert.equal((await computed(sidebar)).visibility, "hidden");

    await menu.click();
    await page.waitForFunction(() => document.activeElement?.textContent?.trim() === "工作台");
    await nav.getByRole("button", { name: labels.jobs, exact: true }).click();
    await page.getByRole("heading", { name: labels.jobs, exact: true }).waitFor();
    await page.waitForFunction(() => document.activeElement?.classList.contains("mobile-menu"));
    assert.equal(await nav.getByRole("button", { name: labels.jobs, exact: true }).getAttribute("aria-current"), "page");
    await sidebar.waitFor({ state: "hidden" });
    assert.equal((await computed(sidebar)).visibility, "hidden");
    await assertNoBodyOverflow(page, "768px");
  } finally {
    await context.close();
  }
});

test("1280px keeps the desktop sidebar visible and filters navigation by role", { timeout: 60_000 }, async () => {
  const viewport = { width: 1280, height: 720 };
  const { context, page } = await openAuthenticatedPage(viewport, ["interviewer"]);
  try {
    const sidebar = page.locator(".sidebar");
    const nav = page.locator("#primary-navigation");
    const menu = page.locator(".mobile-menu");
    const workspace = page.locator(".workspace");
    const sidebarStyle = await computed(sidebar);
    assert.equal(sidebarStyle.visibility, "visible");
    assert.equal(sidebarStyle.position, "fixed");
    assert.equal((await computed(menu)).display, "none");
    assert.equal(await sidebar.getAttribute("role"), null);
    assert.equal(await sidebar.getAttribute("aria-modal"), null);
    await assertNavLabels(nav, interviewerNav);
    assert.equal(await page.getByRole("button", { name: labels.importResume, exact: true }).count(), 0);
    assert.equal(await nav.getByRole("button", { name: labels.workbench, exact: true }).getAttribute("aria-current"), "page");

    for (const name of interviewerNav) {
      await assertInsideViewport(nav.getByRole("button", { name, exact: true }), viewport, `1280px ${name}`);
    }
    const sidebarBox = await sidebar.boundingBox();
    const workspaceBox = await workspace.boundingBox();
    assert.ok(sidebarBox && workspaceBox);
    assert.equal(sidebarBox.x, 0);
    assert.equal(sidebarBox.width, 240);
    assert.equal(workspaceBox.x, 240);
    await assertNoBodyOverflow(page, "1280px");
  } finally {
    await context.close();
  }
});

test("1440px uses the full desktop sidebar", { timeout: 60_000 }, async () => {
  const viewport = { width: 1440, height: 900 };
  const { context, page } = await openAuthenticatedPage(viewport, ["recruiting_admin"]);
  try {
    const sidebarBox = await page.locator(".sidebar").boundingBox();
    const workspaceBox = await page.locator(".workspace").boundingBox();
    assert.ok(sidebarBox && workspaceBox);
    assert.equal(sidebarBox.width, 240);
    assert.equal(workspaceBox.x, 240);
    const brand = page.locator(".brand");
    await assertInsideViewport(brand, viewport, "1440px brand");
    const brandBounds = await brand.evaluate((element) => {
      const container = element.getBoundingClientRect();
      const text = element.querySelector("span").getBoundingClientRect();
      return { containerRight: container.right, textRight: text.right };
    });
    assert.ok(brandBounds.textRight <= brandBounds.containerRight, `brand text exceeds sidebar: ${JSON.stringify(brandBounds)}`);
    await assertNoBodyOverflow(page, "1440px");
  } finally {
    await context.close();
  }
});
