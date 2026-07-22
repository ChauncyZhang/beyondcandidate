import test from "node:test";
import assert from "node:assert/strict";
import {
  buildReportMetrics,
  filterReportCandidates,
  getRoleCapabilities,
  isPermissionExpansion,
} from "./ux07Domain.js";

const candidates = [
  { id: "1", position: "AI 工程师", stage: "新简历", owner: "张小北", score: 81, ruleScore: 81, llmScore: 78, interviews: [] },
  { id: "2", position: "AI 工程师", stage: "面试中", owner: "张小北", score: 88, ruleScore: 88, llmScore: 84, interviews: [{ result: "推荐" }] },
  { id: "3", position: "Java 后端工程师", stage: "待安排", owner: "陈雨", score: 79, ruleScore: 82, llmScore: 76, interviews: [] },
];

test("filters report candidates by position and owner", () => {
  const result = filterReportCandidates(candidates, { position: "AI 工程师", owner: "张小北" });
  assert.deepEqual(result.map((item) => item.id), ["1", "2"]);
});

test("builds metrics and funnel from the same candidate set", () => {
  const metrics = buildReportMetrics(candidates);
  assert.equal(metrics.candidateCount, 3);
  assert.equal(metrics.funnel.reduce((sum, item) => sum + item.count, 0), 3);
  assert.equal(metrics.screening.rulePassRate, 100);
  assert.equal(metrics.interviews.count, 1);
});

test("counts each position application in report funnels", () => {
  const multiApplicationCandidate = [{
    ...candidates[0],
    position: "Java 后端工程师",
    stage: "新简历",
    applications: [
      { id: "APP-NEW", position: "Java 后端工程师", state: "新简历" },
      { id: "APP-OLD", position: "AI 工程师", state: "待复核" },
    ],
  }];
  const metrics = buildReportMetrics(multiApplicationCandidate);
  assert.equal(metrics.candidateCount, 1);
  assert.equal(metrics.applicationCount, 2);
  assert.equal(metrics.funnel.reduce((sum, item) => sum + item.count, 0), 2);
  const filtered = filterReportCandidates(multiApplicationCandidate, { position: "AI 工程师", stage: "待复核" });
  assert.equal(filtered.length, 1);
  const filteredMetrics = buildReportMetrics(filtered);
  assert.equal(filteredMetrics.applicationCount, 1);
  assert.deepEqual(filteredMetrics.funnel, [{ stage: "待复核", count: 1 }]);
});

test("counts partial screening results as parse successes", () => {
  const metrics = buildReportMetrics(candidates, {
    total: 10,
    success: 6,
    partial: 2,
    failed: 2,
  });

  assert.equal(metrics.parseSuccessRate, 80);
  assert.equal(metrics.screening.parseSuccessRate, 80);
});

test("reports full parse success after all failed items are retried successfully", () => {
  const metrics = buildReportMetrics(candidates, {
    total: 10,
    success: 10,
    partial: 0,
    failed: 0,
  });

  assert.equal(metrics.parseSuccessRate, 100);
  assert.equal(metrics.screening.parseSuccessRate, 100);
});

test("returns an empty parse success rate without screening facts", () => {
  const metrics = buildReportMetrics(candidates);

  assert.equal(metrics.parseSuccessRate, null);
  assert.equal(metrics.screening.parseSuccessRate, null);
});

test("applies explicit role capabilities", () => {
  assert.equal(getRoleCapabilities("招聘管理员").settingsEdit, true);
  assert.equal(getRoleCapabilities("HR").settingsEdit, false);
  assert.equal(getRoleCapabilities("HR").reportScope, "owned");
  assert.equal(getRoleCapabilities("面试官").reportsView, false);
  assert.equal(getRoleCapabilities("面试官").interviewTemplatesView, true);
});

test("detects permission expansion only when new scopes are added", () => {
  assert.equal(isPermissionExpansion(["AI 工程师"], ["AI 工程师", "Java 后端工程师"]), true);
  assert.equal(isPermissionExpansion(["AI 工程师", "Java 后端工程师"], ["AI 工程师"]), false);
});
