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

for (const reschedule of [false, true]) {
  test(`${reschedule ? "rescheduling" : "new scheduling"} allows one-hour adjacent interviews but blocks actual overlaps`, { timeout: 30000 }, async () => {
    const page = await browser.newPage({ viewport: { width: 1280, height: 900 } });
    page.setDefaultTimeout(10000);
    try {
      const html = await vite.transformIndexHtml("/", '<html><body><div id="root"></div></body></html>');
      await page.route("**/adjacent-test", (route) => route.fulfill({ contentType: "text/html", body: html }));
      await page.goto(`${vite.resolvedUrls.local[0]}adjacent-test`);
      await page.evaluate(async (reschedule) => {
        const { default: React } = await import("/node_modules/.vite/deps/react.js");
        const { default: ReactDOM } = await import("/node_modules/.vite/deps/react-dom_client.js");
        const { ScheduleWorkspace } = await import("/src/ScheduleWorkspace.jsx");
        const { createInterviewController } = await import("/src/interviewController.js");
        await import("/src/styles.css");
        await import("/src/product-theme.css");
        const userId = "11111111-1111-4111-8111-111111111111";
        const candidate = { id: "candidate-1", applicationId: "application-1", name: "排期候选人", position: "工程师", stage: "待安排" };
        window.availabilityCalls = [];
        window.saveCalls = [];
        const controller = createInterviewController({ client: { async request(path) {
          const params = new URL(path, location.origin).searchParams;
          window.availabilityCalls.push(Object.fromEntries(params));
          const start = new Date(new Date(params.get("from")).getTime() + 15 * 60 * 60 * 1000);
          return { data: { buffer_minutes: Number(params.get("buffer")), participants: [{ participant_id: userId, status: "confirmed", busy: [{ starts_at: start.toISOString(), ends_at: new Date(start.getTime() + 60 * 60 * 1000).toISOString() }] }] } };
        } } });
        const record = reschedule ? { id: "interview-1", candidateId: candidate.id, candidate: candidate.name, position: candidate.position, date: "2099-07-13", time: "10:00", duration: 60, interviewerIds: [userId] } : null;
        ReactDOM.createRoot(document.getElementById("root")).render(React.createElement(ScheduleWorkspace, {
          record, candidateId: candidate.id, candidates: [candidate], participantOptions: [{ id: userId, name: "面试官" }], participantStatus: "ready",
          onGetAvailability: controller.availability, onSave: async (record, form) => { window.saveCalls.push({ record, form }); return {}; }, onNotify() {}, onBack() {},
        }));
      }, reschedule);
      assert.equal(await page.getByLabel(/^时长/).inputValue(), "60");
      await page.getByRole("button", { name: /下一步：选择面试官/ }).click();
      await page.locator('.interviewer-picker input[type="checkbox"]').check();
      await page.getByRole("button", { name: /下一步：选择日期时间/ }).click();
      await page.locator(".schedule-slot-grid").waitFor();
      await page.getByLabel("选择排期周", { exact: true }).fill("2099-07-13");
      await page.locator(".schedule-slot-grid").waitFor();
      const day = page.locator(".schedule-slot-grid > section").first();
      const slot = (time) => day.getByRole("button", { name: new RegExp(`^${time}`) });
      await page.waitForFunction(() => window.availabilityCalls.at(-1).from.startsWith("2099-"));
      assert.equal(await slot("14:00").isEnabled(), true, "14:00-15:00 must be selectable before 15:00-16:00");
      assert.equal(await slot("16:00").isEnabled(), true, "16:00-17:00 must be selectable after 15:00-16:00");
      assert.equal(await slot("14:30").isDisabled(), true);
      assert.equal(await slot("15:00").isDisabled(), true);
      assert.equal(await slot("15:30").isDisabled(), true);
      assert.equal(await page.evaluate(() => window.availabilityCalls.at(-1).buffer), "0");
      assert.equal(await page.evaluate(() => window.availabilityCalls.at(-1).exclude || ""), reschedule ? "interview-1" : "");
      await slot("14:00").click();
      await page.getByRole("button", { name: "确认并保存", exact: true }).click();
      await page.waitForFunction(() => window.saveCalls.length === 1);
      assert.deepEqual(await page.evaluate(() => ({ time: window.saveCalls[0].form.time, duration: window.saveCalls[0].form.duration, override: window.saveCalls[0].form.allowSoftConflict })), { time: "14:00", duration: 60, override: false });
      await page.getByRole("button", { name: "上一步", exact: true }).click();
      await page.getByRole("button", { name: "上一步", exact: true }).click();
      await page.getByRole("button", { name: "上一步", exact: true }).click();
      await page.getByLabel(/^时长/).selectOption("90");
      await page.getByRole("button", { name: /下一步：选择面试官/ }).click();
      await page.getByRole("button", { name: /下一步：选择日期时间/ }).click();
      await page.locator(".schedule-slot-grid").waitFor();
      assert.equal(await slot("14:00").isDisabled(), true, "90-minute interview at 14:00 actually overlaps 15:00");
      assert.equal(await slot("13:30").isEnabled(), true);
    } finally { await page.close(); }
  });
}
