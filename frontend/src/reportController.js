import { apiClient } from "./apiClient.js";

const STAGE_TO_UI = {
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

function safeString(value, fallback = "") {
  return typeof value === "string" ? value : fallback;
}

function safeNumber(value) {
  return Number.isFinite(Number(value)) ? Number(value) : 0;
}

function safeArray(value) {
  return Array.isArray(value) ? value : [];
}

function percentage(value) {
  return Math.round(Math.max(0, Math.min(1, safeNumber(value))) * 1000) / 10;
}

function rounded(value, digits = 1) {
  const factor = 10 ** digits;
  return Math.round(safeNumber(value) * factor) / factor;
}

function reportQuery(filters = {}) {
  const params = new URLSearchParams();
  if (safeString(filters.jobId)) params.set("job_id", filters.jobId);
  if (safeString(filters.from)) params.set("from", filters.from);
  if (safeString(filters.to)) params.set("to", filters.to);
  return params.size ? `?${params}` : "";
}

function normalizeExport(value) {
  const id = safeString(value?.id);
  if (!id) return null;
  return {
    id,
    status: safeString(value?.status, "queued"),
    format: safeString(value?.format, "csv"),
    rowCount: Math.max(0, Math.trunc(safeNumber(value?.row_count))),
    createdAt: safeString(value?.created_at),
    completedAt: safeString(value?.completed_at),
  };
}

export function normalizeReportData({ funnel, quality } = {}) {
  const interview = funnel?.interviews || {};
  return {
    canExport: funnel?.can_export === true,
    totalApplications: Math.max(0, Math.trunc(safeNumber(funnel?.total_applications))),
    stages: safeArray(funnel?.stages).map((item) => ({
      stage: STAGE_TO_UI[item?.stage] || "其他阶段",
      apiStage: safeString(item?.stage),
      currentCount: Math.max(0, Math.trunc(safeNumber(item?.current_count))),
      averageDays: rounded(safeNumber(item?.average_time_in_stage_seconds) / 86400),
    })),
    interviews: {
      count: Math.max(0, Math.trunc(safeNumber(interview.count))),
      feedbackCompleted: Math.max(0, Math.trunc(safeNumber(interview.required_feedback_completed))),
      feedbackTotal: Math.max(0, Math.trunc(safeNumber(interview.required_feedback_total))),
      feedbackCompletionRate: percentage(interview.required_feedback_completion_rate),
      averageFeedbackHours: rounded(safeNumber(interview.average_feedback_turnaround_seconds) / 3600),
    },
    quality: {
      parseSucceeded: Math.max(0, Math.trunc(safeNumber(quality?.resume_parsing?.succeeded))),
      parseTotal: Math.max(0, Math.trunc(safeNumber(quality?.resume_parsing?.total))),
      parseSuccessRate: percentage(quality?.resume_parsing?.success_rate),
      rulePassed: Math.max(0, Math.trunc(safeNumber(quality?.rule_screening?.passed))),
      ruleTotal: Math.max(0, Math.trunc(safeNumber(quality?.rule_screening?.total))),
      rulePassRate: percentage(quality?.rule_screening?.pass_rate),
      llmSucceeded: Math.max(0, Math.trunc(safeNumber(quality?.llm?.succeeded))),
      llmTotal: Math.max(0, Math.trunc(safeNumber(quality?.llm?.total))),
      llmSuccessRate: percentage(quality?.llm?.success_rate),
    },
  };
}

export function createReportController({ client = apiClient, idSource = () => globalThis.crypto.randomUUID() } = {}) {
  return {
    async load(filters = {}, { signal } = {}) {
      const query = reportQuery(filters);
      const [funnel, quality] = await Promise.all([
        client.request(`/api/v1/reports/recruiting-funnel${query}`, { signal }),
        client.request(`/api/v1/reports/screening-quality${query}`, { signal }),
      ]);
      return normalizeReportData({ funnel: funnel?.data, quality: quality?.data });
    },

    async createExport(filters = {}, { signal, idempotencyKey = idSource() } = {}) {
      const payload = await client.request("/api/v1/exports", {
        method: "POST",
        body: {
          job_id: safeString(filters.jobId) || null,
          from: safeString(filters.from) || null,
          to: safeString(filters.to) || null,
        },
        idempotencyKey,
        signal,
      });
      return normalizeExport(payload?.data);
    },

    async getExport(exportId, { signal } = {}) {
      const id = safeString(exportId);
      if (!id) throw new Error("export id required");
      const payload = await client.request(`/api/v1/exports/${encodeURIComponent(id)}`, { signal });
      return normalizeExport(payload?.data);
    },

    async waitForExport(exportId, { signal, delay = abortableDelay, intervalMs = 750, maxAttempts = 120 } = {}) {
      for (let attempt = 0; attempt < maxAttempts; attempt += 1) {
        if (signal?.aborted) throw new DOMException("Aborted", "AbortError");
        const record = await this.getExport(exportId, { signal });
        if (!record || record.status === "succeeded" || record.status === "failed") return record;
        await delay(intervalMs, { signal });
      }
      const error = new Error("export timed out");
      error.code = "export_timeout";
      throw error;
    },

    async downloadExport(exportId, { signal } = {}) {
      const id = safeString(exportId);
      if (!id) throw new Error("export id required");
      const ticket = await client.request(`/api/v1/exports/${encodeURIComponent(id)}/download-tickets`, { method: "POST", signal });
      const token = safeString(ticket?.data?.token);
      if (!token) throw new Error("download ticket missing");
      return client.download("/api/v1/export-download-tickets/consume", { method: "POST", body: { token }, signal });
    },
  };
}

export function abortableDelay(milliseconds, { signal } = {}) {
  if (signal?.aborted) return Promise.reject(new DOMException("Aborted", "AbortError"));
  return new Promise((resolve, reject) => {
    const timer = setTimeout(() => {
      signal?.removeEventListener("abort", onAbort);
      resolve();
    }, milliseconds);
    function onAbort() {
      clearTimeout(timer);
      signal?.removeEventListener("abort", onAbort);
      reject(new DOMException("Aborted", "AbortError"));
    }
    signal?.addEventListener("abort", onAbort, { once: true });
  });
}

export const reportController = createReportController();
