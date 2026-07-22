import test from "node:test";
import assert from "node:assert/strict";
import {
  createScreeningController,
  normalizeScreeningTask,
} from "./screeningController.js";

function createClient(responses = []) {
  const calls = [];
  return {
    calls,
    async request(path, options = {}) {
      calls.push({ path, options });
      const response = responses.shift();
      return typeof response === "function" ? response(path, options) : response;
    },
  };
}

function run(overrides = {}) {
  return {
    id: "run-1",
    job_id: "job-1",
    source: "upload",
    status: "queued",
    total_count: 3,
    processed_count: 0,
    ...overrides,
  };
}

function item(overrides = {}) {
  return {
    id: "item-1",
    filename: "resume.pdf",
    status: "queued",
    llm_status: "not_requested",
    rule_result: null,
    llm_evaluation: null,
    ...overrides,
  };
}

function deferred() {
  let resolve;
  const promise = new Promise((resolvePromise) => {
    resolve = resolvePromise;
  });
  return { promise, resolve };
}

test("lists job options from GET /jobs with only id and title", async () => {
  const client = createClient([{ data: [{ id: "job-1", title: "AI 工程师", status: "open" }] }]);
  const controller = createScreeningController({ client });

  assert.deepEqual(await controller.listJobs(), [{ id: "job-1", title: "AI 工程师" }]);
  assert.equal(client.calls[0].path, "/api/v1/jobs?limit=100");
  assert.deepEqual(client.calls[0].options, {});
});

test("lists every job page using the server cursor and the same abort signal", async () => {
  const signal = new AbortController().signal;
  const client = createClient([
    {
      data: [{ id: "job-1", title: "AI 工程师" }],
      meta: { limit: 100, next_cursor: "opaque cursor/2" },
    },
    {
      data: [{ id: "job-101", title: "数据平台主管" }],
      meta: { limit: 100, next_cursor: null },
    },
  ]);
  const controller = createScreeningController({ client });

  assert.deepEqual(await controller.listJobs({ signal }), [
    { id: "job-1", title: "AI 工程师" },
    { id: "job-101", title: "数据平台主管" },
  ]);
  assert.deepEqual(client.calls, [
    { path: "/api/v1/jobs?limit=100", options: { signal } },
    { path: "/api/v1/jobs?limit=100&cursor=opaque%20cursor%2F2", options: { signal } },
  ]);
});

test("lists screening tasks from the server and preserves job and creator context", async () => {
  const client = createClient([{
    data: [{
      id: "run-1",
      job_id: "job-1",
      job_title: "AI 工程师",
      created_by_name: "张小北",
      source: "upload",
      status: "completed",
      total_count: 5,
      processed_count: 5,
      succeeded_count: 4,
      failed_count: 1,
      manager_review_count: 2,
      deferred_count: 1,
      ai_unavailable_count: 1,
      file_failed_count: 1,
      created_at: "2026-07-17T08:00:00+00:00",
    }],
    meta: { limit: 50, next_cursor: null },
  }]);
  const controller = createScreeningController({ client });

  assert.deepEqual(await controller.listRuns(), [{
    id: "run-1",
    jobId: "job-1",
    position: "AI 工程师",
    creator: "张小北",
    source: "本地上传",
    status: "complete",
    completed: 5,
    total: 5,
    succeeded: 4,
    failed: 1,
    managerReviewCount: 2,
    deferredCount: 1,
    aiUnavailableCount: 1,
    fileFailedCount: 1,
    createdAt: "2026-07-17T08:00:00+00:00",
    serverBacked: true,
  }]);
  assert.equal(client.calls[0].path, "/api/v1/screening-runs?limit=50");
});

test("drops malformed job and item records without creating blank UI rows", async () => {
  const client = createClient([{ data: [null, { id: 7, title: "bad" }, { id: "", title: "blank" }, { id: "job-1", title: "AI 工程师" }] }]);
  const controller = createScreeningController({ client });

  assert.deepEqual(await controller.listJobs(), [{ id: "job-1", title: "AI 工程师" }]);

  const task = normalizeScreeningTask(run(), [null, { id: 7, filename: "bad.pdf" }, item({ id: "item-2", llm_status: "unknown", status: "scored", route_result: "review" })]);
  assert.equal(task.files.length, 1);
  assert.equal(task.files[0].id, "item-2");
  assert.equal(task.files[0].status, "running");
});

test("creates, uploads, and starts with exact contracts and a new key per command", async () => {
  const client = createClient([
    { data: { id: "run-1" } },
    { data: { id: "item-1" } },
    { data: { id: "item-2" } },
    { data: { id: "run-1", status: "parsing" } },
  ]);
  let key = 0;
  const controller = createScreeningController({ client, createIdempotencyKey: () => `key-${++key}` });
  const first = new File(["first"], "first.pdf", { type: "application/pdf" });
  const second = new File(["second"], "second.docx", { type: "application/vnd.openxmlformats-officedocument.wordprocessingml.document" });

  await controller.createRun("job-1");
  await controller.uploadFiles("run-1", [first, second]);
  await controller.startRun("run-1");

  assert.equal(client.calls[0].path, "/api/v1/jobs/job-1/screening-runs");
  assert.deepEqual(client.calls[0].options.body, { source: "upload" });
  assert.equal(client.calls[0].options.idempotencyKey, "key-1");
  assert.equal(client.calls[1].path, "/api/v1/screening-runs/run-1/items");
  assert.equal(client.calls[1].options.body instanceof FormData, true);
  assert.equal(client.calls[1].options.body.get("file"), first);
  assert.equal(client.calls[2].options.body.get("file"), second);
  assert.equal(client.calls[1].options.headers, undefined);
  assert.deepEqual(client.calls.slice(1).map(({ options }) => options.idempotencyKey), ["key-2", "key-3", "key-4"]);
  assert.equal(client.calls[3].path, "/api/v1/screening-runs/run-1/start");
  assert.equal(new Set(client.calls.map(({ options }) => options.idempotencyKey)).size, 4);
});

test("cancels an interrupted empty run with an idempotent command", async () => {
  const client = createClient([{ data: { id: "run-1", status: "cancelled" } }]);
  const controller = createScreeningController({ client, createIdempotencyKey: () => "cancel-key" });

  const run = await controller.cancelRun("run-1");

  assert.equal(run.status, "cancelled");
  assert.deepEqual(client.calls[0], {
    path: "/api/v1/screening-runs/run-1/cancel",
    options: { method: "POST", idempotencyKey: "cancel-key" },
  });
});

test("accepts and preserves a cross-realm-like File object", async () => {
  const client = createClient([{ data: { id: "item-1" } }]);
  const controller = createScreeningController({ client, createIdempotencyKey: () => "upload-key" });
  const file = new File(["resume"], "resume.pdf", { type: "application/pdf" });
  const OriginalFormData = globalThis.FormData;
  let appended;
  Object.defineProperty(file, "name", { value: file.name });
  Object.setPrototypeOf(file, Object.create(Blob.prototype, {
    [Symbol.toStringTag]: { value: "File" },
  }));
  globalThis.FormData = class RecordingFormData {
    append(name, value) { appended = { name, value }; }
  };

  try {
    assert.equal(file instanceof File, false);
    await controller.uploadFiles("run-1", [file]);
  } finally {
    globalThis.FormData = OriginalFormData;
  }

  assert.equal(appended.name, "file");
  assert.equal(appended.value, file);
});

test("normalizes run states and server counts without timer-derived progress", () => {
  const cases = [
    ["queued", "running"],
    ["llm_scoring", "running"],
    ["completed", "complete"],
    ["partial", "partial"],
    ["failed", "failed"],
    ["cancelled", "cancelled"],
  ];

  for (const [serverStatus, expected] of cases) {
    const task = normalizeScreeningTask(run({ status: serverStatus, processed_count: 2, total_count: 9 }), []);
    assert.equal(task.status, expected);
    assert.equal(task.completed, 2);
    assert.equal(task.total, 9);
    assert.equal(task.id, "run-1");
    assert.equal(task.jobId, "job-1");
  }
});

test("keeps cancelled file items distinct from failures", () => {
  const task = normalizeScreeningTask(run({ status: "cancelled", processed_count: 1, total_count: 1 }), [
    item({ status: "cancelled", error_code: "cancelled" }),
  ]);

  assert.equal(task.status, "cancelled");
  assert.equal(task.files[0].status, "cancelled");
  assert.equal(task.files[0].error, "cancelled");
  assert.equal(task.files[0].llmErrorCode, "");
});

test("preserves parser file errors separately from LLM errors", () => {
  const [file] = normalizeScreeningTask(run({ status: "failed", processed_count: 1, total_count: 1 }), [item({
    status: "failed",
    error_code: "parse_failed",
    llm_status: "failed",
    llm_error_code: "provider_unavailable",
    retryable: true,
  })]).files;

  assert.equal(file.status, "failed");
  assert.equal(file.error, "parse_failed");
  assert.equal(file.llmErrorCode, "provider_unavailable");
  assert.equal(file.retryable, true);
});

test("technical file failures suppress contradictory routing and LLM outcomes", () => {
  const [file] = normalizeScreeningTask(run({ status: "failed", processed_count: 1, total_count: 1 }), [item({
    status: "failed",
    error_code: "parse_failed",
    route_result: "review",
    ai_score: 91,
    ai_recommendation: "强烈推荐",
    llm_status: "failed",
    llm_error_code: "provider_unavailable",
    llm_evaluation: {
      dimensions: [{ key: "core_capability", score: 91, evidence: ["矛盾维度证据"], gaps: ["矛盾维度缺口"] }],
      evidence: ["矛盾综合证据"],
      gaps: ["矛盾综合缺口"],
      strengths: ["矛盾优势"],
      risks: ["矛盾风险"],
    },
    retryable: true,
  })]).files;

  assert.equal(file.status, "failed");
  assert.equal(file.routeResult, null);
  assert.equal(file.routeLabel, "未流转");
  assert.equal(file.score, null);
  assert.equal(file.recommendation, "未进入AI评分");
  assert.equal(file.llmEvaluation, null);
  assert.deepEqual(file.dimensions, []);
  assert.deepEqual(file.evidence, []);
  assert.deepEqual(file.gaps, []);
  assert.deepEqual(file.strengths, []);
  assert.deepEqual(file.risks, []);
  assert.equal(file.error, "parse_failed");
  assert.equal(file.llmErrorCode, "provider_unavailable");
  assert.equal(file.retryable, true);
});

test("polling fetches ordered snapshots immediately, waits between rounds, and stops terminally without overlap", async () => {
  const calls = [];
  const waits = [];
  let round = 0;
  let active = 0;
  let maxActive = 0;
  const client = { async request(path, options) {
    calls.push({ path, options });
    active += 1;
    maxActive = Math.max(maxActive, active);
    await Promise.resolve();
    active -= 1;
    if (path.endsWith("?limit=100")) return { data: [item({ status: round ? "scored" : "queued", rule_result: round ? { score: 81 } : null })] };
    const response = { data: run(round ? { status: "completed", processed_count: 1, total_count: 1 } : { status: "parsing", total_count: 1 }) };
    round += 1;
    return response;
  } };
  const controller = createScreeningController({ client, wait: async (ms) => waits.push(ms) });
  const snapshots = [];

  const result = await controller.pollRun("run-1", { intervalMs: 25, onSnapshot: (snapshot) => snapshots.push(snapshot) });

  assert.deepEqual(calls.map(({ path }) => path), [
    "/api/v1/screening-runs/run-1",
    "/api/v1/screening-runs/run-1/items?limit=100",
    "/api/v1/screening-runs/run-1",
    "/api/v1/screening-runs/run-1/items?limit=100",
  ]);
  assert.deepEqual(waits, [25]);
  assert.deepEqual(snapshots.map(({ status }) => status), ["running", "complete"]);
  assert.equal(result.status, "complete");
  assert.equal(maxActive, 1);
});

test("polling reads terminal items after the terminal run and emits that final item state", async () => {
  const terminalRun = deferred();
  const events = [];
  const client = { request(path) {
    if (path.endsWith("?limit=100")) {
      events.push("items-requested");
      return Promise.resolve({ data: [item({ status: "scored", route_result: "review", ai_score: 92 })] });
    }
    events.push("run-requested");
    return terminalRun.promise.then((response) => {
      events.push("run-resolved");
      return response;
    });
  } };
  const controller = createScreeningController({ client });
  const snapshots = [];

  const polling = controller.pollRun("run-1", { onSnapshot: (snapshot) => snapshots.push(snapshot) });
  await Promise.resolve();
  assert.deepEqual(events, ["run-requested"]);

  terminalRun.resolve({ data: run({ status: "completed", processed_count: 1, total_count: 1 }) });
  const result = await polling;

  assert.deepEqual(events, ["run-requested", "run-resolved", "items-requested"]);
  assert.equal(result.status, "complete");
  assert.equal(result.files[0].status, "success");
  assert.deepEqual(snapshots, [result]);
});

test("polling aborts quietly without a stale partial-pair emission", async () => {
  const abortController = new AbortController();
  const client = { async request(path) {
    if (path.endsWith("?limit=100")) {
      abortController.abort();
      throw new DOMException("Aborted", "AbortError");
    }
    return { data: run({ status: "parsing" }) };
  } };
  const controller = createScreeningController({ client });
  const snapshots = [];

  const result = await controller.pollRun("run-1", { signal: abortController.signal, onSnapshot: (snapshot) => snapshots.push(snapshot) });

  assert.equal(result, null);
  assert.deepEqual(snapshots, []);
});

test("a newer poll for the same run suppresses all later emissions from the older poll and stops it", async () => {
  const responses = Array.from({ length: 3 }, deferred);
  let requestIndex = 0;
  const client = { request: () => responses[requestIndex++].promise };
  const controller = createScreeningController({ client });
  const olderSnapshots = [];
  const newerSnapshots = [];

  const olderPoll = controller.pollRun("run-1", { onSnapshot: (snapshot) => olderSnapshots.push(snapshot) });
  await Promise.resolve();
  const newerPoll = controller.pollRun("run-1", { onSnapshot: (snapshot) => newerSnapshots.push(snapshot) });

  responses[1].resolve({ data: run({ status: "completed", processed_count: 1, total_count: 1 }) });
  await Promise.resolve();
  responses[2].resolve({ data: [item({ status: "scored", rule_result: { score: 90 } })] });
  assert.equal((await newerPoll).status, "complete");

  responses[0].resolve({ data: run({ status: "completed", processed_count: 1, total_count: 1 }) });

  assert.equal(await olderPoll, null);
  assert.deepEqual(olderSnapshots, []);
  assert.equal(newerSnapshots.length, 1);
  assert.equal(requestIndex, 3);
});

test("polls for different runs remain independent", async () => {
  const snapshots = { first: [], second: [] };
  const client = { async request(path) {
    if (path.endsWith("?limit=100")) return { data: [item()] };
    const runId = path.split("/").at(-1);
    return { data: run({ id: runId, status: "completed" }) };
  } };
  const controller = createScreeningController({ client });

  const [first, second] = await Promise.all([
    controller.pollRun("run-1", { onSnapshot: (snapshot) => snapshots.first.push(snapshot) }),
    controller.pollRun("run-2", { onSnapshot: (snapshot) => snapshots.second.push(snapshot) }),
  ]);

  assert.equal(first.id, "run-1");
  assert.equal(second.id, "run-2");
  assert.deepEqual(snapshots.first.map(({ id }) => id), ["run-1"]);
  assert.deepEqual(snapshots.second.map(({ id }) => id), ["run-2"]);
});

test("polling forwards non-abort API errors", async () => {
  const expected = new Error("safe API error");
  const controller = createScreeningController({ client: { request: async () => { throw expected; } } });

  await assert.rejects(controller.pollRun("run-1", { onSnapshot() {} }), expected);
});

test("retry uses its endpoint and a fresh idempotency key each time", async () => {
  const client = createClient([{ data: { item: { id: "item-1" } } }, { data: { item: { id: "item-1" } } }]);
  let key = 0;
  const controller = createScreeningController({ client, createIdempotencyKey: () => `retry-${++key}` });

  await controller.retryItem("item-1");
  await controller.retryItem("item-1");

  assert.deepEqual(client.calls.map(({ path }) => path), ["/api/v1/screening-items/item-1/retry", "/api/v1/screening-items/item-1/retry"]);
  assert.deepEqual(client.calls.map(({ options }) => options.idempotencyKey), ["retry-1", "retry-2"]);
});

test("normalizes the four automatic outcome counters directly from the run", () => {
  const task = normalizeScreeningTask(run({
    manager_review_count: 4,
    deferred_count: 3,
    ai_unavailable_count: 2,
    file_failed_count: 1,
    review_total_count: 99,
    review_pending_count: 98,
  }), []);

  assert.equal(task.managerReviewCount, 4);
  assert.equal(task.deferredCount, 3);
  assert.equal(task.aiUnavailableCount, 2);
  assert.equal(task.fileFailedCount, 1);
  assert.equal("reviewTotal" in task, false);
  assert.equal("reviewPending" in task, false);
});

test("normalizes only Task 5 automatic outcome fields and all five LLM dimensions", () => {
  const dimensions = [
    ["core_capability", "核心能力"],
    ["experience_depth", "经验深度"],
    ["role_seniority", "职级匹配"],
    ["transferability", "能力迁移"],
    ["explicit_constraints", "明确约束"],
  ].map(([key], index) => ({
    key,
    score: 60 + index,
    evidence: [`evidence-${index + 1}`],
    gaps: [`gap-${index + 1}`],
  }));
  const expectedDimensions = dimensions.map((dimension, index) => ({
    ...dimension,
    label: ["核心能力", "经验深度", "职级匹配", "能力迁移", "明确约束"][index],
  }));
  const [result] = normalizeScreeningTask(run(), [item({
    status: "scored",
    route_result: "review",
    ai_score: 72,
    ai_recommendation: "建议评审",
    llm_status: "succeeded",
    llm_error_code: null,
    llm_evaluation: {
      dimensions,
      evidence: ["多年平台经验"],
      gaps: ["行业经验待确认"],
      strengths: ["系统设计"],
      risks: ["到岗时间"],
    },
    rule_result: { score: 12, recommendation: "淘汰", required_hits: ["legacy"] },
  })]).files;

  assert.equal(result.routeResult, "review");
  assert.equal(result.routeLabel, "已转交用人经理");
  assert.equal(result.score, 72);
  assert.equal(result.recommendation, "建议评审");
  assert.deepEqual(result.dimensions, expectedDimensions);
  assert.deepEqual(result.evidence, ["多年平台经验"]);
  assert.deepEqual(result.gaps, ["行业经验待确认"]);
  assert.deepEqual(result.strengths, ["系统设计"]);
  assert.deepEqual(result.risks, ["到岗时间"]);
  assert.equal("ruleScore" in result, false);
  assert.equal("matched" in result, false);
  assert.equal("humanReviewed" in result, false);
});

test("maps review and deferred routes to automatic outcome labels", () => {
  const files = normalizeScreeningTask(run(), [
    item({ id: "review", route_result: "review" }),
    item({ id: "deferred", route_result: "deferred" }),
  ]).files;

  assert.deepEqual(files.map(({ routeLabel }) => routeLabel), ["已转交用人经理", "已暂缓"]);
});

test("screening items preserve the exact application identity for duplicate applications", () => {
  const [file] = normalizeScreeningTask(run(), [item({
    candidate_id: "candidate-1",
    application_id: "application-repeat-2",
    route_result: "review",
  })]).files;

  assert.equal(file.candidateId, "candidate-1");
  assert.equal(file.applicationId, "application-repeat-2");
});

test("safely degrades malformed LLM evaluation values", () => {
  const [result] = normalizeScreeningTask(run(), [item({
    route_result: "review",
    ai_score: "72",
    ai_recommendation: { unsafe: true },
    llm_status: "succeeded",
    llm_error_code: { unsafe: true },
    llm_evaluation: {
      dimensions: [
        { key: "core_capability", score: 80, evidence: ["proof", 7], gaps: "none" },
        null,
        "bad",
        { key: { unsafe: true }, score: "90", evidence: null, gaps: [{ unsafe: true }] },
        { key: "explicit_constraints", score: Number.NaN, evidence: [], gaps: [] },
        { key: "transferability", score: 100 },
      ],
      evidence: "bad",
      gaps: [null],
      strengths: ["safe", 9],
      risks: { unsafe: true },
    },
  })]).files;

  assert.equal(result.score, null);
  assert.equal(result.recommendation, "");
  assert.equal(result.error, "");
  assert.equal(result.llmErrorCode, "");
  assert.deepEqual(result.dimensions, [
    { key: "core_capability", label: "核心能力", score: 80, evidence: ["proof"], gaps: [] },
    { key: "", label: "", score: null, evidence: [], gaps: [] },
    { key: "explicit_constraints", label: "明确约束", score: null, evidence: [], gaps: [] },
  ]);
  assert.deepEqual(result.evidence, []);
  assert.deepEqual(result.gaps, []);
  assert.deepEqual(result.strengths, ["safe"]);
  assert.deepEqual(result.risks, []);
});

test("final LLM failure has no score or fabricated evaluation and keeps manager handoff", () => {
  const [result] = normalizeScreeningTask(run(), [item({
    status: "scored",
    route_result: "review",
    ai_score: 91,
    ai_recommendation: "推荐",
    llm_status: "failed",
    llm_error_code: "provider_unavailable",
    llm_evaluation: { dimensions: [{ key: "core_capability", score: 91 }], strengths: ["legacy"] },
  })]).files;

  assert.equal(result.score, null);
  assert.equal(result.recommendation, "AI评分不可用");
  assert.equal(result.routeLabel, "已转交用人经理");
  assert.equal(result.llmEvaluation, null);
  assert.deepEqual(result.dimensions, []);
  assert.deepEqual(result.strengths, []);
  assert.equal(result.error, "");
  assert.equal(result.llmErrorCode, "provider_unavailable");
});

test("controller exposes retryItem but no bulk or undo API", () => {
  const controller = createScreeningController({ client: createClient() });

  assert.equal(typeof controller.retryItem, "function");
  assert.equal("bulkAction" in controller, false);
  assert.equal("undoBulkAction" in controller, false);
});
