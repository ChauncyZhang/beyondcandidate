import test from "node:test";
import assert from "node:assert/strict";
import {
  candidateDetailPath,
  candidateListPath,
  clearJobCreateDraft,
  parseAppRoute,
  readJobCreateDraft,
  routeForNav,
  safeNavigateBack,
  screeningTaskPath,
  settingsPath,
  writeJobCreateDraft,
} from "./appRouter.js";

const UUID_A = "11111111-1111-4111-8111-111111111111";

test("parses every required application route from the URL", () => {
  const cases = [
    ["/workbench", { kind: "workbench", nav: "工作台" }],
    ["/jobs", { kind: "jobs", nav: "职位", mode: "list" }],
    ["/jobs/new", { kind: "jobs", nav: "职位", mode: "new" }],
    ["/screening/tasks", { kind: "screening", nav: "筛选任务", mode: "list" }],
    [`/screening/tasks/${UUID_A}?q=%E6%9E%97&status=failed`, { kind: "screening", nav: "筛选任务", mode: "detail", id: UUID_A, query: "林", status: "失败" }],
    [`/jobs/${UUID_A}`, { kind: "jobs", nav: "职位", mode: "detail", id: UUID_A }],
    [`/jobs/${UUID_A}/edit`, { kind: "jobs", nav: "职位", mode: "edit", id: UUID_A }],
    ["/candidates", { kind: "candidates", nav: "候选人", mode: "list" }],
    [`/candidates/${UUID_A}?tab=timeline`, { kind: "candidates", nav: "候选人", mode: "detail", id: UUID_A, tab: "时间线" }],
    ["/interviews", { kind: "interviews", nav: "面试", mode: "list" }],
    ["/interviews/new", { kind: "interviews", nav: "面试", mode: "new" }],
    [`/interviews/${UUID_A}/reschedule`, { kind: "interviews", nav: "面试", mode: "reschedule", id: UUID_A }],
    [`/interviews/${UUID_A}/feedback`, { kind: "interviews", nav: "面试", mode: "feedback", id: UUID_A }],
    ["/talent", { kind: "talent", nav: "人才库", mode: "list" }],
    [`/talent/${UUID_A}`, { kind: "talent", nav: "人才库", mode: "detail", id: UUID_A }],
    ["/reports", { kind: "reports", nav: "报表" }],
    ["/settings/organization/members", { kind: "settings", nav: "设置", section: "组织与权限", tab: "成员" }],
    ["/settings/organization/departments", { kind: "settings", nav: "设置", section: "组织与权限", tab: "部门" }],
    ["/settings/templates/workflows", { kind: "settings", nav: "设置", section: "流程与评价模板", tab: "招聘流程" }],
    ["/settings/templates/rejection-reasons", { kind: "settings", nav: "设置", section: "流程与评价模板", tab: "淘汰原因" }],
    ["/settings/templates/interview-scorecards", { kind: "settings", nav: "设置", section: "流程与评价模板", tab: "面试评价模板" }],
    ["/settings/ai", { kind: "settings", nav: "设置", section: "AI 设置" }],
    ["/settings/feishu", { kind: "settings", nav: "设置", section: "飞书集成" }],
    ["/settings/governance", { kind: "settings", nav: "设置", section: "审计与数据治理" }],
  ];

  for (const [url, expected] of cases) {
    const parsed = parseAppRoute(new URL(url, "https://ats.example.test"));
    assert.deepEqual(Object.fromEntries(Object.keys(expected).map((key) => [key, parsed[key]])), expected, url);
  }
});

test("candidate list URL keeps only meaningful key filters", () => {
  const path = candidateListPath({
    q: "  林  ",
    jobId: UUID_A,
    stage: "待复核",
    ownerId: "22222222-2222-4222-8222-222222222222",
    minScore: "80",
  });
  assert.equal(path, `/candidates?q=%E6%9E%97&job=${UUID_A}&stage=%E5%BE%85%E5%A4%8D%E6%A0%B8&owner=22222222-2222-4222-8222-222222222222&minScore=80`);
  assert.deepEqual(parseAppRoute(new URL(path, "https://ats.example.test")).filters, {
    q: "林", jobId: UUID_A, stage: "待复核", ownerId: "22222222-2222-4222-8222-222222222222", minScore: "80",
  });
  assert.equal(candidateListPath({ q: "", jobId: "全部职位", stage: "全部阶段", ownerId: "全部负责人", minScore: "不限分数" }), "/candidates");
});

test("candidate detail tab and settings return target are encoded in URLs", () => {
  assert.equal(candidateDetailPath({ id: UUID_A }, "面试与反馈"), `/candidates/${UUID_A}?tab=interviews`);
  assert.equal(
    candidateDetailPath({ id: UUID_A }, "档案与简历", screeningTaskPath("run-1", { query: "林", status: "失败" })),
    `/candidates/${UUID_A}?return=%2Fscreening%2Ftasks%2Frun-1%3Fq%3D%25E6%259E%2597%26status%3Dfailed`,
  );
  assert.equal(settingsPath("组织与权限", "部门", "/jobs/new"), "/settings/organization/departments?return=%2Fjobs%2Fnew");
  assert.equal(settingsPath("飞书集成"), "/settings/feishu");
  assert.equal(parseAppRoute(new URL("/settings/organization/departments?return=%2Fjobs%2Fnew", "https://ats.example.test")).returnTo, "/jobs/new");
});

test("primary navigation maps to canonical list routes", () => {
  assert.equal(routeForNav("工作台"), "/workbench");
  assert.equal(routeForNav("职位"), "/jobs");
  assert.equal(routeForNav("筛选任务"), "/screening/tasks");
  assert.equal(routeForNav("候选人"), "/candidates");
  assert.equal(routeForNav("面试"), "/interviews");
  assert.equal(routeForNav("人才库"), "/talent");
  assert.equal(routeForNav("报表"), "/reports");
  assert.equal(routeForNav("设置"), "/settings/organization/members");
});

test("page back uses browser history only for an in-app entry", () => {
  const calls = [];
  const navigate = (...args) => calls.push(args);
  assert.equal(safeNavigateBack(navigate, "/candidates", { idx: 2 }), true);
  assert.deepEqual(calls.shift(), [-1]);
  assert.equal(safeNavigateBack(navigate, "/candidates", { idx: 0 }), false);
  assert.deepEqual(calls.shift(), ["/candidates", { replace: true }]);
});

test("job create draft persists for the session and clears explicitly", () => {
  const data = new Map();
  const storage = {
    getItem: (key) => data.get(key) ?? null,
    setItem: (key, value) => data.set(key, value),
    removeItem: (key) => data.delete(key),
  };
  const draft = { name: "平台工程师", departmentId: "", jd: "draft" };
  writeJobCreateDraft(storage, "user-1", draft);
  assert.deepEqual(readJobCreateDraft(storage, "user-1"), draft);
  clearJobCreateDraft(storage, "user-1");
  assert.equal(readJobCreateDraft(storage, "user-1"), null);
  assert.doesNotThrow(() => writeJobCreateDraft(null, "user-1", draft));
});
