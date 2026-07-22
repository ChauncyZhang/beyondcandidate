import assert from "node:assert/strict";
import test from "node:test";
import { createCandidateController, mergeCandidateRecords, normalizeCandidateReview, resolveCandidateJobPreset } from "./candidateController.js";

const candidateId = "candidate-1";
const jobId = "job-1";
const response = (data) => ({ data });

test("candidate preset resolves by stable job id before a duplicate title", () => {
  const jobs = [
    { id: "job-1", title: "AI 工程师" },
    { id: "job-2", title: "AI 工程师" },
  ];

  assert.equal(resolveCandidateJobPreset(jobs, { jobId: "job-2", position: "AI 工程师" }), "job-2");
  assert.equal(resolveCandidateJobPreset(jobs, { position: "AI 工程师" }), "job-1");
  assert.equal(resolveCandidateJobPreset(jobs, { jobId: "missing", position: "AI 工程师" }), "全部职位");
  assert.equal(resolveCandidateJobPreset(jobs, { position: "全部职位" }), "全部职位");
});

test("candidate list safely encodes supported filters and normalizes server rows", async () => {
  const calls = [];
  const signal = new AbortController().signal;
  const client = {
    async request(path, options) {
      calls.push({ path, options });
      return {
        data: [{
          id: "candidate/一",
          display_name: "李 嘉明",
          current_title: "AI & RAG 工程师",
          location: "北京",
          contacts: [{ kind: "phone", value: "138****2468" }, { kind: "email", value: "li***@mail.com" }],
          updated_at: "2026-07-13T09:00:00+00:00",
          application: {
            id: "application-1",
            job_id: "job/一",
            job_title: "平台 & AI",
            owner_id: "owner-1",
            owner_name: "张小北",
            stage: "deferred",
            next_interview_round: "技术二面",
            source: "本地上传",
            updated_at: "2026-07-13T10:00:00+00:00",
            rule_score: 81,
            recommendation: "可沟通",
            ai_score: 76,
            ai_recommendation: "暂缓",
          },
        }],
        meta: {
          limit: 50,
          next_cursor: "next/一",
          owners: [{ id: "owner-2", name: "陈雨" }, { id: "owner-1", name: "张小北" }],
        },
      };
    },
  };

  const result = await createCandidateController({ client }).listCandidates({
    q: "李 & 明",
    jobId: "job/一",
    stage: "待复核",
    ownerId: "owner/一",
    minScore: 0,
    cursor: "cursor/一",
    limit: 50,
  }, { signal });

  assert.deepEqual(calls, [{
    path: "/api/v1/candidates?q=%E6%9D%8E+%26+%E6%98%8E&job_id=job%2F%E4%B8%80&stage=review&owner_id=owner%2F%E4%B8%80&min_score=0&cursor=cursor%2F%E4%B8%80&limit=50",
    options: { signal },
  }]);
  assert.equal(result.nextCursor, "next/一");
  assert.deepEqual(result.ownerOptions, [{ id: "owner-2", name: "陈雨" }, { id: "owner-1", name: "张小北" }]);
  assert.deepEqual(result.records[0], {
    id: "application-1",
    serverBacked: true,
    candidateId: "candidate/一",
    applicationId: "application-1",
    jobId: "job/一",
    ownerId: "owner-1",
    name: "李 嘉明",
    role: "AI & RAG 工程师",
    company: "",
    position: "平台 & AI",
    stage: "AI 初筛暂缓",
    nextRound: "技术二面",
    score: 76,
    recommendation: "暂缓",
    source: "本地上传",
    owner: "张小北",
    city: "北京",
    phone: "138****2468",
    email: "li***@mail.com",
    lastActivity: "07/13 18:00",
    historicalRule: { score: 81, recommendation: "可沟通" },
  });
});

test("candidate list omits empty and all-selector filters and preserves null evidence", async () => {
  const calls = [];
  const client = { async request(path, options) {
    calls.push({ path, options });
    return {
      data: [{ id: "candidate-2", display_name: "待补充", application: null }],
      meta: { limit: 50, next_cursor: null },
    };
  } };

  const result = await createCandidateController({ client }).listCandidates({
    q: "  ", jobId: "全部职位", stage: "全部阶段", ownerId: "全部负责人", minScore: "不限分数", cursor: "", limit: 50,
  });

  assert.deepEqual(calls, [{ path: "/api/v1/candidates?limit=50", options: {} }]);
  assert.equal(result.nextCursor, null);
  assert.equal(result.records[0].id, "candidate-2");
  assert.equal(result.records[0].stage, "无当前申请");
  assert.equal(result.records[0].score, "-");
  assert.equal(result.records[0].recommendation, "不提供当前 AI 结论");
  assert.equal(result.records[0].historicalRule, null);
});

test("candidate list omits an empty minimum score instead of coercing it to zero", async () => {
  const calls = [];
  const client = { async request(path, options) {
    calls.push({ path, options });
    return { data: [], meta: { limit: 50, next_cursor: null } };
  } };

  await createCandidateController({ client }).listCandidates({ minScore: "   ", limit: 50 });

  assert.deepEqual(calls, [{ path: "/api/v1/candidates?limit=50", options: {} }]);
});

test("candidate list forwards AbortSignal and propagates request errors unchanged", async () => {
  const failure = new Error("network unavailable");
  const signal = new AbortController().signal;
  const client = { async request(path, options) {
    assert.equal(path, "/api/v1/candidates?limit=50");
    assert.deepEqual(options, { signal });
    throw failure;
  } };

  await assert.rejects(
    createCandidateController({ client }).listCandidates({ limit: 50 }, { signal }),
    (error) => error === failure,
  );
});

test("jobs list supplies server ids and titles for the candidate position filter", async () => {
  const signal = new AbortController().signal;
  const calls = [];
  const client = { async request(path, options) {
    calls.push({ path, options });
    if (path === "/api/v1/jobs?limit=100") return { data: [{ id: "job-1", title: "AI 工程师" }], meta: { next_cursor: "next/一" } };
    if (path === "/api/v1/jobs?limit=100&cursor=next%2F%E4%B8%80") return { data: [{ id: "job-2", title: "平台工程师" }], meta: { next_cursor: null } };
    throw new Error(`unexpected request ${path}`);
  } };

  assert.deepEqual(await createCandidateController({ client }).listJobs({ signal }), [
    { id: "job-1", title: "AI 工程师" },
    { id: "job-2", title: "平台工程师" },
  ]);
  assert.deepEqual(calls, [
    { path: "/api/v1/jobs?limit=100", options: { signal } },
    { path: "/api/v1/jobs?limit=100&cursor=next%2F%E4%B8%80", options: { signal } },
  ]);
});

test("candidate page append deduplicates by application id and falls back to candidate id", () => {
  const current = [
    { id: "row-1", applicationId: "application-1", candidateId: "candidate-1", name: "旧记录" },
    { id: "candidate-2", applicationId: "", candidateId: "candidate-2", name: "无申请" },
  ];
  const incoming = [
    { id: "row-1-new", applicationId: "application-1", candidateId: "candidate-1", name: "更新记录" },
    { id: "candidate-2-new", applicationId: null, candidateId: "candidate-2", name: "无申请重复" },
    { id: "row-3", applicationId: "application-3", candidateId: "candidate-3", name: "新增记录" },
  ];

  assert.deepEqual(mergeCandidateRecords(current, incoming), [current[0], current[1], incoming[2]]);
});

test("candidate review loads the exact selected application when the same candidate applies to one job twice", async () => {
  const calls = [];
  const client = {
    async request(path) {
      calls.push(path);
      if (path === `/api/v1/candidates/${candidateId}`) return response({
        id: candidateId, display_name: "李嘉明", current_title: "AI 算法工程师", location: "北京",
        owner_id: "user-1", version: 3, updated_at: "2026-07-13T09:00:00+00:00",
        contacts: [{ kind: "phone", value: "138****2468" }, { kind: "email", value: "lij***@mail.com" }],
      });
      if (path === `/api/v1/candidates/${candidateId}/applications`) return response([
        { id: "application-other", candidate_id: candidateId, job_id: "job-2", job_title: "平台工程师", resume_id: "resume-2", owner_id: "user-2", stage: "rejected", source: "manual", human_conclusion: null, version: 4, updated_at: "2026-07-12T09:00:00+00:00" },
        { id: "application-older", candidate_id: candidateId, job_id: jobId, job_title: "AI 工程师", resume_id: "resume-old", owner_id: "user-2", stage: "rejected", source: "manual", human_conclusion: "暂不合适：历史申请", version: 5, updated_at: "2026-07-12T10:00:00+00:00" },
        { id: "application-1", candidate_id: candidateId, job_id: jobId, job_title: "AI 工程师", resume_id: "resume-1", owner_id: "user-1", stage: "review", source: "本地上传", human_conclusion: "需要补充：确认到岗时间", version: 2, updated_at: "2026-07-13T09:00:00+00:00", rule_score: 81, recommendation: "可沟通", ai_score: 86, ai_recommendation: "建议评审", llm_status: "succeeded", llm_evaluation: {
          score: 86,
          recommendation: "建议评审",
          summary: "核心能力与岗位高度匹配。",
          dimensions: [
            { key: "core_capability", score: 34, evidence: ["主导 RAG 平台"], gaps: [] },
            { key: "experience_depth", score: 24, evidence: ["5 年研发经验"], gaps: ["规模待核验"] },
            { key: "role_seniority", score: 16, evidence: ["负责方案设计"], gaps: [] },
            { key: "transferability", score: 7, evidence: ["跨行业交付"], gaps: [] },
            { key: "explicit_constraints", score: 5, evidence: ["地点符合"], gaps: ["到岗时间未知"] },
          ],
          strengths: ["RAG 交付经验"], gaps: ["Kubernetes 未体现"], risks: ["项目规模待确认"], questions: ["请说明最大项目规模"],
        } },
      ]);
      if (path === `/api/v1/candidates/${candidateId}/resumes`) return response([
        { id: "resume-old", candidate_id: candidateId, version_number: 1, created_at: "2026-07-12T08:00:00+00:00" },
        { id: "resume-1", candidate_id: candidateId, version_number: 1, created_at: "2026-07-13T08:00:00+00:00", profile: {
          summary: "负责企业级 RAG 和 Agent 平台交付。",
          skills: ["Python", "RAG", "Agent"],
          experience: "5 年大模型应用研发经验",
          education: "浙江大学 · 计算机本科",
          status: "ready",
          source: "llm",
          summary_origin: "generated",
        } },
      ]);
      if (path === `/api/v1/candidates/${candidateId}/notes?application_id=application-1`) return response([
        { id: "note-1", body: "优先确认到岗时间", author_id: "user-1", created_at: "2026-07-13T09:10:00+00:00" },
      ]);
      if (path === `/api/v1/candidates/${candidateId}/timeline`) return response([
        { id: "event-1", event_type: "application.stage_changed", summary: "Application stage changed from new to review: 筛选后人工复核", actor_id: "user-1", created_at: "2026-07-13T09:05:00+00:00" },
        { id: "event-2", event_type: "application.updated", summary: "Application updated", actor_id: "user-1", created_at: "2026-07-13T09:00:00+00:00" },
      ]);
      throw new Error(`unexpected request ${path}`);
    },
  };

  const review = await createCandidateController({ client }).loadReview({
    candidateId, applicationId: "application-1", jobId, position: "AI 工程师", actor: { id: "user-1", name: "张小北" },
    evidence: { score: 12, recommendation: "不应采用临时结果", dimensions: [], strengths: [], risks: [] },
  });

  assert.equal(calls.length, 5);
  assert.equal(review.id, candidateId);
  assert.equal(review.name, "李嘉明");
  assert.equal(review.position, "AI 工程师");
  assert.equal(review.stage, "待复核");
  assert.equal(review.application.id, "application-1");
  assert.equal(review.resume.id, "resume-1");
  assert.equal(review.owner, "张小北");
  assert.equal(review.humanConclusion, "需要补充");
  assert.equal(review.humanConclusionReason, "确认到岗时间");
  assert.deepEqual(review.notes.map((item) => item.body), ["优先确认到岗时间"]);
  assert.ok(calls.includes(`/api/v1/candidates/${candidateId}/notes?application_id=application-1`));
  assert.equal(review.timeline[0].action, "新简历 → 待复核；原因：筛选后人工复核");
  assert.equal(review.timeline[0].actor, "张小北");
  assert.equal(review.timeline[1].action, "更新职位申请");
  assert.equal(review.candidateId, candidateId);
  assert.equal(review.applicationId, "application-1");
  assert.equal(review.jobId, jobId);
  assert.equal(review.score, 86);
  assert.equal(review.recommendation, "建议评审");
  assert.equal(review.llmSummary, "核心能力与岗位高度匹配。");
  assert.deepEqual(review.dimensions[0], { key: "core_capability", label: "核心能力匹配", score: 34, evidence: ["主导 RAG 平台"], gaps: [] });
  assert.deepEqual(review.strengths, ["RAG 交付经验"]);
  assert.deepEqual(review.gaps, ["Kubernetes 未体现"]);
  assert.deepEqual(review.risks, ["项目规模待确认"]);
  assert.deepEqual(review.questions, ["请说明最大项目规模"]);
  assert.deepEqual(review.historicalRule, { score: 81, recommendation: "可沟通" });
  assert.equal(review.summary, "负责企业级 RAG 和 Agent 平台交付。");
  assert.equal(review.summaryOrigin, "generated");
  assert.equal(review.profileStatus, "ready");
  assert.equal(review.profileSource, "llm");
  assert.deepEqual(review.skills, ["Python", "RAG", "Agent"]);
  assert.equal(review.experience, "5 年大模型应用研发经验");
  assert.equal(review.education, "浙江大学 · 计算机本科");
  assert.deepEqual(review.applications.map((item) => [item.id, item.position, item.state]), [
    ["application-other", "平台工程师", "已淘汰"],
    ["application-older", "AI 工程师", "已淘汰"],
    ["application-1", "AI 工程师", "待复核"],
  ]);
});

test("candidate profile never uses JD screening reason as the personal summary", () => {
  const review = normalizeCandidateReview({
    candidate: { id: candidateId, display_name: "候选人" },
    applications: [{ id: "application-1", candidate_id: candidateId, job_id: jobId, resume_id: "resume-1", stage: "review" }],
    resumes: [{ id: "resume-1", profile: { summary: null, status: "unavailable", source: "rules" } }],
    notes: [],
    timeline: [],
    context: {
      candidateId,
      applicationId: "application-1",
      jobId,
      actor: {},
      evidence: {
        llmReason: "该候选人与当前 AI 工程师 JD 高度匹配。",
        matched: "Python、RAG",
      },
    },
  });

  assert.equal(review.summary, "简历中未识别到明确的个人简介。");
  assert.equal(review.profileStatus, "unavailable");
  assert.deepEqual(review.skills, []);
  assert.notEqual(review.summary, review.llmReason);
});

test("candidate review preserves ScreeningViews LLM evidence when opened from a screening result", () => {
  const review = normalizeCandidateReview({
    candidate: { id: candidateId, display_name: "候选人" },
    applications: [], resumes: [], notes: [], timeline: [],
    context: { candidateId, applicationId: "application-screening", jobId, position: "AI 工程师", actor: {}, evidence: {
      score: 79,
      recommendation: "建议评审",
      dimensions: [{ label: "核心能力匹配", score: 31, evidence: ["Python"], gaps: ["Go 未体现"] }],
      strengths: ["后端经验"],
      gaps: ["行业经验未知"],
      risks: ["到岗时间未知"],
      questions: ["何时可到岗？"],
      summary: "具备核心后端经验。",
    } },
  });

  assert.equal(review.candidateId, candidateId);
  assert.equal(review.applicationId, "application-screening");
  assert.equal(review.jobId, jobId);
  assert.equal(review.score, 79);
  assert.deepEqual(review.dimensions[0].evidence, ["Python"]);
  assert.deepEqual(review.strengths, ["后端经验"]);
  assert.deepEqual(review.risks, ["到岗时间未知"]);
});

test("failed LLM status never falls back to stale evaluation or transient evidence", () => {
  const review = normalizeCandidateReview({
    candidate: { id: candidateId, display_name: "候选人" },
    applications: [{
      id: "application-1", candidate_id: candidateId, job_id: jobId, stage: "review", version: 1,
      llm_status: "failed", ai_score: null, ai_recommendation: "AI评分不可用",
      llm_evaluation: { score: 92, recommendation: "建议评审", dimensions: [{ key: "core_capability", score: 35, evidence: ["陈旧证据"], gaps: [] }] },
    }],
    resumes: [], notes: [], timeline: [],
    context: { candidateId, applicationId: "application-1", jobId, actor: {}, evidence: { score: 88, recommendation: "陈旧临时结论", strengths: ["陈旧优势"] } },
  });

  assert.equal(review.score, null);
  assert.equal(review.recommendation, "AI评分不可用");
  assert.deepEqual(review.dimensions, []);
  assert.deepEqual(review.strengths, []);
});

test("candidate review without a current LLM projection stays neutral and keeps legacy rules historical", () => {
  const review = normalizeCandidateReview({
    candidate: { id: candidateId, display_name: "候选人" },
    applications: [{
      id: "application-legacy", candidate_id: candidateId, job_id: jobId, stage: "review", version: 1,
      rule_score: 91, recommendation: "旧规则建议推进",
    }],
    resumes: [], notes: [], timeline: [],
    context: { candidateId, applicationId: "application-legacy", jobId, actor: {}, evidence: {} },
  });

  assert.equal(review.recommendation, "不提供当前 AI 结论");
  assert.equal(review.score, null);
  assert.deepEqual(review.dimensions, []);
  assert.deepEqual(review.historicalRule, { score: 91, recommendation: "旧规则建议推进" });
});

test("normalization refuses cross-job fallback and preserves masked contacts only", () => {
  const review = normalizeCandidateReview({
    candidate: { id: candidateId, display_name: "候选人", contacts: [{ kind: "phone", value: "139****0000" }] },
    applications: [{ id: "wrong", candidate_id: candidateId, job_id: "job-2", stage: "review", source: "manual", version: 1 }],
    resumes: [], notes: [], timeline: [],
    context: { applicationId: "wrong", jobId, position: "AI 工程师", actor: { id: "user-1", name: "张小北" }, evidence: {} },
  });

  assert.equal(review.application, null);
  assert.equal(review.stage, "无当前申请");
  assert.equal(review.phone, "139****0000");
  assert.equal(review.email, "未提供");
});

test("candidate controller exposes workflow actions without the removed conclusion mutation", () => {
  const controller = createCandidateController();
  assert.equal(controller.saveConclusion, undefined);
  assert.equal(typeof controller.workflowAction, "function");
});

test("workflow actions use semantic commands, require business reasons, and send idempotency plus version", async () => {
  const calls = [];
  const client = { async request(path, options) { calls.push({ path, options }); return response({ id: "application-1", stage: "interview_pending", version: 4 }); } };
  const controller = createCandidateController({ client, idempotencyKey: () => "transition-key" });

  await assert.rejects(controller.workflowAction({ id: "application-1", version: 2 }, "review_rejected", "   "), (error) => error?.code === "WORKFLOW_REASON_REQUIRED");
  const advanced = await controller.workflowAction({ id: "application-1", version: 2 }, "review_approved", "经验匹配");

  assert.equal(advanced.stage, "interview_pending");
  assert.deepEqual(calls, [{ path: "/api/v1/applications/application-1/workflow-actions", options: {
    method: "POST", ifMatch: '"2"', idempotencyKey: "transition-key", body: { action: "review_approved", reason_text: "经验匹配" },
  } }]);
});

test("notes, preview, and ticket download use the authorized server APIs", async () => {
  const calls = [];
  const client = {
    async request(path, options = {}) {
      calls.push({ kind: "json", path, options });
      if (path.endsWith("/notes")) return response({ id: "note-2", body: options.body.body, author_id: "user-1" });
      if (path.endsWith("/preview")) return response({ resume_id: "resume-1", text: "private preview" });
      if (path.endsWith("/download-tickets")) return response({ token: "one-time-token", expires_in: 60 });
      throw new Error(`unexpected request ${path}`);
    },
    async download(path, options) { calls.push({ kind: "download", path, options }); return { blob: new Blob(["resume"]), filename: "candidate.pdf" }; },
  };
  const controller = createCandidateController({ client });

  assert.equal((await controller.addNote(candidateId, "application-1", "  电话沟通后更新  ")).body, "电话沟通后更新");
  assert.equal((await controller.previewResume("resume-1")).text, "private preview");
  const previewFile = await controller.getResumeFile("resume-1");
  const downloaded = await controller.downloadResume("resume-1");

  assert.equal(previewFile.filename, "candidate.pdf");
  assert.equal(downloaded.filename, "candidate.pdf");
  assert.deepEqual(calls[0], { kind: "json", path: `/api/v1/candidates/${candidateId}/notes`, options: { method: "POST", body: { application_id: "application-1", body: "电话沟通后更新" } } });
  assert.deepEqual(calls.find((call) => call.path.endsWith("/file")), { kind: "download", path: "/api/v1/resumes/resume-1/file", options: {} });
  assert.deepEqual(calls.at(-1), { kind: "download", path: "/api/v1/download-tickets/consume", options: { method: "POST", body: { token: "one-time-token" } } });
});
