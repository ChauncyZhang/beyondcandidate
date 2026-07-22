import { apiClient } from "./apiClient.js";

const API_TO_UI_METHOD = { video: "视频面试", onsite: "现场面试", phone: "电话面试" };
const UI_TO_API_METHOD = Object.fromEntries(Object.entries(API_TO_UI_METHOD).map(([api, ui]) => [ui, api]));
const API_TO_UI_STATUS = {
  draft: "待确认",
  scheduled: "已安排",
  rescheduled: "已安排",
  confirmed: "已确认",
  completed: "已完成",
  pending_feedback: "已完成",
  feedback_completed: "已完成",
  cancelled: "已取消",
  no_show: "未到场",
};
const API_TO_UI_NOTIFICATION = { sent: "已发送", failed: "发送失败", not_sent: "待发送" };
const UI_TO_API_RATING = { "需提升": 1, "一般": 2, "良好": 3, "优秀": 4 };
const API_TO_UI_RATING = { 1: "需提升", 2: "一般", 3: "良好", 4: "优秀" };
const UI_TO_API_CONCLUSION = { "强烈推荐": "strong_recommend", "推荐": "recommend", "保留": "hold", "待补充评估": "hold", "不推荐": "no_hire" };
const API_TO_UI_CONCLUSION = Object.fromEntries(Object.entries(UI_TO_API_CONCLUSION).map(([ui, api]) => [api, ui]));
const UI_RATING_KEYS = {
  professional: "professional_ability",
  problem: "problem_solving",
  communication: "communication",
  fit: "role_fit",
};

function safeString(value, fallback = "") {
  return typeof value === "string" ? value : fallback;
}

function safeArray(value) {
  return Array.isArray(value) ? value : [];
}

function codedError(code, message) {
  const error = new Error(message);
  error.code = code;
  return error;
}

function requireId(value, code) {
  const id = safeString(value).trim();
  if (!id) throw codedError(code, "resource id required");
  return id;
}

function requireVersion(value, code) {
  if (!Number.isInteger(value) || value < 0) throw codedError(code, "resource version required");
  return value;
}

function signalOption(signal) {
  return signal ? { signal } : {};
}

function quotedVersion(version) {
  return `"${version}"`;
}

function dateParts(value, timezone) {
  const raw = safeString(value);
  const instant = new Date(raw);
  if (!raw || Number.isNaN(instant.getTime())) return { date: "", time: "" };
  try {
    const parts = Object.fromEntries(new Intl.DateTimeFormat("en-CA", {
      timeZone: safeString(timezone, "Asia/Shanghai"),
      year: "numeric",
      month: "2-digit",
      day: "2-digit",
      hour: "2-digit",
      minute: "2-digit",
      hourCycle: "h23",
    }).formatToParts(instant).map((part) => [part.type, part.value]));
    return { date: `${parts.year}-${parts.month}-${parts.day}`, time: `${parts.hour}:${parts.minute}` };
  } catch {
    return { date: raw.slice(0, 10), time: raw.slice(11, 16) };
  }
}

function displayDate(date) {
  return /^\d{4}-\d{2}-\d{2}$/.test(date) ? date.slice(5) : date || "未记录";
}

function durationMinutes(startsAt, endsAt) {
  const start = new Date(startsAt).getTime();
  const end = new Date(endsAt).getTime();
  return Number.isFinite(start) && Number.isFinite(end) && end > start ? Math.round((end - start) / 60000) : 0;
}

function feedbackStatus(status) {
  if (status === "pending_feedback") return "待反馈";
  if (status === "feedback_completed") return "已提交";
  return "未开始";
}

function normalizeParticipant(participant) {
  const id = safeString(participant?.user_id);
  if (!id) return null;
  return {
    id,
    name: safeString(participant?.display_name, "未命名面试官"),
    role: safeString(participant?.role, "interviewer"),
    requiredFeedback: participant?.required_feedback !== false,
  };
}

export function normalizeInterview(value) {
  const id = safeString(value?.id);
  if (!id) return null;
  const participants = safeArray(value?.participants).map(normalizeParticipant).filter(Boolean);
  const timezone = safeString(value?.timezone, "Asia/Shanghai");
  const { date, time } = dateParts(value?.starts_at, timezone);
  const candidateTitle = safeString(value?.candidate?.current_title);
  return {
    id,
    serverBacked: true,
    applicationId: safeString(value?.application_id),
    candidateId: safeString(value?.candidate?.id),
    candidate: safeString(value?.candidate?.display_name, "未命名候选人"),
    role: candidateTitle || "当前职称未填写",
    jobId: safeString(value?.job?.id),
    position: safeString(value?.job?.title, "职位未记录"),
    round: safeString(value?.round_name, "面试"),
    date,
    dateLabel: displayDate(date),
    time,
    duration: durationMinutes(value?.starts_at, value?.ends_at),
    method: API_TO_UI_METHOD[value?.method] || "面试",
    timezone,
    startsAt: safeString(value?.starts_at),
    endsAt: safeString(value?.ends_at),
    interviewerIds: participants.map((item) => item.id),
    interviewers: participants.map((item) => item.name),
    participants,
    location: safeString(value?.meeting_url) || safeString(value?.location, "未填写"),
    status: API_TO_UI_STATUS[value?.status] || "未知状态",
    notification: API_TO_UI_NOTIFICATION[value?.notification_status] || "待发送",
    feedbackStatus: feedbackStatus(value?.status),
    owner: participants[0]?.name || "未分配",
    version: Number.isInteger(value?.version) ? value.version : null,
    calendarSequence: Number.isInteger(value?.calendar_sequence) ? value.calendar_sequence : 0,
    jdPriorities: [],
    suggestedQuestions: [],
    summary: candidateTitle || "候选人信息以服务端档案为准。",
    history: [],
    feedback: null,
  };
}

export function deriveCandidateInterviews(candidateId, records) {
  return safeArray(records)
    .filter((record) => record?.serverBacked === true && record.candidateId === candidateId)
    .map((record) => ({
      interviewId: record.id,
      round: record.round,
      time: `${record.date} ${record.time}`.trim(),
      interviewer: safeArray(record.interviewers).join("、") || "未分配",
      result: record.feedback?.conclusion || record.feedbackStatus || record.status,
      feedback: record.feedback?.strengths || "暂无已提交反馈",
    }));
}

export function selectSchedulableCandidates(records) {
  const applicationIds = new Set();
  return safeArray(records).filter((record) => {
    const applicationId = safeString(record?.applicationId);
    if (record?.serverBacked !== true || record?.stage !== "待安排" || !applicationId || applicationIds.has(applicationId)) {
      return false;
    }
    applicationIds.add(applicationId);
    return true;
  });
}

function timezoneOffset(timezone) {
  return timezone === "Asia/Shanghai" || timezone === "Asia/Singapore" ? "+08:00" : "Z";
}

function timestamp(date, time, timezone) {
  return `${date}T${time}:00${timezoneOffset(timezone)}`;
}

function addMinutes(value, minutes) {
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) throw codedError("INTERVIEW_TIME_INVALID", "interview time invalid");
  return new Date(date.getTime() + minutes * 60000).toISOString().replace(".000Z", "Z");
}

function scheduleTimes(form) {
  const startsAt = timestamp(safeString(form?.date), safeString(form?.time), safeString(form?.timezone, "Asia/Shanghai"));
  const duration = Number(form?.duration);
  const endsAtUtc = addMinutes(startsAt, Number.isFinite(duration) && duration > 0 ? duration : 0);
  const offset = timezoneOffset(form?.timezone);
  if (offset === "Z") return { startsAt, endsAt: endsAtUtc };
  const end = new Date(endsAtUtc);
  const localEnd = new Date(end.getTime() + Number(offset.slice(1, 3)) * 60 * 60000);
  return { startsAt, endsAt: localEnd.toISOString().slice(0, 19) + offset };
}

function participantBody(participants) {
  return safeArray(participants).map((item) => ({
    user_id: requireId(item?.id, "INTERVIEW_PARTICIPANT_ID_REQUIRED"),
    role: safeString(item?.role, "interviewer"),
    required_feedback: item?.requiredFeedback !== false,
  }));
}

function interviewBody(form, includeApplication) {
  const { startsAt, endsAt } = scheduleTimes(form);
  const method = UI_TO_API_METHOD[form?.method] || safeString(form?.method);
  const location = safeString(form?.location).trim() || null;
  return {
    ...(includeApplication ? { application_id: requireId(form?.applicationId, "APPLICATION_ID_REQUIRED") } : {}),
    round_name: safeString(form?.round).trim(),
    method,
    timezone: safeString(form?.timezone, "Asia/Shanghai"),
    starts_at: startsAt,
    ends_at: endsAt,
    location: method === "video" ? null : location,
    meeting_url: method === "video" ? location : null,
    participants: participantBody(form?.participants),
    allow_soft_conflict: form?.allowSoftConflict === true,
  };
}

function conflictBody(form) {
  const { startsAt, endsAt } = scheduleTimes(form);
  return {
    starts_at: startsAt,
    ends_at: endsAt,
    participant_ids: safeArray(form?.participantIds).map((id) => requireId(id, "INTERVIEW_PARTICIPANT_ID_REQUIRED")),
    buffer_minutes: Number.isInteger(form?.bufferMinutes) ? form.bufferMinutes : 15,
  };
}

function normalizeFeedback(value) {
  const ratings = value?.ratings || {};
  return {
    id: safeString(value?.id),
    interviewId: safeString(value?.interview_id),
    authorId: safeString(value?.author_id),
    status: safeString(value?.status, "draft"),
    ratings: Object.fromEntries(Object.entries(UI_RATING_KEYS).map(([ui, api]) => [ui, API_TO_UI_RATING[ratings[api]] || "待评价"])),
    strengths: safeString(value?.strengths),
    risks: safeString(value?.risks),
    conclusion: API_TO_UI_CONCLUSION[value?.conclusion] || "",
    notes: safeString(value?.notes),
    version: Number.isInteger(value?.version) ? value.version : 0,
    submittedAt: safeString(value?.submitted_at),
    author: value?.author ? {
      id: safeString(value.author.id),
      name: safeString(value.author.display_name, "未命名面试官"),
    } : null,
  };
}

function feedbackBody(form) {
  return {
    ratings: Object.fromEntries(Object.entries(UI_RATING_KEYS).map(([ui, api]) => [api, UI_TO_API_RATING[form?.ratings?.[ui]]]).filter(([, score]) => score)),
    strengths: safeString(form?.strengths).trim() || null,
    risks: safeString(form?.risks).trim() || null,
    conclusion: UI_TO_API_CONCLUSION[form?.conclusion] || null,
    notes: safeString(form?.notes).trim() || null,
  };
}

function normalizeTask(value) {
  return {
    id: safeString(value?.id),
    type: safeString(value?.type),
    interviewId: safeString(value?.interview_id),
    applicationId: safeString(value?.application_id),
    candidateId: safeString(value?.candidate?.id),
    candidate: safeString(value?.candidate?.display_name, "未命名候选人"),
    jobId: safeString(value?.job?.id),
    position: safeString(value?.job?.title, "职位未记录"),
    round: safeString(value?.round_name, "面试"),
    startsAt: safeString(value?.starts_at),
    status: safeString(value?.status),
  };
}

function normalizeParticipantOption(value) {
  const id = safeString(value?.id);
  const name = safeString(value?.display_name);
  if (!id || !name) return null;
  return { id, name, roles: safeArray(value?.roles).filter((role) => typeof role === "string") };
}

function normalizeMaterials(value) {
  const focus = value?.interview_focus || value?.screening || {};
  return {
    interviewId: safeString(value?.interview_id),
    candidate: {
      id: safeString(value?.candidate?.id),
      name: safeString(value?.candidate?.display_name, "未命名候选人"),
      currentTitle: safeString(value?.candidate?.current_title, "当前职称未填写"),
    },
    job: { id: safeString(value?.job?.id), title: safeString(value?.job?.title, "职位未记录") },
    jd: value?.jd ? {
      id: safeString(value.jd.id),
      version: Number.isInteger(value.jd.version_number) ? value.jd.version_number : null,
      description: safeString(value.jd.description),
    } : null,
    resume: value?.resume ? {
      id: safeString(value.resume.id),
      version: Number.isInteger(value.resume.version_number) ? value.resume.version_number : null,
      previewText: safeString(value.resume.preview_text),
    } : null,
    interviewFocus: {
      requiredMissing: safeArray(focus.required_missing).filter((item) => typeof item === "string"),
      risks: safeArray(focus.risks).filter((item) => typeof item === "string"),
      suggestedQuestions: safeArray(focus.suggested_questions || focus.questions).filter((item) => typeof item === "string"),
    },
  };
}

export function createInterviewController({ client = apiClient, idempotencyKey = () => crypto.randomUUID() } = {}) {
  const pendingFeedbackSubmissions = new Map();

  async function list(filters = {}, { signal } = {}) {
    const params = new URLSearchParams();
    if (safeString(filters.from)) params.set("from", filters.from);
    if (safeString(filters.to)) params.set("to", filters.to);
    if (safeString(filters.interviewerId)) params.set("interviewer_id", filters.interviewerId);
    if (safeString(filters.status)) params.set("status", filters.status);
    if (safeString(filters.cursor)) params.set("cursor", filters.cursor);
    if (Number.isInteger(filters.limit) && filters.limit > 0) params.set("limit", String(filters.limit));
    const response = await client.request(`/api/v1/interviews${params.size ? `?${params}` : ""}`, signalOption(signal));
    const records = safeArray(response?.data).map(normalizeInterview).filter(Boolean);
    return {
      records,
      count: Number.isInteger(response?.meta?.count) ? response.meta.count : records.length,
      nextCursor: safeString(response?.meta?.next_cursor) || null,
    };
  }

  function rangeTimestamp(date, end = false, timezone = "Asia/Shanghai") {
    return `${date}T${end ? "23:59:59" : "00:00:00"}${timezoneOffset(timezone)}`;
  }

  async function listRange(filters = {}, { signal } = {}) {
    const records = [];
    let cursor = "";
    do {
      const page = await list({
        ...filters,
        from: rangeTimestamp(filters.from, false, filters.timezone),
        to: rangeTimestamp(filters.to, true, filters.timezone),
        cursor: cursor || undefined,
        limit: Number.isInteger(filters.limit) ? filters.limit : 100,
      }, { signal });
      records.push(...page.records);
      cursor = page.nextCursor || "";
    } while (cursor);
    return records;
  }

  async function availability(filters = {}, { signal } = {}) {
    const params = new URLSearchParams();
    params.set("from", rangeTimestamp(filters.from, false, filters.timezone));
    params.set("to", rangeTimestamp(filters.to, true, filters.timezone));
    safeArray(filters.participantIds).forEach((id) => params.append("participant_ids", requireId(id, "INTERVIEW_PARTICIPANT_ID_REQUIRED")));
    params.set("timezone", safeString(filters.timezone, "Asia/Shanghai"));
    params.set("buffer", String(Number.isInteger(filters.buffer) ? filters.buffer : 15));
    if (safeString(filters.exclude)) params.set("exclude", filters.exclude);
    const response = await client.request(`/api/v1/interview-availability?${params}`, signalOption(signal));
    return {
      participants: safeArray(response?.data?.participants).map((participant) => ({
        participantId: safeString(participant?.participant_id),
        status: participant?.status === "confirmed" ? "confirmed" : "unknown",
        busy: safeArray(participant?.busy).map((range) => ({ startsAt: safeString(range?.starts_at), endsAt: safeString(range?.ends_at) })),
      })),
      bufferMinutes: Number.isInteger(response?.data?.buffer_minutes) ? response.data.buffer_minutes : 15,
    };
  }

  async function get(interviewId, { signal } = {}) {
    const id = requireId(interviewId, "INTERVIEW_ID_REQUIRED");
    const response = await client.request(`/api/v1/interviews/${id}`, signalOption(signal));
    return normalizeInterview(response?.data);
  }

  async function save(record, form, { signal } = {}) {
    if (!record) {
      const response = await client.request("/api/v1/interviews", {
        method: "POST", body: interviewBody(form, true), idempotencyKey: idempotencyKey(), ...signalOption(signal),
      });
      return normalizeInterview(response?.data);
    }
    const id = requireId(record.id, "INTERVIEW_ID_REQUIRED");
    const version = requireVersion(record.version, "INTERVIEW_VERSION_REQUIRED");
    const response = await client.request(`/api/v1/interviews/${id}`, {
      method: "PATCH", body: interviewBody(form, false), ifMatch: quotedVersion(version), idempotencyKey: idempotencyKey(), ...signalOption(signal),
    });
    return normalizeInterview(response?.data);
  }

  async function checkConflicts(interviewId, form, { signal } = {}) {
    const id = safeString(interviewId).trim();
    const body = conflictBody(form);
    const path = id
      ? `/api/v1/interviews/${id}/conflicts`
      : "/api/v1/interview-conflicts";
    const response = await client.request(path, {
      method: "POST",
      body: id ? body : { application_id: requireId(form?.applicationId, "APPLICATION_ID_REQUIRED"), ...body },
      ...signalOption(signal),
    });
    return {
      hard: safeArray(response?.data?.hard),
      soft: safeArray(response?.data?.soft),
      calendarHard: safeArray(response?.data?.calendar_hard),
      calendarSoft: safeArray(response?.data?.calendar_soft),
      unconfirmed: safeArray(response?.data?.unconfirmed),
    };
  }

  async function transition(record, target, { reason = null, signal } = {}) {
    const id = requireId(record?.id, "INTERVIEW_ID_REQUIRED");
    const version = requireVersion(record?.version, "INTERVIEW_VERSION_REQUIRED");
    const response = await client.request(`/api/v1/interviews/${id}/transitions`, {
      method: "POST", body: { target, reason }, ifMatch: quotedVersion(version), idempotencyKey: idempotencyKey(), ...signalOption(signal),
    });
    return normalizeInterview(response?.data);
  }

  async function downloadCalendar(interviewId, { signal } = {}) {
    const id = requireId(interviewId, "INTERVIEW_ID_REQUIRED");
    return client.download(`/api/v1/interviews/${id}/calendar-file`, signalOption(signal));
  }

  async function getResumeFile(interviewId, { signal } = {}) {
    const id = requireId(interviewId, "INTERVIEW_ID_REQUIRED");
    return client.download(`/api/v1/interviews/${id}/resume-file`, signalOption(signal));
  }

  async function downloadResumeFile(interviewId, { signal } = {}) {
    const id = requireId(interviewId, "INTERVIEW_ID_REQUIRED");
    return client.download(`/api/v1/interviews/${id}/resume-file?download=true`, signalOption(signal));
  }

  async function getMyFeedback(interviewId, { signal } = {}) {
    const id = requireId(interviewId, "INTERVIEW_ID_REQUIRED");
    const response = await client.request(`/api/v1/interviews/${id}/my-feedback`, signalOption(signal));
    return normalizeFeedback(response?.data);
  }

  async function saveMyFeedback(interviewId, form, version, { signal } = {}) {
    const id = requireId(interviewId, "INTERVIEW_ID_REQUIRED");
    const expected = requireVersion(version, "FEEDBACK_VERSION_REQUIRED");
    const response = await client.request(`/api/v1/interviews/${id}/my-feedback`, {
      method: "PUT", body: feedbackBody(form), ifMatch: quotedVersion(expected), ...signalOption(signal),
    });
    return normalizeFeedback(response?.data);
  }

  async function submitMyFeedback(interviewId, { signal } = {}) {
    const id = requireId(interviewId, "INTERVIEW_ID_REQUIRED");
    const key = pendingFeedbackSubmissions.get(id) || idempotencyKey();
    pendingFeedbackSubmissions.set(id, key);
    try {
      const response = await client.request(`/api/v1/interviews/${id}/my-feedback/submit`, {
        method: "POST", idempotencyKey: key, ...signalOption(signal),
      });
      pendingFeedbackSubmissions.delete(id);
      return normalizeFeedback(response?.data);
    } catch (error) {
      const status = Number(error?.status);
      const unavailableTransport = status === 0 && error?.kind === "unavailable";
      const ambiguous = unavailableTransport || !Number.isFinite(status) || status === 408 || status === 429 || status >= 500;
      if (!ambiguous) pendingFeedbackSubmissions.delete(id);
      throw error;
    }
  }

  async function amendFeedback(feedback, form, reason, { signal } = {}) {
    const id = requireId(feedback?.id, "FEEDBACK_ID_REQUIRED");
    const version = requireVersion(feedback?.version, "FEEDBACK_VERSION_REQUIRED");
    const response = await client.request(`/api/v1/interview-feedback/${id}/amendments`, {
      method: "POST", body: { ...feedbackBody(form), reason: safeString(reason).trim() }, ifMatch: quotedVersion(version), ...signalOption(signal),
    });
    return normalizeFeedback(response?.data);
  }

  async function listMyTasks({ signal } = {}) {
    const response = await client.request("/api/v1/me/tasks", signalOption(signal));
    return safeArray(response?.data).map(normalizeTask).filter((item) => item.interviewId);
  }

  async function listParticipantOptions(applicationId, { signal } = {}) {
    const id = requireId(applicationId, "APPLICATION_ID_REQUIRED");
    const response = await client.request(`/api/v1/applications/${id}/interview-participant-options`, signalOption(signal));
    return safeArray(response?.data).map(normalizeParticipantOption).filter(Boolean);
  }

  async function listFeedbacks(interviewId, { signal } = {}) {
    const id = requireId(interviewId, "INTERVIEW_ID_REQUIRED");
    const response = await client.request(`/api/v1/interviews/${id}/feedbacks`, signalOption(signal));
    return safeArray(response?.data).map(normalizeFeedback).filter((feedback) => feedback.id);
  }

  async function getMaterials(interviewId, { signal } = {}) {
    const id = requireId(interviewId, "INTERVIEW_ID_REQUIRED");
    const response = await client.request(`/api/v1/interviews/${id}/materials`, signalOption(signal));
    return normalizeMaterials(response?.data);
  }

  return { list, listRange, availability, get, save, checkConflicts, transition, downloadCalendar, getResumeFile, downloadResumeFile, getMyFeedback, saveMyFeedback, submitMyFeedback, amendFeedback, listMyTasks, listParticipantOptions, listFeedbacks, getMaterials };
}

export const interviewController = createInterviewController();
export default interviewController;
