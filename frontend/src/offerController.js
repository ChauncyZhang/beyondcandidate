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
  return Object.fromEntries(["update", "submit", "withdraw", "send", "decide"].map((action) => [action, source[action] === true]));
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
    allowedActions: normalizeAllowedActions(value?.allowed_actions),
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

  async function listHistory(offerId, { signal } = {}) {
    const id = requireId(offerId);
    const response = await client.request(`/api/v1/offers/${encodeURIComponent(id)}/history`, requestOptions(signal));
    return normalizeHistory(response?.data);
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
    approve,
    requestChanges,
    send,
    withdraw,
    listHistory,
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
