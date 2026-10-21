import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import test from "node:test";

const stateModule = await import("./jobWorkspaceState.js").catch(() => ({}));

const {
  appendJobPage,
  commitJobMutation,
  createInitialJobWorkspaceState,
  failJobRequest,
  getJobDefinitionErrors,
  getJobPublicationChecks,
  retryJobRefresh,
  startJobRequest,
  succeedJobMutationRefresh,
  succeedJobRequest,
} = stateModule;

const firstPage = {
  records: [{ id: "job-1", name: "平台工程师", version: 1 }],
  nextCursor: "next-1",
  departments: [{ id: "department-1", name: "技术部" }],
  owners: [{ id: "owner-1", name: "招聘经理" }],
  statusCounts: { 草稿: 1, 招聘中: 2, 已暂停: 0, 已关闭: 0, 已归档: 0 },
};

test("publication checks use the submit requirements, not optional department and rules", () => {
  const values = { name: "工程师", jd: "开发工作", process: "标准流程", headcount: 1, hiringManagerIds: ["m1"], hiringOwnerId: "m1" };
  assert.ok(getJobPublicationChecks(values).every((item) => item.complete));
  for (const changes of [{ name: "  " }, { jd: "\n " }, { process: " " }, { hiringManagerIds: [] }, { hiringOwnerId: "m2" }, { headcount: 0 }, { headcount: 1.5 }, { headcount: 1001 }]) {
    const invalid = { ...values, ...changes };
    const checks = getJobPublicationChecks(invalid);
    for (const field of Object.keys(getJobDefinitionErrors(invalid))) {
      assert.equal(checks.find((item) => item.field === field).complete, false, field);
    }
    assert.ok(checks.some((item) => !item.complete));
  }
  const unavailable = getJobPublicationChecks(values, { workflowUnavailable: true });
  assert.equal(unavailable.find((item) => item.field === "process").complete, false);
});

test("publication checks reject server text and rule bounds without requiring optional content", () => {
  const values = { name: "工程师", jd: "描述", process: "流程", headcount: 1, hiringManagerIds: ["m1"], hiringOwnerId: "m1" };
  for (const [field, limit] of [["name", 200], ["jd", 50000], ["location", 200], ["process", 100], ["mustHave", 100], ["niceToHave", 100]]) {
    assert.deepEqual(getJobDefinitionErrors({ ...values, [field]: "字".repeat(limit) }), {});
    const invalid = { ...values, [field]: "字".repeat(limit + 1) };
    assert.ok(getJobDefinitionErrors(invalid)[field], field);
    assert.ok(getJobPublicationChecks(invalid).some((check) => !check.complete), field);
  }
  for (const field of ["mustHave", "niceToHave"]) {
    assert.deepEqual(getJobDefinitionErrors({ ...values, [field]: Array(50).fill("开发").join("、") }), {});
    assert.ok(getJobDefinitionErrors({ ...values, [field]: Array(51).fill("开发").join("，") })[field]);
  }
  assert.deepEqual(getJobDefinitionErrors({ ...values, name: "𠮷".repeat(200), location: "  ", mustHave: "、 ,，", niceToHave: [] }), {});
});

test("stale success and failure responses leave the active request unchanged", () => {
  assert.equal(typeof createInitialJobWorkspaceState, "function");
  const loading = startJobRequest(createInitialJobWorkspaceState(), 2, { q: "平台" });

  const staleSuccess = succeedJobRequest(loading, 1, firstPage);
  const staleFailure = failJobRequest(loading, 1, new Error("旧请求失败"));

  assert.equal(staleSuccess, loading);
  assert.equal(staleFailure, loading);
});

test("append page preserves existing records and deduplicates by job id", () => {
  const ready = succeedJobRequest(
    startJobRequest(createInitialJobWorkspaceState(), 1, {}),
    1,
    firstPage,
  );
  const loadingNextPage = startJobRequest(ready, 2, ready.filters);

  const appended = appendJobPage(loadingNextPage, 2, {
    ...firstPage,
    records: [
      { id: "job-1", name: "重复职位", version: 2 },
      { id: "job-2", name: "前端工程师", version: 1 },
    ],
    nextCursor: null,
  });

  assert.deepEqual(appended.records, [firstPage.records[0], { id: "job-2", name: "前端工程师", version: 1 }]);
  assert.equal(appended.nextCursor, null);
});

test("request failure preserves the previous successful page and metadata", () => {
  const ready = succeedJobRequest(
    startJobRequest(createInitialJobWorkspaceState(), 1, {}),
    1,
    firstPage,
  );
  const loading = startJobRequest(ready, 2, { status: "招聘中" });

  const failed = failJobRequest(loading, 2, new Error("network unavailable"));

  assert.equal(failed.status, "error");
  assert.equal(failed.error, "network unavailable");
  assert.deepEqual(failed.records, firstPage.records);
  assert.deepEqual(failed.departments, firstPage.departments);
  assert.deepEqual(failed.owners, firstPage.owners);
  assert.deepEqual(failed.statusCounts, firstPage.statusCounts);
});

test("mutation refresh replaces records from the current filters instead of unconditionally upserting", () => {
  const ready = succeedJobRequest(
    startJobRequest(createInitialJobWorkspaceState(), 1, {}),
    1,
    firstPage,
  );
  const refreshing = startJobRequest(ready, 2, { status: "招聘中" });
  const filteredPage = {
    ...firstPage,
    records: [{ id: "job-2", name: "仍在招聘", version: 1 }],
    statusCounts: { 草稿: 2, 招聘中: 1, 已暂停: 0, 已关闭: 0, 已归档: 0 },
  };

  const refreshed = succeedJobMutationRefresh(refreshing, 2, filteredPage);
  const stale = succeedJobMutationRefresh(refreshing, 1, filteredPage);

  assert.deepEqual(refreshed.records, filteredPage.records);
  assert.equal(refreshed.records.some((record) => record.id === "job-1"), false);
  assert.deepEqual(refreshed.statusCounts, filteredPage.statusCounts);
  assert.equal(stale, refreshing);
});

test("draft and publish validation require content, selected managers, and a default reviewer", () => {
  assert.deepEqual(getJobDefinitionErrors({ name: "", jd: "", process: "" }), {
    name: "请输入职位名称",
    jd: "请输入公开职位描述",
    process: "请输入招聘流程模板",
    hiringManagerIds: "请至少选择一位用人经理",
    hiringOwnerId: "请选择默认评审人",
  });
  assert.deepEqual(getJobDefinitionErrors({ name: "平台工程师", jd: "建设平台", process: "标准流程", hiringManagerIds: ["manager-1"], hiringOwnerId: "manager-1" }), {});
  assert.equal(getJobDefinitionErrors({ name: "平台工程师", jd: "建设平台", process: "标准流程", hiringManagerIds: ["manager-1"], hiringOwnerId: "manager-2" }).hiringOwnerId, "默认评审人必须来自已选用人经理");
});

test("job form presents accessible multi-reviewer selection and blocks save without a default", async () => {
  const source = await readFile(new URL("./JobViews.jsx", import.meta.url), "utf8");

  assert.match(source, /<fieldset className="job-reviewer-field" aria-describedby="job-reviewer-help">/);
  assert.match(source, /type="checkbox" checked=\{values\.hiringManagerIds\.includes\(item\.id\)\}/);
  assert.match(source, /hiringManagers\.filter\(\(item\) => values\.hiringManagerIds\.includes\(item\.id\)\)/);
  assert.match(source, /disabled=\{saving \|\| workflowUnavailable \|\| reviewerSelectionInvalid\}/);
  assert.match(source, /AI 自动流转只通知默认评审人；其他负责人可查看，但不会自动收到每份简历通知/);
});

test("exit dialog closes before draft save so a failed save error remains visible", async () => {
  const source = await readFile(new URL("./JobViews.jsx", import.meta.url), "utf8");

  assert.match(source, /onSave=\{\(\) => \{\s*setConfirmExit\(false\);\s*void submit\(false\);\s*\}\}/);
  assert.match(source, /ref=\{submitErrorRef\}[^>]*tabIndex="-1"/);
});

test("create success remains committed when refresh fails and retry performs reads only", async () => {
  let mutationCalls = 0;
  let readCalls = 0;
  const created = { id: "job-created", version: 1, status: "草稿", name: "平台工程师" };

  const committed = await commitJobMutation(
    async () => { mutationCalls += 1; return created; },
    async () => { readCalls += 1; throw new Error("read failed"); },
  );
  const retried = await retryJobRefresh(committed.record, async () => {
    readCalls += 1;
    return { ...created, department: "技术部", funnel: { new: 0 } };
  });

  assert.equal(mutationCalls, 1);
  assert.equal(readCalls, 2);
  assert.equal(committed.record, created);
  assert.match(committed.refreshError, /最新数据加载失败/);
  assert.deepEqual(retried, { record: { ...created, department: "技术部", funnel: { new: 0 } }, refreshError: "" });
});

test("transition success keeps returned status and retry never repeats the transition", async () => {
  let transitionCalls = 0;
  let readCalls = 0;
  const transitioned = { id: "job-1", version: 9, status: "已关闭" };

  const committed = await commitJobMutation(
    async () => { transitionCalls += 1; return transitioned; },
    async () => { readCalls += 1; throw new Error("definition unavailable"); },
  );
  await retryJobRefresh(committed.record, async (record) => {
    readCalls += 1;
    return { ...record, name: "平台工程师", funnel: { review: 2 } };
  });

  assert.equal(transitionCalls, 1);
  assert.equal(readCalls, 2);
  assert.equal(committed.record.status, "已关闭");
  assert.equal(committed.record.version, 9);
  assert.ok(committed.refreshError);
});

test("job workspace routes writes through commit helper and refresh retry through read helper", async () => {
  const source = await readFile(new URL("./JobViews.jsx", import.meta.url), "utf8");

  assert.match(source, /commitJobMutation\(/);
  assert.match(source, /retryJobRefresh\(/);
  assert.match(source, /已保存，但最新数据加载失败/);
  assert.match(source, /已更新，但最新数据加载失败/);
});
