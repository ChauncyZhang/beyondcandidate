import assert from "node:assert/strict";
import test from "node:test";

import { ApiError } from "./apiClient.js";
import offerController, { createOfferController } from "./offerController.js";

const APPLICATION_ID = "11111111-1111-4111-8111-111111111111";
const OFFER_ID = "22222222-2222-4222-8222-222222222222";
const APPROVAL_ID = "33333333-3333-4333-8333-333333333333";
const TEMPLATE_ID = "44444444-4444-4444-8444-444444444444";
const APPROVER_ID = "55555555-5555-4555-8555-555555555555";

function apiOffer(changes = {}) {
  return {
    id: OFFER_ID,
    application_id: APPLICATION_ID,
    job_id: "66666666-6666-4666-8666-666666666666",
    status: "draft",
    version: 3,
    current_version_id: "77777777-7777-4777-8777-777777777777",
    current_version_number: 2,
    candidate_response_deadline: "2026-09-01T12:00:00Z",
    is_special: false,
    special_reason: null,
    content: { body: "欢迎加入", compensation: { salary: "30k" } },
    can_view_sensitive_content: true,
    pdf_ready: true,
    allowed_actions: { update: true, submit: true, withdraw: true, send: false, decide: false },
    ...changes,
  };
}

function queuedClient(responses) {
  const calls = [];
  return {
    calls,
    client: {
      async request(path, options = {}) {
        calls.push({ path, options });
        const response = responses.shift();
        if (response instanceof Error) throw response;
        return typeof response === "function" ? response(path, options) : response;
      },
    },
  };
}

test("exports the stable Offer controller interface and singleton", () => {
  for (const name of [
    "getApplicationOffer", "listOffers", "getOffer", "createOffer", "updateDraft", "submitApproval", "approve",
    "requestChanges", "send", "withdraw", "listHistory", "listPendingApprovals", "listTemplates", "createTemplate",
    "updateTemplate", "getSpecialApprovers", "updateSpecialApprovers",
  ]) {
    assert.equal(typeof offerController[name], "function", name);
  }
  assert.equal(typeof createOfferController, "function");
});

test("application lookup uses the exact query contract and returns newest Offer or null", async () => {
  const signal = new AbortController().signal;
  const { client, calls } = queuedClient([{ data: [apiOffer()] }, { data: [] }]);
  const controller = createOfferController({ client });

  const offer = await controller.getApplicationOffer(APPLICATION_ID, { signal });
  const missing = await controller.getApplicationOffer(APPLICATION_ID);

  assert.equal(offer.id, OFFER_ID);
  assert.equal(missing, null);
  assert.deepEqual(calls, [
    { path: `/api/v1/offers?application_id=${APPLICATION_ID}`, options: { signal } },
    { path: `/api/v1/offers?application_id=${APPLICATION_ID}`, options: {} },
  ]);
});

test("create and draft update send exact snapshot bodies and mutation headers", async () => {
  const signal = new AbortController().signal;
  const keys = ["create-key", "update-key"];
  const { client, calls } = queuedClient([{ data: apiOffer() }, { data: apiOffer({ version: 4 }) }]);
  const controller = createOfferController({ client, idempotencyKey: () => keys.shift() });
  const draft = {
    templateId: TEMPLATE_ID,
    candidateResponseDeadline: "2026-09-01T12:00:00Z",
    content: { body: "欢迎加入", compensation: { salary: "30k" } },
    isSpecial: true,
    specialReason: "  超出标准薪酬带宽  ",
  };

  await controller.createOffer(APPLICATION_ID, draft, { signal });
  const updated = await controller.updateDraft({ id: OFFER_ID, version: 3 }, draft, { signal });

  const snapshot = {
    template_id: TEMPLATE_ID,
    candidate_response_deadline: "2026-09-01T12:00:00Z",
    content: { body: "欢迎加入", compensation: { salary: "30k" } },
    is_special: true,
    special_reason: "超出标准薪酬带宽",
  };
  assert.deepEqual(calls, [
    { path: "/api/v1/offers", options: { method: "POST", body: { application_id: APPLICATION_ID, ...snapshot }, idempotencyKey: "create-key", signal } },
    { path: `/api/v1/offers/${OFFER_ID}`, options: { method: "PATCH", body: snapshot, ifMatch: '"3"', idempotencyKey: "update-key", signal } },
  ]);
  assert.equal(updated.version, 4);
});

test("normal draft explicitly clears special metadata and special drafts require a reason before I/O", async () => {
  const { client, calls } = queuedClient([{ data: apiOffer() }]);
  const controller = createOfferController({ client, idempotencyKey: () => "normal-key" });
  const base = { candidateResponseDeadline: "2026-09-01T12:00:00Z", content: { body: "正文" } };

  await assert.rejects(
    () => controller.createOffer(APPLICATION_ID, { ...base, isSpecial: true, specialReason: "  " }),
    { code: "OFFER_SPECIAL_REASON_REQUIRED" },
  );
  await controller.createOffer(APPLICATION_ID, { ...base, isSpecial: false, specialReason: "不应发送" });

  assert.equal(calls.length, 1);
  assert.deepEqual(calls[0].options.body, {
    application_id: APPLICATION_ID,
    template_id: null,
    candidate_response_deadline: "2026-09-01T12:00:00Z",
    content: { body: "正文" },
    is_special: false,
    special_reason: null,
  });
});

test("approval, send, and withdrawal commands use exact endpoints without implicit send", async () => {
  const keys = ["submit", "approve", "reject", "send", "withdraw"];
  const { client, calls } = queuedClient([
    { data: apiOffer({ status: "pending_approval", version: 4 }) },
    { data: apiOffer({ status: "ready_to_send", version: 5 }) },
    { data: apiOffer({ status: "changes_requested", version: 5 }) },
    { data: apiOffer({ status: "sent", version: 6 }) },
    { data: apiOffer({ status: "withdrawn", version: 7 }) },
  ]);
  const controller = createOfferController({ client, idempotencyKey: () => keys.shift() });

  const pending = await controller.submitApproval({ id: OFFER_ID, version: 3 });
  const approved = await controller.approve(APPROVAL_ID, { id: OFFER_ID, version: 4 });
  await controller.requestChanges(APPROVAL_ID, { id: OFFER_ID, version: 4 }, "  请调整薪资  ");
  await controller.send({ id: OFFER_ID, version: 5 });
  await controller.withdraw({ id: OFFER_ID, version: 6 });

  assert.equal(pending.status, "pending_approval");
  assert.equal(approved.status, "ready_to_send");
  assert.equal(calls.filter(({ path }) => path.endsWith("/send")).length, 1, "approval never auto-sends");
  assert.deepEqual(calls.map(({ path, options }) => ({ path, options })), [
    { path: `/api/v1/offers/${OFFER_ID}/approvals`, options: { method: "POST", ifMatch: '"3"', idempotencyKey: "submit" } },
    { path: `/api/v1/offer-approvals/${APPROVAL_ID}/decisions`, options: { method: "POST", body: { decision: "approved", reason: null }, ifMatch: '"4"', idempotencyKey: "approve" } },
    { path: `/api/v1/offer-approvals/${APPROVAL_ID}/decisions`, options: { method: "POST", body: { decision: "rejected", reason: "请调整薪资" }, ifMatch: '"4"', idempotencyKey: "reject" } },
    { path: `/api/v1/offers/${OFFER_ID}/send`, options: { method: "POST", ifMatch: '"5"', idempotencyKey: "send" } },
    { path: `/api/v1/offers/${OFFER_ID}/withdrawals`, options: { method: "POST", ifMatch: '"6"', idempotencyKey: "withdraw" } },
  ]);
});

test("request changes requires a reason before network I/O", async () => {
  const { client, calls } = queuedClient([]);
  await assert.rejects(
    () => createOfferController({ client }).requestChanges(APPROVAL_ID, { id: OFFER_ID, version: 3 }, " "),
    { code: "OFFER_CHANGE_REASON_REQUIRED" },
  );
  assert.equal(calls.length, 0);
});

test("Offer version conflict refreshes once, attaches safe latest state, and never retries mutation", async () => {
  const conflict = new ApiError({ status: 409, code: "resource_version_conflict" });
  const { client, calls } = queuedClient([conflict, { data: apiOffer({ version: 8 }) }]);
  const controller = createOfferController({ client, idempotencyKey: () => "stale-key" });

  await assert.rejects(
    () => controller.submitApproval({ id: OFFER_ID, version: 3 }),
    (error) => error === conflict && error.latestOffer?.version === 8,
  );
  assert.deepEqual(calls.map(({ path }) => path), [`/api/v1/offers/${OFFER_ID}/approvals`, `/api/v1/offers/${OFFER_ID}`]);
});

test("server redaction is enforced locally even if sensitive fields are present", async () => {
  const { client } = queuedClient([{ data: apiOffer({
    can_view_sensitive_content: false,
    content: { body: "不应显示", compensation: { salary: "30k" } },
    special_reason: "不应显示",
  }) }]);
  const offer = await createOfferController({ client }).getOffer(OFFER_ID);

  assert.deepEqual(offer.content, { redacted: true });
  assert.equal(offer.specialReason, "");
  assert.equal(offer.canViewSensitiveContent, false);
  assert.deepEqual(offer.allowedActions, { update: true, submit: true, withdraw: true, send: false, decide: false });
});

test("history and pending workbench tasks use independent exact GET contracts", async () => {
  const signal = new AbortController().signal;
  const { client, calls } = queuedClient([
    { data: { versions: [{ id: "v1", version_number: 1, content: { redacted: true }, pdf_ready: false }], approvals: [], events: [] } },
    { data: [{ id: APPROVAL_ID, offer_id: OFFER_ID, application_id: APPLICATION_ID, candidate_name: "林夕", job_title: "工程师", offer_version: 4 }] },
  ]);
  const controller = createOfferController({ client });

  const history = await controller.listHistory(OFFER_ID, { signal });
  const tasks = await controller.listPendingApprovals({ signal });

  assert.deepEqual(history.versions[0].content, { redacted: true });
  assert.deepEqual({ id: tasks[0].id, offerId: tasks[0].offerId, applicationId: tasks[0].applicationId }, { id: APPROVAL_ID, offerId: OFFER_ID, applicationId: APPLICATION_ID });
  assert.deepEqual(calls, [
    { path: `/api/v1/offers/${OFFER_ID}/history`, options: { signal } },
    { path: "/api/v1/offer-approvals/pending", options: { signal } },
  ]);
});

test("template list/create/update use exact methods, bodies, and optimistic version", async () => {
  const signal = new AbortController().signal;
  const template = { id: TEMPLATE_ID, name: "标准模板", content: { body: "正文" }, status: "active", version: 2 };
  const keys = ["template-create", "template-update"];
  const { client, calls } = queuedClient([{ data: [template] }, { data: template }, { data: { ...template, version: 3, status: "inactive" } }]);
  const controller = createOfferController({ client, idempotencyKey: () => keys.shift() });

  await controller.listTemplates({ signal });
  await controller.createTemplate({ name: " 标准模板 ", content: { body: "正文" } }, { signal });
  await controller.updateTemplate(template, { name: "标准模板", content: { body: "新版" }, status: "inactive" }, { signal });

  assert.deepEqual(calls, [
    { path: "/api/v1/offer-templates", options: { signal } },
    { path: "/api/v1/offer-templates", options: { method: "POST", body: { name: "标准模板", content: { body: "正文" }, status: "active" }, idempotencyKey: "template-create", signal } },
    { path: `/api/v1/offer-templates/${TEMPLATE_ID}`, options: { method: "PUT", body: { name: "标准模板", content: { body: "新版" }, status: "inactive" }, ifMatch: '"2"', idempotencyKey: "template-update", signal } },
  ]);
});

test("template conflict refreshes the list and attaches the current template", async () => {
  const conflict = new ApiError({ status: 409, code: "resource_version_conflict" });
  const latest = { id: TEMPLATE_ID, name: "最新模板", content: {}, status: "active", version: 4 };
  const { client, calls } = queuedClient([conflict, { data: [latest] }]);
  const controller = createOfferController({ client, idempotencyKey: () => "stale-template" });

  await assert.rejects(
    () => controller.updateTemplate({ id: TEMPLATE_ID, version: 2 }, latest),
    (error) => error === conflict && error.latestTemplate?.version === 4,
  );
  assert.deepEqual(calls.map(({ path }) => path), [`/api/v1/offer-templates/${TEMPLATE_ID}`, "/api/v1/offer-templates"]);
});

test("ordered special approvers round-trip with exact PUT contract and conflict refresh", async () => {
  const SECOND_APPROVER_ID = "88888888-8888-4888-8888-888888888888";
  const conflict = new ApiError({ status: 409, code: "resource_version_conflict" });
  const { client, calls } = queuedClient([
    { data: { approver_ids: [APPROVER_ID], version: 6 } },
    { data: { approver_ids: [SECOND_APPROVER_ID, APPROVER_ID], version: 7 } },
    conflict,
    { data: { approver_ids: [APPROVER_ID], version: 8 } },
  ]);
  const keys = ["approvers-save", "approvers-stale"];
  const controller = createOfferController({ client, idempotencyKey: () => keys.shift() });

  const settings = await controller.getSpecialApprovers();
  const updated = await controller.updateSpecialApprovers(settings, [SECOND_APPROVER_ID, APPROVER_ID]);
  await assert.rejects(
    () => controller.updateSpecialApprovers(updated, [APPROVER_ID]),
    (error) => error === conflict && error.latestSettings?.version === 8,
  );

  assert.deepEqual(updated.approverIds, [SECOND_APPROVER_ID, APPROVER_ID]);
  assert.deepEqual(calls, [
    { path: "/api/v1/settings/offer-special-approvers", options: {} },
    { path: "/api/v1/settings/offer-special-approvers", options: { method: "PUT", body: { approver_ids: [SECOND_APPROVER_ID, APPROVER_ID] }, ifMatch: '"6"', idempotencyKey: "approvers-save" } },
    { path: "/api/v1/settings/offer-special-approvers", options: { method: "PUT", body: { approver_ids: [APPROVER_ID] }, ifMatch: '"7"', idempotencyKey: "approvers-stale" } },
    { path: "/api/v1/settings/offer-special-approvers", options: {} },
  ]);
});

test("non-conflict safe API and abort errors pass through unchanged", async () => {
  const safe = new ApiError({ status: 409, code: "offer_send_unavailable" });
  const abort = new DOMException("aborted", "AbortError");
  const unavailable = queuedClient([safe]);
  const aborted = queuedClient([abort]);

  await assert.rejects(() => createOfferController({ client: unavailable.client }).send({ id: OFFER_ID, version: 3 }), (error) => error === safe);
  await assert.rejects(() => createOfferController({ client: aborted.client }).listPendingApprovals(), (error) => error === abort);
  assert.equal(unavailable.calls.length, 1);
});
