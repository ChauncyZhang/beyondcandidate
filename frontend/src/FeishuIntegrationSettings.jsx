import { useEffect, useRef, useState } from "react";
import { AlertTriangle, CheckCircle2, ClipboardCheck, Plus, RefreshCw, ShieldCheck, X } from "lucide-react";
import { apiClient } from "./apiClient.js";
import {
  buildFeishuConfigPayload,
  buildFeishuOnboardingApprovalPayload,
  FEISHU_APPROVAL_CONTROL_TYPES,
  FEISHU_ONBOARDING_FIELDS,
  autoMatchFeishuDepartmentMappings,
  getFeishuDepartmentDirectoryErrorMessage,
  getFeishuConfigErrorMessage,
  getFeishuConnectionTestErrorMessage,
  getFeishuOnboardingApprovalErrorMessage,
  getFeishuApprovalValidationDisabledReason,
  getFeishuApproverSelectionError,
  normalizeFeishuConfig,
  normalizeFeishuDepartmentDirectory,
  normalizeFeishuOnboardingApprovalConfig,
} from "./feishuIntegration.js";

const emptySecrets = { app_secret: "", verification_token: "", encrypt_key: "" };

function approvalDraft(config, departments) {
  const mappingByDepartment = new Map(config.departmentMappings.map((item) => [item.departmentId, item.feishuDepartmentId]));
  return {
    enabled: config.enabled,
    approvalCode: config.approvalCode,
    nodeApprovers: config.nodeApprovers.map((item) => ({ ...item, userIds: [...item.userIds] })),
    fieldMapping: Object.fromEntries(Object.entries(config.fieldMapping).map(([key, value]) => [key, { ...value }])),
    departmentMappings: departments.map((department) => ({
      departmentId: department.id,
      departmentName: department.name || "未命名部门",
      feishuDepartmentId: mappingByDepartment.get(department.id) || "",
    })),
  };
}

function FeishuOnboardingApprovalSettings({ client, onNotify }) {
  const [config, setConfig] = useState(normalizeFeishuOnboardingApprovalConfig());
  const [draft, setDraft] = useState(() => approvalDraft(normalizeFeishuOnboardingApprovalConfig(), []));
  const [status, setStatus] = useState("loading");
  const [message, setMessage] = useState("");
  const [addingDepartment, setAddingDepartment] = useState(false);
  const [departmentName, setDepartmentName] = useState("");
  const [feishuDepartments, setFeishuDepartments] = useState([]);
  const [directoryStatus, setDirectoryStatus] = useState("idle");
  const [directoryMessage, setDirectoryMessage] = useState("");
  const directoryRequestRef = useRef(0);
  const operationRef = useRef(false);
  const [needsRefresh, setNeedsRefresh] = useState(false);
  const [approverOptions, setApproverOptions] = useState({ nodes: [], members: [] });
  const [approverStatus, setApproverStatus] = useState("idle");
  const [approverMessage, setApproverMessage] = useState("");
  const [approverCode, setApproverCode] = useState("");
  const approverRequestRef = useRef(0);

  useEffect(() => {
    setApproverStatus("idle");
    setApproverCode("");
    const timer = setTimeout(() => {
      if (draft.approvalCode.trim()) void loadApproverOptions(draft.approvalCode.trim());
    }, 300);
    return () => { clearTimeout(timer); approverRequestRef.current += 1; };
  }, [client, draft.approvalCode]);

  useEffect(() => {
    let active = true;
    Promise.all([client.getFeishuOnboardingApprovalConfig(), client.listDepartments()]).then(([value, departments]) => {
      if (!active) return;
      const next = normalizeFeishuOnboardingApprovalConfig(value);
      const nextDraft = approvalDraft(next, departments);
      setConfig(next);
      setDraft(nextDraft);
      setStatus("ready");
      void loadFeishuDepartments();
    }).catch((error) => {
      if (!active) return;
      setMessage(getFeishuOnboardingApprovalErrorMessage(error));
      setStatus("error");
    });
    return () => { active = false; directoryRequestRef.current += 1; };
  }, [client]);

  const dirty = JSON.stringify(buildFeishuOnboardingApprovalPayload(draft)) !== JSON.stringify(buildFeishuOnboardingApprovalPayload(config));
  const busy = ["saving", "validating", "refreshing", "creating_department"].includes(status);
  const approverDisabledReason = !draft.approvalCode.trim() ? ""
    : approverStatus === "loading" ? "正在读取审批节点和成员，请稍候。"
      : approverStatus !== "ready" || approverCode !== draft.approvalCode.trim() ? "请先成功读取审批节点和成员。"
        : getFeishuApproverSelectionError(draft, approverOptions);
  const validationDisabledReason = needsRefresh && !busy
    ? "配置版本尚未同步，请先重试刷新配置版本；当前输入已保留。"
    : getFeishuApprovalValidationDisabledReason(draft, status, directoryStatus) || approverDisabledReason;
  const needsNodeValidation = Boolean(config.approvalCode) && config.approvalNodes === null;
  const canEnable = config.validationStatus === "valid" && !needsNodeValidation && !approverDisabledReason
    && JSON.stringify(buildFeishuOnboardingApprovalPayload({ ...draft, enabled: config.enabled })) === JSON.stringify(buildFeishuOnboardingApprovalPayload(config));

  async function loadApproverOptions(code) {
    const requestId = ++approverRequestRef.current;
    setApproverStatus("loading"); setApproverMessage("");
    try {
      const options = await client.getFeishuOnboardingApproverOptions(code);
      if (requestId !== approverRequestRef.current) return;
      if (!Array.isArray(options?.nodes) || !Array.isArray(options?.members)) throw new Error("invalid_approver_options");
      setApproverOptions(options); setApproverCode(code); setApproverStatus("ready");
    } catch (error) {
      if (requestId !== approverRequestRef.current) return;
      setApproverStatus("error"); setApproverMessage(getFeishuOnboardingApprovalErrorMessage(error));
    }
  }

  function updateApprovers(nodeId, userIds) {
    setDraft((current) => ({ ...current, enabled: false, nodeApprovers: [
      ...current.nodeApprovers.filter((item) => item.nodeId !== nodeId),
      ...(userIds.length ? [{ nodeId, userIds }] : []),
    ] }));
    setMessage("");
  }

  function updateField(key, property, value) {
    setDraft((current) => ({
      ...current,
      enabled: false,
      fieldMapping: { ...current.fieldMapping, [key]: { ...current.fieldMapping[key], [property]: value } },
    }));
    setMessage("");
  }

  function updateDepartment(departmentId, value) {
    setDraft((current) => ({
      ...current,
      enabled: false,
      departmentMappings: current.departmentMappings.map((item) => item.departmentId === departmentId ? { ...item, feishuDepartmentId: value } : item),
    }));
    setMessage("");
  }

  function updateGenderOption(key, value) {
    setDraft((current) => ({
      ...current,
      enabled: false,
      fieldMapping: {
        ...current.fieldMapping,
        gender: {
          ...current.fieldMapping.gender,
          options: { ...current.fieldMapping.gender?.options, [key]: value },
        },
      },
    }));
    setMessage("");
  }

  async function loadFeishuDepartments() {
    const requestId = ++directoryRequestRef.current;
    setDirectoryStatus("loading");
    setDirectoryMessage("");
    try {
      const departments = normalizeFeishuDepartmentDirectory(await client.listFeishuDepartments());
      if (requestId !== directoryRequestRef.current) return;
      setFeishuDepartments(departments);
      setDraft((current) => {
        const matched = autoMatchFeishuDepartmentMappings(current.departmentMappings, departments);
        return matched.matchedCount
          ? { ...current, enabled: false, departmentMappings: matched.mappings }
          : current;
      });
      setDirectoryStatus("ready");
      setDirectoryMessage(`已读取 ${departments.length} 个飞书部门，并完成同名自动匹配检查。`);
    } catch (error) {
      if (requestId !== directoryRequestRef.current) return;
      setDirectoryStatus("error");
      setDirectoryMessage(getFeishuDepartmentDirectoryErrorMessage(error));
    }
  }

  async function createDepartment() {
    const name = departmentName.trim();
    if (!name || busy) return;
    setStatus("creating_department"); setMessage("");
    try {
      const department = await client.createDepartment({ name, parent_id: null });
      const matched = autoMatchFeishuDepartmentMappings([{
        departmentId: department.id,
        departmentName: department.name || name,
        feishuDepartmentId: "",
      }], feishuDepartments);
      setDraft((current) => current.departmentMappings.some((item) => item.departmentId === department.id) ? current : ({
        ...current,
        enabled: false,
        departmentMappings: [...current.departmentMappings, matched.mappings[0]],
      }));
      setDepartmentName("");
      setAddingDepartment(false);
      setStatus("ready");
      setMessage(matched.matchedCount ? "部门已创建，并已匹配同名飞书部门。" : "部门已创建，请选择对应的飞书部门。");
      onNotify("部门已创建并加入飞书映射");
    } catch (error) {
      setStatus("error");
      setMessage(error?.code === "department_already_exists" ? "该部门已存在，请前往“组织与权限 → 部门”检查。" : error?.status === 403 ? "当前账号没有新增部门的权限。" : "部门创建失败，请稍后重试。");
    }
  }

  async function save(event) {
    event.preventDefault();
    if (busy || operationRef.current || needsRefresh || directoryStatus === "loading") return;
    operationRef.current = true;
    setStatus("saving"); setMessage("");
    try {
      const next = normalizeFeishuOnboardingApprovalConfig(await client.saveFeishuOnboardingApprovalConfig(
        buildFeishuOnboardingApprovalPayload(draft),
        { version: config.version },
      ));
      setConfig(next);
      setDraft((current) => approvalDraft(next, current.departmentMappings.map((item) => ({ id: item.departmentId, name: item.departmentName }))));
      setStatus("ready");
      setMessage(next.enabled
        ? "入职审批已启用。"
        : next.validationStatus === "valid"
          ? "配置已保存并通过校验，但尚未启用入职审批。"
          : "入职审批配置已保存，尚未校验、未启用。");
      onNotify("飞书入职审批配置已保存");
    } catch (error) {
      setMessage(getFeishuOnboardingApprovalErrorMessage(error));
      setStatus("error");
    } finally {
      operationRef.current = false;
    }
  }

  async function refreshVersion() {
    setStatus("refreshing");
    try {
      const next = normalizeFeishuOnboardingApprovalConfig(await client.getFeishuOnboardingApprovalConfig());
      setConfig(next);
      setDraft((current) => ({ ...current, enabled: current.enabled && next.enabled }));
      setNeedsRefresh(false);
      return true;
    } catch {
      setNeedsRefresh(true);
      return false;
    } finally {
      setStatus("error");
    }
  }

  async function validate() {
    if (validationDisabledReason || operationRef.current) return;
    operationRef.current = true;
    setMessage("");
    let validationStarted = false;
    try {
      let saved = config;
      if (dirty) {
        setStatus("saving");
        saved = normalizeFeishuOnboardingApprovalConfig(await client.saveFeishuOnboardingApprovalConfig(
          { ...buildFeishuOnboardingApprovalPayload(draft), enabled: false },
          { version: config.version },
        ));
        setConfig(saved);
        setDraft((current) => approvalDraft(saved, current.departmentMappings.map((item) => ({ id: item.departmentId, name: item.departmentName }))));
      }
      setStatus("validating");
      validationStarted = true;
      const next = normalizeFeishuOnboardingApprovalConfig(await client.validateFeishuOnboardingApprovalConfig({ version: saved.version }));
      setConfig(next);
      if (next.validationStatus !== "valid") setDraft((current) => ({ ...current, enabled: false }));
      setStatus(next.validationStatus === "valid" ? "ready" : "error");
      setMessage(next.validationStatus === "valid"
        ? next.enabled ? "审批模板校验通过，入职审批已启用。" : "审批模板校验通过，但入职审批尚未启用。"
        : getFeishuOnboardingApprovalErrorMessage(next.validationSafeErrorCode));
      if (next.validationStatus === "valid") onNotify("飞书入职审批模板校验通过");
    } catch (error) {
      setMessage(getFeishuOnboardingApprovalErrorMessage(error));
      if (validationStarted) await refreshVersion();
      setStatus("error");
    } finally {
      operationRef.current = false;
    }
  }

  if (status === "loading") return <div className="settings-section feishu-onboarding-loading" role="status"><RefreshCw className="spin" size={18} />正在加载入职审批配置…</div>;
  const validationLabel = needsNodeValidation && config.validationStatus === "valid" ? "待重新校验" : config.validationStatus === "valid"
    ? config.enabled ? "已启用" : "已校验 · 未启用"
    : config.validationStatus === "invalid" ? "校验未通过" : config.approvalCode ? "已保存 · 待校验" : "尚未配置";
  const feishuDepartmentById = new Map(feishuDepartments.map((item) => [item.openDepartmentId, item]));
  const feishuDepartmentNameCounts = new Map();
  for (const department of feishuDepartments) {
    feishuDepartmentNameCounts.set(department.name, (feishuDepartmentNameCounts.get(department.name) || 0) + 1);
  }
  const feishuDepartmentOptions = feishuDepartments.map((item) => {
    const duplicateName = feishuDepartmentNameCounts.get(item.name) > 1;
    return { ...item, label: duplicateName ? `${item.name}（${item.openDepartmentId}）` : item.name };
  });

  return <form className="settings-section feishu-onboarding-form" onSubmit={save}>
    <div className="settings-section-heading"><div><h2><ClipboardCheck size={20} />入职审批</h2><p>将候选人接受 Offer 后确认的资料映射到飞书“入职申请”。控件 ID 必须来自同一个审批模板，系统不会按中文标题自动猜测。</p></div><span className={`feishu-validation-status ${config.validationStatus}`}>{validationLabel}</span></div>
    {message && <div className={status === "error" ? "settings-error" : "profile-success"} role="status">{status === "error" ? <AlertTriangle size={17} /> : <CheckCircle2 size={17} />}{message}</div>}
    {needsNodeValidation && config.validationStatus === "valid" && <div className="settings-error" role="status"><AlertTriangle size={17} />现有配置尚未校验审批节点，请确认固定审批人并重新校验后再办理入职。</div>}
    <div className="feishu-onboarding-switch"><span><ShieldCheck size={18} /><span><strong>启用入职审批</strong><small>保存并校验模板后才可启用；修改映射或审批人后需重新校验。</small></span></span><label className="compact-switch"><input aria-label="启用飞书入职审批" type="checkbox" checked={draft.enabled} disabled={busy || (!draft.enabled && !canEnable)} onChange={(event) => { setDraft((current) => ({ ...current, enabled: event.target.checked })); setMessage(""); }} /><span aria-hidden="true" /></label></div>
    <label className="feishu-approval-code">Approval Code<input value={draft.approvalCode} disabled={busy} onChange={(event) => { setDraft((current) => ({ ...current, enabled: false, approvalCode: event.target.value })); setMessage(""); }} placeholder="例如：7C468A..." required /><small>从飞书审批管理后台的“入职申请”模板中获取，不是模板名称。</small></label>
    <section className="feishu-mapping-section" aria-labelledby="feishu-field-mapping-title">
      <header><div><h3 id="feishu-field-mapping-title">审批字段映射</h3><p>每个业务字段对应一个飞书控件 ID 和控件类型。</p></div></header>
      <div className="feishu-field-mapping" role="table" aria-label="飞书入职审批字段映射">
        <div className="feishu-mapping-head" role="row"><span>业务字段</span><span>飞书控件 ID</span><span>控件类型</span></div>
        {FEISHU_ONBOARDING_FIELDS.map((field) => <div className="feishu-mapping-row" role="row" key={field.key}>
          <strong role="cell">{field.label}</strong>
          <label role="cell"><span className="mobile-field-label">飞书控件 ID</span><input aria-label={`${field.label}控件 ID`} value={draft.fieldMapping[field.key]?.controlId || ""} disabled={busy} onChange={(event) => updateField(field.key, "controlId", event.target.value)} placeholder="control_id" required={draft.enabled} /></label>
          <label role="cell"><span className="mobile-field-label">控件类型</span><select aria-label={`${field.label}控件类型`} value={draft.fieldMapping[field.key]?.type || field.defaultType} disabled={busy} onChange={(event) => updateField(field.key, "type", event.target.value)}>{FEISHU_APPROVAL_CONTROL_TYPES.map((type) => <option key={type.value} value={type.value}>{type.label}</option>)}</select></label>
        </div>)}
      </div>
      <div className="feishu-gender-options">
        <strong>性别选项值</strong>
        <p>填写飞书“性别”单选控件中的实际选项值。</p>
        <div>{[["male", "男"], ["female", "女"]].map(([key, label]) => <label key={key}><span>{label}</span><input aria-label={`性别选项${label}`} value={draft.fieldMapping.gender?.options?.[key] || ""} disabled={busy} onChange={(event) => updateGenderOption(key, event.target.value)} required /></label>)}</div>
      </div>
    </section>
    <section className="feishu-mapping-section" aria-labelledby="feishu-approvers-title">
      <header className="feishu-mapping-section-header"><h3 id="feishu-approvers-title">固定审批人</h3><button className="button secondary" type="button" disabled={busy || approverStatus === "loading" || !draft.approvalCode.trim()} onClick={() => void loadApproverOptions(draft.approvalCode.trim())}><RefreshCw size={16} className={approverStatus === "loading" ? "spin" : ""} />读取审批节点和成员</button></header>
      {approverMessage && <div className="settings-error" role="alert"><AlertTriangle size={17} />{approverMessage}</div>}
      {approverDisabledReason && <p role="status">{approverDisabledReason}</p>}
      {approverStatus === "ready" && approverCode === draft.approvalCode.trim() && <div className="feishu-fixed-approvers">
        {!approverOptions.nodes.some((node) => node.need_approver) && <p>当前模板没有需要提交时指定审批人的节点，审批流程由飞书模板决定。</p>}
        {approverOptions.nodes.filter((node) => node.need_approver).map((node) => {
          const selected = draft.nodeApprovers.find((item) => item.nodeId === node.node_id)?.userIds || [];
          const missing = selected.filter((id) => !approverOptions.members.some((member) => member.user_id === id));
          return <fieldset key={node.node_id} disabled={busy} className="feishu-approver-node"><legend>{node.name || "审批"}{node.allow_multiple ? "（可多选）" : "（单选）"}</legend>
            {missing.length > 0 && <div className="settings-error" role="alert">已选成员已停用或不可见。<button className="button secondary" type="button" onClick={() => updateApprovers(node.node_id, selected.filter((id) => !missing.includes(id)))}>移除不可用成员</button></div>}
            {approverOptions.members.length === 0 && <p>暂无可选成员，请先添加组织成员并绑定飞书账号。</p>}
            <div className="feishu-approver-members">{approverOptions.members.map((member) => <label key={member.user_id}><input type={node.allow_multiple ? "checkbox" : "radio"} name={`approver-${node.node_id}`} checked={selected.includes(member.user_id)} disabled={!member.available} onChange={(event) => updateApprovers(node.node_id, node.allow_multiple ? event.target.checked ? [...selected, member.user_id] : selected.filter((id) => id !== member.user_id) : [member.user_id])} /><span>{member.display_name}<small>{!member.available ? "未绑定飞书账号，不可选" : ""}</small></span>{!member.available && selected.includes(member.user_id) && <button className="icon-button" type="button" aria-label={`移除${member.display_name}`} onClick={(event) => { event.preventDefault(); updateApprovers(node.node_id, selected.filter((id) => id !== member.user_id)); }}><X size={16} /></button>}</label>)}</div>
          </fieldset>;
        })}
        {draft.nodeApprovers.filter((selection) => !approverOptions.nodes.some((node) => node.need_approver && node.node_id === selection.nodeId)).map((selection) => <div className="settings-error" key={selection.nodeId}>已保存的审批节点已失效。<button className="button secondary" type="button" disabled={busy} onClick={() => updateApprovers(selection.nodeId, [])}>清除失效节点</button></div>)}
      </div>}
    </section>
    <section className="feishu-mapping-section" aria-labelledby="feishu-department-mapping-title">
      <header className="feishu-mapping-section-header"><div><h3 id="feishu-department-mapping-title">部门映射</h3><p>系统会读取飞书通讯录并自动匹配同名部门；名称不同或重名时，请从下拉框中确认。</p></div><div className="feishu-mapping-section-actions"><button className="button secondary" type="button" disabled={busy || directoryStatus === "loading"} onClick={() => void loadFeishuDepartments()}><RefreshCw className={directoryStatus === "loading" ? "spin" : ""} size={16} />刷新飞书部门</button><button className="button secondary" type="button" disabled={busy} aria-expanded={addingDepartment} onClick={() => { setAddingDepartment((current) => !current); setMessage(""); }}><Plus size={16} />新增本地部门</button></div></header>
      {directoryStatus === "loading" && <div className="feishu-directory-status loading" role="status" aria-live="polite"><RefreshCw className="spin" size={17} />正在读取飞书部门…</div>}
      {directoryMessage && <div className={`feishu-directory-status ${directoryStatus}`} role={directoryStatus === "error" ? "alert" : "status"}>{directoryStatus === "error" ? <AlertTriangle size={17} /> : <CheckCircle2 size={17} />}{directoryMessage}</div>}
      {addingDepartment && <div className="feishu-department-create"><label><span>部门名称</span><input aria-label="新增部门名称" autoFocus maxLength="200" value={departmentName} disabled={busy} onChange={(event) => setDepartmentName(event.target.value)} onKeyDown={(event) => { if (event.key === "Enter") { event.preventDefault(); void createDepartment(); } }} placeholder="例如：产品部" /></label><div><button className="icon-button" type="button" aria-label="取消新增部门" disabled={busy} onClick={() => { setAddingDepartment(false); setDepartmentName(""); }}><X size={18} /></button><button className="button primary" type="button" disabled={busy || !departmentName.trim()} onClick={() => void createDepartment()}>{status === "creating_department" ? "创建中…" : "确认新增"}</button></div></div>}
      {draft.departmentMappings.length ? <div className="feishu-department-mapping">
        {draft.departmentMappings.map((item) => {
          const currentIsMissing = item.feishuDepartmentId && !feishuDepartmentById.has(item.feishuDepartmentId);
          return <label key={item.departmentId}><span>{item.departmentName}</span><select aria-label={`${item.departmentName}对应飞书部门`} value={item.feishuDepartmentId} disabled={busy || directoryStatus !== "ready"} onChange={(event) => updateDepartment(item.departmentId, event.target.value)} required={draft.enabled}><option value="">请选择飞书部门</option>{currentIsMissing && <option value={item.feishuDepartmentId}>已保存部门（当前不可见）</option>}{feishuDepartmentOptions.map((department) => <option key={department.openDepartmentId} value={department.openDepartmentId}>{department.label}</option>)}</select></label>;
        })}
      </div> : <div className="feishu-mapping-empty"><AlertTriangle size={17} />组织内尚未创建部门，可点击“新增本地部门”直接创建；重命名、停用等完整管理请前往“组织与权限 → 部门”。</div>}
    </section>
    {config.validationStatus === "valid" && !needsNodeValidation && !draft.enabled && <p role="status">如需启用入职审批，请打开“启用入职审批”开关，再点击“保存入职审批配置”。</p>}
    <p id="feishu-validation-action-reason" role="status" aria-live="polite">{validationDisabledReason || (dirty ? "有未保存的修改；保存并校验会先保存当前配置，不会自动启用入职审批。" : "可校验已保存的审批模板。")}</p>
    {needsRefresh && <button className="button secondary" type="button" disabled={busy} onClick={() => void refreshVersion()}>重试刷新配置版本</button>}
    <div className="feishu-onboarding-actions"><span>{dirty ? "有未保存的修改" : config.validationStatus === "valid" ? `最近校验：${config.validatedAt ? new Date(config.validatedAt).toLocaleString("zh-CN", { hour12: false }) : "已通过"}` : "保存后请校验审批模板"}</span><button className="button secondary" type="button" aria-describedby="feishu-validation-action-reason" disabled={Boolean(validationDisabledReason)} onClick={() => void validate()}>{status === "validating" ? "校验中…" : dirty ? "保存并校验审批模板" : "校验审批模板"}</button><button className="button primary" type="submit" disabled={busy || needsRefresh || directoryStatus === "loading" || !dirty}>{status === "saving" ? "保存中…" : "保存入职审批配置"}</button></div>
  </form>;
}

export function FeishuIntegrationSettings({ onNotify = () => {}, client = apiClient }) {
  const [config, setConfig] = useState(normalizeFeishuConfig());
  const [draft, setDraft] = useState({ app_id: "", redirect_uri: "", calendar_id: "", enabled: false, ...emptySecrets });
  const [status, setStatus] = useState("loading");
  const [message, setMessage] = useState("");

  useEffect(() => {
    let active = true;
    client.getFeishuConfig().then((value) => {
      if (!active) return;
      const next = normalizeFeishuConfig(value);
      setConfig(next);
      setDraft({ app_id: next.appId, redirect_uri: next.redirectUri, calendar_id: next.calendarId, enabled: next.enabled, ...emptySecrets });
      setStatus("ready");
    }).catch(() => { if (active) { setMessage("飞书配置暂时无法读取。"); setStatus("error"); } });
    return () => { active = false; };
  }, [client]);

  function update(name, value) { setDraft((current) => ({ ...current, [name]: value })); }
  async function save(event) {
    event.preventDefault();
    setStatus("saving"); setMessage("");
    const body = buildFeishuConfigPayload(draft);
    try {
      const next = normalizeFeishuConfig(await client.saveFeishuConfig(body));
      setConfig(next); setDraft((current) => ({ ...current, ...emptySecrets })); setStatus("ready"); onNotify("飞书配置已保存");
    } catch (error) { setMessage(getFeishuConfigErrorMessage(error)); setStatus("error"); }
  }
  async function testConnection() {
    setStatus("testing"); setMessage("");
    try {
      const next = normalizeFeishuConfig(await client.testFeishuConnection());
      const succeeded = next.lastTestStatus === "succeeded";
      setConfig(next); setMessage(succeeded ? "凭据和机器人消息测试成功，测试提醒已发送到当前飞书账号。" : "测试提醒发送失败"); setStatus(succeeded ? "ready" : "error");
    }
    catch (error) { setMessage(getFeishuConnectionTestErrorMessage(error)); setStatus("error"); }
  }

  return <div className="feishu-settings-stack">
    {status === "loading" ? <div className="organization-state" role="status"><RefreshCw size={18} />正在加载飞书配置…</div> : <form className="settings-section password-settings-form feishu-integration-form" onSubmit={save}>
      <div className="settings-section-heading"><div><h2>飞书集成</h2><p>启用后可同步面试日历并向已绑定员工发送招聘待办提醒。飞书应用需开启机器人能力及“以应用身份发消息”权限；秘密字段只可替换，服务端不会回传明文。</p></div></div>
      {message && <div className={status === "error" ? "settings-error" : "profile-success"} role="status">{status === "error" ? <AlertTriangle size={17} /> : <CheckCircle2 size={17} />}{message}</div>}
      <label>App ID<input value={draft.app_id} onChange={(event) => update("app_id", event.target.value)} required /></label>
      <label>App Secret<input type="password" autoComplete="new-password" placeholder={config.appSecretConfigured ? "已配置；留空保持不变" : "请输入 App Secret"} value={draft.app_secret} onChange={(event) => update("app_secret", event.target.value)} /></label>
      <label>Redirect URI<input type="url" value={draft.redirect_uri} onChange={(event) => update("redirect_uri", event.target.value)} required /></label>
      <label>Calendar ID（可选）<input value={draft.calendar_id} onChange={(event) => update("calendar_id", event.target.value)} /></label>
      <label>Verification Token<input type="password" autoComplete="new-password" placeholder={config.verificationTokenConfigured ? "已配置；留空保持不变" : "请输入 Verification Token"} value={draft.verification_token} onChange={(event) => update("verification_token", event.target.value)} /></label>
      <label>Encrypt Key<input type="password" autoComplete="new-password" placeholder={config.encryptKeyConfigured ? "已配置；留空保持不变" : "请输入 Encrypt Key"} value={draft.encrypt_key} onChange={(event) => update("encrypt_key", event.target.value)} /></label>
      <div className="feishu-form-footer">
        <label className="feishu-enabled-control"><input type="checkbox" checked={draft.enabled} onChange={(event) => update("enabled", event.target.checked)} /><span>启用飞书集成</span></label>
        <div className="feishu-form-actions"><button className="button primary" type="submit" disabled={status === "saving" || status === "testing"}>{status === "saving" ? "保存中…" : "保存配置"}</button><button className="button secondary" type="button" disabled={!config.configured || status === "saving" || status === "testing"} onClick={testConnection}>{status === "testing" ? "发送中…" : "发送测试提醒"}</button></div>
      </div>
    </form>}
    <FeishuOnboardingApprovalSettings client={client} onNotify={onNotify} />
  </div>;
}
