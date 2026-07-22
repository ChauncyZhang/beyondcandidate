import { lazy, Suspense, useEffect, useMemo, useRef, useState } from "react";
import "./product-theme-people.css";
import {
  ArrowLeft,
  BriefcaseBusiness,
  CalendarDays,
  ChevronDown,
  ChevronRight,
  CircleAlert,
  ClipboardCopy,
  Download,
  Eye,
  FileText,
  Filter,
  GraduationCap,
  Import,
  Mail,
  MessageSquareText,
  Phone,
  Plus,
  Search,
  ShieldCheck,
  ShieldAlert,
  Sparkles,
  UserRound,
  UserRoundCheck,
  X,
  LoaderCircle,
  RotateCcw,
} from "lucide-react";
import { mergeCandidateRecords, resolveCandidateJobPreset } from "./candidateController.js";
import { canAddCandidateToTalentPool } from "./talentController.js";
import { createCandidateGovernanceController } from "./candidateGovernance.js";
import { canManageCandidateLegalHold, canPerformAction, canReadCandidateGovernance, canRequestCandidateDeletion } from "./roleCapabilities.js";
import { PagePrimaryAction } from "./PagePrimaryAction.jsx";
import { aiRecommendationLabel, applicationStageLabel } from "./recruitingTerminology.js";

const PdfResumeViewer = lazy(() => import("./PdfResumeViewer.jsx").then((module) => ({ default: module.PdfResumeViewer })));

const baseTimeline = [
  { time: "今天 10:30", actor: "系统", action: "完成规则评分与 LLM 辅助评估" },
  { time: "今天 10:28", actor: "张小北", action: "从 BOSS 直聘导入简历" },
];

export const initialCandidateRecords = [
  { id: "CAN-001", name: "李嘉明", role: "AI 算法工程师", company: "字节", position: "AI 工程师", stage: "新简历", score: 81, ruleScore: 81, llmScore: 78, recommendation: "可沟通", source: "BOSS 直聘", owner: "张小北", city: "北京", phone: "138****2468", email: "lij***@mail.com", lastActivity: "今天 10:30", tags: ["LLM", "RAG"], skills: ["Python", "PyTorch", "RAG", "Agent"], education: "北京邮电大学 · 计算机硕士", experience: "5 年算法与大模型应用经验", summary: "负责过企业级 RAG 和 Agent 项目，具备从方案设计到上线监控的完整经验。", matched: "Python、LLM、RAG", missing: "Kubernetes", risk: "项目规模和团队职责待确认", llmReason: "语义匹配度较高，项目经历覆盖岗位核心职责。", humanConclusion: null, notes: [], version: 1, timeline: baseTimeline, applications: [{ position: "AI 工程师", state: "新简历", created: "2026-07-11", source: "BOSS 直聘" }], interviews: [] },
  { id: "CAN-002", name: "王晨", role: "算法工程师", company: "腾讯", position: "AI 工程师", stage: "待复核", score: 74, ruleScore: 74, llmScore: 72, recommendation: "人工复核", source: "猎聘", owner: "张小北", city: "深圳", phone: "186****9052", email: "wan***@mail.com", lastActivity: "今天 09:45", tags: ["机器学习"], skills: ["Python", "TensorFlow", "机器学习"], education: "华中科技大学 · 软件工程本科", experience: "4 年推荐算法经验", summary: "算法基础扎实，但大模型项目主要集中在内部验证阶段。", matched: "Python、机器学习", missing: "Agent", risk: "大模型生产经验偏少", llmReason: "基础能力符合，LLM 应用深度需要人工确认。", humanConclusion: null, notes: [], version: 2, timeline: baseTimeline, applications: [{ position: "AI 工程师", state: "待复核", created: "2026-07-10", source: "猎聘" }], interviews: [] },
  { id: "CAN-003", name: "赵宁", role: "大模型应用工程师", company: "百度", position: "AI 工程师", stage: "待沟通", score: 88, ruleScore: 88, llmScore: 84, recommendation: "优先沟通", source: "智联招聘", owner: "陈雨", city: "北京", phone: "139****3306", email: "zha***@mail.com", lastActivity: "昨天 16:30", tags: ["LLM", "Agent", "高优先级"], skills: ["Python", "LangChain", "RAG", "Agent"], education: "浙江大学 · 人工智能硕士", experience: "6 年 NLP 和大模型应用经验", summary: "项目经验与岗位高度匹配，曾负责百万级知识库问答系统。", matched: "LLM、RAG、Agent", missing: "无明显缺失", risk: "到岗时间待确认", llmReason: "核心技能和业务场景均高度匹配。", humanConclusion: "建议推进", notes: ["优先电话沟通到岗时间"], version: 1, timeline: [{ time: "昨天 16:30", actor: "陈雨", action: "添加沟通备注" }, ...baseTimeline], applications: [{ position: "AI 工程师", state: "待沟通", created: "2026-07-09", source: "智联招聘" }], interviews: [] },
  { id: "CAN-004", name: "陈浩", role: "Java 开发工程师", company: "美团", position: "Java 后端工程师", stage: "待安排", score: 79, ruleScore: 82, llmScore: 76, recommendation: "可沟通", source: "员工内推", owner: "陈雨", city: "上海", phone: "137****5811", email: "che***@mail.com", lastActivity: "昨天 14:12", tags: ["Java", "高并发"], skills: ["Java", "Spring Boot", "MySQL", "Redis"], education: "同济大学 · 计算机本科", experience: "7 年 Java 后端经验", summary: "具备高并发交易系统和微服务治理经验。", matched: "Java、Spring Boot、MySQL", missing: "Kubernetes", risk: "薪资预期待确认", llmReason: "后端经验与岗位要求匹配，云原生经历较少。", humanConclusion: "建议推进", notes: [], version: 1, timeline: baseTimeline, applications: [{ position: "Java 后端工程师", state: "待安排", created: "2026-07-08", source: "员工内推" }], interviews: [] },
  { id: "CAN-005", name: "孙悦", role: "AI 产品经理", company: "阿里", position: "产品经理", stage: "面试中", score: 83, ruleScore: 80, llmScore: 83, recommendation: "建议推进", source: "人才库激活", owner: "张小北", city: "杭州", phone: "135****7720", email: "sun***@mail.com", lastActivity: "07-10 18:05", tags: ["AI 产品", "B 端"], skills: ["需求分析", "AI 产品", "项目管理"], education: "上海交通大学 · 管理学硕士", experience: "5 年企业服务产品经验", summary: "熟悉 AI 产品从需求到商业化的完整过程。", matched: "B 端产品、AI 产品、项目管理", missing: "招聘行业", risk: "行业迁移能力待评估", llmReason: "产品能力符合，行业背景需要面试确认。", humanConclusion: "建议推进", notes: [], version: 1, timeline: baseTimeline, applications: [{ position: "产品经理", state: "面试中", created: "2026-07-03", source: "人才库激活" }], interviews: [{ round: "一面", time: "2026-07-10 14:00", interviewer: "王磊", result: "推荐", feedback: "产品方法完整，AI 理解较深入。" }] },
  { id: "CAN-006", name: "刘洋", role: "前端工程师", company: "小米", position: "前端工程师", stage: "待决策", score: 77, ruleScore: 79, llmScore: 75, recommendation: "人工复核", source: "BOSS 直聘", owner: "刘思远", city: "北京", phone: "188****4090", email: "liu***@mail.com", lastActivity: "07-10 15:20", tags: ["React"], skills: ["React", "TypeScript", "CSS"], education: "北京工业大学 · 软件工程本科", experience: "5 年前端工程经验", summary: "有复杂后台和设计系统建设经验。", matched: "React、TypeScript、CSS", missing: "数据可视化", risk: "管理经验较少", llmReason: "技术能力符合，岗位级别需要综合面试反馈。", humanConclusion: "需要补充", notes: [], version: 1, timeline: baseTimeline, applications: [{ position: "前端工程师", state: "待决策", created: "2026-07-01", source: "BOSS 直聘" }], interviews: [{ round: "技术面", time: "2026-07-09 10:00", interviewer: "赵强", result: "推荐", feedback: "工程能力扎实。" }] },
];

const workflowActions = {
  review_approved: { label: "简历评审通过", title: "确认简历评审通过", description: "提交后，候选人将自动进入待安排面试，HR 会在工作台收到待办。", target: "待安排", capability: "评审候选人" },
  review_rejected: { label: "简历评审不通过", title: "确认简历评审不通过", description: "请记录不通过原因，提交后将进入已淘汰。", target: "已淘汰", capability: "评审候选人", reasonRequired: true },
  hiring_approved: { label: "决定进入录用", title: "确认进入录用流程", description: "提交后，候选人将进入待录用确认，HR 会在工作台收到待办。", target: "已通过", capability: "确认录用决策" },
  hiring_rejected: { label: "决定不录用", title: "确认本次不录用", description: "请记录决策原因，提交后将进入已淘汰。", target: "已淘汰", capability: "确认录用决策", reasonRequired: true },
  offer_accepted: { label: "确认候选人接受录用", title: "确认候选人接受录用", description: "请仅在候选人已接受录用后确认。提交后流程结束。", target: "已录用", capability: "确认录用结果" },
  offer_declined: { label: "候选人拒绝录用", title: "确认候选人拒绝录用", description: "请记录候选人拒绝录用的原因，提交后流程结束。", target: "已撤回", capability: "确认录用结果", reasonRequired: true },
};

const stageWorkflowActions = {
  待复核: ["review_approved", "review_rejected"],
  待决策: ["hiring_approved", "hiring_rejected"],
  已通过: ["offer_accepted", "offer_declined"],
};

const llmDimensionLabels = {
  core_capability: "核心能力匹配",
  experience_depth: "经验深度",
  role_seniority: "职级匹配",
  transferability: "经验可迁移性",
  explicit_constraints: "明确约束",
};

export function candidateWorkflowActions(stage, role) {
  return (stageWorkflowActions[stage] || [])
    .map((id) => ({ id, ...workflowActions[id] }))
    .filter((item) => canPerformAction(role, item.capability));
}

export function candidateNextStep(stage) {
  return ({
    新简历: "AI 筛选完成后自动决定流转",
    待复核: "等待用人经理评审",
    "AI 初筛暂缓": "保留在人才库，等待后续岗位匹配",
    待沟通: "HR 联系候选人并确认面试意向",
    待安排: "HR 安排下一场面试",
    面试中: "完成当前面试与面试官反馈",
    待决策: "等待用人经理作出录用决策",
    已通过: "HR 确认候选人是否接受录用",
    已录用: "流程已完成",
    已淘汰: "流程已结束",
    已撤回: "流程已结束",
  })[stage] || "等待流程更新";
}

export function canScheduleCandidateInterview(stage, role, scheduleAvailable) {
  return ["待安排", "待决策"].includes(stage) && canPerformAction(role, "安排面试") && Boolean(scheduleAvailable);
}

export function candidateStageFilterOptions() {
  return ["新简历", "待复核", "AI 初筛暂缓", "待沟通", "待安排", "面试中", "待决策", "已通过", "已录用", "已淘汰", "已撤回"];
}

export function candidateDetailTabs(serverBacked) {
  return serverBacked
    ? ["档案与简历", "职位申请", "筛选证据", "面试与反馈", "时间线"]
    : ["档案与简历", "职位申请", "筛选证据", "面试与反馈", "时间线"];
}

export function candidateMutationError(error) {
  return error?.status === 409
    ? "记录已被其他成员更新。你的修改未保存，请刷新后重新确认。"
    : "操作未完成，请稍后重试。";
}

export function resumeDisplayName(resume) {
  if (!resume) return "暂无可用简历";
  return resume.original_filename || resume.filename || (resume.version_number ? `简历版本 ${resume.version_number}` : "候选人简历");
}

function StageTag({ stage }) {
  const terminal = ["已录用", "已淘汰", "已撤回"].includes(stage);
  return <span className={`candidate-stage ${terminal ? "terminal" : ""}`}>{applicationStageLabel(stage)}</span>;
}

function CandidateList({ controller, onOpen, filters = {}, onFiltersChange = () => {}, onImport, pageActionHost }) {
  const [query, setQuery] = useState(filters.q || "");
  const [debouncedQuery, setDebouncedQuery] = useState("");
  const [position, setPosition] = useState(filters.jobId || "全部职位");
  const [stage, setStage] = useState(filters.stage || "全部阶段");
  const [owner, setOwner] = useState(filters.ownerId || "全部负责人");
  const [minScore, setMinScore] = useState(filters.minScore || "不限分数");
  const [records, setRecords] = useState([]);
  const [ownerOptions, setOwnerOptions] = useState([]);
  const [jobs, setJobs] = useState([]);
  const [jobsReady, setJobsReady] = useState(false);
  const [status, setStatus] = useState("loading");
  const [error, setError] = useState("");
  const [nextCursor, setNextCursor] = useState(null);
  const [loadingMore, setLoadingMore] = useState(false);
  const [loadMoreError, setLoadMoreError] = useState("");
  const [retryVersion, setRetryVersion] = useState(0);
  const requestRef = useRef(null);

  useEffect(() => {
    const timeout = window.setTimeout(() => setDebouncedQuery(query.trim()), 300);
    return () => window.clearTimeout(timeout);
  }, [query]);

  useEffect(() => {
    const abortController = new AbortController();
    void controller.listJobs({ signal: abortController.signal }).then(setJobs).catch((loadError) => {
      if (loadError?.name !== "AbortError") setJobs([]);
    }).finally(() => {
      if (!abortController.signal.aborted) setJobsReady(true);
    });
    return () => abortController.abort();
  }, [controller]);

  useEffect(() => {
    setQuery(filters.q || "");
    setStage(filters.stage || "全部阶段");
    setOwner(filters.ownerId || "全部负责人");
    setMinScore(filters.minScore || "不限分数");
    if (jobsReady || !filters.jobId) setPosition(resolveCandidateJobPreset(jobs, filters));
  }, [filters, jobs, jobsReady]);

  const needsPositionPreset = Boolean(filters.jobId || (filters.position && filters.position !== "全部职位"));
  const presetJobId = jobsReady ? resolveCandidateJobPreset(jobs, filters) : null;
  const presetRequestKey = needsPositionPreset ? `${jobsReady}:${presetJobId || ""}` : "";

  useEffect(() => {
    if (needsPositionPreset && !jobsReady) return undefined;
    if (needsPositionPreset && position !== presetJobId) return undefined;
    const abortController = new AbortController();
    requestRef.current?.abort();
    requestRef.current = abortController;
    setStatus("loading");
    setError("");
    setLoadMoreError("");
    setNextCursor(null);
    setRecords([]);
    void controller.listCandidates({
      q: debouncedQuery, jobId: position, stage, ownerId: owner, minScore, limit: 50,
    }, { signal: abortController.signal }).then((result) => {
      if (requestRef.current !== abortController) return;
      setRecords(result.records);
      setOwnerOptions(result.ownerOptions);
      setNextCursor(result.nextCursor);
      setStatus("ready");
    }).catch((loadError) => {
      if (loadError?.name === "AbortError" || requestRef.current !== abortController) return;
      setStatus("error");
      setError("候选人列表加载失败，请稍后重试。");
    }).finally(() => {
      if (requestRef.current === abortController) requestRef.current = null;
    });
    return () => abortController.abort();
  }, [controller, debouncedQuery, minScore, needsPositionPreset, owner, position, presetJobId, presetRequestKey, retryVersion, stage]);

  useEffect(() => () => requestRef.current?.abort(), []);

  const hasFilters = Boolean(debouncedQuery || position !== "全部职位" || stage !== "全部阶段" || owner !== "全部负责人" || minScore !== "不限分数");

  async function loadMore() {
    if (!nextCursor || loadingMore) return;
    const abortController = new AbortController();
    requestRef.current = abortController;
    setLoadingMore(true);
    setLoadMoreError("");
    try {
      const result = await controller.listCandidates({
        q: debouncedQuery, jobId: position, stage, ownerId: owner, minScore, cursor: nextCursor, limit: 50,
      }, { signal: abortController.signal });
      if (requestRef.current !== abortController) return;
      setRecords((current) => mergeCandidateRecords(current, result.records));
      setOwnerOptions(result.ownerOptions);
      setNextCursor(result.nextCursor);
    } catch (loadError) {
      if (loadError?.name !== "AbortError" && requestRef.current === abortController) setLoadMoreError("加载更多候选人失败，请重试。");
    } finally {
      if (requestRef.current === abortController) requestRef.current = null;
      setLoadingMore(false);
    }
  }

  return <div className="candidate-page candidate-list-page">
    <PagePrimaryAction host={pageActionHost}>{onImport && <button className="button primary" type="button" onClick={onImport}><Import size={17} />导入简历</button>}</PagePrimaryAction>
    <div className="candidate-page-heading"><div><h2>全部候选人</h2><p>跨职位搜索和查看候选人。</p></div><span>已加载 {records.length} 人</span></div>
    <section className="candidate-list-panel">
      <div className="candidate-filters">
        <label className="candidate-search"><Search size={17} /><input aria-label="搜索候选人" value={query} onChange={(event) => { const q = event.target.value; setQuery(q); onFiltersChange({ ...filters, q }); }} placeholder="搜索姓名、当前职称、邮箱或手机号" /></label>
        <label><select aria-label="职位筛选" value={position} onChange={(event) => { const jobId = event.target.value; setPosition(jobId); onFiltersChange({ ...filters, jobId }); }}><option>全部职位</option>{jobs.map((job) => <option key={job.id} value={job.id}>{job.title}</option>)}</select><ChevronDown size={14} /></label>
        <label><select aria-label="阶段筛选" value={stage} onChange={(event) => { const value = event.target.value; setStage(value); onFiltersChange({ ...filters, stage: value }); }}><option>全部阶段</option>{candidateStageFilterOptions().map((item) => <option key={item} value={item}>{applicationStageLabel(item)}</option>)}</select><ChevronDown size={14} /></label>
        <label><select aria-label="负责人筛选" value={owner} onChange={(event) => { const ownerId = event.target.value; setOwner(ownerId); onFiltersChange({ ...filters, ownerId }); }}><option>全部负责人</option>{ownerOptions.map((item) => <option key={item.id} value={item.id}>{item.name}</option>)}</select><ChevronDown size={14} /></label>
        <label><select aria-label="分数筛选" value={minScore} onChange={(event) => { const value = event.target.value; setMinScore(value); onFiltersChange({ ...filters, minScore: value }); }}><option>不限分数</option><option value="80">80 分以上</option><option value="70">70 分以上</option></select><ChevronDown size={14} /></label>
        <button className="button secondary compact" type="button" onClick={() => onFiltersChange({ q: "", jobId: "全部职位", stage: "全部阶段", ownerId: "全部负责人", minScore: "不限分数" })}><X size={15} />清空</button>
      </div>
      <div className="candidate-table">
        <div className="candidate-table-head"><span>候选人</span><span>当前申请</span><span>阶段</span><span>AI 匹配分</span><span>来源</span><span>招聘负责人</span><span>最近进展</span><span>下一步</span></div>
        {status === "loading" && <div className="candidate-list-state" role="status"><LoaderCircle className="spin" size={24} /><strong>正在加载候选人</strong><span>请稍候，正在读取服务端候选人列表。</span></div>}
        {status === "error" && <div className="candidate-list-state error" role="alert"><CircleAlert size={24} /><strong>候选人列表加载失败</strong><span>{error}</span><button className="button primary" type="button" onClick={() => setRetryVersion((value) => value + 1)}><RotateCcw size={15} />重试</button></div>}
        {status === "ready" && records.map((candidate) => <div className="candidate-table-row" role="button" tabIndex={0} key={candidate.applicationId || candidate.candidateId} onClick={() => onOpen(candidate)} onKeyDown={(event) => { if (event.key === "Enter" || event.key === " ") { event.preventDefault(); onOpen(candidate); } }}><span className="candidate-name-cell"><span>{candidate.name.slice(-1)}</span><span><strong>{candidate.name}</strong><small>{candidate.role}{candidate.company ? ` · ${candidate.company}` : ""}</small></span></span><span><strong>{candidate.position}</strong><small>{candidate.city}</small></span><span className="candidate-stage-cell"><StageTag stage={candidate.stage} /></span><span className="candidate-score" aria-label={`AI 匹配分 ${candidate.score}`}>{candidate.score}</span><span>{candidate.source}</span><span>{candidate.owner}</span><span><strong>{candidate.lastActivity}</strong><small>{aiRecommendationLabel(candidate.recommendation)}</small></span><span className="next-cell">查看<ChevronRight size={16} /></span></div>)}
        {status === "ready" && records.length === 0 && <div className="candidate-empty"><Filter size={24} /><strong>{hasFilters ? "没有符合条件的候选人" : "暂无候选人"}</strong><span>{hasFilters ? "调整或清空筛选条件后重试。" : "当前还没有可查看的候选人。"}</span></div>}
      </div>
      {status === "ready" && records.length > 0 && <div className="candidate-pagination">{loadMoreError && <span role="alert">{loadMoreError}</span>}{nextCursor ? <button className="button secondary" type="button" disabled={loadingMore} onClick={() => void loadMore()}>{loadingMore ? <><LoaderCircle className="spin" size={15} />加载中</> : loadMoreError ? <><RotateCcw size={15} />重试加载</> : "加载更多"}</button> : <span>已加载全部</span>}</div>}
    </section>
  </div>;
}

function WorkflowActionDialog({ candidate, action, onClose, onCommit, onConflictRefresh, serverBacked = false, submitting = false, actionError = "", conflict = false }) {
  const [reason, setReason] = useState("");
  const [error, setError] = useState("");
  const [fixtureConflict, setFixtureConflict] = useState(false);

  async function submit(force = false) {
    if (action.reasonRequired && !reason.trim()) { setError("请填写本次操作原因"); return; }
    if (!serverBacked && candidate.version === 2 && !force) { setFixtureConflict(true); return; }
    await onCommit(action, reason);
  }

  return <div className="candidate-dialog-backdrop" role="presentation" onMouseDown={onClose}><section className="candidate-dialog" role="dialog" aria-modal="true" aria-label={action.title} onMouseDown={(event) => event.stopPropagation()}>
    <header><div><h3>{action.title}</h3><p>{candidate.name} · {candidate.position}</p></div><button className="icon-button" type="button" aria-label="关闭" onClick={onClose}><X size={19} /></button></header>
    {(conflict || fixtureConflict) ? <div className="conflict-state" role="alert"><CircleAlert size={23} /><h4>候选人状态已被其他成员更新</h4><p>{serverBacked ? "你的修改没有覆盖服务端记录。请加载最新详情并重新确认。" : "服务端最新状态为“待确认候选人意向”，招聘负责人为张小北。你的修改尚未覆盖该更新。"}</p><div><button className="button secondary" type="button" onClick={() => onConflictRefresh(serverBacked ? undefined : "待沟通")}>{serverBacked ? "刷新最新详情" : "使用最新状态"}</button>{!serverBacked && <button className="button primary" type="button" onClick={() => submit(true)}>基于最新状态重新应用</button>}</div></div> : <>
      <div className="candidate-dialog-body"><div className="transition-current"><span>当前状态</span><StageTag stage={candidate.stage} /></div><p className="workflow-action-description">{action.description}</p><label>操作原因{action.reasonRequired && <span className="required-label">必填</span>}<textarea rows="4" value={reason} disabled={submitting} onChange={(event) => { setReason(event.target.value); setError(""); }} placeholder={action.reasonRequired ? "请填写具体原因" : "补充说明（选填）"} /></label><div className="transition-impact"><ShieldCheck size={16} /><span>状态将由本次业务动作自动更新，并写入候选人时间线。</span></div>{(error || actionError) && <p className="field-error" role="alert"><CircleAlert size={14} />{error || actionError}</p>}</div>
      <footer><button className="button secondary" type="button" disabled={submitting} onClick={onClose}>取消</button><button className="button primary" type="button" disabled={submitting} onClick={() => void submit(false)}>{submitting ? "正在提交" : action.label}</button></footer>
    </>}
  </section></div>;
}

function ResumePreview({ candidate, file, status, error, downloading, onClose, onRetry, onDownload }) {
  const dialogRef = useRef(null);
  const restoreRef = useRef(typeof document === "undefined" ? null : document.activeElement);
  useEffect(() => {
    const dialog = dialogRef.current;
    if (!dialog || typeof document === "undefined") return undefined;
    dialog.querySelector("[data-resume-initial-focus]")?.focus();
    return () => restoreRef.current?.isConnected !== false && restoreRef.current?.focus?.();
  }, []);
  function handleKeyDown(event) {
    if (event.key === "Escape") { event.preventDefault(); onClose(); return; }
    if (event.key !== "Tab") return;
    const controls = [...dialogRef.current.querySelectorAll("button")].filter((item) => !item.disabled);
    const first = controls[0]; const last = controls[controls.length - 1];
    if (event.shiftKey && document.activeElement === first) { event.preventDefault(); last?.focus(); }
    else if (!event.shiftKey && document.activeElement === last) { event.preventDefault(); first?.focus(); }
  }
  return <div className="resume-preview-backdrop" role="presentation" onMouseDown={onClose}>
    <section ref={dialogRef} className="resume-preview-modal" role="dialog" aria-modal="true" aria-labelledby="candidate-resume-preview-title" onMouseDown={(event) => event.stopPropagation()} onKeyDown={handleKeyDown}>
      <header><div><FileText size={21} /><div><h2 id="candidate-resume-preview-title">简历预览</h2><p>{resumeDisplayName(candidate.resume)}</p></div></div><button className="icon-button" type="button" data-resume-initial-focus aria-label="关闭简历预览" onClick={onClose}><X size={19} /></button></header>
      <div className="resume-preview-reader"><Suspense fallback={<div className="pdf-viewer-state" role="status">正在加载 PDF 阅读器</div>}><PdfResumeViewer file={file} status={status} error={error} onRetry={onRetry} onDownload={onDownload} downloading={downloading} /></Suspense></div>
      <footer><button className="button secondary" type="button" onClick={onClose}>关闭</button></footer>
    </section>
  </div>;
}

const governanceCountLabels = [
  ["contacts", "联系方式"], ["resumes", "简历记录"], ["applications", "职位申请"], ["screeningRecords", "筛选记录"], ["interviews", "面试"], ["feedbackRecords", "反馈记录"], ["talentMemberships", "人才库关系"], ["resumeObjects", "简历文件"], ["temporaryExports", "临时导出"],
];
const governanceDeletionStatusLabels = {
  requested: "待系统管理员审批", approved: "已批准，等待执行", executing: "正在删除", completed: "已完成", failed: "执行失败",
};
const governanceDeletionGuidance = {
  requested: "候选人数据会保留到系统管理员审批并完成删除。审批入口：设置 → 审计与数据治理 → 删除请求审批。",
  approved: "系统管理员已批准，删除任务正在等待后台执行；执行完成前候选人仍可查看。",
  executing: "后台正在删除候选人及其关联数据，完成后该候选人将不可访问。",
  failed: "删除执行失败，需要系统管理员在删除请求审批中重新核对并批准。",
};

function GovernanceDialog({ mode, status, busy, reason, onReasonChange, onClose, onConfirm }) {
  const dialogRef = useRef(null);
  const restoreRef = useRef(typeof document === "undefined" ? null : document.activeElement);
  const busyRef = useRef(busy);
  busyRef.current = busy;
  useEffect(() => {
    const dialog = dialogRef.current;
    if (!dialog || typeof document === "undefined") return undefined;
    dialog.querySelector("[data-dialog-initial-focus]")?.focus();
    return () => restoreRef.current?.isConnected !== false && restoreRef.current?.focus?.();
  }, []);
  function handleKeyDown(event) {
    if (event.key === "Escape") { event.preventDefault(); if (!busyRef.current) onClose(); return; }
    if (event.key !== "Tab") return;
    const controls = [...dialogRef.current.querySelectorAll("button, textarea")].filter((item) => !item.disabled);
    const first = controls[0]; const last = controls[controls.length - 1];
    if (event.shiftKey && (document.activeElement === first || !dialogRef.current.contains(document.activeElement))) { event.preventDefault(); last?.focus(); }
    else if (!event.shiftKey && (document.activeElement === last || !dialogRef.current.contains(document.activeElement))) { event.preventDefault(); first?.focus(); }
  }
  const deletion = mode === "delete";
  const title = deletion ? "确认提交候选人删除请求" : mode === "place" ? "设置法律保留" : "解除法律保留";
  const approvedWarning = mode === "place" && status?.deletionStatus === "approved";
  const validReason = deletion || (reason.trim().length >= 1 && reason.trim().length <= 1000);
  return <div className="candidate-dialog-backdrop governance-dialog-backdrop"><section ref={dialogRef} className="candidate-dialog governance-dialog" role="dialog" aria-modal="true" aria-label={title} onKeyDown={handleKeyDown}>
    <header><div><h2>{title}</h2><p>{deletion ? "此操作会提交审批，不会立即删除候选人数据。" : "原因仅在服务端授权范围内显示。"}</p></div><button className="icon-button" type="button" aria-label="关闭" disabled={busy} onClick={onClose}><X size={19} /></button></header>
    <div className="governance-dialog-body">{deletion ? <div className="governance-danger"><ShieldAlert size={21} /><span>批准后将按影响清单进入删除队列，并保留备份恢复窗口。</span></div> : <label>{mode === "place" ? "法律保留原因" : "解除原因"}<textarea data-dialog-initial-focus rows="5" maxLength="1000" disabled={busy} value={reason} onChange={(event) => onReasonChange(event.target.value)} /><small>{reason.trim().length}/1000</small></label>}{approvedWarning && <div className="governance-danger" role="alert"><ShieldAlert size={21} /><span>删除已获批准；设置法律保留可能终止已排队的删除。</span></div>}</div>
    <footer><button className="button secondary" type="button" data-dialog-initial-focus={deletion ? "" : undefined} disabled={busy} onClick={onClose}>取消</button><button className={deletion ? "button danger" : "button primary"} type="button" disabled={busy || !validReason} onClick={onConfirm}>{busy ? "提交中…" : deletion ? "提交删除审批" : mode === "place" ? "设置法律保留" : "解除法律保留"}</button></footer>
  </section></div>;
}

function CandidateGovernance({ candidate, role, onNotify }) {
  const controller = useMemo(() => createCandidateGovernanceController(), [candidate.id, role]);
  const [viewState, setViewState] = useState(() => controller.getState());
  const [dialog, setDialog] = useState("");
  const [reason, setReason] = useState("");
  const canRead = canReadCandidateGovernance(role);
  const canRequest = canRequestCandidateDeletion(role);
  const canHold = canManageCandidateLegalHold(role);
  useEffect(() => {
    const unsubscribe = controller.subscribe(setViewState);
    if (candidate.serverBacked && canRead) void controller.load(candidate.id, role);
    return () => { unsubscribe(); controller.dispose(); };
  }, [canRead, candidate.id, candidate.serverBacked, controller, role]);
  if (!candidate.serverBacked || !canRead) return null;
  const { loadStatus, status, deletionRequest, mutation, error, message } = viewState;
  const legalHoldId = status?.legalHoldId;
  const legalHoldVersion = status?.legalHoldVersion;
  const releaseDisabled = !legalHoldId || !legalHoldVersion;
  const duplicateOpen = ["requested", "approved", "executing", "failed"].includes(status?.deletionStatus);
  async function commit() {
    const completed = dialog === "delete" ? await controller.requestDeletion() : dialog === "place" ? await controller.placeLegalHold(reason) : await controller.releaseLegalHold(reason);
    if (completed) { onNotify(dialog === "delete" ? "删除请求已提交审批" : dialog === "place" ? "法律保留已生效" : "法律保留已解除"); setDialog(""); setReason(""); }
  }
  return <section className="candidate-governance" aria-live="polite"><h3>数据治理</h3>
    {loadStatus === "loading" && <div className="governance-compact-state" role="status"><LoaderCircle className="spin" size={17} />正在读取治理状态…</div>}
    {loadStatus === "error" && <div className="governance-compact-state error" role="alert"><span>{error}</span><button type="button" onClick={() => controller.refresh()}>重试</button></div>}
    {status && <><dl><div><dt>删除状态</dt><dd>{status.deletionStatus ? governanceDeletionStatusLabels[status.deletionStatus] : "无删除请求"}</dd></div><div><dt>法律保留</dt><dd>{status.legalHoldActive ? "已生效" : "未设置"}</dd></div>{status.legalHoldReason && <div><dt>保留原因</dt><dd>{status.legalHoldReason}</dd></div>}</dl>
      {deletionRequest && <details className="governance-impact"><summary>查看删除影响（9 类）</summary><div>{governanceCountLabels.map(([key, label]) => <span key={key}>{label}<strong>{deletionRequest.impact.counts[key]}</strong></span>)}</div><small>备份窗口至 {new Date(deletionRequest.impact.backupWindowEndsAt).toLocaleString("zh-CN", { hour12: false })}</small></details>}
      {error && loadStatus !== "error" && <p className="governance-inline-error" role="alert">{error}</p>}{message && <p className="governance-inline-message" role="status">{message}</p>}
      {governanceDeletionGuidance[status.deletionStatus] && <p className="governance-process-note">{governanceDeletionGuidance[status.deletionStatus]}</p>}
      <div className="governance-actions">{canRequest && !duplicateOpen && <button className="button danger full" type="button" disabled={Boolean(mutation)} onClick={() => setDialog("delete")}>请求删除</button>}{canHold && !status.legalHoldActive && <button className="button secondary full" type="button" disabled={Boolean(mutation)} onClick={() => { setReason(""); setDialog("place"); }}>设置法律保留</button>}{canHold && status.legalHoldActive && <button className="button secondary full" type="button" disabled={releaseDisabled || Boolean(mutation)} title={releaseDisabled ? "缺少法律保留 ID 或版本，请刷新" : undefined} onClick={() => { setReason(""); setDialog("release"); }}>解除法律保留</button>}</div>
    </>}
    {dialog && <GovernanceDialog mode={dialog} status={status} busy={Boolean(mutation)} reason={reason} onReasonChange={setReason} onClose={() => setDialog("")} onConfirm={() => void commit()} />}
  </section>;
}

function CandidateDetail({ candidate, role, onBack, backLabel, onUpdate, onNotify, onScheduleInterview, onOpenInterviewFeedback, onAddToTalentPool, actorName, controller, onRefresh, activeTab = "档案与简历", onTabChange = () => {} }) {
  const tab = candidateDetailTabs(candidate.serverBacked).includes(activeTab) ? activeTab : "档案与简历";
  const [selectedWorkflowAction, setSelectedWorkflowAction] = useState(null);
  const [note, setNote] = useState("");
  const [tagInput, setTagInput] = useState("");
  const [pendingAction, setPendingAction] = useState("");
  const [actionError, setActionError] = useState("");
  const [conflict, setConflict] = useState(false);
  const [previewState, setPreviewState] = useState(null);
  const previewRequestRef = useRef(null);

  useEffect(() => {
    const url = previewState?.file?.url;
    return () => { if (url) URL.revokeObjectURL(url); };
  }, [previewState?.file?.url]);

  useEffect(() => () => previewRequestRef.current?.abort(), [candidate.id]);

  function update(patch) { onUpdate({ ...candidate, ...patch }); }

  async function runServerAction(type, action, successMessage) {
    setPendingAction(type); setActionError(""); setConflict(false);
    try {
      await action();
      await onRefresh();
      onNotify(successMessage);
      return true;
    } catch (error) {
      setConflict(error?.status === 409);
      setActionError(candidateMutationError(error));
      return false;
    } finally {
      setPendingAction("");
    }
  }

  async function addNote() {
    if (!note.trim()) return;
    if (candidate.serverBacked) {
      const saved = await runServerAction("note", () => controller.addNote(candidate.id, candidate.application?.id, note), "备注已保存");
      if (saved) setNote("");
      return;
    }
    update({ notes: [...candidate.notes, note.trim()], timeline: [{ time: "刚刚", actor: actorName, action: `添加备注：${note.trim()}` }, ...candidate.timeline], lastActivity: "刚刚" });
    setNote(""); onNotify("备注已保存");
  }

  function addTag() {
    const value = tagInput.trim(); if (!value || candidate.tags.includes(value)) return;
    update({ tags: [...candidate.tags, value] }); setTagInput(""); onNotify("标签已添加");
  }

  async function commitWorkflowAction(action, reason) {
    if (candidate.serverBacked) {
      const saved = await runServerAction("workflow", () => controller.workflowAction(candidate.application, action.id, reason), `${action.label}已提交`);
      if (saved) setSelectedWorkflowAction(null);
      return;
    }
    update({ stage: action.target, version: candidate.version + 1, lastActivity: "刚刚", applications: candidate.applications.map((item, index) => index === 0 ? { ...item, state: action.target } : item), timeline: [{ time: "刚刚", actor: actorName, action: `${action.label}${reason ? `；原因：${reason}` : ""}` }, ...candidate.timeline] });
    setSelectedWorkflowAction(null); onNotify(`${action.label}已提交`);
  }

  async function loadPreview() {
    if (!candidate.resume?.id) return;
    previewRequestRef.current?.abort();
    const abortController = new AbortController();
    previewRequestRef.current = abortController;
    setPreviewState({ status: "loading", error: "", file: null });
    try {
      const result = await controller.getResumeFile(candidate.resume.id, { signal: abortController.signal });
      if (abortController.signal.aborted) return;
      setPreviewState({ status: "ready", error: "", file: { ...result, url: URL.createObjectURL(result.blob) } });
    } catch (error) {
      if (error?.name !== "AbortError") setPreviewState({ status: "error", error: "原始简历加载失败，请重试。", file: null });
    } finally {
      if (previewRequestRef.current === abortController) previewRequestRef.current = null;
    }
  }

  function closePreview() {
    previewRequestRef.current?.abort();
    previewRequestRef.current = null;
    setPreviewState(null);
  }

  async function downloadResume() {
    if (!candidate.serverBacked) { onNotify("简历下载已记录到审计日志"); return; }
    if (!candidate.resume?.id || pendingAction) return;
    setPendingAction("download"); setActionError("");
    try {
      const result = await controller.downloadResume(candidate.resume.id);
      const url = URL.createObjectURL(result.blob);
      const link = document.createElement("a");
      link.href = url; link.download = result.filename || resumeDisplayName(candidate.resume); link.hidden = true;
      document.body.appendChild(link); link.click(); link.remove(); URL.revokeObjectURL(url);
      onNotify("简历下载已开始");
    } catch {
      setActionError("简历下载失败，请稍后重试。");
    } finally {
      setPendingAction("");
    }
  }

  const availableWorkflowActions = candidate.application || !candidate.serverBacked ? candidateWorkflowActions(candidate.stage, role) : [];
  const nextStep = candidateNextStep(candidate.stage);
  const canScheduleCurrent = canScheduleCandidateInterview(candidate.stage, role, onScheduleInterview);
  const tabs = candidateDetailTabs(candidate.serverBacked);
  const notes = candidate.notes || [];
  const profileLine = [candidate.role, candidate.company, candidate.city].filter(Boolean).join(" · ");
  return <div className="candidate-page candidate-detail-page">
    <button className="back-link" type="button" onClick={onBack}><ArrowLeft size={17} />{backLabel || "返回候选人列表"}</button>
    <section className="candidate-detail-hero"><div className="candidate-profile"><span>{candidate.name.slice(-1)}</span><div><div><h2>{candidate.name}</h2><StageTag stage={candidate.stage} /></div><p>{profileLine}</p><div className="masked-contacts"><span><Phone size={13} />{candidate.phone}</span><span><Mail size={13} />{candidate.email}</span></div></div></div><div className="candidate-detail-actions">{!candidate.serverBacked && <button className="button secondary" type="button" onClick={() => onNotify("联系方式已复制，操作已记录") }><ClipboardCopy size={16} />复制联系信息</button>}<button className="button secondary" type="button" disabled={candidate.serverBacked && (!candidate.resume?.id || pendingAction === "download")} onClick={() => void downloadResume()}><Download size={16} />{pendingAction === "download" ? "下载中" : "下载简历"}</button>{canAddCandidateToTalentPool(candidate) && onAddToTalentPool && <button className="button secondary" type="button" onClick={() => onAddToTalentPool([candidate.id])}><BriefcaseBusiness size={16} />加入人才库</button>}{canScheduleCurrent && <button className="button primary" type="button" onClick={() => onScheduleInterview(candidate)}><CalendarDays size={16} />安排面试</button>}{availableWorkflowActions.map((action) => <button className={`button ${action.reasonRequired ? "secondary" : "primary"}`} type="button" key={action.id} onClick={() => { setActionError(""); setConflict(false); setSelectedWorkflowAction(action); }}><UserRoundCheck size={16} />{action.label}</button>)}</div></section>
    {actionError && !selectedWorkflowAction && <div className="candidate-action-error" role="alert"><CircleAlert size={16} /><span>{actionError}</span>{conflict && <button type="button" onClick={() => void onRefresh()}>刷新最新详情</button>}</div>}
    <div className="candidate-detail-layout"><main className="candidate-detail-main"><section className="candidate-detail-panel"><div className="candidate-detail-tabs">{tabs.map((item) => <button type="button" key={item} className={tab === item ? "active" : ""} onClick={() => onTabChange(item)}>{item}</button>)}</div>
      {tab === "档案与简历" && <div className="candidate-tab-content profile-tab">{candidate.serverBacked && candidate.profileStatus !== "ready" && <div className="candidate-profile-notice" role="status"><CircleAlert size={17} /><span><strong>自动结构化信息不完整</strong><small>部分内容未能可靠识别，请以右侧原始简历为准。</small></span></div>}<section><h3>候选人摘要{candidate.summaryOrigin === "generated" && <span className="candidate-generated-label">AI 基于简历生成</span>}</h3><p>{candidate.summary}</p></section><section><h3>技能</h3><div className="candidate-skill-tags">{candidate.skills.length ? candidate.skills.map((item) => <span key={item}>{item}</span>) : <span>暂无结构化技能</span>}</div></section><div className="profile-facts"><div><BriefcaseBusiness size={18} /><span><strong>工作经验</strong><small>{candidate.experience}</small></span></div><div><GraduationCap size={18} /><span><strong>教育经历</strong><small>{candidate.education}</small></span></div><div className="resume-detail-row"><FileText size={18} /><span><strong>当前简历</strong><small>{candidate.serverBacked ? resumeDisplayName(candidate.resume) : `${candidate.name}_简历.pdf · 解析质量良好`}</small>{candidate.serverBacked && candidate.resume?.id && <span className="resume-inline-actions"><button type="button" onClick={() => void loadPreview()}><Eye size={14} />预览</button><button type="button" onClick={() => void downloadResume()}><Download size={14} />下载</button></span>}</span></div></div></div>}
      {tab === "职位申请" && <div className="candidate-tab-content"><div className="applications-table"><div><span>职位</span><span>状态</span><span>{candidate.serverBacked ? "最近更新" : "申请日期"}</span><span>来源</span></div>{candidate.applications.map((item) => <div key={`${item.position}-${item.created}`}><strong>{item.position}</strong><StageTag stage={item.state} /><span>{item.created}</span><span>{item.source}</span></div>)}</div></div>}
      {tab === "筛选证据" && <div className="candidate-tab-content evidence-grid"><section className="llm-evidence" aria-labelledby="candidate-llm-evidence-title"><header><Sparkles size={18} /><div><h3 id="candidate-llm-evidence-title">AI 筛选结果</h3><span>当前持久化评估</span></div><strong aria-label={candidate.score == null ? "AI评分不可用" : `AI 匹配分 ${candidate.score}`}>{candidate.score ?? "—"}</strong></header>{candidate.recommendation === "AI评分不可用" ? <div className="candidate-muted" role="status">AI 评分失败，已保护性转交用人经理评审</div> : <><p><strong>流转结论：</strong>{aiRecommendationLabel(candidate.recommendation || "—")}</p><p>{candidate.llmSummary || "暂无评估摘要。"}</p><div className="llm-dimensions">{(candidate.dimensions || []).map((dimension) => <section key={dimension.key || dimension.label}><header><h4>{dimension.label || llmDimensionLabels[dimension.key]}</h4><strong>{dimension.score ?? "—"}</strong></header><div><strong>证据</strong>{dimension.evidence?.length ? <ul>{dimension.evidence.map((item) => <li key={item}>{item}</li>)}</ul> : <p>暂无证据</p>}</div><div><strong>缺口</strong>{dimension.gaps?.length ? <ul>{dimension.gaps.map((item) => <li key={item}>{item}</li>)}</ul> : <p>暂无缺口</p>}</div></section>)}</div><div className="llm-evidence-groups"><section><h4>优势</h4>{candidate.strengths?.length ? <ul>{candidate.strengths.map((item) => <li key={item}>{item}</li>)}</ul> : <p>暂无优势记录</p>}</section><section><h4>缺口</h4>{candidate.gaps?.length ? <ul>{candidate.gaps.map((item) => <li key={item}>{item}</li>)}</ul> : <p>暂无缺口记录</p>}</section><section><h4>风险</h4>{candidate.risks?.length ? <ul>{candidate.risks.map((item) => <li key={item}>{item}</li>)}</ul> : <p>暂无风险记录</p>}</section><section><h4>建议问题</h4>{candidate.questions?.length ? <ul>{candidate.questions.map((item) => <li key={item}>{item}</li>)}</ul> : <p>暂无建议问题</p>}</section></div></>}</section>{candidate.historicalRule && <details className="historical-rule"><summary>旧版规则结果</summary><p>历史分数：{candidate.historicalRule.score ?? "—"}</p><p>历史建议：{candidate.historicalRule.recommendation || "—"}</p></details>}</div>}
      {tab === "面试与反馈" && <div className="candidate-tab-content"><div className="candidate-interview-toolbar"><div><h3>面试记录</h3><span>安排、通知和反馈统一记录在候选人时间线中。</span></div>{canScheduleCurrent && <button className="button primary" type="button" onClick={() => onScheduleInterview(candidate)}><CalendarDays size={16} />安排面试</button>}</div>{candidate.interviews.length ? <div className="interview-feedback-list">{candidate.interviews.map((item) => <section key={item.time}><header><div><strong>{item.round}</strong><span>{item.time}</span></div><span className="feedback-result">{item.result}</span></header><p>面试官：{item.interviewer}</p><blockquote>{item.feedback}</blockquote>{onOpenInterviewFeedback && item.interviewId && <button className="button secondary" type="button" onClick={() => onOpenInterviewFeedback(item.interviewId)}>查看面试详情</button>}</section>)}</div> : <div className="candidate-empty compact"><MessageSquareText size={23} /><strong>暂无面试记录</strong><span>{candidate.stage === "待安排" ? "可以为该候选人创建第一场面试。" : "面试将在 HR 完成安排后显示。"}</span>{canScheduleCurrent && <button className="button primary" type="button" onClick={() => onScheduleInterview(candidate)}><CalendarDays size={16} />安排面试</button>}</div>}</div>}
      {tab === "时间线" && <div className="candidate-tab-content candidate-timeline">{candidate.timeline.map((item, index) => <div key={`${item.time}-${index}`}><span /><div><strong>{item.action}</strong><p>{item.actor} · {item.time}</p></div></div>)}{candidate.timeline.length === 0 && <div className="candidate-muted">暂无可见时间线记录</div>}</div>}
    </section></main><aside className="candidate-context"><section><h3>当前申请</h3><dl><div><dt>应聘职位</dt><dd>{candidate.position}</dd></div><div><dt>当前状态</dt><dd><StageTag stage={candidate.stage} /></dd></div><div><dt>招聘负责人（HR）</dt><dd>{candidate.owner}</dd></div><div><dt>下一步</dt><dd>{nextStep}</dd></div><div><dt>最近进展</dt><dd>{candidate.lastActivity || "未记录"}</dd></div></dl><p className="candidate-auto-stage-note">状态会在完成业务动作后自动更新，无需手动维护。</p></section><CandidateGovernance candidate={candidate} role={role} onNotify={onNotify} />{!candidate.serverBacked && <section><h3>标签</h3><div className="context-tags">{candidate.tags.map((item) => <span key={item}>{item}</span>)}</div><div className="inline-add"><input value={tagInput} onChange={(event) => setTagInput(event.target.value)} placeholder="添加标签" /><button type="button" aria-label="添加标签" onClick={addTag}><Plus size={15} /></button></div></section>}<section><h3>招聘备注</h3>{notes.map((item, index) => <p className="saved-note" key={typeof item === "object" ? item.id : `${item}-${index}`}>{typeof item === "object" ? item.body : item}</p>)}{notes.length === 0 && <p className="candidate-muted">暂无招聘备注</p>}<textarea rows="4" disabled={pendingAction === "note"} value={note} onChange={(event) => setNote(event.target.value)} placeholder="记录沟通重点或后续事项" /><button className="button secondary full" type="button" disabled={!note.trim() || pendingAction === "note"} onClick={() => void addNote()}>{pendingAction === "note" ? "保存中" : "保存备注"}</button></section></aside></div>
    {selectedWorkflowAction && <WorkflowActionDialog candidate={candidate} action={selectedWorkflowAction} serverBacked={candidate.serverBacked} submitting={pendingAction === "workflow"} actionError={actionError} conflict={conflict} onClose={() => setSelectedWorkflowAction(null)} onCommit={commitWorkflowAction} onConflictRefresh={(latestStage) => { if (candidate.serverBacked) { void onRefresh(); setSelectedWorkflowAction(null); return; } update({ stage: latestStage, version: 3, lastActivity: "刚刚", timeline: [{ time: "刚刚", actor: "系统", action: `检测到其他成员已更新流程` }, ...candidate.timeline] }); setSelectedWorkflowAction(null); onNotify("已刷新为服务端最新状态"); }} />}
    {previewState && <ResumePreview candidate={candidate} file={previewState.file} status={previewState.status} error={previewState.error} downloading={pendingAction === "download"} onClose={closePreview} onRetry={() => void loadPreview()} onDownload={() => void downloadResume()} />}
  </div>;
}

export function CandidatesWorkspace({ mode, setMode, selectedCandidate, setSelectedCandidate, records, setRecords, onNotify, onBackDetail, detailBackLabel, onOpenCandidate, onScheduleInterview, onOpenInterviewFeedback, onAddToTalentPool, filters, onFiltersChange, detailTab, onDetailTabChange, actorName = "张小北", currentRole, controller, detailState, onRetryDetail, pageActionHost, onImport }) {
  function updateCandidate(updated) { if (!updated.serverBacked) setRecords((current) => current.map((item) => item.id === updated.id ? updated : item)); setSelectedCandidate(updated); }
  if (mode === "detail" && detailState?.status === "loading") return <div className="candidate-page"><button className="back-link" type="button" onClick={onBackDetail}><ArrowLeft size={17} />{detailBackLabel}</button><div className="candidate-detail-state" role="status"><LoaderCircle className="spin" size={28} /><strong>正在加载候选人详情</strong><span>将从服务端读取候选人、申请、简历和时间线。</span></div></div>;
  if (mode === "detail" && detailState?.status === "error") return <div className="candidate-page"><button className="back-link" type="button" onClick={onBackDetail}><ArrowLeft size={17} />{detailBackLabel}</button><div className="candidate-detail-state error" role="alert"><CircleAlert size={28} /><strong>候选人详情加载失败</strong><span>{detailState.error}</span><button className="button primary" type="button" onClick={onRetryDetail}><RotateCcw size={16} />重试加载</button></div></div>;
  if (mode === "detail" && selectedCandidate) return <CandidateDetail candidate={selectedCandidate} role={currentRole} onBack={onBackDetail || (() => { setSelectedCandidate(null); setMode("list"); })} backLabel={detailBackLabel} onUpdate={updateCandidate} onNotify={onNotify} onScheduleInterview={onScheduleInterview} onOpenInterviewFeedback={onOpenInterviewFeedback} onAddToTalentPool={onAddToTalentPool} actorName={actorName} controller={controller} onRefresh={onRetryDetail} activeTab={detailTab} onTabChange={onDetailTabChange} />;
  return <CandidateList controller={controller} onOpen={onOpenCandidate} filters={filters} onFiltersChange={onFiltersChange} onImport={onImport} pageActionHost={pageActionHost} />;
}
