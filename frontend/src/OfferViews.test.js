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
  assert.match(candidateSource, /candidate\.stage === "已通过" && canPerformAction\(role, "管理 Offer"\)/);
  assert.match(candidateSource, /onClick=\{\(\) => onTabChange\("Offer"\)\}[\s\S]*办理 Offer/);
  assert.match(appSource, /offerController=\{offerController\}/);
});

test("Offer workspace makes approval state and the next HR action explicit", () => {
  assert.match(source, /OFFER_PROGRESS_STEPS = Object\.freeze\(\["填写 Offer", "审批", "发送", "候选人确认"\]\)/);
  assert.match(source, /offer\.status === "pending_approval" && <div className="offer-pending-notice"/);
  assert.match(source, /当前暂不需要 HR 操作/);
  assert.match(source, /审批通过后，本页会出现“确认并发送 Offer”按钮/);
  assert.match(source, /刷新状态/);
});

test("Offer form and history use stable aligned structures", () => {
  assert.match(source, /className="offer-compact-field">候选人回复截止日期/);
  assert.match(source, /type="date"/);
  assert.match(source, /offerDeadlineDateValue/);
  assert.match(source, /offerDeadlineEndOfDay/);
  assert.match(source, /displayOfferDeadline/);
  assert.match(source, /候选人可在所选日期当天完成确认/);
  assert.match(source, /审批记录/);
  assert.match(source, /退回原因/);
  assert.match(source, /请根据以上意见修改 Offer/);
  assert.doesNotMatch(source, /className="offer-version-list"/);
  assert.doesNotMatch(source, /className="offer-summary"/);
  assert.doesNotMatch(source, /候选人页面/);
});

test("approved Offer remains explicitly unsent until HR acts", () => {
  assert.match(source, /审批已完成，但尚未发送/);
  assert.match(source, /点击“确认并发送 Offer”/);
  assert.match(source, /offer\.status === "ready_to_send" && !canRenderOfferAction\(role, offer, "send"\)/);
  assert.match(source, /已加入发送队列/);
  assert.match(source, /核对 Offer 内容和候选人邮箱/);
  assert.match(source, /发送请求已提交/);
  assert.match(source, /邮件发送中/);
  assert.match(source, /请先补全 Offer/);
  assert.match(source, /setInterval\(poll, 2_000\)/);
  assert.match(source, /Offer 邮件已发送/);
  assert.match(source, /正在等待候选人确认/);
  assert.doesNotMatch(source, /PDF 生成中/);
  assert.doesNotMatch(source, /disabled=\{Boolean\(action\) \|\| !\(offer\.pdfReady \?\? offer\.pdf_ready\)\}/);
  assert.doesNotMatch(source, /发送功能暂未开放|Task 9 安全令牌流程接入后开放/);
});

test("expired approved Offer explains the conflict and exposes a revision path", () => {
  assert.match(source, /offer_response_deadline_expired/);
  assert.match(source, /候选人回复截止时间已过，请更新截止时间并重新提交审批/);
  assert.match(source, /候选人回复截止日期不能早于今天/);
  assert.match(source, /deadlineExpired/);
  assert.match(source, /当前版本不会进入发送队列/);
  assert.match(source, /更新候选人回复截止时间/);
  assert.match(source, /系统会为新版本重新发起审批/);
  assert.match(source, /"已加入发送队列", "Offer 发送"/);
  assert.match(source, /请先更新截止时间/);
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

test("HR proxy response is gated by sent status, server permission, and local Offer capability", () => {
  assert.match(source, /offer\.status === "sent" && canRenderOfferAction\(role, offer, "proxy_response"\)/);
  assert.match(source, /const localCapability = action === "decide" \? "审批 Offer" : "管理 Offer"/);
  assert.match(source, />代候选人确认</);
});

test("proxy response dialog validates audited fields and requires an explicit second confirmation", () => {
  assert.match(source, /createProxyResponsePayload\(values\)/);
  assert.match(source, /预计入职日期<span className="required-label">必填/);
  assert.match(source, /沟通渠道<span className="required-label">必填/);
  assert.match(source, /沟通时间<span className="required-label">必填/);
  assert.match(source, /备注（可选）/);
  assert.match(source, /请再次确认/);
  assert.match(source, /确认并登记/);
});

test("proxy response locks duplicate submission and refreshes a concurrent final result", () => {
  assert.match(source, /if \(submittingRef\.current\) return/);
  assert.match(source, /submittingRef\.current = true/);
  assert.match(source, /disabled=\{busy\}/);
  assert.match(source, /requestError\?\.code === "resource_version_conflict"/);
  assert.match(source, /\["accepted", "declined"\]\.includes\(latest\?\.status\)/);
  assert.match(source, /await onResolved\(latest, true\)/);
  assert.match(source, /已刷新最终结果/);
  assert.match(source, /proxyButtonRef\.current\?\.focus\(\)/);
  assert.match(source, /event\.key !== "Tab"/);
  assert.match(source, /document\.activeElement === last/);
});

test("accepted and declined results show immutable source and audit details", () => {
  assert.match(source, /accepted: "已接受"/);
  assert.match(source, /declined: "已拒绝"/);
  assert.match(source, /候选人自行确认/);
  assert.match(source, /HR 代为登记/);
  for (const label of ["操作人", "沟通渠道", "沟通时间", "到岗日期", "备注"]) assert.match(source, new RegExp(label));
  assert.match(source, /确认结果历史（不可编辑）/);
  assert.match(source, /responses\.map/);
});

test("accepted Offer exposes one guarded onboarding workspace without rendering full PII", () => {
  assert.match(source, /offer\.status === "accepted" && <OnboardingSection/);
  assert.match(source, /入职办理/);
  assert.match(source, /预计到岗日/);
  assert.match(source, /资料完整度/);
  assert.match(source, /maskedPhone/);
  assert.match(source, /maskedEmail/);
  assert.match(source, /onboarding\.complete && onboarding\.canSubmit/);
  assert.match(source, /预计到岗日 .* 到达后可办理入职/);
  assert.match(source, /实例编号/);
  assert.match(source, /重试办理入职/);
  assert.match(source, /feishu_onboarding_not_configured: "飞书入职审批尚未配置/);
  assert.match(source, /onboarding_gender_invalid: "候选人性别资料需要更正/);
  assert.match(source, /genderCorrectionOnly/);
  assert.match(source, /设置 → 飞书集成 → 入职审批/);
  assert.match(source, /controller\.updateOnboarding/);
  assert.match(source, /controller\.submitOnboarding/);
  assert.match(source, /setInterval\(async \(\) =>/);
  assert.match(source, /不会在页面回显明文/);
  assert.doesNotMatch(source, /onboarding\.phone|onboarding\.email(?!Error)/);
});

test("approval workbench loads independently and navigates to candidate Offer", () => {
  assert.match(source, /controller\.listPendingApprovals\(\)/);
  assert.match(source, /onOpenOffer\(task\)/);
  assert.match(appSource, /offerDetailPath\(task\.offerId \|\| task\.offer_id, task\.id, "\/workbench"\)/);
  const handler = appSource.match(/function openWorkbenchOfferTask\(task\) \{([\s\S]*?)\n  \}/)?.[1] || "";
  assert.doesNotMatch(handler, /loadServerCandidate|candidateDetailPath/);
  assert.match(source, /decisionApprovalId = approvalId \|\| offer\.pendingApprovalId/);
  assert.match(appSource, /<OfferApprovalTasks role=\{currentRole\} controller=\{offerController\}/);
  assert.match(source, /className="offer-task-heading"/);
  assert.match(source, /aria-label="刷新 Offer 审批"/);
  assert.match(source, /当前没有需要你处理的 Offer/);
  assert.match(source, /className="offer-task-list"/);
});

test("direct Offer route loads by offer id and renders server-projected names", () => {
  assert.match(appSource, /<CandidateOfferView offerId=\{route\.offerId\} approvalId=\{route\.approvalId\}/);
  assert.match(source, /offerId \? await controller\.getOffer\(offerId\) : await controller\.getApplicationOffer\(applicationId\)/);
  assert.match(source, /\[offer\.candidateName, offer\.jobTitle\]\.filter\(Boolean\)/);
});

test("Offer creation can use the job default template without template list success", () => {
  assert.match(source, /<option value="">使用职位默认模板<\/option>/);
  assert.doesNotMatch(source, /if \(!draft\.templateId\)/);
  assert.doesNotMatch(source, /templates\.find\(\(item\) => item\.status === "active"\)/);
  assert.match(source, /controller\.listTemplates\(\)\.catch\(\(\) => \[\]\)/);
});

test("draft updates explain immutable version behavior and conflicts preserve input", () => {
  assert.match(source, /保存会创建不可覆盖的新版本/);
  assert.match(source, /当前填写内容已保留，请刷新最新版本后核对/);
  assert.match(source, /controller\.updateDraft\(offer, payload\)/);
  assert.match(source, /offer_approver_required/);
  assert.match(source, /尚未配置默认 Offer 审批人/);
});

test("job form round-trips optional Offer defaults without blocking job save", () => {
  assert.match(jobSource, /offerApproverId: initialJob\?\.offerApproverId/);
  assert.match(jobSource, /offerTemplateId: initialJob\?\.offerTemplateId/);
  assert.match(jobSource, /aria-label="默认 Offer 审批人"/);
  assert.match(jobSource, /默认 Offer 审批人（领导）/);
  assert.match(jobSource, /不会自动使用用人经理/);
  assert.match(jobSource, /aria-label="默认 Offer 模板"/);
  assert.match(jobSource, /职位可暂不配置，但 HR 提交 Offer 审批前必须补充/);
  assert.doesNotMatch(jobSource, /reviewerSelectionInvalid \|\| !values\.offerApproverId/);
  assert.match(jobSource, /const offerApproverOptions = hiringManagers/);
  assert.doesNotMatch(jobSource, /offerApproverOptions = \[\.\.\.new Map\(\[\.\.\.recruiters/);
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
  assert.match(source, /filterEligibleSpecialApprovers\(users\)/);
  assert.match(source, /特殊 Offer 追加审批人（领导）/);
});
