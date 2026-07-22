import { apiClient } from "./apiClient.js";

export function createLatestMembershipRequest() {
  let generation = 0;
  let activeController = null;
  return {
    start() {
      activeController?.abort();
      const controller = new AbortController();
      const requestGeneration = ++generation;
      activeController = controller;
      return {
        signal: controller.signal,
        isCurrent: () => requestGeneration === generation && activeController === controller && !controller.signal.aborted,
      };
    },
    cancel() {
      generation += 1;
      activeController?.abort();
      activeController = null;
    },
  };
}

const VISIBILITY_TO_UI = {
  private: "仅自己可见",
  recruiting_team: "招聘团队可见",
  granted: "指定成员可见",
};
const UI_TO_VISIBILITY = Object.fromEntries(Object.entries(VISIBILITY_TO_UI).map(([api, ui]) => [ui, api]));
const STATUS_TO_UI = { active: "正常", do_not_contact: "永久不再联系", blocked: "黑名单" };
const UI_TO_STATUS = Object.fromEntries(Object.entries(STATUS_TO_UI).map(([api, ui]) => [ui, api]));
const APPLICATION_STAGE_TO_UI = {
  new: "新简历",
  screening: "筛选中",
  review: "用人经理复核",
  deferred: "AI 初筛暂缓",
  contact: "待沟通",
  interview_pending: "待安排",
  interviewing: "面试中",
  communication: "待沟通",
  interview: "面试中",
  decision: "待决策",
  passed: "已通过",
  hired: "已录用",
  rejected: "已淘汰",
  withdrawn: "已撤回",
};

function safeString(value, fallback = "") {
  return typeof value === "string" ? value : fallback;
}

function safeArray(value) {
  return Array.isArray(value) ? value : [];
}

function requireId(value, code) {
  const id = safeString(value).trim();
  if (!id) {
    const error = new Error("resource id required");
    error.code = code;
    throw error;
  }
  return id;
}

function requireVersion(value) {
  if (!Number.isInteger(value) || value < 1) {
    const error = new Error("resource version required");
    error.code = "talent_version_required";
    throw error;
  }
  return value;
}

export function selectExactTalentPool(pools, poolId) {
  const id = safeString(poolId).trim();
  return id ? safeArray(pools).find((pool) => pool?.id === id) || null : null;
}

export function canAddCandidateToTalentPool(candidate) {
  const candidateId = safeString(candidate?.candidateId || candidate?.id).trim();
  const sourceApplicationId = safeString(candidate?.applicationId || candidate?.application?.id).trim();
  return Boolean(candidate?.serverBacked && candidateId && sourceApplicationId);
}

export function buildReactivatedCandidateSummary(candidate, application, position) {
  const candidateId = requireId(application?.candidate_id || candidate?.candidateId || candidate?.id, "talent_candidate_required");
  return {
    ...candidate,
    id: candidateId,
    candidateId,
    applicationId: requireId(application?.id, "talent_application_required"),
    jobId: requireId(application?.job_id, "talent_job_required"),
    position: safeString(position?.name, candidate?.position),
    serverBacked: true,
  };
}

export function selectServerTalentCandidates(candidates, candidateIds) {
  const requested = new Set(safeArray(candidateIds));
  const selected = [];
  const seen = new Set();
  for (const candidate of safeArray(candidates)) {
    const candidateId = candidate?.candidateId || candidate?.id;
    if (!canAddCandidateToTalentPool(candidate) || seen.has(candidateId)) continue;
    if (!requested.has(candidate.id) && !requested.has(candidate.candidateId)) continue;
    seen.add(candidateId);
    selected.push(candidate);
  }
  return selected;
}

function dateOnly(value) {
  const raw = safeString(value);
  return /^\d{4}-\d{2}-\d{2}/.test(raw) ? raw.slice(0, 10) : "";
}

function formatActivity(value) {
  const instant = new Date(value);
  return Number.isNaN(instant.getTime()) ? "时间未记录" : instant.toLocaleString("zh-CN", { hour12: false, month: "2-digit", day: "2-digit", hour: "2-digit", minute: "2-digit" });
}

export function normalizeTalentPool(value) {
  const id = safeString(value?.id);
  if (!id) return null;
  return {
    id,
    serverBacked: true,
    name: safeString(value?.name, "未命名人才库"),
    systemKey: safeString(value?.system_key),
    purpose: safeString(value?.purpose),
    suitableRoles: safeArray(value?.suitable_roles).filter((item) => typeof item === "string"),
    ownerId: safeString(value?.owner?.id),
    owner: safeString(value?.owner?.display_name, "未分配"),
    visibility: VISIBILITY_TO_UI[value?.visibility] || "指定成员可见",
    memberIds: [],
    memberCount: Number.isInteger(value?.member_count) ? value.member_count : 0,
    retentionDays: Number.isInteger(value?.retention_days) ? value.retention_days : 730,
    recentActivity: formatActivity(value?.updated_at),
    activity: "服务端已同步",
    version: Number.isInteger(value?.version) ? value.version : null,
    grants: safeArray(value?.grants),
  };
}

export function normalizeTalentMembership(value) {
  const id = safeString(value?.id);
  const candidateId = safeString(value?.candidate?.id);
  if (!id || !candidateId) return null;
  const redactedSource = Boolean(value?.source_application?.redacted);
  const source = redactedSource ? null : value?.source_application || null;
  const sourceApplicationId = safeString(source?.id);
  const sourceJobId = safeString(source?.job_id);
  const sourceJobTitle = safeString(source?.job_title, "历史职位");
  const tags = safeArray(value?.tags).filter((item) => typeof item === "string");
  const deferredScreening = source && value?.deferred_screening && typeof value.deferred_screening === "object"
    ? value.deferred_screening
    : null;
  const finalScore = typeof deferredScreening?.final_score === "number" ? deferredScreening.final_score : null;
  const mainGaps = safeArray(deferredScreening?.main_gaps).filter((item) => typeof item === "string");
  const candidate = {
    id: candidateId,
    candidateId,
    applicationId: sourceApplicationId,
    jobId: sourceJobId,
    position: sourceJobTitle,
    serverBacked: true,
    name: safeString(value?.candidate?.display_name, "未命名候选人"),
    role: safeString(value?.candidate?.current_title, "当前职称未填写"),
    company: "",
    city: safeString(value?.candidate?.location, "地点未填写"),
    summary: "候选人详细信息需从候选人档案按权限读取。",
    phone: "",
    email: "",
    skills: tags,
    applications: source ? [{
      id: sourceApplicationId,
      applicationId: sourceApplicationId,
      jobId: sourceJobId,
      position: sourceJobTitle,
      state: APPLICATION_STAGE_TO_UI[source.stage] || "状态未知",
      source: "历史申请",
      created: "",
    }] : [],
  };
  return {
    id,
    serverBacked: true,
    poolId: safeString(value?.pool_id),
    candidateId,
    candidate,
    sourceApplicationId,
    originalJob: { id: sourceJobId, title: source ? sourceJobTitle : "" },
    sourceStage: redactedSource ? "来源申请不可见" : APPLICATION_STAGE_TO_UI[source?.stage] || "状态未知",
    finalScore,
    deferredAt: dateOnly(deferredScreening?.deferred_at),
    mainGaps,
    suitableRoles: safeArray(value?.suitable_roles).filter((item) => typeof item === "string"),
    tags,
    ownerId: safeString(value?.owner?.id),
    owner: safeString(value?.owner?.display_name, "未分配"),
    joinedAt: dateOnly(value?.created_at),
    reason: safeString(value?.reason),
    source: redactedSource ? "来源申请不可见" : safeString(source?.job_title, "人才库"),
    nextContact: dateOnly(value?.next_contact_at),
    retentionUntil: dateOnly(value?.retention_until),
    recentInteraction: formatActivity(value?.updated_at),
    latestConclusion: redactedSource ? "来源申请不可见" : safeString(source?.human_conclusion, "暂无历史结论"),
    status: STATUS_TO_UI[value?.status] || "正常",
    version: Number.isInteger(value?.version) ? value.version : null,
  };
}

function poolPayload(form, ownerId) {
  if (form?.visibility === "指定成员可见") {
    const error = new Error("grantee selection is required");
    error.code = "talent_grants_required";
    throw error;
  }
  return {
    name: safeString(form?.name).trim(),
    purpose: safeString(form?.purpose).trim(),
    visibility: UI_TO_VISIBILITY[form?.visibility] || "recruiting_team",
    owner_id: requireId(ownerId, "talent_owner_required"),
    suitable_roles: safeArray(form?.suitableRoles),
    retention_days: Number(form?.retentionDays) || 730,
    grants: [],
  };
}

function membershipPatch(updated) {
  return {
    owner_id: requireId(updated?.ownerId, "talent_owner_required"),
    suitable_roles: safeArray(updated?.suitableRoles),
    tags: safeArray(updated?.tags),
    reason: safeString(updated?.reason),
    next_contact_at: updated?.nextContact ? `${updated.nextContact}T00:00:00+08:00` : null,
    retention_until: `${safeString(updated?.retentionUntil)}T23:59:59+08:00`,
    status: UI_TO_STATUS[updated?.status] || "active",
  };
}

export function createTalentController({ client = apiClient, idSource = () => globalThis.crypto.randomUUID() } = {}) {
  const pendingKeys = new Map();

  async function mutate(intent, body, request) {
    const operation = pendingKeys.get(intent) || { idempotencyKey: idSource(), body };
    pendingKeys.set(intent, operation);
    try {
      const result = await request(operation.idempotencyKey, operation.body);
      pendingKeys.delete(intent);
      return result;
    } catch (error) {
      const status = Number(error?.status);
      const unavailableTransport = status === 0 && error?.kind === "unavailable";
      const ambiguous = unavailableTransport || !Number.isFinite(status) || status === 408 || status === 429 || status >= 500;
      if (!ambiguous) pendingKeys.delete(intent);
      throw error;
    }
  }

  return {
    async listPools(filters = {}, options = {}) {
      const params = new URLSearchParams();
      if (filters.q) params.set("q", filters.q);
      if (filters.cursor) params.set("cursor", filters.cursor);
      if (filters.limit) params.set("limit", String(filters.limit));
      const payload = await client.request(`/api/v1/talent-pools${params.size ? `?${params}` : ""}`, options);
      return {
        records: safeArray(payload?.data).map(normalizeTalentPool).filter(Boolean),
        nextCursor: safeString(payload?.meta?.next_cursor) || null,
      };
    },
    async createPool(form, ownerId, options = {}) {
      const body = poolPayload(form, ownerId);
      const intent = `pool:create:${JSON.stringify(body)}`;
      const payload = await mutate(intent, body, (idempotencyKey, retainedBody) => client.request("/api/v1/talent-pools", {
        method: "POST", body: retainedBody, idempotencyKey, ...options,
      }));
      return normalizeTalentPool(payload?.data);
    },
    async listMemberships(poolId, filters = {}, options = {}) {
      const id = requireId(poolId, "talent_pool_required");
      const params = new URLSearchParams();
      if (filters.q) params.set("q", filters.q);
      if (filters.cursor) params.set("cursor", filters.cursor);
      if (filters.limit) params.set("limit", String(filters.limit));
      const payload = await client.request(`/api/v1/talent-pools/${encodeURIComponent(id)}/memberships${params.size ? `?${params}` : ""}`, options);
      return {
        records: safeArray(payload?.data).map(normalizeTalentMembership).filter(Boolean),
        nextCursor: safeString(payload?.meta?.next_cursor) || null,
      };
    },
    async addMembership(poolId, candidate, ownerId, options = {}) {
      const id = requireId(poolId, "talent_pool_required");
      const candidateId = requireId(candidate?.candidateId || candidate?.id, "talent_candidate_required");
      const sourceApplicationId = requireId(candidate?.applicationId || candidate?.application?.id, "talent_application_required");
      const retention = new Date();
      retention.setUTCFullYear(retention.getUTCFullYear() + 2);
      const body = {
          candidate_id: candidateId,
          source_application_id: sourceApplicationId,
          owner_id: requireId(ownerId, "talent_owner_required"),
          suitable_roles: [safeString(candidate?.position, "待确认岗位")],
          tags: safeArray(candidate?.tags),
          reason: "由招聘人员加入人才库",
          next_contact_at: null,
          retention_until: retention.toISOString(),
      };
      const intent = `membership:add:${id}:${candidateId}:${sourceApplicationId}`;
      const payload = await mutate(intent, body, (idempotencyKey, retainedBody) => client.request(`/api/v1/talent-pools/${encodeURIComponent(id)}/memberships`, {
        method: "POST", body: retainedBody, idempotencyKey, ...options,
      }));
      return normalizeTalentMembership(payload?.data);
    },
    async updateMembership(updated, options = {}) {
      const id = requireId(updated?.id, "talent_membership_required");
      const version = requireVersion(updated?.version);
      const payload = await client.request(`/api/v1/talent-pool-memberships/${encodeURIComponent(id)}`, {
        method: "PATCH",
        body: membershipPatch(updated),
        ifMatch: `"${version}"`,
        ...options,
      });
      return normalizeTalentMembership(payload?.data);
    },
    async removeMembership(member, reason, options = {}) {
      const id = requireId(member?.id, "talent_membership_required");
      const version = requireVersion(member?.version);
      await client.request(`/api/v1/talent-pool-memberships/${encodeURIComponent(id)}`, {
        method: "DELETE",
        body: { reason: safeString(reason, "由招聘人员移出人才库") },
        ifMatch: `"${version}"`,
        ...options,
      });
    },
    async reactivate(memberId, jobId, options = {}) {
      const id = requireId(memberId, "talent_membership_required");
      const targetJobId = requireId(jobId, "talent_job_required");
      const intent = `membership:reactivate:${id}:${targetJobId}`;
      const body = { job_id: targetJobId };
      const payload = await mutate(intent, body, (idempotencyKey, retainedBody) => client.request(`/api/v1/talent-pool-memberships/${encodeURIComponent(id)}/reactivations`, {
        method: "POST", body: retainedBody, idempotencyKey, ...options,
      }));
      return payload?.data ?? null;
    },
    async referToReview(memberId, memberVersion, options = {}) {
      const id = requireId(memberId, "talent_membership_required");
      const version = requireVersion(memberVersion);
      const body = {};
      const intent = `membership:review-referral:${id}:${version}`;
      const payload = await mutate(intent, body, (idempotencyKey, retainedBody) => client.request(`/api/v1/talent-pool-memberships/${encodeURIComponent(id)}/review-referrals`, {
        method: "POST",
        body: retainedBody,
        ifMatch: `"${version}"`,
        idempotencyKey,
        ...options,
      }));
      return {
        application: payload?.data?.application ?? null,
        membership: normalizeTalentMembership(payload?.data?.membership),
      };
    },
  };
}

export const talentController = createTalentController();
