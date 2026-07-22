import { useCallback, useEffect, useRef, useState } from "react";
import "./product-theme-jobs-screening.css";
import {
  ArrowLeft,
  Bot,
  BriefcaseBusiness,
  CalendarDays,
  Check,
  ChevronDown,
  ChevronRight,
  CircleAlert,
  CirclePause,
  CirclePlay,
  Clock3,
  FileText,
  Import,
  ListFilter,
  Pencil,
  Plus,
  Search,
  Users,
  X,
} from "lucide-react";
import { mergeCandidateRecords } from "./candidateController.js";
import {
  JOB_EDIT_CONFLICT_REFRESHED_MESSAGE,
  getJobFormActions,
  getJobSaveSuccessMessage,
} from "./jobController.js";
import { commitJobMutation, getJobDefinitionErrors, retryJobRefresh } from "./jobWorkspaceState.js";
import { PagePrimaryAction } from "./PagePrimaryAction.jsx";
import { applicationStageLabel } from "./recruitingTerminology.js";
import { workflowTemplateController } from "./workflowTemplateController.js";

// Legacy workflow scenarios still import this fixture. The authenticated job
// workspace never uses it as list or detail data.
export const initialPositionRecords = [
  { id: "JOB-AI-001", name: "AI 工程师", department: "技术部", location: "北京", owner: "张小北", status: "招聘中", priority: "高", headcount: 3, candidates: 48, review: 8, interview: 5, decision: 3, updated: "今天 11:05", jd: "负责大模型应用、AI Agent、RAG 检索增强生成等 AI 应用的设计、开发和落地。", mustHave: ["Python", "机器学习", "深度学习", "LLM"], niceToHave: ["RAG", "Agent", "Docker", "Kubernetes"], process: "技术岗位标准流程" },
  { id: "JOB-JAVA-002", name: "Java 后端工程师", department: "技术部", location: "上海", owner: "陈雨", status: "招聘中", priority: "中", headcount: 2, candidates: 32, review: 6, interview: 4, decision: 1, updated: "今天 09:42", jd: "负责核心业务服务的设计与开发，建设稳定、可观测的微服务体系。", mustHave: ["Java", "Spring Boot", "MySQL", "Redis"], niceToHave: ["Kafka", "Kubernetes", "高并发"], process: "技术岗位标准流程" },
  { id: "JOB-PM-003", name: "产品经理", department: "产品部", location: "北京", owner: "张小北", status: "招聘中", priority: "中", headcount: 1, candidates: 21, review: 4, interview: 2, decision: 1, updated: "昨天 18:20", jd: "负责企业服务产品的需求分析、方案设计、项目推进和效果复盘。", mustHave: ["B 端产品", "需求分析", "项目管理"], niceToHave: ["招聘行业", "数据分析", "AI 产品"], process: "产品岗位标准流程" },
  { id: "JOB-FE-004", name: "前端工程师", department: "技术部", location: "深圳", owner: "刘思远", status: "草稿", priority: "低", headcount: 2, candidates: 0, review: 0, interview: 0, decision: 0, updated: "07-10 16:30", jd: "负责招聘协同平台 Web 端开发与体验优化。", mustHave: ["React", "TypeScript", "CSS"], niceToHave: ["数据可视化", "设计系统"], process: "技术岗位标准流程" },
  { id: "JOB-OPS-005", name: "招聘运营专员", department: "人力资源部", location: "北京", owner: "王敏", status: "已暂停", priority: "低", headcount: 1, candidates: 15, review: 2, interview: 0, decision: 0, updated: "07-09 14:10", jd: "负责招聘渠道运营、数据分析和候选人体验提升。", mustHave: ["招聘运营", "数据分析"], niceToHave: ["ATS 使用经验", "雇主品牌"], process: "职能岗位标准流程" },
];

const legacyProcessTemplates = {
  "标准社招流程": ["新简历", "用人经理评审", "确认候选人意向", "面试", "录用决策", "录用"],
  "技术岗位流程": ["新简历", "用人经理评审", "一面", "二面", "录用决策", "录用"],
  "精简流程": ["新简历", "面试", "录用决策", "录用"],
};

function formProcessTemplate(value) {
  if (Object.hasOwn(legacyProcessTemplates, value)) return value;
  if (value === "技术岗位标准流程") return "技术岗位流程";
  return "标准社招流程";
}

const JOB_STATUSES = ["全部", "招聘中", "草稿", "已暂停", "已关闭", "已归档"];
const CANDIDATE_STAGES = ["全部阶段", "新简历", "待复核", "待沟通", "待安排", "面试中", "待决策", "已淘汰"];
const DEFAULT_CANDIDATE_FILTERS = Object.freeze({ q: "", stage: "全部阶段" });
const FUNNEL_STAGES = [
  ["新简历", "new"],
  ["待复核", "review"],
  ["待沟通", "contact"],
  ["待安排", "interview_pending"],
  ["面试中", "interviewing"],
  ["待决策", "decision"],
];
const LIFECYCLE_ACTIONS = {
  招聘中: [["已暂停", "暂停招聘", CirclePause], ["已关闭", "关闭职位", X]],
  已暂停: [["招聘中", "恢复招聘", CirclePlay], ["已关闭", "关闭职位", X]],
  已关闭: [["已归档", "归档职位", Check]],
};

function StatusTag({ children }) {
  const className = children === "招聘中" ? "status-active" : children === "草稿" ? "status-draft" : "status-paused";
  return <span className={`job-status ${className}`}>{children || "状态未知"}</span>;
}

function JobList({ state, onLoad, onOpen, onCreate, pageActionHost }) {
  const [query, setQuery] = useState(state.filters.q);
  const firstQueryRender = useRef(true);

  useEffect(() => {
    if (firstQueryRender.current) {
      firstQueryRender.current = false;
      return undefined;
    }
    const timer = window.setTimeout(() => {
      if (query !== state.filters.q) void onLoad({ ...state.filters, q: query });
    }, 300);
    return () => window.clearTimeout(timer);
  }, [onLoad, query, state.filters]);

  const total = Object.values(state.statusCounts).reduce((sum, count) => sum + count, 0);
  const updateFilters = (changes) => void onLoad({ ...state.filters, ...changes });
  const clearFilters = () => {
    setQuery("");
    void onLoad({ q: "", status: "全部", departmentId: "", ownerId: "" });
  };

  return (
    <div className="job-page job-list-page">
      <PagePrimaryAction host={pageActionHost}>{onCreate && <button className="button primary" type="button" onClick={onCreate}><Plus size={17} />新建职位</button>}</PagePrimaryAction>
      <div className="job-page-heading"><div><h2>职位管理</h2><p>统一维护招聘职位、负责人和候选人推进情况。</p></div></div>
      <div className="job-status-tabs" role="tablist" aria-label="职位状态">
        {JOB_STATUSES.map((item) => <button key={item} role="tab" aria-selected={state.filters.status === item} type="button" className={state.filters.status === item ? "active" : ""} onClick={() => updateFilters({ status: item })}>{item}<span>{item === "全部" ? total : state.statusCounts[item] || 0}</span></button>)}
      </div>

      <section className="job-list-panel">
        <div className="job-filters">
          <label className="search-control"><Search size={17} /><input aria-label="搜索职位" value={query} onChange={(event) => setQuery(event.target.value)} placeholder="搜索职位名称" /></label>
          <label className="select-control"><BriefcaseBusiness size={16} /><select aria-label="部门筛选" value={state.filters.departmentId} onChange={(event) => updateFilters({ departmentId: event.target.value })}><option value="">全部部门</option>{state.departments.map((item) => <option key={item.id} value={item.id}>{item.name}</option>)}</select><ChevronDown size={14} /></label>
          <label className="select-control"><Users size={16} /><select aria-label="负责人筛选" value={state.filters.ownerId} onChange={(event) => updateFilters({ ownerId: event.target.value })}><option value="">全部负责人</option>{state.owners.map((item) => <option key={item.id} value={item.id}>{item.name}</option>)}</select><ChevronDown size={14} /></label>
          <button className="button secondary compact" type="button" onClick={clearFilters}><X size={15} />清空</button>
        </div>

        {state.status === "error" && <div className="job-request-state error" role="alert"><CircleAlert size={17} /><span>{state.error}</span><button type="button" onClick={() => onLoad(state.filters)}>重试</button></div>}
        {state.status === "loading" && state.records.length > 0 && <div className="job-request-state" role="status">正在更新职位列表…</div>}

        <div className="job-table" role="table" aria-label="职位列表" aria-busy={state.status === "loading"}>
          <div className="job-table-head" role="row"><span>职位</span><span>状态</span><span>招聘负责人</span><span>招聘进度</span><span>候选人</span><span>更新时间</span><span>操作</span></div>
          {state.records.map((job) => (
            <button className="job-table-row" role="row" type="button" key={job.id} onClick={() => onOpen(job)}>
              <span className="job-title-cell"><strong>{job.name || "未命名职位"}</strong><small>{job.department || "未分配部门"}</small></span>
              <span><StatusTag>{job.status}</StatusTag></span>
              <span className="owner-cell"><span>{(job.owner || "未").slice(0, 1)}</span>{job.owner || "未分配"}</span>
              <span className="progress-cell"><strong>{job.headcount} 人</strong><small>优先级：{job.priority || "未设置"}</small></span>
              <span className="candidate-count"><strong>{job.candidates}</strong><small>待用人经理评审 {job.review}</small></span>
              <span className="updated-cell">{job.updated}</span>
              <span className="row-actions"><span title="查看职位"><ChevronRight size={18} /></span></span>
            </button>
          ))}
          {state.status === "loading" && state.records.length === 0 && <div className="job-empty" role="status"><strong>正在加载职位…</strong></div>}
          {state.status === "ready" && state.records.length === 0 && <div className="job-empty"><ListFilter size={25} /><strong>没有符合条件的职位</strong><span>调整搜索词或清空筛选条件后重试。</span></div>}
        </div>
        {state.nextCursor && <div className="job-load-more"><button className="button secondary" type="button" disabled={state.status === "loading"} onClick={() => onLoad(state.filters, { append: true, cursor: state.nextCursor })}>{state.status === "loading" ? "正在加载…" : "加载更多"}</button></div>}
      </section>
    </div>
  );
}

function JobDialog({ onClose, onDiscard, onSave, saving }) {
  return <div className="job-confirm-backdrop" role="presentation" onMouseDown={onClose}><section className="job-confirm" role="dialog" aria-modal="true" aria-label="保存未完成的职位" onMouseDown={(event) => event.stopPropagation()}><header><CircleAlert size={21} /><h3>职位尚未保存</h3></header><p>你填写的内容还没有保存。可以先保存为草稿，或者放弃本次修改。</p><footer><button className="button secondary" type="button" onClick={onClose} disabled={saving}>继续编辑</button><button className="button danger-text" type="button" onClick={onDiscard} disabled={saving}>放弃修改</button><button className="button primary" type="button" onClick={onSave} disabled={saving}>保存草稿</button></footer></section></div>;
}

function JobForm({ initialJob, initialDraft, departments, owners, ownersStatus, workflowTemplates, workflowTemplatesStatus, onBack, onDiscard, onSubmit, onRetryConflictRefresh, onManageDepartments, onManageTemplates, onDraftChange, onDraftClear = () => {}, onRetryOwners, pageActionHost }) {
  const actions = getJobFormActions(initialJob);
  const [values, setValues] = useState({
    name: initialJob?.name || initialDraft?.name || "",
    departmentId: initialJob?.departmentId || initialDraft?.departmentId || "",
    location: initialJob?.location || initialDraft?.location || "",
    headcount: initialJob?.headcount || initialDraft?.headcount || 1,
    ownerId: initialJob?.hiringOwnerId || initialJob?.ownerId || initialDraft?.ownerId || "",
    priority: initialJob?.priority || initialDraft?.priority || "中",
    jd: initialJob?.jd || initialDraft?.jd || "",
    mustHave: initialJob?.mustHave?.join("、") || initialDraft?.mustHave || "",
    niceToHave: initialJob?.niceToHave?.join("、") || initialDraft?.niceToHave || "",
    process: initialJob ? formProcessTemplate(initialJob.process) : formProcessTemplate(initialDraft?.process),
    workflowTemplateId: initialJob?.workflowTemplateId || initialDraft?.workflowTemplateId || "",
    llmEnabled: initialJob ? initialJob.llmEnabled === true : initialDraft?.llmEnabled === true,
  });
  const [errors, setErrors] = useState({});
  const [dirty, setDirty] = useState(Boolean(!initialJob && initialDraft));
  const [saving, setSaving] = useState(false);
  const [submitError, setSubmitError] = useState("");
  const [conflictRefreshFailed, setConflictRefreshFailed] = useState(false);
  const submitErrorRef = useRef(null);
  const [confirmExit, setConfirmExit] = useState(false);

  useEffect(() => {
    if (submitError) submitErrorRef.current?.focus();
  }, [submitError]);

  useEffect(() => {
    if (!workflowTemplates.length) return;
    setValues((current) => {
      const selected = workflowTemplates.find((item) => item.id === current.workflowTemplateId)
        || workflowTemplates.find((item) => item.name === current.process || (current.process === "技术岗位流程" && item.name === "技术岗位标准流程"))
        || workflowTemplates.find((item) => item.status === "active")
        || workflowTemplates[0];
      if (!selected || (current.workflowTemplateId === selected.id && current.process === selected.name)) return current;
      return { ...current, workflowTemplateId: selected.id, process: selected.name };
    });
  }, [workflowTemplates]);

  function change(field, value) {
    setValues((current) => {
      const next = { ...current, [field]: value };
      if (!initialJob && onDraftChange) onDraftChange(next);
      return next;
    });
    setDirty(true);
    if (!conflictRefreshFailed) setSubmitError("");
    setErrors((current) => ({ ...current, [field]: "" }));
  }

  function changeWorkflowTemplate(id) {
    const selected = workflowTemplates.find((item) => item.id === id);
    if (!selected) return;
    setValues((current) => {
      const next = { ...current, workflowTemplateId: selected.id, process: selected.name };
      if (!initialJob && onDraftChange) onDraftChange(next);
      return next;
    });
    setDirty(true);
    setErrors((current) => ({ ...current, process: "" }));
  }

  function validate() {
    const next = getJobDefinitionErrors(values);
    setErrors(next);
    return Object.keys(next).length === 0;
  }

  async function submit(publish) {
    if (saving || !validate()) return;
    setSaving(true);
    setSubmitError("");
    try {
      const outcome = await onSubmit(values, publish);
      if (outcome?.status === "conflict") {
        setSubmitError(outcome.error);
        setConflictRefreshFailed(outcome.retryable);
        return;
      }
      setConflictRefreshFailed(false);
      setDirty(false);
      if (!initialJob && onDraftClear) onDraftClear();
    } catch (error) {
      setSubmitError("职位保存失败，请检查网络后重试。当前表单内容已保留。");
    } finally {
      setSaving(false);
    }
  }

  async function retryConflictRefresh() {
    if (saving || !onRetryConflictRefresh) return;
    setSaving(true);
    try {
      const outcome = await onRetryConflictRefresh(values);
      setSubmitError(outcome.error);
      setConflictRefreshFailed(outcome.retryable);
    } finally {
      setSaving(false);
    }
  }

  const completion = [values.name, values.departmentId, values.jd, values.mustHave, values.process].filter(Boolean).length;
  const workflowUnavailable = workflowTemplatesStatus !== "ready" || !values.workflowTemplateId;
  return (
    <div className="job-page job-form-page">
      <PagePrimaryAction host={pageActionHost}><>{actions.secondary && <button className="button secondary" type="button" onClick={() => submit(actions.secondary.publish)} disabled={saving || workflowUnavailable}>{saving ? "正在保存…" : actions.secondary.label}</button>}<button className="button primary" type="button" onClick={() => submit(actions.primary.publish)} disabled={saving || workflowUnavailable}>{saving ? "正在保存…" : actions.primary.label}</button></></PagePrimaryAction>
      <button className="back-link" type="button" onClick={() => dirty ? setConfirmExit(true) : onBack()} disabled={saving}><ArrowLeft size={17} />返回职位列表</button>
      <div className="job-page-heading form-heading"><div><h2>{initialJob ? "编辑职位" : "新建职位"}</h2><p>填写职位信息和筛选标准，保存后以服务端记录为准。</p></div></div>
      {submitError && <div ref={submitErrorRef} tabIndex="-1" className="job-request-state error" role="alert"><CircleAlert size={17} /><span>{submitError}</span>{conflictRefreshFailed && <button type="button" onClick={retryConflictRefresh} disabled={saving}>{saving ? "正在刷新…" : "重试刷新"}</button>}</div>}
      <fieldset className="job-form-fieldset" disabled={saving}>
        <div className="job-form-layout">
          <div className="job-form-sections">
            <section className="form-section"><header><span>1</span><div><h3>基本信息</h3><p>设置职位归属、招聘目标和负责人。</p></div></header><div className="job-fields two-columns">
              <label>职位名称<input value={values.name} onChange={(event) => change("name", event.target.value)} placeholder="例如：平台工程师" />{errors.name && <small className="field-error">{errors.name}</small>}</label>
              <div className="job-department-field"><span className="field-label-row"><label htmlFor="job-department">所属部门</label><button aria-label="管理部门" type="button" onClick={onManageDepartments}>管理部门</button></span><select id="job-department" aria-label="所属部门" value={values.departmentId} onChange={(event) => change("departmentId", event.target.value)}><option value="">未分配部门</option>{departments.map((item) => <option key={item.id} value={item.id} disabled={item.status === "inactive" && item.id !== values.departmentId}>{item.name}{item.status === "inactive" ? "（已停用）" : ""}</option>)}</select></div>
              <label>工作地点<input value={values.location} onChange={(event) => change("location", event.target.value)} placeholder="例如：上海或远程" /></label>
              <label>招聘人数<input type="number" min="1" max="99" value={values.headcount} onChange={(event) => change("headcount", Number(event.target.value))} /></label>
              <label>招聘负责人（HR）<select value={values.ownerId} onChange={(event) => change("ownerId", event.target.value)} disabled={ownersStatus === "loading"}><option value="">{ownersStatus === "loading" ? "正在加载招聘负责人…" : "未分配招聘负责人"}</option>{owners.map((item) => <option key={item.id} value={item.id}>{item.name}</option>)}</select>{ownersStatus === "error" && <span className="field-state owner-directory-error" role="alert">招聘负责人加载失败。<button type="button" onClick={onRetryOwners}>重试</button></span>}</label>
              <label>优先级<span className="segmented-control" role="group" aria-label="职位优先级">{["高", "中", "低"].map((item) => <button key={item} type="button" aria-pressed={values.priority === item} className={values.priority === item ? "active" : ""} onClick={() => change("priority", item)}>{item}</button>)}</span></label>
            </div></section>
            <section className="form-section"><header><span>2</span><div><h3>职位描述与筛选标准</h3><p>JD 面向候选人展示，筛选标准仅供招聘团队使用。</p></div></header><div className="job-fields">
              <label>公开 JD<textarea rows="8" value={values.jd} onChange={(event) => change("jd", event.target.value)} placeholder="粘贴或输入完整职位描述" />{errors.jd && <small className="field-error">{errors.jd}</small>}</label>
              <label>必须条件<textarea rows="3" value={values.mustHave} onChange={(event) => change("mustHave", event.target.value)} placeholder="用顿号分隔" /></label>
              <label>加分项<textarea rows="3" value={values.niceToHave} onChange={(event) => change("niceToHave", event.target.value)} placeholder="用顿号分隔" /></label>
            </div></section>
            <section className="form-section recruitment-config"><header><span>3</span><div><h3>招聘配置</h3><p>选择招聘流程；LLM 是当前唯一的评分和路由来源。</p></div></header><div className="job-fields">
              <div className="job-template-field"><span className="field-label-row"><label htmlFor="job-workflow-template">流程模板</label><button type="button" onClick={onManageTemplates}>管理模板</button></span><select id="job-workflow-template" aria-label="流程模板" value={values.workflowTemplateId} disabled={workflowTemplatesStatus === "loading" || !workflowTemplates.length} onChange={(event) => changeWorkflowTemplate(event.target.value)}><option value="">{workflowTemplatesStatus === "loading" ? "正在加载流程模板…" : "请选择流程模板"}</option>{workflowTemplates.map((template) => <option key={template.id} value={template.id} disabled={template.status === "inactive" && template.id !== values.workflowTemplateId}>{template.name}{template.status === "inactive" ? "（已停用）" : ""}</option>)}</select>{workflowTemplatesStatus === "error" && <small className="field-error">流程模板加载失败，请稍后重试。</small>}{errors.process && <small className="field-error">{errors.process}</small>}</div>
              <div className="process-summary"><strong>阶段摘要</strong><span>新简历 → 用人经理评审 → 待安排面试 → {(workflowTemplates.find((item) => item.id === values.workflowTemplateId)?.rounds || []).join(" → ") || "请选择模板"} → 用人经理录用决策</span></div>
              <div className="toggle-row ai-evaluation-row"><span><Bot size={18} /><span><strong>AI 简历评估</strong><small>启用后由 LLM 生成评分、结论与依据，并自动转交用人经理评审。</small></span></span><label className="compact-switch"><input aria-label="AI 简历评估" type="checkbox" checked={values.llmEnabled} onChange={(event) => change("llmEnabled", event.target.checked)} /><span aria-hidden="true" /></label></div>
            </div></section>
          </div>
          <aside className="form-summary"><h3>发布检查</h3><div className="completion-ring"><strong>{completion}/5</strong><span>关键项已完成</span></div>{[["职位名称", values.name], ["所属部门", values.departmentId], ["公开 JD", values.jd], ["筛选条件", values.mustHave], ["招聘流程", values.process]].map(([label, value]) => <div className={value ? "check-row done" : "check-row"} key={label}>{value ? <Check size={15} /> : <Clock3 size={15} />}<span>{label}</span></div>)}</aside>
        </div>
      </fieldset>
      {confirmExit && <JobDialog onClose={() => setConfirmExit(false)} onDiscard={() => { onDraftClear(); onDiscard(); }} onSave={() => { setConfirmExit(false); void submit(false); }} saving={saving} />}
    </div>
  );
}

function JobDetail({ state, lifecycleState, refreshState, onBack, onEdit, onImport, onOpenCandidate, onReload, onRetryRefresh, onLoadCandidates, onTransition }) {
  const [tab, setTab] = useState("候选人");
  const [query, setQuery] = useState(state.candidates?.filters.q || "");
  const firstQueryRender = useRef(true);
  const job = state.job;
  const candidates = state.candidates;
  const candidateFilters = candidates?.filters || DEFAULT_CANDIDATE_FILTERS;

  useEffect(() => {
    if (!candidates) return undefined;
    if (firstQueryRender.current) { firstQueryRender.current = false; return undefined; }
    const timer = window.setTimeout(() => {
      if (query !== candidateFilters.q) void onLoadCandidates({ ...candidateFilters, q: query });
    }, 300);
    return () => window.clearTimeout(timer);
  }, [candidateFilters, candidates, onLoadCandidates, query]);

  if (state.status === "loading") return <div className="job-page"><button className="back-link" type="button" onClick={onBack}><ArrowLeft size={17} />返回职位列表</button><div className="job-detail-state" role="status">正在加载职位详情…</div></div>;
  if (state.status === "not-found") return <div className="job-page"><button className="back-link" type="button" onClick={onBack}><ArrowLeft size={17} />返回职位列表</button><div className="job-detail-state"><CircleAlert size={24} /><strong>无法查看该职位</strong><span>职位不存在，或你没有查看权限。</span><button className="button secondary" type="button" onClick={onReload}>重试</button></div></div>;
  if (state.status === "error") return <div className="job-page"><button className="back-link" type="button" onClick={onBack}><ArrowLeft size={17} />返回职位列表</button><div className="job-detail-state" role="alert"><CircleAlert size={24} /><strong>职位详情加载失败</strong><span>请检查网络连接后重试。</span><button className="button secondary" type="button" onClick={onReload}>重试</button></div></div>;
  if (!job || !candidates) return null;

  const lifecycleActions = LIFECYCLE_ACTIONS[job.status] || [];
  const writesDisabled = lifecycleState.status === "loading" || refreshState.retrying || Boolean(refreshState.error);
  return (
    <div className="job-page job-detail-page">
      <button className="back-link" type="button" onClick={onBack}><ArrowLeft size={17} />返回职位列表</button>
      <section className="job-detail-hero"><div className="job-detail-title"><span className="job-icon"><BriefcaseBusiness size={22} /></span><div><div><h2>{job.name}</h2><StatusTag>{job.status}</StatusTag></div><p>{job.department || "未分配部门"} · {job.location || "地点未填写"} · 负责人 {job.owner || "未分配"}</p></div></div><div className="job-detail-actions"><button className="button secondary" type="button" onClick={onEdit} disabled={writesDisabled}><Pencil size={16} />编辑职位</button>{lifecycleActions.map(([target, label, Icon]) => <button className="button secondary" type="button" key={target} onClick={() => onTransition(target)} disabled={writesDisabled}><Icon size={16} />{label}</button>)}{job.status === "招聘中" && <button className="button primary" type="button" onClick={onImport} disabled={writesDisabled}><Import size={16} />导入简历</button>}</div></section>
      {refreshState.error && <div className="job-request-state error" role="alert"><CircleAlert size={17} /><span>{refreshState.error}</span><button type="button" onClick={onRetryRefresh} disabled={refreshState.retrying}>{refreshState.retrying ? "正在读取…" : "重试读取"}</button></div>}
      {lifecycleState.error && <div className="job-request-state error" role="alert"><CircleAlert size={17} /><span>{lifecycleState.error}</span>{lifecycleState.conflict && <button type="button" onClick={onReload}>刷新职位</button>}</div>}
      <div className="job-metrics">{[["候选人总数", job.candidates, Users], ["待用人经理评审", job.review, FileText], ["面试流程中", job.interview, CalendarDays], ["待用人经理录用决策", job.decision, Clock3]].map(([label, value, Icon]) => <div key={label}><span><Icon size={18} /></span><div><strong>{value}</strong><small>{label}</small></div></div>)}</div>
      <section className="job-detail-panel">
        <div className="detail-tabs" role="tablist">{["候选人", "职位信息"].map((item) => <button key={item} role="tab" aria-selected={tab === item} type="button" className={tab === item ? "active" : ""} onClick={() => setTab(item)}>{item}</button>)}</div>
        {tab === "候选人" && <div className="detail-tab-content">
          <div className="funnel-strip">{FUNNEL_STAGES.map(([stage, key], index) => <button type="button" key={key} aria-pressed={candidates.filters.stage === stage} onClick={() => onLoadCandidates({ ...candidates.filters, stage })}><span>{applicationStageLabel(stage)}</span><strong>{job.funnel?.[key] || 0}</strong>{index < FUNNEL_STAGES.length - 1 && <ChevronRight size={15} />}</button>)}</div>
          <div className="candidate-toolbar"><label className="search-control"><Search size={16} /><input aria-label="搜索当前职位候选人" value={query} onChange={(event) => setQuery(event.target.value)} placeholder="搜索当前职位候选人" /></label><label className="select-control"><select aria-label="候选人阶段" value={candidates.filters.stage} onChange={(event) => onLoadCandidates({ ...candidates.filters, stage: event.target.value })}>{CANDIDATE_STAGES.map((stage) => <option key={stage} value={stage}>{stage === "全部阶段" ? stage : applicationStageLabel(stage)}</option>)}</select><ChevronDown size={14} /></label></div>
          {candidates.status === "error" && <div className="job-request-state error" role="alert"><CircleAlert size={17} /><span>{candidates.error}</span><button type="button" onClick={() => onLoadCandidates(candidates.filters)}>重试</button></div>}
          {candidates.status === "loading" && candidates.records.length > 0 && <div className="job-request-state" role="status">正在更新候选人…</div>}
          <div className="detail-candidate-table" aria-busy={candidates.status === "loading"}><div className="detail-candidate-head"><span>候选人</span><span>当前阶段</span><span>AI 匹配分</span><span>最近进展</span><span>招聘负责人</span><span /></div>{candidates.records.map((candidate) => <button type="button" key={candidate.applicationId || candidate.candidateId} onClick={() => onOpenCandidate(candidate)}><span className="candidate-identity"><span>{candidate.name.slice(-1)}</span><span><strong>{candidate.name}</strong><small>{candidate.role}</small></span></span><span><span className="stage-pill">{applicationStageLabel(candidate.stage)}</span></span><span className="score-cell">{candidate.score}</span><span>{candidate.lastActivity}</span><span>{candidate.owner}</span><ChevronRight size={16} /></button>)}{candidates.status === "loading" && candidates.records.length === 0 && <div className="detail-empty" role="status"><strong>正在加载候选人…</strong></div>}{candidates.status === "ready" && candidates.records.length === 0 && <div className="detail-empty"><Users size={24} /><strong>当前筛选下没有候选人</strong><span>可调整搜索或阶段筛选。</span></div>}</div>
          {candidates.nextCursor && <div className="job-load-more"><button className="button secondary" type="button" disabled={candidates.status === "loading"} onClick={() => onLoadCandidates(candidates.filters, { append: true, cursor: candidates.nextCursor })}>{candidates.status === "loading" ? "正在加载…" : "加载更多候选人"}</button></div>}
        </div>}
        {tab === "职位信息" && <div className="detail-tab-content job-info-grid"><section><h3>公开职位描述</h3><p>{job.jd || "未填写职位描述"}</p></section><section><h3>必须条件</h3><div className="skill-tags">{job.mustHave.length ? job.mustHave.map((item) => <span key={item}>{item}</span>) : <span>未设置</span>}</div><h3>加分项</h3><div className="skill-tags muted">{job.niceToHave.length ? job.niceToHave.map((item) => <span key={item}>{item}</span>) : <span>未设置</span>}</div></section><section><h3>招聘配置</h3><dl><div><dt>招聘人数</dt><dd>{job.headcount} 人</dd></div><div><dt>优先级</dt><dd>{job.priority || "未设置"}</dd></div><div><dt>流程模板</dt><dd>{job.process || "未设置"}</dd></div><div><dt>LLM 评估</dt><dd>{job.llmEnabled ? "已启用" : "未启用"}</dd></div></dl></section></div>}
      </section>
    </div>
  );
}

export function JobsWorkspace({ mode, setMode, selectedJob, setSelectedJob, listState, onLoadJobs, jobController, candidateController, onRefreshJobMutation, onNotify, onImport, onOpenCandidate, onManageDepartments, onManageTemplates, initialDraft, onDraftChange, onDraftClear, pageActionHost, onCreateJob }) {
  const [detailState, setDetailState] = useState({ status: "idle", job: null, candidates: null });
  const [lifecycleState, setLifecycleState] = useState({ status: "idle", error: "", conflict: false });
  const [refreshState, setRefreshState] = useState({ error: "", retrying: false, kind: "updated" });
  const [formDepartments, setFormDepartments] = useState([]);
  const [formOwners, setFormOwners] = useState({ status: "idle", records: [] });
  const [formWorkflowTemplates, setFormWorkflowTemplates] = useState({ status: "idle", records: [] });
  const [ownerDirectoryVersion, setOwnerDirectoryVersion] = useState(0);
  const detailRequestRef = useRef(null);
  const candidateRequestRef = useRef(null);
  const departmentRequestRef = useRef(null);
  const ownerRequestRef = useRef(null);
  const workflowTemplateRequestRef = useRef(null);
  const skipNextDetailLoadRef = useRef(false);
  const detailSequenceRef = useRef(0);
  const candidateSequenceRef = useRef(0);

  const loadDetail = useCallback(async (summary) => {
    detailRequestRef.current?.abort();
    candidateRequestRef.current?.abort();
    const controller = new AbortController();
    const requestId = ++detailSequenceRef.current;
    detailRequestRef.current = controller;
    setDetailState({ status: "loading", job: null, candidates: null });
    setLifecycleState({ status: "idle", error: "", conflict: false });
    setRefreshState({ error: "", retrying: false, kind: "updated" });
    try {
      const [definition, candidatePage] = await Promise.all([
        jobController.loadDefinition(summary.id, { signal: controller.signal }),
        candidateController.listCandidates({ jobId: summary.id, limit: 20 }, { signal: controller.signal }),
      ]);
      if (detailRequestRef.current !== controller || requestId !== detailSequenceRef.current) return;
      const job = { ...jobController.mergeDefinition(summary, definition, listState), ...(summary.formMode === "edit" ? { formMode: "edit" } : {}) };
      setSelectedJob(job);
      setDetailState({ status: "ready", job, candidates: { status: "ready", records: candidatePage.records, nextCursor: candidatePage.nextCursor, filters: { q: "", stage: "全部阶段" }, error: "" } });
    } catch (error) {
      if (error?.name === "AbortError" || detailRequestRef.current !== controller) return;
      setDetailState({ status: error?.status === 403 || error?.status === 404 ? "not-found" : "error", job: null, candidates: null });
    } finally {
      if (detailRequestRef.current === controller) detailRequestRef.current = null;
    }
  }, [candidateController, jobController, listState.departments, listState.owners, setSelectedJob]);

  useEffect(() => {
    if ((mode !== "detail" && !(mode === "form" && selectedJob?.formMode === "edit")) || !selectedJob?.id) return;
    if (skipNextDetailLoadRef.current) {
      skipNextDetailLoadRef.current = false;
      return;
    }
    void loadDetail(selectedJob);
  }, [loadDetail, mode, selectedJob?.id]);

  useEffect(() => {
    if (mode !== "form") return undefined;
    departmentRequestRef.current?.abort();
    const controller = new AbortController();
    departmentRequestRef.current = controller;
    setFormDepartments(listState.departments);
    if (typeof jobController.listDepartments !== "function") return () => controller.abort();
    void jobController.listDepartments({ signal: controller.signal }).then((departments) => {
      if (departmentRequestRef.current === controller) setFormDepartments(departments);
    }).catch((error) => {
      if (error?.name !== "AbortError" && departmentRequestRef.current === controller) setFormDepartments(listState.departments);
    }).finally(() => {
      if (departmentRequestRef.current === controller) departmentRequestRef.current = null;
    });
    return () => controller.abort();
  }, [jobController, mode]);

  useEffect(() => {
    if (mode !== "form") return undefined;
    ownerRequestRef.current?.abort();
    const controller = new AbortController();
    ownerRequestRef.current = controller;
    setFormOwners({ status: "loading", records: [] });
    void jobController.listHiringManagers({ signal: controller.signal }).then((owners) => {
      if (ownerRequestRef.current === controller) setFormOwners({ status: "ready", records: owners });
    }).catch((error) => {
      if (error?.name !== "AbortError" && ownerRequestRef.current === controller) setFormOwners({ status: "error", records: [] });
    }).finally(() => {
      if (ownerRequestRef.current === controller) ownerRequestRef.current = null;
    });
    return () => controller.abort();
  }, [jobController, mode, ownerDirectoryVersion]);

  useEffect(() => {
    if (mode !== "form") return undefined;
    workflowTemplateRequestRef.current?.abort();
    const controller = new AbortController();
    workflowTemplateRequestRef.current = controller;
    setFormWorkflowTemplates({ status: "loading", records: [] });
    void workflowTemplateController.list({ signal: controller.signal }).then((records) => {
      if (workflowTemplateRequestRef.current === controller) setFormWorkflowTemplates({ status: "ready", records });
    }).catch((error) => {
      if (error?.name !== "AbortError" && workflowTemplateRequestRef.current === controller) setFormWorkflowTemplates({ status: "error", records: [] });
    }).finally(() => {
      if (workflowTemplateRequestRef.current === controller) workflowTemplateRequestRef.current = null;
    });
    return () => controller.abort();
  }, [mode]);

  useEffect(() => () => {
    detailRequestRef.current?.abort();
    candidateRequestRef.current?.abort();
    departmentRequestRef.current?.abort();
    ownerRequestRef.current?.abort();
    workflowTemplateRequestRef.current?.abort();
  }, []);

  const loadCandidates = useCallback(async (filters, { append = false, cursor = null } = {}) => {
    const jobId = detailState.job?.id;
    if (!jobId) return;
    candidateRequestRef.current?.abort();
    const controller = new AbortController();
    const requestId = ++candidateSequenceRef.current;
    candidateRequestRef.current = controller;
    setDetailState((current) => ({ ...current, candidates: { ...current.candidates, status: "loading", filters, error: "" } }));
    try {
      const page = await candidateController.listCandidates({ jobId, q: filters.q, stage: filters.stage, cursor: cursor || undefined, limit: 20 }, { signal: controller.signal });
      if (candidateRequestRef.current !== controller || requestId !== candidateSequenceRef.current) return;
      setDetailState((current) => ({ ...current, candidates: { ...current.candidates, status: "ready", records: append ? mergeCandidateRecords(current.candidates.records, page.records) : page.records, nextCursor: page.nextCursor, filters, error: "" } }));
    } catch (error) {
      if (error?.name === "AbortError" || candidateRequestRef.current !== controller) return;
      setDetailState((current) => ({ ...current, candidates: { ...current.candidates, status: "error", error: "候选人加载失败，请重试。" } }));
    } finally {
      if (candidateRequestRef.current === controller) candidateRequestRef.current = null;
    }
  }, [candidateController, detailState.job?.id]);

  async function saveDefinition(values, publish) {
    const existing = selectedJob?.formMode === "edit" ? selectedJob : null;
    let result;
    try {
      result = await commitJobMutation(async () => {
        const saved = await jobController.saveDefinition(values, { job: existing, publish });
        return existing ? jobController.mergeDefinition(saved, existing, listState) : saved;
      }, onRefreshJobMutation);
    } catch (error) {
      if (error?.status !== 409 || !existing) throw error;
      const recovery = await jobController.refreshEditBaseline(existing, values, { metadata: listState });
      if (!recovery.retryable) setSelectedJob(recovery.job);
      return {
        status: "conflict",
        error: recovery.error || JOB_EDIT_CONFLICT_REFRESHED_MESSAGE,
        retryable: recovery.retryable,
      };
    }
    const complete = result.record;
    const refreshError = result.refreshError ? "已保存，但最新数据加载失败，请重试读取。" : "";
    setSelectedJob(complete);
    setRefreshState({ error: refreshError, retrying: false, kind: "saved" });
    onNotify(refreshError || getJobSaveSuccessMessage(existing, publish));
    if (existing || publish || refreshError) {
      setDetailState({ status: "ready", job: complete, candidates: detailState.candidates || { status: "ready", records: [], nextCursor: null, filters: { q: "", stage: "全部阶段" }, error: "" } });
      skipNextDetailLoadRef.current = true;
      setMode("detail");
    } else {
      setMode("list");
    }
    return { status: "saved" };
  }

  async function retryEditConflictRefresh(values) {
    const existing = selectedJob?.formMode === "edit" ? selectedJob : null;
    if (!existing) return { status: "conflict", error: JOB_EDIT_CONFLICT_REFRESHED_MESSAGE, retryable: false };
    const recovery = await jobController.refreshEditBaseline(existing, values, { metadata: listState });
    if (!recovery.retryable) setSelectedJob(recovery.job);
    return {
      status: "conflict",
      error: recovery.error || JOB_EDIT_CONFLICT_REFRESHED_MESSAGE,
      retryable: recovery.retryable,
    };
  }

  async function transition(target) {
    if (lifecycleState.status === "loading" || !detailState.job) return;
    setLifecycleState({ status: "loading", error: "", conflict: false });
    try {
      const result = await commitJobMutation(async () => {
        const summary = await jobController.transition(detailState.job, target);
        return jobController.mergeDefinition(summary, detailState.job, listState);
      }, onRefreshJobMutation);
      const next = result.record;
      const refreshError = result.refreshError ? "已更新，但最新数据加载失败，请重试读取。" : "";
      setDetailState((current) => ({ ...current, job: next }));
      setSelectedJob(next);
      setRefreshState({ error: refreshError, retrying: false, kind: "updated" });
      setLifecycleState({ status: "ready", error: "", conflict: false });
      onNotify(refreshError || `职位状态已更新为${next.status}`);
    } catch (error) {
      const conflict = error?.status === 409;
      setLifecycleState({ status: "error", conflict, error: conflict ? "职位已被其他人更新，请刷新后重试。" : "职位状态更新失败，请重试。" });
    }
  }

  async function retryMutationRefresh() {
    if (refreshState.retrying || !detailState.job) return;
    setRefreshState((current) => ({ ...current, retrying: true }));
    const result = await retryJobRefresh(detailState.job, onRefreshJobMutation);
    setDetailState((current) => ({ ...current, job: result.record }));
    setSelectedJob(result.record);
    setRefreshState((current) => ({
      ...current,
      retrying: false,
      error: result.refreshError ? (current.kind === "saved" ? "已保存，但最新数据加载失败，请重试读取。" : "已更新，但最新数据加载失败，请重试读取。") : "",
    }));
  }

  if (mode === "form" && selectedJob?.formMode === "edit" && ["idle", "loading"].includes(detailState.status)) return <div className="job-request-state" role="status">正在加载职位详情…</div>;
  if (mode === "form") return <JobForm initialJob={selectedJob?.formMode === "edit" ? selectedJob : null} initialDraft={initialDraft} departments={formDepartments} owners={formOwners.records} ownersStatus={formOwners.status} workflowTemplates={formWorkflowTemplates.records} workflowTemplatesStatus={formWorkflowTemplates.status} onBack={() => { setSelectedJob(null); setMode("list"); }} onDiscard={() => { setSelectedJob(null); setMode("list"); }} onSubmit={saveDefinition} onRetryConflictRefresh={retryEditConflictRefresh} onManageDepartments={onManageDepartments} onManageTemplates={onManageTemplates} onDraftChange={onDraftChange} onDraftClear={onDraftClear} onRetryOwners={() => setOwnerDirectoryVersion((current) => current + 1)} pageActionHost={pageActionHost} />;
  if (mode === "detail" && selectedJob) return <JobDetail state={detailState} lifecycleState={lifecycleState} refreshState={refreshState} onBack={() => { detailRequestRef.current?.abort(); candidateRequestRef.current?.abort(); setSelectedJob(null); setMode("list"); }} onEdit={() => { setSelectedJob((current) => ({ ...current, formMode: "edit" })); setMode("form"); }} onImport={onImport} onOpenCandidate={onOpenCandidate} onReload={() => loadDetail(selectedJob)} onRetryRefresh={retryMutationRefresh} onLoadCandidates={loadCandidates} onTransition={transition} />;
  return <JobList state={listState} onLoad={onLoadJobs} onOpen={(job) => { setSelectedJob(job); setMode("detail"); }} onCreate={onCreateJob} pageActionHost={pageActionHost} />;
}
