import { useEffect, useMemo, useRef, useState } from "react";
import {
  AlertTriangle,
  ArrowDown,
  ArrowUp,
  CheckCircle2,
  ChevronRight,
  Clock3,
  FileCheck2,
  FileText,
  History,
  LoaderCircle,
  Plus,
  RefreshCw,
  Send,
  ShieldCheck,
  Trash2,
  Undo2,
  UserCheck,
  XCircle,
} from "lucide-react";
import { apiClient } from "./apiClient.js";
import { canPerformAction } from "./roleCapabilities.js";
import { createProxyResponsePayload, filterEligibleSpecialApprovers } from "./offerController.js";

const STATUS_LABELS = Object.freeze({
  draft: "草稿",
  pending_approval: "审批中",
  changes_requested: "需修改",
  ready_to_send: "待发送",
  sent: "已发送",
  accepted: "已接受",
  declined: "已拒绝",
  withdrawn: "已撤回",
  expired: "已过期",
});

const OFFER_PROGRESS_STEPS = Object.freeze(["填写 Offer", "审批", "发送", "候选人确认"]);

function offerProgressIndex(status) {
  if (["pending_approval"].includes(status)) return 1;
  if (["ready_to_send"].includes(status)) return 2;
  if (["sent", "accepted", "declined"].includes(status)) return 3;
  return 0;
}

function OfferProgress({ status = "draft" }) {
  const current = offerProgressIndex(status);
  return <ol className="offer-progress" aria-label="Offer 办理进度">
    {OFFER_PROGRESS_STEPS.map((label, index) => <li key={label} className={index < current ? "complete" : index === current ? "current" : ""} aria-current={index === current ? "step" : undefined}><span>{index < current ? <CheckCircle2 size={16} /> : index + 1}</span><strong>{label}</strong></li>)}
  </ol>;
}

export function offerStatusLabel(status) {
  return STATUS_LABELS[status] || "状态未知";
}

export function canRenderOfferAction(role, offer, action) {
  const localCapability = action === "decide" ? "审批 Offer" : "管理 Offer";
  return Boolean(offer?.allowedActions?.[action] ?? offer?.allowed_actions?.[action])
    && canPerformAction(role, localCapability);
}

export function offerErrorMessage(error, action = "操作") {
  if (error?.code === "resource_version_conflict") return "Offer 已被其他成员更新。当前填写内容已保留，请刷新最新版本后核对。";
  if (error?.code === "candidate_email_unconfirmed") return "候选人邮箱尚未确认，请先确认邮箱后再发送。";
  if (error?.code === "offer_send_unavailable") return "当前版本暂不可发送，请稍后重试。";
  if (error?.code === "offer_approver_required") return "当前职位尚未配置默认 Offer 审批人，请先编辑职位并完成配置后再提交。";
  if (error?.code === "offer_approver_ineligible") return "当前职位的默认 Offer 审批人已停用或无审批权限，请先编辑职位并重新选择。";
  if (error?.code === "invalid_offer_state") return "Offer 状态已变化，请刷新后重试。";
  if (error?.code === "OFFER_SPECIAL_REASON_REQUIRED") return "特殊 Offer 必须填写说明。";
  if (error?.code === "OFFER_PROXY_START_DATE_REQUIRED") return "登记接受时必须填写预计入职日期。";
  if (error?.code === "OFFER_PROXY_CHANNEL_REQUIRED") return "请选择实际沟通渠道。";
  if (error?.code === "OFFER_PROXY_COMMUNICATED_AT_REQUIRED") return "请填写实际沟通时间。";
  return `${action}未完成，请稍后重试。`;
}

const CHANNEL_LABELS = Object.freeze({ phone: "电话", wechat: "微信", email: "邮件", other: "其他" });

export function offerResponseSourceLabel(source) {
  if (source === "hr_proxy") return "HR 代为登记";
  if (["candidate", "candidate_self", "candidate_self_service"].includes(source)) return "候选人自行确认";
  return "结果来源未记录";
}

function displayDate(value, dateOnly = false) {
  if (!value) return "未记录";
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return value;
  return dateOnly ? date.toLocaleDateString("zh-CN") : date.toLocaleString("zh-CN", { hour12: false });
}

function OfferResponseRecord({ response, current = false }) {
  if (!response) return null;
  const status = response.status || response.decision;
  const source = response.source;
  const actor = response.actorName || response.actor_name || response.respondedByName || response.responded_by_name || response.actorId || response.actor_user_id;
  const channel = response.channel;
  const communicatedAt = response.communicatedAt || response.communicated_at;
  const startDate = response.expectedStartDate || response.expected_start_date;
  const note = response.note || response.reasonText || response.reason_text;
  return <article className={`offer-response-record${current ? " current" : ""}`}>
    <header><div><strong>{status === "accepted" ? "已接受 Offer" : status === "declined" ? "已拒绝 Offer" : "Offer 确认结果"}</strong><span>{offerResponseSourceLabel(source)}</span></div><small>{displayDate(response.respondedAt || response.responded_at || response.created_at)}</small></header>
    <dl>
      <div><dt>操作人</dt><dd>{source === "hr_proxy" ? actor || "HR（姓名未记录）" : "候选人"}</dd></div>
      <div><dt>沟通渠道</dt><dd>{CHANNEL_LABELS[channel] || (source === "hr_proxy" ? "未记录" : "候选人确认页")}</dd></div>
      <div><dt>沟通时间</dt><dd>{source === "hr_proxy" ? displayDate(communicatedAt) : displayDate(response.respondedAt || response.responded_at)}</dd></div>
      <div><dt>到岗日期</dt><dd>{startDate ? displayDate(startDate, true) : "不适用"}</dd></div>
      <div className="offer-response-note"><dt>备注</dt><dd>{note || "无"}</dd></div>
    </dl>
  </article>;
}

function deadlineInputValue(value) {
  if (!value) return "";
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return "";
  const offset = date.getTimezoneOffset() * 60_000;
  return new Date(date.getTime() - offset).toISOString().slice(0, 16);
}

function offerDraft(offer) {
  const content = offer?.content && offer.content.redacted !== true ? offer.content : {};
  return {
    templateId: offer?.templateId || offer?.template_id || "",
    title: content.title || "",
    body: content.body || "",
    compensation: content.compensation || "",
    benefits: content.benefits || "",
    candidateResponseDeadline: deadlineInputValue(offer?.candidateResponseDeadline || offer?.candidate_response_deadline),
    isSpecial: Boolean(offer?.isSpecial ?? offer?.is_special),
    specialReason: offer?.specialReason || offer?.special_reason || "",
  };
}

function draftPayload(draft) {
  return {
    templateId: draft.templateId || null,
    candidateResponseDeadline: new Date(draft.candidateResponseDeadline).toISOString(),
    content: {
      title: draft.title.trim(),
      body: draft.body.trim(),
      compensation: draft.compensation.trim(),
      benefits: draft.benefits.trim(),
    },
    isSpecial: draft.isSpecial,
    specialReason: draft.isSpecial ? draft.specialReason.trim() : null,
  };
}

export function hasOfferDraftChanges(offer, draft) {
  if (!offer) return true;
  return JSON.stringify(draftPayload(draft)) !== JSON.stringify(draftPayload(offerDraft(offer)));
}

function OfferHistory({ history }) {
  if (!history) return null;
  return <section className="offer-history" aria-labelledby="offer-history-title">
    <header><History size={18} /><h4 id="offer-history-title">版本与审批历史</h4></header>
    <div className="offer-version-list">
      {(history.versions || []).slice().reverse().map((version) => <article key={version.id}>
        <span className="offer-version-main"><strong>版本 {version.versionNumber ?? version.version_number}</strong><span>{version.pdfReady ?? version.pdf_ready ? "HTML Offer · PDF 可下载" : "HTML Offer"}</span></span>
        <small>{version.createdAt || version.created_at ? new Date(version.createdAt || version.created_at).toLocaleString("zh-CN", { hour12: false }) : "时间未记录"}</small>
      </article>)}
    </div>
    {(history.approvals || []).length > 0 && <ol className="offer-approval-history">
      {history.approvals.map((approval) => <li key={approval.id}><span>第 {approval.sequence} 步</span><strong>{approval.status === "approved" ? "已批准" : approval.status === "rejected" ? "要求修改" : "待审批"}</strong>{approval.reason && <p>{approval.reason}</p>}</li>)}
    </ol>}
    {(history.responses || []).length > 0 && <div className="offer-response-history" aria-label="候选人确认历史">
      <h5>确认结果历史（不可编辑）</h5>
      {history.responses.map((response, index) => <OfferResponseRecord key={response.id || `${response.source}-${response.respondedAt || index}`} response={response} />)}
    </div>}
  </section>;
}

function OfferApproverDialog({ offer, controller, onClose, onConfigured, onOfferChanged }) {
  const [state, setState] = useState({ status: "loading", options: [], selectedId: "", jobVersion: null, error: "" });
  const dialogRef = useRef(null);
  const titleRef = useRef(null);

  useEffect(() => {
    let active = true;
    queueMicrotask(() => titleRef.current?.focus());
    controller.listApproverOptions(offer.id).then(({ options, jobVersion }) => {
      if (!active) return;
      setState({ status: "ready", options, selectedId: options[0]?.id || "", jobVersion, error: options.length ? "" : "当前没有可用的 Offer 审批人，请先在组织成员中启用招聘管理员或用人经理。" });
    }).catch(() => {
      if (active) setState({ status: "error", options: [], selectedId: "", jobVersion: null, error: "审批人列表加载失败，请重试。" });
    });
    return () => { active = false; };
  }, [controller, offer.id]);

  useEffect(() => {
    function handleKeydown(event) {
      if (event.key === "Escape" && state.status !== "saving") { onClose(); return; }
      if (event.key !== "Tab") return;
      const focusable = [...(dialogRef.current?.querySelectorAll('button:not([disabled]), select:not([disabled]), [tabindex]:not([tabindex="-1"])') || [])];
      if (!focusable.length) return;
      const first = focusable[0];
      const last = focusable.at(-1);
      if (event.shiftKey && (document.activeElement === first || document.activeElement === titleRef.current)) { event.preventDefault(); last.focus(); }
      else if (!event.shiftKey && document.activeElement === last) { event.preventDefault(); first.focus(); }
    }
    document.addEventListener("keydown", handleKeydown);
    return () => document.removeEventListener("keydown", handleKeydown);
  }, [onClose, state.status]);

  async function save() {
    if (!state.selectedId || state.status === "saving") return;
    setState((current) => ({ ...current, status: "saving", error: "" }));
    try {
      await controller.setDefaultApprover(offer, state.selectedId, state.jobVersion);
      await onConfigured();
    } catch (error) {
      if (error?.code === "resource_version_conflict" && error.latestOffer) {
        onOfferChanged(error.latestOffer);
        return;
      }
      if (error?.code === "job_version_conflict") {
        try {
          const { options, jobVersion } = await controller.listApproverOptions(offer.id);
          setState((current) => ({
            ...current,
            status: "ready",
            options,
            selectedId: options.some((item) => item.id === current.selectedId) ? current.selectedId : options[0]?.id || "",
            jobVersion,
            error: "职位配置已被其他成员更新，请核对审批人后再次保存。",
          }));
        } catch {
          setState((current) => ({ ...current, status: "error", error: "职位配置已更新，但审批人列表刷新失败。请取消后重试。" }));
        }
        return;
      }
      setState((current) => ({
        ...current,
        status: "ready",
        error: error?.code === "offer_approver_ineligible"
          ? "该成员已停用或不具备 Offer 审批权限，请选择其他成员。"
          : offerErrorMessage(error, "保存默认审批人"),
      }));
    }
  }

  return <div className="offer-proxy-backdrop" onMouseDown={(event) => { if (event.target === event.currentTarget && state.status !== "saving") onClose(); }}>
    <section ref={dialogRef} className="offer-proxy-dialog offer-approver-dialog" role="dialog" aria-modal="true" aria-labelledby="offer-approver-title" aria-describedby="offer-approver-description">
      <header><div><h2 id="offer-approver-title" ref={titleRef} tabIndex="-1"><UserCheck size={20} />设置默认审批人</h2><p id="offer-approver-description">该职位尚未配置有效的 Offer 审批人。保存后会自动继续本次提交。</p></div><button type="button" aria-label="关闭审批人设置" disabled={state.status === "saving"} onClick={onClose}>×</button></header>
      <div className="offer-proxy-body">
        <div className="offer-approver-context"><ShieldCheck size={19} /><span><strong>职位默认配置</strong><small>后续该职位的普通 Offer 将优先提交给此人审批。</small></span></div>
        {state.status === "loading" ? <div className="offer-approver-loading" role="status"><LoaderCircle className="spin" size={18} />正在加载可选审批人</div> : <label>默认 Offer 审批人<select aria-label="默认 Offer 审批人" value={state.selectedId} disabled={state.status === "saving" || !state.options.length} onChange={(event) => setState((current) => ({ ...current, selectedId: event.target.value, error: "" }))}><option value="">请选择审批人</option>{state.options.map((item) => <option key={item.id} value={item.id}>{item.name}</option>)}</select><small>仅显示已启用的招聘管理员和用人经理。</small></label>}
        {state.error && <div className="offer-error" role="alert"><AlertTriangle size={17} />{state.error}</div>}
        <footer><button className="button secondary" type="button" disabled={state.status === "saving"} onClick={onClose}>取消</button><button className="button primary" type="button" disabled={state.status !== "ready" || !state.selectedId} onClick={() => void save()}>{state.status === "saving" ? "保存并提交中…" : "保存并继续提交"}</button></footer>
      </div>
    </section>
  </div>;
}

function ProxyResponseDialog({ offer, controller, onClose, onResolved, onNotify }) {
  const [values, setValues] = useState({ decision: "accepted", expectedStartDate: "", channel: "", communicatedAt: deadlineInputValue(new Date().toISOString()), note: "" });
  const [confirming, setConfirming] = useState(false);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const titleRef = useRef(null);
  const dialogRef = useRef(null);
  const submittingRef = useRef(false);

  useEffect(() => {
    titleRef.current?.focus();
    function handleDialogKeydown(event) {
      if (event.key === "Escape" && !submittingRef.current) { onClose(); return; }
      if (event.key !== "Tab") return;
      const focusable = [...(dialogRef.current?.querySelectorAll('button:not([disabled]), input:not([disabled]), select:not([disabled]), textarea:not([disabled]), [tabindex]:not([tabindex="-1"])') || [])];
      if (focusable.length === 0) return;
      const first = focusable[0];
      const last = focusable.at(-1);
      if (event.shiftKey && document.activeElement === first) { event.preventDefault(); last.focus(); }
      else if (!event.shiftKey && document.activeElement === last) { event.preventDefault(); first.focus(); }
    }
    document.addEventListener("keydown", handleDialogKeydown);
    return () => document.removeEventListener("keydown", handleDialogKeydown);
  }, [onClose]);

  function change(field, value) {
    setValues((current) => ({ ...current, [field]: value }));
    setError("");
    setConfirming(false);
  }

  function continueToConfirmation(event) {
    event.preventDefault();
    try {
      createProxyResponsePayload(values);
      setConfirming(true);
    } catch (validationError) {
      setError(offerErrorMessage(validationError, "校验"));
    }
  }

  async function submit() {
    if (submittingRef.current) return;
    submittingRef.current = true;
    setBusy(true);
    setError("");
    try {
      const saved = await controller.proxyResponse(offer, values);
      await onResolved(saved, false);
      onNotify("候选人确认结果已登记");
      onClose();
    } catch (requestError) {
      const latest = requestError?.latestOffer;
      if (requestError?.code === "resource_version_conflict" && ["accepted", "declined"].includes(latest?.status)) {
        await onResolved(latest, true);
        onNotify("Offer 已由其他操作确认，已刷新最终结果");
        onClose();
      } else {
        setError(offerErrorMessage(requestError, "代候选人确认"));
      }
    } finally {
      submittingRef.current = false;
      setBusy(false);
    }
  }

  return <div className="offer-proxy-backdrop" onMouseDown={(event) => { if (event.target === event.currentTarget && !busy) onClose(); }}>
    <section ref={dialogRef} className="offer-proxy-dialog" role="dialog" aria-modal="true" aria-labelledby="offer-proxy-title" aria-describedby="offer-proxy-description">
      <header><div><h2 id="offer-proxy-title" ref={titleRef} tabIndex="-1">代候选人确认</h2><p id="offer-proxy-description">仅登记已通过线下沟通确认的最终结果，提交后不可编辑。</p></div><button type="button" aria-label="关闭代候选人确认对话框" disabled={busy} onClick={onClose}>×</button></header>
      {!confirming ? <form className="offer-proxy-body" onSubmit={continueToConfirmation}>
        <fieldset disabled={busy}><legend>候选人决定</legend><label><input type="radio" name="proxy-decision" checked={values.decision === "accepted"} onChange={() => change("decision", "accepted")} />接受</label><label><input type="radio" name="proxy-decision" checked={values.decision === "declined"} onChange={() => change("decision", "declined")} />拒绝</label></fieldset>
        {values.decision === "accepted" && <label>预计入职日期<span className="required-label">必填</span><input aria-label="预计入职日期" type="date" required disabled={busy} value={values.expectedStartDate} onChange={(event) => change("expectedStartDate", event.target.value)} /></label>}
        <label>沟通渠道<span className="required-label">必填</span><select aria-label="沟通渠道" required disabled={busy} value={values.channel} onChange={(event) => change("channel", event.target.value)}><option value="">请选择</option><option value="phone">电话</option><option value="wechat">微信</option><option value="email">邮件</option><option value="other">其他</option></select></label>
        <label>沟通时间<span className="required-label">必填</span><input aria-label="沟通时间" type="datetime-local" required disabled={busy} value={values.communicatedAt} onChange={(event) => change("communicatedAt", event.target.value)} /></label>
        <label>备注（可选）<textarea aria-label="备注" rows="4" maxLength="2000" disabled={busy} value={values.note} onChange={(event) => change("note", event.target.value)} /></label>
        {error && <p className="offer-error" role="alert"><AlertTriangle size={16} />{error}</p>}
        <footer><button className="button secondary" type="button" disabled={busy} onClick={onClose}>取消</button><button className="button primary" type="submit" disabled={busy}>下一步：确认登记</button></footer>
      </form> : <div className="offer-proxy-confirmation">
        <div role="status"><AlertTriangle size={20} /><span><strong>请再次确认</strong><small>该操作立即生效，并作为不可编辑的审计历史保存。</small></span></div>
        <dl><div><dt>决定</dt><dd>{values.decision === "accepted" ? "接受 Offer" : "拒绝 Offer"}</dd></div>{values.decision === "accepted" && <div><dt>预计入职日期</dt><dd>{displayDate(values.expectedStartDate, true)}</dd></div>}<div><dt>沟通渠道</dt><dd>{CHANNEL_LABELS[values.channel]}</dd></div><div><dt>沟通时间</dt><dd>{displayDate(values.communicatedAt)}</dd></div><div><dt>备注</dt><dd>{values.note.trim() || "无"}</dd></div></dl>
        {error && <p className="offer-error" role="alert"><AlertTriangle size={16} />{error}</p>}
        <footer><button className="button secondary" type="button" disabled={busy} onClick={() => setConfirming(false)}>返回修改</button><button className="button primary" type="button" disabled={busy} onClick={() => void submit()}>{busy ? "登记中…" : "确认并登记"}</button></footer>
      </div>}
    </section>
  </div>;
}

function OfferDraftForm({ offer, templates, applicationId, controller, role, onSaved, onNotify }) {
  const [draft, setDraft] = useState(() => offerDraft(offer));
  const [status, setStatus] = useState("idle");
  const [error, setError] = useState("");
  const [latestConflictOffer, setLatestConflictOffer] = useState(null);
  const [approverSetupOffer, setApproverSetupOffer] = useState(null);
  const errorRef = useRef(null);
  const submitButtonRef = useRef(null);

  useEffect(() => {
    setDraft(offerDraft(offer));
    setLatestConflictOffer(null);
    setError("");
  }, [offer?.id, offer?.version]);
  useEffect(() => { if (error) errorRef.current?.focus(); }, [error]);

  function change(field, value) {
    setDraft((current) => ({ ...current, [field]: value }));
    setError("");
  }

  function validate() {
    if (!draft.title.trim() || !draft.body.trim() || !draft.compensation.trim()) return "请完整填写标题、正文和薪酬方案。";
    if (!draft.candidateResponseDeadline || Number.isNaN(new Date(draft.candidateResponseDeadline).getTime())) return "请设置候选人回复截止时间。";
    if (draft.isSpecial && !draft.specialReason.trim()) return "特殊 Offer 必须填写说明。";
    return "";
  }

  async function save({ submitAfter = false } = {}) {
    const validation = validate();
    if (validation) { setError(validation); return null; }
    if (offer && !hasOfferDraftChanges(offer, draft)) {
      if (!submitAfter) onNotify("当前 Offer 内容没有修改");
      return offer;
    }
    setStatus("saving"); setError("");
    try {
      const payload = draftPayload(draft);
      const saved = offer
        ? await controller.updateDraft(offer, payload)
        : await controller.createOffer(applicationId, payload);
      onSaved(saved);
      onNotify(offer ? "已创建新的 Offer 版本" : "Offer 草稿已创建");
      return saved;
    } catch (requestError) {
      const latest = requestError?.latestOffer;
      if (requestError?.code === "resource_version_conflict" && latest) {
        if (!["draft", "changes_requested"].includes(latest.status)) {
          onSaved(latest);
          onNotify("Offer 状态已更新，已加载最新审批进度");
          return null;
        }
        setLatestConflictOffer(latest);
      }
      setError(offerErrorMessage(requestError, "保存"));
      return null;
    } finally { setStatus("idle"); }
  }

  async function submitSavedOffer(saved, { allowApproverSetup = true } = {}) {
    setStatus("submitting"); setError("");
    try {
      const submitted = await controller.submitApproval(saved);
      onSaved(submitted);
      onNotify("Offer 已提交审批；审批通过后仍需 HR 明确发送");
    } catch (requestError) {
      const latest = requestError?.latestOffer;
      if (requestError?.code === "resource_version_conflict" && latest && !["draft", "changes_requested"].includes(latest.status)) {
        onSaved(latest);
        onNotify("Offer 状态已更新，已加载最新审批进度");
      } else if (allowApproverSetup && ["offer_approver_required", "offer_approver_ineligible"].includes(requestError?.code)) {
        setApproverSetupOffer(saved);
      } else {
        setError(offerErrorMessage(requestError, "提交审批"));
      }
    }
    finally { setStatus("idle"); }
  }

  async function submit() {
    const saved = await save({ submitAfter: true });
    if (!saved) return;
    await submitSavedOffer(saved);
  }

  function closeApproverSetup() {
    setApproverSetupOffer(null);
    queueMicrotask(() => submitButtonRef.current?.focus());
  }

  const busy = status !== "idle";
  const canSubmit = !offer || canRenderOfferAction(role, offer, "submit");
  return <form className="offer-form" onSubmit={(event) => { event.preventDefault(); void save(); }}>
    <div className="offer-form-grid">
      <label className="offer-compact-field">Offer 模板<select aria-label="Offer 模板" disabled={busy} value={draft.templateId} onChange={(event) => change("templateId", event.target.value)}><option value="">使用职位默认模板</option>{templates.map((item) => <option key={item.id} value={item.id} disabled={item.status === "inactive" && item.id !== draft.templateId}>{item.name}{item.status === "inactive" ? "（已停用）" : ""}</option>)}</select><small>{templates.length ? "可覆盖职位默认模板。" : "未加载到可选模板，将使用职位默认模板。"}</small></label>
      <label className="offer-compact-field">候选人回复截止时间<input aria-label="候选人回复截止时间" required type="datetime-local" disabled={busy} value={draft.candidateResponseDeadline} onChange={(event) => change("candidateResponseDeadline", event.target.value)} /><small>候选人需要在此时间前完成确认。</small></label>
      <label className="offer-full-field">Offer 标题<input aria-label="Offer 标题" required disabled={busy} value={draft.title} onChange={(event) => change("title", event.target.value)} placeholder="例如：正式录用通知" /></label>
      <label className="offer-full-field">Offer 正文<textarea aria-label="Offer 正文" required rows="7" disabled={busy} value={draft.body} onChange={(event) => change("body", event.target.value)} placeholder="填写岗位、汇报关系、入职安排等内容" /></label>
      <label>薪酬方案<textarea aria-label="薪酬方案" required rows="4" disabled={busy} value={draft.compensation} onChange={(event) => change("compensation", event.target.value)} /></label>
      <label>福利与补充说明<textarea aria-label="福利与补充说明" rows="4" disabled={busy} value={draft.benefits} onChange={(event) => change("benefits", event.target.value)} /></label>
    </div>
    <div className="offer-special-row"><span><ShieldCheck size={18} /><span><strong>特殊 Offer</strong><small>开启后会在岗位默认审批人之后追加组织固定审批链。</small></span></span><label className="compact-switch"><input aria-label="特殊 Offer" type="checkbox" disabled={busy} checked={draft.isSpecial} onChange={(event) => change("isSpecial", event.target.checked)} /><span aria-hidden="true" /></label></div>
    {draft.isSpecial && <label className="offer-special-reason">特殊 Offer 说明<span className="required-label">必填</span><textarea aria-label="特殊 Offer 说明" required rows="3" disabled={busy} value={draft.specialReason} onChange={(event) => change("specialReason", event.target.value)} placeholder="说明为何需要追加特殊审批" /></label>}
    {error && <div ref={errorRef} tabIndex="-1" className="offer-error" role="alert"><AlertTriangle size={17} />{error}{error.includes("刷新") && offer && <button type="button" onClick={() => { setError(""); latestConflictOffer ? onSaved(latestConflictOffer) : onSaved(null); }}>刷新最新版本</button>}</div>}
    <footer><span>{offer ? `保存会创建不可覆盖的新版本；当前主记录版本 ${offer.version}` : "创建后可预览并提交审批。"}</span><div><button className="button secondary" type="submit" disabled={busy}>{status === "saving" ? "保存中…" : offer ? "保存新版本" : "创建草稿"}</button>{canSubmit && <button ref={submitButtonRef} className="button primary" type="button" disabled={busy} onClick={() => void submit()}>{status === "submitting" ? "提交中…" : "保存并提交审批"}</button>}</div></footer>
    {approverSetupOffer && <OfferApproverDialog offer={approverSetupOffer} controller={controller} onClose={closeApproverSetup} onOfferChanged={(latest) => { setApproverSetupOffer(null); onSaved(latest); onNotify("Offer 状态已更新，已加载最新进度"); }} onConfigured={async () => { const pending = approverSetupOffer; setApproverSetupOffer(null); onNotify("默认 Offer 审批人已保存"); await submitSavedOffer(pending, { allowApproverSetup: false }); }} />}
  </form>;
}

function ApprovalDecision({ offer, approvalId, controller, onSaved, onNotify }) {
  const [reason, setReason] = useState("");
  const [busy, setBusy] = useState("");
  const [error, setError] = useState("");
  async function decide(decision) {
    if (decision === "rejected" && !reason.trim()) { setError("要求修改时必须填写原因。"); return; }
    setBusy(decision); setError("");
    try {
      const saved = decision === "approved"
        ? await controller.approve(approvalId, offer)
        : await controller.requestChanges(approvalId, offer, reason);
      onSaved(saved);
      onNotify(decision === "approved" ? "Offer 已批准；不会自动发送" : "已退回 HR 修改");
    } catch (requestError) { setError(offerErrorMessage(requestError, "审批")); }
    finally { setBusy(""); }
  }
  return <section className="offer-decision" aria-labelledby="offer-decision-title"><h4 id="offer-decision-title">审批决策</h4><label>修改原因（要求修改时必填）<textarea rows="3" disabled={Boolean(busy)} value={reason} onChange={(event) => { setReason(event.target.value); setError(""); }} /></label>{error && <p className="offer-error" role="alert"><AlertTriangle size={16} />{error}</p>}<div><button className="button secondary" type="button" disabled={Boolean(busy)} onClick={() => void decide("rejected")}><Undo2 size={16} />{busy === "rejected" ? "提交中…" : "要求修改"}</button><button className="button primary" type="button" disabled={Boolean(busy)} onClick={() => void decide("approved")}><CheckCircle2 size={16} />{busy === "approved" ? "提交中…" : "批准 Offer"}</button></div></section>;
}

export function CandidateOfferView({ candidate, offerId, role, controller, approvalId, onNotify = () => {} }) {
  const applicationId = candidate?.application?.id || candidate?.applicationId;
  const [state, setState] = useState({ status: "loading", offer: null, templates: [], history: null, error: "" });
  const [action, setAction] = useState("");
  const [proxyOpen, setProxyOpen] = useState(false);
  const proxyButtonRef = useRef(null);

  function closeProxyDialog() {
    setProxyOpen(false);
    queueMicrotask(() => proxyButtonRef.current?.focus());
  }

  async function load() {
    if (!offerId && !applicationId) { setState({ status: "error", offer: null, templates: [], history: null, error: "当前页面没有可用 Offer 或申请标识。" }); return; }
    setState((current) => ({ ...current, status: "loading", error: "" }));
    try {
      const offer = offerId ? await controller.getOffer(offerId) : await controller.getApplicationOffer(applicationId);
      const [templates, history] = await Promise.all([
        canPerformAction(role, "管理 Offer") ? controller.listTemplates().catch(() => []) : Promise.resolve([]),
        offer ? controller.listHistory(offer.id).catch(() => null) : Promise.resolve(null),
      ]);
      setState({ status: "ready", offer, templates, history, error: "" });
    } catch { setState((current) => ({ ...current, status: "error", error: "Offer 加载失败，请重试。" })); }
  }

  useEffect(() => { void load(); }, [applicationId, offerId, controller, role]);

  async function run(name, command, message) {
    setAction(name);
    try { const offer = await command(); setState((current) => ({ ...current, offer })); onNotify(message); await load(); }
    catch (error) { setState((current) => ({ ...current, error: offerErrorMessage(error, message) })); }
    finally { setAction(""); }
  }

  if (state.status === "loading" && !state.offer) return <div className="offer-state" role="status"><LoaderCircle className="spin" size={24} /><strong>正在加载 Offer</strong><span>正在读取当前版本和审批状态。</span></div>;
  if (state.status === "error" && !state.offer) return <div className="offer-state error" role="alert"><AlertTriangle size={24} /><strong>Offer 无法加载</strong><span>{state.error}</span><button className="button secondary" type="button" onClick={() => void load()}><RefreshCw size={16} />重试</button></div>;

  const offer = state.offer;
  if (!offer) return <div className="offer-workspace"><header className="offer-heading"><div><h3>Offer 管理</h3><p>填写并提交审批；审批通过后由 HR 核对并发送给候选人。</p></div></header><OfferProgress />{canPerformAction(role, "管理 Offer") ? <OfferDraftForm applicationId={applicationId} templates={state.templates} controller={controller} role={role} onSaved={(saved) => saved ? setState((current) => ({ ...current, offer: saved })) : void load()} onNotify={onNotify} /> : <div className="offer-state"><FileText size={24} /><strong>暂无 Offer</strong><span>当前角色不能创建 Offer。</span></div>}</div>;

  const sensitive = Boolean(offer.canViewSensitiveContent ?? offer.can_view_sensitive_content);
  const contentReady = Boolean(offer.contentReady ?? offer.content_ready);
  const sendQueued = Boolean(offer.sendQueued ?? offer.send_queued);
  const decisionApprovalId = approvalId || offer.pendingApprovalId || offer.pending_approval_id;
  const canDecide = canRenderOfferAction(role, offer, "decide") && Boolean(decisionApprovalId);
  return <div className="offer-workspace">
    <header className="offer-heading"><div><h3>Offer 管理</h3><p>{[offer.candidateName, offer.jobTitle].filter(Boolean).join(" · ") || "Offer 版本、审批、发送和候选人招聘状态相互独立。"}</p></div><span className={`offer-status ${offer.status}`}>{offerStatusLabel(offer.status)}</span></header>
    <OfferProgress status={offer.status} />
    {state.error && <div className="offer-error" role="alert"><AlertTriangle size={17} />{state.error}<button type="button" onClick={() => void load()}>刷新</button></div>}
    <div className="offer-summary">
      <span><FileCheck2 size={18} /><span><small>当前版本</small><strong>v{offer.currentVersionNumber ?? offer.current_version_number ?? "—"}</strong></span></span>
      <span><Clock3 size={18} /><span><small>回复截止</small><strong>{new Date(offer.candidateResponseDeadline || offer.candidate_response_deadline).toLocaleString("zh-CN", { hour12: false })}</strong></span></span>
      <span><FileText size={18} /><span><small>候选人页面</small><strong>HTML Offer</strong></span></span>
    </div>
    {offer.status === "pending_approval" && <div className="offer-pending-notice" role="status"><Clock3 size={20} /><span><strong>{canDecide ? "该 Offer 等待你审批" : "Offer 已提交审批"}</strong><small>{canDecide ? "请核对下方 Offer 内容并完成审批；批准后将交由 HR 明确发送。" : "当前暂不需要 HR 操作。审批通过后，本页会出现“确认并发送 Offer”按钮。"}</small></span><button className="button secondary" type="button" disabled={state.status === "loading"} onClick={() => void load()}><RefreshCw size={16} />刷新状态</button></div>}
    {offer.status === "ready_to_send" && <div className="offer-ready-notice" role="status"><CheckCircle2 size={20} /><span><strong>{sendQueued ? "发送请求已提交" : contentReady ? "审批已完成，但尚未发送" : "Offer 信息不完整"}</strong><small>{sendQueued ? "邮件正在投递，发送完成后状态会自动更新。" : !contentReady ? "请返回修改并补全 Offer 标题、正文和薪酬方案。" : canRenderOfferAction(role, offer, "send") ? "发送功能已开放。系统不会自动发送，请 HR 核对 HTML Offer 页面和候选人邮箱后明确点击发送。" : "当前账号无发送权限，请联系负责 HR 操作。"}</small></span></div>}
    {offer.status === "changes_requested" && <div className="offer-changes-notice"><Undo2 size={20} /><span><strong>审批人要求修改</strong><small>{state.history?.approvals?.slice().reverse().find((item) => item.status === "rejected")?.reason || "请查看下方审批历史。"}</small></span></div>}
    {["accepted", "declined"].includes(offer.status) && <section className="offer-result" aria-labelledby="offer-result-title"><h4 id="offer-result-title">最终确认结果</h4><OfferResponseRecord response={offer.response || state.history?.responses?.at(-1)} current /></section>}
    {!sensitive ? <div className="offer-redacted" role="status"><ShieldCheck size={22} /><strong>敏感内容已由服务端隐藏</strong><span>当前账号只能查看 Offer 状态，薪酬和正文不会在浏览器中展示。</span></div> : <>
      {["draft", "changes_requested"].includes(offer.status) && canRenderOfferAction(role, offer, "update") && <OfferDraftForm offer={offer} templates={state.templates} applicationId={applicationId} controller={controller} role={role} onSaved={(saved) => saved ? setState((current) => ({ ...current, offer: saved })) : void load()} onNotify={onNotify} />}
      {!(["draft", "changes_requested"].includes(offer.status) && canRenderOfferAction(role, offer, "update")) && <section className="offer-preview" aria-labelledby="offer-preview-title"><header><h4 id="offer-preview-title">Offer 预览</h4>{offer.isSpecial ?? offer.is_special ? <span>特殊 Offer</span> : null}</header><h5>{offer.content?.title || "正式录用通知"}</h5><p className="offer-body-copy">{offer.content?.body || "正文未填写"}</p><dl><div><dt>薪酬方案</dt><dd>{offer.content?.compensation || "未填写"}</dd></div><div><dt>福利与补充说明</dt><dd>{offer.content?.benefits || "未填写"}</dd></div>{(offer.specialReason || offer.special_reason) && <div><dt>特殊说明</dt><dd>{offer.specialReason || offer.special_reason}</dd></div>}</dl></section>}
    </>}
    {canDecide && <ApprovalDecision offer={offer} approvalId={decisionApprovalId} controller={controller} onSaved={(saved) => setState((current) => ({ ...current, offer: saved }))} onNotify={onNotify} />}
    <div className="offer-actions">
      {canRenderOfferAction(role, offer, "withdraw") && <button className="button secondary" type="button" disabled={Boolean(action)} onClick={() => void run("withdraw", () => controller.withdraw(offer), "Offer 已撤回")}><XCircle size={16} />{action === "withdraw" ? "撤回中…" : "撤回 Offer"}</button>}
      {canRenderOfferAction(role, offer, "send") && <button className="button primary" type="button" disabled={Boolean(action)} onClick={() => void run("send", () => controller.send(offer), "已加入发送队列")}><Send size={16} />{action === "send" ? "提交中…" : "确认并发送 Offer"}</button>}
      {offer.status === "sent" && canRenderOfferAction(role, offer, "proxy_response") && <button ref={proxyButtonRef} className="button primary" type="button" disabled={Boolean(action)} onClick={() => setProxyOpen(true)}><CheckCircle2 size={16} />代候选人确认</button>}
      {offer.status === "ready_to_send" && !canRenderOfferAction(role, offer, "send") && <button className="button secondary" type="button" disabled aria-disabled="true"><Send size={16} />{sendQueued ? "邮件发送中" : contentReady ? "当前账号无发送权限" : "请先补全 Offer"}</button>}
    </div>
    <OfferHistory history={state.history} />
    {proxyOpen && <ProxyResponseDialog offer={offer} controller={controller} onClose={closeProxyDialog} onResolved={async (saved) => { setState((current) => ({ ...current, offer: saved, error: "" })); await load(); }} onNotify={onNotify} />}
  </div>;
}

export function OfferApprovalTasks({ role, controller, onOpenOffer }) {
  const [state, setState] = useState({ status: "loading", records: [], error: "" });
  async function load() {
    if (!canPerformAction(role, "审批 Offer")) { setState({ status: "ready", records: [], error: "" }); return; }
    setState((current) => ({ ...current, status: "loading", error: "" }));
    try { setState({ status: "ready", records: await controller.listPendingApprovals(), error: "" }); }
    catch { setState({ status: "error", records: [], error: "Offer 审批待办加载失败。" }); }
  }
  useEffect(() => { void load(); }, [controller, role]);
  if (!canPerformAction(role, "审批 Offer")) return null;
  const hasTasks = state.records.length > 0;
  const statusClass = state.status === "loading" ? "is-loading" : state.status === "error" ? "is-error" : hasTasks ? "has-items" : "is-empty";
  const statusText = state.status === "loading"
    ? "正在读取最新审批任务"
    : state.status === "error"
      ? state.error
      : hasTasks
        ? String(state.records.length) + " 份 Offer 等待你的决定"
        : "当前没有需要你处理的 Offer";
  return <section className={"offer-task-section " + statusClass} aria-labelledby="offer-task-title" aria-busy={state.status === "loading"}>
    <header>
      <div className="offer-task-heading">
        <span className="offer-task-icon" aria-hidden="true">{state.status === "loading" ? <LoaderCircle size={21} /> : state.status === "error" ? <AlertTriangle size={21} /> : hasTasks ? <FileCheck2 size={21} /> : <CheckCircle2 size={21} />}</span>
        <div><h3 id="offer-task-title">待我审批的 Offer</h3><p role={state.status === "error" ? "alert" : "status"}>{statusText}</p></div>
      </div>
      <button className="icon-button offer-task-refresh" type="button" title="刷新 Offer 审批" aria-label="刷新 Offer 审批" disabled={state.status === "loading"} onClick={() => void load()}><RefreshCw size={17} /></button>
    </header>
    {hasTasks && <div className="offer-task-list">{state.records.map((task) => <button type="button" key={task.id} onClick={() => onOpenOffer(task)}><span className="offer-task-item-icon" aria-hidden="true"><FileText size={18} /></span><span className="offer-task-item-main"><strong>{task.candidateName || task.candidate_name}</strong><small>{task.jobTitle || task.job_title} · Offer v{task.versionNumber || task.version_number}</small></span><span className="offer-task-deadline"><small>回复截止</small><strong>{new Date(task.candidateResponseDeadline || task.candidate_response_deadline).toLocaleDateString("zh-CN")}</strong></span><ChevronRight size={18} aria-hidden="true" /></button>)}</div>}
  </section>;
}

function moveItem(items, index, offset) {
  const target = index + offset;
  if (target < 0 || target >= items.length) return items;
  const next = [...items];
  [next[index], next[target]] = [next[target], next[index]];
  return next;
}

export function OfferSettings({ controller, onNotify = () => {}, onDirtyChange = () => {} }) {
  const [state, setState] = useState({ status: "loading", templates: [], approvers: { approverIds: [], version: 0 }, users: [], error: "" });
  const [selectedId, setSelectedId] = useState("");
  const [templateDraft, setTemplateDraft] = useState({ name: "", body: "", status: "active" });
  const [approverIds, setApproverIds] = useState([]);
  const [busy, setBusy] = useState("");
  const preserveTemplateDraftRef = useRef(false);
  const selected = state.templates.find((item) => item.id === selectedId);
  const dirty = Boolean(selected && (selected.name !== templateDraft.name || (selected.content?.body || "") !== templateDraft.body || selected.status !== templateDraft.status))
    || JSON.stringify(approverIds) !== JSON.stringify(state.approvers.approverIds || state.approvers.approver_ids || []);

  useEffect(() => onDirtyChange(dirty), [dirty, onDirtyChange]);
  useEffect(() => {
    if (preserveTemplateDraftRef.current) { preserveTemplateDraftRef.current = false; return; }
    if (selected) setTemplateDraft({ name: selected.name, body: selected.content?.body || "", status: selected.status });
  }, [selectedId, selected?.version]);

  async function load() {
    setState((current) => ({ ...current, status: "loading", error: "" }));
    try {
      const [templates, approvers, users] = await Promise.all([controller.listTemplates(), controller.getSpecialApprovers(), apiClient.listUsers()]);
      const eligible = filterEligibleSpecialApprovers(users);
      setState({ status: "ready", templates, approvers, users: eligible, error: "" });
      setSelectedId((current) => templates.some((item) => item.id === current) ? current : templates[0]?.id || "");
      setApproverIds(approvers.approverIds || approvers.approver_ids || []);
    } catch { setState((current) => ({ ...current, status: "error", error: "Offer 设置加载失败，请检查权限后重试。" })); }
  }
  useEffect(() => { void load(); }, [controller]);

  async function saveTemplate() {
    if (!templateDraft.name.trim() || !templateDraft.body.trim()) { setState((current) => ({ ...current, error: "请填写模板名称和正文。" })); return; }
    setBusy("template");
    try {
      const payload = { name: templateDraft.name, content: { body: templateDraft.body }, status: templateDraft.status };
      const saved = selected ? await controller.updateTemplate(selected, payload) : await controller.createTemplate(payload);
      onNotify(selected ? "Offer 模板已保存" : "Offer 模板已创建");
      await load(); setSelectedId(saved.id);
    } catch (error) {
      if (error?.latestTemplate) preserveTemplateDraftRef.current = true;
      setState((current) => ({
        ...current,
        templates: error?.latestTemplate ? current.templates.map((item) => item.id === error.latestTemplate.id ? error.latestTemplate : item) : current.templates,
        error: error?.code === "resource_version_conflict" ? "模板已被其他管理员更新。当前填写内容已保留，已刷新版本基线，请核对后再次保存。" : offerErrorMessage(error, "保存模板"),
      }));
    }
    finally { setBusy(""); }
  }

  async function saveApprovers() {
    setBusy("approvers");
    try { const saved = await controller.updateSpecialApprovers(state.approvers, approverIds); setState((current) => ({ ...current, approvers: saved, error: "" })); setApproverIds(saved.approverIds || saved.approver_ids || []); onNotify("特殊 Offer 审批顺序已保存"); }
    catch (error) {
      setState((current) => ({
        ...current,
        approvers: error?.latestSettings || current.approvers,
        error: error?.code === "resource_version_conflict" ? "审批顺序已被其他管理员更新。当前排序已保留，已刷新版本基线，请核对后再次保存。" : offerErrorMessage(error, "保存审批顺序"),
      }));
    }
    finally { setBusy(""); }
  }

  return <div className="settings-section offer-settings"><div className="settings-section-heading"><div><h2><FileCheck2 size={21} />Offer 设置</h2><p>维护企业 Offer 模板和特殊 Offer 的固定追加审批顺序。</p></div>{state.status === "error" && <button className="button secondary" type="button" onClick={() => void load()}><RefreshCw size={16} />重新加载</button>}</div>{state.error && <div className="settings-error" role="alert"><AlertTriangle size={17} />{state.error}</div>}{state.status === "loading" ? <div className="organization-state" role="status"><LoaderCircle className="spin" size={18} />正在加载 Offer 设置</div> : <>
    <section className="offer-template-settings" aria-labelledby="offer-template-settings-title"><header><div><h3 id="offer-template-settings-title">Offer 模板</h3><p>模板保存为结构化内容，职位可选择启用中的默认模板。</p></div><button className="button secondary" type="button" onClick={() => { setSelectedId(""); setTemplateDraft({ name: "", body: "", status: "active" }); }}><Plus size={16} />新建模板</button></header>{state.templates.length > 0 && <label>选择模板<select value={selectedId} onChange={(event) => setSelectedId(event.target.value)}><option value="">新建模板</option>{state.templates.map((item) => <option key={item.id} value={item.id}>{item.name}{item.status === "inactive" ? "（已停用）" : ""}</option>)}</select></label>}<div className="offer-settings-grid"><label>模板名称<input value={templateDraft.name} onChange={(event) => setTemplateDraft({ ...templateDraft, name: event.target.value })} /></label><label>状态<select value={templateDraft.status} onChange={(event) => setTemplateDraft({ ...templateDraft, status: event.target.value })}><option value="active">启用</option><option value="inactive">停用</option></select></label><label className="offer-full-field">模板正文<textarea rows="7" value={templateDraft.body} onChange={(event) => setTemplateDraft({ ...templateDraft, body: event.target.value })} /></label></div><footer><span>{selected ? `当前版本 ${selected.version}` : "创建后可设为职位默认模板"}</span><button className="button primary" type="button" disabled={busy === "template"} onClick={() => void saveTemplate()}>{busy === "template" ? "保存中…" : selected ? "保存模板" : "创建模板"}</button></footer></section>
    <section className="offer-approver-settings" aria-labelledby="offer-approver-settings-title"><header><div><h3 id="offer-approver-settings-title">特殊 Offer 追加审批人（领导）</h3><p>特殊 Offer 会在岗位默认审批领导之后，按此顺序追加更高层审批人；空列表表示不追加。</p></div></header><label>添加审批人<select value="" onChange={(event) => { const id = event.target.value; if (id && !approverIds.includes(id)) setApproverIds([...approverIds, id]); }}><option value="">选择成员</option>{state.users.filter((user) => !approverIds.includes(user.id)).map((user) => <option key={user.id} value={user.id}>{user.display_name || user.name || user.email}</option>)}</select></label><ol>{approverIds.map((id, index) => { const user = state.users.find((item) => item.id === id); return <li key={id}><span>{index + 1}</span><strong>{user?.display_name || user?.name || user?.email || id}</strong><div><button type="button" aria-label={`上移${user?.display_name || user?.name || id}`} disabled={index === 0} onClick={() => setApproverIds(moveItem(approverIds, index, -1))}><ArrowUp size={16} /></button><button type="button" aria-label={`下移${user?.display_name || user?.name || id}`} disabled={index === approverIds.length - 1} onClick={() => setApproverIds(moveItem(approverIds, index, 1))}><ArrowDown size={16} /></button><button type="button" aria-label={`移除${user?.display_name || user?.name || id}`} onClick={() => setApproverIds(approverIds.filter((item) => item !== id))}><Trash2 size={16} /></button></div></li>; })}</ol><footer><span>{dirty ? "有未保存修改" : "审批顺序已保存"}</span><button className="button primary" type="button" disabled={busy === "approvers" || JSON.stringify(approverIds) === JSON.stringify(state.approvers.approverIds || state.approvers.approver_ids || [])} onClick={() => void saveApprovers()}>{busy === "approvers" ? "保存中…" : "保存审批顺序"}</button></footer></section>
  </>}</div>;
}
