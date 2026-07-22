import { useEffect, useMemo, useState } from "react";
import {
  AlertTriangle,
  ArrowLeft,
  CalendarDays,
  CalendarPlus,
  Check,
  CheckCircle2,
  ChevronDown,
  ChevronRight,
  CircleAlert,
  FileText,
  List,
  RefreshCw,
  Search,
  Send,
  X,
} from "lucide-react";
import { buildWorkweekColumns, getInterviewPrimaryAction, isInWorkweek, isMyInterview } from "./interviewViewState.js";
import { InterviewCalendar } from "./InterviewCalendar.jsx";
import { InterviewFeedbackWorkspace } from "./InterviewFeedbackWorkspace.jsx";
import { ScheduleWorkspace } from "./ScheduleWorkspace.jsx";
import { feedbackRatingDimensions, formatSubmittedFeedbackRatings } from "./feedbackRatings.js";
import { PagePrimaryAction } from "./PagePrimaryAction.jsx";
import { interviewStatusLabel } from "./recruitingTerminology.js";
import "./product-theme-interviews.css";
export { copyInterviewText, getScheduleConflictType, getScheduleSavedMessage } from "./ScheduleWorkspace.jsx";

/* feedback-draft-helpers:start */
export const INTERVIEW_FEEDBACK_DRAFT_PREFIX = "ats.interview-feedback-draft.v1:";

export function getInterviewFeedbackDraftKey(userId, interviewId) {
  return `${INTERVIEW_FEEDBACK_DRAFT_PREFIX}${userId}:${interviewId}`;
}

export function normalizeInterviewConclusion(conclusion) {
  return conclusion === "保留" ? "待补充评估" : conclusion;
}

function defaultDraftStorage() {
  try {
    return typeof window === "undefined" ? null : window.localStorage;
  } catch {
    return null;
  }
}

export function loadInterviewFeedbackDraft(userId, record, storage = defaultDraftStorage()) {
  if (!userId || !record?.id || record.feedback || !storage) return null;
  try {
    const value = storage.getItem(getInterviewFeedbackDraftKey(userId, record.id));
    if (!value) return null;
    const draft = JSON.parse(value);
    return { ...draft, conclusion: normalizeInterviewConclusion(draft?.conclusion) };
  } catch {
    return null;
  }
}

export function saveInterviewFeedbackDraft(userId, interviewId, draft, storage = defaultDraftStorage()) {
  if (!userId || !interviewId || !storage) return false;
  try {
    storage.setItem(getInterviewFeedbackDraftKey(userId, interviewId), JSON.stringify({ ...draft, conclusion: normalizeInterviewConclusion(draft?.conclusion) }));
    return true;
  } catch {
    return false;
  }
}

export function clearInterviewFeedbackDraft(userId, interviewId, storage = defaultDraftStorage()) {
  if (!userId || !interviewId || !storage) return false;
  try {
    storage.removeItem(getInterviewFeedbackDraftKey(userId, interviewId));
    return true;
  } catch {
    return false;
  }
}

export function resolveInterviewFeedbackDraft(localDraft, serverFeedback) {
  if (serverFeedback?.id) return { form: serverFeedback, source: "server" };
  if (localDraft) return { form: localDraft, source: "local" };
  return { form: null, source: "empty" };
}

export function getFeedbackSubmitError(error) {
  if (error?.code === "resource_version_conflict") {
    return "服务端草稿已在其他页面或设备更新。本机内容已保留，请刷新后核对再提交。";
  }
  if (error?.code === "invalid_state_transition") {
    return "当前面试状态暂不允许提交反馈，本机草稿已保留。请刷新面试状态后重试。";
  }
  return "网络请求失败，表单和本机草稿均已保留。请重试提交。";
}

export function isAmbiguousFeedbackSubmitError(error) {
  const status = Number(error?.status);
  return (status === 0 && error?.kind === "unavailable")
    || !Number.isFinite(status)
    || status === 408
    || status === 429
    || status >= 500;
}
/* feedback-draft-helpers:end */

export function getInterviewTerminalActions(record, canSchedule) {
  if (!canSchedule || !record) return [];
  if (record.status === "待确认") return [{ target: "cancelled", label: "取消面试" }];
  if (["已安排", "已确认"].includes(record.status)) {
    return [
      { target: "cancelled", label: "取消面试" },
      { target: "no_show", label: "标记未到场" },
    ];
  }
  return [];
}

function StatusTag({ status }) {
  const tone = status === "已完成" || status === "已提交" || status === "已发送" ? "success" : status === "发送失败" || status === "已取消" || status === "未到场" ? "danger" : status === "待反馈" || status === "待确认" ? "warning" : "info";
  return <span className={`interview-status ${tone}`}>{interviewStatusLabel(status)}</span>;
}

function InterviewList({ records, status: loadStatus, error, onRetry, nextCursor, loadingMore, onLoadMore, onLoadRange, onSchedule, onFeedback, onDownload, onTransition, canSchedule = true, interviewerId, pageActionHost }) {
  const [view, setView] = useState("list");
  const [query, setQuery] = useState("");
  const [status, setStatus] = useState("全部状态");
  const [date, setDate] = useState("本周");
  const [mineOnly, setMineOnly] = useState(!canSchedule);
  const [transitionDraft, setTransitionDraft] = useState(null);
  const [reason, setReason] = useState("");
  const [transitioning, setTransitioning] = useState(false);
  const dayColumns = useMemo(() => buildWorkweekColumns(), []);

  const filtered = useMemo(() => records.filter((item) => {
    const text = `${item.candidate}${item.position}${item.round}${item.interviewers.join("")}`.toLowerCase();
    return (!query || text.includes(query.toLowerCase())) && (status === "全部状态" || item.status === status || item.feedbackStatus === status) && (!(mineOnly || !canSchedule) || isMyInterview(item, interviewerId)) && (date === "全部日期" || isInWorkweek(item.date, dayColumns));
  }), [canSchedule, date, dayColumns, interviewerId, mineOnly, query, records, status]);

  function requestTerminalTransition(record, action) {
    setTransitionDraft({ record, ...action });
    setReason("");
  }

  async function submitTerminalTransition() {
    if (!reason.trim() || !transitionDraft) return;
    setTransitioning(true);
    const succeeded = await onTransition(transitionDraft.record, transitionDraft.target, reason.trim());
    setTransitioning(false);
    if (succeeded) setTransitionDraft(null);
  }

  function runPrimaryAction(record) {
    const action = getInterviewPrimaryAction(record, { canSchedule, userId: interviewerId });
    if (action?.kind === "feedback") onFeedback(record);
    if (action?.kind === "confirm") void onTransition(record, "confirmed");
    if (action?.kind === "complete") void onTransition(record, "completed");
  }

  function openCalendarInterview(record) {
    const action = getInterviewPrimaryAction(record, { canSchedule, userId: interviewerId });
    if (action?.kind === "feedback") onFeedback(record);
    else if (canSchedule) onSchedule(record);
  }

  return <div className="interview-page interview-list-page">
    <PagePrimaryAction host={pageActionHost}>{canSchedule && <button className="button primary" type="button" onClick={() => onSchedule(null)}><CalendarPlus size={17} />安排面试</button>}</PagePrimaryAction>
    <div className="interview-page-heading"><div><h2>面试安排</h2><p>{canSchedule ? "统一查看排期、冲突、通知状态和待反馈任务。" : "仅展示你参与的面试和待反馈任务。"}</p></div></div>
    {loadStatus === "loading" && records.length === 0 && <div className="workbench-status" role="status" aria-live="polite"><CalendarDays size={22} /><div><strong>正在加载面试</strong><p>正在读取服务端面试安排与反馈状态。</p></div></div>}
    {loadStatus === "error" && records.length === 0 && <div className="workbench-status error" role="alert"><CircleAlert size={22} /><div><strong>面试暂时无法加载</strong><p>{error}</p></div><button className="button secondary" type="button" onClick={onRetry}>重试</button></div>}
    {loadStatus === "ready" && records.length === 0 && <div className="workbench-status empty"><CalendarDays size={22} /><div><strong>暂无面试安排</strong><p>{canSchedule ? "可以从待安排候选人创建第一场面试。" : "当前没有分配给你的面试或反馈任务。"}</p></div></div>}
    {records.length > 0 && loadStatus === "error" && <div className="workbench-inline-error" role="alert"><CircleAlert size={17} /><span>{error}，当前展示上次成功数据。</span><button type="button" onClick={onRetry}>重新加载</button></div>}
    {(records.length > 0 || loadStatus === "ready") && <section className="interview-list-panel">
      <div className="interview-toolbar"><div className="segmented-control" aria-label="面试视图"><button type="button" className={view === "list" ? "active" : ""} onClick={() => setView("list")}><List size={15} />列表</button><button type="button" className={view === "calendar" ? "active" : ""} onClick={() => setView("calendar")}><CalendarDays size={15} />周日历</button></div><label className="interview-search"><Search size={16} /><input aria-label="搜索面试" value={query} onChange={(event) => setQuery(event.target.value)} placeholder="搜索候选人、职位或面试官" /></label><label className="interview-select"><select aria-label="日期筛选" value={date} onChange={(event) => setDate(event.target.value)}><option>本周</option><option>全部日期</option></select><ChevronDown size={14} /></label><label className="interview-select"><select aria-label="状态筛选" value={status} onChange={(event) => setStatus(event.target.value)}><option>全部状态</option>{["已安排", "待确认", "已完成", "待反馈", "已提交", "已取消", "未到场"].map((item) => <option key={item} value={item}>{interviewStatusLabel(item)}</option>)}</select><ChevronDown size={14} /></label>{canSchedule && <label className="mine-toggle"><input type="checkbox" checked={mineOnly} onChange={(event) => setMineOnly(event.target.checked)} />仅看我的面试</label>}</div>
      {view === "list" ? <div className="interview-table">
        <div className="interview-table-head"><span>候选人</span><span>职位与轮次</span><span>时间与方式</span><span>面试官</span><span>面试状态</span><span>邀请/反馈状态</span><span>待办</span></div>
        {filtered.map((record) => {
          const primaryAction = getInterviewPrimaryAction(record, { canSchedule, userId: interviewerId });
          return <div className="interview-table-row" key={record.id}>
            <button className="interview-person" type="button" aria-label={`查看${record.candidate}的面试材料与评价`} onClick={() => onFeedback(record)}><span>{record.candidate.slice(-1)}</span><span><strong>{record.candidate}</strong><small>{record.role}</small></span></button>
            <span><strong>{record.position}</strong><small>{record.round}</small></span>
            <span><strong>{record.dateLabel} {record.time}</strong><small>{record.method} · {record.duration} 分钟</small><button className="interview-calendar-action" type="button" onClick={() => onDownload(record)}><CalendarPlus size={13} />添加到日历</button></span>
            <span><strong>{record.interviewers.join("、")}</strong><small>{record.location}</small></span>
            <span><StatusTag status={record.status} /></span>
            <span className="interview-state-stack"><StatusTag status={record.notification} /><StatusTag status={record.feedbackStatus} /></span>
            <span className="interview-row-actions">
              {primaryAction && <button className="interview-primary-action" type="button" onClick={() => runPrimaryAction(record)}>{primaryAction.label}<ChevronRight size={14} /></button>}
              {canSchedule && ["已安排", "已确认"].includes(record.status) && <button type="button" onClick={() => onSchedule(record)}>改期</button>}
              {getInterviewTerminalActions(record, canSchedule).map((action) => <button type="button" className="danger-link" key={action.target} onClick={() => requestTerminalTransition(record, action)}>{action.label}</button>)}
            </span>
          </div>;
        })}
        {filtered.length === 0 && <div className="interview-empty"><CalendarDays size={24} /><strong>没有符合条件的面试</strong><span>调整筛选条件或安排新的面试。</span></div>}
      </div> : <InterviewCalendar query={query} status={status} mineOnly={mineOnly} canSchedule={canSchedule} interviewerId={interviewerId} onLoadRange={onLoadRange} onOpen={openCalendarInterview} />}
      {nextCursor && <footer className="interview-pagination"><button className="button secondary" type="button" disabled={loadingMore} onClick={onLoadMore}>{loadingMore ? "正在加载" : "加载更多面试"}</button></footer>}
    </section>}
    {transitionDraft && <div className="modal-backdrop" role="presentation" onMouseDown={() => !transitioning && setTransitionDraft(null)}><section className="modal interview-transition-modal" role="dialog" aria-modal="true" aria-label={transitionDraft.label} onMouseDown={(event) => event.stopPropagation()}><header className="modal-header"><div><h2>{transitionDraft.label}</h2><p>{transitionDraft.record.candidate} · {transitionDraft.record.position}</p></div><button className="icon-button" type="button" aria-label="关闭" disabled={transitioning} onClick={() => setTransitionDraft(null)}><X size={20} /></button></header><div className="modal-body"><label>操作原因 <span>*</span><textarea rows="4" value={reason} disabled={transitioning} onChange={(event) => setReason(event.target.value)} placeholder="请填写操作原因" /></label>{!reason.trim() && <small className="field-hint">原因会写入面试历史，便于 HR 后续追踪。</small>}</div><footer className="modal-footer"><button className="button secondary" type="button" disabled={transitioning} onClick={() => setTransitionDraft(null)}>取消</button><button className="button danger" type="button" disabled={transitioning || !reason.trim()} onClick={() => void submitTerminalTransition()}>{transitioning ? "正在保存" : "确认操作"}</button></footer></section></div>}
  </div>;
}

const ratingOptions = ["待评价", "需提升", "一般", "良好", "优秀"];

function FeedbackMaterials({ record, materialsState, onRetry }) {
  const [previewOpen, setPreviewOpen] = useState(false);
  const materials = materialsState.data;
  const priorities = materials
    ? [...materials.interviewFocus.requiredMissing, ...materials.interviewFocus.risks]
    : record.jdPriorities;
  const questions = materials?.interviewFocus.suggestedQuestions || record.suggestedQuestions;
  const summary = materials?.jd?.description || record.summary;
  const previewText = materialsState.data?.resume?.previewText || "";

  return <><section className="feedback-material"><header><h3>本次面试重点</h3><button type="button" disabled={!previewText} onClick={() => setPreviewOpen(true)}><FileText size={14} />查看脱敏简历</button></header>{materialsState.status === "loading" && <p>正在加载候选人材料</p>}{materialsState.status === "error" && <div className="feedback-submit-error" role="alert"><CircleAlert size={17} /><div><strong>材料加载失败</strong><p>反馈草稿不受影响，可以重试加载候选人材料。</p></div><button type="button" onClick={onRetry}><RefreshCw size={14} />重试</button></div>}<div className="feedback-priorities">{priorities.map((item) => <span key={item}>{item}</span>)}</div><p>{summary}</p><details><summary>建议问题（{questions.length}）</summary>{questions.map((item) => <p key={item}>· {item}</p>)}</details></section>{previewOpen && <div className="modal-backdrop" role="presentation" onMouseDown={() => setPreviewOpen(false)}><section className="modal" role="dialog" aria-modal="true" aria-label="脱敏简历预览" onMouseDown={(event) => event.stopPropagation()}><header className="modal-header"><div><h2>脱敏简历预览</h2><p>仅包含本次面试授权范围内的脱敏文本，不提供原文件下载。</p></div><button className="icon-button" type="button" aria-label="关闭脱敏简历预览" onClick={() => setPreviewOpen(false)}><X size={20} /></button></header><div className="modal-body"><pre>{previewText}</pre></div></section></div>}</>;
}

function FeedbackForm({ record, onBack, backLabel, onSaved, onNotify, actorName = "张小北", userId, controller }) {
  const emptyForm = { ratings: { professional: "待评价", problem: "待评价", communication: "待评价", fit: "待评价" }, strengths: "", risks: "", conclusion: "", notes: "" };
  const ownsFeedback = record.interviewerIds.includes(userId) || record.interviewers.includes(actorName);
  const localDraft = useMemo(() => loadInterviewFeedbackDraft(userId, record), [record, userId]);
  const [existing, setExisting] = useState(null);
  const [summaryFeedbacks, setSummaryFeedbacks] = useState([]);
  const [feedbackVersion, setFeedbackVersion] = useState(0);
  const [loading, setLoading] = useState(true);
  const [editing, setEditing] = useState(ownsFeedback);
  const [editReason, setEditReason] = useState("");
  const [draftState, setDraftState] = useState(localDraft ? "本机草稿" : "草稿未保存");
  const [errors, setErrors] = useState({});
  const [submitError, setSubmitError] = useState("");
  const [submitting, setSubmitting] = useState(false);
  const [submissionPending, setSubmissionPending] = useState(false);
  const [form, setForm] = useState(() => localDraft || emptyForm);
  const [materialsState, setMaterialsState] = useState({ status: "loading", data: null });
  const [materialsReload, setMaterialsReload] = useState(0);

  useEffect(() => {
    const abortController = new AbortController();
    let active = true;
    setMaterialsState((current) => ({ status: "loading", data: current.data }));
    void controller.getMaterials(record.id, { signal: abortController.signal }).then((data) => {
      if (active) setMaterialsState({ status: "ready", data });
    }).catch((error) => {
      if (active && error?.name !== "AbortError") setMaterialsState((current) => ({ status: "error", data: current.data }));
    });
    return () => { active = false; abortController.abort(); };
  }, [controller, materialsReload, record.id]);

  useEffect(() => {
    const abortController = new AbortController();
    let active = true;
    setLoading(true);
    const requestOptions = { signal: abortController.signal };
    const feedbackRequest = ownsFeedback ? controller.getMyFeedback(record.id, requestOptions) : controller.listFeedbacks(record.id, requestOptions);
    void feedbackRequest.then((feedback) => {
      if (!active) return;
      if (!ownsFeedback) {
        setSummaryFeedbacks(feedback);
        setDraftState(feedback.length ? `已提交 ${feedback.length} 份` : "暂无已提交反馈");
        setEditing(false);
        setSubmitError("");
        return;
      }
      setFeedbackVersion(feedback.version);
      if (feedback.id && ["submitted", "amended"].includes(feedback.status)) {
        setExisting({ ...feedback, submittedBy: actorName, submittedAt: feedback.submittedAt || "已提交", canEdit: ownsFeedback });
        setForm(feedback);
        setEditing(false);
        setDraftState("已提交");
      } else {
        const resolved = resolveInterviewFeedbackDraft(localDraft, feedback);
        if (resolved.form) setForm(resolved.form);
        if (resolved.source === "server") {
          clearInterviewFeedbackDraft(userId, record.id);
          setDraftState("服务端草稿");
        }
      }
      setSubmitError("");
    }).catch((error) => {
      if (active && error?.name !== "AbortError") setSubmitError("反馈加载失败；本机草稿仍可继续编辑，提交前请重试加载。");
    }).finally(() => { if (active) setLoading(false); });
    return () => { active = false; abortController.abort(); };
  }, [actorName, controller, localDraft, ownsFeedback, record.id, userId]);

  useEffect(() => {
    if (!editing) return undefined;
    setDraftState("保存中...");
    const timer = window.setTimeout(() => {
      saveInterviewFeedbackDraft(userId, record.id, form);
      setDraftState("草稿已保存");
    }, 450);
    return () => {
      window.clearTimeout(timer);
      saveInterviewFeedbackDraft(userId, record.id, form);
    };
  }, [editing, form, record.id, userId]);

  function update(field, value) { setForm((current) => ({ ...current, [field]: value })); setErrors((current) => ({ ...current, [field]: "" })); setSubmitError(""); }
  function rate(field, value) { setForm((current) => ({ ...current, ratings: { ...current.ratings, [field]: value } })); setErrors((current) => ({ ...current, [field]: "" })); }
  async function submit() {
    const next = {};
    Object.entries(form.ratings).forEach(([key, value]) => { if (value === "待评价") next[key] = "请选择评价"; });
    if (!form.strengths.trim()) next.strengths = "请填写候选人优点";
    if (!form.risks.trim()) next.risks = "请填写风险或待确认项";
    if (!form.conclusion) next.conclusion = "请选择面试结论";
    if (existing && !editReason.trim()) next.editReason = "修改已提交反馈必须填写原因";
    setErrors(next); if (Object.keys(next).length) return;
    setSubmitting(true); setSubmitError("");
    saveInterviewFeedbackDraft(userId, record.id, form);
    try {
      let feedback;
      if (existing) {
        feedback = await controller.amendFeedback(existing, form, editReason);
      } else {
        if (!submissionPending) {
          const saved = await controller.saveMyFeedback(record.id, form, feedbackVersion);
          setFeedbackVersion(saved.version);
          setSubmissionPending(true);
        }
        feedback = await controller.submitMyFeedback(record.id);
      }
      const submitted = { ...feedback, submittedBy: actorName, submittedAt: feedback.submittedAt || "刚刚", canEdit: true };
      setExisting(submitted);
      setForm(feedback);
      setFeedbackVersion(feedback.version);
      clearInterviewFeedbackDraft(userId, record.id);
      setSubmissionPending(false);
      setEditing(false); setDraftState("已提交");
      await onSaved(record, submitted);
      onNotify(existing ? "面试反馈修改已保存" : "面试反馈已提交，下一步由 HR 处理");
    } catch (error) {
      if (error?.name !== "AbortError") {
        if (!isAmbiguousFeedbackSubmitError(error)) setSubmissionPending(false);
        setSubmitError(getFeedbackSubmitError(error));
      }
    } finally {
      setSubmitting(false);
    }
  }

  if (!ownsFeedback) {
    return <div className="interview-page feedback-page"><button className="back-link" type="button" onClick={onBack}><ArrowLeft size={17} />{backLabel}</button><header className="feedback-header"><div className="feedback-candidate"><span>{record.candidate.slice(-1)}</span><div><div><h2>{record.candidate}</h2><StatusTag status={draftState} /></div><p>{record.position} · {record.round} · {record.dateLabel} {record.time}</p></div></div><div className="feedback-header-actions"><span><CheckCircle2 size={14} />{loading ? "正在加载反馈" : draftState}</span></div></header>
      <InterviewFeedbackWorkspace record={record} controller={controller}><div className="feedback-layout"><main className="feedback-main"><FeedbackMaterials record={record} materialsState={materialsState} onRetry={() => setMaterialsReload((value) => value + 1)} />
        <section className="feedback-form-section"><header><h3>已提交反馈</h3><p>仅展示已提交或已修订的面试反馈，草稿对其他人不可见。</p></header>{loading && <div className="workbench-status" role="status">正在加载反馈汇总</div>}{!loading && summaryFeedbacks.length === 0 && <div className="workbench-status empty">暂无已提交反馈</div>}{summaryFeedbacks.map((feedback) => <article className="submitted-feedback-card" key={feedback.id}><header><strong>{feedback.author?.name || "面试官"}</strong><StatusTag status="已提交" /></header><p><strong>结论：</strong>{feedback.conclusion || "未填写"}</p><p><strong>候选人优点：</strong>{feedback.strengths || "未填写"}</p><p><strong>风险与待确认项：</strong>{feedback.risks || "未填写"}</p>{feedback.notes && <p><strong>补充说明：</strong>{feedback.notes}</p>}<div className="feedback-priorities">{formatSubmittedFeedbackRatings(feedback.ratings).map((rating) => <span key={rating}>{rating}</span>)}</div></article>)}{submitError && <div className="feedback-submit-error" role="alert"><CircleAlert size={18} /><p>{submitError}</p></div>}</section></main><aside className="feedback-aside"><section><h3>面试信息</h3><dl><div><dt>方式</dt><dd>{record.method}</dd></div><div><dt>时长</dt><dd>{record.duration} 分钟</dd></div><div><dt>面试官</dt><dd>{record.interviewers.join("、")}</dd></div><div><dt>面试安排负责人</dt><dd>{record.owner}</dd></div></dl></section><section className="permission-note"><AlertTriangle size={17} /><div><strong>只读反馈汇总</strong><p>你可以查看已提交结果，但不能查看或修改面试官草稿。</p></div></section></aside></div></InterviewFeedbackWorkspace></div>;
  }
  return <div className="interview-page feedback-page"><button className="back-link" type="button" onClick={onBack}><ArrowLeft size={17} />{backLabel}</button><header className="feedback-header"><div className="feedback-candidate"><span>{record.candidate.slice(-1)}</span><div><div><h2>{record.candidate}</h2><StatusTag status={draftState} /></div><p>{record.position} · {record.round} · {record.dateLabel} {record.time}</p></div></div><div className="feedback-header-actions"><span><CheckCircle2 size={14} />{loading ? "正在加载反馈" : draftState}</span>{existing && !editing && ownsFeedback && <button className="button secondary" type="button" onClick={() => setEditing(true)}>修改反馈</button>}</div></header>
    <InterviewFeedbackWorkspace record={record} controller={controller}><div className="feedback-layout"><main className="feedback-main"><FeedbackMaterials record={record} materialsState={materialsState} onRetry={() => setMaterialsReload((value) => value + 1)} />
      <section className="feedback-form-section"><header><h3>结构化评价</h3><p>仅当前面试官可编辑自己的草稿和反馈。</p></header>{feedbackRatingDimensions.map(([key, label]) => <div className="rating-row" key={key}><strong>{label}<span>*</span></strong><div>{ratingOptions.slice(1).map((option) => <button type="button" disabled={!editing || submitting} className={form.ratings[key] === option ? "active" : ""} key={option} onClick={() => rate(key, option)}>{option}</button>)}</div>{errors[key] && <small className="field-error">{errors[key]}</small>}</div>)}<label>候选人优点 <span>*</span><textarea disabled={!editing || submitting} rows="4" value={form.strengths} onChange={(event) => update("strengths", event.target.value)} placeholder="记录与岗位相关的优势和证据" />{errors.strengths && <small className="field-error">{errors.strengths}</small>}</label><label>风险与待确认项 <span>*</span><textarea disabled={!editing || submitting} rows="4" value={form.risks} onChange={(event) => update("risks", event.target.value)} placeholder="记录风险、信息缺口或后续建议" />{errors.risks && <small className="field-error">{errors.risks}</small>}</label><div className="feedback-conclusion"><strong>面试结论 <span>*</span></strong><div>{["强烈推荐", "推荐", "待补充评估", "不推荐"].map((option) => <button type="button" disabled={!editing || submitting} className={form.conclusion === option ? "active" : ""} key={option} onClick={() => update("conclusion", option)}>{option}</button>)}</div>{errors.conclusion && <small className="field-error">{errors.conclusion}</small>}</div><label>补充说明<textarea disabled={!editing || submitting} rows="3" value={form.notes} onChange={(event) => update("notes", event.target.value)} placeholder="可选：给 HR 或下一轮面试官的建议" /></label>{existing && editing && <label className="edit-reason">修改原因 <span>*</span><input disabled={submitting} value={editReason} onChange={(event) => { setEditReason(event.target.value); setErrors((current) => ({ ...current, editReason: "" })); }} placeholder="说明为什么需要修改已提交反馈" />{errors.editReason && <small className="field-error">{errors.editReason}</small>}</label>}{submitError && <div className="feedback-submit-error" role="alert"><CircleAlert size={18} /><div><strong>反馈请求失败</strong><p>{submitError}</p></div><button type="button" disabled={submitting || loading} onClick={() => void submit()}><RefreshCw size={14} />重试提交</button></div>}{editing && <footer><span><Check size={14} />{draftState}</span><button className="button primary" type="button" disabled={submitting || loading || !ownsFeedback} onClick={() => void submit()}><Send size={16} />{submitting ? "正在提交" : "提交反馈"}</button></footer>}</section></main><aside className="feedback-aside"><section><h3>面试信息</h3><dl><div><dt>方式</dt><dd>{record.method}</dd></div><div><dt>时长</dt><dd>{record.duration} 分钟</dd></div><div><dt>面试官</dt><dd>{record.interviewers.join("、")}</dd></div><div><dt>面试安排负责人</dt><dd>{record.owner}</dd></div></dl></section><section><h3>提交后</h3><p>HR 将汇总本轮反馈，并决定推进、追加面试、淘汰或加入人才库。</p></section>{!ownsFeedback && <section className="permission-note"><AlertTriangle size={17} /><div><strong>只读反馈</strong><p>你不是该面试的反馈参与人。</p></div></section>}</aside></div></InterviewFeedbackWorkspace></div>;
}

export function InterviewsWorkspace({ mode, setMode, selectedInterviewId, setSelectedInterviewId, scheduleCandidateId, records, status, error, onRetry, nextCursor, loadingMore, onLoadMore, candidates, onNotify, onBack, backLabel = "返回面试列表", onOpenSubView, onRecordsChanged, canSchedule = true, actorName = "张小北", actorId, controller, pageActionHost }) {
  const selectedInterview = records.find((item) => item.id === selectedInterviewId) || null;
  const scheduleCandidate = candidates.find((item) => item.id === scheduleCandidateId || item.candidateId === scheduleCandidateId) || candidates[0] || null;
  const participantApplicationId = selectedInterview?.applicationId || scheduleCandidate?.applicationId || scheduleCandidate?.application?.id || "";
  const [participantDirectory, setParticipantDirectory] = useState([]);
  const [participantStatus, setParticipantStatus] = useState("idle");

  useEffect(() => {
    let active = true;
    if (mode !== "schedule" || !participantApplicationId) {
      setParticipantDirectory([]);
      setParticipantStatus("idle");
      return () => { active = false; };
    }
    setParticipantDirectory([]);
    setParticipantStatus("loading");
    controller.listParticipantOptions(participantApplicationId).then((options) => {
      if (!active) return;
      setParticipantDirectory(options);
      setParticipantStatus("ready");
    }).catch((requestError) => {
      if (!active || requestError?.name === "AbortError") return;
      setParticipantDirectory([]);
      setParticipantStatus("error");
    });
    return () => { active = false; };
  }, [controller, mode, participantApplicationId]);

  const participantOptions = useMemo(() => {
    const people = new Map(participantDirectory.map((person) => [person.id, person]));
    (selectedInterview?.participants || []).forEach((person) => people.set(person.id, { id: person.id, name: person.name }));
    return [...people.values()];
  }, [participantDirectory, selectedInterview]);

  function openSchedule(record) { if (!canSchedule) { onNotify("面试官不能创建或改期面试"); return; } onOpenSubView?.(); setSelectedInterviewId(record?.id || null); setMode("schedule"); }
  function openFeedback(record) { onOpenSubView?.(); setSelectedInterviewId(record.id); setMode("feedback"); }
  function backToList() { if (onBack?.()) return; setSelectedInterviewId(null); setMode("list"); }

  async function saveRecord(record, form) {
    const saved = await controller.save(record, form);
    await onRecordsChanged(saved);
    backToList();
  }

  async function downloadCalendar(record) {
    try {
      const { blob, filename } = await controller.downloadCalendar(record.id);
      const url = URL.createObjectURL(blob);
      const anchor = document.createElement("a");
      anchor.href = url;
      anchor.download = filename;
      anchor.click();
      URL.revokeObjectURL(url);
    } catch (error) {
      if (error?.name !== "AbortError") onNotify("日历文件下载失败，请检查网络后重试");
    }
  }

  async function transitionRecord(record, target, reason = null) {
    try {
      const updated = await controller.transition(record, target, { reason });
      await onRecordsChanged(updated);
      const messages = {
        confirmed: "面试已确认",
        completed: "面试已完成，反馈任务已生成",
        cancelled: "面试已取消，请下载更新后的日历文件",
        no_show: "已标记候选人未到场",
      };
      onNotify(messages[target] || "面试状态已更新");
      return true;
    } catch (error) {
      if (error?.name !== "AbortError") onNotify("面试状态更新失败，请刷新后重试");
      return false;
    }
  }

  if (mode === "schedule" && canSchedule) return <ScheduleWorkspace record={selectedInterview} candidateId={scheduleCandidateId} candidates={candidates} participantOptions={participantOptions} participantStatus={participantStatus} onBack={backToList} backLabel={backLabel} onSave={saveRecord} onCheckConflicts={(record, form) => controller.checkConflicts(record?.id, form)} onGetAvailability={(filters, options) => controller.availability(filters, options)} onNotify={onNotify} />;
  if (mode === "feedback" && selectedInterview) return <FeedbackForm record={selectedInterview} onBack={backToList} backLabel={backLabel} onSaved={async (_record, feedback) => { await onRecordsChanged({ ...selectedInterview, feedback, feedbackStatus: "已提交" }); }} onNotify={onNotify} actorName={actorName} userId={actorId} controller={controller} />;
  return <InterviewList records={records} status={status} error={error} onRetry={onRetry} nextCursor={nextCursor} loadingMore={loadingMore} onLoadMore={onLoadMore} onLoadRange={(range, options) => controller.listRange({ ...range, timezone: "Asia/Shanghai" }, options)} onSchedule={openSchedule} onFeedback={openFeedback} onDownload={(record) => void downloadCalendar(record)} onTransition={transitionRecord} canSchedule={canSchedule} interviewerId={actorId} pageActionHost={pageActionHost} />;
}
