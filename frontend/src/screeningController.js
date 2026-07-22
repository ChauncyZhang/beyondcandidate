import { apiClient as defaultApiClient } from "./apiClient.js";

const TERMINAL_RUN_STATUSES = new Set(["complete", "partial", "failed", "cancelled"]);
const SUCCESSFUL_LLM_STATUSES = new Set(["succeeded", "skipped", "not_requested"]);
const DIMENSION_LABELS = {
  core_capability: "核心能力",
  experience_depth: "经验深度",
  role_seniority: "职级匹配",
  transferability: "能力迁移",
  explicit_constraints: "明确约束",
};

function isRecord(value) {
  return value !== null && typeof value === "object" && !Array.isArray(value);
}

function safeString(value) {
  return typeof value === "string" ? value : "";
}

function safeCount(value) {
  return Number.isInteger(value) && value >= 0 ? value : 0;
}

function safeScore(value) {
  return Number.isFinite(value) ? value : null;
}

function safeStrings(value) {
  return Array.isArray(value) ? value.filter((entry) => typeof entry === "string") : [];
}

function isFile(value) {
  return Object.prototype.toString.call(value) === "[object File]"
    && typeof value?.name === "string"
    && typeof value?.arrayBuffer === "function";
}

function normalizeRunStatus(status) {
  if (status === "completed") return "complete";
  if (["partial", "failed", "cancelled"].includes(status)) return status;
  return "running";
}

function sourceLabel(source) {
  return source === "upload" ? "本地上传" : source === "manual" ? "手动创建" : safeString(source);
}

export function normalizeScreeningRunSummary(run) {
  const safeRun = isRecord(run) ? run : {};
  return {
    id: safeString(safeRun.id),
    jobId: safeString(safeRun.job_id),
    position: safeString(safeRun.job_title) || "职位信息不可用",
    creator: safeString(safeRun.created_by_name) || "发起人不可用",
    source: sourceLabel(safeRun.source),
    status: normalizeRunStatus(safeRun.status),
    completed: safeCount(safeRun.processed_count),
    total: safeCount(safeRun.total_count),
    succeeded: safeCount(safeRun.succeeded_count),
    failed: safeCount(safeRun.failed_count),
    managerReviewCount: safeCount(safeRun.manager_review_count),
    deferredCount: safeCount(safeRun.deferred_count),
    aiUnavailableCount: safeCount(safeRun.ai_unavailable_count),
    fileFailedCount: safeCount(safeRun.file_failed_count),
    createdAt: safeString(safeRun.created_at),
    serverBacked: true,
  };
}

function routeLabel(routeResult) {
  if (routeResult === "review") return "已转交用人经理";
  if (routeResult === "deferred") return "已暂缓";
  if (routeResult === "ai_unavailable") return "AI评分不可用";
  if (routeResult === "file_failed") return "文件处理失败";
  return "";
}

function normalizeDimension(dimension) {
  if (!isRecord(dimension)) return null;
  const key = safeString(dimension.key);
  return {
    key,
    label: DIMENSION_LABELS[key] ?? "",
    score: safeScore(dimension.score),
    evidence: safeStrings(dimension.evidence),
    gaps: safeStrings(dimension.gaps),
  };
}

function normalizeLlmEvaluation(value) {
  if (!isRecord(value)) return null;
  return {
    dimensions: Array.isArray(value.dimensions)
      ? value.dimensions.slice(0, 5).map(normalizeDimension).filter(Boolean)
      : [],
    evidence: safeStrings(value.evidence),
    gaps: safeStrings(value.gaps),
    strengths: safeStrings(value.strengths),
    risks: safeStrings(value.risks),
  };
}

function normalizeFile(item) {
  const technicalFailure = item?.status === "failed" && Boolean(safeString(item?.error_code));
  const routeResult = technicalFailure ? null : safeString(item?.route_result);
  const llmFailed = item?.llm_status === "failed";
  const llmEvaluation = technicalFailure || llmFailed ? null : normalizeLlmEvaluation(item?.llm_evaluation);
  let status = item?.status === "queued" ? "queued" : "running";

  if (item?.status === "failed") {
    status = "failed";
  } else if (item?.status === "cancelled") {
    status = "cancelled";
  } else if (llmFailed) {
    status = "partial";
  } else if (routeResult && SUCCESSFUL_LLM_STATUSES.has(item?.llm_status)) {
    status = "success";
  }

  return {
    id: safeString(item?.id),
    name: safeString(item?.filename),
    candidateId: safeString(item?.candidate_id) || null,
    applicationId: safeString(item?.application_id) || null,
    candidate: safeString(item?.candidate_name),
    status,
    routeResult,
    routeLabel: technicalFailure ? "未流转" : routeLabel(routeResult),
    score: technicalFailure || llmFailed ? null : safeScore(item?.ai_score),
    recommendation: technicalFailure ? "未进入AI评分" : llmFailed ? "AI评分不可用" : safeString(item?.ai_recommendation),
    llmStatus: safeString(item?.llm_status),
    error: safeString(item?.error_code),
    llmErrorCode: safeString(item?.llm_error_code),
    llmEvaluation,
    dimensions: llmEvaluation?.dimensions ?? [],
    evidence: llmEvaluation?.evidence ?? [],
    gaps: llmEvaluation?.gaps ?? [],
    strengths: llmEvaluation?.strengths ?? [],
    risks: llmEvaluation?.risks ?? [],
    retryable: item?.retryable === true,
    llmRetryable: item?.llm_retryable === true,
  };
}

export function normalizeScreeningTask(run, items) {
  const safeRun = isRecord(run) ? run : {};
  const summary = normalizeScreeningRunSummary(safeRun);
  return {
    ...summary,
    files: Array.isArray(items) ? items.filter((item) => isRecord(item) && safeString(item.id)).map(normalizeFile) : [],
  };
}

function defaultIdempotencyKey() {
  return globalThis.crypto.randomUUID();
}

function abortError() {
  return new DOMException("Aborted", "AbortError");
}

function defaultWait(milliseconds, { signal } = {}) {
  if (signal?.aborted) return Promise.reject(abortError());
  return new Promise((resolve, reject) => {
    const timer = setTimeout(() => {
      signal?.removeEventListener("abort", onAbort);
      resolve();
    }, milliseconds);
    function onAbort() {
      clearTimeout(timer);
      reject(abortError());
    }
    signal?.addEventListener("abort", onAbort, { once: true });
  });
}

function withSignal(signal) {
  return signal ? { signal } : {};
}

function resourceData(response) {
  return response?.data ?? null;
}

export function createScreeningController({
  client = defaultApiClient,
  createIdempotencyKey = defaultIdempotencyKey,
  wait = defaultWait,
} = {}) {
  const pollGenerations = new Map();

  async function listJobs({ signal } = {}) {
    const jobs = [];
    let cursor = "";
    do {
      const path = `/api/v1/jobs?limit=100${cursor ? `&cursor=${encodeURIComponent(cursor)}` : ""}`;
      const response = await client.request(path, withSignal(signal));
      if (Array.isArray(response?.data)) jobs.push(...response.data);
      cursor = safeString(response?.meta?.next_cursor);
    } while (cursor);

    return jobs
        .filter(isRecord)
        .map((job) => ({ id: safeString(job.id), title: safeString(job.title) }))
        .filter((job) => job.id && job.title);
  }

  async function listRuns({ signal } = {}) {
    const runs = [];
    let cursor = "";
    do {
      const path = `/api/v1/screening-runs?limit=50${cursor ? `&cursor=${encodeURIComponent(cursor)}` : ""}`;
      const response = await client.request(path, withSignal(signal));
      if (Array.isArray(response?.data)) runs.push(...response.data);
      cursor = safeString(response?.meta?.next_cursor);
    } while (cursor);
    return runs.filter((run) => isRecord(run) && safeString(run.id)).map(normalizeScreeningRunSummary);
  }

  async function createRun(jobId, { signal } = {}) {
    const response = await client.request(`/api/v1/jobs/${encodeURIComponent(jobId)}/screening-runs`, {
      method: "POST",
      body: { source: "upload" },
      idempotencyKey: createIdempotencyKey(),
      ...withSignal(signal),
    });
    return resourceData(response);
  }

  async function uploadFiles(runId, files, { signal } = {}) {
    const uploads = Array.from(files, (file) => {
      if (!isFile(file)) throw new TypeError("screening uploads require File objects");
      const body = new FormData();
      body.append("file", file);
      return client.request(`/api/v1/screening-runs/${encodeURIComponent(runId)}/items`, {
        method: "POST",
        body,
        idempotencyKey: createIdempotencyKey(),
        ...withSignal(signal),
      }).then(resourceData);
    });
    return Promise.all(uploads);
  }

  async function startRun(runId, { signal } = {}) {
    const response = await client.request(`/api/v1/screening-runs/${encodeURIComponent(runId)}/start`, {
      method: "POST",
      idempotencyKey: createIdempotencyKey(),
      ...withSignal(signal),
    });
    return resourceData(response);
  }

  async function cancelRun(runId, { signal } = {}) {
    const response = await client.request(`/api/v1/screening-runs/${encodeURIComponent(runId)}/cancel`, {
      method: "POST",
      idempotencyKey: createIdempotencyKey(),
      ...withSignal(signal),
    });
    return resourceData(response);
  }

  async function getRun(runId, { signal } = {}) {
    return resourceData(await client.request(`/api/v1/screening-runs/${encodeURIComponent(runId)}`, withSignal(signal)));
  }

  async function getItems(runId, { signal } = {}) {
    const response = await client.request(`/api/v1/screening-runs/${encodeURIComponent(runId)}/items?limit=100`, withSignal(signal));
    return Array.isArray(response?.data) ? response.data : [];
  }

  async function retryItem(itemId, { signal } = {}) {
    const response = await client.request(`/api/v1/screening-items/${encodeURIComponent(itemId)}/retry`, {
      method: "POST",
      idempotencyKey: createIdempotencyKey(),
      ...withSignal(signal),
    });
    return resourceData(response);
  }

  async function pollRun(runId, { signal, intervalMs = 1000, onSnapshot } = {}) {
    const generation = (pollGenerations.get(runId) ?? 0) + 1;
    pollGenerations.set(runId, generation);
    const isCurrent = () => pollGenerations.get(runId) === generation;

    try {
      while (!signal?.aborted && isCurrent()) {
        const run = await getRun(runId, { signal });
        if (signal?.aborted || !isCurrent()) return null;
        const items = await getItems(runId, { signal });
        if (signal?.aborted || !isCurrent()) return null;
        const snapshot = normalizeScreeningTask(run, items);
        onSnapshot?.(snapshot);
        if (TERMINAL_RUN_STATUSES.has(snapshot.status)) return snapshot;
        await wait(intervalMs, { signal });
      }
      return null;
    } catch (error) {
      if (signal?.aborted || error?.name === "AbortError") return null;
      throw error;
    }
  }

  return { listJobs, listRuns, createRun, uploadFiles, startRun, cancelRun, getRun, getItems, retryItem, pollRun };
}

export const screeningController = createScreeningController();
