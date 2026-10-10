import test, { before, after } from "node:test";
import assert from "node:assert/strict";
import { fileURLToPath } from "node:url";
import { mkdir } from "node:fs/promises";
import { chromium } from "playwright";
import { createServer } from "vite";

let vite, browser, baseUrl;
before(async () => {
  vite = await createServer({ root: fileURLToPath(new URL("../", import.meta.url)), logLevel: "silent", server: { host: "127.0.0.1", port: 0 } });
  await vite.listen();
  baseUrl = vite.resolvedUrls.local[0];
  browser = await chromium.launch({ headless: true });
});
after(async () => { await browser?.close(); await vite?.close(); });

for (const width of [1440, 1280, 900, 390]) {
  test(`workbench kanban keeps all six stages on one scrollable row at ${width}px`, async () => {
    const { context, page } = await openPage({ width, height: 1000 });
    try {
      const board = page.locator(".kanban").filter({ has: page.locator(".candidate-card") });
      await board.scrollIntoViewIfNeeded();
      await mkdir(fileURLToPath(new URL("../../.tmp/", import.meta.url)), { recursive: true });
      await board.screenshot({ path: fileURLToPath(new URL(`../../.tmp/workbench-kanban-${width}-start.png`, import.meta.url)) });
      const boxes = await board.locator(":scope > .stage").evaluateAll((stages) => stages.map((stage) => {
        const { x, y, width, height } = stage.getBoundingClientRect();
        return { x, y, width, height };
      }));
      assert.equal(boxes.length, 6);
      const borders = await board.locator(":scope > .stage").evaluateAll((stages) => stages.map((stage) => {
        const style = getComputedStyle(stage);
        return { width: style.borderTopWidth, style: style.borderTopStyle, color: style.borderTopColor };
      }));
      assert.ok(borders.every((border) => border.width === "3px" && border.style === "solid"), `missing stage accent: ${JSON.stringify(borders)}`);
      assert.equal(borders[5].color, "rgb(8, 151, 156)");
      assert.equal(new Set(borders.map((border) => border.color)).size, 6);
      assert.ok(boxes.every((box) => Math.abs(box.y - boxes[0].y) < 1), `stages wrapped: ${JSON.stringify(boxes)}`);
      for (let index = 1; index < boxes.length; index++) {
        assert.ok(boxes[index].x >= boxes[index - 1].x + boxes[index - 1].width - 1, "stage columns overlap");
      }
      const pending = board.getByRole("button", { name: /Pending candidate/ });
      await pending.scrollIntoViewIfNeeded();
      const boardBox = await board.boundingBox();
      const cardBox = await pending.boundingBox();
      assert.ok(cardBox.x >= boardBox.x && cardBox.x + cardBox.width <= boardBox.x + boardBox.width + 1, "pending card is not reachable within scrollport");
      assert.equal(await page.evaluate(() => document.documentElement.scrollWidth > innerWidth), false);
      await board.screenshot({ path: fileURLToPath(new URL(`../../.tmp/workbench-kanban-${width}-pending.png`, import.meta.url)) });
      await pending.click();
      await page.getByRole("heading", { name: "入职办理", exact: true }).waitFor();
    } finally { await context.close(); }
  });
}

function workbench(count = 1) {
  const empty = { count: 0, items: [] };
  const candidate = { application_id: "app-1", candidate_id: "candidate-1", job_id: "job-1", display_name: "Pending candidate", current_title: "Engineer", location: null, source: "upload", stage: "hired", updated_at: "2026-09-07T01:00:00Z" };
  return {
    generated_at: candidate.updated_at,
    jobs: [{ id: "job-1", title: "Engineer", department_name: "Engineering", status: "open", updated_at: candidate.updated_at, active_count: count, stages: { new: empty, review: empty, contact: empty, interview_pending: empty, interviewing: empty, decision: empty, passed: empty, pending_onboarding: { count, items: count ? [candidate] : [] } } }],
    tasks: { review: empty, interview_pending: empty, decision: empty, passed: empty, onboarding: { count, items: count ? [{ ...candidate, id: "onboarding-1", job_title: "Engineer", department_name: "Engineering", expected_start_date: null, status: "incomplete", version: 0, complete: false, can_submit: false, blocking_reason: "onboarding_data_missing" }] : [] } },
    notifications: { review: empty, interview_pending: empty, decision: empty, passed: empty },
    interviews: { available: false, upcoming: [], pending_feedback: [] },
  };
}

async function openPage(viewport, count = 1, outsideJobs = false, jobCount = 1) {
  const context = await browser.newContext({ viewport });
  await context.route("**/api/v1/**", async (route) => {
    const path = new URL(route.request().url()).pathname;
    const payload = workbench(count);
    for (let index = 2; index <= jobCount; index++) {
      const job = structuredClone(payload.jobs[0]);
      job.id = `job-${index}`;
      job.title = `Position ${index}`;
      job.stages.pending_onboarding.items = job.stages.pending_onboarding.items.map((item) => ({ ...item, application_id: `app-${index}`, candidate_id: `candidate-${index}`, job_id: job.id, display_name: `Pending candidate ${index}` }));
      payload.jobs.push(job);
    }
    if (outsideJobs && payload.tasks.onboarding.items[0]) payload.tasks.onboarding.items[0].job_id = "closed-job";
    const data = path === "/api/v1/me" ? { id: "user-1", display_name: "HR", roles: ["recruiting_admin"] }
      : path === "/api/v1/workbench" ? payload
      : path === "/api/v1/candidates/candidate-1" ? { id: "candidate-1", display_name: "Pending candidate" }
      : path === "/api/v1/candidates/candidate-1/applications" ? [{ id: "app-1", candidate_id: "candidate-1", job_id: outsideJobs ? "closed-job" : "job-1", job_title: "Engineer", stage: "hired", version: 1 }]
      : /^\/api\/v1\/candidates\/candidate-1\/(resumes|timeline|notes)$/.test(path) ? []
      : path === "/api/v1/offers" ? [{ id: "offer-1", application_id: "app-1", job_id: "job-1", candidate_name: "Pending candidate", job_title: "Engineer", status: "accepted", version: 1, content: {}, allowed_actions: {} }]
      : path === "/api/v1/applications/app-1/onboarding" ? { ...payload.tasks.onboarding.items[0], can_update: true }
      : null;
    await route.fulfill({ status: data ? 200 : 503, contentType: "application/json", body: JSON.stringify(data ? { data } : { status: 503 }) });
  });
  const page = await context.newPage();
  await page.goto(`${baseUrl}workbench`);
  await page.locator(".dashboard-metrics").waitFor();
  return { context, page };
}

for (const width of [1440, 390]) {
  test(`all jobs and any-job selection stay in the workbench at ${width}px`, async () => {
    const { context, page } = await openPage({ width, height: 1000 }, 1, false, 7);
    try {
      const switcher = page.locator(".job-switcher");
      await switcher.getByRole("button", { name: /全部职位/ }).click();
      await page.locator(".pipeline-header").getByRole("heading", { name: "全部职位", exact: true }).waitFor();
      const pending = page.locator(".kanban .stage").filter({ has: page.getByText("待入职", { exact: true }) });
      assert.equal(await pending.locator("header > span").textContent(), "7");
      assert.equal(await pending.locator(".candidate-card").count(), 5);
      await pending.getByRole("button", { name: "打开待入职列表", exact: true }).waitFor();
      await switcher.getByRole("combobox", { name: "选择看板职位" }).selectOption("job-7");
      await page.locator(".pipeline-header").getByRole("heading", { name: "Position 7", exact: true }).waitFor();
      assert.equal(new URL(page.url()).pathname, "/workbench");
      assert.equal(await pending.locator("header > span").textContent(), "1");
      await pending.getByRole("button", { name: /Pending candidate 7/ }).waitFor();
      await switcher.getByRole("combobox", { name: "选择看板职位" }).selectOption("all");
      await page.locator(".updated").getByRole("button", { name: "刷新", exact: true }).click();
      await page.locator(".pipeline-header").getByRole("heading", { name: "全部职位", exact: true }).waitFor();
      await mkdir(fileURLToPath(new URL("../../.tmp/", import.meta.url)), { recursive: true });
      await page.screenshot({ path: fileURLToPath(new URL(`../../.tmp/workbench-all-jobs-${width}.png`, import.meta.url)), fullPage: true });
      assert.equal(await page.evaluate(() => document.documentElement.scrollWidth > innerWidth), false, JSON.stringify(await page.locator(".job-switcher, .job-tabs, .pipeline-header, .main-column, .pipeline-tools").evaluateAll((items) => items.map((item) => ({ name: item.className, width: item.getBoundingClientRect().width, scroll: item.scrollWidth })))));
    } finally { await context.close(); }
  });
}

for (const viewport of [{ width: 1440, height: 1000 }, { width: 390, height: 844 }]) {
  test(`pending onboarding board and metric navigate to actionable workflow at ${viewport.width}px`, async () => {
    const { context, page } = await openPage(viewport);
    try {
      const column = page.locator(".kanban .stage").filter({ has: page.getByText("待入职", { exact: true }) });
      await column.getByRole("button", { name: /Pending candidate/ }).click();
      await page.waitForURL(/\/candidates\/candidate-1\?/);
      let params = new URL(page.url()).searchParams;
      assert.equal(params.get("tab"), "offer");
      assert.equal(params.get("application"), "app-1");
      assert.equal(params.get("job"), "job-1");
      await page.getByRole("heading", { name: "入职办理", exact: true }).waitFor();
      await page.goBack();
      await page.locator(".dashboard-metrics").getByRole("button", { name: /待入职/ }).focus();
      await page.keyboard.press("Enter");
      await page.waitForURL(/view=onboarding/);
      await page.locator(".kanban").waitFor({ state: "detached" });
      assert.equal(await page.locator(".kanban").count(), 0);
      await page.getByRole("button", { name: "查看详情", exact: true }).waitFor();
      await page.reload();
      await page.getByRole("button", { name: "查看详情", exact: true }).waitFor();
      assert.equal(await page.locator(".kanban").count(), 0);
      assert.equal(await page.evaluate(() => document.documentElement.scrollWidth > window.innerWidth), false);
      await mkdir(fileURLToPath(new URL("../../.tmp/", import.meta.url)), { recursive: true });
      await page.screenshot({ path: fileURLToPath(new URL(`../../.tmp/workbench-onboarding-${viewport.width}.png`, import.meta.url)), fullPage: true });
      await page.getByRole("button", { name: "查看详情", exact: true }).click();
      await page.waitForURL(/\/candidates\/candidate-1\?/);
      params = new URL(page.url()).searchParams;
      assert.equal(params.get("tab"), "offer");
      assert.equal(params.get("return"), "/workbench?view=onboarding");
      await page.getByRole("heading", { name: "入职办理", exact: true }).waitFor();
      await page.goBack();
      await page.getByRole("button", { name: "返回工作台", exact: true }).click();
      await page.locator(".kanban").waitFor();
    } finally { await context.close(); }
  });
}

test("empty onboarding metric still opens workflow with an explicit empty state", async () => {
  const { context, page } = await openPage({ width: 1280, height: 900 }, 0);
  try {
    await page.locator(".dashboard-metrics").getByRole("button", { name: /待入职/ }).click();
    await page.waitForURL(/view=onboarding/);
    await page.getByText("暂无待入职人员", { exact: true }).waitFor();
    assert.equal(await page.getByRole("button", { name: "办理入职", exact: true }).count(), 0);
  } finally { await context.close(); }
});

test("organization onboarding entry preserves a job outside the current board", async () => {
  const { context, page } = await openPage({ width: 1280, height: 900 }, 1, true);
  try {
    await page.locator(".dashboard-metrics").getByRole("button", { name: /待入职/ }).click();
    await page.getByRole("button", { name: "查看详情", exact: true }).click();
    await page.waitForURL(/\/candidates\/candidate-1\?/);
    assert.equal(new URL(page.url()).searchParams.get("job"), "closed-job");
    await page.getByRole("heading", { name: "入职办理", exact: true }).waitFor();
  } finally { await context.close(); }
});

test("pending previews disclose truncation and workflow refresh failures can be retried", async () => {
  const { context, page } = await openPage({ width: 1280, height: 900 }, 25);
  try {
    await page.getByRole("button", { name: "打开待入职列表", exact: true }).click();
    await page.waitForURL(/view=onboarding/);
    await page.getByText("共 25 人，当前显示 1 人（最多展示前 20 人）。", { exact: true }).waitFor();
    await page.route("**/api/v1/workbench?all_jobs=true", (route) => route.fulfill({ status: 503, contentType: "application/json", body: JSON.stringify({ status: 503 }) }));
    await page.getByRole("button", { name: "刷新待入职", exact: true }).click();
    await page.getByRole("alert").filter({ hasText: "当前展示上次成功数据" }).waitFor();
    await page.getByRole("button", { name: "查看详情", exact: true }).waitFor();
    await page.unroute("**/api/v1/workbench?all_jobs=true");
    await page.getByRole("button", { name: "重新加载", exact: true }).click();
    await page.getByRole("alert").filter({ hasText: "当前展示上次成功数据" }).waitFor({ state: "detached" });
  } finally { await context.close(); }
});
