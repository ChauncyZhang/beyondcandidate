import assert from "node:assert/strict";
import test from "node:test";
import {
  createScreeningWorkflow,
  getRecentScreeningTaskStorageKey,
  isResumableRecentScreeningTask,
  mergeServerTaskMetadata,
  parseRecentScreeningTask,
  pollServerTask,
  serializeRecentScreeningTask,
} from "./screeningIntegration.js";

const metadata = {
  position: "AI 工程师",
  source: "本地上传",
  note: "校招批次",
  creator: "张小北",
  createdAt: "刚刚",
};

const recoverableTask = {
  id: "run-recoverable",
  jobId: "job-1",
  serverBacked: true,
  status: "running",
  ...metadata,
};

test("recent task storage key is scoped only by the authenticated opaque user id", () => {
  const first = getRecentScreeningTaskStorageKey({
    id: "8f74d33d-0f65-4fb8-a56f-d849260ec66c",
    email: "first@example.test",
    display_name: "第一位用户",
    organization: { id: "org-secret", slug: "private-org" },
  });
  const sameUser = getRecentScreeningTaskStorageKey({
    id: "8f74d33d-0f65-4fb8-a56f-d849260ec66c",
    email: "renamed@example.test",
    display_name: "已改名用户",
    organization: { id: "other-org", slug: "other-private-org" },
  });
  const second = getRecentScreeningTaskStorageKey({ id: "3cb4d60b-3a89-440e-8079-353d3ac45b60" });

  assert.equal(first, "ats_recent_screening_task:user:8f74d33d-0f65-4fb8-a56f-d849260ec66c");
  assert.equal(sameUser, first);
  assert.notEqual(second, first);
  assert.equal(first.includes("first@example.test"), false);
  assert.equal(first.includes("第一位用户"), false);
  assert.equal(first.includes("private-org"), false);
});

test("recent task storage key refuses persistence when stable user id is unavailable", () => {
  assert.equal(getRecentScreeningTaskStorageKey(), null);
  assert.equal(getRecentScreeningTaskStorageKey({}), null);
  assert.equal(getRecentScreeningTaskStorageKey({ id: "   ", email: "fallback@example.test" }), null);
  assert.equal(getRecentScreeningTaskStorageKey({ id: 42, display_name: "数字 ID" }), null);
});

test("recent task persistence keeps only safe server metadata", () => {
  const raw = serializeRecentScreeningTask({
    id: "run-1",
    jobId: "job-1",
    serverBacked: true,
    status: "running",
    ...metadata,
    llmEnabled: true,
    files: [{ id: "item-1", candidate: "敏感姓名" }],
    token: "secret",
  });

  assert.deepEqual(JSON.parse(raw), {
    id: "run-1",
    jobId: "job-1",
    serverBacked: true,
    status: "running",
    ...metadata,
  });
  assert.deepEqual(parseRecentScreeningTask(raw), JSON.parse(raw));
  assert.deepEqual(
    parseRecentScreeningTask(JSON.stringify({ ...JSON.parse(raw), llmEnabled: true })),
    JSON.parse(raw),
  );
  assert.equal(parseRecentScreeningTask(JSON.stringify({ ...JSON.parse(raw), files: [] })), null);
  assert.equal(parseRecentScreeningTask("not-json"), null);
  assert.equal(parseRecentScreeningTask(JSON.stringify({ id: "run-1", serverBacked: true })), null);
});

test("only an active screening run remains resumable from the import dialog", () => {
  assert.equal(isResumableRecentScreeningTask(recoverableTask), true);
  for (const status of ["complete", "partial", "failed", "cancelled", undefined]) {
    const task = { ...recoverableTask, status };
    assert.equal(isResumableRecentScreeningTask(task), false);
    assert.equal(serializeRecentScreeningTask(task), "");
  }
  assert.equal(parseRecentScreeningTask(JSON.stringify({
    id: "legacy-run",
    jobId: "job-1",
    serverBacked: true,
    ...metadata,
  })), null);
});

test("workflow creates once, uploads real files sequentially, tolerates partial failures, and reports progress", async () => {
  const first = new File(["first"], "first.pdf", { type: "application/pdf" });
  const second = new File(["second"], "second.pdf", { type: "application/pdf" });
  const calls = [];
  const progress = [];
  const controller = {
    async createRun(jobId) { calls.push(["create", jobId]); return { id: "run-1" }; },
    async uploadFiles(runId, files) {
      calls.push(["upload", runId, files[0]]);
      if (files[0] === first) throw new Error("upload failed");
      return [{ id: "item-2" }];
    },
    async startRun(runId) { calls.push(["start", runId]); },
    async getRun() { return { id: "run-1", job_id: "job-1", status: "running", processed_count: 0, total_count: 1 }; },
    async getItems() { return [{ id: "item-2", filename: "second.pdf", status: "queued" }]; },
  };

  const workflow = createScreeningWorkflow(controller);
  const result = await workflow.submit({ jobId: "job-1", files: [first, second], metadata, onProgress: (value) => progress.push(value) });

  assert.equal(calls.filter(([type]) => type === "create").length, 1);
  assert.deepEqual(calls.map(([type]) => type), ["create", "upload", "upload", "start"]);
  assert.equal(calls[1][2], first);
  assert.equal(calls[2][2], second);
  assert.deepEqual(progress, [{ completed: 0, total: 2 }, { completed: 1, total: 2 }, { completed: 2, total: 2 }]);
  assert.equal(result.failedCount, 1);
  assert.equal(result.task.id, "run-1");
  assert.equal(result.task.serverBacked, true);
  assert.equal(result.task.files[0].id, "item-2");
});

test("workflow exposes safe recoverable metadata before an in-flight upload completes", async () => {
  const resume = new File(["resume body with phone 13800000000"], "private-name.pdf", { type: "application/pdf" });
  const createdTasks = [];
  let finishUpload;
  let markUploadStarted;
  const uploadStarted = new Promise((resolve) => { markUploadStarted = resolve; });
  const controller = {
    async createRun() { return { id: "run-recoverable" }; },
    uploadFiles() {
      markUploadStarted();
      return new Promise((resolve) => { finishUpload = resolve; });
    },
    async startRun() {},
    async getRun() { return { id: "run-recoverable", job_id: "job-1", status: "running", processed_count: 0, total_count: 1 }; },
    async getItems() { return []; },
  };
  const workflow = createScreeningWorkflow(controller);
  const submission = workflow.submit({
    jobId: "job-1",
    files: [resume],
    metadata: {
      ...metadata,
      filename: resume.name,
      resumeContent: "resume body with phone 13800000000",
      phone: "13800000000",
      email: "candidate@example.com",
      llmEnabled: true,
    },
    onRunCreated: (task) => createdTasks.push(task),
  });

  await uploadStarted;
  assert.deepEqual(createdTasks, [recoverableTask]);
  assert.equal(JSON.stringify(createdTasks).includes(resume.name), false);
  assert.equal(JSON.stringify(createdTasks).includes("13800000000"), false);
  assert.equal(JSON.stringify(createdTasks).includes("candidate@example.com"), false);

  finishUpload([{ id: "item-1" }]);
  await submission;
});

test("workflow does not start when every upload fails", async () => {
  let starts = 0;
  const controller = {
    async createRun() { return { id: "run-2" }; },
    async uploadFiles() { throw new Error("upload failed"); },
    async startRun() { starts += 1; },
  };
  const workflow = createScreeningWorkflow(controller);

  await assert.rejects(
    workflow.submit({ jobId: "job-1", files: [new File(["x"], "x.pdf")], metadata }),
    (error) => error?.code === "ALL_UPLOADS_FAILED",
  );
  assert.equal(starts, 0);
});

test("workflow exposes deterministic in-flight state and rejects duplicate submission", async () => {
  let release;
  const controller = {
    createRun: () => new Promise((resolve) => { release = resolve; }),
  };
  const workflow = createScreeningWorkflow(controller);
  const first = workflow.submit({ jobId: "job-1", files: [new File(["x"], "x.pdf")], metadata });

  assert.equal(workflow.isSubmitting(), true);
  await assert.rejects(
    workflow.submit({ jobId: "job-1", files: [new File(["y"], "y.pdf")], metadata }),
    (error) => error?.code === "SUBMISSION_IN_PROGRESS",
  );
  release({ id: "run-3" });
  await assert.rejects(first);
  assert.equal(workflow.isSubmitting(), false);
});

test("server snapshots retain authoritative fields and merge only current UI metadata", () => {
  const snapshot = { id: "server-run", jobId: "server-job", status: "running", completed: 2, total: 3, files: [{ id: "server-item" }], source: "server" };
  const merged = mergeServerTaskMetadata(snapshot, { ...metadata, llmEnabled: true, id: "fake", jobId: "fake", files: [], completed: 99, status: "complete", unsafe: "no" });

  assert.deepEqual(merged, { ...snapshot, ...metadata, serverBacked: true });
  assert.equal("unsafe" in merged, false);
  assert.equal("llmEnabled" in merged, false);
});

test("abort stops upload workflow quietly and never starts the run", async () => {
  const abortController = new AbortController();
  let starts = 0;
  const controller = {
    async createRun() { return { id: "run-4" }; },
    async uploadFiles() { abortController.abort(); throw new DOMException("Aborted", "AbortError"); },
    async startRun() { starts += 1; },
  };
  const workflow = createScreeningWorkflow(controller);

  const createdTasks = [];

  const result = await workflow.submit({
    jobId: "job-1",
    files: [new File(["secret resume"], "candidate-name.pdf")],
    metadata,
    signal: abortController.signal,
    onRunCreated: (task) => createdTasks.push(task),
  });
  assert.equal(result, null);
  assert.equal(starts, 0);
  assert.deepEqual(createdTasks, [{ ...recoverableTask, id: "run-4" }]);
  assert.equal(JSON.stringify(createdTasks).includes("candidate-name.pdf"), false);
  assert.equal(JSON.stringify(createdTasks).includes("secret resume"), false);
});

test("server polling merges metadata, retries through the controller, and synthetic tasks stay local", async () => {
  const snapshots = [];
  let retries = 0;
  let polls = 0;
  const controller = {
    async pollRun(id, { onSnapshot }) {
      polls += 1;
      onSnapshot({ id, jobId: "job-1", status: "running", completed: 1, total: 2, files: [] });
      return null;
    },
    async retryItem(id) { retries += id === "item-1" ? 1 : 0; },
  };

  const lifecycle = pollServerTask({ task: { id: "run-5", jobId: "job-1", serverBacked: true, ...metadata }, controller, onTaskChange: (task) => snapshots.push(task) });
  await lifecycle.done;
  assert.equal(await lifecycle.retry("item-1"), true);
  assert.equal(polls, 2);
  assert.equal(retries, 1);
  assert.equal(snapshots[0].serverBacked, true);
  assert.equal(snapshots[0].position, metadata.position);

  const synthetic = pollServerTask({ task: { id: "fixture", serverBacked: false }, controller, onTaskChange() {} });
  await synthetic.done;
  await synthetic.retry("item-1");
  assert.equal(polls, 2);
  assert.equal(retries, 1);
});

test("recovery starts a queued run when at least one uploaded file remains", async () => {
  const calls = [];
  const controller = {
    async getRun(id) { calls.push(["getRun", id]); return { id, job_id: "job-1", status: "queued", total_count: 1, processed_count: 0 }; },
    async getItems(id) { calls.push(["getItems", id]); return [{ id: "item-1", filename: "resume.pdf", status: "queued" }]; },
    async startRun(id) { calls.push(["startRun", id]); },
    async pollRun(id) { calls.push(["pollRun", id]); return null; },
    async retryItem() {},
  };

  const lifecycle = pollServerTask({
    task: { id: "run-recover", jobId: "job-1", serverBacked: true, total: 0, files: [], ...metadata },
    controller,
    onTaskChange() {},
  });
  await lifecycle.done;

  assert.deepEqual(calls.map(([name]) => name), ["getRun", "getItems", "startRun", "pollRun"]);
});

test("recovery stops an empty queued run instead of polling forever", async () => {
  const errors = [];
  let polls = 0;
  const controller = {
    async getRun(id) { return { id, job_id: "job-1", status: "queued", total_count: 0, processed_count: 0 }; },
    async getItems() { return []; },
    async pollRun() { polls += 1; },
    async retryItem() {},
  };

  const lifecycle = pollServerTask({
    task: { id: "run-empty", jobId: "job-1", serverBacked: true, total: 0, files: [], ...metadata },
    controller,
    onTaskChange() {},
    onError: (error) => errors.push(error.code),
  });
  await lifecycle.done;

  assert.deepEqual(errors, ["RECOVERED_RUN_EMPTY"]);
  assert.equal(polls, 0);
});
