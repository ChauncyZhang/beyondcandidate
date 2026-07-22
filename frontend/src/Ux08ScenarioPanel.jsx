import { useState } from "react";
import { AlertTriangle, Beaker, CheckCircle2, ChevronDown, RotateCcw, X } from "lucide-react";

const scenarios = [
  ["default", "默认完整数据"],
  ["new-position", "新职位待导入"],
  ["partial-screening", "筛选部分失败"],
  ["pending-feedback", "面试待反馈"],
  ["talent-reactivation", "人才库待激活"],
  ["empty", "空数据"],
  ["restricted", "权限受限"],
];

export function Ux08ScenarioPanel({ currentScenario, validation, onSelect }) {
  const [open, setOpen] = useState(false);
  const [pending, setPending] = useState(null);
  const currentLabel = scenarios.find(([value]) => value === currentScenario)?.[1] || "默认完整数据";

  function requestScenario(value) {
    if (value === currentScenario) return;
    setPending(value);
  }

  function confirm() {
    onSelect(pending);
    setPending(null);
    setOpen(false);
  }

  return <>
    <aside className={`ux08-scenario-panel ${open ? "open" : ""}`} aria-label="UX-08 验收场景">
      <button className="ux08-scenario-trigger" type="button" onClick={() => setOpen((value) => !value)} aria-expanded={open}><Beaker size={17} /><span>验收场景</span>{validation.length === 0 ? <CheckCircle2 size={15} /> : <AlertTriangle size={15} />}</button>
      {open && <div className="ux08-scenario-body"><header><div><strong>UX-08 验收场景</strong><small>仅开发环境可见</small></div><button className="icon-button" type="button" aria-label="关闭验收场景" onClick={() => setOpen(false)}><X size={17} /></button></header><label>当前场景<div><select value={currentScenario} onChange={(event) => requestScenario(event.target.value)}>{scenarios.map(([value, label]) => <option value={value} key={value}>{label}</option>)}</select><ChevronDown size={14} /></div></label><section className={validation.length === 0 ? "valid" : "invalid"}>{validation.length === 0 ? <CheckCircle2 size={17} /> : <AlertTriangle size={17} />}<div><strong>{validation.length === 0 ? "数据一致" : "发现数据不一致"}</strong><small>{validation[0] || `${currentLabel}可开始测试`}</small></div></section><button className="button secondary compact" type="button" onClick={() => requestScenario("default")}><RotateCcw size={15} />恢复默认数据</button></div>}
    </aside>
    {pending && <div className="ux07-dialog-backdrop"><section className="ux07-dialog" role="dialog" aria-modal="true" aria-label="切换验收场景"><header><div><h3>切换验收场景</h3><p>当前原型中的临时操作将被清除。</p></div><button className="icon-button" type="button" aria-label="关闭" onClick={() => setPending(null)}><X size={19} /></button></header><div className="ux07-danger-impact"><AlertTriangle size={21} /><span>将重置为“{scenarios.find(([value]) => value === pending)?.[1]}”的确定性测试数据。此操作只影响当前浏览器原型。</span></div><footer><button className="button secondary" type="button" onClick={() => setPending(null)}>取消</button><button className="button primary" type="button" onClick={confirm}>确认切换</button></footer></section></div>}
  </>;
}
