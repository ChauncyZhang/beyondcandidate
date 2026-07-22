import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { after, before, test } from "node:test";
import { createElement } from "react";
import { renderToStaticMarkup } from "react-dom/server";
import { createServer } from "vite";

let helpers;
let candidateHelpers;
let controllerHelpers;
let vite;

before(async () => {
  vite = await createServer({
    root: process.cwd(),
    logLevel: "silent",
    server: { middlewareMode: true },
    appType: "custom",
  });
  helpers = await vite.ssrLoadModule("/src/ScreeningViews.jsx");
  candidateHelpers = await vite.ssrLoadModule("/src/CandidateViews.jsx");
  controllerHelpers = await vite.ssrLoadModule("/src/screeningController.js");
});

after(async () => {
  await vite?.close();
});

test("target job is preselected only when its title has one exact match", () => {
  const jobs = [
    { id: "job-ai-001", title: "AI 工程师" },
    { id: "job-ai-002", title: "AI 工程师" },
    { id: "job-fe-001", title: "前端工程师" },
  ];

  assert.equal(helpers.resolveInitialJobId(jobs, "前端工程师"), "job-fe-001");
  assert.equal(helpers.resolveInitialJobId(jobs, "AI 工程师"), "");
  assert.equal(helpers.resolveInitialJobId(jobs, "不存在的职位"), "");
});

test("duplicate job titles include a short id while unique titles stay concise", () => {
  const jobs = [
    { id: "job-ai-0001", title: "AI 工程师" },
    { id: "job-ai-0002", title: "AI 工程师" },
    { id: "job-fe-0001", title: "前端工程师" },
  ];

  assert.equal(helpers.jobOptionLabel(jobs[0], jobs), "AI 工程师（ID: job-…0001）");
  assert.equal(helpers.jobOptionLabel(jobs[1], jobs), "AI 工程师（ID: job-…0002）");
  assert.equal(helpers.jobOptionLabel(jobs[2], jobs), "前端工程师");
});

test("format examples can never advance into the real task flow", () => {
  assert.equal(helpers.canAdvanceFromFiles([{ valid: true, sourceFile: {} }]), true);
  assert.equal(helpers.canAdvanceFromFiles([{ valid: true, sourceFile: {}, example: true }]), false);
  assert.equal(helpers.canAdvanceFromFiles([{ valid: false, sourceFile: {} }]), false);
});

test("server candidate labels never present a derived name as verified", () => {
  assert.equal(helpers.candidateDisplayName({ candidate: "张三" }, true), "张三（姓名待核验）");
  assert.equal(helpers.candidateDisplayName({ candidate: "" }, true), "候选人姓名待核验");
  assert.equal(helpers.candidateDisplayName({ candidate: "张三" }, false), "张三");
});

test("screening keeps a single-file LLM retry only when the server marks it retryable", () => {
  assert.deepEqual(helpers.screeningRetryAction({ status: "partial", llmRetryable: true }), { kind: "llm", label: "重试 LLM" });
  assert.equal(helpers.screeningRetryAction({ status: "partial", llmRetryable: false }), null);
  assert.deepEqual(helpers.reconcileRetryingIds(["item-1"], [
    { id: "item-1", status: "partial", llmRetryable: true },
  ]), ["item-1"]);
  assert.deepEqual(helpers.reconcileRetryingIds(["item-1"], [
    { id: "item-1", status: "partial", llmRetryable: false },
  ]), []);
});

test("screening keeps a single-file parser retry only for retryable file failures", () => {
  assert.deepEqual(helpers.screeningRetryAction({ status: "failed", retryable: true }), { kind: "parse", label: "重新解析" });
  assert.equal(helpers.screeningRetryAction({ status: "failed", retryable: false }), null);
  assert.equal(helpers.screeningRetryAction({ status: "success", retryable: true }), null);
});

test("server task metadata uses persisted source and creator context", () => {
  const line = helpers.taskMetadataLine({ id: "run-1", source: "BOSS 直聘", creator: "张小北", createdAt: "刚刚", serverBacked: true });

  assert.match(line, /来源 BOSS 直聘/);
  assert.match(line, /发起人 张小北/);
});

test("screening task status ends with automatic processing instead of a review phase", () => {
  assert.equal(helpers.taskLifecycleLabel({ status: "running", reviewTotal: 5, reviewed: 0 }), "处理中");
  assert.equal(helpers.taskLifecycleLabel({ status: "complete", reviewTotal: 5, reviewed: 0 }), "已完成");
  assert.equal(helpers.taskLifecycleLabel({ status: "complete", reviewTotal: 5, reviewed: 2 }), "已完成");
});

test("screening renders the controller automatic outcome instead of the historical rule score", () => {
  assert.deepEqual(helpers.screeningDisplayOutcome({
    ruleScore: 12,
    score: 72,
    recommendation: "建议评审",
    routeResult: "review",
    routeLabel: "已转交用人经理",
  }), { score: 72, recommendation: "进入评审", routeLabel: "已进入用人经理评审" });
  assert.equal(helpers.screeningRouteLabel({ routeResult: "review" }), "已进入用人经理评审");
  assert.equal(helpers.screeningRouteLabel({ routeResult: "deferred" }), "暂不进入评审");
});

test("screening final AI failure stays neutral and retains manager handoff", () => {
  assert.deepEqual(helpers.screeningDisplayOutcome({
    status: "partial",
    score: null,
    recommendation: "AI评分不可用",
    routeResult: "review",
    routeLabel: "已转交用人经理",
  }), { score: null, recommendation: "AI评分失败 · 已保护性转交评审", routeLabel: "已进入用人经理评审" });
  assert.doesNotMatch(JSON.stringify(helpers.screeningDisplayOutcome({ status: "failed", score: null })), /候选人失败/);
});

test("screening dimensions render five valid scores and malformed optional data degrades safely", () => {
  const dimensions = ["岗位匹配", "技能经验", "项目深度", "成长潜力", "稳定性"].map((label, index) => ({
    label,
    score: 80 - index,
    evidence: [`证据 ${index + 1}`],
    gaps: index === 0 ? [] : [`缺口 ${index}`],
  }));

  assert.deepEqual(helpers.normalizeScreeningDimensions(dimensions).map(({ label, score }) => ({ label, score })), dimensions.map(({ label, score }) => ({ label, score })));
  assert.deepEqual(helpers.normalizeScreeningDimensions([null, { label: "技能经验", score: "bad", evidence: "错误", gaps: 7 }]), [
    { label: "技能经验", score: null, evidence: [], gaps: [] },
  ]);
  assert.deepEqual(helpers.normalizeScreeningDimensions("bad"), []);
});

test("screening summary uses the four server counters without recomputing rows", () => {
  const task = controllerHelpers.normalizeScreeningTask({
    id: "run-summary",
    manager_review_count: 8,
    deferred_count: 3,
    ai_unavailable_count: 2,
    file_failed_count: 1,
  }, [{ id: "item-1", status: "failed", route_result: "deferred" }]);

  assert.deepEqual(helpers.screeningSummaryCounts(task), [
    { label: "已进入用人经理评审", value: 8 },
    { label: "暂不进入评审", value: 3 },
    { label: "AI 评分失败并保护性转交", value: 2 },
    { label: "文件处理失败", value: 1 },
  ]);
  assert.equal(task.managerReviewCount, 8);
  assert.equal("serverCounts" in task, false);
});

test("ScreeningTaskView renders all four server counters instead of recomputing its rows", () => {
  const task = controllerHelpers.normalizeScreeningTask({
    id: "run-rendered-summary",
    job_title: "AI 工程师",
    status: "completed",
    processed_count: 1,
    total_count: 1,
    manager_review_count: 8,
    deferred_count: 3,
    ai_unavailable_count: 2,
    file_failed_count: 1,
  }, [{
    id: "only-row",
    filename: "唯一一行.pdf",
    status: "success",
    route_result: "deferred",
    ai_recommendation: "暂缓",
    ai_score: 42,
    llm_status: "succeeded",
  }]);
  const html = renderToStaticMarkup(createElement(helpers.ScreeningTaskView, {
    task,
    onTaskChange() {},
    onBack() {},
    onOpenCandidate() {},
    onNotify() {},
  }));

  assert.match(html, /<strong>8<\/strong><span>已进入用人经理评审<\/span>/);
  assert.match(html, /<strong>3<\/strong><span>暂不进入评审<\/span>/);
  assert.match(html, /<strong>2<\/strong><span>AI 评分失败并保护性转交<\/span>/);
  assert.match(html, /<strong>1<\/strong><span>文件处理失败<\/span>/);
});

test("screening file errors take priority over simultaneous LLM failure metadata", () => {
  const task = controllerHelpers.normalizeScreeningTask({ id: "run-errors" }, [
    {
      id: "parse-error",
      filename: "损坏简历.pdf",
      status: "failed",
      route_result: "review",
      error_code: "parse_failed",
      llm_status: "failed",
      llm_error_code: "provider_unavailable",
      retryable: true,
    },
    {
      id: "malware-error",
      filename: "危险简历.pdf",
      status: "failed",
      route_result: "review",
      error_code: "malware_detected",
      llm_status: "failed",
      llm_error_code: "provider_unavailable",
    },
  ]);

  assert.equal(task.files[0].recommendation, "未进入AI评分");
  assert.equal(task.files[0].llmErrorCode, "provider_unavailable");
  assert.match(helpers.serverIssueMessage(task.files[0]), /文件解析失败/);
  assert.doesNotMatch(helpers.serverIssueMessage(task.files[0]), /AI评分不可用/);
  assert.match(helpers.serverIssueMessage(task.files[1]), /恶意文件/);
  assert.doesNotMatch(helpers.serverIssueMessage(task.files[1]), /AI评分不可用/);
});

test("technical screening failures render no contradictory route, LLM conclusion, or AI evidence", () => {
  const task = controllerHelpers.normalizeScreeningTask({
    id: "run-technical-failure",
    job_title: "AI 工程师",
    status: "failed",
    processed_count: 1,
    total_count: 1,
    failed_count: 1,
  }, [{
    id: "parse-error",
    filename: "损坏简历.pdf",
    status: "failed",
    error_code: "parse_failed",
    route_result: "review",
    ai_score: 91,
    ai_recommendation: "强烈推荐",
    llm_status: "failed",
    llm_error_code: "provider_unavailable",
    llm_evaluation: {
      dimensions: [{ key: "core_capability", score: 91, evidence: ["矛盾维度证据"] }],
      strengths: ["矛盾优势"],
      risks: ["矛盾风险"],
    },
    retryable: true,
  }]);
  const html = renderToStaticMarkup(createElement(helpers.ScreeningTaskView, {
    task,
    onTaskChange() {},
    onBack() {},
    onOpenCandidate() {},
    onNotify() {},
  }));
  const [row] = html.match(/<div class="screening-row"[\s\S]*?<\/div>/) || [];

  assert.ok(row);
  assert.match(row, /aria-label="流转结果：未流转"[^>]*>未流转<\/span>/);
  assert.match(row, /aria-label="AI 流转结论：未进入AI评分"[^>]*>未进入AI评分<\/span>/);
  assert.match(row, /aria-label="AI 匹配分：—"/);
  assert.doesNotMatch(row, /已进入用人经理评审|AI评分失败|强烈推荐|矛盾维度证据|矛盾优势|矛盾风险/);
  assert.match(row, /文件解析失败/);
});

test("ScreeningTaskView renders final LLM and technical failures with distinct routing evidence", () => {
  const task = controllerHelpers.normalizeScreeningTask({
    id: "run-rendered-failures",
    job_title: "AI 工程师",
    status: "partial",
    processed_count: 2,
    total_count: 2,
    manager_review_count: 1,
    deferred_count: 0,
    ai_unavailable_count: 1,
    file_failed_count: 1,
  }, [{
    id: "llm-final-failure",
    filename: "LLM失败.pdf",
    candidate_name: "李雷",
    status: "success",
    route_result: "review",
    ai_score: 96,
    ai_recommendation: "陈旧推荐",
    llm_status: "failed",
    llm_error_code: "provider_unavailable",
    llm_evaluation: {
      dimensions: [{ key: "core_capability", score: 96, evidence: ["陈旧AI证据"] }],
      strengths: ["陈旧AI优势"],
    },
  }, {
    id: "technical-file-failure",
    filename: "解析失败.pdf",
    candidate_name: "韩梅梅",
    status: "failed",
    error_code: "parse_failed",
    route_result: "review",
    ai_score: 91,
    ai_recommendation: "陈旧技术推荐",
    llm_status: "failed",
    llm_evaluation: {
      dimensions: [{ key: "core_capability", score: 91, evidence: ["陈旧技术AI证据"] }],
      strengths: ["陈旧技术AI优势"],
    },
  }]);
  const html = renderToStaticMarkup(createElement(helpers.ScreeningTaskView, {
    task,
    onTaskChange() {},
    onBack() {},
    onOpenCandidate() {},
    onNotify() {},
  }));
  const rows = [...html.matchAll(/<div class="screening-row"[\s\S]*?<\/div>/g)].map(([row]) => row);
  const llmFailureRow = rows.find((row) => row.includes("LLM失败.pdf"));
  const technicalFailureRow = rows.find((row) => row.includes("解析失败.pdf"));

  assert.ok(llmFailureRow);
  assert.match(llmFailureRow, /aria-label="流转结果：已进入用人经理评审"[^>]*>已进入用人经理评审<\/span>/);
  assert.match(llmFailureRow, /aria-label="AI 流转结论：AI评分失败 · 已保护性转交评审"[^>]*>AI评分失败 · 已保护性转交评审<\/span>/);
  assert.match(llmFailureRow, /aria-label="AI 匹配分：—"/);
  assert.doesNotMatch(llmFailureRow, /陈旧推荐|陈旧AI证据|陈旧AI优势/);

  assert.ok(technicalFailureRow);
  assert.match(technicalFailureRow, /aria-label="流转结果：未流转"[^>]*>未流转<\/span>/);
  assert.match(technicalFailureRow, /aria-label="AI 流转结论：未进入AI评分"[^>]*>未进入AI评分<\/span>/);
  assert.match(technicalFailureRow, /aria-label="AI 匹配分：—"/);
  assert.doesNotMatch(technicalFailureRow, /已进入用人经理评审|AI评分失败|陈旧技术推荐|陈旧技术AI证据|陈旧技术AI优势/);
});

test("screening result grid exposes table semantics and understandable mobile field labels", () => {
  const task = {
    ...controllerHelpers.normalizeScreeningTask({
      id: "run-1",
      job_title: "AI 工程师",
      status: "completed",
      processed_count: 1,
      total_count: 1,
      source: "boss",
      created_by_name: "张小北",
      manager_review_count: 1,
      deferred_count: 0,
      ai_unavailable_count: 0,
      file_failed_count: 0,
    }, [{
      id: "item-1",
      filename: "候选人.pdf",
      candidate_name: "张三",
      candidate_id: "candidate-1",
      status: "success",
      route_result: "review",
      ai_recommendation: "建议评审",
      ai_score: 72,
      llm_status: "succeeded",
      llm_evaluation: {
        dimensions: [{ key: "core_capability", score: 80, evidence: [], gaps: [] }],
        strengths: ["经验匹配"],
        risks: ["到岗时间待确认"],
      },
    }]),
    note: "语义测试",
  };
  const html = renderToStaticMarkup(createElement(helpers.ScreeningTaskView, {
    task,
    onTaskChange() {},
    onBack() {},
    onOpenCandidate() {},
    onNotify() {},
  }));

  assert.match(html, /role="table"[^>]*aria-labelledby="screening-results-title"/);
  assert.equal((html.match(/role="columnheader"/g) || []).length, 8);
  assert.equal((html.match(/role="cell"/g) || []).length, 8);
  for (const label of ["流转结果", "候选人/文件", "处理状态", "AI 流转结论", "AI 匹配分", "维度评分", "主要优势与风险", "查看候选人"]) {
    assert.match(html, new RegExp(`data-label="${label}"`));
  }
  assert.match(html, /aria-label="AI 流转结论：进入评审"/);
  assert.match(html, /aria-label="AI 匹配分：72"/);

  const css = readFileSync(new URL("./product-theme-jobs-screening.css", import.meta.url), "utf8");
  assert.match(css, /content:\s*attr\(data-label\)/);
});

test("screening source has the eight automatic columns and no removed manual path", () => {
  const source = readFileSync(new URL("./ScreeningViews.jsx", import.meta.url), "utf8");
  for (const heading of ["流转结果", "候选人/文件", "处理状态", "AI 流转结论", "AI 匹配分", "维度评分", "主要优势与风险", "查看候选人"]) assert.match(source, new RegExp(heading));
  const removed = ["待" + "HR审核", "HR初筛" + "进度", "待提交" + "用人经理", "advance" + "_to_review"];
  for (const text of removed) assert.equal(source.includes(text), false);
});

test("screening import and demo flow contains only LLM automatic scoring and routing copy", () => {
  const source = readFileSync(new URL("./ScreeningViews.jsx", import.meta.url), "utf8");
  const removed = ["规则" + "评分", "保留规则" + "结果", "规则评分" + "中"];

  for (const text of removed) assert.equal(source.includes(text), false);
  assert.match(source, /LLM 自动评分/);
  assert.match(source, /自动路由/);
  assert.match(source, /保护性转交评审/);
});

test("job configuration describes LLM as the only scoring and routing source", () => {
  const source = readFileSync(new URL("./JobViews.jsx", import.meta.url), "utf8");

  assert.match(source, /LLM 是当前唯一的评分和路由来源/);
  assert.doesNotMatch(source, /规则评分后|规则评分\s*\+\s*LLM 辅助评估|LLM 辅助评估/);
});

test("App no longer wires screening result apply or undo callbacks into ScreeningTaskView", () => {
  const source = readFileSync(new URL("./App.jsx", import.meta.url), "utf8");

  assert.doesNotMatch(source, /function applyScreeningAction/);
  assert.doesNotMatch(source, /onApplyResults=/);
  assert.doesNotMatch(source, /onUndoResults=/);
});

test("cancelled tasks never describe progress as completed", () => {
  assert.equal(helpers.statusLabel("cancelled"), "已取消");
  assert.equal(helpers.progressSummary({ status: "cancelled", completed: 2, total: 5 }), "任务已取消：已处理 2/5 份简历");
  assert.equal(helpers.progressSummary({ status: "complete", completed: 5, total: 5 }), "处理完成：5/5 份简历");
});

test("an interrupted empty upload offers abandonment instead of an ineffective retry", () => {
  assert.deepEqual(helpers.pollFailureAction({ code: "RECOVERED_RUN_EMPTY" }), {
    code: "RECOVERED_RUN_EMPTY",
    message: "该任务在上传前中断，没有可恢复的简历。可放弃此任务后重新导入。",
    action: "cancel",
    label: "放弃任务",
  });
  assert.equal(helpers.pollFailureAction(new Error("network")).action, "retry");
});

test("server retry stays locked while the refreshed row remains failed and retryable", () => {
  assert.deepEqual(helpers.reconcileRetryingIds(["item-1", "missing"], [
    { id: "item-1", status: "failed", retryable: true },
  ]), ["item-1"]);
  assert.deepEqual(helpers.reconcileRetryingIds(["item-1"], [
    { id: "item-1", status: "running", retryable: false },
  ]), []);
  assert.deepEqual(helpers.reconcileRetryingIds(["item-1"], [
    { id: "item-1", status: "failed", retryable: false },
  ]), []);
});

test("server result opens review only from a completed row with a candidate id", () => {
  assert.equal(helpers.canOpenCandidateReview({ status: "success", candidateId: "candidate-1" }, true), true);
  assert.equal(helpers.canOpenCandidateReview({ status: "partial", candidateId: "candidate-2" }, true), true);
  assert.equal(helpers.canOpenCandidateReview({ status: "failed", candidateId: "candidate-3" }, true), false);
  assert.equal(helpers.canOpenCandidateReview({ status: "success", candidateId: "" }, true), false);
  assert.equal(helpers.canOpenCandidateReview({ status: "success", candidate: "同名候选人" }, true), false);
  assert.equal(helpers.canOpenCandidateReview({ status: "success" }, false), true);
});

test("server candidate context keeps identity separate from automatic screening fields", () => {
  const context = helpers.candidateReviewContext({
    candidateId: "candidate-1", applicationId: "application-repeat-2", candidate: "未核验姓名", email: "derived@example.com",
    score: 78, recommendation: "建议评审", routeResult: "review", routeLabel: "已转交用人经理",
    dimensions: [{ label: "技能经验", score: 82, evidence: ["Python"], gaps: ["Kubernetes"] }], strengths: ["经验匹配"], risks: ["规模待确认"],
  }, { jobId: "job-1", position: "AI 工程师" });

  assert.deepEqual(context, {
    candidateId: "candidate-1",
    applicationId: "application-repeat-2",
    jobId: "job-1",
    position: "AI 工程师",
    evidence: {
      score: 78,
      recommendation: "建议评审",
      routeResult: "review",
      routeLabel: "已进入用人经理评审",
      dimensions: [{ label: "技能经验", score: 82, evidence: ["Python"], gaps: ["Kubernetes"] }],
      strengths: ["经验匹配"],
      risks: ["规模待确认"],
    },
  });
  assert.equal("email" in context, false);
  assert.equal("candidate" in context, false);
});

test("screening view context restores filters without row selection state", () => {
  assert.equal(typeof helpers.restoreScreeningViewState, "function");
  assert.deepEqual(helpers.restoreScreeningViewState({
    taskId: "task-1",
    query: "zhang",
    filter: "成功",
  }, {
    id: "task-1",
    serverBacked: true,
    files: [
      { id: "candidate-1", status: "success", application_stage: "new", application_version: 3 },
      { id: "candidate-2", status: "success", application_stage: "review", application_version: 4 },
    ],
  }), {
    query: "zhang",
    filter: "成功",
  });
});

test("server candidate detail exposes the connected interview path and reports conflicts safely", () => {
  assert.deepEqual(candidateHelpers.candidateDetailTabs(true), ["档案与简历", "职位申请", "筛选证据", "面试与反馈", "时间线"]);
  assert.match(candidateHelpers.candidateMutationError({ status: 409 }), /其他成员更新/);
  assert.doesNotMatch(candidateHelpers.candidateMutationError(new Error("database internal.example")), /database|internal/);
  assert.equal(candidateHelpers.resumeDisplayName({ original_filename: "真实简历.pdf" }), "真实简历.pdf");
  assert.equal(candidateHelpers.resumeDisplayName(null), "暂无可用简历");
  assert.deepEqual(candidateHelpers.candidateStageFilterOptions(), ["新简历", "待复核", "AI 初筛暂缓", "待沟通", "待安排", "面试中", "待决策", "已通过", "已录用", "已淘汰", "已撤回"]);
  assert.deepEqual(candidateHelpers.candidateWorkflowActions("待决策", "用人经理").map((item) => item.id), ["hiring_approved", "hiring_rejected"]);
  assert.deepEqual(candidateHelpers.candidateWorkflowActions("待安排", "HR 招聘专员"), []);
  assert.equal(candidateHelpers.candidateNextStep("面试中"), "完成当前面试与面试官反馈");
  assert.equal(candidateHelpers.canScheduleCandidateInterview("待安排", "HR 招聘专员", true), true);
  assert.equal(candidateHelpers.canScheduleCandidateInterview("待决策", "HR 招聘专员", true), true);
  assert.equal(candidateHelpers.canScheduleCandidateInterview("待复核", "HR 招聘专员", true), false);
  assert.equal(candidateHelpers.canScheduleCandidateInterview("待安排", "用人经理", true), false);
  assert.equal(candidateHelpers.canScheduleCandidateInterview("已通过", "HR 招聘专员", true), false);
});
