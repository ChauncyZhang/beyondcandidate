import { apiClient } from "./apiClient.js";

const API_TO_UI_STATUS = new Map([
  ["draft", "草稿"],
  ["open", "招聘中"],
  ["paused", "已暂停"],
  ["closed", "已关闭"],
  ["archived", "已归档"],
]);

const UI_TO_API_STATUS = new Map([...API_TO_UI_STATUS].map(([api, ui]) => [ui, api]));

const API_TO_UI_PRIORITY = new Map([
  ["high", "高"],
  ["normal", "中"],
  ["low", "低"],
]);

const UI_TO_API_PRIORITY = new Map([...API_TO_UI_PRIORITY].map(([api, ui]) => [ui, api]));

const JOB_TRANSITIONS = new Map([
  ["draft", new Set(["open"])],
  ["open", new Set(["paused", "closed"])],
  ["paused", new Set(["open", "closed"])],
  ["closed", new Set(["archived"])],
  ["archived", new Set()],
]);

const UUID_PATTERN = /^[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/i;

export const JOB_EDIT_CONFLICT_REFRESHED_MESSAGE = "职位已被其他人更新。当前表单内容已保留，已加载最新版本，请核对后再次保存。";
export const JOB_EDIT_CONFLICT_REFRESH_ERROR = "职位已被其他人更新。当前表单内容已保留，但最新版本加载失败，请重试刷新。";

export function getJobFormActions(job) {
  const secondary = { label: "保存草稿", publish: false };
  if (!job) return { secondary, primary: { label: "发布职位", publish: true } };
  if (job.status === "草稿" || job.status === "draft") {
    return { secondary, primary: { label: "保存并发布", publish: true } };
  }
  return { secondary: null, primary: { label: "保存修改", publish: false } };
}

export function getJobSaveSuccessMessage(job, publish) {
  if (publish) return "职位已发布";
  return job ? "职位修改已保存" : "职位已保存为草稿";
}

function safeString(value) {
  return typeof value === "string" ? value : "";
}

function safeArray(value) {
  return Array.isArray(value) ? value : [];
}

function safeCount(value) {
  return typeof value === "number" && Number.isFinite(value) && value >= 0 ? value : 0;
}

function safeVersion(value) {
  return Number.isInteger(value) && value >= 0 ? value : null;
}

function safeUuid(value) {
  const candidate = safeString(value).trim();
  return UUID_PATTERN.test(candidate) ? candidate : "";
}

function codedError(code, message) {
  const error = new Error(message);
  error.code = code;
  return error;
}

function requireJobId(value) {
  const candidate = safeString(value).trim();
  if (!candidate) throw codedError("JOB_ID_REQUIRED", "job id required");
  const id = safeUuid(candidate);
  if (!id) throw codedError("JOB_ID_INVALID", "job id invalid");
  return id;
}

function requestOptions(signal, options = {}) {
  return signal ? { ...options, signal } : options;
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

function normalizeFacets(value) {
  return safeArray(value)
    .map((item) => ({ id: safeString(item?.id).trim(), name: safeString(item?.name).trim() }))
    .filter((item) => item.id && item.name);
}

function normalizeStages(value) {
  return Object.fromEntries(Object.entries(value && typeof value === "object" ? value : {}).map(([stage, count]) => [stage, safeCount(count)]));
}

function normalizeJob(item, funnelOverride) {
  const stages = normalizeStages(funnelOverride?.stages ?? item?.funnel?.stages);
  const total = safeCount(funnelOverride?.total ?? item?.funnel?.total);
  const id = safeUuid(item?.id);
  const recruitingOwnerId = safeUuid(item?.owner_id);
  const recruitingOwnerName = safeString(item?.owner_name).trim();
  const hiringOwnerId = safeUuid(item?.hiring_owner_id);
  const hiringOwnerName = safeString(item?.hiring_owner_name).trim();
  const useHiringOwner = Boolean(hiringOwnerId && hiringOwnerName);
  const useRecruitingOwner = Boolean(recruitingOwnerId && recruitingOwnerName);
  const title = safeString(item?.title);

  return {
    id,
    serverBacked: Boolean(id),
    version: safeVersion(item?.version),
    title,
    name: title,
    departmentId: safeUuid(item?.department_id),
    department: safeString(item?.department_name),
    recruitingOwnerId,
    hiringOwnerId,
    ownerId: useHiringOwner ? hiringOwnerId : useRecruitingOwner ? recruitingOwnerId : "",
    owner: useHiringOwner ? hiringOwnerName : useRecruitingOwner ? recruitingOwnerName : "",
    headcount: safeCount(item?.headcount),
    status: API_TO_UI_STATUS.get(item?.status) || "",
    priority: API_TO_UI_PRIORITY.get(item?.priority) || "",
    updated: displayDateTime(item?.updated_at),
    updatedAt: safeString(item?.updated_at),
    funnel: stages,
    candidates: total,
    review: safeCount(stages.review),
    interview: safeCount(stages.interview_pending) + safeCount(stages.interviewing),
    decision: safeCount(stages.decision),
  };
}

function normalizeDefinition(resource, funnel) {
  const data = resource?.data || {};
  const jd = data.jd;
  const rules = data.rules;
  return {
    ...normalizeJob(data.job, funnel),
    jd: safeString(jd?.description),
    location: safeString(jd?.location),
    process: safeString(jd?.process_template),
    workflowTemplateId: safeUuid(jd?.workflow_template_id),
    llmEnabled: jd?.llm_enabled === true,
    mustHave: safeArray(rules?.must_have).filter((item) => typeof item === "string"),
    niceToHave: safeArray(rules?.nice_to_have).filter((item) => typeof item === "string"),
    jdId: safeString(jd?.id) || null,
    jdVersion: safeVersion(jd?.version_number),
    rulesId: safeString(rules?.id) || null,
    rulesVersion: safeVersion(rules?.version_number),
  };
}

function normalizeStatusCounts(value) {
  return Object.fromEntries([...API_TO_UI_STATUS].map(([api, ui]) => [ui, safeCount(value?.[api])]));
}

function normalizeRules(value) {
  const rules = Array.isArray(value) ? value : safeString(value).split(/[、,，]/);
  return rules.map((item) => safeString(item).trim()).filter(Boolean);
}

function formUuid(values, field, job) {
  const source = Object.prototype.hasOwnProperty.call(values || {}, field) ? values[field] : job?.[field];
  return safeUuid(source) || null;
}

function facetName(facets, id) {
  return safeArray(facets).find((item) => item?.id === id)?.name || "";
}

function mergeDefinition(listRecord, definition, metadata = {}) {
  const merged = listRecord ? { ...definition, ...listRecord } : { ...definition };
  let ownerId = merged.ownerId;
  let owner = merged.owner;
  if (!listRecord) {
    const hiringOwnerId = definition?.hiringOwnerId || "";
    const recruitingOwnerId = definition?.recruitingOwnerId || (!hiringOwnerId ? definition?.ownerId : "");
    const hiringOwnerName = facetName(metadata.owners, hiringOwnerId);
    const recruitingOwnerName = facetName(metadata.owners, recruitingOwnerId)
      || (definition?.ownerId === recruitingOwnerId ? definition?.owner : "");
    if (hiringOwnerId && hiringOwnerName) {
      ownerId = hiringOwnerId;
      owner = hiringOwnerName;
    } else if (recruitingOwnerId && recruitingOwnerName) {
      ownerId = recruitingOwnerId;
      owner = recruitingOwnerName;
    } else {
      ownerId = "";
      owner = "";
    }
  }
  return {
    ...merged,
    department: merged.department || facetName(metadata.departments, merged.departmentId),
    ownerId,
    owner,
  };
}

function definitionCommand(values, job, publish) {
  const departmentId = formUuid(values, "departmentId", job);
  const ownerId = Object.prototype.hasOwnProperty.call(values || {}, "ownerId")
    ? safeUuid(values.ownerId) || null
    : safeUuid(job?.hiringOwnerId) || safeUuid(job?.ownerId) || null;
  const priorityValue = safeString(values?.priority).trim();
  const priority = UI_TO_API_PRIORITY.get(priorityValue) || (API_TO_UI_PRIORITY.has(priorityValue) ? priorityValue : "");
  if (!priority) throw codedError("JOB_PRIORITY_UNSUPPORTED", "job priority unsupported");
  return {
    title: safeString(values?.name).trim(),
    department_id: departmentId,
    headcount: Number.isInteger(values?.headcount) ? values.headcount : 1,
    priority,
    hiring_owner_id: ownerId,
    description: safeString(values?.jd).trim(),
    location: safeString(values?.location).trim(),
    process_template: safeString(values?.process).trim(),
    workflow_template_id: safeUuid(values?.workflowTemplateId) || null,
    llm_enabled: values?.llmEnabled === true,
    must_have: normalizeRules(values?.mustHave),
    nice_to_have: normalizeRules(values?.niceToHave),
    publish: publish === true,
  };
}

export function createJobController({ client = apiClient, idempotencyKey = () => globalThis.crypto.randomUUID() } = {}) {
  async function listDepartments({ signal } = {}) {
    return safeArray(await client.listDepartments({ signal }))
      .map((item) => ({
        id: safeString(item?.id).trim(),
        name: safeString(item?.name).trim(),
        status: item?.status === "inactive" ? "inactive" : "active",
      }))
      .filter((item) => item.id && item.name);
  }

  async function listHiringManagers({ signal } = {}) {
    const result = await client.request("/api/v1/job-owner-options", requestOptions(signal));
    return normalizeFacets(result?.data).filter((item) => safeUuid(item.id));
  }

  async function listJobs(filters = {}, { signal } = {}) {
    const params = new URLSearchParams();
    const query = safeString(filters.q).trim();
    const statusValue = safeString(filters.status).trim();
    const status = UI_TO_API_STATUS.get(statusValue) || (API_TO_UI_STATUS.has(statusValue) ? statusValue : "");
    const departmentId = safeUuid(filters.departmentId);
    const ownerId = safeUuid(filters.ownerId);
    const cursor = safeString(filters.cursor).trim();
    const limit = Number(filters.limit);
    if (query) params.set("q", query);
    if (status) params.set("status", status);
    if (departmentId) params.set("department_id", departmentId);
    if (ownerId) params.set("owner_id", ownerId);
    if (cursor) params.set("cursor", cursor);
    if (Number.isInteger(limit) && limit >= 1 && limit <= 100) params.set("limit", String(limit));
    const queryString = params.toString();
    const result = await client.request(`/api/v1/jobs${queryString ? `?${queryString}` : ""}`, requestOptions(signal));
    return {
      records: safeArray(result?.data).map((item) => normalizeJob(item)),
      nextCursor: safeString(result?.meta?.next_cursor) || null,
      departments: normalizeFacets(result?.meta?.departments),
      owners: normalizeFacets(result?.meta?.owners),
      statusCounts: normalizeStatusCounts(result?.meta?.status_counts),
    };
  }

  async function loadDefinition(jobId, { signal } = {}) {
    const id = requireJobId(jobId);
    const options = requestOptions(signal);
    const [definition, funnel] = await Promise.all([
      client.request(`/api/v1/job-definitions/${id}`, options),
      client.request(`/api/v1/jobs/${id}/funnel`, options),
    ]);
    return normalizeDefinition(definition, funnel?.data);
  }

  async function saveDefinition(values, { job = null, publish = false, signal } = {}) {
    const existing = job !== null && job !== undefined;
    const id = existing ? requireJobId(job?.id) : "";
    const version = safeVersion(job?.version);
    if (existing && version === null) throw codedError("JOB_VERSION_REQUIRED", "job version required");
    const options = requestOptions(signal, {
      method: existing ? "PUT" : "POST",
      body: definitionCommand(values, job, publish),
      ...(existing ? { ifMatch: `"${version}"` } : {}),
      idempotencyKey: idempotencyKey(),
    });
    const result = await client.request(existing ? `/api/v1/job-definitions/${id}` : "/api/v1/job-definitions", options);
    return normalizeDefinition(result);
  }

  async function refreshEditBaseline(job, values, { metadata = {}, signal } = {}) {
    try {
      const id = requireJobId(job?.id);
      const resource = await client.request(`/api/v1/job-definitions/${id}`, requestOptions(signal));
      const latest = normalizeDefinition(resource);
      return {
        job: { ...mergeDefinition(null, latest, metadata), formMode: job?.formMode },
        values,
        error: "",
        retryable: false,
      };
    } catch (error) {
      if (error?.name === "AbortError") throw error;
      return { job, values, error: JOB_EDIT_CONFLICT_REFRESH_ERROR, retryable: true };
    }
  }

  async function transition(job, targetUiStatus, { signal } = {}) {
    const id = requireJobId(job?.id);
    const version = safeVersion(job?.version);
    if (version === null) throw codedError("JOB_VERSION_REQUIRED", "job version required");
    const sourceValue = safeString(job?.status).trim();
    const source = UI_TO_API_STATUS.get(sourceValue) || (API_TO_UI_STATUS.has(sourceValue) ? sourceValue : "");
    const target = UI_TO_API_STATUS.get(safeString(targetUiStatus).trim()) || "";
    if (!source || !target || !JOB_TRANSITIONS.get(source)?.has(target)) {
      throw codedError("JOB_TRANSITION_UNSUPPORTED", "job transition unsupported");
    }
    const result = await client.request(`/api/v1/jobs/${id}/transitions`, requestOptions(signal, {
      method: "POST",
      body: { target },
      ifMatch: `"${version}"`,
      idempotencyKey: idempotencyKey(),
    }));
    return normalizeJob(result?.data);
  }

  return { listDepartments, listHiringManagers, listJobs, loadDefinition, saveDefinition, refreshEditBaseline, transition, mergeDefinition };
}

export const jobController = createJobController();
export default jobController;
