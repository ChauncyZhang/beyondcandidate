import { apiClient } from "./apiClient.js";

const STAGES = {
  new: "新简历",
  review: "待复核",
  contact: "待沟通",
  interview_pending: "待安排",
  interviewing: "面试中",
  decision: "待决策",
  passed: "已通过",
};

function safeString(value, fallback = "") {
  return typeof value === "string" ? value : fallback;
}

function safeArray(value) {
  return Array.isArray(value) ? value : [];
}

function safeCount(value) {
  return Number.isInteger(value) && value >= 0 ? value : 0;
}

function invalidResponse() {
  const error = new Error("Workbench response is incomplete");
  error.code = "WORKBENCH_INVALID_RESPONSE";
  return error;
}

function isObject(value) {
  return value !== null && typeof value === "object" && !Array.isArray(value);
}

function isNonEmptyString(value) {
  return typeof value === "string" && value.trim().length > 0;
}

function isNullableString(value) {
  return value === null || typeof value === "string";
}

function isDateTime(value) {
  return typeof value === "string" && !Number.isNaN(new Date(value).getTime());
}

function isCount(value) {
  return Number.isInteger(value) && value >= 0;
}

function isCandidate(item, expectedStage) {
  return isObject(item)
    && isNonEmptyString(item.application_id)
    && isNonEmptyString(item.candidate_id)
    && isNonEmptyString(item.job_id)
    && isNonEmptyString(item.display_name)
    && Object.hasOwn(item, "current_title")
    && isNullableString(item.current_title)
    && Object.hasOwn(item, "location")
    && isNullableString(item.location)
    && isNonEmptyString(item.source)
    && item.stage === expectedStage
    && isDateTime(item.updated_at);
}

function isStageGroup(group, stage, allowedJobIds) {
  return isObject(group)
    && isCount(group.count)
    && Array.isArray(group.items)
    && group.items.length <= 5
    && group.items.length <= group.count
    && group.items.every((item) => isCandidate(item, stage) && allowedJobIds.has(item.job_id));
}

function reviewCandidateLink(item) {
  return `/candidates/${item.candidate_id}?tab=evidence&application=${item.application_id}&job=${item.job_id}`;
}

function isReviewTask(item) {
  return isCandidate(item, "review")
    && isNonEmptyString(item.task_id)
    && (item.ai_status === "succeeded" || item.ai_status === "failed")
    && typeof item.config_warning === "boolean"
    && item.candidate_link === reviewCandidateLink(item);
}

function isReviewTaskGroup(group) {
  return isObject(group)
    && isCount(group.count)
    && Array.isArray(group.items)
    && group.items.length <= 5
    && group.items.length <= group.count
    && group.items.every(isReviewTask);
}

function isNotification(item, expectedStage, review = false) {
  return (review ? isReviewTask(item) : isCandidate(item, expectedStage))
    && typeof item.notification_version === "string"
    && /^[0-9a-f]{64}$/.test(item.notification_version);
}

function isNotificationGroup(group, stage, { review = false } = {}) {
  return isObject(group)
    && isCount(group.count)
    && Array.isArray(group.items)
    && group.items.length <= 5
    && group.items.length <= group.count
    && group.items.every((item) => isNotification(item, stage, review));
}

function isJob(job) {
  if (
    !isObject(job)
    || !isNonEmptyString(job.id)
    || !isNonEmptyString(job.title)
    || !Object.hasOwn(job, "department_name")
    || !isNullableString(job.department_name)
    || job.status !== "open"
    || !isDateTime(job.updated_at)
    || !isCount(job.active_count)
    || !isObject(job.stages)
  ) return false;
  const jobScope = new Set([job.id]);
  if (!Object.keys(STAGES).every((stage) => isStageGroup(job.stages[stage], stage, jobScope))) return false;
  return Object.keys(STAGES).reduce((total, stage) => total + job.stages[stage].count, 0) === job.active_count;
}

function validateEnvelope(response) {
  const payload = response?.data;
  const taskGroups = payload?.tasks;
  const notifications = payload?.notifications;
  const interviews = payload?.interviews;
  if (
    !isObject(payload)
    || !isDateTime(payload.generated_at)
    || !Array.isArray(payload.jobs)
    || payload.jobs.length > 20
    || !payload.jobs.every(isJob)
    || !isObject(taskGroups)
    || !isObject(notifications)
    || !isObject(interviews)
    || interviews.available !== false
    || !Array.isArray(interviews.upcoming)
    || interviews.upcoming.length !== 0
    || !Array.isArray(interviews.pending_feedback)
    || interviews.pending_feedback.length !== 0
  ) throw invalidResponse();
  const jobIds = new Set(payload.jobs.map((job) => job.id));
  const taskStages = ["interview_pending", "decision", "passed"];
  if (
    !isReviewTaskGroup(taskGroups.review)
    || !taskStages.every((stage) => (
    isStageGroup(taskGroups[stage], stage, jobIds)
    && taskGroups[stage].count === payload.jobs.reduce((total, job) => total + job.stages[stage].count, 0)
    ))
    || !isNotificationGroup(notifications.review, "review", { review: true })
    || !isNotificationGroup(notifications.interview_pending, "interview_pending")
    || !isNotificationGroup(notifications.decision, "decision")
    || !isNotificationGroup(notifications.passed, "passed")
  ) throw invalidResponse();
  return payload;
}

function displayDateTime(value) {
  const raw = safeString(value);
  if (!raw) return "未记录";
  const date = new Date(raw);
  if (Number.isNaN(date.getTime())) return "未记录";
  return new Intl.DateTimeFormat("zh-CN", {
    month: "2-digit",
    day: "2-digit",
    hour: "2-digit",
    minute: "2-digit",
    hour12: false,
  }).format(date);
}

function normalizeItem(item, jobNames, { allowUnknownJob = false } = {}) {
  const applicationId = safeString(item?.application_id);
  const candidateId = safeString(item?.candidate_id);
  const jobId = safeString(item?.job_id);
  const stage = safeString(item?.stage);
  if (!applicationId || !candidateId || !jobId || !STAGES[stage] || (!allowUnknownJob && !jobNames.has(jobId))) return null;
  return {
    id: applicationId,
    applicationId,
    candidateId,
    jobId,
    serverBacked: true,
    name: safeString(item?.display_name, "未命名候选人"),
    role: safeString(item?.current_title, "当前职称未填写"),
    company: "",
    position: jobNames.get(jobId) || "职位信息不可用",
    stage: STAGES[stage],
    nextRound: safeString(item?.next_interview_round),
    source: safeString(item?.source, "未记录"),
    city: safeString(item?.location, "地点未填写"),
    lastActivity: displayDateTime(item?.updated_at),
    evidence: {},
  };
}

function normalizeItems(items, jobNames) {
  return safeArray(items).map((item) => normalizeItem(item, jobNames)).filter(Boolean);
}

function normalizeStage(stage, apiStage, jobNames) {
  const items = normalizeItems(stage?.items, jobNames).filter((item) => item.stage === STAGES[apiStage]);
  return { count: safeCount(stage?.count), items };
}

function normalizeTaskGroup(group, apiStage, jobNames) {
  return {
    count: safeCount(group?.count),
    items: normalizeItems(group?.items, jobNames).filter((item) => item.stage === STAGES[apiStage]),
  };
}

function normalizeReviewTaskGroup(group, jobNames) {
  return {
    count: safeCount(group?.count),
    items: safeArray(group?.items).map((item) => {
      const candidate = normalizeItem(item, jobNames, { allowUnknownJob: true });
      if (!candidate) return null;
      return {
        ...candidate,
        taskId: item.task_id,
        aiStatus: item.ai_status,
        aiLabel: item.ai_status === "failed" ? "AI评分不可用" : "",
        configWarning: item.config_warning,
        candidateLink: item.candidate_link,
      };
    }).filter(Boolean),
  };
}

function withNotificationVersion(candidate, item) {
  return candidate ? { ...candidate, notificationVersion: item.notification_version } : null;
}

function normalizeNotificationTaskGroup(group, apiStage, jobNames) {
  return {
    count: safeCount(group?.count),
    items: safeArray(group?.items)
      .map((item) => withNotificationVersion(normalizeItem(item, jobNames), item))
      .filter((item) => item?.stage === STAGES[apiStage]),
  };
}

function normalizeReviewNotificationGroup(group, jobNames) {
  const normalized = normalizeReviewTaskGroup(group, jobNames);
  return {
    count: normalized.count,
    items: normalized.items.map((item, index) => ({
      ...item,
      notificationVersion: group.items[index].notification_version,
    })),
  };
}

function normalizeWorkbench(payload) {
  const rawJobs = safeArray(payload?.jobs).filter((job) => safeString(job?.id) && safeString(job?.title));
  const jobNames = new Map(rawJobs.map((job) => [job.id, job.title]));
  const jobs = rawJobs.map((job) => ({
    id: job.id,
    name: job.title,
    department: safeString(job?.department_name, "部门未设置"),
    updatedAt: safeString(job?.updated_at),
    activeCount: safeCount(job?.active_count),
    stages: Object.fromEntries(Object.entries(STAGES).map(([apiStage, label]) => [
      label,
      normalizeStage(job?.stages?.[apiStage], apiStage, jobNames),
    ])),
  }));
  const tasks = payload?.tasks || {};
  const notifications = payload?.notifications || {};
  const interviews = payload?.interviews || {};
  const interviewsAvailable = interviews.available === true;
  return {
    generatedAt: safeString(payload?.generated_at),
    jobs,
    tasks: {
      review: normalizeReviewTaskGroup(tasks.review, jobNames),
      interviewPending: normalizeTaskGroup(tasks.interview_pending, "interview_pending", jobNames),
      decision: normalizeTaskGroup(tasks.decision, "decision", jobNames),
      passed: normalizeTaskGroup(tasks.passed, "passed", jobNames),
    },
    notifications: {
      review: normalizeReviewNotificationGroup(notifications.review, jobNames),
      interviewPending: normalizeNotificationTaskGroup(notifications.interview_pending, "interview_pending", jobNames),
      decision: normalizeNotificationTaskGroup(notifications.decision, "decision", jobNames),
      passed: normalizeNotificationTaskGroup(notifications.passed, "passed", jobNames),
    },
    interviews: {
      available: interviewsAvailable,
      upcoming: interviewsAvailable ? safeArray(interviews.upcoming) : [],
      pendingFeedback: interviewsAvailable ? safeArray(interviews.pending_feedback) : [],
    },
  };
}

export function createWorkbenchController({ client = apiClient } = {}) {
  return {
    async load({ signal } = {}) {
      const response = await client.request("/api/v1/workbench", signal ? { signal } : {});
      return normalizeWorkbench(validateEnvelope(response));
    },
    async markNotificationRead(candidate) {
      return client.markNotificationRead(candidate.applicationId, candidate.notificationVersion);
    },
  };
}

export const workbenchController = createWorkbenchController();
export default workbenchController;
