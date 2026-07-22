import test from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";

import { ApiError } from "./apiClient.js";
import {
  createCandidateGovernanceController,
  normalizeDeletionRequest,
  normalizeGovernanceStatus,
} from "./candidateGovernance.js";

const candidateId = "11111111-1111-4111-8111-111111111111";
const requestId = "22222222-2222-4222-8222-222222222222";
const holdId = "33333333-3333-4333-8333-333333333333";
const counts = {
  contacts: 1, resumes: 2, applications: 3, screening_records: 4, interviews: 5,
  feedback_records: 6, talent_memberships: 7, resume_objects: 8, temporary_exports: 9,
};
const request = {
  id: requestId, status: "requested", version: 4, reason_code: "administrator_request",
  requested_at: "2026-07-15T01:00:00Z", approved_at: null, safe_error_code: null,
  impact: { schema_version: 1, candidate_ref: candidateId, candidate_version: 8, policy_version: 3, counts, backup_window_ends_at: "2026-08-15T01:00:00Z", private_manifest: "secret" },
  object_key: "private/key",
};

function deferred() {
  let resolve;
  let reject;
  const promise = new Promise((res, rej) => { resolve = res; reject = rej; });
  return { promise, resolve, reject };
}

function clientWith(handler) {
  const calls = [];
  return {
    calls,
    async request(path, options = {}) {
      calls.push({ path, options });
      return handler(path, options, calls.length);
    },
  };
}

test("normalizers expose only documented governance fields and all nine counts", () => {
  assert.deepEqual(normalizeGovernanceStatus({
    deletion_status: "approved", deletion_request_id: requestId, legal_hold_active: true,
    legal_hold_reason: "诉讼保全", legal_hold_id: holdId, legal_hold_version: 7, credential: "secret",
  }), {
    deletionStatus: "approved", deletionRequestId: requestId, legalHoldActive: true,
    legalHoldReason: "诉讼保全", legalHoldId: holdId, legalHoldVersion: 7,
  });
  assert.deepEqual(normalizeDeletionRequest(request), {
    id: requestId, status: "requested", version: 4, reasonCode: "administrator_request",
    requestedAt: "2026-07-15T01:00:00Z", approvedAt: null, safeErrorCode: null,
    activeApplicationCount: null,
    impact: { schemaVersion: 1, candidateRef: candidateId, candidateVersion: 8, policyVersion: 3, counts: {
      contacts: 1, resumes: 2, applications: 3, screeningRecords: 4, interviews: 5,
      feedbackRecords: 6, talentMemberships: 7, resumeObjects: 8, temporaryExports: 9,
    }, backupWindowEndsAt: "2026-08-15T01:00:00Z" },
  });
  assert.equal(normalizeGovernanceStatus({ deletion_status: "queued" }).deletionStatus, null);
  assert.equal(normalizeGovernanceStatus({ deletion_status: "processing" }).deletionStatus, null);
  assert.equal(normalizeDeletionRequest({ ...request, status: "queued" }).status, "");
  assert.equal(normalizeDeletionRequest({ ...request, status: "processing" }).status, "");
});

test("context load uses exact status and request URLs and suppresses stale candidate responses", async () => {
  const first = deferred();
  const client = clientWith((path) => path.includes(candidateId) ? first.promise : { data: { deletion_status: null, deletion_request_id: null, legal_hold_active: false } });
  const controller = createCandidateGovernanceController({ client });
  const loadingFirst = controller.load(candidateId, "招聘管理员");
  const firstSignal = client.calls[0].options.signal;
  const otherId = "44444444-4444-4444-8444-444444444444";
  await controller.load(otherId, "HR 招聘专员");
  assert.equal(firstSignal.aborted, true);
  first.resolve({ data: { deletion_status: "requested", deletion_request_id: requestId, legal_hold_active: true } });
  await loadingFirst;
  assert.equal(controller.getState().context.candidateId, otherId);
  assert.equal(controller.getState().status.deletionStatus, null);
  assert.deepEqual(client.calls.map((call) => call.path), [
    `/api/v1/candidates/${candidateId}/governance-status`,
    `/api/v1/candidates/${otherId}/governance-status`,
  ]);
});

test("deletion request sends exact body and reuses key only after ambiguous failure", async () => {
  const keys = ["request-key-1", "request-key-2"];
  let posts = 0;
  const client = clientWith((path, options) => {
    if (options.method === "POST") {
      posts += 1;
      if (posts === 1) throw new ApiError({ status: 503, code: "service_unavailable", kind: "unavailable" });
      if (posts === 2) throw new ApiError({ status: 422, code: "validation_failed" });
      return { data: request };
    }
    if (path.endsWith("governance-status")) return { data: { deletion_status: posts >= 3 ? "requested" : null, deletion_request_id: posts >= 3 ? requestId : null, legal_hold_active: false } };
    return { data: request };
  });
  const controller = createCandidateGovernanceController({ client, createIdempotencyKey: () => keys.shift() });
  await controller.load(candidateId, "HR 招聘专员");
  await controller.requestDeletion();
  await controller.requestDeletion();
  await controller.requestDeletion();
  const calls = client.calls.filter((call) => call.options.method === "POST");
  assert.deepEqual(calls.map((call) => call.path), Array(3).fill(`/api/v1/candidates/${candidateId}/deletion-requests`));
  assert.deepEqual(calls.map((call) => call.options.body), Array(3).fill({ reason_code: "administrator_request" }));
  assert.deepEqual(calls.map((call) => call.options.idempotencyKey), ["request-key-1", "request-key-1", "request-key-2"]);
  assert.equal(controller.getState().deletionRequest.impact.counts.temporaryExports, 9);
});

test("requested approved executing and failed block duplicate deletion before any POST", async () => {
  for (const deletionStatus of ["requested", "approved", "executing", "failed"]) {
    const client = clientWith(() => ({
      data: { deletion_status: deletionStatus, deletion_request_id: null, legal_hold_active: false },
    }));
    const controller = createCandidateGovernanceController({ client });

    await controller.load(candidateId, "HR 招聘专员");

    assert.equal(await controller.requestDeletion(), false, deletionStatus);
    assert.equal(client.calls.filter((call) => call.options.method === "POST").length, 0, deletionStatus);
    assert.match(controller.getState().error, /已有待处理的删除请求/, deletionStatus);
  }
});

test("completed is not open and does not block a request attempt as a duplicate", async () => {
  const client = clientWith((_path, options) => {
    if (options.method === "POST") throw new ApiError({ status: 409, code: "candidate_deletion_completed" });
    return { data: { deletion_status: "completed", deletion_request_id: null, legal_hold_active: false } };
  });
  const controller = createCandidateGovernanceController({ client });

  await controller.load(candidateId, "HR 招聘专员");
  await controller.requestDeletion();

  assert.equal(client.calls.filter((call) => call.options.method === "POST").length, 1);
  assert.doesNotMatch(controller.getState().error, /已有待处理的删除请求/);
});

test("hold placement validates trimmed reason then refreshes status and current request", async () => {
  const client = clientWith((path, options) => {
    if (options.method === "POST") return { data: { id: holdId, status: "active", reason: options.body.reason, placed_at: "2026-07-15T02:00:00Z", released_at: null, version: 1 } };
    if (path.endsWith("governance-status")) return { data: { deletion_status: "approved", deletion_request_id: requestId, legal_hold_active: true, legal_hold_reason: "case", legal_hold_id: holdId, legal_hold_version: 1 } };
    return { data: request };
  });
  const controller = createCandidateGovernanceController({ client, createIdempotencyKey: () => "hold-key" });
  await controller.load(candidateId, "招聘管理员");
  assert.equal(await controller.placeLegalHold("   "), false);
  assert.equal(await controller.placeLegalHold("  case  "), true);
  const post = client.calls.find((call) => call.options.method === "POST");
  assert.equal(post.path, `/api/v1/candidates/${candidateId}/legal-holds`);
  assert.deepEqual(post.options.body, { reason: "case" });
  assert.equal(post.options.idempotencyKey, "hold-key");
  assert.equal(controller.getState().status.legalHoldActive, true);
  assert.equal(controller.getState().deletionRequest.id, requestId);
});

test("hold release requires status id/version and sends quoted If-Match", async () => {
  let active = true;
  const client = clientWith((path, options) => {
    if (options.method === "POST") { active = false; return { data: { id: holdId, status: "released", reason: "case", placed_at: "2026-07-15T02:00:00Z", released_at: "2026-07-15T03:00:00Z", version: 8 } }; }
    return { data: { deletion_status: null, deletion_request_id: null, legal_hold_active: active, ...(active ? { legal_hold_id: holdId, legal_hold_version: 7 } : {}) } };
  });
  const controller = createCandidateGovernanceController({ client, createIdempotencyKey: () => "release-key" });
  await controller.load(candidateId, "招聘管理员");
  assert.equal(await controller.releaseLegalHold("解除原因"), true);
  const post = client.calls.find((call) => call.options.method === "POST");
  assert.equal(post.path, `/api/v1/legal-holds/${holdId}/releases`);
  assert.deepEqual(post.options.body, { reason: "解除原因" });
  assert.equal(post.options.ifMatch, '"7"');
  assert.equal(post.options.idempotencyKey, "release-key");
  assert.equal(controller.getState().status.legalHoldActive, false);
});

test("release is disabled when hold id/version is absent and dispose aborts late work", async () => {
  const pending = deferred();
  const client = clientWith(() => pending.promise);
  const controller = createCandidateGovernanceController({ client });
  const loading = controller.load(candidateId, "招聘管理员");
  const signal = client.calls[0].options.signal;
  controller.dispose();
  assert.equal(signal.aborted, true);
  pending.resolve({ data: { deletion_status: null, deletion_request_id: null, legal_hold_active: true } });
  await loading;
  assert.equal(await controller.releaseLegalHold("原因"), false);
});

test("candidate detail wires governance states, destructive confirmation and role-gated hold actions", () => {
  const source = readFileSync(new URL("./CandidateViews.jsx", import.meta.url), "utf8");
  const css = readFileSync(new URL("./product-theme-people.css", import.meta.url), "utf8");
  assert.match(source, /createCandidateGovernanceController/);
  assert.match(source, /canReadCandidateGovernance/);
  assert.match(source, /canRequestCandidateDeletion/);
  assert.match(source, /canManageCandidateLegalHold/);
  assert.match(source, /提交审批，不会立即删除/);
  assert.match(source, /legalHoldId/);
  assert.match(source, /legalHoldVersion/);
  assert.match(source, /data-dialog-initial-focus/);
  assert.match(source, /onKeyDown=\{handleKeyDown\}/);
  assert.match(source, /\["requested", "approved", "executing", "failed"\]/);
  assert.match(source, /requested: "待系统管理员审批"/);
  assert.match(source, /approved: "已批准，等待执行"/);
  assert.match(source, /executing: "正在删除"/);
  assert.match(source, /completed: "已完成"/);
  assert.match(source, /failed: "执行失败"/);
  assert.match(source, /设置 → 审计与数据治理 → 删除请求审批/);
  assert.match(source, /canRequest && !duplicateOpen/);
  assert.doesNotMatch(source, /已有删除请求/);
  assert.match(css, /grid-template-columns:[^;]*168px 76px/);
  assert.match(css, /\.candidate-page \.candidate-stage-cell \{[^}]*padding-right: 8px/);
  assert.match(css, /\.candidate-page \.candidate-score \{[^}]*padding-left: 4px/);
  assert.doesNotMatch(source, /queued|processing/);
  assert.doesNotMatch(source, /private_manifest|object_key|raw problem/i);
});
