import test from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";

const source = readFileSync(new URL("./OfferViews.jsx", import.meta.url), "utf8");
const candidateSource = readFileSync(new URL("./CandidateViews.jsx", import.meta.url), "utf8");
const appSource = readFileSync(new URL("./App.jsx", import.meta.url), "utf8");
const jobSource = readFileSync(new URL("./JobViews.jsx", import.meta.url), "utf8");
const settingsSource = readFileSync(new URL("./SettingsViews.jsx", import.meta.url), "utf8");

test("candidate detail exposes a routed Offer tab backed by the dedicated controller", () => {
  assert.match(candidateSource, /"Offer"/);
  assert.match(candidateSource, /<CandidateOfferView candidate=\{candidate\} role=\{role\} controller=\{offerController\} approvalId=\{offerApprovalId\}/);
  assert.match(appSource, /offerController=\{offerController\}/);
});

test("approved Offer remains explicitly unsent until HR acts", () => {
  assert.match(source, /审批已完成，但尚未发送/);
  assert.match(source, /系统不会自动发送/);
  assert.match(source, /canRenderOfferAction\(role, offer, "send"\)/);
  assert.match(source, /确认并发送 Offer/);
});

test("special Offer requires an explicit explanation", () => {
  assert.match(source, /draft\.isSpecial && !draft\.specialReason\.trim\(\)/);
  assert.match(source, /特殊 Offer 必须填写说明/);
  assert.match(source, /aria-label="特殊 Offer 说明" required/);
});

test("server redaction prevents compensation and body rendering", () => {
  assert.match(source, /const sensitive = Boolean\(offer\.canViewSensitiveContent \?\? offer\.can_view_sensitive_content\)/);
  assert.match(source, /!sensitive \? <div className="offer-redacted"/);
  assert.match(source, /敏感内容已由服务端隐藏/);
});

test("Offer actions require both local capability and server allowed actions", () => {
  assert.match(source, /Boolean\(offer\?\.allowedActions\?\.\[action\] \?\? offer\?\.allowed_actions\?\.\[action\]\)/);
  assert.match(source, /canPerformAction\(role, localCapability\)/);
});

test("approval workbench loads independently and navigates to candidate Offer", () => {
  assert.match(source, /controller\.listPendingApprovals\(\)/);
  assert.match(source, /onOpenOffer\(task\)/);
  assert.match(appSource, /candidateDetailPath\(context, "Offer", "\/workbench"\)/);
  assert.match(appSource, /approvalId: task\.id/);
  assert.match(source, /decisionApprovalId = approvalId \|\| offer\.pendingApprovalId/);
  assert.match(appSource, /<OfferApprovalTasks role=\{currentRole\} controller=\{offerController\}/);
});

test("draft updates explain immutable version behavior and conflicts preserve input", () => {
  assert.match(source, /保存会创建不可覆盖的新版本/);
  assert.match(source, /当前填写内容已保留，请刷新最新版本后核对/);
  assert.match(source, /controller\.updateDraft\(offer, payload\)/);
});

test("job form round-trips optional Offer defaults without blocking job save", () => {
  assert.match(jobSource, /offerApproverId: initialJob\?\.offerApproverId/);
  assert.match(jobSource, /offerTemplateId: initialJob\?\.offerTemplateId/);
  assert.match(jobSource, /aria-label="默认 Offer 审批人"/);
  assert.match(jobSource, /aria-label="默认 Offer 模板"/);
  assert.match(jobSource, /职位可在未配置时保存；但 HR 提交 Offer 审批前必须配置默认审批人/);
  assert.doesNotMatch(jobSource, /reviewerSelectionInvalid \|\| !values\.offerApproverId/);
});

test("Offer settings preserve template conflict and ordered approver controls", () => {
  assert.match(settingsSource, /<OfferSettings controller=\{offerController\}/);
  assert.match(source, /controller\.updateTemplate\(selected, payload\)/);
  assert.match(source, /controller\.updateSpecialApprovers\(state\.approvers, approverIds\)/);
  assert.match(source, /error\?\.latestTemplate/);
  assert.match(source, /error\?\.latestSettings/);
  assert.match(source, /当前排序已保留，已刷新版本基线/);
  assert.match(source, /setApproverIds\(moveItem\(approverIds, index, -1\)\)/);
  assert.match(source, /setApproverIds\(moveItem\(approverIds, index, 1\)\)/);
});
