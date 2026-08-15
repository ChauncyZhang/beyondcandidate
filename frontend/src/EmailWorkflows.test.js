import assert from "node:assert/strict";
import test, { after, before } from "node:test";
import { fileURLToPath } from "node:url";
import { chromium } from "playwright";
import { createServer } from "vite";

const prototypeRoot = fileURLToPath(new URL("../", import.meta.url));
const candidateId = "30000000-0000-4000-8000-000000000001";
const applicationId = "20000000-0000-4000-8000-000000000001";
const jobId = "10000000-0000-4000-8000-000000000001";
const nativeContactId = "41000000-0000-4000-8000-000000000001";
const ocrContactId = "41000000-0000-4000-8000-000000000002";
let browser;
let vite;
let baseUrl;

before(async () => {
  vite = await createServer({ root: prototypeRoot, logLevel: "silent", server: { host: "127.0.0.1", port: 0 } });
  await vite.listen();
  baseUrl = vite.resolvedUrls.local[0];
  browser = await chromium.launch({ headless: true });
});

after(async () => {
  await browser?.close();
  await vite?.close();
});

function emailConfig(version = 3) {
  return {
    configured: true,
    host: "smtp.example.test",
    port: 587,
    tls_mode: "starttls",
    username: "mailer",
    password_masked: "********",
    enabled: true,
    version,
    sender_name: "星河招聘",
    sender_address: "jobs@example.test",
    default_reply_to_email: "hr@example.test",
    default_reply_to_name: "招聘团队",
  };
}

async function openSettings({ role = "system_admin", conflictOnce = false, startPath = "settings/organization/members" } = {}) {
  const context = await browser.newContext({ viewport: { width: 1280, height: 800 } });
  const requests = { get: 0, put: [], test: [], logout: 0 };
  let config = emailConfig();
  let shouldConflict = conflictOnce;
  await context.route("**/api/v1/**", async (route) => {
    const request = route.request();
    const pathname = new URL(request.url()).pathname.replace(/\/$/, "");
    if (pathname === "/api/v1/me") return route.fulfill({ status: 200, contentType: "application/json", headers: { "x-csrf-token": "email-workflow" }, body: JSON.stringify({ data: { id: "user-1", display_name: "Admin", roles: [role] } }) });
    if (pathname === "/api/v1/settings/email" && request.method() === "GET") {
      requests.get += 1;
      return route.fulfill({ status: 200, contentType: "application/json", body: JSON.stringify({ data: config }) });
    }
    if (pathname === "/api/v1/settings/email" && request.method() === "PUT") {
      requests.put.push({ body: request.postDataJSON(), headers: request.headers() });
      if (shouldConflict) {
        shouldConflict = false;
        config = emailConfig(4);
        return route.fulfill({ status: 409, contentType: "application/problem+json", body: JSON.stringify({ status: 409, code: "resource_version_conflict" }) });
      }
      config = { ...config, ...request.postDataJSON(), version: config.version + 1 };
      return route.fulfill({ status: 200, contentType: "application/json", body: JSON.stringify({ data: config }) });
    }
    if (pathname === "/api/v1/settings/email/test") {
      requests.test.push(request.postDataJSON());
      return route.fulfill({ status: 202, contentType: "application/json", body: JSON.stringify({ data: { id: "delivery-1", status: "queued" } }) });
    }
    if (pathname === "/api/v1/auth/logout") {
      requests.logout += 1;
      return route.fulfill({ status: 204, body: "" });
    }
    return route.fulfill({ status: 503, contentType: "application/problem+json", body: JSON.stringify({ status: 503, code: "service_unavailable" }) });
  });
  const page = await context.newPage();
  await page.goto(`${baseUrl}${startPath}`);
  return { context, page, requests };
}

function candidateEmail(version = 2) {
  return {
    masked_value: "n***@example.com",
    value: "native@example.com",
    source: "native",
    confirmation_status: "unconfirmed",
    confirmed_at: null,
    version,
    addresses: [
      { id: nativeContactId, masked_value: "n***@example.com", value: "native@example.com", source: "native", confirmation_status: "unconfirmed", confirmed_at: null, version },
      { id: ocrContactId, masked_value: "o***@example.com", value: "ocr@example.com", source: "ocr", confirmation_status: "unconfirmed", confirmed_at: null, version: 1 },
    ],
  };
}

async function openCandidate({ role = "recruiter", conflictOnce = false, conflictCode = "resource_version_conflict", holdPut = false, viewport = { width: 1280, height: 800 } } = {}) {
  const context = await browser.newContext({ viewport });
  const requests = { emailGet: 0, emailPut: [], governanceGet: 0 };
  let releasePut;
  let putGate = holdPut ? new Promise((resolve) => { releasePut = resolve; }) : null;
  requests.releasePut = () => { releasePut?.(); releasePut = null; };
  let email = candidateEmail();
  let shouldConflict = conflictOnce;
  await context.route("**/api/v1/**", async (route) => {
    const request = route.request();
    const pathname = new URL(request.url()).pathname.replace(/\/$/, "");
    const json = (data, status = 200) => route.fulfill({ status, contentType: "application/json", body: JSON.stringify({ data }) });
    if (pathname === "/api/v1/me") return route.fulfill({ status: 200, contentType: "application/json", headers: { "x-csrf-token": "candidate-email" }, body: JSON.stringify({ data: { id: "user-1", display_name: "HR", roles: [role] } }) });
    if (pathname === `/api/v1/candidates/${candidateId}`) return json({ id: candidateId, display_name: "陈曦", current_title: "算法工程师", location: "上海", version: 1, contacts: [{ kind: "email", value: "n***@example.com" }] });
    if (pathname === `/api/v1/candidates/${candidateId}/applications`) return json([{ id: applicationId, candidate_id: candidateId, job_id: jobId, job_title: "AI 工程师", job_status: "open", stage: "contact", source: "upload", owner_id: "user-1", owner_name: "HR", version: 2 }]);
    if (pathname === `/api/v1/candidates/${candidateId}/resumes` || pathname === `/api/v1/candidates/${candidateId}/timeline` || pathname === `/api/v1/candidates/${candidateId}/notes`) return json([]);
    if (pathname === `/api/v1/candidates/${candidateId}/email` && request.method() === "GET") {
      requests.emailGet += 1;
      return json(email);
    }
    if (pathname === `/api/v1/candidates/${candidateId}/email` && request.method() === "PUT") {
      requests.emailPut.push({ body: request.postDataJSON(), headers: request.headers() });
      if (putGate) { await putGate; putGate = null; }
      if (shouldConflict) {
        shouldConflict = false;
        email = candidateEmail(3);
        return route.fulfill({ status: 409, contentType: "application/problem+json", body: JSON.stringify({ status: 409, code: conflictCode }) });
      }
      email = { ...email, value: "ocr@example.com", masked_value: "o***@example.com", source: "ocr", confirmation_status: "confirmed", version: email.version + 1 };
      return json(email);
    }
    if (pathname === `/api/v1/candidates/${candidateId}/governance-status`) {
      requests.governanceGet += 1;
      return json({ deletion_status: null, deletion_request_id: null, legal_hold_active: false });
    }
    if (pathname === "/api/v1/jobs") return route.fulfill({ status: 200, contentType: "application/json", body: JSON.stringify({ data: [], meta: { next_cursor: null } }) });
    return route.fulfill({ status: 503, contentType: "application/problem+json", body: JSON.stringify({ status: 503, code: "service_unavailable" }) });
  });
  const page = await context.newPage();
  await page.goto(`${baseUrl}candidates/${candidateId}?application=${applicationId}&job=${jobId}`);
  await page.getByRole("heading", { name: "陈曦", exact: true }).waitFor();
  return { context, page, requests };
}

test("system administrator edits sender without replacing the SMTP password, saves reply-to, tests saved config, and recovers a stale version", { timeout: 60_000 }, async () => {
  const { context, page, requests } = await openSettings({ conflictOnce: true, startPath: "settings/ai" });
  try {
    await page.getByRole("heading", { name: "邮件发送设置", exact: true }).waitFor();
    assert.equal(await page.getByLabel("发件人名称").inputValue(), "星河招聘");
    assert.equal(await page.getByLabel("发件地址").inputValue(), "jobs@example.test");
    assert.equal(await page.getByLabel("替换 SMTP 密码").inputValue(), "");
    assert.deepEqual(await page.getByLabel("连接加密").locator("option").allTextContents(), ["STARTTLS（连接后升级加密）", "SSL/TLS（连接即加密）"]);
    await page.getByLabel("发件人名称").fill("星河人才团队");
    await page.getByLabel("发件地址").fill("talent@example.test");
    await page.getByLabel("默认回复地址").fill("talent@example.test");
    await page.getByLabel("默认回复名称").fill("人才招聘团队");
    assert.equal(await page.getByRole("button", { name: "发送测试邮件" }).isDisabled(), true);
    await page.getByRole("button", { name: "保存邮件设置" }).click();
    await page.getByRole("alert").filter({ hasText: "其他管理员" }).waitFor();
    const getsBeforeReload = requests.get;
    await page.getByRole("button", { name: "重新加载邮件设置" }).click();
    await page.getByText("已保存版本 4", { exact: true }).waitFor();
    assert.equal(requests.get, getsBeforeReload + 1);
    await page.getByLabel("默认回复名称").fill("人才招聘团队");
    await page.getByLabel("发件人名称").fill("星河人才团队");
    await page.getByLabel("发件地址").fill("talent@example.test");
    await page.getByRole("button", { name: "保存邮件设置" }).click();
    await page.getByText("已保存版本 5", { exact: true }).waitFor();
    assert.equal(requests.put.at(-1).body.default_reply_to_name, "人才招聘团队");
    assert.equal(requests.put.at(-1).body.sender_name, "星河人才团队");
    assert.equal(requests.put.at(-1).body.sender_address, "talent@example.test");
    assert.equal(Object.hasOwn(requests.put.at(-1).body, "password"), false);
    await page.getByLabel("测试收件人").fill("admin@example.test");
    await page.getByRole("button", { name: "发送测试邮件" }).click();
    await page.getByText("测试邮件已使用已保存配置进入发送队列", { exact: true }).waitFor();
    assert.deepEqual(requests.test, [{ recipient: "admin@example.test" }]);
  } finally { await context.close(); }
});

test("email settings stay hidden from non-system administrators", { timeout: 60_000 }, async () => {
  const { context, page, requests } = await openSettings({ role: "recruiting_admin", startPath: "settings/ai" });
  try {
    await page.getByRole("heading", { name: "AI 设置", exact: true }).waitFor();
    assert.equal(await page.getByRole("heading", { name: "邮件发送设置", exact: true }).count(), 0);
    assert.equal(requests.get, 0);
  } finally { await context.close(); }
});

test("dirty SMTP settings block section navigation, browser Back, and logout until explicitly discarded", { timeout: 60_000 }, async () => {
  const { context, page, requests } = await openSettings();
  try {
    await page.getByRole("button", { name: "AI 设置", exact: true }).click();
    await page.getByRole("heading", { name: "邮件发送设置", exact: true }).waitFor();
    await page.getByRole("button", { name: "飞书集成", exact: true }).click();
    await page.getByRole("heading", { name: "飞书集成", exact: true }).waitFor();
    await page.goBack();
    await page.getByRole("heading", { name: "邮件发送设置", exact: true }).waitFor();
    await page.getByLabel("SMTP 主机").fill("smtp.changed.test");
    assert.equal(await page.getByRole("button", { name: "保存邮件设置", exact: true }).isDisabled(), true);
    await page.getByLabel("替换 SMTP 密码").fill("replacement-only-in-memory");
    await page.getByRole("button", { name: "组织与权限", exact: true }).click();
    const guard = page.getByRole("dialog", { name: "未保存的设置", exact: true });
    await guard.waitFor();
    await guard.getByRole("button", { name: "继续编辑", exact: true }).click();
    assert.equal(new URL(page.url()).pathname, "/settings/ai");
    await page.goBack();
    await guard.waitFor();
    await guard.getByRole("button", { name: "继续编辑", exact: true }).click();
    assert.equal(new URL(page.url()).pathname, "/settings/ai");
    await page.goForward();
    await guard.waitFor();
    await guard.getByRole("button", { name: "继续编辑", exact: true }).click();
    assert.equal(new URL(page.url()).pathname, "/settings/ai");
    await page.getByRole("button", { name: "退出登录", exact: true }).click();
    await guard.waitFor();
    assert.equal(requests.logout, 0);
    await guard.getByRole("button", { name: "继续编辑", exact: true }).click();
    await page.getByRole("button", { name: "退出登录", exact: true }).click();
    await guard.getByRole("button", { name: "放弃修改并退出", exact: true }).click();
    await page.getByRole("heading", { name: "登录工作台", exact: true }).waitFor();
    assert.equal(requests.logout, 1);
  } finally { await context.close(); }
});

test("candidate plaintext is fetched only in the focused dialog and existing choices use contact ids with stale refresh", { timeout: 60_000 }, async () => {
  const { context, page, requests } = await openCandidate({ conflictOnce: true });
  try {
    assert.equal(requests.emailGet, 0);
    assert.equal((await page.locator("body").textContent()).includes("native@example.com"), false);
    const trigger = page.getByRole("button", { name: "查看并确认邮箱", exact: true });
    await trigger.click();
    const dialog = page.getByRole("dialog", { name: "确认候选人邮箱", exact: true });
    await dialog.getByText("native@example.com", { exact: true }).waitFor();
    const nativeOption = dialog.locator(".candidate-email-options > label").filter({ hasText: "native@example.com" });
    const optionLayout = await nativeOption.evaluate((element) => {
      const radio = element.querySelector('input[type="radio"]').getBoundingClientRect();
      const title = element.querySelector("strong").getBoundingClientRect();
      return { display: getComputedStyle(element).display, radioLeft: radio.left, radioRight: radio.right, radioTop: radio.top, titleLeft: title.left, titleTop: title.top };
    });
    assert.equal(optionLayout.display, "grid");
    assert.ok(optionLayout.radioLeft < optionLayout.titleLeft && optionLayout.radioRight <= optionLayout.titleLeft, JSON.stringify(optionLayout));
    assert.ok(Math.abs(optionLayout.radioTop - optionLayout.titleTop) <= 4, JSON.stringify(optionLayout));
    assert.ok(requests.emailGet >= 1);
    assert.match(await dialog.textContent(), /简历原文.*OCR 识别/s);
    assert.equal(await dialog.evaluate((element) => element.contains(document.activeElement)), true);
    await page.keyboard.press("Shift+Tab");
    assert.equal(await dialog.evaluate((element) => element.contains(document.activeElement)), true);
    await page.keyboard.press("Escape");
    await dialog.waitFor({ state: "hidden" });
    assert.equal(await trigger.evaluate((element) => element === document.activeElement), true);

    await trigger.click();
    await dialog.getByLabel(/ocr@example\.com/).check();
    await dialog.getByLabel(/我已与候选人核对/).check();
    await dialog.getByRole("button", { name: "确认并用于发送" }).click();
    await dialog.getByText("邮箱信息已被其他成员更新", { exact: false }).waitFor();
    const getsBeforeRefresh = requests.emailGet;
    await dialog.getByRole("button", { name: "加载最新邮箱", exact: true }).click();
    await dialog.getByText("native@example.com", { exact: true }).waitFor();
    assert.equal(requests.emailGet, getsBeforeRefresh + 1);
    await dialog.getByLabel(/ocr@example\.com/).check();
    await dialog.getByLabel(/我已与候选人核对/).check();
    await dialog.getByRole("button", { name: "确认并用于发送" }).click();
    await dialog.waitFor({ state: "hidden" });
    assert.deepEqual(requests.emailPut.map((item) => item.body), [{ contact_id: ocrContactId }, { contact_id: ocrContactId }]);
    assert.equal(requests.emailPut[0].headers["if-match"], '"2"');
    assert.equal(requests.emailPut[1].headers["if-match"], '"3"');
    assert.equal((await page.locator("body").textContent()).includes("ocr@example.com"), false);
  } finally { await context.close(); }
});

test("unauthorized candidate roles never see the plaintext entry point", { timeout: 60_000 }, async () => {
  const { context, page, requests } = await openCandidate({ role: "hiring_manager" });
  try {
    assert.equal(await page.getByRole("button", { name: "查看并确认邮箱", exact: true }).count(), 0);
    assert.equal(requests.emailGet, 0);
  } finally { await context.close(); }
});

test("candidate and idempotency conflicts offer accurate non-refresh recovery", { timeout: 60_000 }, async () => {
  for (const scenario of [
    { code: "candidate_email_conflict", message: "所选邮箱与现有候选人联系方式冲突", action: "改用手动更正" },
    { code: "idempotency_conflict", message: "本次确认请求与先前操作不一致", action: "重新核对" },
  ]) {
    const { context, page } = await openCandidate({ conflictOnce: true, conflictCode: scenario.code });
    try {
      await page.getByRole("button", { name: "查看并确认邮箱", exact: true }).click();
      const dialog = page.getByRole("dialog", { name: "确认候选人邮箱", exact: true });
      await dialog.getByText("native@example.com", { exact: true }).waitFor();
      await dialog.getByLabel(/我已与候选人核对/).check();
      await dialog.getByRole("button", { name: "确认并用于发送" }).click();
      await dialog.getByText(scenario.message, { exact: false }).waitFor();
      assert.equal(await dialog.getByRole("button", { name: "加载最新邮箱", exact: true }).count(), 0);
      await dialog.getByRole("button", { name: scenario.action, exact: true }).click();
      assert.equal(await dialog.getByText(scenario.message, { exact: false }).count(), 0);
      if (scenario.code === "candidate_email_conflict") await dialog.getByLabel("手动更正邮箱", { exact: true }).waitFor();
    } finally { await context.close(); }
  }
});

test("candidate dialog keeps focus contained while confirmation is saving and restores it after close", { timeout: 60_000 }, async () => {
  const { context, page, requests } = await openCandidate({ holdPut: true });
  try {
    const trigger = page.getByRole("button", { name: "查看并确认邮箱", exact: true });
    await trigger.click();
    const dialog = page.getByRole("dialog", { name: "确认候选人邮箱", exact: true });
    await dialog.getByText("native@example.com", { exact: true }).waitFor();
    await dialog.getByLabel(/我已与候选人核对/).check();
    await dialog.getByRole("button", { name: "确认并用于发送" }).click();
    await dialog.getByRole("button", { name: "确认中…", exact: true }).waitFor();
    assert.equal(await dialog.evaluate((element) => element.contains(document.activeElement)), true);
    requests.releasePut();
    await dialog.waitFor({ state: "hidden" });
    assert.equal(await trigger.evaluate((element) => element === document.activeElement), true);
  } finally { requests.releasePut(); await context.close(); }
});

test("candidate email choices remain contained at 390px", { timeout: 60_000 }, async () => {
  const { context, page } = await openCandidate({ viewport: { width: 390, height: 844 } });
  try {
    await page.getByRole("button", { name: "查看并确认邮箱", exact: true }).click();
    await page.getByRole("dialog", { name: "确认候选人邮箱", exact: true }).getByText("ocr@example.com", { exact: true }).waitFor();
    const widths = await page.evaluate(() => ({ client: document.documentElement.clientWidth, scroll: document.documentElement.scrollWidth }));
    assert.equal(widths.scroll, widths.client, JSON.stringify(widths));
  } finally { await context.close(); }
});

test("candidate data management is on-demand for recruiting admins and absent for recruiters", { timeout: 60_000 }, async () => {
  const admin = await openCandidate({ role: "recruiting_admin" });
  try {
    assert.equal(admin.requests.governanceGet, 0);
    const trigger = admin.page.getByRole("button", { name: "更多操作", exact: true });
    await trigger.click();
    await admin.page.getByRole("menuitem", { name: "数据管理", exact: true }).click();
    const dialog = admin.page.getByRole("dialog", { name: "候选人数据管理", exact: true });
    await dialog.getByText("数据治理状态", { exact: true }).waitFor();
    await dialog.getByText("无删除请求", { exact: true }).waitFor();
    assert.ok(admin.requests.governanceGet >= 1);
    const closeButtons = dialog.getByRole("button", { name: "关闭", exact: true });
    await closeButtons.last().focus();
    await admin.page.keyboard.press("Tab");
    assert.equal(await closeButtons.first().evaluate((element) => element === document.activeElement), true);
    await closeButtons.first().focus();
    await admin.page.keyboard.press("Shift+Tab");
    assert.equal(await closeButtons.last().evaluate((element) => element === document.activeElement), true);
    await closeButtons.last().click();
    await dialog.waitFor({ state: "hidden" });
    assert.equal(await trigger.evaluate((element) => element === document.activeElement), true);
  } finally { await admin.context.close(); }

  const recruiter = await openCandidate({ role: "recruiter" });
  try {
    assert.equal(await recruiter.page.getByRole("button", { name: "更多操作", exact: true }).count(), 0);
    assert.equal(recruiter.requests.governanceGet, 0);
  } finally { await recruiter.context.close(); }
});
