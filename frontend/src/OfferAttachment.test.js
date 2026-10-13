import test, { before, after } from "node:test";
import assert from "node:assert/strict";
import { mkdir } from "node:fs/promises";
import { fileURLToPath } from "node:url";
import { chromium } from "playwright";
import { createServer } from "vite";

let vite;
let browser;
const screenshotDir = fileURLToPath(new URL("../.tmp/offer-attachment/", import.meta.url));
before(async () => {
  await mkdir(screenshotDir, { recursive: true });
  vite = await createServer({ root: fileURLToPath(new URL("../", import.meta.url)), logLevel: "silent", server: { host: "127.0.0.1", port: 0 } });
  await vite.listen();
  browser = await chromium.launch({ headless: true });
});
after(async () => { await browser?.close(); await vite?.close(); });

async function mount(page) {
  const html = await vite.transformIndexHtml("/", '<html><body style="margin:0"><div id="root" style="max-width:900px;margin:auto;padding:16px;box-sizing:border-box"></div></body></html>');
  await page.route("**/offer-attachment-test", (route) => route.fulfill({ contentType: "text/html", body: html }));
  await page.goto(`${vite.resolvedUrls.local[0]}offer-attachment-test`);
  await page.evaluate(async () => {
    const { default: React } = await import("/node_modules/.vite/deps/react.js");
    const { default: ReactDOM } = await import("/node_modules/.vite/deps/react-dom_client.js");
    const { CandidateOfferView } = await import("/src/OfferViews.jsx");
    const { createOfferController } = await import("/src/offerController.js");
    await import("/src/styles.css");
    await import("/src/product-theme.css");
    window.calls = [];
    window.stored = { id: "test-offer", version: 4, status: "ready_to_send", candidate_name: "测试候选人", job_title: "测试职位", candidate_response_deadline: "2099-01-01T00:00:00Z", content_ready: true, can_view_sensitive_content: true, content: { title: "测试录用通知", body: "仅供界面自动化验证", compensation: "测试薪酬" }, allowed_actions: { send: true, manage_attachment: true }, attachment: null };
    const client = { async request(path, options = {}) {
      if (path.includes("offer-templates")) return { data: [] };
      if (path.endsWith("/history")) return { data: {} };
      if (options.method) {
        window.calls.push({ path, method: options.method, version: options.ifMatch });
        await new Promise((resolve) => { window.finishRequest = resolve; });
        if (window.failCode) { const code = window.failCode; window.failCode = ""; throw Object.assign(new Error("safe"), { code }); }
        const sending = path.endsWith("/send");
        window.stored = { ...window.stored, version: window.stored.version + 1,
          ...(sending ? { status: "sent", allowed_actions: {} } : { attachment: options.method === "DELETE" ? null : { filename: options.body.get("file").name, size_bytes: options.body.get("file").size, content_type: "application/pdf", sha256: "test-sha" } }) };
      }
      return { data: window.stored };
    } };
    const root = ReactDOM.createRoot(document.getElementById("root"));
    window.renderOffer = (role = "HR") => root.render(React.createElement(CandidateOfferView, { key: Math.random(), offerId: "test-offer", role, controller: createOfferController({ client }) }));
    window.renderOffer();
  });
  await page.getByRole("heading", { name: "正式 Offer 附件 （可选）" }).waitFor();
}
const filename = "测试正式Offer带公章附件-very-long-filename-for-mobile-wrapping-verification.pdf";
async function upload(page, name = filename) {
  await page.getByLabel("上传正式 Offer 附件").setInputFiles({ name, mimeType: "application/pdf", buffer: Buffer.from("%PDF-1.7 test") });
  await page.getByRole("button", { name: "上传中…" }).waitFor();
}
async function finish(page) { await page.evaluate(() => window.finishRequest()); }

for (const width of [1280, 390]) {
  test(`optional attachment upload/replace/remove and current-version send at ${width}px`, async () => {
    const page = await browser.newPage({ viewport: { width, height: 1000 } });
    try {
      await mount(page);
      const send = page.getByRole("button", { name: "确认并发送 Offer" });
      assert.equal(await send.isEnabled(), true);
      await upload(page);
      assert.equal(await send.isDisabled(), true);
      assert.equal(await page.getByLabel("上传正式 Offer 附件").isDisabled(), true);
      await finish(page);
      await page.getByText(filename, { exact: true }).waitFor();
      await page.screenshot({ path: `${screenshotDir}/attachment-${width === 1280 ? "desktop" : "mobile"}.png`, fullPage: true });
      assert.equal(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth), true);
      const bounds = await page.locator(".offer-attachment-file strong").boundingBox();
      assert.ok(bounds.x + bounds.width <= width);
      await page.evaluate(() => window.renderOffer());
      await page.getByText(filename, { exact: true }).waitFor();
      await upload(page, "replacement.pdf");
      await finish(page);
      await page.getByText("replacement.pdf", { exact: true }).waitFor();
      await page.getByRole("button", { name: "移除正式 Offer 附件" }).click();
      await page.getByText("正在移除附件", { exact: true }).waitFor();
      assert.equal(await send.isDisabled(), true);
      await finish(page);
      await page.getByText("未上传附件", { exact: true }).waitFor();
      assert.equal(await send.isEnabled(), true);
      await upload(page);
      await finish(page);
      await page.getByText(filename, { exact: true }).waitFor();
      await send.click();
      await page.getByRole("button", { name: "提交中…" }).waitFor();
      assert.equal(await page.getByRole("button", { name: "替换附件" }).isDisabled(), true);
      assert.deepEqual(await page.evaluate(() => window.calls.map((call) => call.version)), ['"4"', '"5"', '"6"', '"7"', '"8"']);
      await finish(page);
      await page.getByText("Offer 已发送", { exact: true }).waitFor();
      assert.equal(await page.getByRole("button", { name: "替换附件" }).count(), 0);
    } finally { await page.close(); }
  });
}

test("attachment UI validates locally, reports safe server failures and hides unauthorized controls", async () => {
  const page = await browser.newPage();
  try {
    await mount(page);
    await page.getByLabel("上传正式 Offer 附件").setInputFiles({ name: "bad.txt", mimeType: "text/plain", buffer: Buffer.from("bad") });
    await page.getByRole("alert").filter({ hasText: "请选择有效的 PDF 文件" }).waitFor();
    assert.equal(await page.evaluate(() => window.calls.length), 0);
    for (const [code, text] of [
      ["offer_attachment_invalid", "请选择有效的 PDF 文件"],
      ["offer_attachment_invalid_type", "请选择有效的 PDF 文件"],
      ["offer_attachment_invalid_pdf", "PDF 文件无效"],
      ["offer_attachment_file_required", "请选择要上传的 PDF 文件"],
      ["offer_attachment_invalid_upload", "附件上传无效"],
      ["offer_attachment_too_large", "PDF 文件不能超过 10MB"],
      ["offer_attachment_scan_unavailable", "附件安全扫描暂不可用"],
      ["offer_attachment_scanner_unavailable", "附件安全扫描暂不可用"],
      ["offer_attachment_unsafe", "附件未通过安全检查"],
      ["offer_attachment_malware_detected", "附件未通过安全检查"],
      ["offer_attachment_locked", "当前 Offer 不可修改附件"],
      ["resource_version_conflict", "Offer 已被其他成员更新"],
    ]) {
      await page.evaluate((code) => { window.failCode = code; if (code === "resource_version_conflict") window.stored.version = 10; }, code);
      await upload(page);
      await finish(page);
      await page.getByRole("alert").filter({ hasText: text }).waitFor();
      assert.equal(await page.getByRole("button", { name: "确认并发送 Offer" }).isEnabled(), true);
    }
    await page.evaluate(() => { window.stored.attachment = { filename: "private.pdf", size_bytes: 100 }; window.renderOffer("用人经理"); });
    await page.getByText("private.pdf", { exact: true }).waitFor();
    assert.equal(await page.getByRole("button", { name: "替换附件" }).count(), 0);
    await page.evaluate(() => { window.stored.can_view_sensitive_content = false; window.renderOffer("用人经理"); });
    await page.getByText("敏感内容已由服务端隐藏", { exact: true }).waitFor();
    assert.equal(await page.getByText("private.pdf", { exact: true }).count(), 0);
    assert.equal(await page.locator(".offer-attachment").count(), 0);
  } finally { await page.close(); }
});
