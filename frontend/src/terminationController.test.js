import test from "node:test";
import assert from "node:assert/strict";
import { createTerminationController, canManageTermination, hasUnsettledExternal } from "./terminationController.js";
import { deriveCandidateInterviews, normalizeInterview, normalizeEmailDelivery, requiresManualCandidateNotice } from "./interviewController.js";

test("termination commands carry preview, distinct versions and idempotency keys", async () => {
  const calls = [];
  const preview = { application_id: "application-1", version: 12, preview_version: "fingerprint", termination: { version: 4 } };
  const controller = createTerminationController({ client: { request: async (path, options) => { calls.push({ path, options }); return { data: preview }; } } });
  assert.equal(await controller.preview("application-1"), preview);
  await controller.terminate(preview, "  候选人退出  ", true, "terminate-key");
  await controller.resolve(preview, " 已在飞书处理 ", "resolve-key");
  await controller.pending();
  assert.deepEqual(calls[1], { path: "/api/v1/applications/application-1/termination", options: { method: "POST", ifMatch: '"12"', idempotencyKey: "terminate-key", body: { preview_version: "fingerprint", reason_text: "候选人退出", confirm_not_onboarded: true } } });
  assert.deepEqual(calls[2], { path: "/api/v1/applications/application-1/termination/external-resolution", options: { method: "POST", ifMatch: '"4"', idempotencyKey: "resolve-key", body: { reason_text: "已在飞书处理" } } });
  assert.equal(calls[3].path, "/api/v1/application-terminations/pending");
});

test("termination conflicts are propagated without hidden command retry", async () => {
  let calls = 0;
  const conflict = Object.assign(new Error("conflict"), { status: 409 });
  const controller = createTerminationController({ client: { request: async () => { calls++; throw conflict; } } });
  await assert.rejects(controller.terminate({ application_id: "a", version: 1 }, "原因", false, "key"), (error) => error === conflict);
  assert.equal(calls, 1);
});

test("external reconciliation only queues an original-request lookup with termination version", async () => {
  let call;
  const controller = createTerminationController({client:{request:async(path,options)=>{call={path,options};return {data:{queued:true}};}}});
  assert.deepEqual(await controller.reconcile({application_id:"a",termination:{version:17}},"lookup-key"),{queued:true});
  assert.deepEqual(call,{path:"/api/v1/applications/a/termination/reconcile-external",options:{method:"POST",ifMatch:'"17"',idempotencyKey:"lookup-key",body:{reason_text:"核对飞书原请求结果"}}});
});

test("manager and interviewer have no termination entry capability; unsettled facts block attestation", () => {
  for (const role of ["用人经理", "hiring_manager", "面试官", "系统管理员"]) assert.equal(canManageTermination(role), false);
  for (const role of ["招聘管理员", "HR 招聘专员", "HR"]) assert.equal(canManageTermination(role), true);
  for (const state of ["unknown", "in_flight"]) assert.equal(hasUnsettledExternal({ external_items: [{ state }] }), true);
  assert.equal(hasUnsettledExternal({ external_items: [{ state: "needs_action" }] }), false);
});

test("candidate interview projection preserves authoritative management workflow and version", () => {
  const record = normalizeInterview({ id: "interview-1", application_id: "a", candidate: { id: "c" }, status: "pending_feedback", workflow_status: "confirmed", version: 8 });
  const [projected] = deriveCandidateInterviews("c", [record], "a");
  assert.equal(projected.status, "待评价");
  assert.equal(projected.workflowStatus, "已确认");
  assert.equal(projected.version, 8);
});

test("successful local cancellation without usable email honestly requires manual contact", () => {
  for (const safe_error_code of ["candidate_email_unconfirmed", "candidate_email_missing", "email_not_configured", "email_configuration_invalid"]) {
    const delivery = normalizeEmailDelivery({ status: "not_sent", safe_error_code });
    assert.equal(requiresManualCandidateNotice({ emailDelivery: delivery }), true);
    assert.equal(delivery.recipient, "请人工通知候选人");
  }
  assert.equal(requiresManualCandidateNotice({ emailDelivery: normalizeEmailDelivery({ id: "mail", status: "sent" }) }), false);
});
