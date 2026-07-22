import test from "node:test";
import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import { ApiError } from "./apiClient.js";
import { createOcrSettingsController, getOcrSettingsErrorMessage, getOcrTestDisabledReason } from "./ocrSettings.js";

const config = {
  configured: true,
  enabled: true,
  provider_id: "document-ai",
  base_url: "https://ocr.example.com/v1",
  model: "scan-v2",
  version: 4,
  key_configured: true,
  last_test_status: "succeeded",
  last_test_error_code: null,
  last_test_latency_ms: 210,
  last_tested_at: "2026-07-21T01:00:00Z",
};

function createClient(handler) {
  const calls = [];
  return { calls, async request(path, options = {}) { calls.push({ path, options }); return handler(path, options, calls.length); } };
}

test("loads OCR config without retaining an API key", async () => {
  const client = createClient(() => ({ data: { ...config, api_key: "must-not-survive" } }));
  const controller = createOcrSettingsController({ client });
  await controller.load();

  assert.equal(client.calls[0].path, "/api/v1/settings/ocr");
  assert.deepEqual(controller.getState().draft, {
    provider_id: "document-ai",
    base_url: "https://ocr.example.com/v1",
    model: "scan-v2",
    enabled: true,
  });
  assert.equal("api_key" in controller.getState().config, false);
});

test("saves the complete contract with concurrency and idempotency headers", async () => {
  const client = createClient((path, options) => options.method === "PUT"
    ? { data: { ...config, model: "scan-v3", version: 5 } }
    : { data: config });
  const controller = createOcrSettingsController({ client, createIdempotencyKey: () => "ocr-save-key" });
  await controller.load();
  controller.updateDraft({ model: " scan-v3 " });
  controller.startKeyReplacement();
  controller.setReplacementKey("secret-replacement");
  await controller.save();

  assert.deepEqual(client.calls[1], {
    path: "/api/v1/settings/ocr",
    options: {
      method: "PUT",
      body: { provider_id: "document-ai", base_url: "https://ocr.example.com/v1", model: "scan-v3", enabled: true, api_key: "secret-replacement" },
      ifMatch: '"4"',
      idempotencyKey: "ocr-save-key",
    },
  });
  assert.equal(controller.getState().replacementKey, "");
  assert.equal(controller.getState().dirty, false);
});

test("normalizes a mixed-case Provider identifier before saving", async () => {
  const client = createClient((path, options) => options.method === "PUT"
    ? { data: { ...config, provider_id: "ali", version: 5 } }
    : { data: config });
  const controller = createOcrSettingsController({ client, createIdempotencyKey: () => "ocr-normalize-provider" });
  await controller.load();
  controller.updateDraft({ provider_id: " Ali " });

  await controller.save();

  assert.equal(client.calls[1].options.body.provider_id, "ali");
});

test("retains the saved key by omitting api_key and discard restores all fields", async () => {
  const client = createClient((path, options) => options.method === "PUT" ? { data: { ...config, enabled: false } } : { data: config });
  const controller = createOcrSettingsController({ client, createIdempotencyKey: () => "save-key" });
  await controller.load();
  controller.updateDraft({ base_url: "https://other.example.com", enabled: false });
  controller.discardDraft();
  assert.equal(controller.getState().draft.base_url, config.base_url);
  controller.updateDraft({ enabled: false });
  await controller.save();
  assert.equal("api_key" in client.calls[1].options.body, false);
});

test("tests only saved config, uses a fresh key, and reloads authoritative status", async () => {
  const tested = { ...config, last_test_latency_ms: 98, last_tested_at: "2026-07-21T02:00:00Z" };
  const client = createClient((path, options, count) => {
    if (path.endsWith("/test")) return { data: { status: "succeeded" } };
    return { data: count === 1 ? config : tested };
  });
  const controller = createOcrSettingsController({ client, createIdempotencyKey: () => "ocr-test-key" });
  await controller.load();
  assert.equal(await controller.testConnection(), true);

  assert.deepEqual(client.calls[1], { path: "/api/v1/settings/ocr/test", options: { method: "POST", idempotencyKey: "ocr-test-key" } });
  assert.equal(client.calls[2].path, "/api/v1/settings/ocr");
  assert.equal(controller.getState().config.last_test_latency_ms, 98);
});

test("test and save failures expose only safe mapped messages", async () => {
  const client = createClient((path, options) => {
    if (options.method === "PUT") throw new ApiError({ code: "persistence_failed", detail: "private database detail" });
    return { data: config };
  });
  const controller = createOcrSettingsController({ client, createIdempotencyKey: () => "save-key" });
  await controller.load();
  controller.updateDraft({ enabled: false });
  await controller.save();

  assert.equal(controller.getState().error, getOcrSettingsErrorMessage({ code: "persistence_failed" }));
  assert.equal(controller.getState().error.includes("database"), false);
  assert.equal(
    getOcrSettingsErrorMessage({ code: "provider_auth_failed" }),
    "OCR 服务拒绝了 API Key，请检查 Key、地域和模型访问权限。",
  );
  assert.equal(
    getOcrSettingsErrorMessage({ code: "provider_request_rejected" }),
    "OCR 服务拒绝了测试请求，请检查模型是否支持图片识别。",
  );
});

test("test is blocked for dirty, unconfigured, or keyless saved settings", () => {
  assert.match(getOcrTestDisabledReason({ status: "ready", dirty: true, config }), /先保存/);
  assert.match(getOcrTestDisabledReason({ status: "ready", dirty: false, config: { ...config, configured: false } }), /服务配置/);
  assert.match(getOcrTestDisabledReason({ status: "ready", dirty: false, config: { ...config, key_configured: false } }), /API Key/);
  assert.equal(getOcrTestDisabledReason({ status: "ready", dirty: false, config }), "");
});

test("AI Settings renders flat LLM and OCR sections with combined navigation protection", async () => {
  const [source, styles] = await Promise.all([
    readFile(new URL("./SettingsViews.jsx", import.meta.url), "utf8"),
    readFile(new URL("./product-theme-admin.css", import.meta.url), "utf8"),
  ]);

  assert.match(source, /<h2 id="llm-settings-title">LLM 简历评估<\/h2>/);
  assert.match(source, /<h2 id="ocr-settings-title">OCR 文档识别<\/h2>/);
  assert.match(source, /仅扫描件或低质量 PDF 的页面图像会发送到外部 OCR 服务/);
  assert.match(source, /onDirtyChange\?\.\(llmDirty \|\| ocrDirty\)/);
  assert.match(source, /window\.addEventListener\("beforeunload"/);
  assert.match(source, /招聘管理员仅可查看安全配置状态/);
  assert.match(source, /仅支持小写字母、数字、下划线和连字符/);
  assert.match(styles, /\.ai-settings-block \+ \.ai-settings-block/);
  assert.doesNotMatch(source, /className="settings-section ocr-settings"/);
});
