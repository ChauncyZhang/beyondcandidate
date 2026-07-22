import assert from "node:assert/strict";
import test from "node:test";

import { ApiError } from "./apiClient.js";
import jobController, {
  JOB_EDIT_CONFLICT_REFRESH_ERROR,
  createJobController,
  getJobFormActions,
  getJobSaveSuccessMessage,
} from "./jobController.js";

const JOB_ID = "11111111-1111-4111-8111-111111111111";
const DEPARTMENT_ID = "22222222-2222-4222-8222-222222222222";
const OWNER_ID = "33333333-3333-4333-8333-333333333333";
const HIRING_OWNER_ID = "44444444-4444-4444-8444-444444444444";
const WORKFLOW_TEMPLATE_ID = "55555555-5555-4555-8555-555555555555";

function apiJob(changes = {}) {
  return {
    id: JOB_ID,
    title: "平台工程师",
    department_id: DEPARTMENT_ID,
    department_name: "技术部",
    headcount: 3,
    priority: "high",
    hiring_owner_id: HIRING_OWNER_ID,
    hiring_owner_name: "招聘经理",
    owner_id: OWNER_ID,
    owner_name: "招聘负责人",
    status: "open",
    version: 7,
    updated_at: "2026-07-13T03:05:00Z",
    funnel: {
      stages: { new: 9, review: 4, interview_pending: 2, interviewing: 3, decision: 1 },
      total: 19,
    },
    ...changes,
  };
}

function definitionResource(changes = {}) {
  return {
    data: {
      job: apiJob(changes.job),
      jd: {
        id: "jd-1",
        version_number: 3,
        description: "建设可靠的招聘平台。",
        location: "上海",
        process_template: "技术岗位标准流程",
        llm_enabled: true,
        ...changes.jd,
      },
      rules: {
        id: "rules-1",
        version_number: 4,
        must_have: ["JavaScript", "React"],
        nice_to_have: ["Vite"],
        ...changes.rules,
      },
    },
  };
}

function queuedClient(responses) {
  const calls = [];
  return {
    calls,
    client: {
      async request(path, options = {}) {
        calls.push({ path, options });
        const response = responses.shift();
        if (response instanceof Error) throw response;
        return typeof response === "function" ? response(path, options) : response;
      },
    },
  };
}

test("exports the job controller factory and default controller", () => {
  assert.equal(typeof createJobController, "function");
  assert.equal(typeof jobController.listJobs, "function");
  assert.equal(typeof jobController.listDepartments, "function");
});

test("job form departments come from the organization directory instead of job facets", async () => {
  const calls = [];
  const signal = new AbortController().signal;
  const controller = createJobController({ client: {
    async listDepartments(options) {
      calls.push(options);
      return [
        { id: DEPARTMENT_ID, name: "技术部", job_count: 0 },
      ];
    },
  } });

  assert.deepEqual(await controller.listDepartments({ signal }), [{ id: DEPARTMENT_ID, name: "技术部", status: "active" }]);
  assert.equal(calls[0].signal, signal);
});

test("job form owners come from the dedicated hiring-manager directory", async () => {
  const { client, calls } = queuedClient([{
    data: [
      { id: HIRING_OWNER_ID, name: "招聘经理" },
      { id: "invalid", name: "无效用户" },
    ],
    meta: { count: 1 },
  }]);
  const signal = new AbortController().signal;
  const controller = createJobController({ client });

  const owners = await controller.listHiringManagers({ signal });

  assert.deepEqual(calls, [{ path: "/api/v1/job-owner-options", options: { signal } }]);
  assert.deepEqual(owners, [{ id: HIRING_OWNER_ID, name: "招聘经理" }]);
});

test("job form actions execute the correct publish payload for create and every editable status", async () => {
  const cases = [
    { job: null, expected: { secondary: { label: "保存草稿", publish: false }, primary: { label: "发布职位", publish: true } } },
    { job: { id: JOB_ID, version: 7, status: "草稿" }, expected: { secondary: { label: "保存草稿", publish: false }, primary: { label: "保存并发布", publish: true } } },
    { job: { id: JOB_ID, version: 7, status: "招聘中" }, expected: { secondary: null, primary: { label: "保存修改", publish: false } } },
    { job: { id: JOB_ID, version: 7, status: "已暂停" }, expected: { secondary: null, primary: { label: "保存修改", publish: false } } },
    { job: { id: JOB_ID, version: 7, status: "已关闭" }, expected: { secondary: null, primary: { label: "保存修改", publish: false } } },
  ];
  const actionCount = cases.reduce((count, { expected }) => count + (expected.secondary ? 2 : 1), 0);
  const { client, calls } = queuedClient(Array.from({ length: actionCount }, () => definitionResource()));
  const controller = createJobController({ client, idempotencyKey: () => "job-form-action" });
  const values = { name: "平台工程师", priority: "中" };

  for (const { job, expected } of cases) {
    const actions = getJobFormActions(job);
    assert.deepEqual(actions, expected);
    for (const action of [actions.secondary, actions.primary].filter(Boolean)) {
      await controller.saveDefinition(values, { job, publish: action.publish });
    }
  }

  assert.deepEqual(calls.map(({ options }) => ({
    method: options.method,
    publish: options.body.publish,
    ifMatch: options.ifMatch,
  })), [
    { method: "POST", publish: false, ifMatch: undefined },
    { method: "POST", publish: true, ifMatch: undefined },
    { method: "PUT", publish: false, ifMatch: '"7"' },
    { method: "PUT", publish: true, ifMatch: '"7"' },
    { method: "PUT", publish: false, ifMatch: '"7"' },
    { method: "PUT", publish: false, ifMatch: '"7"' },
    { method: "PUT", publish: false, ifMatch: '"7"' },
  ]);
  assert.equal(getJobSaveSuccessMessage(null, true), "职位已发布");
  assert.equal(getJobSaveSuccessMessage(cases[1].job, true), "职位已发布");
  assert.equal(getJobSaveSuccessMessage(cases[2].job, false), "职位修改已保存");
});

test("listJobs encodes supplied filters and fully normalizes records and facets", async () => {
  const response = {
    data: [apiJob()],
    meta: {
      next_cursor: "next/page",
      departments: [{ id: DEPARTMENT_ID, name: "技术部" }],
      owners: [{ id: HIRING_OWNER_ID, name: "招聘经理" }],
      status_counts: { draft: 2, open: 5, paused: 1, closed: 3, archived: 4 },
    },
  };
  const { client, calls } = queuedClient([response]);
  const signal = new AbortController().signal;
  const controller = createJobController({ client });

  const result = await controller.listJobs({
    q: "  平台 & 架构  ",
    status: "招聘中",
    departmentId: DEPARTMENT_ID,
    ownerId: HIRING_OWNER_ID,
    cursor: "next/page",
    limit: 100,
    ignored: "never-send",
  }, { signal });

  assert.equal(calls[0].path, `/api/v1/jobs?q=%E5%B9%B3%E5%8F%B0+%26+%E6%9E%B6%E6%9E%84&status=open&department_id=${DEPARTMENT_ID}&owner_id=${HIRING_OWNER_ID}&cursor=next%2Fpage&limit=100`);
  assert.deepEqual(calls[0].options, { signal });
  assert.deepEqual(result, {
    records: [{
      id: JOB_ID,
      serverBacked: true,
      version: 7,
      title: "平台工程师",
      name: "平台工程师",
      departmentId: DEPARTMENT_ID,
      department: "技术部",
      recruitingOwnerId: OWNER_ID,
      hiringOwnerId: HIRING_OWNER_ID,
      ownerId: HIRING_OWNER_ID,
      owner: "招聘经理",
      headcount: 3,
      status: "招聘中",
      priority: "高",
      updated: new Intl.DateTimeFormat("zh-CN", { month: "2-digit", day: "2-digit", hour: "2-digit", minute: "2-digit", hour12: false }).format(new Date("2026-07-13T03:05:00Z")),
      updatedAt: "2026-07-13T03:05:00Z",
      funnel: { new: 9, review: 4, interview_pending: 2, interviewing: 3, decision: 1 },
      candidates: 19,
      review: 4,
      interview: 5,
      decision: 1,
    }],
    nextCursor: "next/page",
    departments: [{ id: DEPARTMENT_ID, name: "技术部" }],
    owners: [{ id: HIRING_OWNER_ID, name: "招聘经理" }],
    statusCounts: { 草稿: 2, 招聘中: 5, 已暂停: 1, 已关闭: 3, 已归档: 4 },
  });
});

test("listJobs omits invalid filters and safely defaults malformed optional values", async () => {
  const { client, calls } = queuedClient([{
    data: [apiJob({
      department_id: null,
      department_name: null,
      hiring_owner_id: null,
      hiring_owner_name: null,
      headcount: "3",
      priority: "urgent",
      status: "unknown",
      version: null,
      updated_at: "bad-date",
      funnel: { stages: { review: "4", interviewing: -2 }, total: "19" },
    })],
    meta: {
      next_cursor: 42,
      departments: [{ id: DEPARTMENT_ID, name: "" }, null, { id: DEPARTMENT_ID, name: "技术部" }],
      owners: [{ id: "", name: "无效" }, { id: OWNER_ID, name: "招聘负责人" }],
      status_counts: { open: "5", paused: -1 },
    },
  }]);
  const controller = createJobController({ client });

  const result = await controller.listJobs({
    q: " ", status: "全部状态", departmentId: "not-a-uuid", ownerId: "not-a-uuid", cursor: "", limit: 101,
  });

  assert.equal(calls[0].path, "/api/v1/jobs");
  assert.deepEqual(calls[0].options, {});
  assert.deepEqual(result.departments, [{ id: DEPARTMENT_ID, name: "技术部" }]);
  assert.deepEqual(result.owners, [{ id: OWNER_ID, name: "招聘负责人" }]);
  assert.deepEqual(result.statusCounts, { 草稿: 0, 招聘中: 0, 已暂停: 0, 已关闭: 0, 已归档: 0 });
  assert.equal(result.nextCursor, null);
  assert.deepEqual(result.records[0], {
    id: JOB_ID,
    serverBacked: true,
    version: null,
    title: "平台工程师",
    name: "平台工程师",
    departmentId: "",
    department: "",
    recruitingOwnerId: OWNER_ID,
    hiringOwnerId: "",
    ownerId: OWNER_ID,
    owner: "招聘负责人",
    headcount: 0,
    status: "",
    priority: "",
    updated: "未记录",
    updatedAt: "bad-date",
    funnel: { review: 0, interviewing: 0 },
    candidates: 0,
    review: 0,
    interview: 0,
    decision: 0,
  });
});

test("status and priority mappings ignore prototype-chain keys", async () => {
  const { client, calls } = queuedClient([{
    data: [apiJob({ status: "__proto__", priority: "constructor" })],
    meta: {},
  }]);
  const controller = createJobController({ client });

  const result = await controller.listJobs({ status: "__proto__" });

  assert.equal(calls[0].path, "/api/v1/jobs");
  assert.equal(result.records[0].status, "");
  assert.equal(result.records[0].priority, "");
  assert.equal(typeof result.records[0].status, "string");
  assert.equal(typeof result.records[0].priority, "string");
  await assert.rejects(
    () => controller.saveDefinition({ name: "职位", priority: "constructor" }, { publish: false }),
    { code: "JOB_PRIORITY_UNSUPPORTED" },
  );
  assert.equal(calls.length, 1);
});

test("normalizeJob only trusts valid UUID job identities", async () => {
  const { client } = queuedClient([{
    data: [apiJob({ id: "constructor" })],
    meta: {},
  }]);

  const result = await createJobController({ client }).listJobs();

  assert.equal(result.records[0].id, "");
  assert.equal(result.records[0].serverBacked, false);
});

test("loadDefinition rejects an invalid UUID before starting either request", async () => {
  const { client, calls } = queuedClient([]);

  await assert.rejects(
    () => createJobController({ client }).loadDefinition("../jobs/other"),
    { code: "JOB_ID_INVALID" },
  );
  assert.equal(calls.length, 0);
});

test("existing definition writes and transitions reject invalid UUIDs before network I/O", async () => {
  const { client, calls } = queuedClient([]);
  const controller = createJobController({ client });

  await assert.rejects(
    () => controller.saveDefinition({ name: "职位", priority: "中" }, { job: { id: "__proto__", version: 1 } }),
    { code: "JOB_ID_INVALID" },
  );
  await assert.rejects(
    () => controller.transition({ id: "constructor", version: 1, status: "招聘中" }, "已暂停"),
    { code: "JOB_ID_INVALID" },
  );
  assert.equal(calls.length, 0);
});

test("effective owner selects hiring owner only when its UUID and name are both valid", async () => {
  const { client } = queuedClient([{
    data: [
      apiJob({ hiring_owner_name: "   " }),
      apiJob({ hiring_owner_id: "not-a-uuid", hiring_owner_name: "招聘经理" }),
      apiJob(),
    ],
    meta: {},
  }]);

  const result = await createJobController({ client }).listJobs();

  assert.deepEqual(result.records.map(({ ownerId, owner }) => ({ ownerId, owner })), [
    { ownerId: OWNER_ID, owner: "招聘负责人" },
    { ownerId: OWNER_ID, owner: "招聘负责人" },
    { ownerId: HIRING_OWNER_ID, owner: "招聘经理" },
  ]);
});

test("loadDefinition starts definition and funnel requests concurrently and propagates the signal", async () => {
  const signal = new AbortController().signal;
  const calls = [];
  let resolveDefinition;
  let resolveFunnel;
  const client = {
    request(path, options) {
      calls.push({ path, options });
      return new Promise((resolve) => {
        if (path.includes("job-definitions")) resolveDefinition = resolve;
        else resolveFunnel = resolve;
      });
    },
  };
  const pending = createJobController({ client }).loadDefinition(JOB_ID, { signal });
  await Promise.resolve();

  assert.deepEqual(calls, [
    { path: `/api/v1/job-definitions/${JOB_ID}`, options: { signal } },
    { path: `/api/v1/jobs/${JOB_ID}/funnel`, options: { signal } },
  ]);

  resolveDefinition(definitionResource({ job: { funnel: undefined } }));
  resolveFunnel({ data: { job_id: JOB_ID, stages: { review: 2, interviewing: 1 }, total: 6 } });
  const result = await pending;
  assert.equal(result.jd, "建设可靠的招聘平台。");
  assert.equal(result.location, "上海");
  assert.equal(result.process, "技术岗位标准流程");
  assert.equal(result.llmEnabled, true);
  assert.deepEqual(result.mustHave, ["JavaScript", "React"]);
  assert.deepEqual(result.niceToHave, ["Vite"]);
  assert.equal(result.jdId, "jd-1");
  assert.equal(result.jdVersion, 3);
  assert.equal(result.rulesId, "rules-1");
  assert.equal(result.rulesVersion, 4);
  assert.equal(result.candidates, 6);
  assert.equal(result.review, 2);
  assert.equal(result.interview, 1);
});

test("loadDefinition gives legacy definitions safe blanks without invented version identity", async () => {
  const { client } = queuedClient([
    { data: { job: apiJob({ funnel: undefined }), jd: null, rules: null } },
    { data: { job_id: JOB_ID, stages: null, total: null } },
  ]);

  const result = await createJobController({ client }).loadDefinition(JOB_ID);

  assert.deepEqual({
    jd: result.jd,
    location: result.location,
    process: result.process,
    llmEnabled: result.llmEnabled,
    mustHave: result.mustHave,
    niceToHave: result.niceToHave,
    jdId: result.jdId,
    jdVersion: result.jdVersion,
    rulesId: result.rulesId,
    rulesVersion: result.rulesVersion,
  }, {
    jd: "", location: "", process: "", llmEnabled: false, mustHave: [], niceToHave: [],
    jdId: null, jdVersion: null, rulesId: null, rulesVersion: null,
  });
});

test("saveDefinition maps the complete UI form for draft, publish, and versioned update", async () => {
  const responses = [
    definitionResource({ job: { status: "draft" } }),
    definitionResource({ job: { status: "open" } }),
    definitionResource({ job: { status: "open", version: 8 } }),
  ];
  const { client, calls } = queuedClient(responses);
  const keys = ["create-draft", "create-published", "update-definition"];
  const controller = createJobController({ client, idempotencyKey: () => keys.shift() });
  const signal = new AbortController().signal;
  const values = {
    name: "  平台工程师  ",
    department: "技术部",
    departmentId: DEPARTMENT_ID,
    location: "  上海  ",
    headcount: 3,
    owner: "招聘经理",
    ownerId: HIRING_OWNER_ID,
    priority: "高",
    jd: "  建设可靠的招聘平台。  ",
    mustHave: " JavaScript、React， ",
    niceToHave: [" Vite ", ""],
    process: "  技术岗位标准流程  ",
    workflowTemplateId: WORKFLOW_TEMPLATE_ID,
    llmEnabled: true,
  };

  const draft = await controller.saveDefinition(values, { publish: false, signal });
  const published = await controller.saveDefinition(values, { publish: true, signal });
  const updated = await controller.saveDefinition(values, { job: { id: JOB_ID, version: 7 }, publish: false, signal });

  const baseBody = {
    title: "平台工程师",
    department_id: DEPARTMENT_ID,
    headcount: 3,
    priority: "high",
    hiring_owner_id: HIRING_OWNER_ID,
    description: "建设可靠的招聘平台。",
    location: "上海",
    process_template: "技术岗位标准流程",
    workflow_template_id: WORKFLOW_TEMPLATE_ID,
    llm_enabled: true,
    must_have: ["JavaScript", "React"],
    nice_to_have: ["Vite"],
  };
  assert.deepEqual(calls, [
    { path: "/api/v1/job-definitions", options: { method: "POST", body: { ...baseBody, publish: false }, idempotencyKey: "create-draft", signal } },
    { path: "/api/v1/job-definitions", options: { method: "POST", body: { ...baseBody, publish: true }, idempotencyKey: "create-published", signal } },
    { path: `/api/v1/job-definitions/${JOB_ID}`, options: { method: "PUT", body: { ...baseBody, publish: false }, ifMatch: '"7"', idempotencyKey: "update-definition", signal } },
  ]);
  assert.equal(draft.status, "草稿");
  assert.equal(published.status, "招聘中");
  assert.equal(updated.version, 8);
  assert.equal(updated.jd, "建设可靠的招聘平台。");
});

test("409 recovery preserves form values, refreshes the version baseline, and retries with the new If-Match", async () => {
  const conflict = new ApiError({ status: 409, code: "resource_version_conflict" });
  const { client, calls } = queuedClient([
    conflict,
    definitionResource({ job: { status: "open", version: 8 } }),
    definitionResource({ job: { status: "open", version: 9 } }),
  ]);
  const keys = ["stale-save", "retry-save"];
  const controller = createJobController({ client, idempotencyKey: () => keys.shift() });
  const values = { name: "用户尚未提交的职位名称", priority: "中", location: "远程" };
  const staleJob = { id: JOB_ID, version: 7, status: "招聘中", formMode: "edit" };

  await assert.rejects(
    () => controller.saveDefinition(values, { job: staleJob, publish: false }),
    (error) => error === conflict,
  );
  const recovered = await controller.refreshEditBaseline(staleJob, values);

  assert.equal(recovered.values, values);
  assert.equal(recovered.job.version, 8);
  assert.equal(recovered.job.formMode, "edit");
  assert.equal(recovered.error, "");
  assert.equal(recovered.retryable, false);

  const saved = await controller.saveDefinition(recovered.values, { job: recovered.job, publish: false });

  assert.equal(saved.version, 9);
  assert.deepEqual(calls.map(({ path, options }) => ({ path, method: options.method, ifMatch: options.ifMatch })), [
    { path: `/api/v1/job-definitions/${JOB_ID}`, method: "PUT", ifMatch: '"7"' },
    { path: `/api/v1/job-definitions/${JOB_ID}`, method: undefined, ifMatch: undefined },
    { path: `/api/v1/job-definitions/${JOB_ID}`, method: "PUT", ifMatch: '"8"' },
  ]);
});

test("failed conflict refresh preserves the form and stale baseline with a stable retryable error", async () => {
  const refreshFailure = new Error("network details must not reach the UI");
  const { client } = queuedClient([
    refreshFailure,
    definitionResource({ job: { status: "open", version: 8 } }),
  ]);
  const controller = createJobController({ client });
  const values = { name: "保留的表单", priority: "中" };
  const staleJob = { id: JOB_ID, version: 7, status: "招聘中", formMode: "edit" };

  const failed = await controller.refreshEditBaseline(staleJob, values);

  assert.equal(failed.values, values);
  assert.equal(failed.job, staleJob);
  assert.equal(failed.error, JOB_EDIT_CONFLICT_REFRESH_ERROR);
  assert.equal(failed.retryable, true);
  assert.doesNotMatch(failed.error, /network details/);

  const retried = await controller.refreshEditBaseline(failed.job, failed.values);
  assert.equal(retried.values, values);
  assert.equal(retried.job.version, 8);
  assert.equal(retried.error, "");
});

test("definition update submits explicit unassigned department and owner as null", async () => {
  const { client, calls } = queuedClient([definitionResource()]);
  const controller = createJobController({ client, idempotencyKey: () => "clear-assignment-key" });

  await controller.saveDefinition({
    name: "平台工程师",
    departmentId: "",
    ownerId: "",
    headcount: 2,
    priority: "中",
    jd: "建设平台",
    location: "上海",
    process: "标准流程",
    llmEnabled: false,
    mustHave: [],
    niceToHave: [],
  }, { job: { id: JOB_ID, version: 7, departmentId: DEPARTMENT_ID, ownerId: HIRING_OWNER_ID } });

  assert.equal(calls[0].options.body.department_id, null);
  assert.equal(calls[0].options.body.hiring_owner_id, null);
});

test("mergeDefinition keeps list metadata and complete definition fields", () => {
  const controller = createJobController({ client: { request: async () => { throw new Error("unused"); } } });
  const definition = {
    id: JOB_ID,
    name: "平台工程师",
    departmentId: "",
    department: "",
    ownerId: "",
    owner: "",
    funnel: { review: 4 },
    candidates: 4,
    jd: "完整 JD",
    process: "标准流程",
    mustHave: ["React"],
    niceToHave: ["Vite"],
  };
  const listRecord = {
    id: JOB_ID,
    name: "平台工程师",
    departmentId: DEPARTMENT_ID,
    department: "技术部",
    ownerId: HIRING_OWNER_ID,
    owner: "招聘经理",
    funnel: { new: 2, review: 3 },
    candidates: 5,
    review: 3,
    version: 8,
  };

  assert.deepEqual(controller.mergeDefinition(listRecord, definition), {
    ...definition,
    ...listRecord,
    jd: "完整 JD",
    process: "标准流程",
    mustHave: ["React"],
    niceToHave: ["Vite"],
  });
});

test("mergeDefinition fills names from refreshed facets when a closed job is excluded from the open list", () => {
  const controller = createJobController({ client: { request: async () => { throw new Error("unused"); } } });
  const definition = {
    id: JOB_ID,
    version: 9,
    status: "已关闭",
    name: "平台工程师",
    departmentId: DEPARTMENT_ID,
    department: "",
    ownerId: HIRING_OWNER_ID,
    owner: "",
    funnel: { new: 1, review: 2, decision: 1 },
    candidates: 4,
    review: 2,
    jd: "完整 JD",
    process: "标准流程",
    mustHave: [],
    niceToHave: [],
  };
  const refreshedMetadata = {
    departments: [{ id: DEPARTMENT_ID, name: "技术部" }],
    owners: [{ id: HIRING_OWNER_ID, name: "招聘经理" }],
  };

  const merged = controller.mergeDefinition(null, definition, refreshedMetadata);

  assert.equal(merged.department, "技术部");
  assert.equal(merged.owner, "招聘经理");
  assert.equal(merged.status, "已关闭");
  assert.deepEqual(merged.funnel, { new: 1, review: 2, decision: 1 });
  assert.equal(merged.candidates, 4);
});

test("definition keeps raw owner identities and filtered refresh restores the hiring owner for display and edit", async () => {
  const rawDefinition = definitionResource({
    job: {
      hiring_owner_id: HIRING_OWNER_ID,
      hiring_owner_name: "",
      owner_id: OWNER_ID,
      owner_name: "招聘负责人",
      status: "closed",
      version: 9,
    },
  });
  const { client, calls } = queuedClient([
    rawDefinition,
    { data: { stages: { new: 1, review: 2 }, total: 3 } },
    rawDefinition,
  ]);
  const controller = createJobController({ client, idempotencyKey: () => "edit-owner-key" });

  const definition = await controller.loadDefinition(JOB_ID);
  assert.equal(definition.recruitingOwnerId, OWNER_ID);
  assert.equal(definition.hiringOwnerId, HIRING_OWNER_ID);
  assert.deepEqual({ ownerId: definition.ownerId, owner: definition.owner }, { ownerId: OWNER_ID, owner: "招聘负责人" });

  const merged = controller.mergeDefinition(null, definition, {
    departments: [{ id: DEPARTMENT_ID, name: "技术部" }],
    owners: [
      { id: OWNER_ID, name: "招聘负责人" },
      { id: HIRING_OWNER_ID, name: "招聘经理" },
    ],
  });
  assert.deepEqual({ ownerId: merged.ownerId, owner: merged.owner }, { ownerId: HIRING_OWNER_ID, owner: "招聘经理" });

  await controller.saveDefinition({
    name: definition.name,
    departmentId: definition.departmentId,
    headcount: definition.headcount,
    priority: definition.priority,
    jd: definition.jd,
    location: definition.location,
    process: definition.process,
    llmEnabled: definition.llmEnabled,
    mustHave: definition.mustHave,
    niceToHave: definition.niceToHave,
  }, { job: definition });

  assert.equal(calls.at(-1).options.body.hiring_owner_id, HIRING_OWNER_ID);
});

test("transition maps pause, resume, close, and archive targets with mutation headers", async () => {
  const { client, calls } = queuedClient([
    { data: apiJob({ status: "paused", version: 8 }) },
    { data: apiJob({ status: "open", version: 9 }) },
    { data: apiJob({ status: "closed", version: 10 }) },
    { data: apiJob({ status: "archived", version: 11 }) },
  ]);
  const keys = ["pause", "resume", "close", "archive"];
  const controller = createJobController({ client, idempotencyKey: () => keys.shift() });
  const signal = new AbortController().signal;

  await controller.transition({ id: JOB_ID, version: 7, status: "招聘中" }, "已暂停", { signal });
  await controller.transition({ id: JOB_ID, version: 8, status: "已暂停" }, "招聘中", { signal });
  await controller.transition({ id: JOB_ID, version: 9, status: "招聘中" }, "已关闭", { signal });
  const archived = await controller.transition({ id: JOB_ID, version: 10, status: "已关闭" }, "已归档", { signal });

  assert.deepEqual(calls.map(({ path, options }) => ({ path, options })), [
    { path: `/api/v1/jobs/${JOB_ID}/transitions`, options: { method: "POST", body: { target: "paused" }, ifMatch: '"7"', idempotencyKey: "pause", signal } },
    { path: `/api/v1/jobs/${JOB_ID}/transitions`, options: { method: "POST", body: { target: "open" }, ifMatch: '"8"', idempotencyKey: "resume", signal } },
    { path: `/api/v1/jobs/${JOB_ID}/transitions`, options: { method: "POST", body: { target: "closed" }, ifMatch: '"9"', idempotencyKey: "close", signal } },
    { path: `/api/v1/jobs/${JOB_ID}/transitions`, options: { method: "POST", body: { target: "archived" }, ifMatch: '"10"', idempotencyKey: "archive", signal } },
  ]);
  assert.equal(archived.status, "已归档");
  assert.equal(archived.version, 11);
});

test("mutations reject missing identity, missing version, and unsupported transitions before network I/O", async () => {
  const { client, calls } = queuedClient([]);
  const controller = createJobController({ client });
  const form = { name: "职位" };

  await assert.rejects(() => controller.saveDefinition(form, { job: { version: 1 } }), { code: "JOB_ID_REQUIRED" });
  await assert.rejects(() => controller.saveDefinition(form, { job: { id: JOB_ID } }), { code: "JOB_VERSION_REQUIRED" });
  await assert.rejects(() => controller.transition({ version: 1, status: "招聘中" }, "已暂停"), { code: "JOB_ID_REQUIRED" });
  await assert.rejects(() => controller.transition({ id: JOB_ID, status: "招聘中" }, "已暂停"), { code: "JOB_VERSION_REQUIRED" });
  await assert.rejects(() => controller.transition({ id: JOB_ID, version: 1, status: "招聘中" }, "已归档"), { code: "JOB_TRANSITION_UNSUPPORTED" });
  await assert.rejects(() => controller.transition({ id: JOB_ID, version: 1, status: "未知" }, "已暂停"), { code: "JOB_TRANSITION_UNSUPPORTED" });
  assert.equal(calls.length, 0);
});

test("ApiError and AbortError identities pass through unchanged", async () => {
  const apiError = new ApiError({ status: 409, code: "resource_version_conflict" });
  const abortError = new DOMException("aborted", "AbortError");
  const api = queuedClient([apiError]);
  const aborted = queuedClient([abortError]);

  await assert.rejects(
    () => createJobController({ client: api.client }).listJobs(),
    (error) => error === apiError,
  );
  await assert.rejects(
    () => createJobController({ client: aborted.client }).loadDefinition(JOB_ID),
    (error) => error === abortError,
  );
});
