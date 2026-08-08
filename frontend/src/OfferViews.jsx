import { useEffect, useMemo, useRef, useState } from "react";
import {
  AlertTriangle,
  ArrowDown,
  ArrowUp,
  CheckCircle2,
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
  XCircle,
} from "lucide-react";
import { apiClient } from "./apiClient.js";
import { canPerformAction } from "./roleCapabilities.js";

const STATUS_LABELS = Object.freeze({
  draft: "草稿",
  pending_approval: "审批中",
  changes_requested: "需修改",
  ready_to_send: "待发送",
  sent: "已发送",
  withdrawn: "已撤回",
  expired: "已过期",
});

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
  if (error?.code === "invalid_offer_state") return "Offer 状态已变化，请刷新后重试。";
  if (error?.code === "OFFER_SPECIAL_REASON_REQUIRED") return "特殊 Offer 必须填写说明。";
  return `${action}未完成，请稍后重试。`;
}

function deadlineInputValue(value) {
  if (!value) return "";
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return "";
  const offset = date.getTimezoneOffset() * 60_000;
  return new Date(date.getTime() - offset).toISOString().slice(0, 16);
}

function offerDraft(offer, templates) {
  const content = offer?.content && offer.content.redacted !== true ? offer.content : {};
  return {
    templateId: offer?.templateId || offer?.template_id || templates.find((item) => item.status === "active")?.id || "",
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

function OfferHistory({ history }) {
  if (!history) return null;
  return <section className="offer-history" aria-labelledby="offer-history-title">
    <header><History size={18} /><h4 id="offer-history-title">版本与审批历史</h4></header>
    <div className="offer-version-list">
      {(history.versions || []).slice().reverse().map((version) => <article key={version.id}>
        <strong>版本 {version.versionNumber ?? version.version_number}</strong>
        <span>{version.pdfReady ?? version.pdf_ready ? "PDF 已生成" : "PDF 生成中"}</span>
        <small>{version.createdAt || version.created_at ? new Date(version.createdAt || version.created_at).toLocaleString("zh-CN", { hour12: false }) : "时间未记录"}</small>
      </article>)}
    </div>
    {(history.approvals || []).length > 0 && <ol className="offer-approval-history">
      {history.approvals.map((approval) => <li key={approval.id}><span>第 {approval.sequence} 步</span><strong>{approval.status === "approved" ? "已批准" : approval.status === "rejected" ? "要求修改" : "待审批"}</strong>{approval.reason && <p>{approval.reason}</p>}</li>)}
    </ol>}
  </section>;
}

function OfferDraftForm({ offer, templates, applicationId, controller, role, onSaved, onNotify }) {
  const [draft, setDraft] = useState(() => offerDraft(offer, templates));
  const [status, setStatus] = useState("idle");
  const [error, setError] = useState("");
  const errorRef = useRef(null);

  useEffect(() => setDraft(offerDraft(offer, templates)), [offer?.id, offer?.version, templates]);
  useEffect(() => { if (error) errorRef.current?.focus(); }, [error]);

  function change(field, value) {
    setDraft((current) => ({ ...current, [field]: value }));
    setError("");
  }

  function validate() {
    if (!draft.templateId) return "请选择 Offer 模板。";
    if (!draft.title.trim() || !draft.body.trim() || !draft.compensation.trim()) return "请完整填写标题、正文和薪酬方案。";
    if (!draft.candidateResponseDeadline || Number.isNaN(new Date(draft.candidateResponseDeadline).getTime())) return "请设置候选人回复截止时间。";
    if (draft.isSpecial && !draft.specialReason.trim()) return "特殊 Offer 必须填写说明。";
    return "";
  }

  async function save() {
    const validation = validate();
    if (validation) { setError(validation); return null; }
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
      setError(offerErrorMessage(requestError, "保存"));
      return null;
    } finally { setStatus("idle"); }
  }

  async function submit() {
    const saved = await save();
    if (!saved) return;
    setStatus("submitting"); setError("");
    try {
      const submitted = await controller.submitApproval(saved);
      onSaved(submitted);
      onNotify("Offer 已提交审批；审批通过后仍需 HR 明确发送");
    } catch (requestError) { setError(offerErrorMessage(requestError, "提交审批")); }
    finally { setStatus("idle"); }
  }

  const busy = status !== "idle";
  const canSubmit = !offer || canRenderOfferAction(role, offer, "submit");
  return <form className="offer-form" onSubmit={(event) => { event.preventDefault(); void save(); }}>
    <div className="offer-form-grid">
      <label>Offer 模板<select aria-label="Offer 模板" required disabled={busy} value={draft.templateId} onChange={(event) => change("templateId", event.target.value)}><option value="">请选择模板</option>{templates.map((item) => <option key={item.id} value={item.id} disabled={item.status === "inactive"}>{item.name}{item.status === "inactive" ? "（已停用）" : ""}</option>)}</select></label>
      <label>候选人回复截止时间<input aria-label="候选人回复截止时间" required type="datetime-local" disabled={busy} value={draft.candidateResponseDeadline} onChange={(event) => change("candidateResponseDeadline", event.target.value)} /></label>
      <label className="offer-full-field">Offer 标题<input aria-label="Offer 标题" required disabled={busy} value={draft.title} onChange={(event) => change("title", event.target.value)} placeholder="例如：正式录用通知" /></label>
      <label className="offer-full-field">Offer 正文<textarea aria-label="Offer 正文" required rows="7" disabled={busy} value={draft.body} onChange={(event) => change("body", event.target.value)} placeholder="填写岗位、汇报关系、入职安排等内容" /></label>
      <label>薪酬方案<textarea aria-label="薪酬方案" required rows="4" disabled={busy} value={draft.compensation} onChange={(event) => change("compensation", event.target.value)} /></label>
      <label>福利与补充说明<textarea aria-label="福利与补充说明" rows="4" disabled={busy} value={draft.benefits} onChange={(event) => change("benefits", event.target.value)} /></label>
    </div>
    <div className="offer-special-row"><span><ShieldCheck size={18} /><span><strong>特殊 Offer</strong><small>开启后会在岗位默认审批人之后追加组织固定审批链。</small></span></span><label className="compact-switch"><input aria-label="特殊 Offer" type="checkbox" disabled={busy} checked={draft.isSpecial} onChange={(event) => change("isSpecial", event.target.checked)} /><span aria-hidden="true" /></label></div>
    {draft.isSpecial && <label className="offer-special-reason">特殊 Offer 说明<span className="required-label">必填</span><textarea aria-label="特殊 Offer 说明" required rows="3" disabled={busy} value={draft.specialReason} onChange={(event) => change("specialReason", event.target.value)} placeholder="说明为何需要追加特殊审批" /></label>}
    {error && <div ref={errorRef} tabIndex="-1" className="offer-error" role="alert"><AlertTriangle size={17} />{error}{error.includes("刷新") && offer && <button type="button" onClick={() => onSaved(null)}>刷新最新版本</button>}</div>}
    <footer><span>{offer ? `保存会创建不可覆盖的新版本；当前主记录版本 ${offer.version}` : "创建后可预览并提交审批。"}</span><div><button className="button secondary" type="submit" disabled={busy}>{status === "saving" ? "保存中…" : offer ? "保存新版本" : "创建草稿"}</button>{canSubmit && <button className="button primary" type="button" disabled={busy} onClick={() => void submit()}>{status === "submitting" ? "提交中…" : "保存并提交审批"}</button>}</div></footer>
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

export function CandidateOfferView({ candidate, role, controller, approvalId, onNotify = () => {} }) {
  const applicationId = candidate?.application?.id || candidate?.applicationId;
  const [state, setState] = useState({ status: "loading", offer: null, templates: [], history: null, error: "" });
  const [action, setAction] = useState("");

  async function load() {
    if (!applicationId) { setState({ status: "error", offer: null, templates: [], history: null, error: "当前候选人没有可用申请，无法管理 Offer。" }); return; }
    setState((current) => ({ ...current, status: "loading", error: "" }));
    try {
      const offer = await controller.getApplicationOffer(applicationId);
      const [templates, history] = await Promise.all([
        canPerformAction(role, "管理 Offer") ? controller.listTemplates().catch(() => []) : Promise.resolve([]),
        offer ? controller.listHistory(offer.id).catch(() => null) : Promise.resolve(null),
      ]);
      setState({ status: "ready", offer, templates, history, error: "" });
    } catch { setState((current) => ({ ...current, status: "error", error: "Offer 加载失败，请重试。" })); }
  }

  useEffect(() => { void load(); }, [applicationId, controller, role]);

  async function run(name, command, message) {
    setAction(name);
    try { const offer = await command(); setState((current) => ({ ...current, offer })); onNotify(message); await load(); }
    catch (error) { setState((current) => ({ ...current, error: offerErrorMessage(error, message) })); }
    finally { setAction(""); }
  }

  if (state.status === "loading" && !state.offer) return <div className="offer-state" role="status"><LoaderCircle className="spin" size={24} /><strong>正在加载 Offer</strong><span>正在读取当前版本和审批状态。</span></div>;
  if (state.status === "error" && !state.offer) return <div className="offer-state error" role="alert"><AlertTriangle size={24} /><strong>Offer 无法加载</strong><span>{state.error}</span><button className="button secondary" type="button" onClick={() => void load()}><RefreshCw size={16} />重试</button></div>;

  const offer = state.offer;
  if (!offer) return <div className="offer-workspace"><header className="offer-heading"><div><h3>Offer 管理</h3><p>为候选人创建版本化 Offer，并在审批通过后由 HR 明确发送。</p></div></header>{canPerformAction(role, "管理 Offer") ? <OfferDraftForm applicationId={applicationId} templates={state.templates} controller={controller} role={role} onSaved={(saved) => saved ? setState((current) => ({ ...current, offer: saved })) : void load()} onNotify={onNotify} /> : <div className="offer-state"><FileText size={24} /><strong>暂无 Offer</strong><span>当前角色不能创建 Offer。</span></div>}</div>;

  const sensitive = Boolean(offer.canViewSensitiveContent ?? offer.can_view_sensitive_content);
  const decisionApprovalId = approvalId || offer.pendingApprovalId || offer.pending_approval_id;
  return <div className="offer-workspace">
    <header className="offer-heading"><div><h3>Offer 管理</h3><p>Offer 版本、审批、发送和候选人招聘状态相互独立。</p></div><span className={`offer-status ${offer.status}`}>{offerStatusLabel(offer.status)}</span></header>
    {state.error && <div className="offer-error" role="alert"><AlertTriangle size={17} />{state.error}<button type="button" onClick={() => void load()}>刷新</button></div>}
    <div className="offer-summary">
      <span><FileCheck2 size={18} /><span><small>当前版本</small><strong>v{offer.currentVersionNumber ?? offer.current_version_number ?? "—"}</strong></span></span>
      <span><Clock3 size={18} /><span><small>回复截止</small><strong>{new Date(offer.candidateResponseDeadline || offer.candidate_response_deadline).toLocaleString("zh-CN", { hour12: false })}</strong></span></span>
      <span><FileText size={18} /><span><small>PDF</small><strong>{offer.pdfReady ?? offer.pdf_ready ? "已生成，可预览" : "生成中"}</strong></span></span>
    </div>
    {offer.status === "ready_to_send" && <div className="offer-ready-notice" role="status"><CheckCircle2 size={20} /><span><strong>审批已完成，但尚未发送</strong><small>系统不会自动发送。请 HR 核对 PDF 和候选人邮箱后明确点击发送。</small></span></div>}
    {offer.status === "changes_requested" && <div className="offer-changes-notice"><Undo2 size={20} /><span><strong>审批人要求修改</strong><small>{state.history?.approvals?.slice().reverse().find((item) => item.status === "rejected")?.reason || "请查看下方审批历史。"}</small></span></div>}
    {!sensitive ? <div className="offer-redacted" role="status"><ShieldCheck size={22} /><strong>敏感内容已由服务端隐藏</strong><span>当前账号只能查看 Offer 状态，薪酬和正文不会在浏览器中展示。</span></div> : <>
      {["draft", "changes_requested"].includes(offer.status) && canRenderOfferAction(role, offer, "update") && <OfferDraftForm offer={offer} templates={state.templates} applicationId={applicationId} controller={controller} role={role} onSaved={(saved) => saved ? setState((current) => ({ ...current, offer: saved })) : void load()} onNotify={onNotify} />}
      {!(["draft", "changes_requested"].includes(offer.status) && canRenderOfferAction(role, offer, "update")) && <section className="offer-preview" aria-labelledby="offer-preview-title"><header><h4 id="offer-preview-title">Offer 预览</h4>{offer.isSpecial ?? offer.is_special ? <span>特殊 Offer</span> : null}</header><h5>{offer.content?.title || "正式录用通知"}</h5><p className="offer-body-copy">{offer.content?.body || "正文未填写"}</p><dl><div><dt>薪酬方案</dt><dd>{offer.content?.compensation || "未填写"}</dd></div><div><dt>福利与补充说明</dt><dd>{offer.content?.benefits || "未填写"}</dd></div>{(offer.specialReason || offer.special_reason) && <div><dt>特殊说明</dt><dd>{offer.specialReason || offer.special_reason}</dd></div>}</dl></section>}
    </>}
    {canRenderOfferAction(role, offer, "decide") && decisionApprovalId && <ApprovalDecision offer={offer} approvalId={decisionApprovalId} controller={controller} onSaved={(saved) => setState((current) => ({ ...current, offer: saved }))} onNotify={onNotify} />}
    <div className="offer-actions">
      {canRenderOfferAction(role, offer, "withdraw") && <button className="button secondary" type="button" disabled={Boolean(action)} onClick={() => void run("withdraw", () => controller.withdraw(offer), "Offer 已撤回")}><XCircle size={16} />{action === "withdraw" ? "撤回中…" : "撤回 Offer"}</button>}
      {canRenderOfferAction(role, offer, "send") && <button className="button primary" type="button" disabled={Boolean(action) || !(offer.pdfReady ?? offer.pdf_ready)} onClick={() => void run("send", () => controller.send(offer), "Offer 已发送")}><Send size={16} />{action === "send" ? "发送中…" : "确认并发送 Offer"}</button>}
    </div>
    <OfferHistory history={state.history} />
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
  return <section className="rail-section offer-task-section" aria-labelledby="offer-task-title"><header><h3 id="offer-task-title">待我审批的 Offer</h3><button type="button" onClick={() => void load()}>刷新</button></header>{state.status === "loading" && <p role="status">正在加载审批待办…</p>}{state.status === "error" && <p className="offer-error" role="alert">{state.error}</p>}{state.status === "ready" && state.records.length === 0 && <p>暂无待审批 Offer</p>}{state.records.map((task) => <button className="rail-item" type="button" key={task.id} onClick={() => onOpenOffer(task)}><strong>{task.candidateName || task.candidate_name}</strong><small>{task.jobTitle || task.job_title} · Offer v{task.versionNumber || task.version_number}</small><small>回复截止：{new Date(task.candidateResponseDeadline || task.candidate_response_deadline).toLocaleDateString("zh-CN")}</small></button>)}</section>;
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
      const eligible = users.filter((user) => user.status === "active" && user.roles?.some((role) => ["recruiting_admin", "recruiter", "hiring_manager"].includes(role)));
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
    <section className="offer-approver-settings" aria-labelledby="offer-approver-settings-title"><header><div><h3 id="offer-approver-settings-title">特殊 Offer 审批人</h3><p>按顺序追加到岗位默认 Offer 审批人之后；空列表表示不追加。</p></div></header><label>添加审批人<select value="" onChange={(event) => { const id = event.target.value; if (id && !approverIds.includes(id)) setApproverIds([...approverIds, id]); }}><option value="">选择成员</option>{state.users.filter((user) => !approverIds.includes(user.id)).map((user) => <option key={user.id} value={user.id}>{user.display_name || user.name || user.email}</option>)}</select></label><ol>{approverIds.map((id, index) => { const user = state.users.find((item) => item.id === id); return <li key={id}><span>{index + 1}</span><strong>{user?.display_name || user?.name || user?.email || id}</strong><div><button type="button" aria-label={`上移${user?.display_name || user?.name || id}`} disabled={index === 0} onClick={() => setApproverIds(moveItem(approverIds, index, -1))}><ArrowUp size={16} /></button><button type="button" aria-label={`下移${user?.display_name || user?.name || id}`} disabled={index === approverIds.length - 1} onClick={() => setApproverIds(moveItem(approverIds, index, 1))}><ArrowDown size={16} /></button><button type="button" aria-label={`移除${user?.display_name || user?.name || id}`} onClick={() => setApproverIds(approverIds.filter((item) => item !== id))}><Trash2 size={16} /></button></div></li>; })}</ol><footer><span>{dirty ? "有未保存修改" : "审批顺序已保存"}</span><button className="button primary" type="button" disabled={busy === "approvers" || JSON.stringify(approverIds) === JSON.stringify(state.approvers.approverIds || state.approvers.approver_ids || [])} onClick={() => void saveApprovers()}>{busy === "approvers" ? "保存中…" : "保存审批顺序"}</button></footer></section>
  </>}</div>;
}
