import test from "node:test";
import assert from "node:assert/strict";

import { createReportController, normalizeReportData } from "./reportController.js";


test("report normalization maps server stages and rates without fixture-derived metrics", () => {
  const report = normalizeReportData({
    funnel: {
      can_export: true,
      total_applications: 4,
      stages: [
        { stage: "new", current_count: 2, average_time_in_stage_seconds: 86400 },
        { stage: "deferred", current_count: 3, average_time_in_stage_seconds: 7200 },
        { stage: "interviewing", current_count: 1, average_time_in_stage_seconds: 129600 },
        { stage: "rejected", current_count: 1, average_time_in_stage_seconds: 3600 },
      ],
      interviews: {
        count: 3,
        required_feedback_completed: 2,
        required_feedback_total: 3,
        required_feedback_completion_rate: 0.666667,
        average_feedback_turnaround_seconds: 5400,
      },
    },
    quality: {
      resume_parsing: { succeeded: 8, total: 10, success_rate: 0.8 },
      rule_screening: { passed: 5, total: 8, pass_rate: 0.625 },
      llm: { succeeded: 3, total: 4, success_rate: 0.75 },
    },
  });

  assert.equal(report.totalApplications, 4);
  assert.equal(report.canExport, true);
  assert.deepEqual(report.stages.map((item) => [item.stage, item.currentCount, item.averageDays]), [
    ["新简历", 2, 1],
    ["AI 初筛暂缓", 3, 0.1],
    ["面试中", 1, 1.5],
    ["已淘汰", 1, 0],
  ]);
  assert.equal(report.interviews.feedbackCompletionRate, 66.7);
  assert.equal(report.interviews.averageFeedbackHours, 1.5);
  assert.equal(report.quality.parseSuccessRate, 80);
  assert.equal(report.quality.rulePassRate, 62.5);
  assert.equal(report.quality.llmSuccessRate, 75);
});


test("report normalization only enables export for an explicit server capability", () => {
  assert.equal(normalizeReportData({ funnel: { can_export: true } }).canExport, true);
  assert.equal(normalizeReportData({ funnel: { can_export: false } }).canExport, false);
  assert.equal(normalizeReportData({ funnel: { can_export: "true" } }).canExport, false);
  assert.equal(normalizeReportData({ funnel: {} }).canExport, false);
});


test("report controller loads both authorized report projections with identical filters", async () => {
  const calls = [];
  const client = { async request(path, options) {
    calls.push({ path, options });
    if (path.startsWith("/api/v1/reports/recruiting-funnel")) return { data: { total_applications: 0, stages: [], interviews: {} } };
    if (path.startsWith("/api/v1/reports/screening-quality")) return { data: {} };
    throw new Error(`unexpected request ${path}`);
  } };
  const signal = new AbortController().signal;
  const controller = createReportController({ client });

  const report = await controller.load({ jobId: "job-1", from: "2026-07-01T00:00:00.000Z", to: "2026-07-31T23:59:59.999Z" }, { signal });

  assert.equal(report.totalApplications, 0);
  assert.equal(calls.length, 2);
  assert.equal(calls[0].path, "/api/v1/reports/recruiting-funnel?job_id=job-1&from=2026-07-01T00%3A00%3A00.000Z&to=2026-07-31T23%3A59%3A59.999Z");
  assert.equal(calls[1].path, "/api/v1/reports/screening-quality?job_id=job-1&from=2026-07-01T00%3A00%3A00.000Z&to=2026-07-31T23%3A59%3A59.999Z");
  assert.equal(calls[0].options.signal, signal);
  assert.equal(calls[1].options.signal, signal);
});


test("export controller creates, checks, and downloads through a one-time ticket", async () => {
  const calls = [];
  const client = {
    async request(path, options = {}) {
      calls.push({ type: "request", path, options });
      if (path === "/api/v1/exports") return { data: { id: "export-1", status: "queued", format: "csv", row_count: 0 } };
      if (path === "/api/v1/exports/export-1") return { data: { id: "export-1", status: "succeeded", format: "csv", row_count: 7 } };
      if (path === "/api/v1/exports/export-1/download-tickets") return { data: { token: "opaque-download-token", expires_in: 60 } };
      throw new Error(`unexpected request ${path}`);
    },
    async download(path, options = {}) {
      calls.push({ type: "download", path, options });
      return { blob: new Blob(["candidate_id\n1\n"], { type: "text/csv" }), filename: "report.csv" };
    },
  };
  const controller = createReportController({ client, idSource: () => "export-idempotency" });

  const signal = new AbortController().signal;
  const created = await controller.createExport({ jobId: "job-1" }, { signal, idempotencyKey: "retained-export-key" });
  const ready = await controller.getExport(created.id);
  const file = await controller.downloadExport(ready.id);

  assert.equal(created.status, "queued");
  assert.equal(ready.rowCount, 7);
  assert.equal(file.filename, "report.csv");
  assert.equal(calls[0].options.idempotencyKey, "retained-export-key");
  assert.equal(calls[0].options.signal, signal);
  assert.deepEqual(calls[0].options.body, { job_id: "job-1", from: null, to: null });
  assert.equal(calls[2].options.method, "POST");
  assert.deepEqual(calls[3].options.body, { token: "opaque-download-token" });
});


test("export controller keeps a generated idempotency key as a safe direct-call default", async () => {
  const calls = [];
  const client = { async request(path, options) {
    calls.push({ path, options });
    return { data: { id: "export-default", status: "queued" } };
  } };
  const controller = createReportController({ client, idSource: () => "generated-export-key" });

  await controller.createExport();

  assert.equal(calls[0].options.idempotencyKey, "generated-export-key");
});


test("export controller supplies safe defaults and polls until the export succeeds", async () => {
  let checks = 0;
  const client = { async request(path) {
    assert.equal(path, "/api/v1/exports/export-2");
    checks += 1;
    if (checks <= 2) return { data: { id: "export-2" } };
    return { data: { id: "export-2", status: "succeeded", format: "csv", row_count: 2 } };
  } };
  const controller = createReportController({ client });
  const delays = [];

  const pending = await controller.getExport("export-2");
  assert.equal(pending.status, "queued");
  assert.equal(pending.format, "csv");

  const ready = await controller.waitForExport("export-2", {
    delay: async (milliseconds) => delays.push(milliseconds),
    intervalMs: 25,
    maxAttempts: 3,
  });

  assert.equal(checks, 3);
  assert.deepEqual(delays, [25]);
  assert.equal(ready.status, "succeeded");
  assert.equal(ready.format, "csv");
});


test("export polling passes the signal into each delay", async () => {
  const client = { async request() {
    return { data: { id: "export-signal", status: "processing" } };
  } };
  const controller = createReportController({ client });
  const abortController = new AbortController();
  const signals = [];

  await assert.rejects(
    controller.waitForExport("export-signal", {
      signal: abortController.signal,
      delay: async (_milliseconds, { signal } = {}) => {
        signals.push(signal);
        abortController.abort();
      },
      maxAttempts: 2,
    }),
    { name: "AbortError" },
  );
  assert.deepEqual(signals, [abortController.signal]);
});


test("default export polling delay rejects promptly when aborted", async () => {
  const client = { async request() {
    return { data: { id: "export-abort", status: "processing" } };
  } };
  const controller = createReportController({ client });
  const abortController = new AbortController();
  const pending = controller.waitForExport("export-abort", {
    signal: abortController.signal,
    intervalMs: 1000,
    maxAttempts: 2,
  });
  setTimeout(() => abortController.abort(), 5);

  const result = await Promise.race([
    pending.then(() => "resolved", (error) => error?.name),
    new Promise((resolve) => setTimeout(() => resolve("too-slow"), 100)),
  ]);

  assert.equal(result, "AbortError");
});


test("report workspace operation support suppresses stale work and retains ambiguous export keys", async () => {
  const support = await import("./reportWorkspaceState.js").catch(() => ({}));
  assert.equal(typeof support.createLatestOperation, "function");
  assert.equal(typeof support.createExportIntent, "function");

  const operations = support.createLatestOperation();
  const first = operations.start();
  const second = operations.start();
  assert.equal(first.signal.aborted, true);
  assert.equal(first.isCurrent(), false);
  assert.equal(second.isCurrent(), true);
  operations.cancel();
  assert.equal(second.signal.aborted, true);
  assert.equal(second.isCurrent(), false);

  const keys = ["intent-1", "intent-2"];
  const intent = support.createExportIntent(() => keys.shift());
  assert.equal(intent.key(), "intent-1");
  assert.equal(intent.key(), "intent-1");
  intent.succeed();
  assert.equal(intent.key(), "intent-2");
  intent.reset();
  assert.equal(intent.peek(), null);
});


test("a deferred report response cannot become current after a newer query starts", async () => {
  const support = await import("./reportWorkspaceState.js");
  const operations = support.createLatestOperation();
  const accepted = [];
  let resolveFirst;
  const firstResponse = new Promise((resolve) => { resolveFirst = resolve; });
  const first = operations.start();
  const firstCompletion = firstResponse.then((value) => {
    if (first.isCurrent()) accepted.push(value);
  });

  const second = operations.start();
  if (second.isCurrent()) accepted.push("scope-b");
  resolveFirst("scope-a");
  await firstCompletion;

  assert.deepEqual(accepted, ["scope-b"]);
});


test("an ambiguous export creation retry reuses the same explicit key", async () => {
  const support = await import("./reportWorkspaceState.js");
  const sentKeys = [];
  let attempt = 0;
  const client = { async request(_path, options) {
    sentKeys.push(options.idempotencyKey);
    attempt += 1;
    if (attempt === 1) throw new Error("connection lost after commit");
    return { data: { id: "export-replayed", status: "queued" } };
  } };
  const controller = createReportController({ client });
  const intent = support.createExportIntent(() => "stable-intent-key");

  await assert.rejects(controller.createExport({}, { idempotencyKey: intent.key() }));
  const replayed = await controller.createExport({}, { idempotencyKey: intent.key() });
  intent.succeed();

  assert.equal(replayed.id, "export-replayed");
  assert.deepEqual(sentKeys, ["stable-intent-key", "stable-intent-key"]);
  assert.equal(intent.peek(), null);
});


test("export polling is sequential and preserves failed and timeout terminals", async () => {
  let activeRequests = 0;
  let maxActiveRequests = 0;
  let requests = 0;
  const client = { async request() {
    activeRequests += 1;
    maxActiveRequests = Math.max(maxActiveRequests, activeRequests);
    requests += 1;
    await Promise.resolve();
    activeRequests -= 1;
    return { data: { id: "export-terminal", status: requests === 2 ? "failed" : "processing" } };
  } };
  const controller = createReportController({ client });

  const failed = await controller.waitForExport("export-terminal", { delay: async () => {}, maxAttempts: 3 });
  assert.equal(failed.status, "failed");
  assert.equal(maxActiveRequests, 1);

  const pendingClient = { async request() {
    return { data: { id: "export-timeout", status: "processing" } };
  } };
  const pendingController = createReportController({ client: pendingClient });
  await assert.rejects(
    pendingController.waitForExport("export-timeout", { delay: async () => {}, maxAttempts: 2 }),
    (error) => error?.code === "export_timeout",
  );
});


test("report workspace load states never retain previous-scope metrics", async () => {
  const support = await import("./reportWorkspaceState.js").catch(() => ({}));
  assert.deepEqual(support.loadingReportState?.(), { status: "loading", data: null, error: "" });
  assert.deepEqual(support.failedReportState?.(), { status: "error", data: null, error: "报表加载失败，请检查网络后重试。" });
});


test("only a confirmed terminal export failure starts a fresh export intent", async () => {
  const support = await import("./reportWorkspaceState.js");
  const intent = support.createExportIntent(() => "replacement-key");
  intent.key();

  assert.equal(support.isTerminalExportFailure({ status: "queued" }), false);
  assert.equal(support.isTerminalExportFailure(null), false);
  assert.equal(support.isTerminalExportFailure({ status: "failed" }), true);
  if (support.isTerminalExportFailure({ status: "failed" })) intent.reset();
  assert.equal(intent.peek(), null);
});
