import { apiClient } from "./apiClient.js";

const COUNT_FIELDS = [
  ["contacts", "contacts"], ["resumes", "resumes"], ["applications", "applications"],
  ["screening_records", "screeningRecords"], ["interviews", "interviews"],
  ["feedback_records", "feedbackRecords"], ["talent_memberships", "talentMemberships"],
  ["resume_objects", "resumeObjects"], ["temporary_exports", "temporaryExports"],
];
const DELETION_STATUSES = new Set(["requested", "approved", "executing", "completed", "failed"]);
const OPEN_DELETION_STATUSES = new Set(["requested", "approved", "executing", "failed"]);
const ERROR_MESSAGES = {
  authentication_required: "登录状态已失效，请重新登录。",
  csrf_validation_failed: "当前会话已失效，请刷新后重试。",
  resource_not_found: "当前账号无权查看或操作该候选人的治理信息。",
  deletion_request_open: "该候选人已有待处理的删除请求，请勿重复提交。",
  legal_hold_active: "该候选人已处于法律保留状态。",
  legal_hold_already_released: "法律保留已解除，已刷新最新状态。",
  precondition_required: "法律保留版本缺失，请刷新后重试。",
  resource_version_conflict: "治理状态已变化，请核对最新状态后重试。",
  idempotency_conflict: "本次操作与先前请求冲突，请核对后重试。",
  validation_failed: "提交内容无效，请检查后重试。",
  service_unavailable: "服务暂时不可用，可直接重试当前操作。",
};

let keySequence = 0;
function defaultKey() {
  if (globalThis.crypto?.randomUUID) return globalThis.crypto.randomUUID();
  keySequence += 1;
  return `candidate-governance-${Date.now()}-${keySequence}`;
}
function safeString(value) { return typeof value === "string" ? value : ""; }
function nullableString(value) { return typeof value === "string" ? value : null; }
function deletionStatus(value, fallback = null) { return DELETION_STATUSES.has(value) ? value : fallback; }
function positiveVersion(value) { return Number.isInteger(value) && value >= 1 ? value : null; }
function safeCount(value) { return Number.isSafeInteger(value) && value >= 0 ? value : 0; }
function isAbort(error) { return error?.name === "AbortError"; }
function ambiguous(error) { return error?.kind === "unavailable" || error?.status === 0 || error?.status >= 500 || error?.code === "service_unavailable"; }
function errorMessage(error) { return ERROR_MESSAGES[error?.code] || "操作未完成，请稍后重试。"; }
function usableKey(value) { const key = safeString(value); return key && key.length <= 255 ? key : defaultKey(); }

export function normalizeGovernanceStatus(value) {
  return {
    deletionStatus: deletionStatus(value?.deletion_status),
    deletionRequestId: nullableString(value?.deletion_request_id),
    legalHoldActive: value?.legal_hold_active === true,
    legalHoldReason: nullableString(value?.legal_hold_reason),
    legalHoldId: nullableString(value?.legal_hold_id),
    legalHoldVersion: positiveVersion(value?.legal_hold_version),
  };
}

export function normalizeDeletionRequest(value) {
  const id = safeString(value?.id);
  const version = positiveVersion(value?.version);
  const impact = value?.impact;
  if (!id || !version || !impact || typeof impact !== "object") return null;
  const counts = {};
  for (const [serverName, clientName] of COUNT_FIELDS) counts[clientName] = safeCount(impact?.counts?.[serverName]);
  return {
    id,
    status: deletionStatus(value?.status, ""),
    version,
    reasonCode: safeString(value?.reason_code),
    requestedAt: safeString(value?.requested_at),
    approvedAt: nullableString(value?.approved_at),
    safeErrorCode: nullableString(value?.safe_error_code),
    activeApplicationCount: Number.isInteger(value?.active_application_count)
      && value.active_application_count >= 0
      ? value.active_application_count
      : null,
    impact: {
      schemaVersion: Number.isInteger(impact.schema_version) ? impact.schema_version : 0,
      candidateRef: safeString(impact.candidate_ref),
      candidateVersion: Number.isInteger(impact.candidate_version) ? impact.candidate_version : 0,
      policyVersion: Number.isInteger(impact.policy_version) ? impact.policy_version : 0,
      counts,
      backupWindowEndsAt: safeString(impact.backup_window_ends_at),
    },
  };
}

export function createCandidateGovernanceController({ client = apiClient, createIdempotencyKey = defaultKey, createAbortController = () => new AbortController() } = {}) {
  let disposed = false;
  let generation = 0;
  let requestController = null;
  let mutationController = null;
  const listeners = new Set();
  const intents = new Map();
  let state = {
    context: { candidateId: "", role: "" },
    loadStatus: "idle",
    status: null,
    deletionRequest: null,
    mutation: "",
    error: "",
    message: "",
  };

  function publish(patch) {
    if (disposed) return;
    state = { ...state, ...patch };
    listeners.forEach((listener) => listener(state));
  }
  function current(expected) { return !disposed && expected === generation; }
  function abortAll() { requestController?.abort(); mutationController?.abort(); requestController = null; mutationController = null; }
  function intent(type, signature) {
    const currentIntent = intents.get(type);
    if (currentIntent?.signature === signature) return currentIntent;
    const next = { signature, key: usableKey(createIdempotencyKey()) };
    intents.set(type, next);
    return next;
  }
  function clearIntent(type) { intents.delete(type); }

  async function refresh(expected = generation, { loading = false } = {}) {
    if (!state.context.candidateId || !current(expected)) return false;
    requestController?.abort();
    requestController = createAbortController();
    const signal = requestController.signal;
    if (loading) publish({ loadStatus: "loading", status: null, deletionRequest: null, error: "", message: "" });
    try {
      const response = await client.request(`/api/v1/candidates/${state.context.candidateId}/governance-status`, { signal });
      if (!current(expected)) return false;
      const status = normalizeGovernanceStatus(response?.data);
      let deletionRequest = null;
      if (status.deletionRequestId) {
        const detail = await client.request(`/api/v1/deletion-requests/${status.deletionRequestId}`, { signal });
        if (!current(expected)) return false;
        deletionRequest = normalizeDeletionRequest(detail?.data);
      }
      publish({ loadStatus: "ready", status, deletionRequest, error: "" });
      return true;
    } catch (error) {
      if (isAbort(error) || !current(expected)) return false;
      publish({ loadStatus: "error", error: errorMessage(error) });
      return false;
    }
  }

  async function load(candidateId, role) {
    abortAll();
    generation += 1;
    intents.clear();
    state = { ...state, context: { candidateId: safeString(candidateId), role: safeString(role) } };
    if (!state.context.candidateId) {
      publish({ loadStatus: "idle", status: null, deletionRequest: null, error: "" });
      return false;
    }
    return refresh(generation, { loading: true });
  }

  async function mutate(type, signature, path, body, options = {}) {
    if (disposed || !state.context.candidateId || state.mutation) return false;
    const operation = intent(type, signature);
    mutationController?.abort();
    mutationController = createAbortController();
    const expected = generation;
    publish({ mutation: type, error: "", message: "" });
    try {
      const response = await client.request(path, {
        method: "POST", body, idempotencyKey: operation.key,
        ...(options.ifMatch ? { ifMatch: options.ifMatch } : {}), signal: mutationController.signal,
      });
      if (!current(expected)) return false;
      clearIntent(type);
      const returnedRequest = normalizeDeletionRequest(response?.data);
      if (returnedRequest) publish({ deletionRequest: returnedRequest });
      await refresh(expected);
      if (!current(expected)) return false;
      publish({ mutation: "", message: options.successMessage || "治理状态已更新。" });
      return true;
    } catch (error) {
      if (isAbort(error) || !current(expected)) return false;
      if (!ambiguous(error)) clearIntent(type);
      publish({ mutation: "", error: errorMessage(error) });
      if (["resource_version_conflict", "precondition_required", "legal_hold_already_released"].includes(error?.code)) await refresh(expected);
      return false;
    }
  }

  async function requestDeletion() {
    if (state.status && OPEN_DELETION_STATUSES.has(state.status.deletionStatus)) {
      publish({ error: ERROR_MESSAGES.deletion_request_open });
      return false;
    }
    const body = { reason_code: "administrator_request" };
    return mutate("request-deletion", JSON.stringify(body), `/api/v1/candidates/${state.context.candidateId}/deletion-requests`, body, { successMessage: "删除请求已提交审批。" });
  }
  async function placeLegalHold(reason) {
    const value = safeString(reason).trim();
    if (!value || value.length > 1000) { publish({ error: "法律保留原因须为 1 至 1000 个非空字符。" }); return false; }
    const body = { reason: value };
    return mutate("place-hold", JSON.stringify(body), `/api/v1/candidates/${state.context.candidateId}/legal-holds`, body, { successMessage: "法律保留已生效。" });
  }
  async function releaseLegalHold(reason) {
    const value = safeString(reason).trim();
    const holdId = state.status?.legalHoldId;
    const version = state.status?.legalHoldVersion;
    if (!value || value.length > 1000) { publish({ error: "解除原因须为 1 至 1000 个非空字符。" }); return false; }
    if (!holdId || !version) { publish({ error: "缺少法律保留 ID 或版本，请刷新后重试。" }); return false; }
    const body = { reason: value };
    return mutate("release-hold", JSON.stringify({ body, holdId, version }), `/api/v1/legal-holds/${holdId}/releases`, body, { ifMatch: `"${version}"`, successMessage: "法律保留已解除。" });
  }

  return {
    getState: () => state,
    subscribe(listener) { listeners.add(listener); return () => listeners.delete(listener); },
    load,
    refresh: () => refresh(generation, { loading: true }),
    requestDeletion,
    placeLegalHold,
    releaseLegalHold,
    dispose() { if (disposed) return; abortAll(); generation += 1; disposed = true; listeners.clear(); intents.clear(); },
  };
}
