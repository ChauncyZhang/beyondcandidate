import { useEffect, useMemo, useRef, useState } from "react";
import { AlertTriangle, ArrowRight, BarChart3, CalendarRange, CheckCircle2, ChevronDown, Clock3, Download, FileCheck2, LockKeyhole, MessageSquareText, RefreshCw, UsersRound } from "lucide-react";
import { canPerformAction } from "./roleCapabilities.js";
import { createExportIntent, createLatestOperation, failedReportState, isTerminalExportFailure, loadingReportState } from "./reportWorkspaceState.js";
import { PagePrimaryAction } from "./PagePrimaryAction.jsx";
import { applicationStageLabel } from "./recruitingTerminology.js";
import "./product-theme-admin.css";

function NoPermission({ onNotify }) {
  return <section className="ux07-permission"><LockKeyhole size={34} /><h2>暂无报表查看权限</h2><p>当前账号不能读取招聘聚合数据或导出候选人记录。</p><button className="button primary" type="button" onClick={() => onNotify("请联系招聘管理员开通报表权限")}>联系管理员</button></section>;
}

function MetricCard({ label, value, unit, note, icon: Icon }) {
  return <section className="report-metric"><div><span>{label}</span><Icon size={18} /></div><strong>{value ?? "—"}<small>{value == null ? "" : unit}</small></strong><p>{note}</p></section>;
}

export function ScreeningQualityPanel({ quality }) {
  return <section className="report-panel quality-panel"><header><div><h3>筛选质量</h3><p>当前筛选质量基于简历解析与 LLM 自动评分；历史规则字段即使由 API 返回，也不在当前面板展示。</p></div></header><div className="quality-rates">{[["解析成功率", quality.parseSuccessRate], ["LLM 成功率", quality.llmSuccessRate]].map(([label, value]) => <div key={label}><span>{label}</span><strong>{value}%</strong><div><i style={{ width: `${value}%` }} /></div></div>)}</div></section>;
}

function rangeForPeriod(period, now = new Date()) {
  const to = new Date(now);
  const from = new Date(now);
  if (period === "近 7 天") from.setUTCDate(from.getUTCDate() - 7);
  else if (period === "本季度") from.setUTCMonth(Math.floor(from.getUTCMonth() / 3) * 3, 1);
  else from.setUTCDate(from.getUTCDate() - 30);
  if (period === "本季度") from.setUTCHours(0, 0, 0, 0);
  return { from: from.toISOString(), to: to.toISOString() };
}

function saveBlob({ blob, filename }) {
  const url = URL.createObjectURL(blob);
  const anchor = globalThis.document.createElement("a");
  anchor.href = url;
  anchor.download = filename || "recruiting-report.csv";
  anchor.click();
  URL.revokeObjectURL(url);
}

function ExportAction({ state, onCreate, onDownload }) {
  if (state.status === "ready") return <button className="button primary" type="button" onClick={onDownload}><Download size={16} />下载 CSV（{state.record.rowCount} 行）</button>;
  if (state.status === "downloading") return <button className="button primary" type="button" disabled><RefreshCw size={16} />正在下载</button>;
  if (state.status === "creating" || state.status === "processing") return <button className="button primary" type="button" disabled><RefreshCw size={16} />正在生成导出</button>;
  return <button className="button primary" type="button" onClick={onCreate}><Download size={16} />导出当前范围</button>;
}

export function ReportWorkspace({ positions = [], currentRole, onDrillDown, onNotify, controller, pageActionHost }) {
  const [filters, setFilters] = useState({ period: "近 30 天", jobId: "" });
  const [requestVersion, setRequestVersion] = useState(0);
  const [state, setState] = useState({ status: "loading", data: null, error: "" });
  const [exportState, setExportState] = useState({ status: "idle", record: null, error: "" });
  const loadOperationRef = useRef(null);
  const exportOperationRef = useRef(null);
  const downloadOperationRef = useRef(null);
  const exportIntentRef = useRef(null);
  if (!loadOperationRef.current) loadOperationRef.current = createLatestOperation();
  if (!exportOperationRef.current) exportOperationRef.current = createLatestOperation();
  if (!downloadOperationRef.current) downloadOperationRef.current = createLatestOperation();
  if (!exportIntentRef.current) exportIntentRef.current = createExportIntent();
  const canView = canPerformAction(currentRole, "查看报表");
  const selectedPosition = positions.find((item) => item.id === filters.jobId) || null;
  const query = useMemo(() => ({ jobId: filters.jobId, ...rangeForPeriod(filters.period) }), [filters]);

  useEffect(() => {
    if (!controller || !canView) return undefined;
    const operation = loadOperationRef.current.start();
    setState(loadingReportState());
    void controller.load(query, { signal: operation.signal }).then(
      (data) => { if (operation.isCurrent()) setState({ status: "ready", data, error: "" }); },
      (error) => { if (operation.isCurrent() && error?.name !== "AbortError") setState(failedReportState()); },
    );
    return () => loadOperationRef.current.cancel();
  }, [canView, controller, query, requestVersion]);

  useEffect(() => {
    exportOperationRef.current.cancel();
    downloadOperationRef.current.cancel();
    exportIntentRef.current.reset();
    setExportState({ status: "idle", record: null, error: "" });
  }, [query]);

  useEffect(() => () => {
    loadOperationRef.current.cancel();
    exportOperationRef.current.cancel();
    downloadOperationRef.current.cancel();
    exportIntentRef.current.reset();
  }, []);

  async function createExport() {
    const operation = exportOperationRef.current.start();
    downloadOperationRef.current.cancel();
    setExportState({ status: "creating", record: null, error: "" });
    try {
      const created = await controller.createExport(query, { signal: operation.signal, idempotencyKey: exportIntentRef.current.key() });
      if (!operation.isCurrent()) return;
      if (!created) throw new Error("export missing");
      setExportState({ status: "processing", record: created, error: "" });
      const completed = await controller.waitForExport(created.id, { signal: operation.signal });
      if (!operation.isCurrent()) return;
      if (isTerminalExportFailure(completed)) exportIntentRef.current.reset();
      if (!completed || completed.status !== "succeeded") throw new Error("export failed");
      exportIntentRef.current.succeed();
      setExportState({ status: "ready", record: completed, error: "" });
      onNotify("报表导出已生成");
    } catch (error) {
      if (!operation.isCurrent() || error?.name === "AbortError") return;
      setExportState({ status: "error", record: null, error: error?.code === "export_timeout" ? "导出仍在处理中，请稍后重试。" : "导出生成失败，请稍后重试。" });
    }
  }

  async function downloadExport() {
    if (!exportState.record) return;
    const operation = downloadOperationRef.current.start();
    const exportId = exportState.record.id;
    setExportState((current) => ({ ...current, status: "downloading", error: "" }));
    try {
      const file = await controller.downloadExport(exportId, { signal: operation.signal });
      if (!operation.isCurrent()) return;
      saveBlob(file);
      setExportState((current) => ({ ...current, status: "ready" }));
      onNotify("报表下载已开始");
    } catch (error) {
      if (!operation.isCurrent() || error?.name === "AbortError") return;
      setExportState((current) => ({ ...current, status: "ready", error: "下载票据已失效，请重新下载。" }));
    }
  }

  if (!canView) return <div className="report-page"><div className="report-heading"><div><h2>基础招聘报表</h2><p>招聘漏斗、筛选质量和面试效率。</p></div></div><NoPermission onNotify={onNotify} /></div>;

  const data = state.data;
  const empty = state.status === "ready" && data?.totalApplications === 0 && data?.quality.parseTotal === 0 && data?.interviews.count === 0;
  const maxStageCount = Math.max(1, ...(data?.stages || []).map((item) => item.currentCount));

  return <div className="report-page">
    <PagePrimaryAction host={pageActionHost}>{data?.canExport && <ExportAction state={exportState} onCreate={() => void createExport()} onDownload={() => void downloadExport()} />}</PagePrimaryAction>
    <div className="report-heading"><div><h2>基础招聘报表</h2><p>所有指标均按当前账号的服务端授权范围计算。</p></div></div>
    <section className="report-filter-panel"><div className="report-filters"><label><CalendarRange size={16} /><select aria-label="时间范围" value={filters.period} onChange={(event) => setFilters((current) => ({ ...current, period: event.target.value }))}><option>近 7 天</option><option>近 30 天</option><option>本季度</option></select><ChevronDown size={14} /></label><label><select aria-label="报表职位" value={filters.jobId} onChange={(event) => setFilters((current) => ({ ...current, jobId: event.target.value }))}><option value="">全部有权限职位</option>{positions.filter((item) => item.id && item.name).map((item) => <option value={item.id} key={item.id}>{item.name}</option>)}</select><ChevronDown size={14} /></label><button className="button secondary" type="button" onClick={() => setRequestVersion((value) => value + 1)}><RefreshCw size={15} />刷新</button></div></section>

    {exportState.error && <div className="settings-error" role="alert"><AlertTriangle size={17} />{exportState.error}</div>}
    {state.status === "loading" && !data ? <div className="report-skeleton" role="status" aria-live="polite" aria-label="报表加载中">{Array.from({ length: 8 }, (_, index) => <span key={index} />)}</div> : state.status === "error" && !data ? <section className="report-empty" role="alert"><AlertTriangle size={32} /><h3>报表加载失败</h3><p>{state.error}</p><button className="button primary" type="button" onClick={() => setRequestVersion((value) => value + 1)}>重新加载</button></section> : empty ? <section className="report-empty" role="status"><BarChart3 size={32} /><h3>当前范围暂无招聘数据</h3><p>可调整时间范围或选择其他有权限职位。</p></section> : data && <>
      {state.status === "error" && <div className="settings-error" role="alert"><AlertTriangle size={17} />{state.error}<button type="button" onClick={() => setRequestVersion((value) => value + 1)}>重试</button></div>}
      <div className="report-metrics"><MetricCard label="招聘申请" value={data.totalApplications} unit="份" note={selectedPosition ? selectedPosition.name : "全部有权限职位"} icon={UsersRound} /><MetricCard label="简历解析成功率" value={data.quality.parseSuccessRate} unit="%" note={`${data.quality.parseSucceeded}/${data.quality.parseTotal} 份解析成功`} icon={FileCheck2} /><MetricCard label="面试反馈完成率" value={data.interviews.feedbackCompletionRate} unit="%" note={`${data.interviews.feedbackCompleted}/${data.interviews.feedbackTotal} 份必需反馈`} icon={MessageSquareText} /><MetricCard label="平均反馈时效" value={data.interviews.averageFeedbackHours} unit="小时" note="从面试结束到提交反馈" icon={Clock3} /></div>
      <div className="report-grid">
        <div className="report-column">
          <section className="report-panel funnel-panel"><header><div><h3>招聘漏斗</h3><p>当前阶段人数，点击后查看候选人</p></div><span>{filters.period}</span></header><div className="report-funnel">{data.stages.map((item) => <button type="button" key={item.apiStage} onClick={() => onDrillDown({ jobId: filters.jobId || "全部职位", stage: item.stage })} style={{ "--funnel-fill": `${Math.max(0, item.currentCount / maxStageCount * 100)}%` }}><span>{applicationStageLabel(item.stage)}</span><strong>{item.currentCount}</strong><small>{data.totalApplications ? `${Math.round(item.currentCount / data.totalApplications * 100)}%` : "0%"}</small><ArrowRight size={15} /></button>)}</div></section>
          <ScreeningQualityPanel quality={data.quality} />
        </div>
        <div className="report-column">
          <section className="report-panel duration-panel"><header><div><h3>阶段平均停留</h3><p>基于申请阶段事件计算</p></div></header><div className="duration-bars">{data.stages.map((item) => <div key={item.apiStage}><span>{applicationStageLabel(item.stage)}</span><div><i style={{ width: `${Math.min(100, item.averageDays / Math.max(1, ...data.stages.map((row) => row.averageDays)) * 100)}%` }} /></div><strong>{item.averageDays} 天</strong><small>当前 {item.currentCount} 人</small></div>)}</div></section>
          <section className="report-panel interview-report"><header><div><h3>面试效率</h3><p>面试场次、必需反馈和反馈时效</p></div></header><div><section><span>面试场次</span><strong>{data.interviews.count}</strong></section><section><span>完成反馈</span><strong>{data.interviews.feedbackCompleted}</strong></section><section><span>平均反馈时效</span><strong>{data.interviews.averageFeedbackHours}h</strong></section></div><p><CheckCircle2 size={15} />反馈完成率 {data.interviews.feedbackCompletionRate}%</p></section>
        </div>
      </div>
    </>}
  </div>;
}
