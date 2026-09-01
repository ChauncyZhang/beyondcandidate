import { apiClient } from "./apiClient.js";

function safeString(value) {
  return typeof value === "string" ? value.trim() : "";
}

function safeVersion(value) {
  return Number.isInteger(value) && value >= 0 ? value : null;
}

function safeObject(value) {
  return value && typeof value === "object" && !Array.isArray(value) ? { ...value } : {};
}

function safeArray(value) {
  return Array.isArray(value) ? value : [];
}

function codedError(code, message) {
  const error = new Error(message);
  error.code = code;
  return error;
}

function requireId(value, code = "OFFER_ID_REQUIRED") {
  const id = safeString(value);
  if (!id) throw codedError(code, "offer identity required");
  return id;
}

function requireVersion(value) {
  const version = safeVersion(value);
  if (version === null) throw codedError("OFFER_VERSION_REQUIRED", "offer version required");
  return version;
}

function requestOptions(signal, options = {}) {
  return signal ? { ...options, signal } : options;
}

function randomKey() {
  return globalThis.crypto?.randomUUID?.() || `offer-${Date.now()}`;
}

function normalizeAllowedActions(value) {
  const source = safeObject(value);
  return Object.fromEntries(["update", "submit", "withdraw", "send", "decide", "proxy_response"].map((action) => [action, source[action] === true]));
}

export function normalizeOnboarding(value) {
  if (!value || typeof value !== "object" || Array.isArray(value)) return null;
  return {
    id: safeString(value.id),
    status: safeString(value.status),
    version: safeVersion(value.version),
    expectedStartDate: safeString(value.expected_start_date ?? value.expectedStartDate),
    jobTitle: safeString(value.job_title ?? value.jobTitle),
    departmentName: safeString(value.department_name ?? value.departmentName),
    maskedPhone: safeString(value.masked_phone ?? value.maskedPhone),
    maskedEmail: safeString(value.masked_email ?? value.maskedEmail),
    complete: value.complete === true,
    canSubmit: value.can_submit === true || value.canSubmit === true,
    canUpdate: value?.allowed_actions?.update === true || value?.allowedActions?.update === true,
    blockingReason: safeString(value.blocking_reason ?? value.blockingReason),
    safeErrorCode: safeString(value.safe_error_code ?? value.safeErrorCode),
    instanceCode: safeString(value.instance_code ?? value.instanceCode),
  };
}

function normalizeResponse(value) {
  if (!value || typeof value !== "object" || Array.isArray(value)) return null;
  return {
    id: safeString(value.id),
    status: safeString(value.status || value.decision),
    source: safeString(value.source),
    expectedStartDate: safeString(value.expected_start_date),
    reasonText: safeString(value.reason_text),
    channel: safeString(value.channel || value.communication_channel),
    communicatedAt: safeString(value.communicated_at),
    note: safeString(value.note),
    actorId: safeString(value.actor_id || value.actor_user_id || value.responded_by),
    actorName: safeString(value.actor_name || value.responded_by_name),
    respondedAt: safeString(value.responded_at || value.created_at),
  };
}

function normalizeOffer(value) {
  const canViewSensitiveContent = value?.can_view_sensitive_content === true;
  return {
    id: safeString(value?.id),
    applicationId: safeString(value?.application_id),
    jobId: safeString(value?.job_id),
    candidateName: safeString(value?.candidate_name),
    jobTitle: safeString(value?.job_title),
    status: safeString(value?.status),
    version: safeVersion(value?.version),
    currentVersionId: safeString(value?.current_version_id),
    currentVersionNumber: safeVersion(value?.current_version_number),
    templateId: safeString(value?.template_id),
    candidateResponseDeadline: safeString(value?.candidate_response_deadline),
    isSpecial: value?.is_special === true,
    specialReason: canViewSensitiveContent ? safeString(value?.special_reason) : "",
    content: canViewSensitiveContent ? safeObject(value?.content) : { redacted: true },
    canViewSensitiveContent,
    pdfReady: value?.pdf_ready === true,
    contentReady: value?.content_ready === true,
    sendQueued: value?.send_queued === true,
    deadlineExpired: value?.deadline_expired === true,
    pendingApprovalId: safeString(value?.pending_approval_id),
    allowedActions: normalizeAllowedActions(value?.allowed_actions),
    response: normalizeResponse(value?.response),
    onboarding: normalizeOnboarding(value?.onboarding),
  };
}

export function filterEligibleSpecialApprovers(users) {
  return safeArray(users).filter((user) => user?.status === "active"
    && safeArray(user?.roles).some((role) => ["recruiting_admin", "hiring_manager"].includes(role)));
}

function normalizeTemplate(value) {
  return {
    id: safeString(value?.id),
    name: safeString(value?.name),
    content: safeObject(value?.content),
    status: value?.status === "inactive" ? "inactive" : "active",
    version: safeVersion(value?.version),
  };
}

function normalizePendingApproval(value) {
  return {
    id: safeString(value?.id),
    offerId: safeString(value?.offer_id),
    applicationId: safeString(value?.application_id),
    candidateId: safeString(value?.candidate_id),
    candidateName: safeString(value?.candidate_name),
    jobId: safeString(value?.job_id),
    jobTitle: safeString(value?.job_title),
    offerStatus: safeString(value?.offer_status),
    offerVersion: safeVersion(value?.offer_version),
    candidateResponseDeadline: safeString(value?.candidate_response_deadline),
    sequence: safeVersion(value?.sequence),
    roundNumber: safeVersion(value?.round_number),
    versionNumber: safeVersion(value?.version_number),
  };
}

function normalizeHistory(value) {
  const source = safeObject(value);
  return {
    versions: safeArray(source.versions).map((item) => ({
      id: safeString(item?.id),
      versionNumber: safeVersion(item?.version_number),
      content: item?.content?.redacted === true ? { redacted: true } : safeObject(item?.content),
      templateId: safeString(item?.template_id),
      candidateResponseDeadline: safeString(item?.candidate_response_deadline),
      isSpecial: item?.is_special === true,
      specialReason: safeString(item?.special_reason),
      submittedAt: safeString(item?.submitted_at),
      pdfReady: item?.pdf_ready === true,
      createdAt: safeString(item?.created_at),
    })),
    approvals: safeArray(source.approvals).map((item) => ({
      id: safeString(item?.id),
      versionNumber: safeVersion(item?.version_number),
      roundNumber: safeVersion(item?.round_number),
      sequence: safeVersion(item?.sequence),
      assigneeId: safeString(item?.assignee_id),
      status: safeString(item?.status),
      reason: safeString(item?.reason),
      decidedAt: safeString(item?.decided_at),
    })),
    events: safeArray(source.events).map((item) => ({
      id: safeString(item?.id),
      eventType: safeString(item?.event_type),
      createdAt: safeString(item?.created_at),
      payload: safeObject(item?.payload),
    })),
    responses: safeArray(source.responses).map(normalizeResponse).filter(Boolean),
  };
}

export function createProxyResponsePayload(values) {
  const decision = safeString(values?.decision);
  if (!["accepted", "declined"].includes(decision)) {
    throw codedError("OFFER_PROXY_DECISION_REQUIRED", "proxy response decision required");
  }
  const expectedStartDate = safeString(values?.expectedStartDate ?? values?.expected_start_date);
  if (decision === "accepted" && !expectedStartDate) {
    throw codedError("OFFER_PROXY_START_DATE_REQUIRED", "accepted proxy response requires expected start date");
  }
  const channel = safeString(values?.channel);
  if (!["phone", "wechat", "email", "other"].includes(channel)) {
    throw codedError("OFFER_PROXY_CHANNEL_REQUIRED", "proxy response channel required");
  }
  const communicatedAt = safeString(values?.communicatedAt ?? values?.communicated_at);
  const communicatedDate = new Date(communicatedAt);
  if (!communicatedAt || Number.isNaN(communicatedDate.getTime())) {
    throw codedError("OFFER_PROXY_COMMUNICATED_AT_REQUIRED", "proxy response communication time required");
  }
  return {
    decision,
    expected_start_date: decision === "accepted" ? expectedStartDate : null,
    channel,
    communicated_at: communicatedDate.toISOString(),
    note: safeString(values?.note) || null,
  };
}

export function createOnboardingDataPayload(values) {
  const source = safeObject(values);
  const onboardingData = {};
  for (const key of ["name", "gender", "phone", "email", "home_address"]) {
    const camelKey = key === "home_address" ? "homeAddress" : key;
    const value = safeString(source[key] ?? source[camelKey]);
    if (value) onboardingData[key] = value;
  }
  const expectedStartDate = safeString(source.expected_start_date ?? source.expectedStartDate);
  if (Object.keys(onboardingData).length === 0 && !expectedStartDate) {
    throw codedError("ONBOARDING_DATA_REQUIRED", "onboarding data required");
  }
  return {
    onboarding_data: onboardingData,
    ...(expectedStartDate ? { expected_start_date: expectedStartDate } : {}),
  };
}

function draftBody(applicationId, draft, includeApplication) {
  const content = safeObject(draft?.content);
  if (Object.keys(content).length === 0) throw codedError("OFFER_CONTENT_REQUIRED", "offer content required");
  const candidateResponseDeadline = safeString(draft?.candidateResponseDeadline ?? draft?.candidate_response_deadline);
  if (!candidateResponseDeadline) throw codedError("OFFER_DEADLINE_REQUIRED", "offer deadline required");
  const isSpecial = draft?.isSpecial === true || draft?.is_special === true;
  const specialReason = safeString(draft?.specialReason ?? draft?.special_reason);
  if (isSpecial && !specialReason) throw codedError("OFFER_SPECIAL_REASON_REQUIRED", "special offer reason required");
  const body = {
    template_id: safeString(draft?.templateId ?? draft?.template_id) || null,
    candidate_response_deadline: candidateResponseDeadline,
    content,
    is_special: isSpecial,
    special_reason: isSpecial ? specialReason : null,
  };
  return includeApplication ? { application_id: requireId(applicationId, "APPLICATION_ID_REQUIRED"), ...body } : body;
}

function templateBody(values) {
  const name = safeString(values?.name);
  if (!name) throw codedError("OFFER_TEMPLATE_NAME_REQUIRED", "offer template name required");
  return { name, content: safeObject(values?.content), status: values?.status === "inactive" ? "inactive" : "active" };
}

export function createOfferController({ client = apiClient, idempotencyKey = randomKey } = {}) {
  async function listOffers(applicationId, { signal } = {}) {
    const id = requireId(applicationId, "APPLICATION_ID_REQUIRED");
    const response = await client.request(`/api/v1/offers?application_id=${encodeURIComponent(id)}`, requestOptions(signal));
    return safeArray(response?.data).map(normalizeOffer).filter((offer) => offer.id);
  }

  async function getApplicationOffer(applicationId, options = {}) {
    return (await listOffers(applicationId, options))[0] || null;
  }

  async function getOffer(offerId, { signal } = {}) {
    const id = requireId(offerId);
    const response = await client.request(`/api/v1/offers/${encodeURIComponent(id)}`, requestOptions(signal));
    return normalizeOffer(response?.data);
  }

  async function refreshOfferConflict(error, offerId, signal) {
    if (error?.code !== "resource_version_conflict") throw error;
    try {
      error.latestOffer = await getOffer(offerId, { signal });
    } catch (refreshError) {
      if (refreshError?.name === "AbortError") throw refreshError;
      error.refreshFailed = true;
    }
    throw error;
  }

  async function offerMutation(path, offer, options, signal) {
    const id = requireId(offer?.id);
    const version = requireVersion(offer?.version);
    try {
      const response = await client.request(path(id), requestOptions(signal, {
        ...options,
        ifMatch: `"${version}"`,
        idempotencyKey: idempotencyKey(),
      }));
      return normalizeOffer(response?.data);
    } catch (error) {
      return refreshOfferConflict(error, id, signal);
    }
  }

  async function createOffer(applicationId, draft, { signal } = {}) {
    const response = await client.request("/api/v1/offers", requestOptions(signal, {
      method: "POST",
      body: draftBody(applicationId, draft, true),
      idempotencyKey: idempotencyKey(),
    }));
    return normalizeOffer(response?.data);
  }

  async function updateDraft(offer, draft, { signal } = {}) {
    return offerMutation((id) => `/api/v1/offers/${encodeURIComponent(id)}`, offer, {
      method: "PATCH",
      body: draftBody(null, draft, false),
    }, signal);
  }

  async function submitApproval(offer, { signal } = {}) {
    return offerMutation((id) => `/api/v1/offers/${encodeURIComponent(id)}/approvals`, offer, { method: "POST" }, signal);
  }

  async function listApproverOptions(offerId, { signal } = {}) {
    const id = requireId(offerId);
    const response = await client.request(`/api/v1/offers/${encodeURIComponent(id)}/approver-options`, requestOptions(signal));
    return {
      options: safeArray(response?.data).map((item) => ({ id: safeString(item?.id), name: safeString(item?.name) })).filter((item) => item.id && item.name),
      jobVersion: safeVersion(response?.meta?.job_version),
    };
  }

  async function setDefaultApprover(offer, approverId, jobVersion, { signal } = {}) {
    const id = requireId(offer?.id);
    const offerVersion = requireVersion(offer?.version);
    const expectedJobVersion = requireVersion(jobVersion);
    const normalizedApproverId = requireId(approverId, "OFFER_APPROVER_REQUIRED");
    try {
      const response = await client.request(`/api/v1/offers/${encodeURIComponent(id)}/default-approver`, requestOptions(signal, {
        method: "PUT",
        body: { approver_id: normalizedApproverId, offer_version: offerVersion },
        ifMatch: `"${expectedJobVersion}"`,
        idempotencyKey: idempotencyKey(),
      }));
      return safeObject(response?.data);
    } catch (error) {
      if (error?.code === "job_version_conflict") throw error;
      return refreshOfferConflict(error, id, signal);
    }
  }

  function decide(approvalId, offer, decision, reason, signal) {
    const id = requireId(approvalId, "OFFER_APPROVAL_ID_REQUIRED");
    return offerMutation(() => `/api/v1/offer-approvals/${encodeURIComponent(id)}/decisions`, offer, {
      method: "POST",
      body: { decision, reason: reason || null },
    }, signal);
  }

  async function approve(approvalId, offer, { signal } = {}) {
    return decide(approvalId, offer, "approved", null, signal);
  }

  async function requestChanges(approvalId, offer, reason, { signal } = {}) {
    const normalizedReason = safeString(reason);
    if (!normalizedReason) throw codedError("OFFER_CHANGE_REASON_REQUIRED", "offer change reason required");
    return decide(approvalId, offer, "rejected", normalizedReason, signal);
  }

  async function send(offer, { signal } = {}) {
    return offerMutation((id) => `/api/v1/offers/${encodeURIComponent(id)}/send`, offer, { method: "POST" }, signal);
  }

  async function withdraw(offer, { signal } = {}) {
    return offerMutation((id) => `/api/v1/offers/${encodeURIComponent(id)}/withdrawals`, offer, { method: "POST" }, signal);
  }

  async function proxyResponse(offer, payload, { signal } = {}) {
    return offerMutation((id) => `/api/v1/offers/${encodeURIComponent(id)}/proxy-responses`, offer, {
      method: "POST",
      body: createProxyResponsePayload(payload),
    }, signal);
  }

  async function listHistory(offerId, { signal } = {}) {
    const id = requireId(offerId);
    const response = await client.request(`/api/v1/offers/${encodeURIComponent(id)}/history`, requestOptions(signal));
    return normalizeHistory(response?.data);
  }

  async function getOnboarding(applicationId, { signal } = {}) {
    const id = requireId(applicationId, "APPLICATION_ID_REQUIRED");
    const response = await client.request(`/api/v1/applications/${encodeURIComponent(id)}/onboarding`, requestOptions(signal));
    return normalizeOnboarding(response?.data?.onboarding ?? response?.data);
  }

  async function updateOnboarding(onboarding, values, { signal } = {}) {
    const id = requireId(onboarding?.id, "ONBOARDING_ID_REQUIRED");
    const version = requireVersion(onboarding?.version);
    const correctionOnly = onboarding?.status === "failed" && onboarding?.blockingReason === "onboarding_gender_invalid";
    const response = await client.request(`/api/v1/onboardings/${encodeURIComponent(id)}`, requestOptions(signal, {
      method: "PUT",
      body: createOnboardingDataPayload(correctionOnly ? { gender: values?.gender } : values),
      ifMatch: `"${version}"`,
    }));
    return normalizeOnboarding(response?.data?.onboarding ?? response?.data);
  }

  async function submitOnboarding(onboarding, { signal } = {}) {
    const id = requireId(onboarding?.id, "ONBOARDING_ID_REQUIRED");
    const version = requireVersion(onboarding?.version);
    const response = await client.request(`/api/v1/onboardings/${encodeURIComponent(id)}/submissions`, requestOptions(signal, {
      method: "POST",
      ifMatch: `"${version}"`,
      idempotencyKey: idempotencyKey(),
    }));
    return normalizeOnboarding(response?.data?.onboarding ?? response?.data);
  }

  async function listPendingApprovals({ signal } = {}) {
    const response = await client.request("/api/v1/offer-approvals/pending", requestOptions(signal));
    return safeArray(response?.data).map(normalizePendingApproval).filter((item) => item.id && item.offerId);
  }

  async function listTemplates({ signal } = {}) {
    const response = await client.request("/api/v1/offer-templates", requestOptions(signal));
    return safeArray(response?.data).map(normalizeTemplate).filter((item) => item.id);
  }

  async function createTemplate(values, { signal } = {}) {
    const response = await client.request("/api/v1/offer-templates", requestOptions(signal, {
      method: "POST", body: templateBody(values), idempotencyKey: idempotencyKey(),
    }));
    return normalizeTemplate(response?.data);
  }

  async function updateTemplate(template, values, { signal } = {}) {
    const id = requireId(template?.id, "OFFER_TEMPLATE_ID_REQUIRED");
    const version = requireVersion(template?.version);
    try {
      const response = await client.request(`/api/v1/offer-templates/${encodeURIComponent(id)}`, requestOptions(signal, {
        method: "PUT", body: templateBody(values), ifMatch: `"${version}"`, idempotencyKey: idempotencyKey(),
      }));
      return normalizeTemplate(response?.data);
    } catch (error) {
      if (error?.code !== "resource_version_conflict") throw error;
      try {
        error.latestTemplate = (await listTemplates({ signal })).find((item) => item.id === id) || null;
      } catch (refreshError) {
        if (refreshError?.name === "AbortError") throw refreshError;
        error.refreshFailed = true;
      }
      throw error;
    }
  }

  async function getSpecialApprovers({ signal } = {}) {
    const response = await client.request("/api/v1/settings/offer-special-approvers", requestOptions(signal));
    return {
      approverIds: safeArray(response?.data?.approver_ids).map(safeString).filter(Boolean),
      version: safeVersion(response?.data?.version),
    };
  }

  async function updateSpecialApprovers(settings, approverIds, { signal } = {}) {
    const version = requireVersion(settings?.version);
    const normalizedIds = safeArray(approverIds).map(safeString).filter(Boolean);
    if (new Set(normalizedIds).size !== normalizedIds.length) {
      throw codedError("OFFER_APPROVER_DUPLICATE", "offer approvers must be unique");
    }
    try {
      const response = await client.request("/api/v1/settings/offer-special-approvers", requestOptions(signal, {
        method: "PUT",
        body: { approver_ids: normalizedIds },
        ifMatch: `"${version}"`,
        idempotencyKey: idempotencyKey(),
      }));
      return { approverIds: safeArray(response?.data?.approver_ids).map(safeString).filter(Boolean), version: safeVersion(response?.data?.version) };
    } catch (error) {
      if (error?.code !== "resource_version_conflict") throw error;
      try {
        error.latestSettings = await getSpecialApprovers({ signal });
      } catch (refreshError) {
        if (refreshError?.name === "AbortError") throw refreshError;
        error.refreshFailed = true;
      }
      throw error;
    }
  }

  return {
    getApplicationOffer,
    listOffers,
    getOffer,
    createOffer,
    updateDraft,
    submitApproval,
    listApproverOptions,
    setDefaultApprover,
    approve,
    requestChanges,
    send,
    withdraw,
    proxyResponse,
    listHistory,
    getOnboarding,
    updateOnboarding,
    submitOnboarding,
    listPendingApprovals,
    listTemplates,
    createTemplate,
    updateTemplate,
    getSpecialApprovers,
    updateSpecialApprovers,
  };
}

export const offerController = createOfferController();
export default offerController;
