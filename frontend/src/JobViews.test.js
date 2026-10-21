import test, { before, after } from "node:test";
import assert from "node:assert/strict";
import { fileURLToPath } from "node:url";
import { chromium } from "playwright";
import { createServer } from "vite";

let vite;
let browser;
before(async () => {
  vite = await createServer({ root: fileURLToPath(new URL("../", import.meta.url)), logLevel: "silent", server: { host: "127.0.0.1", port: 0 } });
  await vite.listen();
  browser = await chromium.launch({ headless: true });
});
after(async () => { await browser?.close(); await vite?.close(); });

for (const width of [1280, 390]) {
  test(`publication checklist reflects missing reviewers, whitespace and real template availability at ${width}px`, async () => {
    const page = await browser.newPage({ viewport: { width, height: 900 } });
    page.setDefaultTimeout(10000);
    try {
      const html = await vite.transformIndexHtml("/", '<html><body><header id="actions"></header><div id="root"></div></body></html>');
      await page.route("**/job-form-test", (route) => route.fulfill({ contentType: "text/html", body: html }));
      await page.goto(`${vite.resolvedUrls.local[0]}job-form-test`);
      await page.evaluate(async () => {
        const { default: React } = await import("/node_modules/.vite/deps/react.js");
        const { default: ReactDOM } = await import("/node_modules/.vite/deps/react-dom_client.js");
        const { JobsWorkspace } = await import("/src/JobViews.jsx");
        const { workflowTemplateController } = await import("/src/workflowTemplateController.js");
        await import("/src/styles.css");
        await import("/src/product-theme.css");
        workflowTemplateController.list = () => new Promise((resolve) => { window.resolveTemplates = resolve; });
        window.saveCalls = [];
        const controller = {
          async listRecruiters() { return []; },
          async listHiringManagers() { return [{ id: "manager-1", name: "测试经理" }]; },
          async saveDefinition(values, options) { window.saveCalls.push({ values, options }); return { id: "created", ...values }; },
        };
        ReactDOM.createRoot(document.getElementById("root")).render(React.createElement(JobsWorkspace, {
          mode: "form", setMode() {}, setSelectedJob() {}, listState: { departments: [{ id: "dept", name: "技术部" }], owners: [] },
          jobController: controller, offerController: { async listTemplates() { return []; } },
          initialDraft: { name: "测试职位", jd: "职位描述", departmentId: "dept", mustHave: "开发经验" },
          onRefreshJobMutation: async (record) => record, onNotify() {}, pageActionHost: document.getElementById("actions"),
        }));
      });
      const summary = page.locator(".form-summary");
      const publish = page.getByRole("button", { name: "发布职位", exact: true });
      await summary.getByText("3/6", { exact: true }).waitFor();
      assert.equal(await publish.isDisabled(), true);
      assert.match(await summary.textContent(), /请至少选择一位用人经理/);
      assert.match(await summary.textContent(), /请加载并选择可用的流程模板/);
      const hintsDoNotOverlap = await summary.locator(".check-row").evaluateAll((rows) => rows.every((row) => {
        const hint = row.querySelector("small");
        if (!hint) return true;
        const range = document.createRange();
        range.selectNodeContents(hint.parentElement.firstChild);
        return hint.getBoundingClientRect().top >= range.getBoundingClientRect().bottom;
      }));
      assert.equal(hintsDoNotOverlap, true);
      await page.evaluate(() => window.resolveTemplates([
        { id: "inactive", name: "标准社招流程", status: "inactive", rounds: [] },
        { id: "template-1", name: "标准流程", status: "active", rounds: ["一面"] },
      ]));
      await summary.getByText("4/6", { exact: true }).waitFor();
      assert.equal(await page.locator("#job-workflow-template").inputValue(), "template-1");
      await page.getByRole("checkbox", { name: "测试经理", exact: true }).check();
      await summary.getByText("5/6", { exact: true }).waitFor();
      assert.equal(await publish.isDisabled(), true);
      await page.locator("#job-default-reviewer").selectOption("manager-1");
      await summary.getByText("6/6", { exact: true }).waitFor();
      assert.equal(await publish.isEnabled(), true);
      await page.getByLabel(/^公开 JD/).fill("   ");
      await summary.getByText("5/6", { exact: true }).waitFor();
      await publish.click();
      assert.equal(await page.evaluate(() => window.saveCalls.length), 0);
      await page.getByLabel(/^公开 JD/).fill("完整职位描述");
      await page.getByLabel(/^招聘人数/).fill("0");
      await summary.getByText("5/6", { exact: true }).waitFor();
      await publish.click();
      assert.equal(await page.evaluate(() => window.saveCalls.length), 0);
      await page.getByLabel(/^招聘人数/).fill("1");
      await page.getByLabel("所属部门", { exact: true }).selectOption("");
      await page.getByLabel(/^必须条件/).fill("");
      await summary.getByText("6/6", { exact: true }).waitFor();
      const overflow = await page.evaluate(() => document.documentElement.scrollWidth > innerWidth);
      assert.equal(overflow, false);
      await publish.click();
      await page.waitForFunction(() => window.saveCalls.length === 1);
      assert.equal(await page.evaluate(() => window.saveCalls[0].options.publish), true);
    } finally { await page.close(); }
  });
}

for (const width of [1280, 390]) {
  test(`job pending onboarding funnel filters and opens the original candidate at ${width}px`, async () => {
    const page = await browser.newPage({ viewport: { width, height: 850 } });
    try {
      const html = await vite.transformIndexHtml("/", '<html><body><div id="root"></div></body></html>');
      await page.route("**/job-view-test", (route) => route.fulfill({ contentType: "text/html", body: html }));
      await page.goto(`${vite.resolvedUrls.local[0]}job-view-test`);
      await page.evaluate(async () => {
        const { default: React } = await import("/node_modules/.vite/deps/react.js");
        const { default: ReactDOM } = await import("/node_modules/.vite/deps/react-dom_client.js");
        const { JobsWorkspace } = await import("/src/JobViews.jsx");
        await import("/src/product-theme.css");
        const job = { id: "job-1", title: "工程师", status: "招聘中", funnel: { pending_onboarding: 1, hired: 2 } };
        const records = [true, false].map((pendingOnboarding, index) => ({ candidateId: `c${index}`, applicationId: `a${index}`, jobId: job.id, name: index ? "已提交候选人" : "待入职候选人", role: "工程师", stage: "已录用", pendingOnboarding, score: "-", owner: "HR", lastActivity: "今天" }));
        window.filterCalls = [];
        const controller = { async loadDefinition() { return job; }, mergeDefinition() { return job; } };
        const candidates = { async listCandidates(filters) { window.filterCalls.push(filters); return { records: filters.stage === "待入职" ? records.filter((item) => item.pendingOnboarding) : records, nextCursor: null }; } };
        ReactDOM.createRoot(document.getElementById("root")).render(React.createElement(JobsWorkspace, {
          mode: "detail", selectedJob: job, setSelectedJob() {}, setMode() {}, listState: { departments: [], owners: [] },
          jobController: controller, candidateController: candidates, onOpenCandidate(candidate) { window.openedCandidate = candidate; },
        }));
      });
      const funnel = page.locator(".funnel-strip").getByRole("button", { name: "待入职" });
      await funnel.waitFor();
      assert.equal(await funnel.locator("strong").textContent(), "1");
      assert.equal(await page.getByLabel("候选人阶段", { exact: true }).inputValue(), "全部阶段");
      assert.equal(await page.locator(".detail-candidate-table .stage-pill").allTextContents().then((items) => items.join(",")), "待入职,已录用");
      await funnel.click();
      await page.waitForFunction(() => window.filterCalls.at(-1).stage === "待入职");
      await page.locator(".detail-candidate-table").getByRole("button", { name: /已提交候选人/ }).waitFor({ state: "detached" });
      assert.equal(await funnel.getAttribute("aria-pressed"), "true");
      const row = page.locator(".detail-candidate-table").getByRole("button", { name: /待入职候选人/ });
      await row.focus();
      await page.keyboard.press("Enter");
      assert.deepEqual(await page.evaluate(() => [window.openedCandidate.applicationId, window.openedCandidate.stage, window.openedCandidate.pendingOnboarding]), ["a0", "已录用", true]);
      await page.getByLabel("候选人阶段", { exact: true }).selectOption("全部阶段");
      await page.locator(".detail-candidate-table").getByRole("button", { name: /已提交候选人/ }).waitFor();
      await page.getByLabel("候选人阶段", { exact: true }).selectOption("待入职");
      await page.waitForFunction(() => window.filterCalls.at(-1).stage === "待入职");
      assert.equal(await page.locator(".funnel-strip").evaluate((el) => getComputedStyle(el).overflowX), "auto");
    } finally { await page.close(); }
  });
}
