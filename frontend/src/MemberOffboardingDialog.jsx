import { useEffect, useState, useSyncExternalStore } from "react";
import { AlertTriangle, ArrowRightLeft, CheckCircle2, RefreshCw, UserMinus } from "lucide-react";
import { createMemberOffboardingController } from "./organizationSettings.js";

function localHref(value) {
  return typeof value === "string" && /^\/(?!\/)/.test(value) && !/[\\\s]/.test(value) ? value : null;
}

function permissionReason(item) {
  return item.status === "blocked" && /permission|scope|role_required/.test(item.reason_code || "") ? "权限不足：" : "";
}

export function MemberOffboardingDialog({ member, Drawer, onClose, onComplete, controller: suppliedController }) {
  const [controller] = useState(() => suppliedController || createMemberOffboardingController());
  const state = useSyncExternalStore(controller.subscribe, controller.getSnapshot, controller.getSnapshot);
  const [confirmed, setConfirmed] = useState(false);
  const [mode, setMode] = useState(null);
  const detail = state.preview;
  const busy = state.status === "saving";
  const ready = state.status === "ready";
  const success = state.status === "success";
  const canTransfer = ready && detail?.successors.some((user) => user.id === state.successorId && user.eligible);
  useEffect(() => {
    void controller.load(member.id);
    return () => { controller.cancel(); };
  }, [controller, member.id]);
  const close = () => { if (controller.cancel()) onClose(); };
  function refresh() { setConfirmed(false); setMode(null); void controller.refresh(); }
  function chooseSuccessor(id) { setConfirmed(false); setMode(null); void controller.load(member.id, id || null); }
  async function submit() {
    if (!confirmed || !mode || busy) return;
    const result = await controller.submit(mode === "revoke");
    setConfirmed(false); setMode(null);
    if (result) onComplete(result);
  }
  return <Drawer surfaceClassName="member-offboarding-drawer" title={member.offboarding ? "继续成员交接" : "移除成员并交接"} description={`${member.name} · ${member.email}`} busy={busy} onClose={close} footer={<>
    <button className="button secondary" type="button" disabled={busy} onClick={close}>{success ? "完成" : "取消"}</button>
    {!success && mode && <button className="button danger" type="button" disabled={!ready || busy || !confirmed || (mode === "transfer" && !canTransfer)} onClick={() => void submit()}><CheckCircle2 size={16} />{busy ? "正在提交…" : mode === "revoke" ? "确认仅收回账号" : member.offboarding ? "确认继续交接" : "确认移除并交接"}</button>}
  </>}>
    <div className="member-offboarding">
      <div className="offboarding-impact"><AlertTriangle size={19} aria-hidden="true" /><p>收回账号后，该成员将无法登录，现有会话及激活、重置链接将失效。历史记录仍保留原姓名，不复制角色或负责范围。</p></div>
      {state.error && <div className="settings-error" role="alert"><AlertTriangle size={17} />{state.error}</div>}
      {state.status === "loading" && <p role="status"><RefreshCw size={16} />正在加载交接预览…</p>}
      {["error", "conflict"].includes(state.status) && <button className="button secondary" type="button" onClick={refresh}><RefreshCw size={16} />刷新预览</button>}
      {detail && <>
        {success && <div className="offboarding-result" role="status"><CheckCircle2 size={19} /><div><strong>账号已收回 · {detail.handover_status === "completed" ? "交接已完成" : "仍有待交接事项"}</strong><p>本次已交接 {detail.transferred_count ?? 0} 项。{detail.handover_status !== "completed" && "请处理下方未完成事项，随后从成员列表继续交接。"}</p></div></div>}
        {detail.handover_error && <div className="settings-error" role="alert"><AlertTriangle size={17} />{detail.handover_error}</div>}
        <dl className="offboarding-facts"><div><dt>账号状态</dt><dd>{detail.removed_at ? "已移除" : "尚未移除"}</dd></div>{detail.removed_at && <div><dt>移除时间</dt><dd>{new Date(detail.removed_at).toLocaleString("zh-CN", { hour12: false })}</dd></div>}<div><dt>交接状态</dt><dd>{detail.handover_status === "completed" ? "已完成" : detail.handover_status === "pending" ? "待交接" : "未开始"}</dd></div></dl>
        {!success && <>
          <label className="offboarding-successor">统一接任人<select aria-label="统一接任人" disabled={state.status === "loading" || busy} value={state.successorId || ""} onChange={(event) => chooseSuccessor(event.target.value)}><option value="">暂不选择</option>{detail.successors.map((user) => <option key={user.id} value={user.id} disabled={!user.eligible}>{user.display_name}{!user.eligible ? `（不可用：${user.reason || "不符合接任条件"}）` : ""}</option>)}</select></label>
          {!detail.successors.some((user) => user.eligible) && <p className="offboarding-warning">暂无可用接任人，仍可仅收回账号，稍后交接。</p>}
          {detail.successors.some((user) => !user.eligible) && <ul className="offboarding-unavailable" aria-label="不可用接任人">{detail.successors.filter((user) => !user.eligible).map((user) => <li key={user.id}>{user.display_name}：{user.reason || "不符合接任条件"}</li>)}</ul>}
        </>}
        <section aria-labelledby="offboarding-counts"><h3 id="offboarding-counts">责任清单</h3><div className="offboarding-counts">{detail.counts.map((count) => <div key={count.category}><strong>{count.label}</strong><span>共 {count.total} · 可交接 {count.transferable} · 待处理 {count.blocked}</span></div>)}</div>
          {detail.items.length === 0 && <p className="offboarding-empty">暂无待交接事项。</p>}
          <ul className="offboarding-items">{detail.items.map((item, index) => <li key={`${item.category}-${item.resource_type}-${item.resource_id}-${index}`}><div><strong>{item.title}</strong><span className={item.status === "blocked" ? "offboarding-warning" : "status-ok"}>{item.status === "blocked" ? "待处理" : "可交接"}</span></div><small>{detail.counts.find((count) => count.category === item.category)?.label || item.category}</small>{item.reason && <p>{permissionReason(item)}{item.reason}</p>}{item.status === "blocked" && !item.reason && <p>此事项无法自动交接，请管理员核对责任和权限后处理。</p>}{localHref(item.href) && <a href={localHref(item.href)} target="_blank" rel="noopener noreferrer">查看事项（新窗口）</a>}</li>)}</ul>
        </section>
        <div className="offboarding-manual"><h3>人工与外部事项</h3><p>权限不足的事项需先在现有成员配置中调整，再刷新预览。已提交的飞书审批、外部日程及已进行面试的缺失反馈，仍需按清单原因人工核对；本操作不代表外部交接完成。</p></div>
        {!success && <>
          <div className="offboarding-actions"><button className="button primary" type="button" disabled={!canTransfer || busy} onClick={() => { setMode("transfer"); setConfirmed(false); }}><ArrowRightLeft size={16} />{member.offboarding ? "继续交接" : "移除成员并交接"}</button>{!detail.removed_at && <button className="button secondary" type="button" disabled={!ready || busy} onClick={() => { setMode("revoke"); setConfirmed(false); }}><UserMinus size={16} />仅收回账号，稍后交接</button>}<button className="icon-button" type="button" aria-label="刷新预览" title="刷新预览" disabled={!ready || busy} onClick={refresh}><RefreshCw size={16} /></button></div>
          {mode && <div className="offboarding-confirm"><p>{mode === "revoke" ? "仅收回账号，不执行责任交接。未完成事项将保留为待交接。" : `将收回账号，并仅把符合权限的责任交给 ${detail.successors.find((user) => user.id === state.successorId)?.display_name || "所选接任人"}；受阻事项仍需处理。`}</p><label><input type="checkbox" checked={confirmed} disabled={!ready || busy} onChange={(event) => setConfirmed(event.target.checked)} />我已核对账号影响、责任清单与未完成事项</label></div>}
        </>}
      </>}
    </div>
  </Drawer>;
}
