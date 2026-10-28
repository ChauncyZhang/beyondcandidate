import { useCallback, useEffect, useRef, useState } from "react";
import { CircleAlert, RefreshCw, X, XCircle } from "lucide-react";
import { terminationController, canManageTermination, hasUnsettledExternal } from "./terminationController.js";
import { interviewController, requiresManualCandidateNotice } from "./interviewController.js";
import { applicationStageLabel } from "./recruitingTerminology.js";
import "./termination.css";

const countLabels = { review_tasks: "未完成评审", interviews: "面试", feedback_tasks: "反馈待办", offers: "有效 Offer", approvals: "审批", queued_messages: "排队邮件", onboarding: "入职任务" };
const stateLabels = { in_flight: "调用在途", unknown: "结果未知", needs_action: "需人工处理", resolved: "已处理" };
const kindLabels = { feishu_onboarding: "飞书入职审批", onboarding: "入职办理", calendar: "外部面试日历", feishu_calendar: "飞书面试日历", interview_calendar: "外部面试日历", email: "候选人邮件", smtp: "候选人邮件" };
const stageLabels = { new: "新简历", screening: "筛选中", deferred: "暂缓", review: "待评审", contact: "沟通中", interview_pending: "待安排面试", interviewing: "面试中", decision: "待录用决策", passed: "待录用确认", hired: "待入职", withdrawn: "已退出招聘流程", rejected: "已淘汰" };

function useDialog(ref, onClose, busy) {
  useEffect(() => {
    const previous = document.activeElement;
    ref.current?.focus();
    function keydown(event) {
      if (event.key === "Escape" && !busy) { event.preventDefault(); onClose(); }
      if (event.key !== "Tab") return;
      const nodes = [...ref.current.querySelectorAll('button:not(:disabled), textarea:not(:disabled), input:not(:disabled), a[href]')];
      const first = nodes[0], last = nodes.at(-1);
      if (!first) { event.preventDefault(); return; }
      if (event.shiftKey && (document.activeElement === first || document.activeElement === ref.current)) { event.preventDefault(); last.focus(); }
      else if (!event.shiftKey && (document.activeElement === last || document.activeElement === ref.current)) { event.preventDefault(); first.focus(); }
    }
    document.addEventListener("keydown", keydown);
    return () => { document.removeEventListener("keydown", keydown); if (previous?.isConnected) previous.focus(); };
  }, [ref, onClose, busy]);
}

function ExternalItems({ items = [] }) {
  return <ul className="termination-external-items">{items.map((item, index) => <li key={`${item.kind}-${item.resource_id}-${index}`}><strong>{kindLabels[item.kind] || "外部关联事项"} · {stateLabels[item.state] || "待核对"}</strong><span>事项：{item.resource_id}{item.generation != null ? ` · 请求代次 ${item.generation}` : ""}</span>{item.reference && (/^https:\/\//i.test(item.reference) ? <a href={item.reference} target="_blank" rel="noopener noreferrer">{["calendar", "feishu_calendar", "interview_calendar"].includes(item.kind) ? "打开外部日历" : "打开外部审批"}</a> : <span>实例 / 参考：{item.reference}</span>)}</li>)}</ul>;
}

export function TerminationDialog({ applicationId, candidateName, jobTitle, onClose, onChanged = () => {}, controller = terminationController }) {
  const [preview, setPreview] = useState(null);
  const [reason, setReason] = useState("");
  const [confirmed, setConfirmed] = useState(false);
  const [busy, setBusy] = useState(false);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState("");
  const [success, setSuccess] = useState(false);
  const [conflict, setConflict] = useState(false);
  const [reconcileNotice, setReconcileNotice] = useState("");
  const key = useRef(null);
  const reconcileKey = useRef(null);
  const inFlight = useRef(false);
  const lifecycle = useRef(0);
  const loadRequest = useRef(0);
  const dialog = useRef(null);
  useDialog(dialog, onClose, busy);
  const load = useCallback(async () => {
    if (inFlight.current) return;
    const epoch = lifecycle.current, request = ++loadRequest.current;
    setLoading(true);
    try {
      const saved = await controller.preview(applicationId);
      if (epoch !== lifecycle.current || request !== loadRequest.current) return;
      setPreview(saved); setConflict(false); setError(""); key.current = null; reconcileKey.current = null;
      setReconcileNotice("");
    }
    catch { if (epoch === lifecycle.current && request === loadRequest.current) setError("影响预览加载失败，请重新加载。当前输入已保留。"); }
    finally { if (epoch === lifecycle.current && request === loadRequest.current) setLoading(false); }
  }, [applicationId, controller]);
  useEffect(() => { lifecycle.current++; void load(); return () => { lifecycle.current++; }; }, [load]);
  const termination = preview?.termination;
  const canResolve = Boolean(termination && preview.allowed_actions?.confirm_external && !hasUnsettledExternal(termination));
  const canTerminate = Boolean(!termination && preview?.allowed_actions?.terminate);
  const canReconcile = Boolean(termination && preview.allowed_actions?.reconcile_external);
  async function reconcile() {
    if (inFlight.current || loading || conflict || !canReconcile) return;
    const epoch = lifecycle.current;
    inFlight.current = true; setBusy(true); setError("");
    reconcileKey.current ||= crypto.randomUUID();
    try {
      const saved = await controller.reconcile(preview, reconcileKey.current);
      if (epoch !== lifecycle.current) return;
      setPreview(saved); setReconcileNotice("核对任务已提交，请稍后刷新"); reconcileKey.current = null;
      onChanged(saved);
    } catch (requestError) {
      if (epoch !== lifecycle.current) return;
      if (requestError?.status === 409) setConflict(true);
      setError(requestError?.status === 409 ? "外部处理状态已变化，说明已保留。请刷新处理状态后核对。" : "核对任务未确认提交，说明已保留，请重试。外部结果仍不能视为已确认。");
    } finally { inFlight.current = false; if (epoch === lifecycle.current) setBusy(false); }
  }
  async function submit(event) {
    event.preventDefault();
    if (inFlight.current || conflict || loading || !reason.trim() || !(canResolve || canTerminate)) return;
    inFlight.current = true; setBusy(true); setError("");
    const epoch = lifecycle.current;
    key.current ||= crypto.randomUUID();
    try {
      const saved = termination ? await controller.resolve(preview, reason, key.current) : await controller.terminate(preview, reason, confirmed, key.current);
      if (epoch !== lifecycle.current) return;
      setPreview(saved); setSuccess(true); setReason(""); key.current = null;
      window.dispatchEvent(new Event("recruiting-termination-changed"));
      onChanged(saved);
    } catch (requestError) {
      if (epoch !== lifecycle.current) return;
      if (requestError?.status === 409) {
        setConflict(true);
        setError("申请或影响事项已变化。原因已保留，请重新加载预览，核对后再次确认。");
      } else setError("操作未确认成功，输入已保留。请重试；不要假定外部事项已撤销。");
    } finally { inFlight.current = false; if (epoch === lifecycle.current) setBusy(false); }
  }
  return <div className="modal-backdrop termination-backdrop"><section ref={dialog} tabIndex={-1} role="dialog" aria-modal="true" aria-labelledby="termination-title" className="modal termination-dialog">
    <header className="modal-header"><div><h2 id="termination-title">{termination ? "本次招聘已终止" : "终止本次招聘"}</h2><p>{candidateName || "当前候选人"} · {jobTitle || "当前职位申请"}</p></div><button type="button" className="icon-button" aria-label="关闭终止确认" disabled={busy} onClick={onClose}><X size={20} /></button></header>
    <form onSubmit={submit}><div className="modal-body termination-body">
      {loading && <p role="status">正在加载影响预览…</p>}
      {termination && <div><button type="button" className="button secondary" disabled={busy || loading} onClick={() => void load()}><RefreshCw size={16} />刷新处理状态</button>{canReconcile && <button type="button" className="button secondary" disabled={busy || loading || conflict} onClick={() => void reconcile()}><RefreshCw size={16} />核对飞书结果</button>}</div>}
      {reconcileNotice && <p role="status">{reconcileNotice}</p>}
      {error && <p role="alert" className="termination-error"><CircleAlert size={17} />{error}</p>}
      {(!preview || conflict) && !loading && <button type="button" className="button secondary" onClick={() => void load()}><RefreshCw size={16} />重新加载预览</button>}
      {preview && !loading && <>
        <p>当前阶段：{stageLabels[preview.stage] || applicationStageLabel(preview.stage)} · 仅当前申请</p>
        <p>不影响其他职位申请；保留简历、已提交评价、Offer 接受记录和操作历史。旧流程不能恢复，重新招聘须新建申请。</p>
        <p className="termination-notice">本操作不会自动发送新的候选人邮件。请 HR 联系候选人；已发送或发送中的邮件无法召回。</p>
        {!termination && <p>已发起的飞书审批不会自动撤回。终止后如有外部事项待处理，请有权限的人在飞书收尾；无权限时联系飞书审批管理员。</p>}
        {!termination && preview.external_items?.length > 0 && <section aria-label="已知外部影响"><h3>已知外部影响</h3><ExternalItems items={preview.external_items} /></section>}
        {!termination && <dl className="termination-counts">{Object.entries(countLabels).map(([name, label]) => <div key={name}><dt>{label}</dt><dd>{preview.counts?.[name] ?? 0}</dd></div>)}</dl>}
        {termination && <><p>终止原因：{termination.reason_text}</p><p>终止时间：{termination.terminated_at}</p>{success && <p role="status">本地操作已保存，招聘流程不会恢复。</p>}<h3>外部收尾{termination.external_status !== "resolved" ? "待处理" : "已确认"}</h3><p>请有权限的人在飞书处理；无权操作时联系飞书审批管理员。人工确认只是处理声明，不代表系统已核验或自动撤回。</p><ExternalItems items={termination.external_items} />{hasUnsettledExternal(termination) && <p role="status">创建调用仍在途或结果未知，暂不能确认外部处理完成。请联系系统管理员，提供下方事项标识和请求代次，核对原飞书请求与远端审批结果并登记核对结论；刷新外部状态明确后，再由有权限的 HR 处理并确认。不要重新发起入职审批。</p>}{termination.resolution_history?.length > 0 && <details><summary>历史处理记录</summary><ul>{termination.resolution_history.map((item, index) => <li key={index}>{item.reason_text || item.reason || "人工确认"} · {item.resolved_at || item.created_at || ""}</li>)}</ul></details>}</>}
        {(canTerminate || canResolve || termination?.external_status === "pending") && <><label className="termination-reason">{termination ? "外部处理说明" : "终止原因"}（必填）<textarea rows={4} required maxLength={2000} disabled={busy} value={reason} onChange={(event) => { setReason(event.target.value); key.current = null; }} /></label>{canTerminate && preview.requires_not_onboarded_confirmation && <label className="termination-checkbox"><input type="checkbox" checked={confirmed} disabled={busy} onChange={(event) => { setConfirmed(event.target.checked); key.current = null; }} />确认候选人尚未实际入职，本操作不办理员工离职</label>}</>}
        {!termination && !canTerminate && <p role="status">当前申请或账号不允许终止。请刷新申请状态或联系有权限的 HR。</p>}
      </>}
    </div><footer className="modal-footer"><button type="button" className="button secondary" disabled={busy} onClick={onClose}>关闭</button>{(canTerminate || canResolve) && <button type="submit" className="button danger" disabled={busy || loading || conflict || !reason.trim() || (canTerminate && preview.requires_not_onboarded_confirmation && !confirmed)}>{busy ? "正在保存…" : termination ? "确认外部处理完成" : "确认终止本次招聘"}</button>}</footer></form>
  </section></div>;
}

export function TerminationEntry({ applicationId, candidateName, jobTitle, role, stage, onChanged, label, controller = terminationController }) {
  const [open, setOpen] = useState(false);
  const saved = useRef(null);
  const close = useCallback(() => { setOpen(false); if (saved.current) { onChanged?.(saved.current); saved.current = null; } }, [onChanged]);
  if (!applicationId || !canManageTermination(role)) return null;
  const terminal = ["withdrawn", "rejected", "已撤回", "已淘汰"].includes(stage);
  if (["rejected", "已淘汰"].includes(stage)) return null;
  return <><button className="button secondary termination-entry" type="button" onClick={() => setOpen(true)}><XCircle size={16} />{terminal ? "查看终止记录与外部收尾" : label || "终止本次招聘"}</button>{open && <TerminationDialog applicationId={applicationId} candidateName={candidateName} jobTitle={jobTitle} onClose={close} onChanged={(value) => { saved.current = value; }} controller={controller} />}</>;
}

export function PendingTerminationPanel({ role, onOpen, controller = terminationController }) {
  const [state, setState] = useState({ items: [], total: 0, loading: true, error: "" });
  const allowed = canManageTermination(role);
  const load = useCallback(async () => {
    if (!allowed) return;
    setState((current) => ({ ...current, loading: true, error: "" }));
    try { const data = await controller.pending(); setState({ ...data, loading: false, error: "" }); }
    catch { setState((current) => ({ ...current, loading: false, error: "外部收尾列表加载失败，请重试。" })); }
  }, [allowed, controller]);
  useEffect(() => { void load(); window.addEventListener("recruiting-termination-changed", load); return () => window.removeEventListener("recruiting-termination-changed", load); }, [load]);
  if (!allowed) return null;
  return <section className="pending-termination-panel" aria-labelledby="pending-termination-title"><header><h2 id="pending-termination-title">招聘已终止 · 外部收尾待处理（{state.total}）</h2><button type="button" className="icon-button" aria-label="刷新外部收尾" disabled={state.loading} onClick={() => void load()}><RefreshCw size={16} /></button></header>{state.loading && <p role="status">正在加载外部事项…</p>}{state.error && <p role="alert">{state.error}</p>}{!state.loading && !state.error && !state.items.length && <p>暂无待处理外部事项</p>}{state.items.map((item) => <div className="pending-termination-row" key={item.application_id}><div><strong>{item.candidate_name} · {item.job_title}</strong><p>招聘流程已停止，外部事项仍需处理。</p><ExternalItems items={item.external_items} /></div><div><TerminationEntry applicationId={item.application_id} candidateName={item.candidate_name} jobTitle={item.job_title} stage="withdrawn" role={role} onChanged={load} controller={controller} /><button type="button" className="button secondary" onClick={() => onOpen({ id: item.candidate_id, candidateId: item.candidate_id, applicationId: item.application_id, jobId: item.job_id, name: item.candidate_name, position: item.job_title })}>查看申请历史</button></div></div>)}</section>;
}

export function InterviewCancellationEntry({ interview, applicationId, candidateName, jobTitle, role, onChanged, onNotify, controller = interviewController }) {
  const [open, setOpen] = useState(false);
  const [reason, setReason] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const [notice, setNotice] = useState("");
  const [whole, setWhole] = useState(false);
  const [conflict, setConflict] = useState(false);
  const latest = useRef(null);
  const wholeSaved = useRef(null);
  const ref = useRef(null);
  const lock = useRef(false);
  const close = useCallback(() => setOpen(false), []);
  const closeWhole = useCallback(() => { setWhole(false); if (wholeSaved.current) { onChanged?.(wholeSaved.current); wholeSaved.current = null; } }, [onChanged]);
  const status = interview?.workflowStatus || interview?.status;
  const cancellable = ["已安排", "已确认", "待确认", "待评价", "待反馈"].includes(status) || (status === "已完成" && interview.feedbackStatus !== "已提交");
  if (!canManageTermination(role) || !interview?.interviewId || !cancellable) return null;
  async function cancel(event) {
    event.preventDefault(); if (lock.current || conflict || !reason.trim()) return;
    lock.current = true; setBusy(true); setError("");
    try {
      const record = latest.current || (Number.isInteger(interview.version) ? { id: interview.interviewId, version: interview.version } : await controller.get(interview.interviewId));
      const saved = await controller.transition(record, "cancelled", { reason: reason.trim() });
      const message = requiresManualCandidateNotice(saved) ? "已取消，需人工通知候选人；外部同步状态请在面试记录中核对。" : "本场面试已取消，申请仍保留；请核对候选人通知与外部日历状态。";
      setNotice(message); onNotify?.(message);
      setOpen(false); onChanged?.(saved);
    } catch (requestError) { if (requestError?.status === 409) setConflict(true); setError(requestError?.status === 409 ? "面试已变化，原因已保留。请刷新记录后重试。" : "取消未确认成功，原因已保留，请重试。"); }
    finally { lock.current = false; setBusy(false); }
  }
  async function reload() {
    if (lock.current) return;
    lock.current = true; setBusy(true);
    try { latest.current = await controller.get(interview.interviewId); setConflict(false); setError("已刷新面试状态，请核对后再次确认取消。"); }
    catch { setError("面试刷新失败，原因已保留。请重试。"); }
    finally { lock.current = false; setBusy(false); }
  }
  return <>{notice && <p role="status">{notice}</p>}<button type="button" className="button secondary" onClick={() => setOpen(true)}><XCircle size={16} />取消面试</button>{open && <SingleCancellationDialog dialogRef={ref} conflict={conflict} onReload={reload} busy={busy} onClose={close} onSubmit={cancel} reason={reason} setReason={setReason} error={error} onWhole={() => { setOpen(false); setWhole(true); }} applicationId={applicationId} />}{whole && <TerminationDialog applicationId={applicationId} candidateName={candidateName} jobTitle={jobTitle} onClose={closeWhole} onChanged={(value) => { wholeSaved.current = value; }} />}</>;
}

function SingleCancellationDialog({ dialogRef, conflict, onReload, busy, onClose, onSubmit, reason, setReason, error, onWhole, applicationId }) {
  useDialog(dialogRef, onClose, busy);
  return <div className="modal-backdrop termination-backdrop"><section ref={dialogRef} tabIndex={-1} className="modal termination-dialog" role="dialog" aria-modal="true" aria-labelledby="single-cancel-title"><header className="modal-header"><h2 id="single-cancel-title">仅取消本场面试</h2><button type="button" className="icon-button" aria-label="关闭取消面试" disabled={busy} onClick={onClose}><X size={20} /></button></header><form onSubmit={onSubmit}><div className="modal-body termination-body"><p>仅取消本场，可重新安排；不结束当前申请，保留已提交评价。</p><label className="termination-reason">操作原因（必填）<textarea required maxLength={1000} rows={4} value={reason} disabled={busy} onChange={(event) => setReason(event.target.value)} /></label>{error && <p role="alert">{error}</p>}{conflict && <button className="button secondary" type="button" disabled={busy} onClick={() => void onReload()}>刷新面试状态</button>}{applicationId && <button className="button secondary" type="button" disabled={busy} onClick={onWhole}>候选人退出，终止本次招聘</button>}</div><footer className="modal-footer"><button type="button" className="button secondary" disabled={busy} onClick={onClose}>返回</button><button type="submit" className="button danger" disabled={busy || conflict || !reason.trim()}>{busy ? "正在取消…" : "仅取消本场面试"}</button></footer></form></section></div>;
}
