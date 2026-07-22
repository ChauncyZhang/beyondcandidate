import { apiClient } from "./apiClient.js";

const API_TO_UI_STAGE = {
  new: "新简历",
  review: "待复核",
  deferred: "AI 初筛暂缓",
  contact: "待沟通",
  interview_pending: "待安排",
  interviewing: "面试中",
  decision: "待决策",
  passed: "已通过",
  hired: "已录用",
  rejected: "已淘汰",
  withdrawn: "已撤回",
};

const UI_TO_API_STAGE = Object.fromEntries(Object.entries(API_TO_UI_STAGE).map(([api, ui]) => [ui, api]));
const CONCLUSIONS = new Set(["建议推进", "需要补充", "暂不合适"]);
const DIMENSION_LABELS = {
  core_capability: "核心能力匹配",
  experience_depth: "经验深度",
  role_seniority: "职级匹配",
  transferability: "经验可迁移性",
  explicit_constraints: "明确约束",
};

function safeString(value, fallback = "") {
  return typeof value === "string" ? value : fallback;
}

function safeArray(value) {
  return Array.isArray(value) ? value : [];
}

function safeScore(value) {
  return typeof value === "number" && Number.isFinite(value) ? value : null;
}

function historicalRule(application) {
  const score = safeScore(application?.rule_score);
  const recommendation = safeString(application?.recommendation).trim();
  return score !== null || recommendation ? { score, recommendation } : null;
}

function normalizeDimensions(value) {
  return safeArray(value).flatMap((item) => {
    if (!item || typeof item !== "object") return [];
    const key = safeString(item.key);
    const label = safeString(item.label, DIMENSION_LABELS[key] || key).trim();
    if (!label) return [];
    return [{
      ...(key ? { key } : {}),
      label,
      score: safeScore(item.score),
      evidence: safeArray(item.evidence).map((entry) => safeString(entry).trim()).filter(Boolean),
      gaps: safeArray(item.gaps).map((entry) => safeString(entry).trim()).filter(Boolean),
    }];
  });
}

function normalizeLlmEvidence(application, contextEvidence) {
  const unavailable = application?.llm_status === "failed" && application?.ai_score == null;
  const persisted = !unavailable && application?.llm_evaluation && typeof application.llm_evaluation === "object"
    ? application.llm_evaluation
    : null;
  const source = persisted || (!unavailable ? contextEvidence : null) || {};
  return {
    score: unavailable ? null : safeScore(persisted?.score ?? application?.ai_score ?? source.score),
    recommendation: unavailable
      ? "AI评分不可用"
      : safeString(persisted?.recommendation ?? application?.ai_recommendation ?? source.recommendation).trim() || "不提供当前 AI 结论",
    summary: safeString(source.summary),
    dimensions: normalizeDimensions(source.dimensions),
    strengths: safeArray(source.strengths).map((item) => safeString(item).trim()).filter(Boolean),
    gaps: safeArray(source.gaps).map((item) => safeString(item).trim()).filter(Boolean),
    risks: safeArray(source.risks).map((item) => safeString(item).trim()).filter(Boolean),
    questions: safeArray(source.questions).map((item) => safeString(item).trim()).filter(Boolean),
  };
}

function codedError(code, message) {
  const error = new Error(message);
  error.code = code;
  return error;
}

function signalOption(signal) {
  return signal ? { signal } : {};
}

function parseConclusion(value) {
  const text = safeString(value).trim();
  if (!text) return { conclusion: "", reason: "" };
  const separator = text.indexOf("：");
  const conclusion = separator < 0 ? text : text.slice(0, separator);
  return CONCLUSIONS.has(conclusion)
    ? { conclusion, reason: separator < 0 ? "" : text.slice(separator + 1) }
    : { conclusion: "", reason: text };
}

function contactValue(contacts, kind) {
  return safeString(safeArray(contacts).find((item) => item?.kind === kind)?.value, "未提供");
}

function displayDateTime(value) {
  const raw = safeString(value);
  if (!raw) return "未记录";
  const date = new Date(raw);
  if (Number.isNaN(date.getTime())) return "未记录";
  return new Intl.DateTimeFormat("zh-CN", { month: "2-digit", day: "2-digit", hour: "2-digit", minute: "2-digit", hour12: false }).format(date);
}

function normalizeTimelineEvent(item, actor) {
  const labels = {
    "candidate.created": "创建候选人档案",
    "candidate.corrected": "更新候选人档案",
    "candidate.note_added": "添加招聘备注",
    "application.created": "创建职位申请",
    "application.updated": "更新职位申请",
    "application.stage_changed": "更新职位申请阶段",
  };
  const summary = safeString(item?.summary);
  const transition = summary.match(/^Application stage changed from ([a-z_]+) to ([a-z_]+)(?:: (.+))?$/);
  const action = transition
    ? `${API_TO_UI_STAGE[transition[1]] || "未知阶段"} → ${API_TO_UI_STAGE[transition[2]] || "未知阶段"}${transition[3] ? `；原因：${transition[3]}` : ""}`
    : labels[item?.event_type] || "更新候选人记录";
  return {
    id: safeString(item?.id),
    action,
    time: displayDateTime(item?.created_at),
    actor: safeString(item?.actor_id) === actor?.id ? safeString(actor?.name, "当前用户") : "招聘团队成员",
  };
}

function normalizeCandidateListItem(candidate) {
  const application = candidate?.application || null;
  const score = safeScore(application?.ai_score);
  const recommendation = safeString(application?.ai_recommendation).trim() || "不提供当前 AI 结论";
  const candidateId = safeString(candidate?.id);
  const applicationId = safeString(application?.id);
  return {
    id: applicationId || candidateId,
    serverBacked: true,
    candidateId,
    applicationId,
    jobId: safeString(application?.job_id),
    ownerId: safeString(application?.owner_id),
    name: safeString(candidate?.display_name, "未命名候选人"),
    role: safeString(candidate?.current_title, "当前职称未填写"),
    company: "",
    position: safeString(application?.job_title, "无当前申请"),
    stage: application ? (API_TO_UI_STAGE[application.stage] || "未知阶段") : "无当前申请",
    nextRound: safeString(application?.next_interview_round),
    score: score ?? "-",
    recommendation,
    source: safeString(application?.source, "未记录"),
    owner: safeString(application?.owner_name, "未分配"),
    city: safeString(candidate?.location, "地点未填写"),
    phone: contactValue(candidate?.contacts, "phone"),
    email: contactValue(candidate?.contacts, "email"),
    lastActivity: displayDateTime(application?.updated_at || candidate?.updated_at),
    historicalRule: historicalRule(application),
  };
}

export function mergeCandidateRecords(current, incoming) {
  const seen = new Set(safeArray(current).map((item) => item?.applicationId || item?.candidateId).filter(Boolean));
  return [...safeArray(current), ...safeArray(incoming).filter((item) => {
    const key = item?.applicationId || item?.candidateId;
    if (!key || seen.has(key)) return false;
    seen.add(key);
    return true;
  })];
}

export function resolveCandidateJobPreset(jobs, initialFilters) {
  const records = safeArray(jobs);
  const requestedId = safeString(initialFilters?.jobId).trim();
  if (requestedId) return records.some((job) => job?.id === requestedId) ? requestedId : "全部职位";
  const requestedTitle = safeString(initialFilters?.position).trim();
  if (!requestedTitle || requestedTitle === "全部职位") return "全部职位";
  return records.find((job) => job?.title === requestedTitle)?.id || "全部职位";
}

function normalizeOwnerFacets(owners) {
  return safeArray(owners).map((item) => ({ id: safeString(item?.id), name: safeString(item?.name) })).filter((item) => item.id && item.name);
}

function selectApplication(applications, candidateId, context) {
  return safeArray(applications).find((item) => (
    item?.candidate_id === candidateId
    && item?.job_id === context.jobId
    && (!context.applicationId || item?.id === context.applicationId)
  )) || null;
}

export function normalizeCandidateReview({ candidate, applications, resumes, notes, timeline, context }) {
  const candidateId = safeString(candidate?.id);
  const candidateApplications = safeArray(applications).filter((item) => item?.candidate_id === candidateId);
  const application = selectApplication(candidateApplications, candidateId, context);
  const resume = application
    ? safeArray(resumes).find((item) => item?.id === application.resume_id) || null
    : null;
  const evidence = context.evidence || {};
  const llmEvidence = normalizeLlmEvidence(application, evidence);
  const parsedConclusion = parseConclusion(application?.human_conclusion);
  const actorOwnsApplication = application?.owner_id && application.owner_id === context.actor?.id;
  const matched = safeString(evidence.matched);
  const profile = resume?.profile && typeof resume.profile === "object" ? resume.profile : {};
  const structuredSkills = safeArray(profile.skills).map((item) => safeString(item)).filter(Boolean);

  return {
    id: candidateId,
    candidateId,
    applicationId: safeString(application?.id, safeString(context.applicationId)),
    jobId: safeString(application?.job_id, safeString(context.jobId)),
    serverBacked: true,
    name: safeString(candidate?.display_name, "未命名候选人"),
    role: safeString(candidate?.current_title, "当前职称未填写"),
    company: "",
    city: safeString(candidate?.location, "地点未填写"),
    phone: contactValue(candidate?.contacts, "phone"),
    email: contactValue(candidate?.contacts, "email"),
    position: safeString(application?.job_title, safeString(context.position, "当前职位")),
    stage: application ? (API_TO_UI_STAGE[application.stage] || "未知阶段") : "无当前申请",
    nextRound: safeString(application?.next_interview_round),
    source: safeString(application?.source, "未记录"),
    owner: actorOwnsApplication ? safeString(context.actor?.name, "当前 HR") : "招聘负责人",
    lastActivity: displayDateTime(application?.updated_at || candidate?.updated_at),
    recommendation: llmEvidence.recommendation,
    score: llmEvidence.score,
    llmSummary: llmEvidence.summary,
    dimensions: llmEvidence.dimensions,
    strengths: llmEvidence.strengths,
    gaps: llmEvidence.gaps,
    risks: llmEvidence.risks,
    questions: llmEvidence.questions,
    historicalRule: historicalRule(application),
    matched: matched || "暂无命中项",
    missing: safeString(evidence.missing, "暂无缺失项"),
    risk: safeString(evidence.risk, "暂无已记录风险"),
    llmReason: safeString(evidence.llmReason, "本次筛选未返回 LLM 摘要。"),
    summary: safeString(profile.summary, "简历中未识别到明确的个人简介。"),
    summaryOrigin: ["llm", "ocr_llm"].includes(safeString(profile.source)) ? safeString(profile.summary_origin) : "",
    profileStatus: safeString(profile.status, "unavailable"),
    profileSource: safeString(profile.source, "rules"),
    skills: structuredSkills,
    education: safeString(profile.education, "简历中未识别到教育经历。"),
    experience: safeString(profile.experience, "简历中未识别到工作经历。"),
    tags: [],
    notes: safeArray(notes).map((item) => ({ id: safeString(item?.id), body: safeString(item?.body), authorId: safeString(item?.author_id), createdAt: safeString(item?.created_at) })).filter((item) => item.id && item.body),
    timeline: safeArray(timeline).map((item) => normalizeTimelineEvent(item, context.actor)).filter((item) => item.id),
    applications: candidateApplications.map((item) => ({
      id: item.id,
      position: safeString(item.job_title, item.job_id === context.jobId ? safeString(context.position, "当前职位") : "职位名称未提供"),
      state: API_TO_UI_STAGE[item.stage] || "未知阶段",
      created: displayDateTime(item.updated_at),
      source: safeString(item.source, "未记录"),
    })),
    interviews: [],
    application,
    resume,
    humanConclusion: parsedConclusion.conclusion,
    humanConclusionReason: parsedConclusion.reason,
    version: application?.version ?? candidate?.version ?? 0,
  };
}

export function createCandidateController({ client = apiClient, idempotencyKey = () => globalThis.crypto.randomUUID() } = {}) {
  async function listCandidates(filters = {}, { signal } = {}) {
    const params = new URLSearchParams();
    const query = safeString(filters.q).trim();
    const jobId = safeString(filters.jobId).trim();
    const stage = safeString(filters.stage).trim();
    const ownerId = safeString(filters.ownerId).trim();
    const cursor = safeString(filters.cursor).trim();
    const minScoreRaw = filters.minScore == null ? "" : String(filters.minScore).trim();
    const minScore = minScoreRaw ? Number(minScoreRaw) : Number.NaN;
    const limit = Number(filters.limit);
    if (query) params.set("q", query);
    if (jobId && jobId !== "全部职位") params.set("job_id", jobId);
    if (stage && stage !== "全部阶段") params.set("stage", UI_TO_API_STAGE[stage] || stage);
    if (ownerId && ownerId !== "全部负责人") params.set("owner_id", ownerId);
    if (Number.isFinite(minScore) && minScore >= 0 && minScore <= 100) params.set("min_score", String(minScore));
    if (cursor) params.set("cursor", cursor);
    if (Number.isInteger(limit) && limit > 0) params.set("limit", String(limit));
    const result = await client.request(`/api/v1/candidates?${params.toString()}`, signalOption(signal));
    return {
      records: safeArray(result?.data).map(normalizeCandidateListItem),
      nextCursor: safeString(result?.meta?.next_cursor) || null,
      ownerOptions: normalizeOwnerFacets(result?.meta?.owners),
    };
  }

  async function listJobs({ signal } = {}) {
    const jobs = [];
    let cursor = "";
    do {
      const params = new URLSearchParams({ limit: "100" });
      if (cursor) params.set("cursor", cursor);
      const result = await client.request(`/api/v1/jobs?${params.toString()}`, signalOption(signal));
      jobs.push(...safeArray(result?.data).map((item) => ({ id: safeString(item?.id), title: safeString(item?.title) })).filter((item) => item.id && item.title));
      cursor = safeString(result?.meta?.next_cursor);
    } while (cursor);
    return jobs;
  }

  async function loadReview(context, { signal } = {}) {
    if (!safeString(context?.candidateId)) throw codedError("CANDIDATE_ID_REQUIRED", "candidate id required");
    const root = `/api/v1/candidates/${context.candidateId}`;
    const options = signalOption(signal);
    const [candidate, applications, resumes, timeline] = await Promise.all([
      client.request(root, options),
      client.request(`${root}/applications`, options),
      client.request(`${root}/resumes`, options),
      client.request(`${root}/timeline`, options),
    ]);
    const candidateData = candidate?.data;
    if (safeString(candidateData?.id) !== context.candidateId) throw codedError("CANDIDATE_MISMATCH", "candidate mismatch");
    const application = selectApplication(applications?.data, context.candidateId, context);
    if (context.applicationId && !application) throw codedError("APPLICATION_MISMATCH", "application mismatch");
    const notes = application
      ? await client.request(`${root}/notes?application_id=${encodeURIComponent(application.id)}`, options)
      : { data: [] };
    return normalizeCandidateReview({
      candidate: candidateData,
      applications: applications?.data,
      resumes: resumes?.data,
      notes: notes?.data,
      timeline: timeline?.data,
      context,
    });
  }

  async function workflowAction(application, action, reason = "", { signal } = {}) {
    const detail = safeString(reason).trim();
    const supported = new Set(["review_approved", "review_rejected", "hiring_approved", "hiring_rejected", "offer_accepted", "offer_declined"]);
    const reasonRequired = new Set(["review_rejected", "hiring_rejected", "offer_declined"]);
    if (!application?.id || !Number.isInteger(application.version) || !supported.has(action)) throw codedError("WORKFLOW_ACTION_INVALID", "workflow action invalid");
    if (reasonRequired.has(action) && !detail) throw codedError("WORKFLOW_REASON_REQUIRED", "workflow reason required");
    const body = { action };
    if (detail) body.reason_text = detail;
    const result = await client.request(`/api/v1/applications/${application.id}/workflow-actions`, {
      method: "POST", ifMatch: `"${application.version}"`, idempotencyKey: idempotencyKey(), body, ...signalOption(signal),
    });
    return result?.data;
  }

  async function addNote(candidateId, applicationId, body, { signal } = {}) {
    const value = safeString(body).trim();
    if (!safeString(applicationId)) throw codedError("APPLICATION_REQUIRED", "application required");
    if (!value) throw codedError("NOTE_REQUIRED", "note required");
    const result = await client.request(`/api/v1/candidates/${candidateId}/notes`, { method: "POST", body: { application_id: applicationId, body: value }, ...signalOption(signal) });
    return result?.data;
  }

  async function previewResume(resumeId, { signal } = {}) {
    const result = await client.request(`/api/v1/resumes/${resumeId}/preview`, signalOption(signal));
    return result?.data;
  }

  async function getResumeFile(resumeId, { signal } = {}) {
    return client.download(`/api/v1/resumes/${resumeId}/file`, signalOption(signal));
  }

  async function downloadResume(resumeId, { signal } = {}) {
    const ticket = await client.request(`/api/v1/resumes/${resumeId}/download-tickets`, { method: "POST", ...signalOption(signal) });
    return client.download("/api/v1/download-tickets/consume", { method: "POST", body: { token: ticket?.data?.token }, ...signalOption(signal) });
  }

  return { listCandidates, listJobs, loadReview, workflowAction, addNote, previewResume, getResumeFile, downloadResume };
}

export const candidateController = createCandidateController();
