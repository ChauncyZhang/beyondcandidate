import { useEffect, useMemo, useRef, useState } from "react";
import "./product-theme-people.css";
import {
  ArrowLeft,
  BriefcaseBusiness,
  CheckCircle2,
  ChevronDown,
  ChevronRight,
  CircleAlert,
  Download,
  Eye,
  FileCheck2,
  FileText,
  FolderPlus,
  History,
  LockKeyhole,
  Plus,
  RefreshCw,
  Search,
  ShieldAlert,
  Users,
  X,
} from "lucide-react";
import { buildResumeDocument } from "./resumeDocument.js";
import { buildReactivatedCandidateSummary, createLatestMembershipRequest } from "./talentController.js";
import { PagePrimaryAction } from "./PagePrimaryAction.jsx";
import { applicationStageLabel } from "./recruitingTerminology.js";

export const initialTalentPools = [
  { id: "POOL-AI", name: "AI 技术人才", purpose: "沉淀大模型、算法与 AI 应用方向的可复用候选人", suitableRoles: ["AI 工程师", "算法工程师"], owner: "张小北", visibility: "招聘团队可见", memberIds: ["CAN-001", "CAN-002", "CAN-003"], retentionDays: 730, recentActivity: "今天 10:45", activity: "张小北更新了 2 位人才的适合岗位" },
  { id: "POOL-ENGINEER", name: "资深工程人才", purpose: "后端、前端和平台方向的成熟工程师", suitableRoles: ["Java 后端工程师", "前端工程师"], owner: "陈雨", visibility: "招聘团队可见", memberIds: ["CAN-004", "CAN-006"], retentionDays: 730, recentActivity: "昨天 17:20", activity: "陈雨新增了陈浩" },
  { id: "POOL-PRODUCT", name: "AI 产品与业务人才", purpose: "AI 产品、企业服务和业务增长方向人才", suitableRoles: ["产品经理", "AI 产品经理"], owner: "张小北", visibility: "产品职位成员可见", memberIds: ["CAN-005"], retentionDays: 540, recentActivity: "07-10 18:05", activity: "孙悦从历史人才库重新激活" },
  { id: "POOL-FOLLOW", name: "重点跟进观察", purpose: "当前时机不匹配但值得持续沟通的人才", suitableRoles: ["技术与产品岗位"], owner: "张小北", visibility: "仅自己可见", memberIds: ["CAN-002", "CAN-006"], retentionDays: 365, recentActivity: "07-09 14:30", activity: "设置了 2 项下次联系提醒" },
];

export const initialTalentMemberships = [
  { id: "MEM-001", poolId: "POOL-AI", candidateId: "CAN-001", suitableRoles: ["AI 工程师", "RAG 工程师"], tags: ["LLM", "RAG", "可快速沟通"], owner: "张小北", joinedAt: "2026-06-20", reason: "核心技术匹配，当前到岗时间不合适", source: "AI 工程师申请", nextContact: "2026-07-18", retentionUntil: "2028-06-19", recentInteraction: "今天 10:45 更新适合岗位", latestConclusion: "建议保持联系", status: "正常" },
  { id: "MEM-002", poolId: "POOL-AI", candidateId: "CAN-002", suitableRoles: ["算法工程师"], tags: ["机器学习", "需补充 LLM 经验"], owner: "张小北", joinedAt: "2026-05-12", reason: "算法基础较好，待积累生产经验", source: "AI 工程师申请", nextContact: "2026-07-15", retentionUntil: "2026-08-05", recentInteraction: "07-09 14:30 记录跟进计划", latestConclusion: "三个月后复访", status: "即将到期" },
  { id: "MEM-003", poolId: "POOL-AI", candidateId: "CAN-003", suitableRoles: ["AI 工程师", "大模型应用工程师"], tags: ["高优先级", "Agent"], owner: "陈雨", joinedAt: "2026-04-08", reason: "岗位高度匹配，保留长期关系", source: "智联招聘", nextContact: "2026-08-01", retentionUntil: "2028-04-07", recentInteraction: "昨天 16:30 电话沟通", latestConclusion: "优先激活", status: "正常" },
  { id: "MEM-004", poolId: "POOL-ENGINEER", candidateId: "CAN-004", suitableRoles: ["Java 后端工程师", "架构师"], tags: ["Java", "高并发"], owner: "陈雨", joinedAt: "2026-07-01", reason: "高并发经验可复用", source: "员工内推", nextContact: "2026-07-22", retentionUntil: "2028-06-30", recentInteraction: "昨天 14:12 更新面试安排", latestConclusion: "建议推进", status: "正常" },
  { id: "MEM-005", poolId: "POOL-ENGINEER", candidateId: "CAN-006", suitableRoles: ["前端工程师", "前端架构师"], tags: ["React", "设计系统"], owner: "刘思远", joinedAt: "2026-03-18", reason: "复杂后台经验适合后续高级岗位", source: "BOSS 直聘", nextContact: "2026-09-01", retentionUntil: "2027-03-17", recentInteraction: "07-10 15:20 完成技术面", latestConclusion: "需要补充管理能力", status: "正常" },
  { id: "MEM-006", poolId: "POOL-PRODUCT", candidateId: "CAN-005", suitableRoles: ["产品经理", "AI 产品经理"], tags: ["AI 产品", "B 端"], owner: "张小北", joinedAt: "2026-02-10", reason: "企业服务产品经验完整", source: "历史人才库", nextContact: "2026-07-30", retentionUntil: "2027-08-03", recentInteraction: "07-10 18:05 重新激活到产品经理", latestConclusion: "已重新激活", status: "正常" },
  { id: "MEM-007", poolId: "POOL-FOLLOW", candidateId: "CAN-002", suitableRoles: ["算法工程师"], tags: ["观察", "季度联系"], owner: "张小北", joinedAt: "2026-05-12", reason: "关注大模型项目成长", source: "AI 工程师申请", nextContact: "2026-07-15", retentionUntil: "2027-05-11", recentInteraction: "07-09 14:30 添加提醒", latestConclusion: "暂不适合", status: "正常" },
  { id: "MEM-008", poolId: "POOL-FOLLOW", candidateId: "CAN-006", suitableRoles: ["前端负责人"], tags: ["观察", "管理潜力"], owner: "张小北", joinedAt: "2026-06-01", reason: "等待更匹配的高级岗位", source: "前端工程师申请", nextContact: "2026-08-15", retentionUntil: "2027-05-31", recentInteraction: "07-08 11:10 更新跟进备注", latestConclusion: "半年内可再联系", status: "正常" },
];

function VisibilityTag({ value }) {
  return <span className={`pool-visibility ${value.includes("仅") || value.includes("成员") ? "restricted" : "team"}`}>{value.includes("仅") || value.includes("成员") ? <LockKeyhole size={13} /> : <Users size={13} />}{value}</span>;
}

function PoolList({
  pools,
  memberships,
  onOpen,
  onCreate,
  status = "ready",
  error = "",
  onRetry,
  nextCursor,
  loadingMore,
  onLoadMore,
  pageActionHost,
}) {
  const [query, setQuery] = useState("");
  const [visibility, setVisibility] = useState("全部范围");
  const [createOpen, setCreateOpen] = useState(false);
  const filtered = pools.filter(
    (pool) =>
      `${pool.name}${pool.purpose}${pool.suitableRoles.join("")}`
        .toLowerCase()
        .includes(query.toLowerCase()) &&
      (visibility === "全部范围" || pool.visibility === visibility),
  );
  const expiring = memberships.filter(
    (item) => item.status === "即将到期",
  ).length;
  const due = memberships.filter(
    (item) => item.nextContact <= "2026-07-18",
  ).length;
  return (
    <div className="talent-page pool-list-page">
      <PagePrimaryAction host={pageActionHost}>
        <button
          className="button primary"
          type="button"
          onClick={() => setCreateOpen(true)}
        >
          <FolderPlus size={17} />
          新建人才库
        </button>
      </PagePrimaryAction>
      <div className="talent-page-heading">
        <div>
          <h2>人才库管理</h2>
          <p>按关系和适合岗位沉淀人才，并在合适时机重新激活。</p>
        </div>
      </div>
      <div className="pool-metrics">
        <div>
          <span>可复用人才</span>
          <strong>
            {new Set(memberships.map((item) => item.candidateId)).size ||
              pools.reduce((sum, pool) => sum + (pool.memberCount || 0), 0)}
          </strong>
          <small>跨 {pools.length} 个人才库</small>
        </div>
        <div>
          <span>即将到期</span>
          <strong>{expiring}</strong>
          <small>需要确认保留期限</small>
        </div>
        <div>
          <span>待跟进</span>
          <strong>{due}</strong>
          <small>未来 7 天</small>
        </div>
        <div>
          <span>已同步人才</span>
          <strong>
            {pools.reduce((sum, pool) => sum + (pool.memberCount || 0), 0)}
          </strong>
          <small>按当前授权范围</small>
        </div>
      </div>
      <section className="pool-list-panel">
        <div className="pool-toolbar">
          <label className="pool-search">
            <Search size={16} />
            <input
              aria-label="搜索人才库"
              value={query}
              onChange={(event) => setQuery(event.target.value)}
              placeholder="搜索人才库名称、用途或适合岗位"
            />
          </label>
          <label className="pool-select">
            <select
              aria-label="可见范围筛选"
              value={visibility}
              onChange={(event) => setVisibility(event.target.value)}
            >
              <option>全部范围</option>
              <option>招聘团队可见</option>
              <option>指定成员可见</option>
              <option>仅自己可见</option>
            </select>
            <ChevronDown size={14} />
          </label>
        </div>
        {status === "loading" && pools.length === 0 ? (
          <div className="talent-empty" role="status">
            <RefreshCw size={24} />
            <strong>正在加载人才库</strong>
          </div>
        ) : status === "error" && pools.length === 0 ? (
          <div className="talent-empty" role="alert">
            <CircleAlert size={24} />
            <strong>人才库加载失败</strong>
            <span>{error}</span>
            <button
              className="button secondary"
              type="button"
              onClick={onRetry}
            >
              重试
            </button>
          </div>
        ) : (
          <div className="pool-table">
            <div className="pool-table-head">
              <span>人才库</span>
              <span>适合岗位</span>
              <span>人才库负责人</span>
              <span>可见范围</span>
              <span>人才数</span>
              <span>默认保留</span>
              <span>最近活动</span>
              <span />
            </div>
            {filtered.map((pool) => (
              <button
                className="pool-table-row"
                type="button"
                key={pool.id}
                onClick={() => onOpen(pool.id)}
              >
                <span className="pool-name-cell">
                  <span>
                    <BriefcaseBusiness size={17} />
                  </span>
                  <span>
                    <strong>{pool.name}</strong>
                    <small>{pool.purpose}</small>
                  </span>
                </span>
                <span>{pool.suitableRoles.join("、")}</span>
                <span>{pool.owner}</span>
                <span>
                  <VisibilityTag value={pool.visibility} />
                </span>
                <span>
                  <strong>{pool.memberCount ?? pool.memberIds.length}</strong>{" "}
                  人
                </span>
                <span>{pool.retentionDays} 天</span>
                <span>
                  <strong>{pool.recentActivity}</strong>
                  <small>{pool.activity}</small>
                </span>
                <ChevronRight size={17} />
              </button>
            ))}
            {filtered.length === 0 && (
              <div className="talent-empty">
                <BriefcaseBusiness size={24} />
                <strong>没有符合条件的人才库</strong>
                <span>调整搜索或可见范围后重试。</span>
              </div>
            )}
            {nextCursor && (
              <div className="talent-load-more">
                <button
                  className="button secondary"
                  type="button"
                  disabled={loadingMore}
                  onClick={onLoadMore}
                >
                  {loadingMore ? "正在加载" : "加载更多人才库"}
                </button>
              </div>
            )}
          </div>
        )}
      </section>
      {createOpen && (
        <CreatePoolDialog
          onClose={() => setCreateOpen(false)}
          onCreate={async (pool) => {
            const created = await onCreate(pool);
            if (created) setCreateOpen(false);
          }}
        />
      )}
    </div>
  );
}

function CreatePoolDialog({ onClose, onCreate }) {
  const [form, setForm] = useState({ name: "", purpose: "", suitableRole: "", owner: "张小北", visibility: "招聘团队可见", retentionDays: 730 });
  const [errors, setErrors] = useState({});
  const [submitting, setSubmitting] = useState(false);
  async function submit() { const next = {}; if (!form.name.trim()) next.name = "请输入人才库名称"; if (!form.purpose.trim()) next.purpose = "请说明该人才库的用途"; if (!form.suitableRole.trim()) next.suitableRole = "请填写至少一个适合岗位"; setErrors(next); if (Object.keys(next).length) return; setSubmitting(true); try { await onCreate({ name: form.name.trim(), purpose: form.purpose.trim(), suitableRoles: form.suitableRole.split(/[、,，]/).map((item) => item.trim()).filter(Boolean), owner: form.owner, visibility: form.visibility, retentionDays: Number(form.retentionDays) }); } finally { setSubmitting(false); } }
  return <div className="talent-dialog-backdrop" role="presentation" onMouseDown={onClose}><section className="talent-dialog" role="dialog" aria-modal="true" aria-label="新建人才库" onMouseDown={(event) => event.stopPropagation()}><header><div><h3>新建人才库</h3><p>用于明确沉淀范围、协作权限和保留期限。</p></div><button className="icon-button" type="button" aria-label="关闭" disabled={submitting} onClick={onClose}><X size={19} /></button></header><div className="talent-dialog-body"><label>人才库名称<input value={form.name} disabled={submitting} onChange={(event) => { setForm({ ...form, name: event.target.value }); setErrors({ ...errors, name: "" }); }} placeholder="例如：AI 技术人才" />{errors.name && <small className="field-error">{errors.name}</small>}</label><label>用途说明<textarea rows="3" value={form.purpose} disabled={submitting} onChange={(event) => { setForm({ ...form, purpose: event.target.value }); setErrors({ ...errors, purpose: "" }); }} placeholder="说明什么人才应该进入该分组" />{errors.purpose && <small className="field-error">{errors.purpose}</small>}</label><label>适合岗位<input value={form.suitableRole} disabled={submitting} onChange={(event) => { setForm({ ...form, suitableRole: event.target.value }); setErrors({ ...errors, suitableRole: "" }); }} placeholder="使用顿号或逗号分隔" />{errors.suitableRole && <small className="field-error">{errors.suitableRole}</small>}</label><div className="talent-form-grid"><label>可见范围<select value={form.visibility} disabled={submitting} onChange={(event) => setForm({ ...form, visibility: event.target.value })}><option>招聘团队可见</option><option>仅自己可见</option></select></label><label>默认保留期限<select value={form.retentionDays} disabled={submitting} onChange={(event) => setForm({ ...form, retentionDays: event.target.value })}><option value="365">365 天</option><option value="540">540 天</option><option value="730">730 天</option></select></label></div></div><footer><button className="button secondary" type="button" disabled={submitting} onClick={onClose}>取消</button><button className="button primary" type="button" disabled={submitting} onClick={() => void submit()}>{submitting ? "正在创建" : "创建人才库"}</button></footer></section></div>;
}

function ReactivateDrawer({ member, candidate, pool, positions, onClose, onReactivate, onOpenCandidate }) {
  const authorized = positions.filter((item) => item.status === "招聘中" || item.status === "open");
  const preferred = authorized.find((item) => !candidate.applications.some((application) => application.position === item.name && !["已淘汰", "已撤回", "已录用"].includes(application.state))) || authorized[0];
  const [positionId, setPositionId] = useState(preferred?.id || "");
  const [resume, setResume] = useState("当前简历");
  const [created, setCreated] = useState(null);
  const position = authorized.find((item) => item.id === positionId);
  const conflict = position && candidate.applications.find((item) => item.position === position.name && !["已淘汰", "已撤回", "已录用"].includes(item.state));
  const history = position && candidate.applications.filter((item) => item.position === position.name && ["已淘汰", "已撤回", "已录用"].includes(item.state));
  const [submitting, setSubmitting] = useState(false);
  const [submitError, setSubmitError] = useState("");
  async function submit() { setSubmitting(true); setSubmitError(""); try { const result = await onReactivate(member.id, position, pool.id, resume); if (result) setCreated({ ...result, position: position.name }); } catch (error) { setSubmitError(error?.code === "active_application_exists" ? "该职位已有进行中的申请，请从候选人档案继续处理。" : "重新激活失败，请刷新后重试"); } finally { setSubmitting(false); } }
  return <aside className="reactivate-drawer" aria-label="重新激活候选人"><header><div><h2>重新激活到职位</h2><p>{candidate.name} · 来自 {pool.name}</p></div><button className="icon-button" type="button" aria-label="关闭" onClick={onClose}><X size={20} /></button></header>{created ? <div className="reactivation-success"><CheckCircle2 size={34} /><h3>已创建新的职位申请</h3><p>{candidate.name} 已重新激活到“{created.position}”，历史申请和人才库关系均已保留。</p><dl><div><dt>新申请状态</dt><dd>新简历</dd></div><div><dt>来源</dt><dd>人才库重新激活</dd></div><div><dt>关联人才库</dt><dd>{pool.name}</dd></div></dl><button className="button primary full" type="button" onClick={() => onOpenCandidate(buildReactivatedCandidateSummary(candidate, created, position))}>去新申请</button><button className="button secondary full" type="button" onClick={onClose}>返回人才库</button></div> : <div className="reactivate-body"><section className="reactivate-summary"><span>{candidate.name.slice(-1)}</span><div><h3>{candidate.name}</h3><p>{candidate.role} · {candidate.company} · {candidate.city}</p><small>{candidate.summary}</small></div></section><label>目标职位<select aria-label="目标职位" value={positionId} onChange={(event) => setPositionId(event.target.value)}>{authorized.map((item) => <option value={item.id} key={item.id}>{item.name} · {item.department}</option>)}</select></label>{conflict && <div className="reactivate-conflict"><CircleAlert size={19} /><div><strong>该职位已有进行中的申请</strong><p>当前状态为“{applicationStageLabel(conflict.state)}”，不能重复创建。请进入现有申请继续处理。</p></div><button type="button" onClick={() => onOpenCandidate(candidate)}>查看现有申请</button></div>}{!conflict && history.length > 0 && <div className="reactivate-warning"><History size={18} /><div><strong>发现 {history.length} 条历史终态申请</strong><p>新申请会关联历史记录，但不会覆盖原结论。</p></div></div>}<section className="reactivate-preview"><h3>新申请预览</h3><dl><div><dt>候选人</dt><dd>{candidate.name}</dd></div><div><dt>目标职位</dt><dd>{position?.name || "无可用职位"}</dd></div><div><dt>初始状态</dt><dd>新简历</dd></div><div><dt>来源</dt><dd>人才库重新激活</dd></div><div><dt>招聘负责人（HR）</dt><dd>{position?.owner || "-"}</dd></div><div><dt>简历</dt><dd>来源申请简历</dd></div></dl></section>{submitError && <p className="field-error" role="alert">{submitError}</p>}</div>} {!created && <footer><button className="button secondary" type="button" disabled={submitting} onClick={onClose}>取消</button><button className="button primary" type="button" disabled={submitting || !position || Boolean(conflict)} onClick={() => void submit()}>{submitting ? "正在创建" : "确认创建新申请"}</button></footer>}</aside>;
}

function ResumeFileActions({ document, onPreview, onDownload }) {
  return <div className="resume-file-actions"><FileText size={17} /><button className="resume-file-name" type="button" onClick={onPreview}>{document.fileName.replace(/\.txt$/, ".pdf")}</button><button className="resume-action" type="button" onClick={onPreview}><Eye size={15} />预览</button><button className="resume-action" type="button" onClick={onDownload}><Download size={15} />下载</button></div>;
}

function ResumePreviewDrawer({ candidate, document, onClose, onDownload }) {
  return <aside className="resume-preview-drawer" aria-label="简历预览"><header><div><FileCheck2 size={22} /><div><h2>简历预览</h2><p>{document.fileName.replace(/\.txt$/, ".pdf")}</p></div></div><button className="icon-button" type="button" aria-label="关闭简历预览" onClick={onClose}><X size={20} /></button></header><div className="resume-preview-meta"><span>PDF</span><span>{document.pages.length} 页</span><span>解析质量良好</span><span>原型预览</span></div><div className="resume-preview-body">{document.pages.map((page) => <article className="resume-preview-page" key={page.number}><header><span>{candidate.name} · 简历</span><small>{page.number} / {document.pages.length}</small></header><h3>{page.title}</h3><pre>{page.content}</pre></article>)}</div><footer><button className="button secondary" type="button" onClick={onClose}>关闭预览</button><button className="button primary" type="button" onClick={onDownload}><Download size={16} />下载简历</button></footer></aside>;
}

function MemberDrawer({ member, candidate, pool, pools, onClose, onUpdate, onMove, onRemove, onRefer, referring, positions, onReactivateCandidate, onOpenCandidate, onNotify, initialReactivateOpen = false }) {
  const [tagInput, setTagInput] = useState("");
  const [tags, setTags] = useState(member.tags);
  const [selectedRoles, setSelectedRoles] = useState(member.suitableRoles);
  const [danger, setDanger] = useState(null);
  const [dangerReason, setDangerReason] = useState("");
  const [reactivateOpen, setReactivateOpen] = useState(initialReactivateOpen);
  const [resumePreviewOpen, setResumePreviewOpen] = useState(false);
  const resumeDocument = candidate.serverBacked ? null : buildResumeDocument(candidate);
  const isDeferredPool = pool.systemKey === "ai_screening_deferred";
  const isReferred = member.sourceStage === "用人经理复核";
  const selectablePositions = positions.filter((item) => item.name && !["已关闭", "已归档", "closed", "archived"].includes(item.status));
  useEffect(() => { setTags(member.tags); setSelectedRoles(member.suitableRoles); }, [member.id, member.tags, member.suitableRoles]);
  function saveTags(next) { setTags(next); onUpdate({ ...member, tags: next, suitableRoles: selectedRoles }); }
  function addTag() { const value = tagInput.trim(); if (!value || tags.includes(value)) return; saveTags([...tags, value]); setTagInput(""); }
  function removeTag(value) { saveTags(tags.filter((item) => item !== value)); }
  function updateSuitableRole(value, selected) { const next = selected ? [...new Set([...selectedRoles, value])] : selectedRoles.filter((item) => item !== value); setSelectedRoles(next); onUpdate({ ...member, suitableRoles: next, tags }); }
  function downloadResume() {
    try {
      const url = URL.createObjectURL(new Blob([resumeDocument.downloadText], { type: resumeDocument.mimeType }));
      const anchor = globalThis.document.createElement("a");
      anchor.href = url;
      anchor.download = resumeDocument.fileName;
      anchor.click();
      URL.revokeObjectURL(url);
      onNotify("简历下载已开始");
    } catch {
      onNotify("简历下载失败，请稍后重试");
    }
  }
  return <><aside className="talent-member-drawer" aria-label="人才详情"><header><div><span>{candidate.name.slice(-1)}</span><div><h2>{candidate.name}</h2><p>{candidate.role} · {candidate.company}</p></div></div><button className="icon-button" type="button" aria-label="关闭" onClick={onClose}><X size={20} /></button></header><div className="talent-member-body"><section><h3>候选人摘要</h3><p>{candidate.summary}</p>{candidate.serverBacked ? <p className="field-hint">联系方式和原始简历请从候选人档案按权限查看。</p> : <dl><div><dt>手机</dt><dd>{candidate.phone}</dd></div><div><dt>邮箱</dt><dd>{candidate.email}</dd></div><div className="resume-detail-row"><dt>当前简历</dt><dd><ResumeFileActions document={resumeDocument} onPreview={() => setResumePreviewOpen(true)} onDownload={downloadResume} /></dd></div></dl>}</section>{isDeferredPool && <section><h3>AI 初筛未进入评审</h3><dl><div><dt>原岗位</dt><dd>{member.originalJob.title || "来源申请不可见"}</dd></div><div><dt>AI 匹配分</dt><dd>{member.finalScore ?? "不可见"}</dd></div><div><dt>进入人才库时间</dt><dd>{member.deferredAt || "未记录"}</dd></div><div><dt>主要缺口</dt><dd>{member.mainGaps.join("；") || "未记录"}</dd></div><div><dt>跟进负责人</dt><dd>{member.owner}</dd></div></dl></section>}<section><h3>人才库信息</h3><dl><div><dt>所在人才库</dt><dd>{pool.name}</dd></div><div><dt>加入原因</dt><dd>{member.reason}</dd></div><div><dt>来源</dt><dd>{member.source}</dd></div><div><dt>加入日期</dt><dd>{member.joinedAt}</dd></div><div><dt>最近互动</dt><dd>{member.recentInteraction}</dd></div></dl><label>保留至<input type="date" value={member.retentionUntil} onChange={(event) => onUpdate({ ...member, suitableRoles: selectedRoles, tags, retentionUntil: event.target.value, status: "正常" })} /></label></section><section><h3>推荐岗位</h3><div className="member-tags">{selectedRoles.map((item) => <span className="member-removable-tag" key={item}>{item}<button type="button" aria-label={`移除推荐岗位：${item}`} onClick={() => updateSuitableRole(item, false)}><X size={12} /></button></span>)}</div><div className="member-position-picker" aria-label="推荐岗位">{selectablePositions.length > 0 ? selectablePositions.map((item) => <label key={item.id || item.name}><input type="checkbox" checked={selectedRoles.includes(item.name)} onChange={(event) => updateSuitableRole(item.name, event.target.checked)} /><span><strong>{item.name}</strong><small>{[item.department, item.status].filter(Boolean).join(" · ")}</small></span></label>) : <p>暂无可选择的职位</p>}</div></section><section><h3>人才标签</h3><div className="member-tags">{tags.map((item) => <span className="member-removable-tag" key={item}>{item}<button type="button" aria-label={`移除标签：${item}`} onClick={() => removeTag(item)}><X size={12} /></button></span>)}</div><div className="member-inline-add"><input value={tagInput} onChange={(event) => setTagInput(event.target.value)} onKeyDown={(event) => { if (event.key === "Enter") { event.preventDefault(); addTag(); } }} placeholder="输入标签名称" /><button type="button" aria-label="添加标签" onClick={addTag}><Plus size={15} /></button></div></section><section><h3>跟进设置</h3><label>下次联系<input type="date" value={member.nextContact} onChange={(event) => onUpdate({ ...member, suitableRoles: selectedRoles, tags, nextContact: event.target.value })} /></label></section><section><h3>历史申请</h3>{candidate.applications.map((item) => <div className="member-application" key={`${item.position}-${item.created}`}><strong>{item.position}</strong><span>{applicationStageLabel(item.state)}</span><small>{item.created} · {item.source}</small></div>)}</section><section className="membership-actions"><h3>成员关系</h3>{!member.serverBacked && <label>移动到<select defaultValue="" onChange={(event) => { if (event.target.value) onMove(member, event.target.value); }}><option value="">选择目标人才库</option>{pools.filter((item) => item.id !== pool.id).map((item) => <option value={item.id} key={item.id}>{item.name}</option>)}</select></label>}<button type="button" onClick={() => onRemove(member)}>从当前人才库移出</button><button className="danger-text" type="button" onClick={() => setDanger("永久不再联系")}>永久不再联系</button><button className="danger-text" type="button" onClick={() => setDanger("黑名单")}>加入黑名单</button></section></div><footer><button className="button secondary" type="button" onClick={() => onOpenCandidate(candidate)}>候选人档案</button>{isDeferredPool ? <button className="button primary" type="button" aria-label={`转交用人经理：${candidate.name}`} disabled={referring || isReferred} onClick={() => void onRefer(member)}>{isReferred ? "已转交用人经理" : referring ? "正在转交" : "转交用人经理"}</button> : <button className="button primary" type="button" onClick={() => setReactivateOpen(true)}>重新激活到职位</button>}</footer></aside>{danger && <div className="talent-dialog-backdrop"><section className="talent-dialog danger-confirm"><header><div><h3>{danger}</h3><p>该操作与“暂不适合”不同，会限制后续联系和激活。</p></div><button className="icon-button" type="button" aria-label="关闭" onClick={() => setDanger(null)}><X size={19} /></button></header><div className="talent-dialog-body"><div className="danger-impact"><ShieldAlert size={22} /><span>执行后候选人仍保留审计记录，但不会出现在普通人才搜索和推荐结果中。</span></div><label>操作原因<textarea rows="4" value={dangerReason} onChange={(event) => setDangerReason(event.target.value)} placeholder="必须说明原因" /></label></div><footer><button className="button secondary" type="button" onClick={() => setDanger(null)}>取消</button><button className="button danger" type="button" disabled={!dangerReason.trim()} onClick={() => { onUpdate({ ...member, suitableRoles: selectedRoles, tags, status: danger, latestConclusion: `${danger}：${dangerReason}` }); setDanger(null); }}>确认{danger}</button></footer></section></div>}{reactivateOpen && <ReactivateDrawer member={member} candidate={candidate} pool={pool} positions={positions} onClose={() => setReactivateOpen(false)} onReactivate={onReactivateCandidate} onOpenCandidate={onOpenCandidate} />}{resumePreviewOpen && resumeDocument && <ResumePreviewDrawer candidate={candidate} document={resumeDocument} onClose={() => setResumePreviewOpen(false)} onDownload={downloadResume} />}</>;
}

function DeferredPoolDetail({ pool, pools, memberships, candidates, positions, onBack, onUpdateMember, onMove, onRemove, onReferMember, onReactivateCandidate, onOpenCandidate, onNotify, status, error, onRetry, nextCursor, loadingMore, onLoadMore }) {
  const [query, setQuery] = useState("");
  const [selectedMemberId, setSelectedMemberId] = useState(null);
  const [referringMemberId, setReferringMemberId] = useState(null);
  const [referralMessage, setReferralMessage] = useState("");
  const poolMembers = memberships
    .filter((item) => item.poolId === pool.id)
    .map((member) => ({ member, candidate: candidates.find((item) => item.id === member.candidateId) || member.candidate }))
    .filter(({ candidate }) => candidate);
  const filtered = poolMembers.filter(({ member, candidate }) => `${candidate.name}${member.originalJob.title}${member.mainGaps.join("")}${member.owner}`.toLowerCase().includes(query.toLowerCase()));
  const selected = poolMembers.find(({ member }) => member.id === selectedMemberId);

  async function refer(member) {
    if (member.sourceStage === "用人经理复核" || referringMemberId) return;
    setReferringMemberId(member.id);
    setReferralMessage(`正在将 ${member.candidate.name} 转交用人经理`);
    try {
      await onReferMember(member);
      setReferralMessage(`${member.candidate.name} 已转交用人经理`);
    } catch {
      setReferralMessage(`${member.candidate.name} 转交失败，请刷新后重试`);
    } finally {
      setReferringMemberId(null);
    }
  }

  return <div className="talent-page pool-detail-page">
    <button className="back-link" type="button" onClick={onBack}><ArrowLeft size={17} />返回人才库</button>
    <section className="pool-detail-hero"><div><span><BriefcaseBusiness size={20} /></span><div><div><h2>{pool.name}</h2><VisibilityTag value={pool.visibility} /></div><p>{pool.purpose}</p><small>人才库负责人：{pool.owner} · AI 初筛未进入评审恢复链路</small></div></div><strong>{pool.memberCount ?? poolMembers.length}<small>人才</small></strong></section>
    <p className="field-hint" role="status" aria-live="polite">{referralMessage}</p>
    <section className="pool-detail-panel">
      <div className="pool-detail-toolbar"><label className="pool-search"><Search size={16} /><input aria-label="搜索未进入评审人才" value={query} onChange={(event) => setQuery(event.target.value)} placeholder="搜索姓名、原岗位、缺口或负责人" /></label></div>
      {status === "loading" && memberships.length === 0 ? <div className="talent-empty" role="status"><RefreshCw size={24} /><strong>正在加载未进入评审人才</strong></div> : status === "error" && memberships.length === 0 ? <div className="talent-empty" role="alert"><CircleAlert size={24} /><strong>未进入评审人才加载失败</strong><span>{error}</span><button className="button secondary" type="button" onClick={onRetry}>重试</button></div> : <div className="talent-table" role="table" aria-label="AI 初筛未进入评审人才">
        <div className="talent-table-head" role="row"><span role="columnheader">人才</span><span role="columnheader">原岗位</span><span role="columnheader">AI 匹配分</span><span role="columnheader">进入人才库时间</span><span role="columnheader">主要缺口</span><span role="columnheader">跟进负责人</span><span role="columnheader">状态</span><span role="columnheader">操作</span></div>
        {filtered.map(({ member, candidate }) => <div className="talent-table-row" role="row" key={member.id}>
          <span role="cell" data-label="人才"><button className="talent-person" type="button" aria-label={`查看人才详情：${candidate.name}`} onClick={() => setSelectedMemberId(member.id)}><span>{candidate.name.slice(-1)}</span><span><strong>{candidate.name}</strong><small>{candidate.role} · {candidate.city}</small></span></button></span>
          <span role="cell" data-label="原岗位">{member.originalJob.title || "来源申请不可见"}</span>
          <span role="cell" data-label="AI 匹配分">{member.finalScore ?? "不可见"}</span>
          <span role="cell" data-label="进入人才库时间">{member.deferredAt || "未记录"}</span>
          <span role="cell" data-label="主要缺口"><small>{member.mainGaps.join("；") || "未记录"}</small></span>
          <span role="cell" data-label="跟进负责人">{member.owner}</span>
          <span role="cell" data-label="状态">{member.sourceStage}</span>
          <span role="cell" data-label="操作"><button className="button primary small" type="button" aria-label={`转交用人经理：${candidate.name}`} disabled={member.sourceStage === "用人经理复核" || Boolean(referringMemberId)} onClick={() => void refer(member)}>{member.sourceStage === "用人经理复核" ? "已转交用人经理" : referringMemberId === member.id ? "正在转交" : "转交用人经理"}</button></span>
        </div>)}
        {filtered.length === 0 && <div className="talent-empty"><Search size={24} /><strong>没有符合条件的未进入评审人才</strong><span>调整搜索条件后重试。</span></div>}
        {nextCursor && <div className="talent-load-more"><button className="button secondary" type="button" disabled={loadingMore} onClick={onLoadMore}>{loadingMore ? "正在加载" : "加载更多人才"}</button></div>}
      </div>}
    </section>
    {selected && <MemberDrawer key={selected.member.id} member={selected.member} candidate={selected.candidate} pool={pool} pools={pools} positions={positions} onClose={() => setSelectedMemberId(null)} onUpdate={onUpdateMember} onMove={onMove} onRemove={(member) => { onRemove(member); setSelectedMemberId(null); }} onRefer={refer} referring={Boolean(referringMemberId)} onReactivateCandidate={onReactivateCandidate} onOpenCandidate={onOpenCandidate} onNotify={onNotify} />}
  </div>;
}

function PoolDetail({ pool, pools, memberships, candidates, positions, onBack, onUpdateMember, onMove, onRemove, onReferMember, onReactivateCandidate, onOpenCandidate, onNotify, status = "ready", error = "", onRetry, nextCursor, loadingMore, onLoadMore }) {
  const [query, setQuery] = useState(""); const [role, setRole] = useState("全部适合岗位"); const [city, setCity] = useState("全部城市"); const [owner, setOwner] = useState("全部跟进人"); const [followup, setFollowup] = useState("全部跟进"); const [selectedMemberId, setSelectedMemberId] = useState(null); const [reactivateMemberId, setReactivateMemberId] = useState(null);
  const poolMembers = memberships.filter((item) => item.poolId === pool.id).map((member) => ({ member, candidate: candidates.find((item) => item.id === member.candidateId) || member.candidate })).filter((item) => item.candidate);
  const filtered = poolMembers.filter(({ member, candidate }) => `${candidate.name}${candidate.role}${candidate.phone}${candidate.email}${member.tags.join("")}${member.suitableRoles.join("")}`.toLowerCase().includes(query.toLowerCase()) && (role === "全部适合岗位" || member.suitableRoles.includes(role)) && (city === "全部城市" || candidate.city === city) && (owner === "全部跟进人" || member.owner === owner) && (followup === "全部跟进" || (followup === "7 天内联系" && member.nextContact <= "2026-07-19") || (followup === "即将到期" && member.status === "即将到期")));
  const selected = poolMembers.find((item) => item.member.id === selectedMemberId);
  if (pool.systemKey === "ai_screening_deferred") return <DeferredPoolDetail pool={pool} pools={pools} memberships={memberships} candidates={candidates} positions={positions} onBack={onBack} onUpdateMember={onUpdateMember} onMove={onMove} onRemove={onRemove} onReferMember={onReferMember} onReactivateCandidate={onReactivateCandidate} onOpenCandidate={onOpenCandidate} onNotify={onNotify} status={status} error={error} onRetry={onRetry} nextCursor={nextCursor} loadingMore={loadingMore} onLoadMore={onLoadMore} />;
  return <div className="talent-page pool-detail-page"><button className="back-link" type="button" onClick={onBack}><ArrowLeft size={17} />返回人才库</button><section className="pool-detail-hero"><div><span><BriefcaseBusiness size={20} /></span><div><div><h2>{pool.name}</h2><VisibilityTag value={pool.visibility} /></div><p>{pool.purpose}</p><small>人才库负责人：{pool.owner} · 默认保留 {pool.retentionDays} 天 · 最近活动 {pool.recentActivity}</small></div></div><strong>{pool.memberCount ?? poolMembers.length}<small>人才</small></strong></section><section className="pool-detail-panel"><div className="pool-detail-toolbar"><label className="pool-search"><Search size={16} /><input aria-label="搜索人才" value={query} onChange={(event) => setQuery(event.target.value)} placeholder="搜索姓名、技能或标签" /></label><label className="pool-select"><select aria-label="适合岗位筛选" value={role} onChange={(event) => setRole(event.target.value)}><option>全部适合岗位</option>{[...new Set(poolMembers.flatMap((item) => item.member.suitableRoles))].map((item) => <option key={item}>{item}</option>)}</select><ChevronDown size={14} /></label><label className="pool-select"><select aria-label="城市筛选" value={city} onChange={(event) => setCity(event.target.value)}><option>全部城市</option>{[...new Set(poolMembers.map((item) => item.candidate.city))].map((item) => <option key={item}>{item}</option>)}</select><ChevronDown size={14} /></label><label className="pool-select"><select aria-label="跟进人筛选" value={owner} onChange={(event) => setOwner(event.target.value)}><option>全部跟进人</option>{[...new Set(poolMembers.map((item) => item.member.owner))].map((item) => <option key={item}>{item}</option>)}</select><ChevronDown size={14} /></label><label className="pool-select"><select aria-label="跟进状态筛选" value={followup} onChange={(event) => setFollowup(event.target.value)}><option>全部跟进</option><option>7 天内联系</option><option>即将到期</option></select><ChevronDown size={14} /></label></div>{status === "loading" && memberships.length === 0 ? <div className="talent-empty" role="status"><RefreshCw size={24} /><strong>正在加载人才</strong></div> : status === "error" && memberships.length === 0 ? <div className="talent-empty" role="alert"><CircleAlert size={24} /><strong>人才列表加载失败</strong><span>{error}</span><button className="button secondary" type="button" onClick={onRetry}>重试</button></div> : <div className="talent-table"><div className="talent-table-head"><span>人才</span><span>适合岗位</span><span>技能与标签</span><span>历史职位/结论</span><span>跟进人</span><span>下次联系</span><span>保留期限</span><span>操作</span></div>{filtered.map(({ member, candidate }) => <div className="talent-table-row" key={member.id}><button className="talent-person" type="button" onClick={() => setSelectedMemberId(member.id)}><span>{candidate.name.slice(-1)}</span><span><strong>{candidate.name}</strong><small>{candidate.role} · {candidate.city}</small></span></button><span>{member.suitableRoles.join("、")}</span><span><div className="talent-row-tags">{[...candidate.skills.slice(0, 2), ...member.tags.slice(0, 1)].map((item) => <small key={item}>{item}</small>)}</div></span><span><strong>{candidate.applications.map((item) => item.position).join("、")}</strong><small>{member.latestConclusion}</small></span><span>{member.owner}</span><span><strong>{member.nextContact || "未设置"}</strong><small>{member.recentInteraction}</small></span><span className={member.status === "即将到期" ? "retention-warning" : ""}>{member.retentionUntil}<small>{member.status}</small></span><span><button className="button primary small" type="button" onClick={() => { setSelectedMemberId(member.id); setReactivateMemberId(member.id); }}>重新激活</button></span></div>)}{filtered.length === 0 && <div className="talent-empty"><Search size={24} /><strong>没有符合条件的人才</strong><span>调整筛选条件后重试。</span></div>}{nextCursor && <div className="talent-load-more"><button className="button secondary" type="button" disabled={loadingMore} onClick={onLoadMore}>{loadingMore ? "正在加载" : "加载更多人才"}</button></div>}</div>}</section>{selected && <MemberDrawer key={selected.member.id} member={selected.member} candidate={selected.candidate} pool={pool} pools={pools} positions={positions} initialReactivateOpen={selected.member.id === reactivateMemberId} onClose={() => { setSelectedMemberId(null); setReactivateMemberId(null); }} onUpdate={onUpdateMember} onMove={onMove} onRemove={(member) => { onRemove(member); setSelectedMemberId(null); setReactivateMemberId(null); }} onReactivateCandidate={onReactivateCandidate} onOpenCandidate={onOpenCandidate} onNotify={onNotify} />}</div>;
}

export function TalentPoolWorkspace({ mode, setMode, selectedPoolId, setSelectedPoolId, pools = [], setPools, memberships = [], setMemberships, candidates, positions, onReactivateCandidate, onReferralComplete = () => {}, onOpenCandidate, onNotify, controller, actorId, pageActionHost }) {
  const serverBacked = Boolean(controller);
  const [serverState, setServerState] = useState({ poolStatus: serverBacked ? "loading" : "ready", pools: [], poolCursor: null, loadingPools: false, memberStatus: "idle", memberships: [], memberCursor: null, loadingMembers: false, error: "" });
  const memberLoadRef = useRef(null);
  if (!memberLoadRef.current) memberLoadRef.current = createLatestMembershipRequest();
  const activePools = serverBacked ? serverState.pools : pools;
  const activeMemberships = serverBacked ? serverState.memberships : memberships;
  const selectedPool = activePools.find((item) => item.id === selectedPoolId);

  async function loadPools({ cursor = null, append = false } = {}) {
    if (!controller) return;
    setServerState((current) => ({ ...current, poolStatus: append ? current.poolStatus : "loading", loadingPools: append, error: "" }));
    try {
      const page = await controller.listPools({ limit: 50, cursor: cursor || undefined });
      setServerState((current) => ({ ...current, poolStatus: "ready", pools: append ? [...current.pools, ...page.records] : page.records, poolCursor: page.nextCursor, loadingPools: false }));
    } catch {
      setServerState((current) => ({ ...current, poolStatus: "error", loadingPools: false, error: "请检查网络后重试。" }));
    }
  }

  async function loadMembers(poolId, { cursor = null, append = false } = {}) {
    if (!controller || !poolId) return;
    const operation = memberLoadRef.current.start();
    setServerState((current) => ({ ...current, memberStatus: append ? current.memberStatus : "loading", loadingMembers: append, error: "" }));
    try {
      const page = await controller.listMemberships(poolId, { limit: 50, cursor: cursor || undefined }, { signal: operation.signal });
      if (!operation.isCurrent()) return;
      setServerState((current) => ({ ...current, memberStatus: "ready", memberships: append ? [...current.memberships, ...page.records] : page.records, memberCursor: page.nextCursor, loadingMembers: false }));
    } catch (error) {
      if (!operation.isCurrent() || error?.name === "AbortError") return;
      setServerState((current) => ({ ...current, memberStatus: "error", loadingMembers: false, error: "请检查网络后重试。" }));
    }
  }

  useEffect(() => { if (controller) void loadPools(); }, [controller]);
  useEffect(() => { if (controller && mode === "detail" && selectedPoolId) void loadMembers(selectedPoolId); }, [controller, mode, selectedPoolId]);
  useEffect(() => () => memberLoadRef.current.cancel(), [controller]);

  function openPool(id) { setSelectedPoolId(id); setMode("detail"); }
  async function createPool(pool) { if (!serverBacked) { setPools((current) => [pool, ...current]); return pool; } try { const created = await controller.createPool(pool, actorId); if (!created) return null; setServerState((current) => ({ ...current, pools: [created, ...current.pools] })); onNotify("人才库已创建"); return created; } catch { onNotify("人才库创建失败，请检查名称和权限后重试"); return null; } }
  async function updateMember(updated) { if (!serverBacked) { setMemberships((current) => current.map((item) => item.id === updated.id ? updated : item)); return; } try { const saved = await controller.updateMembership(updated); setServerState((current) => ({ ...current, memberships: current.memberships.map((item) => item.id === saved.id ? saved : item) })); onNotify("人才信息已保存"); } catch { onNotify("人才信息保存失败，请刷新后重试"); } }
  function moveMember(member, targetPoolId) { if (serverBacked) return; setMemberships((current) => current.map((item) => item.id === member.id ? { ...item, poolId: targetPoolId } : item)); setPools((current) => current.map((pool) => ({ ...pool, memberIds: pool.id === member.poolId ? pool.memberIds.filter((id) => id !== member.candidateId) : pool.id === targetPoolId ? [...new Set([...pool.memberIds, member.candidateId])] : pool.memberIds }))); }
  async function removeMember(member) { if (!serverBacked) { setMemberships((current) => current.filter((item) => item.id !== member.id)); return; } try { await controller.removeMembership(member, "由招聘人员从人才库移出"); setServerState((current) => ({ ...current, memberships: current.memberships.filter((item) => item.id !== member.id), pools: current.pools.map((pool) => pool.id === member.poolId ? { ...pool, memberCount: Math.max(0, pool.memberCount - 1) } : pool) })); onNotify("已从人才库移出"); } catch { onNotify("移出失败，请刷新后重试"); } }
  async function referToReview(member) { try { const result = await controller.referToReview(member.id, member.version); if (!result.membership) throw new Error("referral membership missing"); setServerState((current) => ({ ...current, memberships: current.memberships.map((item) => item.id === result.membership.id ? result.membership : item) })); onReferralComplete(result.application); onNotify("已转交用人经理"); return result; } catch (error) { onNotify("转交失败，请刷新后重试"); throw error; } }
  async function reactivate(memberId, position) { if (!serverBacked) return onReactivateCandidate(memberId, position); return controller.reactivate(memberId, position.id); }
  if (mode === "detail" && selectedPool) return <PoolDetail pool={selectedPool} pools={activePools} memberships={activeMemberships} candidates={candidates} positions={positions} onBack={() => { setSelectedPoolId(null); setMode("list"); }} onUpdateMember={updateMember} onMove={moveMember} onRemove={removeMember} onReferMember={referToReview} onReactivateCandidate={reactivate} onOpenCandidate={onOpenCandidate} onNotify={onNotify} status={serverState.memberStatus} error={serverState.error} onRetry={() => void loadMembers(selectedPool.id)} nextCursor={serverState.memberCursor} loadingMore={serverState.loadingMembers} onLoadMore={() => void loadMembers(selectedPool.id, { cursor: serverState.memberCursor, append: true })} />;
  return <PoolList pools={activePools} memberships={activeMemberships} onOpen={openPool} onCreate={createPool} status={serverState.poolStatus} error={serverState.error} onRetry={() => void loadPools()} nextCursor={serverState.poolCursor} loadingMore={serverState.loadingPools} onLoadMore={() => void loadPools({ cursor: serverState.poolCursor, append: true })} pageActionHost={pageActionHost} />;
}
