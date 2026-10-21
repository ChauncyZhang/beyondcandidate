import assert from "node:assert/strict";
import { after, before, test } from "node:test";
import { fileURLToPath } from "node:url";
import { createElement } from "react";
import { renderToStaticMarkup } from "react-dom/server";
import { chromium } from "playwright";
import { createServer } from "vite";

let vite;
let ImportWizard;

before(async () => {
  vite = await createServer({
    root: fileURLToPath(new URL("../", import.meta.url)),
    logLevel: "silent",
    server: { host: "127.0.0.1", port: 0 },
  });
  await vite.listen();
  ({ ImportWizard } = await vite.ssrLoadModule("/src/ScreeningViews.jsx"));
});

after(async () => { await vite?.close(); });

test("batch note defaults use the local month at each mount, including year rollover", (t) => {
  t.mock.timers.enable({ apis: ["Date"], now: new Date(2026, 4, 15).getTime() });
  for (const [year, month, day] of [[2026, 5, 15], [2026, 12, 31], [2027, 1, 1]]) {
    t.mock.timers.setTime(new Date(year, month - 1, day).getTime());
    const html = renderToStaticMarkup(createElement(ImportWizard, { activeJob: "AI 工程师" }));
    assert.match(html, new RegExp(`<textarea[^>]*>${month} 月 AI 工程师主动搜寻批次</textarea>`));
    assert.match(html, /placeholder="例如：AI 工程师主动搜寻批次"/);
  }
});

test("batch note preserves manual edits through rerenders and steps, then refreshes on reopen", async () => {
  const browser = await chromium.launch({ headless: true });
  try {
    const page = await browser.newPage({ timezoneId: "Asia/Shanghai" });
    page.setDefaultTimeout(10000);
    await page.clock.install({ time: new Date("2026-12-31T23:59:00+08:00") });
    const html = await vite.transformIndexHtml("/", '<html><body><div id="root"></div></body></html>');
    await page.route("**/batch-note-test", (route) => route.fulfill({ contentType: "text/html", body: html }));
    await page.goto(`${vite.resolvedUrls.local[0]}batch-note-test`);
    await page.evaluate(async () => {
      const { default: React } = await import("/node_modules/.vite/deps/react.js");
      const { default: ReactDOM } = await import("/node_modules/.vite/deps/react-dom_client.js");
      const { ImportWizard } = await import("/src/ScreeningViews.jsx");
      const root = ReactDOM.createRoot(document.getElementById("root"));
      const controller = { async listJobs() { return [{ id: "job-1", title: "AI 工程师" }]; } };
      window.mountWizard = (key) => root.render(React.createElement(ImportWizard, {
        key, activeJob: "AI 工程师", controller, onClose() {},
      }));
      window.mountWizard("december");
    });
    const note = page.getByLabel(/^批次说明/);
    await note.waitFor();
    await page.waitForFunction(() => !document.querySelector(".screening-modal-footer .primary").disabled);
    assert.equal(await note.inputValue(), "12 月 AI 工程师主动搜寻批次");
    assert.equal(await note.getAttribute("placeholder"), "例如：AI 工程师主动搜寻批次");

    const manualNote = "专项内推批次（保留手工备注）";
    await note.fill(manualNote);
    await page.clock.setFixedTime(new Date("2027-01-01T00:01:00+08:00"));
    await page.getByLabel(/^简历来源/).selectOption("员工内推");
    assert.equal(await note.inputValue(), manualNote);
    await page.getByRole("button", { name: "下一步", exact: true }).click();
    await page.locator('input[type="file"]').setInputFiles({
      name: "resume.txt", mimeType: "text/plain", buffer: Buffer.from("Synthetic resume"),
    });
    await page.getByRole("button", { name: "下一步", exact: true }).click();
    assert.equal(await page.locator(".confirmation-summary dl div").filter({ has: page.locator("dt", { hasText: "批次说明" }) }).locator("dd").textContent(), manualNote);
    await page.getByRole("button", { name: "上一步", exact: true }).click();
    await page.getByRole("button", { name: "上一步", exact: true }).click();
    assert.equal(await note.inputValue(), manualNote);
    await note.fill("");
    await page.getByLabel(/^简历来源/).selectOption("猎聘");
    assert.equal(await note.inputValue(), "");

    await page.evaluate(() => window.mountWizard("january"));
    await page.waitForFunction(() => document.querySelector("textarea")?.value === "1 月 AI 工程师主动搜寻批次");
    assert.equal(await note.inputValue(), "1 月 AI 工程师主动搜寻批次");
  } finally {
    await browser.close();
  }
});
