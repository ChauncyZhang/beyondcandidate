import test, { before, after } from "node:test";
import assert from "node:assert/strict";
import { fileURLToPath } from "node:url";
import { chromium } from "playwright";
import { createServer } from "vite";

let browser, vite, baseUrl;
before(async () => {
  vite = await createServer({ root: fileURLToPath(new URL("../", import.meta.url)), logLevel: "silent", server: { host: "127.0.0.1", port: 0 } });
  await vite.listen(); baseUrl = vite.resolvedUrls.local[0];
  browser = await chromium.launch({ headless: true });
});
after(async () => { await browser?.close(); await vite?.close(); });

for (const width of [1440, 390]) {
  test(`member status label and select stay aligned and contained at ${width}px`, { timeout: 25000 }, async () => {
    const context = await browser.newContext({ viewport: { width, height: 900 } });
    try {
      await context.route("**/api/v1/**", async (route) => {
        const path = new URL(route.request().url()).pathname;
        let data = [];
        if (path === "/api/v1/me") data = { id: "actor", display_name: "管理员", roles: ["recruiting_admin"] };
        if (path === "/api/v1/auth/config") data = { default_organization: { slug: "test", name: "Test" } };
        if (path === "/api/v1/settings/users") data = [
          { id: "active", display_name: "在职同事", roles: ["recruiter"], status: "active" },
          { id: "invited", display_name: "待激活同事", roles: ["interviewer"], status: "invited" },
          { id: "disabled", display_name: "停用同事", roles: ["interviewer"], status: "disabled" },
          { id: "removed", display_name: "离职同事", roles: ["recruiter"], status: "disabled", offboarding: { removed_at: "2026-10-26T03:00:00Z", successor_user_id: "active", handover_status: "pending", pending_count: 1 } },
        ];
        await route.fulfill({ status: 200, contentType: "application/json", body: JSON.stringify({ data }) });
      });
      const page = await context.newPage();
      await page.goto(`${baseUrl}settings/organization/members`);
      const select = page.getByRole("combobox", { name: "成员状态", exact: true });
      await select.waitFor();
      const geometry = await select.evaluate((element) => {
        const label = element.closest("label");
        const range = document.createRange();
        range.selectNodeContents(Array.from(label.childNodes).find((node) => node.nodeType === Node.TEXT_NODE && node.textContent.trim()));
        const rect = (node) => { const { x, y, width, height } = node.getBoundingClientRect(); return { x, y, width, height }; };
        return { label: rect(label), select: rect(element), text: rect(range), appearance: getComputedStyle(element).appearance };
      });
      const { label, select: control, text } = geometry;
      assert.ok(control.y >= label.y && control.y + control.height <= label.y + label.height + 1, "select must fit inside its label, not float below it");
      assert.ok(control.x >= text.x + text.width + 4, "visible label and selected value must occupy separate columns");
      assert.ok(Math.abs(text.y + text.height / 2 - control.y - control.height / 2) <= 2, "label and select must be vertically centered");
      assert.ok(control.height >= 40 && control.width >= 110, "select must have usable dimensions");
      assert.notEqual(geometry.appearance, "none", "native dropdown affordance must remain visible");
      assert.ok(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth));
      const invitedRow = page.locator(".settings-table-row").filter({ has: page.getByText("待激活同事", { exact: true }) });
      await invitedRow.getByRole("button", { name: "移除成员并交接", exact: true }).waitFor();
      assert.equal(await invitedRow.getByRole("button").count(), 2, "invited member keeps reinvitation alongside removal");
      assert.equal(await invitedRow.getByRole("button", { name: "重新邀请", exact: true }).count(), 1);
      assert.equal(await page.locator(".settings-table-row").filter({ has: page.getByText("停用同事", { exact: true }) }).getByRole("button", { name: "移除成员并交接", exact: true }).count(), 1);
      const actionBounds = await invitedRow.locator(".member-row-action").boundingBox();
      const buttons = await invitedRow.getByRole("button").all();
      const bounds = await Promise.all(buttons.map((button) => button.boundingBox()));
      for (const rect of bounds) {
        assert.ok(rect.x >= actionBounds.x - 1 && rect.x + rect.width <= actionBounds.x + actionBounds.width + 1, "invited actions must fit within their column");
        assert.ok(rect.y >= actionBounds.y - 1 && rect.y + rect.height <= actionBounds.y + actionBounds.height + 1, "wrapped actions must fit within their row");
      }
      assert.ok(bounds[0].x + bounds[0].width <= bounds[1].x || bounds[1].x + bounds[1].width <= bounds[0].x || bounds[0].y + bounds[0].height <= bounds[1].y || bounds[1].y + bounds[1].height <= bounds[0].y, "invited actions must not overlap");
      await page.screenshot({ path: fileURLToPath(new URL(`../.tmp/member-invited-actions-${width}.png`, import.meta.url)), fullPage: true });
      await select.selectOption("pending");
      await page.getByText("离职同事", { exact: true }).waitFor();
      assert.equal(await page.getByText("在职同事", { exact: true }).count(), 0);
      const removedRow = page.locator(".settings-table-row").filter({ has: page.getByText("离职同事", { exact: true }) });
      const badge = removedRow.locator(".status-muted");
      assert.equal((await badge.textContent()).trim(), "已移除", "status badge must not contain handover, removal time or successor details");
      const badgeGeometry = await badge.evaluate((element) => {
        const range = document.createRange(); range.selectNodeContents(element);
        return { lines: range.getClientRects().length, textWidth: range.getBoundingClientRect().width, width: element.getBoundingClientRect().width, whiteSpace: getComputedStyle(element).whiteSpace };
      });
      assert.equal(badgeGeometry.lines, 1, "removed badge must stay on a single line");
      assert.equal(badgeGeometry.whiteSpace, "nowrap");
      assert.ok(badgeGeometry.width <= badgeGeometry.textWidth + 20, "badge must hug its label rather than fill the status cell");
      const identity = removedRow.locator(":scope > span").first();
      const summary = identity.locator(".offboarding-member-summary");
      assert.match(await summary.textContent(), /接任人：在职同事/);
      const time = summary.locator("time");
      assert.equal(await time.getAttribute("datetime"), "2026-10-26T03:00:00Z");
      assert.equal(await time.evaluate((element) => { const range = document.createRange(); range.selectNodeContents(element); return range.getClientRects().length; }), 1, "removal timestamp must not break mid-date");
      const identityBounds = await identity.boundingBox();
      const summaryBounds = await summary.boundingBox();
      assert.ok(summaryBounds.x >= identityBounds.x && summaryBounds.x + summaryBounds.width <= identityBounds.x + identityBounds.width + 1, "summary must fit inside the member identity column");
      assert.equal(await removedRow.locator(".offboarding-handover-status").textContent(), "待交接 1 项");
      assert.ok(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth));
      await page.screenshot({ path: fileURLToPath(new URL(`../.tmp/member-status-filter-${width}.png`, import.meta.url)), fullPage: true });
    } finally { await context.close(); }
  });
}
