import { useEffect, useState } from "react";
import { AlertTriangle, CheckCircle2, ClipboardCheck, Plus, RefreshCw, ShieldCheck, X } from "lucide-react";
import { apiClient } from "./apiClient.js";
import {
  buildFeishuConfigPayload,
  buildFeishuOnboardingApprovalPayload,
  FEISHU_APPROVAL_CONTROL_TYPES,
  FEISHU_ONBOARDING_FIELDS,
  getFeishuConfigErrorMessage,
  getFeishuConnectionTestErrorMessage,
  getFeishuOnboardingApprovalErrorMessage,
  normalizeFeishuConfig,
  normalizeFeishuOnboardingApprovalConfig,
} from "./feishuIntegration.js";

const emptySecrets = { app_secret: "", verification_token: "", encrypt_key: "" };

function approvalDraft(config, departments) {
  const mappingByDepartment = new Map(config.departmentMappings.map((item) => [item.departmentId, item.feishuDepartmentId]));
  return {
    enabled: config.enabled,
    approvalCode: config.approvalCode,
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

  useEffect(() => {
    let active = true;
    Promise.all([client.getFeishuOnboardingApprovalConfig(), client.listDepartments()]).then(([value, departments]) => {
      if (!active) return;
      const next = normalizeFeishuOnboardingApprovalConfig(value);
      setConfig(next);
      setDraft(approvalDraft(next, departments));
      setStatus("ready");
    }).catch((error) => {
      if (!active) return;
      setMessage(getFeishuOnboardingApprovalErrorMessage(error));
      setStatus("error");
    });
    return () => { active = false; };
  }, [client]);

  const dirty = JSON.stringify(buildFeishuOnboardingApprovalPayload(draft)) !== JSON.stringify(buildFeishuOnboardingApprovalPayload(config));
  const busy = ["saving", "validating", "creating_department"].includes(status);

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

  async function createDepartment() {
    const name = departmentName.trim();
    if (!name || busy) return;
    setStatus("creating_department"); setMessage("");
    try {
      const department = await client.createDepartment({ name, parent_id: null });
      setDraft((current) => current.departmentMappings.some((item) => item.departmentId === department.id) ? current : ({
        ...current,
        enabled: false,
        departmentMappings: [...current.departmentMappings, {
          departmentId: department.id,
          departmentName: department.name || name,
          feishuDepartmentId: "",
        }],
      }));
      setDepartmentName("");
      setAddingDepartment(false);
      setStatus("ready");
      setMessage("部门已创建，请继续填写对应的飞书部门 ID。");
      onNotify("部门已创建并加入飞书映射");
    } catch (error) {
      setStatus("error");
      setMessage(error?.code === "department_already_exists" ? "该部门已存在，请前往“组织与权限 → 部门”检查。" : error?.status === 403 ? "当前账号没有新增部门的权限。" : "部门创建失败，请稍后重试。");
    }
  }

  async function save(event) {
    event.preventDefault();
    if (busy) return;
    setStatus("saving"); setMessage("");
    try {
      const next = normalizeFeishuOnboardingApprovalConfig(await client.saveFeishuOnboardingApprovalConfig(
        buildFeishuOnboardingApprovalPayload(draft),
        { version: config.version },
      ));
      setConfig(next);
      setDraft((current) => approvalDraft(next, current.departmentMappings.map((item) => ({ id: item.departmentId, name: item.departmentName }))));
      setStatus("ready");
      setMessage("入职审批配置已保存，请继续校验审批模板。");
      onNotify("飞书入职审批配置已保存");
    } catch (error) {
      setMessage(getFeishuOnboardingApprovalErrorMessage(error));
      setStatus("error");
    }
  }

  async function validate() {
    if (busy || dirty) return;
    setStatus("validating"); setMessage("");
    try {
      const next = normalizeFeishuOnboardingApprovalConfig(await client.validateFeishuOnboardingApprovalConfig({ version: config.version }));
      setConfig(next);
      setStatus(next.validationStatus === "valid" ? "ready" : "error");
      setMessage(next.validationStatus === "valid"
        ? "审批模板校验通过，可以在候选人到岗时办理入职。"
        : getFeishuOnboardingApprovalErrorMessage(next.validationSafeErrorCode));
      if (next.validationStatus === "valid") onNotify("飞书入职审批模板校验通过");
    } catch (error) {
      setMessage(getFeishuOnboardingApprovalErrorMessage(error));
      setStatus("error");
    }
  }

  if (status === "loading") return <div className="settings-section feishu-onboarding-loading" role="status"><RefreshCw className="spin" size={18} />正在加载入职审批配置…</div>;
  const validationLabel = config.validationStatus === "valid" ? "已校验" : config.validationStatus === "invalid" ? "校验未通过" : "尚未校验";

  return <form className="settings-section feishu-onboarding-form" onSubmit={save}>
    <div className="settings-section-heading"><div><h2><ClipboardCheck size={20} />入职审批</h2><p>将候选人接受 Offer 后确认的资料映射到飞书“入职申请”。控件 ID 必须来自同一个审批模板，系统不会按中文标题自动猜测。</p></div><span className={`feishu-validation-status ${config.validationStatus}`}>{validationLabel}</span></div>
    {message && <div className={status === "error" ? "settings-error" : "profile-success"} role="status">{status === "error" ? <AlertTriangle size={17} /> : <CheckCircle2 size={17} />}{message}</div>}
    <div className="feishu-onboarding-switch"><span><ShieldCheck size={18} /><span><strong>启用入职审批</strong><small>保存并校验模板后才可启用；修改映射会自动关闭，避免按错误字段发起审批。</small></span></span><label className="compact-switch"><input aria-label="启用飞书入职审批" type="checkbox" checked={draft.enabled} disabled={busy || config.validationStatus !== "valid"} onChange={(event) => { setDraft((current) => ({ ...current, enabled: event.target.checked })); setMessage(""); }} /><span aria-hidden="true" /></label></div>
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
    <section className="feishu-mapping-section" aria-labelledby="feishu-department-mapping-title">
      <header className="feishu-mapping-section-header"><div><h3 id="feishu-department-mapping-title">部门映射</h3><p>飞书部门 ID 必须对应实际接收新员工的部门；未映射的职位不能办理入职。</p></div><button className="button secondary" type="button" disabled={busy} aria-expanded={addingDepartment} onClick={() => { setAddingDepartment((current) => !current); setMessage(""); }}><Plus size={16} />新增部门</button></header>
      {addingDepartment && <div className="feishu-department-create"><label><span>部门名称</span><input aria-label="新增部门名称" autoFocus maxLength="200" value={departmentName} disabled={busy} onChange={(event) => setDepartmentName(event.target.value)} onKeyDown={(event) => { if (event.key === "Enter") { event.preventDefault(); void createDepartment(); } }} placeholder="例如：产品部" /></label><div><button className="icon-button" type="button" aria-label="取消新增部门" disabled={busy} onClick={() => { setAddingDepartment(false); setDepartmentName(""); }}><X size={18} /></button><button className="button primary" type="button" disabled={busy || !departmentName.trim()} onClick={() => void createDepartment()}>{status === "creating_department" ? "创建中…" : "确认新增"}</button></div></div>}
      {draft.departmentMappings.length ? <div className="feishu-department-mapping">
        {draft.departmentMappings.map((item) => <label key={item.departmentId}><span>{item.departmentName}</span><input aria-label={`${item.departmentName}飞书部门 ID`} value={item.feishuDepartmentId} disabled={busy} onChange={(event) => updateDepartment(item.departmentId, event.target.value)} placeholder="od-xxxxxxxx" required={draft.enabled} /></label>)}
      </div> : <div className="feishu-mapping-empty"><AlertTriangle size={17} />组织内尚未创建部门，可点击“新增部门”直接创建；重命名、停用等完整管理请前往“组织与权限 → 部门”。</div>}
    </section>
    <div className="feishu-onboarding-actions"><span>{dirty ? "有未保存的修改" : config.validationStatus === "valid" ? `最近校验：${config.validatedAt ? new Date(config.validatedAt).toLocaleString("zh-CN", { hour12: false }) : "已通过"}` : "保存后请校验审批模板"}</span><button className="button secondary" type="button" disabled={busy || dirty || !config.approvalCode} onClick={() => void validate()}>{status === "validating" ? "校验中…" : "校验审批模板"}</button><button className="button primary" type="submit" disabled={busy || !dirty}>{status === "saving" ? "保存中…" : "保存入职审批配置"}</button></div>
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
