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

async function openPage({ width = 1440, noSuccessor = false, conflict = false } = {}) {
  const context = await browser.newContext({ viewport: { width, height: 900 } });
  const writes = [];
  let removed = false, reads = 0;
  const item = { category: "offers", resource_type: "offer_approval", resource_id: "approval", title: "待审批录用通知", status: "blocked", reason_code: "successor_role_required", reason: "接任人缺少审批角色，请先配置角色后重试。" };
  await context.route("**/api/v1/**", async (route) => {
    const request = route.request(), url = new URL(request.url());
    let data = [];
    if (url.pathname === "/api/v1/me") data = { id: "current", display_name: "管理员", roles: ["system_admin"] };
    if (url.pathname === "/api/v1/auth/config") data = { default_organization: { slug: "acme", name: "Acme" } };
    if (url.pathname === "/api/v1/settings/users") data = [
      { id: "source", display_name: "离职成员", email: "departed@example.test", roles: ["recruiter"], status: removed ? "disabled" : "active", ...(removed ? { offboarding: { removed_at: "2026-10-26T03:00:00Z", handover_status: "pending", pending_count: 1, successor_user_id: "next" } } : {}) },
      { id: "next", display_name: "接任人", email: "next@example.test", roles: ["recruiter"], status: "active" },
    ];
    if (url.pathname === "/api/v1/settings/users/source/offboarding") {
      if (request.method() === "POST") {
        writes.push(request.postDataJSON());
        if (conflict) return route.fulfill({ status: 409, contentType: "application/problem+json", body: JSON.stringify({ code: "offboarding_preview_changed" }) });
        removed = true;
      } else { reads++; }
      data = { user_id: "source", user_name: "离职成员", removed_at: removed ? "2026-10-26T03:00:00Z" : null,
        successor_user_id: url.searchParams.get("successor_id") || (removed && !noSuccessor ? "next" : null),
        handover_status: removed ? "pending" : "not_started", preview_version: `preview-${url.searchParams.get("successor_id") || "none"}`,
        successors: noSuccessor ? [] : [{ id: "next", display_name: "接任人", eligible: true, reason: null }],
        counts: [{ category: "offers", label: "Offer 审批", total: 1, transferable: 0, blocked: 1 }],
        items: [item], transferred_count: removed && !noSuccessor ? 2 : 0 };
    }
    await route.fulfill({ status: 200, contentType: "application/json", headers: { "X-CSRF-Token": "offboarding-test-csrf" }, body: JSON.stringify({ data }) });
  });
  const page = await context.newPage(); page.setDefaultTimeout(8000);
  await page.goto(`${baseUrl}settings/organization/members`);
  const sourceRow = page.locator(".settings-table-row").filter({ has: page.getByText("离职成员", { exact: true }) });
  await sourceRow.getByRole("button", { name: "移除成员并交接", exact: true }).click();
  const dialog = page.getByRole("dialog", { name: "移除成员并交接", exact: true });
  await dialog.getByLabel("统一接任人").waitFor();
  return { context, page, dialog, writes };
}

for (const width of [1440, 390]) {
  test(`offboarding confirmation stays readable on hover and focus at ${width}px`, { timeout: 30000 }, async () => {
    const { context, page, dialog, writes } = await openPage({ width });
    try {
      await dialog.getByLabel("统一接任人").selectOption("next");
      await dialog.getByRole("button", { name: "移除成员并交接", exact: true }).click();
      const confirm = dialog.getByRole("button", { name: "确认移除并交接", exact: true });
      const colors = () => confirm.evaluate(async (element) => {
        await Promise.all(element.getAnimations().map((animation) => animation.finished));
        const style = getComputedStyle(element);
        return { color: style.color, background: style.backgroundColor };
      });
      const disabledColors = await colors();
      await confirm.hover({ force: true });
      assert.equal(await confirm.isDisabled(), true);
      assert.deepEqual(await colors(), disabledColors, "disabled hover must not change its colors");
      await dialog.getByRole("checkbox").check();
      await page.mouse.move(0, 0);
      const contrast = ({ color, background }) => {
        const luminance = (rgb) => rgb.match(/[\d.]+/g).slice(0, 3).map(Number).map((value) => {
          const channel = value / 255;
          return channel <= 0.04045 ? channel / 12.92 : ((channel + 0.055) / 1.055) ** 2.4;
        }).reduce((sum, channel, index) => sum + channel * [0.2126, 0.7152, 0.0722][index], 0);
        const values = [luminance(color), luminance(background)].sort((a, b) => b - a);
        return (values[0] + 0.05) / (values[1] + 0.05);
      };
      assert.ok(contrast(await colors()) >= 4.5, "normal text must have readable contrast");
      await confirm.hover();
      assert.ok(contrast(await colors()) >= 4.5, "hover text must have readable contrast");
      await page.screenshot({ path: fileURLToPath(new URL(`../.tmp/member-offboarding-hover-${width}.png`, import.meta.url)), fullPage: true });
      await page.mouse.move(0, 0);
      await confirm.focus();
      assert.ok(contrast(await colors()) >= 4.5, "focused text must have readable contrast");
      assert.equal(writes.length, 0, "hover and focus must never submit the removal");
    } finally { await context.close(); }
  });

  test(`single-successor confirmation, partial result and pending resume at ${width}px`, { timeout: 30000 }, async () => {
    const { context, page, dialog, writes } = await openPage({ width });
    try {
      assert.equal(writes.length, 0, "opening preview must not mutate");
      await dialog.getByLabel("统一接任人").selectOption("next");
      await dialog.getByRole("button", { name: "移除成员并交接", exact: true }).click();
      const confirm = dialog.getByRole("button", { name: "确认移除并交接", exact: true });
      assert.equal(await confirm.isEnabled(), false);
      await dialog.getByRole("checkbox").check();
      assert.match(await dialog.locator(".offboarding-items").textContent(), /权限不足.*缺少审批角色/);
      assert.ok(await page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth));
      const bounds = await dialog.boundingBox(); assert.ok(bounds.x >= 0 && bounds.x + bounds.width <= width + 1);
      await page.screenshot({ path: fileURLToPath(new URL(`../.tmp/member-offboarding-${width}.png`, import.meta.url)), fullPage: true });
      await confirm.click();
      await dialog.getByText(/账号已收回 · 仍有待交接事项/).waitFor();
      assert.equal(writes.length, 1);
      assert.deepEqual(writes[0], { successor_user_id: "next", preview_version: "preview-next", revoke_only: false });
      await dialog.getByRole("button", { name: "完成", exact: true }).click();
      await page.getByLabel("成员状态").selectOption("pending");
      const row = page.locator(".settings-table-row").filter({ has: page.getByText("离职成员", { exact: true }) });
      await row.getByRole("button", { name: "继续交接", exact: true }).click();
      const resumed = page.getByRole("dialog", { name: "继续成员交接", exact: true });
      await resumed.getByLabel("统一接任人").waitFor();
      assert.equal(await resumed.getByLabel("统一接任人").inputValue(), "next");
    } finally { await context.close(); }
  });
}

test("revoke-only is possible with no successor and keeps unresolved work visible", { timeout: 25000 }, async () => {
  const { context, dialog, writes } = await openPage({ noSuccessor: true });
  try {
    assert.equal(await dialog.getByRole("button", { name: "移除成员并交接", exact: true }).isDisabled(), true);
    await dialog.getByRole("button", { name: "仅收回账号，稍后交接", exact: true }).click();
    await dialog.getByRole("checkbox").check();
    await dialog.getByRole("button", { name: "确认仅收回账号", exact: true }).click();
    await dialog.getByText(/账号已收回 · 仍有待交接事项/).waitFor();
    assert.equal(writes[0].successor_user_id, null); assert.equal(writes[0].revoke_only, true);
    assert.match(await dialog.locator(".offboarding-items").textContent(), /缺少审批角色/);
  } finally { await context.close(); }
});

test("stale preview requires explicit refresh and preserves selected successor", { timeout: 25000 }, async () => {
  const { context, dialog, writes } = await openPage({ conflict: true });
  try {
    await dialog.getByLabel("统一接任人").selectOption("next");
    await dialog.getByRole("button", { name: "移除成员并交接", exact: true }).click();
    await dialog.getByRole("checkbox").check();
    await dialog.getByRole("button", { name: "确认移除并交接", exact: true }).click();
    await dialog.getByRole("alert").waitFor();
    assert.equal(writes.length, 1);
    assert.match(await dialog.getByRole("alert").textContent(), /刷新预览/);
    await dialog.getByRole("button", { name: "刷新预览", exact: true }).and(dialog.locator("button:enabled")).click();
    assert.equal(await dialog.getByLabel("统一接任人").inputValue(), "next");
    assert.equal(writes.length, 1);
  } finally { await context.close(); }
});
