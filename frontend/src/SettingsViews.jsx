import { useEffect, useMemo, useRef, useState, useSyncExternalStore } from "react";
import { AlertTriangle, ArrowDown, ArrowUp, Bot, CalendarDays, CheckCircle2, ChevronDown, Copy, Database, FileClock, KeyRound, LockKeyhole, Plus, RefreshCw, Search, ShieldCheck, SlidersHorizontal, Trash2, Users, X } from "lucide-react";
import { canEditAiSettings, canEditOrganizationSettings, canEditRetentionSettings, canViewAuditSettings, canViewDeletionApprovalQueue, canViewRetentionSettings, getAllowedSettingsSections } from "./roleCapabilities.js";
import { createLlmSettingsController, getTestDisabledReason, releaseLlmSettingsSubscription } from "./llmSettings.js";
import { createOcrSettingsController, getOcrTestDisabledReason, releaseOcrSettingsSubscription } from "./ocrSettings.js";
import { createGovernanceSettingsController, releaseGovernanceSettingsSubscription } from "./governanceSettings.js";
import { getInviteRoleOptions, organizationSettingsController } from "./organizationSettings.js";
import { FeishuIntegrationSettings } from "./FeishuIntegrationSettings.jsx";
import { PagePrimaryAction } from "./PagePrimaryAction.jsx";
import { workflowTemplateController } from "./workflowTemplateController.js";
import "./product-theme-admin.css";

const settingsSections = [
  ["组织与权限", Users],
  ["流程与评价模板", SlidersHorizontal],
  ["AI 设置", Bot],
  ["飞书集成", CalendarDays],
  ["审计与数据治理", FileClock],
];
const settingsDefaultTabs = { "组织与权限": "成员", "流程与评价模板": "招聘流程" };
function RoleSwitch({ value, onChange }) {
  if (!onChange || value !== "招聘管理员") return null;
  return <div className="role-switch" aria-label="当前角色">{["招聘管理员", "HR", "面试官"].map((role) => <button type="button" key={role} className={value === role ? "active" : ""} onClick={() => onChange(role)}>{role}</button>)}</div>;
}

export function createDialogFocusManager({ dialog, restoreTarget, documentRef, isBusy, onClose }) {
  const focusable = () => Array.from(dialog.querySelectorAll("button, [href], input, select, textarea, [tabindex]"))
    .filter((element) => !element.disabled && element.getAttribute("aria-hidden") !== "true" && element.getAttribute("tabindex") !== "-1");
  return {
    focusInitial() {
      const initial = dialog.querySelector("[data-dialog-initial-focus]") || focusable()[0];
      initial?.focus();
    },
    handleKeyDown(event) {
      if (event.key === "Escape") {
        event.preventDefault();
        if (!isBusy()) onClose();
        return;
      }
      if (event.key !== "Tab") return;
      const elements = focusable();
      if (elements.length === 0) {
        event.preventDefault();
        return;
      }
      const first = elements[0];
      const last = elements[elements.length - 1];
      const active = documentRef.activeElement;
      if (event.shiftKey && (!dialog.contains(active) || active === first)) {
        event.preventDefault();
        last.focus();
      } else if (!event.shiftKey && (!dialog.contains(active) || active === last)) {
        event.preventDefault();
        first.focus();
      }
    },
    restoreFocus() {
      if (restoreTarget?.isConnected !== false) restoreTarget?.focus?.();
    },
  };
}

function DangerDialog({ title, description, impact, confirmText, confirmDisabled = false, surfaceClassName = "", onCancel, onConfirm }) {
  const dialogRef = useRef(null);
  const focusManagerRef = useRef(null);
  const restoreTargetRef = useRef(typeof document === "undefined" ? null : document.activeElement);
  const busyRef = useRef(confirmDisabled);
  const cancelRef = useRef(onCancel);
  const resolvedSurfaceClassName = surfaceClassName || (title === "确认批准删除请求" ? "deletion-approval-dialog" : "");
  busyRef.current = confirmDisabled;
  cancelRef.current = onCancel;
  useEffect(() => {
    if (!dialogRef.current || typeof document === "undefined") return undefined;
    const manager = createDialogFocusManager({
      dialog: dialogRef.current,
      restoreTarget: restoreTargetRef.current,
      documentRef: document,
      isBusy: () => busyRef.current,
      onClose: () => cancelRef.current(),
    });
    focusManagerRef.current = manager;
    manager.focusInitial();
    return () => {
      focusManagerRef.current = null;
      manager.restoreFocus();
    };
  }, []);
  function handleDialogKeyDown(event) { focusManagerRef.current?.handleKeyDown(event); }
  return <div className="ux07-dialog-backdrop"><section ref={dialogRef} className={`ux07-dialog${resolvedSurfaceClassName ? ` ${resolvedSurfaceClassName}` : ""}`} role="dialog" aria-modal="true" aria-label={title} onKeyDown={handleDialogKeyDown}><header><div><h3>{title}</h3><p>{description}</p></div><button className="icon-button" type="button" aria-label="关闭" disabled={confirmDisabled} onClick={onCancel}><X size={19} /></button></header><div className="ux07-danger-impact"><AlertTriangle size={22} /><span>{impact}</span></div><footer><button className="button secondary" type="button" data-dialog-initial-focus disabled={confirmDisabled} onClick={onCancel}>取消</button><button className="button danger" type="button" disabled={confirmDisabled} onClick={onConfirm}>{confirmText}</button></footer></section></div>;
}

function PermissionNotice({ children }) {
  return <div className="settings-permission-notice"><LockKeyhole size={18} /><span>{children}</span></div>;
}

function OrganizationDrawer({ title, description, busy, onClose, children, footer }) {
  const drawerRef = useRef(null);
  const restoreTargetRef = useRef(typeof document === "undefined" ? null : document.activeElement);
  const busyRef = useRef(busy);
  const closeRef = useRef(onClose);
  busyRef.current = busy;
  closeRef.current = onClose;
  useEffect(() => {
    if (!drawerRef.current || typeof document === "undefined") return undefined;
    const manager = createDialogFocusManager({ dialog: drawerRef.current, restoreTarget: restoreTargetRef.current, documentRef: document, isBusy: () => busyRef.current, onClose: () => closeRef.current() });
    manager.focusInitial();
    const handleKeyDown = (event) => manager.handleKeyDown(event);
    document.addEventListener("keydown", handleKeyDown);
    return () => { document.removeEventListener("keydown", handleKeyDown); manager.restoreFocus(); };
  }, []);
  return <aside ref={drawerRef} className="settings-drawer organization-drawer" role="dialog" aria-modal="true" aria-label={title}><header><div><h2>{title}</h2><p>{description || (title === "邀请成员" ? "发送一次性激活邀请，成员状态将显示为待激活。" : "创建一级部门供成员和职位归属使用。")}</p></div><button className="icon-button" type="button" aria-label="关闭" disabled={busy} onClick={onClose}><X size={20} /></button></header><div className="settings-drawer-body">{children}</div><footer>{footer}</footer></aside>;
}

function OrganizationSettings({
  role,
  onNotify,
  pageActionHost,
  activeTab = "成员",
  onTabChange = () => {},
  controller = organizationSettingsController,
}) {
  const editable = canEditOrganizationSettings(role);
  const state = useSyncExternalStore(
    controller.subscribe,
    controller.getSnapshot,
    controller.getSnapshot,
  );
  const tab = activeTab === "部门" ? "部门" : "成员";
  const [query, setQuery] = useState("");
  const [drawer, setDrawer] = useState(null);
  const [invite, setInvite] = useState({
    displayName: "",
    email: "",
    departmentId: "",
    role: getInviteRoleOptions(role)[0]?.value || "",
  });
  const [departmentName, setDepartmentName] = useState("");
  const [copied, setCopied] = useState(false);
  const inviteRoles = getInviteRoleOptions(role);
  const activeDepartments = state.departments.filter((department) => department.status === "active");
  const busy = state.actionStatus === "saving";
  const invitationLink =
    state.invitation && typeof window !== "undefined"
      ? `${window.location.origin}${window.location.pathname}#invite=${encodeURIComponent(state.invitation.token)}`
      : "";

  useEffect(() => {
    void controller.load();
  }, [controller]);
  if (role === "面试官")
    return (
      <section className="settings-denied">
        <LockKeyhole size={31} />
        <h3>无组织权限</h3>
        <p>面试官不能查看公司成员或部门。</p>
      </section>
    );
  const visibleUsers = state.users.filter((user) =>
    `${user.name}${user.email}${user.department}${user.role}`
      .toLowerCase()
      .includes(query.toLowerCase()),
  );
  const validInvite = Boolean(
    invite.displayName.trim() &&
    /^\S+@\S+\.\S+$/.test(invite.email.trim()) &&
    invite.departmentId &&
    invite.role,
  );
  const closeDrawer = () => {
    setDrawer(null);
    setCopied(false);
    controller.dismissInvitation();
    controller.clearDepartment();
  };
  async function submitInvite() {
    if (!validInvite || busy) return;
    try {
      await controller.inviteMember(invite);
      onNotify("邀请已发送，链接仅显示一次");
    } catch {
      /* safe controller message is rendered */
    }
  }
  async function submitDepartment() {
    if (!departmentName.trim() || busy) return;
    try {
      await controller.addDepartment(departmentName);
      setDrawer(null);
      setDepartmentName("");
      onNotify("部门已创建");
    } catch {
      /* safe controller message is rendered */
    }
  }
  async function openDepartment(department) {
    setDrawer("department-detail");
    setDepartmentName(department.name);
    try {
      await controller.loadDepartment(department.id);
    } catch {
      /* safe controller message is rendered */
    }
  }
  async function updateDepartment(changes, successMessage) {
    const detail = state.departmentDetail;
    if (!detail || busy) return;
    try {
      const updated = await controller.updateDepartment(detail.id, changes);
      setDepartmentName(updated.name);
      onNotify(successMessage);
    } catch {
      /* safe controller message is rendered */
    }
  }
  async function copyInvitation() {
    try {
      await navigator.clipboard.writeText(invitationLink);
      setCopied(true);
    } catch {
      setCopied(false);
    }
  }
  function openCreateDrawer() {
    if (tab === "部门") {
      setDrawer("department-create");
      return;
    }
    controller.dismissInvitation();
    setInvite({
      displayName: "",
      email: "",
      departmentId: activeDepartments[0]?.id || "",
      role: inviteRoles[0]?.value || "",
    });
    setDrawer("invite");
  }
  return (
    <div className="settings-section organization-settings">
      <PagePrimaryAction host={pageActionHost}>
        {editable && (
          <button className="button primary" type="button" onClick={openCreateDrawer}>
            <Plus size={16} />
            {tab === "部门" ? "新增部门" : "邀请成员"}
          </button>
        )}
      </PagePrimaryAction>
      <div className="settings-section-heading">
        <div>
          <h2>组织与权限</h2>
          <p>从服务端管理成员邀请和部门归属。</p>
        </div>
      </div>
      {!editable && (
        <PermissionNotice>
          当前为只读模式，邀请成员和新增部门由管理员完成。
        </PermissionNotice>
      )}
      <div
        className="settings-tabs organization-tabs"
        role="tablist"
        aria-label="组织与权限"
      >
        <button
          type="button"
          role="tab"
          aria-selected={tab === "成员"}
          className={tab === "成员" ? "active" : ""}
          onClick={() => onTabChange("成员")}
        >
          成员
        </button>
        <button
          type="button"
          role="tab"
          aria-selected={tab === "部门"}
          className={tab === "部门" ? "active" : ""}
          onClick={() => onTabChange("部门")}
        >
          部门
        </button>
      </div>
      {state.status === "loading" && (
        <div className="organization-state" role="status">
          <RefreshCw size={18} />
          正在加载组织信息…
        </div>
      )}
      {state.status === "error" && (
        <div className="organization-state error" role="alert">
          <AlertTriangle size={18} />
          <span>{state.error}</span>
          <button
            className="button secondary"
            type="button"
            onClick={() => controller.load()}
          >
            重试
          </button>
        </div>
      )}
      {state.status === "ready" && tab === "成员" && (
        <>
          <div className="settings-toolbar">
            <label className="settings-search">
              <Search size={16} />
              <input
                aria-label="搜索成员"
                value={query}
                onChange={(event) => setQuery(event.target.value)}
                placeholder="搜索姓名、邮箱、部门或角色"
              />
            </label>
          </div>
          <div className="settings-table users-table">
            <div className="settings-table-head">
              <span>成员</span>
              <span>部门</span>
              <span>角色</span>
              <span>状态</span>
            </div>
            {visibleUsers.map((user) => (
              <div className="settings-table-row" key={user.id}>
                <span>
                  <strong>{user.name}</strong>
                  <small>{user.email}</small>
                </span>
                <span>{user.department}</span>
                <span>{user.role}</span>
                <span
                  className={
                    user.status === "启用" ? "status-ok" : "status-muted"
                  }
                >
                  {user.status}
                </span>
              </div>
            ))}
            {visibleUsers.length === 0 && (
              <div className="organization-empty">暂无符合条件的成员</div>
            )}
          </div>
        </>
      )}
      {state.status === "ready" && tab === "部门" && (
        <div className="department-list">
          {state.departments.map((department) => (
            <button type="button" key={department.id} onClick={() => void openDepartment(department)}>
              <span className="department-name"><strong>{department.name}</strong><small className={department.status === "active" ? "status-ok" : "status-muted"}>{department.status === "active" ? "启用" : "已停用"}</small></span>
              <span>
                {department.memberCount} 名成员 · {department.jobCount} 个职位
              </span>
            </button>
          ))}
          {state.departments.length === 0 && (
            <div className="organization-empty">暂无部门</div>
          )}
        </div>
      )}
      {drawer === "invite" && (
        <OrganizationDrawer
          title="邀请成员"
          busy={busy}
          onClose={closeDrawer}
          footer={
            <>
              <button
                className="button secondary"
                type="button"
                disabled={busy}
                onClick={closeDrawer}
              >
                {state.invitation ? "完成" : "取消"}
              </button>
              {!state.invitation && (
                <button
                  className="button primary"
                  type="button"
                  disabled={!validInvite || busy}
                  onClick={submitInvite}
                >
                  {busy ? "正在发送…" : "发送邀请"}
                </button>
              )}
            </>
          }
        >
          {state.actionError && (
            <div className="settings-error" role="alert">
              <AlertTriangle size={17} />
              {state.actionError}
            </div>
          )}
          {state.invitation ? (
            <div className="invitation-result">
              <CheckCircle2 size={24} />
              <h3>邀请已创建</h3>
              <p>
                该链接仅显示一次，请立即安全发送给成员。邀请在 48 小时后失效。
              </p>
              <label>
                邀请链接
                <div>
                  <input
                    aria-label="邀请链接"
                    readOnly
                    value={invitationLink}
                  />
                  <button
                    className="button secondary"
                    type="button"
                    onClick={copyInvitation}
                  >
                    <Copy size={16} />
                    {copied ? "已复制" : "复制"}
                  </button>
                </div>
              </label>
              <small>
                有效期至{" "}
                {state.invitation.expiresAt
                  ? new Date(state.invitation.expiresAt).toLocaleString(
                      "zh-CN",
                      { hour12: false },
                    )
                  : "48 小时后"}
              </small>
            </div>
          ) : (
            <>
              <label>
                姓名
                <input
                  data-dialog-initial-focus
                  value={invite.displayName}
                  onChange={(event) =>
                    setInvite({ ...invite, displayName: event.target.value })
                  }
                  autoComplete="name"
                />
              </label>
              <label>
                工作邮箱
                <input
                  type="email"
                  value={invite.email}
                  onChange={(event) =>
                    setInvite({ ...invite, email: event.target.value })
                  }
                  autoComplete="email"
                />
              </label>
              <label>
                部门
                <select
                  value={invite.departmentId}
                  onChange={(event) =>
                    setInvite({ ...invite, departmentId: event.target.value })
                  }
                >
                  <option value="">请选择部门</option>
                  {activeDepartments.map((department) => (
                    <option key={department.id} value={department.id}>
                      {department.name}
                    </option>
                  ))}
                </select>
              </label>
              <label>
                角色
                <select
                  value={invite.role}
                  onChange={(event) =>
                    setInvite({ ...invite, role: event.target.value })
                  }
                >
                  {inviteRoles.map((item) => (
                    <option key={item.value} value={item.value}>
                      {item.label}
                    </option>
                  ))}
                </select>
              </label>
              <p className="form-help">成员接受邀请前状态为“待激活”。</p>
            </>
          )}
        </OrganizationDrawer>
      )}
      {drawer === "department-create" && (
        <OrganizationDrawer
          title="新增部门"
          busy={busy}
          onClose={closeDrawer}
          footer={
            <>
              <button
                className="button secondary"
                type="button"
                disabled={busy}
                onClick={closeDrawer}
              >
                取消
              </button>
              <button
                className="button primary"
                type="button"
                disabled={!departmentName.trim() || busy}
                onClick={submitDepartment}
              >
                {busy ? "正在创建…" : "创建部门"}
              </button>
            </>
          }
        >
          {state.actionError && (
            <div className="settings-error" role="alert">
              <AlertTriangle size={17} />
              {state.actionError}
            </div>
          )}
          <label>
            部门名称
            <input
              data-dialog-initial-focus
              value={departmentName}
              onChange={(event) => setDepartmentName(event.target.value)}
            />
          </label>
        </OrganizationDrawer>
      )}
      {drawer === "department-detail" && (
        <OrganizationDrawer
          title={state.departmentDetail?.name || departmentName || "部门详情"}
          description="查看部门成员和关联职位，并管理部门状态。"
          busy={busy}
          onClose={closeDrawer}
          footer={<button className="button secondary" type="button" disabled={busy} onClick={closeDrawer}>关闭</button>}
        >
          {state.departmentDetailStatus === "loading" && <div className="organization-state" role="status"><RefreshCw size={18} />正在加载部门详情…</div>}
          {state.departmentDetailStatus === "error" && <div className="settings-error" role="alert"><AlertTriangle size={17} />{state.actionError}</div>}
          {state.departmentDetailStatus === "ready" && state.departmentDetail && <>
            {state.actionError && <div className="settings-error" role="alert"><AlertTriangle size={17} />{state.actionError}</div>}
            <section className="department-detail-summary">
              <label>部门名称<input data-dialog-initial-focus value={departmentName} disabled={!editable || busy} onChange={(event) => setDepartmentName(event.target.value)} /></label>
              <div className="department-detail-actions">
                {editable && <button className="button primary" type="button" disabled={busy || !departmentName.trim() || departmentName.trim() === state.departmentDetail.name} onClick={() => void updateDepartment({ name: departmentName.trim() }, "部门名称已更新")}>{busy ? "保存中…" : "保存名称"}</button>}
                {editable && <button className="button secondary" type="button" disabled={busy} onClick={() => void updateDepartment({ status: state.departmentDetail.status === "active" ? "inactive" : "active" }, state.departmentDetail.status === "active" ? "部门已停用" : "部门已重新启用")}>{state.departmentDetail.status === "active" ? "停用部门" : "重新启用"}</button>}
              </div>
              <p>{state.departmentDetail.status === "active" ? "该部门可用于新职位和成员归属。" : "该部门已停用，历史成员和职位仍保留。"}</p>
            </section>
            <section className="department-detail-section"><h3>部门成员 <span>{state.departmentDetail.memberCount}</span></h3>{state.departmentDetail.members.length ? state.departmentDetail.members.map((member) => <div className="department-detail-row" key={member.id}><span><strong>{member.name}</strong><small>{member.roles.join("、") || "未分配角色"}</small></span><small>{member.status}</small></div>) : <p>暂无成员</p>}</section>
            <section className="department-detail-section"><h3>关联职位 <span>{state.departmentDetail.jobCount}</span></h3>{state.departmentDetail.jobs.length ? state.departmentDetail.jobs.map((job) => <div className="department-detail-row" key={job.id}><strong>{job.name}</strong><small>{job.status}</small></div>) : <p>暂无关联职位</p>}</section>
          </>}
        </OrganizationDrawer>
      )}
    </div>
  );
}

function TemplateSettings({ role, onNotify, activeTab, onTabChange = () => {} }) {
  const editable = role === "招聘管理员";
  const tabs = role === "面试官" ? ["面试评价模板"] : ["招聘流程", "淘汰原因", "面试评价模板"];
  const tab = tabs.includes(activeTab) ? activeTab : tabs[0];
  const [templates, setTemplates] = useState({ status: "idle", records: [] });
  const [selectedId, setSelectedId] = useState("");
  const [draft, setDraft] = useState({ name: "", rounds: [], status: "active" });
  const [creating, setCreating] = useState(false);
  const [newName, setNewName] = useState("");
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState("");
  const selected = templates.records.find((item) => item.id === selectedId) || null;

  async function loadTemplates() {
    setTemplates((current) => ({ ...current, status: "loading" }));
    setError("");
    try {
      const records = await workflowTemplateController.list();
      setTemplates({ status: "ready", records });
      setSelectedId((current) => records.some((item) => item.id === current) ? current : records[0]?.id || "");
    } catch {
      setTemplates({ status: "error", records: [] });
      setError("流程模板暂时无法加载，请重试。");
    }
  }

  useEffect(() => {
    if (tab === "招聘流程" && templates.status === "idle") void loadTemplates();
  }, [tab, templates.status]);

  useEffect(() => {
    if (selected) setDraft({ name: selected.name, rounds: [...selected.rounds], status: selected.status });
  }, [selectedId, selected?.version]);

  function validate(values) {
    if (!values.name.trim()) return "请填写模板名称。";
    if (!values.rounds.length) return "至少保留一个面试轮次。";
    if (values.rounds.some((item) => !item.trim())) return "请填写每个面试轮次的名称。";
    if (new Set(values.rounds.map((item) => item.trim())).size !== values.rounds.length) return "面试轮次名称不能重复。";
    return "";
  }

  async function saveTemplate() {
    const validation = validate(draft);
    if (validation || !selected) { setError(validation || "请选择要编辑的流程模板。"); return; }
    setSaving(true); setError("");
    try {
      const saved = await workflowTemplateController.update(selected, draft);
      setTemplates((current) => ({ ...current, records: current.records.map((item) => item.id === saved.id ? saved : item) }));
      onNotify("流程模板已保存，职位将使用最新面试轮次");
    } catch (requestError) {
      setError(requestError?.status === 409 ? "模板已被其他人更新，请刷新后再保存。" : "保存失败，当前修改已保留，请重试。");
    } finally { setSaving(false); }
  }

  async function createTemplate() {
    const values = { name: newName, rounds: ["一面"] };
    const validation = validate(values);
    if (validation) { setError(validation); return; }
    setSaving(true); setError("");
    try {
      const saved = await workflowTemplateController.create(values);
      setTemplates((current) => ({ status: "ready", records: [...current.records, saved] }));
      setSelectedId(saved.id); setCreating(false); setNewName("");
      onNotify("流程模板已创建，可继续添加面试轮次");
    } catch (requestError) {
      setError(requestError?.status === 409 ? "已存在同名模板，请更换名称。" : "创建失败，请检查网络后重试。");
    } finally { setSaving(false); }
  }

  function updateRound(index, value) {
    setDraft((current) => ({ ...current, rounds: current.rounds.map((item, itemIndex) => itemIndex === index ? value : item) }));
    setError("");
  }

  function moveRound(index, offset) {
    setDraft((current) => {
      const target = index + offset;
      if (target < 0 || target >= current.rounds.length) return current;
      const rounds = [...current.rounds];
      [rounds[index], rounds[target]] = [rounds[target], rounds[index]];
      return { ...current, rounds };
    });
  }

  const flowContent = <>
    <div className="settings-section-heading workflow-heading"><div><h2>招聘流程模板</h2><p>系统阶段自动流转；你只需要配置需要安排的面试轮次。</p></div>{editable && <button className="button primary" type="button" onClick={() => { setCreating(true); setError(""); }}><Plus size={16} />新建流程模板</button>}</div>
    {creating && <div className="workflow-create"><label>模板名称<input autoFocus value={newName} onChange={(event) => setNewName(event.target.value)} placeholder="例如：技术岗位三轮面试" /></label><div><button className="button secondary" type="button" disabled={saving} onClick={() => { setCreating(false); setNewName(""); }}>取消</button><button className="button primary" type="button" disabled={saving} onClick={() => void createTemplate()}>{saving ? "正在创建…" : "创建并编辑"}</button></div></div>}
    {!editable && <PermissionNotice>当前为只读模式，流程模板修改由招聘管理员完成。</PermissionNotice>}
    {templates.status === "loading" && <div className="organization-state" role="status"><RefreshCw size={18} />正在加载流程模板</div>}
    {error && <div className="settings-error" role="alert"><AlertTriangle size={18} /><span>{error}</span>{templates.status === "error" && <button type="button" onClick={() => void loadTemplates()}>重试</button>}</div>}
    {templates.status === "ready" && !templates.records.length && <div className="organization-empty"><strong>还没有招聘流程模板</strong><p>创建第一个模板后，职位即可选择并自动进入下一面。</p></div>}
    {templates.status === "ready" && templates.records.length > 0 && <section className="template-editor workflow-template-editor">
      <div className="workflow-template-toolbar"><label>选择模板<select value={selectedId} onChange={(event) => setSelectedId(event.target.value)}>{templates.records.map((item) => <option value={item.id} key={item.id}>{item.name}{item.status === "inactive" ? "（已停用）" : ""}</option>)}</select></label><span>{draft.rounds.length} 个面试轮次</span></div>
      <div className="workflow-system-flow"><strong>自动招聘主线</strong><p>新简历 → 用人经理评审 → 待安排面试 → <b>{draft.rounds.join(" → ") || "面试轮次"}</b> → 用人经理录用决策</p><small>评审通过、面试安排和反馈完成后，系统会自动更新候选人状态。</small></div>
      <header><div><label>模板名称<input disabled={!editable} value={draft.name} onChange={(event) => setDraft((current) => ({ ...current, name: event.target.value }))} /></label><span>面试完成后，若还有下一轮，候选人会自动进入“待安排面试”。</span></div>{editable && <button className="button primary" type="button" disabled={saving} onClick={() => void saveTemplate()}>{saving ? "正在保存…" : "保存模板"}</button>}</header>
      <div className="stage-editor workflow-round-editor">{draft.rounds.map((round, index) => <div key={`${index}-${round}`}><span>{index + 1}</span><input aria-label={`第 ${index + 1} 轮名称`} disabled={!editable} value={round} onChange={(event) => updateRound(index, event.target.value)} /><small>{index === draft.rounds.length - 1 ? "完成后进入用人经理录用决策" : `完成后等待安排${draft.rounds[index + 1] || "下一轮"}`}</small>{editable && <div className="workflow-round-actions"><button type="button" title="上移" aria-label={`上移${round}`} disabled={index === 0} onClick={() => moveRound(index, -1)}><ArrowUp size={16} /></button><button type="button" title="下移" aria-label={`下移${round}`} disabled={index === draft.rounds.length - 1} onClick={() => moveRound(index, 1)}><ArrowDown size={16} /></button><button type="button" title="删除" aria-label={`删除${round}`} disabled={draft.rounds.length === 1} onClick={() => setDraft((current) => ({ ...current, rounds: current.rounds.filter((_, itemIndex) => itemIndex !== index) }))}><Trash2 size={16} /></button></div>}</div>)}</div>
      {editable && <div className="workflow-add-round"><button className="button secondary" type="button" onClick={() => setDraft((current) => ({ ...current, rounds: [...current.rounds, `${["一", "二", "三", "四", "五"][current.rounds.length] || current.rounds.length + 1}面`] }))}><Plus size={16} />添加面试轮次</button><small>至少保留一轮；可拖动替代操作由上移、下移按钮提供。</small></div>}
    </section>}
  </>;

  return <div className="settings-section">{tab !== "招聘流程" && <div className="settings-section-heading"><div><h2>流程与评价模板</h2><p>管理招聘阶段、状态原因和结构化评价。</p></div></div>}<div className="settings-tabs">{tabs.map((item) => <button type="button" key={item} className={tab === item ? "active" : ""} onClick={() => onTabChange(item)}>{item}</button>)}</div>{tab === "招聘流程" && flowContent}{tab === "淘汰原因" && <div className="reason-list">{[["岗位要求不匹配", "必填", "启用"], ["候选人主动放弃", "必填", "启用"], ["薪资预期不匹配", "可选", "启用"], ["暂不招聘", "可选", "停用"]].map((item) => <div key={item[0]}><strong>{item[0]}</strong><span>{item[1]}</span><span>{item[2]}</span>{editable && <button type="button">编辑</button>}</div>)}</div>}{tab === "面试评价模板" && <div className="evaluation-template"><header><div><strong>技术岗位结构化评价</strong><span>适用：技术一面、技术二面</span></div>{editable && <button className="button secondary" type="button">编辑模板</button>}</header>{["专业能力", "问题解决", "沟通协作", "岗位匹配"].map((item) => <div key={item}><span>{item}</span><small>必填 · 需提升 / 一般 / 良好 / 优秀</small></div>)}<footer>结论：强烈推荐、推荐、保留、不推荐</footer></div>}</div>;
}

function formatTestTime(value) {
  if (!value) return "—";
  const date = new Date(value);
  return Number.isNaN(date.getTime()) ? "—" : date.toLocaleString("zh-CN", { hour12: false });
}

function ReadOnlyAiSettings({ config }) {
  const rows = [
    ["configured", "配置状态", (value) => value ? "已配置" : "未配置"],
    ["enabled", "启用状态", (value) => value ? "已启用" : "未启用"],
    ["provider_id", "Provider", (value) => value || "—"],
    ["model", "模型", (value) => value || "—"],
    ["version", "配置版本", (value) => String(value)],
    ["last_test_status", "最近测试", (value) => value === "succeeded" ? "成功" : value === "failed" ? "失败" : "尚未测试"],
    ["last_test_error_code", "测试错误码", (value) => value || "—"],
    ["last_test_latency_ms", "测试耗时", (value) => Number.isFinite(value) ? `${value} ms` : "—"],
    ["last_tested_at", "测试时间", formatTestTime],
  ].filter(([key]) => Object.prototype.hasOwnProperty.call(config, key));
  return <div className="llm-readonly"><PermissionNotice>招聘管理员仅可查看服务端授权返回的模型配置，修改由系统管理员完成。</PermissionNotice><dl>{rows.map(([key, label, format]) => <div key={key}><dt>{label}</dt><dd>{format(config[key])}</dd></div>)}</dl></div>;
}

function ProviderCreateDrawer({ busy, error, onClose, onSubmit }) {
  const [form, setForm] = useState({ display_name: "", provider_id: "", base_url: "https://", models: "" });
  const models = form.models.split(/[\n,，]/).map((value) => value.trim()).filter(Boolean);
  const valid = form.display_name.trim() && /^[a-z][a-z0-9_-]{1,63}$/.test(form.provider_id) && /^https:\/\//.test(form.base_url) && models.length > 0;
  async function submit(event) {
    event.preventDefault();
    if (!valid) return;
    if (await onSubmit({ ...form, display_name: form.display_name.trim(), base_url: form.base_url.trim(), models })) onClose();
  }
  return <><div className="provider-drawer-backdrop" onClick={busy ? undefined : onClose} /><aside className="settings-drawer provider-create-drawer" role="dialog" aria-modal="true" aria-label="添加 Provider"><header><div><h2>添加 Provider</h2><p>配置一个 OpenAI 兼容的模型服务。</p></div><button className="icon-button" type="button" aria-label="关闭" disabled={busy} onClick={onClose}><X size={19} /></button></header><form id="provider-create-form" className="settings-drawer-body" onSubmit={submit}>{error && <div className="settings-error" role="alert"><AlertTriangle size={17} />{error}</div>}<label>显示名称<input autoFocus value={form.display_name} onChange={(event) => setForm({ ...form, display_name: event.target.value })} placeholder="例如：智谱 BigModel" /></label><label>Provider 标识<input value={form.provider_id} onChange={(event) => setForm({ ...form, provider_id: event.target.value.trim().toLowerCase() })} placeholder="例如：bigmodel" /><small>仅支持小写字母、数字、下划线和连字符，保存后用于系统内部识别。</small></label><label>Base URL<input value={form.base_url} onChange={(event) => setForm({ ...form, base_url: event.target.value })} placeholder="https://example.com/v1" /><small>必须使用 HTTPS 和标准 443 端口，不能填写内网地址。</small></label><label>可用模型<textarea value={form.models} onChange={(event) => setForm({ ...form, models: event.target.value })} placeholder={"每行一个模型，例如：\nglm-5.2\nglm-4.5"} /><small>可使用换行或逗号分隔，模型名称必须与服务商控制台一致。</small></label></form><footer><button className="button secondary" type="button" disabled={busy} onClick={onClose}>取消</button><button className="button primary" type="submit" form="provider-create-form" disabled={!valid || busy}>{busy ? "添加中…" : "添加 Provider"}</button></footer></aside></>;
}

function LlmSettingsSection({ role, onNotify, onDirtyChange }) {
  const editable = canEditAiSettings(role);
  const controller = useMemo(() => createLlmSettingsController(), [role]);
  const [viewState, setViewState] = useState(() => controller.getState());
  const [addingProvider, setAddingProvider] = useState(false);
  useEffect(() => {
    const unsubscribe = controller.subscribe(setViewState);
    controller.load();
    return () => {
      releaseLlmSettingsSubscription(controller, unsubscribe);
      onDirtyChange?.(false);
    };
  }, [controller, onDirtyChange]);
  useEffect(() => { onDirtyChange?.(viewState.dirty); }, [viewState.dirty, onDirtyChange]);
  const { status, config, draft, replacingKey, replacementKey, dirty, error, message } = viewState;
  if ((status === "idle" || status === "loading") && !config) return <section className="ai-settings-block" aria-labelledby="llm-settings-title"><div className="ai-settings-block-heading"><div><h2 id="llm-settings-title">LLM 简历评估</h2><p>正在读取已保存的模型配置。</p></div></div><div className="llm-settings-loading" role="status"><RefreshCw size={18} />正在加载设置…</div></section>;
  if (!config) return <section className="ai-settings-block" aria-labelledby="llm-settings-title"><div className="ai-settings-block-heading"><div><h2 id="llm-settings-title">LLM 简历评估</h2><p>控制候选人文本是否发送到外部模型服务。</p></div></div><div className="llm-settings-load-error" role="alert"><AlertTriangle size={20} /><div><strong>LLM 设置加载失败</strong><p>{error}</p></div><button className="button secondary" type="button" onClick={() => controller.load()}>重试</button></div></section>;
  if (!editable) return <section className="ai-settings-block" aria-labelledby="llm-settings-title"><div className="ai-settings-block-heading"><div><h2 id="llm-settings-title">LLM 简历评估</h2><p>查看组织当前使用的简历评估模型配置。</p></div></div><ReadOnlyAiSettings config={config} /></section>;

  const providers = Object.keys(config.available_providers || {});
  const providerLabels = Object.fromEntries((config.provider_options || []).map((provider) => [provider.provider_id, provider.display_name]));
  const models = config.available_providers?.[draft.provider_id] || [];
  const testDisabledReason = getTestDisabledReason(viewState);
  const saveMissingKey = draft.enabled && config.key_configured !== true && !replacementKey;
  const saveDisabled = ["saving", "testing", "adding_provider"].includes(status) || !dirty || !draft.provider_id || !draft.model || saveMissingKey;
  const scopeIds = Array.isArray(config.allowed_job_ids) ? config.allowed_job_ids : [];
  const lastTestFailed = config.last_test_status === "failed";
  const lastTestSucceeded = config.last_test_status === "succeeded";
  async function save() { if (await controller.save()) onNotify("AI 设置已保存"); }
  async function testConnection() { if (await controller.testConnection()) onNotify("LLM 连接测试成功"); }
  async function createProvider(provider) {
    const succeeded = await controller.createProvider(provider);
    if (succeeded) onNotify("Provider 已添加");
    return succeeded;
  }
  return <section className="ai-settings-block" aria-labelledby="llm-settings-title">
    <div className="ai-settings-block-heading"><div><h2 id="llm-settings-title">LLM 简历评估</h2><p>配置简历筛选使用的模型服务和数据外发范围。</p></div><button className="button secondary" type="button" disabled={dirty || status === "adding_provider"} title={dirty ? "请先保存或取消当前修改" : undefined} onClick={() => setAddingProvider(true)}><Plus size={16} />添加 Provider</button></div>
    {error && <div className="settings-error" role="alert"><AlertTriangle size={17} />{error}</div>}
    {message && <div className="llm-settings-message" role="status">{message}</div>}
    <section className="ai-governance"><ShieldCheck size={20} /><div><strong>数据外发控制</strong><p>只有系统管理员添加的 Provider 才可使用；候选人文本仅在启用后发送到所选服务。</p></div><label className="llm-enabled-toggle"><input type="checkbox" checked={draft.enabled} onChange={(event) => controller.updateDraft({ enabled: event.target.checked })} />启用</label></section>
    {providers.length === 0 && <section className="provider-empty"><Bot size={24} /><div><strong>还没有可用的 Provider</strong><p>先添加模型服务，再配置模型和 API Key。</p></div><button className="button primary" type="button" onClick={() => setAddingProvider(true)}><Plus size={16} />添加 Provider</button></section>}
    <div className="settings-form llm-settings-form">
      <label>Provider<select value={draft.provider_id} onChange={(event) => { const provider_id = event.target.value; controller.updateDraft({ provider_id, model: config.available_providers?.[provider_id]?.[0] || "" }); }}><option value="">请选择 Provider</option>{providers.map((provider) => <option key={provider} value={provider}>{providerLabels[provider] ? `${providerLabels[provider]} (${provider})` : provider}</option>)}</select></label>
      <label>模型<select value={draft.model} disabled={!draft.provider_id || models.length === 0} onChange={(event) => controller.updateDraft({ model: event.target.value })}><option value="">请选择模型</option>{models.map((model) => <option key={model} value={model}>{model}</option>)}</select></label>
      <div className="llm-key-field"><span className="llm-field-label">API Key</span><div className="masked-key"><KeyRound size={16} aria-hidden="true" /><span>{config.key_configured ? "已安全配置" : "尚未配置"}</span><button type="button" onClick={() => replacingKey ? controller.cancelKeyReplacement() : controller.startKeyReplacement()}>{replacingKey ? "取消替换" : config.key_configured ? "替换" : "添加"}</button></div>{replacingKey && <label className="llm-replacement-key">新的 API Key<input type="password" autoComplete="new-password" value={replacementKey} onChange={(event) => controller.setReplacementKey(event.target.value)} placeholder="保存后不会再次显示" /></label>}{saveMissingKey && <small className="llm-field-hint">启用模型前必须添加并保存 API Key。</small>}</div>
      <div className="llm-scope-summary"><strong>允许使用的岗位</strong><p>{scopeIds.length === 0 ? "全部岗位（空列表表示不限制岗位）" : `已限制为 ${scopeIds.length} 个岗位`}</p>{scopeIds.length > 0 && <code>{scopeIds.join("、")}</code>}<small>岗位选择器尚未开放；保存时会原样保留当前岗位 ID。</small></div>
    </div>
    <section className={`llm-test-result ${lastTestSucceeded ? "success" : lastTestFailed ? "error" : "idle"}`} aria-live="polite"><div>{lastTestSucceeded ? <CheckCircle2 size={20} /> : lastTestFailed ? <AlertTriangle size={20} /> : <Bot size={20} />}<span><strong>{status === "testing" ? "正在测试已保存的配置" : lastTestSucceeded ? "最近一次连接成功" : lastTestFailed ? "最近一次连接失败" : "尚未测试已保存的配置"}</strong><small>{lastTestSucceeded ? `耗时 ${config.last_test_latency_ms ?? "—"} ms · ${formatTestTime(config.last_tested_at)}` : lastTestFailed ? `安全错误码：${config.last_test_error_code || "未知"} · ${formatTestTime(config.last_tested_at)}` : "测试只使用服务器中最后保存的 Provider、模型和 API Key。"}</small>{testDisabledReason && <small className="llm-test-explanation">{testDisabledReason}</small>}</span></div><button className="button secondary" type="button" disabled={Boolean(testDisabledReason)} onClick={testConnection}>{status === "testing" && <RefreshCw size={15} />}测试连接</button></section>
    <div className="ai-section-actions"><span>{status === "saving" ? "正在保存…" : status === "adding_provider" ? "正在添加 Provider…" : dirty ? "LLM 有尚未保存的修改" : "LLM 配置已保存"}</span>{dirty && <button className="button secondary" type="button" disabled={status === "saving" || status === "testing"} onClick={() => controller.discardDraft()}>取消修改</button>}<button className="button primary" type="button" disabled={saveDisabled} onClick={save}>{status === "saving" ? "保存中…" : "保存 LLM 设置"}</button></div>
    {addingProvider && <ProviderCreateDrawer busy={status === "adding_provider"} error={error} onClose={() => setAddingProvider(false)} onSubmit={createProvider} />}
  </section>;
}

function ReadOnlyOcrSettings({ config }) {
  const rows = [
    ["enabled", "启用状态", (value) => value ? "已启用" : "未启用"],
    ["provider_id", "Provider 标识", (value) => value || "—"],
    ["base_url", "Base URL", (value) => value || "—"],
    ["model", "模型 / 服务", (value) => value || "—"],
    ["key_configured", "API Key", (value) => value === true ? "已安全配置" : value === false ? "尚未配置" : "状态不可见"],
    ["last_test_status", "最近测试", (value) => value === "succeeded" ? "成功" : value === "failed" ? "失败" : "尚未测试"],
    ["last_test_error_code", "安全错误码", (value) => value || "—"],
    ["last_test_latency_ms", "测试耗时", (value) => Number.isFinite(value) ? `${value} ms` : "—"],
    ["last_tested_at", "测试时间", formatTestTime],
  ];
  return <div className="llm-readonly"><PermissionNotice>招聘管理员仅可查看安全配置状态，修改和密钥管理由系统管理员完成。</PermissionNotice><dl>{rows.map(([key, label, format]) => <div key={key}><dt>{label}</dt><dd>{format(config[key])}</dd></div>)}</dl></div>;
}

function OcrSettingsSection({ role, onNotify, onDirtyChange }) {
  const editable = canEditAiSettings(role);
  const controller = useMemo(() => createOcrSettingsController(), [role]);
  const [viewState, setViewState] = useState(() => controller.getState());
  useEffect(() => {
    const unsubscribe = controller.subscribe(setViewState);
    controller.load();
    return () => {
      releaseOcrSettingsSubscription(controller, unsubscribe);
      onDirtyChange?.(false);
    };
  }, [controller, onDirtyChange]);
  useEffect(() => { onDirtyChange?.(viewState.dirty); }, [viewState.dirty, onDirtyChange]);

  const { status, config, draft, replacingKey, replacementKey, dirty, error, message } = viewState;
  if ((status === "idle" || status === "loading") && !config) return <section className="ai-settings-block ocr-settings" aria-labelledby="ocr-settings-title"><div className="ai-settings-block-heading"><div><h2 id="ocr-settings-title">OCR 文档识别</h2><p>正在读取已保存的 OCR 配置。</p></div></div><div className="llm-settings-loading" role="status"><RefreshCw size={18} />正在加载设置…</div></section>;
  if (!config) return <section className="ai-settings-block ocr-settings" aria-labelledby="ocr-settings-title"><div className="ai-settings-block-heading"><div><h2 id="ocr-settings-title">OCR 文档识别</h2><p>仅在需要时识别无法直接提取文字的简历页面。</p></div></div><div className="llm-settings-load-error" role="alert"><AlertTriangle size={20} /><div><strong>OCR 设置加载失败</strong><p>{error}</p></div><button className="button secondary" type="button" onClick={() => controller.load()}>重试</button></div></section>;
  if (!editable) return <section className="ai-settings-block ocr-settings" aria-labelledby="ocr-settings-title"><div className="ai-settings-block-heading"><div><h2 id="ocr-settings-title">OCR 文档识别</h2><p>仅扫描件或低质量 PDF 的页面图像会发送到外部 OCR 服务。</p></div></div><ReadOnlyOcrSettings config={config} /></section>;

  const testDisabledReason = getOcrTestDisabledReason(viewState);
  const saveMissingKey = draft.enabled && config.key_configured !== true && !replacementKey;
  const requiredFieldsMissing = !draft.provider_id.trim() || !draft.base_url.trim() || !draft.model.trim();
  const saveDisabled = ["saving", "testing"].includes(status) || !dirty || requiredFieldsMissing || saveMissingKey;
  const lastTestFailed = config.last_test_status === "failed";
  const lastTestSucceeded = config.last_test_status === "succeeded";
  async function save() { if (await controller.save()) onNotify("OCR 设置已保存"); }
  async function testConnection() { if (await controller.testConnection()) onNotify("OCR 连接测试成功"); }

  return <section className="ai-settings-block ocr-settings" aria-labelledby="ocr-settings-title">
    <div className="ai-settings-block-heading"><div><h2 id="ocr-settings-title">OCR 文档识别</h2><p>仅扫描件或低质量 PDF 的页面图像会发送到外部 OCR 服务；可直接提取文字的简历不会外发页面图像。</p></div></div>
    {error && <div className="settings-error" role="alert"><AlertTriangle size={17} />{error}</div>}
    {message && <div className="llm-settings-message" role="status">{message}</div>}
    <section className="ai-governance"><ShieldCheck size={20} /><div><strong>页面图像外发控制</strong><p>启用后仍只处理扫描件或文字质量不足的 PDF 页面，不会把所有简历图片默认发送到外部服务。</p></div><label className="llm-enabled-toggle"><input type="checkbox" checked={draft.enabled} onChange={(event) => controller.updateDraft({ enabled: event.target.checked })} />启用</label></section>
    <div className="settings-form ocr-settings-form">
      <label>Provider 标识<input value={draft.provider_id} onChange={(event) => controller.updateDraft({ provider_id: event.target.value.toLowerCase() })} placeholder="例如：aliyun" autoComplete="off" /><small>仅支持小写字母、数字、下划线和连字符，输入的大写字母会自动转为小写。</small></label>
      <label>模型 / 服务<input value={draft.model} onChange={(event) => controller.updateDraft({ model: event.target.value })} placeholder="例如：ocr-v2" autoComplete="off" /></label>
      <label className="ocr-base-url">Base URL<input type="url" value={draft.base_url} onChange={(event) => controller.updateDraft({ base_url: event.target.value })} placeholder="https://ocr.example.com/v1" autoComplete="url" /></label>
      <div className="llm-key-field"><span className="llm-field-label">API Key</span><div className="masked-key"><KeyRound size={16} aria-hidden="true" /><span>{config.key_configured ? "已安全配置" : "尚未配置"}</span><button type="button" onClick={() => replacingKey ? controller.cancelKeyReplacement() : controller.startKeyReplacement()}>{replacingKey ? "取消替换" : config.key_configured ? "替换" : "添加"}</button></div>{replacingKey && <label className="llm-replacement-key">新的 API Key<input type="password" autoComplete="new-password" value={replacementKey} onChange={(event) => controller.setReplacementKey(event.target.value)} placeholder="保存后不会再次显示" /></label>}{saveMissingKey && <small className="llm-field-hint">启用 OCR 前必须添加并保存 API Key。</small>}</div>
    </div>
    <section className={`llm-test-result ${lastTestSucceeded ? "success" : lastTestFailed ? "error" : "idle"}`} aria-live="polite"><div>{lastTestSucceeded ? <CheckCircle2 size={20} /> : lastTestFailed ? <AlertTriangle size={20} /> : <Bot size={20} />}<span><strong>{status === "testing" ? "正在测试已保存的 OCR 配置" : lastTestSucceeded ? "最近一次连接成功" : lastTestFailed ? "最近一次连接失败" : "尚未测试已保存的配置"}</strong><small>{lastTestSucceeded ? `耗时 ${config.last_test_latency_ms ?? "—"} ms · ${formatTestTime(config.last_tested_at)}` : lastTestFailed ? `安全错误码：${config.last_test_error_code || "未知"} · ${formatTestTime(config.last_tested_at)}` : "测试只使用服务器中最后保存的 Provider、Base URL、模型和 API Key。"}</small>{testDisabledReason && <small className="llm-test-explanation">{testDisabledReason}</small>}</span></div><button className="button secondary" type="button" disabled={Boolean(testDisabledReason)} onClick={testConnection}>{status === "testing" && <RefreshCw size={15} />}测试连接</button></section>
    <div className="ai-section-actions"><span>{status === "saving" ? "正在保存…" : dirty ? "OCR 有尚未保存的修改" : "OCR 配置已保存"}</span>{dirty && <button className="button secondary" type="button" disabled={["saving", "testing"].includes(status)} onClick={() => controller.discardDraft()}>取消修改</button>}<button className="button primary" type="button" disabled={saveDisabled} onClick={save}>{status === "saving" ? "保存中…" : "保存 OCR 设置"}</button></div>
  </section>;
}

function AiSettings({ role, onNotify, onDirtyChange }) {
  const [llmDirty, setLlmDirty] = useState(false);
  const [ocrDirty, setOcrDirty] = useState(false);
  useEffect(() => { onDirtyChange?.(llmDirty || ocrDirty); }, [llmDirty, ocrDirty, onDirtyChange]);
  if (role === "面试官") return <section className="settings-denied"><LockKeyhole size={31} /><h3>无 AI 设置权限</h3><p>面试官不能查看 Provider、模型范围或密钥状态。</p></section>;
  return <div className="settings-section ai-settings"><div className="settings-section-heading ai-page-heading"><div><h2>AI 设置</h2><p>分别管理简历评估与文档识别服务；两个区域独立保存和测试。</p></div></div><LlmSettingsSection role={role} onNotify={onNotify} onDirtyChange={setLlmDirty} /><OcrSettingsSection role={role} onNotify={onNotify} onDirtyChange={setOcrDirty} /></div>;
}

function formatAuditTime(value) {
  if (!value) return "—";
  const date = new Date(value);
  return Number.isNaN(date.getTime()) ? "—" : date.toLocaleString("zh-CN", { hour12: false });
}

function auditResourceLabel(resource) {
  if (!resource) return "授权范围外资源";
  return resource.label ? `${resource.label} · ${resource.type} · ${resource.id}` : `${resource.type} · ${resource.id}`;
}

function auditOutcomeLabel(outcome) {
  if (outcome === "success") return "成功";
  if (outcome === "denied") return "已拒绝";
  if (outcome === "failure") return "失败";
  return "未知";
}

function auditFilterDate(value) {
  if (!value) return "";
  const date = new Date(value);
  return Number.isNaN(date.getTime()) ? "" : date.toISOString();
}

const deletionCountLabels = [
  ["contacts", "联系方式"], ["resumes", "简历记录"], ["applications", "职位申请"], ["screeningRecords", "筛选记录"], ["interviews", "面试"], ["feedbackRecords", "反馈记录"], ["talentMemberships", "人才库关系"], ["resumeObjects", "简历文件"], ["temporaryExports", "临时导出"],
];

function deletionStatusLabel(status) {
  return ({ requested: "待系统管理员审批", approved: "已批准，等待执行", executing: "正在删除", completed: "已完成", failed: "执行失败" })[status] || "未知";
}

function GovernanceState({ title, message, onRetry, denied = false }) {
  return <div className={`governance-state${denied ? " denied" : ""}`} role={denied ? undefined : "alert"}>{denied ? <LockKeyhole size={22} /> : <AlertTriangle size={22} />}<div><strong>{title}</strong><p>{message}</p></div>{onRetry && <button className="button secondary" type="button" onClick={onRetry}>重试</button>}</div>;
}

function AuditSettings({ role, onNotify }) {
  const canViewAudit = canViewAuditSettings(role);
  const canViewRetention = canViewRetentionSettings(role);
  const editable = canEditRetentionSettings(role);
  const controller = useMemo(() => createGovernanceSettingsController(), [role]);
  const [viewState, setViewState] = useState(() => controller.getState());
  const [filters, setFilters] = useState({ from: "", to: "", eventType: "", outcome: "" });
  const [selected, setSelected] = useState(null);
  const [deletionFilter, setDeletionFilter] = useState("");
  const [approvalOpen, setApprovalOpen] = useState(false);
  const canApproveDeletion = canViewDeletionApprovalQueue(role);

  useEffect(() => {
    const unsubscribe = controller.subscribe(setViewState);
    if (canViewAudit) controller.loadAudit();
    if (canViewAudit) controller.loadDeletionRequests();
    if (canViewRetention) controller.loadRetention();
    return () => releaseGovernanceSettingsSubscription(controller, unsubscribe);
  }, [canViewAudit, canViewRetention, controller]);

  if (!canViewAudit || !canViewRetention) return <section className="settings-denied"><LockKeyhole size={31} /><h3>无审计与治理权限</h3><p>当前角色不能查看系统访问记录或候选人保留策略。</p></section>;

  const { audit, retention, deletionQueue } = viewState;
  const selectedStillVisible = selected && audit.rows.some((row) => row.id === selected.id);
  const activeSelected = selectedStillVisible ? selected : null;
  const retentionBusy = retention.status === "loading" || retention.status === "saving" || retention.status === "previewing";

  function applyFilters(event) {
    event.preventDefault();
    setSelected(null);
    controller.loadAudit({
      from: auditFilterDate(filters.from),
      to: auditFilterDate(filters.to),
      eventType: filters.eventType.trim(),
      outcome: filters.outcome,
    });
  }

  function changeRetention(key, value) {
    controller.updateRetentionDraft({ [key]: Number(value) });
  }

  async function saveRetention() {
    if (await controller.saveRetention()) onNotify("数据保留策略已保存");
  }

  async function confirmRetention() {
    if (await controller.confirmRetentionSave()) onNotify("数据保留策略已保存并记录审计");
  }

  async function confirmDeletionApproval() {
    const approved = await controller.approveDeletionRequest({
      terminateActiveApplications: deletionQueue.selected?.activeApplicationCount > 0,
    });
    setApprovalOpen(false);
    if (approved) {
      await controller.loadDeletionRequests(deletionQueue.statusFilter);
      onNotify("删除请求已批准并进入处理队列");
    }
  }

  return <div className="settings-section governance-settings"><div className="settings-section-heading"><div><h2>审计与数据治理</h2><p>查询服务端授权的关键操作并管理候选人数据保留周期。</p></div></div>{!editable && <PermissionNotice>当前为只读模式；审计范围由服务端授权，保留策略仅系统管理员可修改。</PermissionNotice>}<form className="audit-toolbar governance-filters" onSubmit={applyFilters}><label>开始时间<input type="datetime-local" value={filters.from} onChange={(event) => setFilters({ ...filters, from: event.target.value })} /></label><label>结束时间<input type="datetime-local" value={filters.to} onChange={(event) => setFilters({ ...filters, to: event.target.value })} /></label><label>事件类型<input value={filters.eventType} onChange={(event) => setFilters({ ...filters, eventType: event.target.value })} placeholder="如 candidate.created" /></label><label>结果<select value={filters.outcome} onChange={(event) => setFilters({ ...filters, outcome: event.target.value })}><option value="">全部结果</option><option value="success">成功</option><option value="denied">已拒绝</option><option value="failure">失败</option></select></label><button className="button secondary" type="submit">查询</button><span>{audit.rows.length} 条已加载记录</span></form>{audit.status === "loading" && <div className="governance-loading" role="status"><RefreshCw size={18} />正在加载授权审计记录…</div>}{audit.status === "denied" && <GovernanceState denied title="无审计记录权限" message="服务端未授权当前账号查看审计记录。" />}{audit.status === "error" && <GovernanceState title="审计记录加载失败" message={audit.error} onRetry={() => controller.loadAudit(audit.filters)} />}{audit.status === "empty" && <div className="governance-empty" role="status"><FileClock size={22} /><strong>没有符合条件的审计记录</strong><span>请调整筛选条件后重试。</span></div>}{audit.rows.length > 0 && <><div className="settings-table audit-table"><div className="settings-table-head"><span>时间</span><span>操作者</span><span>事件</span><span>资源</span><span>结果</span><span /></div>{audit.rows.map((row) => <button type="button" className="settings-table-row" key={row.id} onClick={() => setSelected(row)}><span>{formatAuditTime(row.createdAt)}</span><span>{row.actor.displayName || "已删除用户"}</span><span>{row.summary || row.eventType || "—"}</span><span>{auditResourceLabel(row.resource)}</span><span className={row.outcome === "success" ? "status-ok" : "status-danger"}>{auditOutcomeLabel(row.outcome)}</span><span>详情</span></button>)}</div><div className="audit-pagination" aria-live="polite">{audit.error && <span role="alert">{audit.error}</span>}{audit.nextCursor && <button className="button secondary" type="button" disabled={audit.loadingMore} onClick={() => controller.loadMoreAudit()}>{audit.loadingMore ? "加载中…" : "加载更多"}</button>}</div></>}
  <section className="deletion-queue"><header><ShieldCheck size={21} /><div><h3>删除请求审批</h3><p>{canApproveDeletion ? "查看组织内请求并审批待处理或失败请求。" : "仅显示服务端授权返回的本人请求。"}</p></div></header><div className="deletion-queue-toolbar"><label>状态筛选<select value={deletionFilter} onChange={(event) => { const value = event.target.value; setDeletionFilter(value); controller.loadDeletionRequests(value); }}><option value="">全部状态</option><option value="requested">待审批</option><option value="approved">已批准</option><option value="executing">执行中</option><option value="completed">已完成</option><option value="failed">失败</option></select></label><span>{deletionQueue.rows.length} 条已加载请求</span></div>{deletionQueue.status === "loading" && <div className="governance-loading" role="status"><RefreshCw size={18} />正在加载删除请求…</div>}{deletionQueue.status === "denied" && <GovernanceState denied title="无删除请求权限" message="服务端未授权当前账号查看删除请求。" />}{deletionQueue.status === "error" && <GovernanceState title="删除请求加载失败" message={deletionQueue.error} onRetry={() => controller.loadDeletionRequests(deletionQueue.statusFilter)} />}{deletionQueue.status === "empty" && <div className="governance-empty" role="status"><Database size={22} /><strong>没有符合条件的删除请求</strong><span>可调整状态筛选后重试。</span></div>}{deletionQueue.rows.length > 0 && <div className="deletion-request-list">{deletionQueue.rows.map((row) => <button type="button" key={row.id} onClick={() => controller.loadDeletionRequest(row.id)}><span><strong>{row.id}</strong><small>{formatAuditTime(row.requestedAt)}</small></span><span className={row.status === "failed" ? "status-danger" : "status-muted"}>{deletionStatusLabel(row.status)}</span><ChevronDown size={15} /></button>)}</div>}{deletionQueue.nextCursor && <div className="audit-pagination"><button className="button secondary" type="button" disabled={deletionQueue.loadingMore} onClick={() => controller.loadMoreDeletionRequests()}>{deletionQueue.loadingMore ? "加载中…" : "加载更多"}</button></div>}</section>
  <section className="retention-policy"><header><Database size={21} /><div><h3>数据保留策略</h3><p>缩短任一周期必须先预览服务端影响并显式确认。</p></div></header>{retention.status === "loading" && !retention.policy && <div className="governance-loading" role="status"><RefreshCw size={18} />正在加载保留策略…</div>}{retention.status === "denied" && <GovernanceState denied title="无保留策略权限" message="服务端未授权当前账号查看保留策略。" />}{retention.status === "error" && !retention.policy && <GovernanceState title="保留策略加载失败" message={retention.error} onRetry={() => controller.loadRetention()} />}{retention.status !== "denied" && retention.policy && retention.draft && <>{retention.error && <div className="settings-error" role="alert"><AlertTriangle size={17} />{retention.error}</div>}{retention.message && <div className="llm-settings-message" role="status">{retention.message}</div>}<div><label>终态候选人保留天数<input type="number" min="30" max="3650" disabled={!editable || retentionBusy} value={retention.draft.terminalDays} onChange={(event) => changeRetention("terminalDays", event.target.value)} /></label><label>人才库保留天数<input type="number" min="30" max="3650" disabled={!editable || retentionBusy} value={retention.draft.talentPoolDays} onChange={(event) => changeRetention("talentPoolDays", event.target.value)} /></label><label>备份窗口天数<input type="number" min="30" max="3650" disabled={!editable || retentionBusy} value={retention.draft.backupWindowDays} onChange={(event) => changeRetention("backupWindowDays", event.target.value)} /></label>{editable && <button className="button primary" type="button" disabled={!retention.dirty || retentionBusy} onClick={saveRetention}>{retention.status === "previewing" ? "正在预览…" : retention.status === "saving" ? "保存中…" : "保存保留策略"}</button>}</div><small className="retention-version">当前版本 {retention.policy.version} · 最近更新 {formatAuditTime(retention.policy.updatedAt)}</small></>}</section>{activeSelected && <aside className="settings-drawer" aria-label="审计详情"><header><div><h2>审计详情</h2><p>{activeSelected.id} · {auditOutcomeLabel(activeSelected.outcome)}</p></div><button className="icon-button" type="button" aria-label="关闭" onClick={() => setSelected(null)}><X size={20} /></button></header><div className="settings-drawer-body"><dl><div><dt>时间</dt><dd>{formatAuditTime(activeSelected.createdAt)}</dd></div><div><dt>操作者</dt><dd>{activeSelected.actor.displayName || "已删除用户"}</dd></div><div><dt>事件摘要</dt><dd>{activeSelected.summary || activeSelected.eventType || "—"}</dd></div><div><dt>资源</dt><dd>{auditResourceLabel(activeSelected.resource)}</dd></div><div><dt>结果</dt><dd>{auditOutcomeLabel(activeSelected.outcome)}</dd></div><div><dt>网络标识</dt><dd>{activeSelected.networkRef || "—"}</dd></div><div><dt>Trace ID</dt><dd>{activeSelected.traceId || "—"}</dd></div></dl></div><footer><button className="button primary" type="button" onClick={() => setSelected(null)}>完成</button></footer></aside>}{retention.preview && <DangerDialog title="确认缩短数据保留周期" description={`服务端影响预览有效期至 ${formatAuditTime(retention.preview.expiresAt)}。`} impact={`预计 ${retention.preview.affectedCandidateCount} 位候选人受到影响。请核对后明确确认。`} confirmText={retention.status === "saving" ? "保存中…" : "确认缩短期限"} confirmDisabled={retention.status === "saving"} onCancel={() => controller.cancelRetentionPreview()} onConfirm={confirmRetention} />}{deletionQueue.selected && <aside className="settings-drawer deletion-request-drawer" role="dialog" aria-modal="true" aria-label="删除请求详情"><header><div><h2>删除请求详情</h2><p>{deletionQueue.selected.id} · {deletionStatusLabel(deletionQueue.selected.status)}</p></div><button className="icon-button" type="button" aria-label="关闭" disabled={deletionQueue.approving} onClick={() => controller.loadDeletionRequests(deletionQueue.statusFilter)}><X size={20} /></button></header><div className="settings-drawer-body">{deletionQueue.detailStatus === "loading" ? <div className="governance-loading" role="status"><RefreshCw size={18} />正在刷新请求…</div> : <><dl><div><dt>请求 ID</dt><dd>{deletionQueue.selected.id}</dd></div><div><dt>原因代码</dt><dd>{deletionQueue.selected.reasonCode || "—"}</dd></div><div><dt>状态</dt><dd>{deletionStatusLabel(deletionQueue.selected.status)}</dd></div><div><dt>版本</dt><dd>{deletionQueue.selected.version}</dd></div><div><dt>请求时间</dt><dd>{formatAuditTime(deletionQueue.selected.requestedAt)}</dd></div><div><dt>批准时间</dt><dd>{formatAuditTime(deletionQueue.selected.approvedAt)}</dd></div><div><dt>安全错误码</dt><dd>{deletionQueue.selected.safeErrorCode || "—"}</dd></div><div><dt>进行中的申请</dt><dd>{deletionQueue.selected.activeApplicationCount === null ? "未确认" : `${deletionQueue.selected.activeApplicationCount} 项`}</dd></div><div><dt>策略版本</dt><dd>{deletionQueue.selected.impact.policyVersion}</dd></div><div><dt>候选人版本</dt><dd>{deletionQueue.selected.impact.candidateVersion}</dd></div><div><dt>备份窗口</dt><dd>{formatAuditTime(deletionQueue.selected.impact.backupWindowEndsAt)}</dd></div></dl>{deletionQueue.selected.activeApplicationCount > 0 && <div className="settings-error" role="alert"><AlertTriangle size={17} />批准删除将先把 {deletionQueue.selected.activeApplicationCount} 项进行中申请标记为已撤回，再删除候选人及关联数据。</div>}<div className="deletion-impact-grid">{deletionCountLabels.map(([key, label]) => <span key={key}>{label}<strong>{deletionQueue.selected.impact.counts[key]}</strong></span>)}</div>{deletionQueue.impactChanged && <div className="settings-error" role="alert"><AlertTriangle size={17} />影响已变化，请重新核对后再次确认。</div>}{deletionQueue.detailError && <div className="settings-error" role="alert"><AlertTriangle size={17} />{deletionQueue.detailError}</div>}</>}</div><footer><button className="button secondary" type="button" disabled={deletionQueue.approving} onClick={() => controller.loadDeletionRequests(deletionQueue.statusFilter)}>关闭</button>{canApproveDeletion && ["requested", "failed"].includes(deletionQueue.selected.status) && <button className="button danger" type="button" disabled={deletionQueue.approving || deletionQueue.detailStatus === "loading"} onClick={() => setApprovalOpen(true)}>批准删除</button>}</footer></aside>}{approvalOpen && deletionQueue.selected && <DangerDialog surfaceClassName="deletion-approval-dialog" title={deletionQueue.selected.activeApplicationCount > 0 ? "确认终止申请并删除候选人" : "确认批准删除请求"} description={`请求 ${deletionQueue.selected.id} 将进入删除处理队列。`} impact={deletionQueue.selected.activeApplicationCount > 0 ? `系统将先把 ${deletionQueue.selected.activeApplicationCount} 项进行中申请标记为已撤回。该操作不可撤销，请确认后继续。` : "请核对九类影响数量和备份窗口。批准后仍可能因影响变化而要求重新确认。"} confirmText={deletionQueue.approving ? "批准中…" : deletionQueue.selected.activeApplicationCount > 0 ? `终止 ${deletionQueue.selected.activeApplicationCount} 项申请并批准删除` : "确认批准"} confirmDisabled={deletionQueue.approving} onCancel={() => setApprovalOpen(false)} onConfirm={confirmDeletionApproval} />}</div>;
}

export function SettingsWorkspace({ currentRole, onRoleChange, onNotify, pageActionHost, section = "组织与权限", organizationTab = "成员", templateTab = "招聘流程", onRouteChange = () => {} }) {
  const [aiDirty, setAiDirty] = useState(false);
  const [pendingSection, setPendingSection] = useState(null);
  const allowedSettingsSections = getAllowedSettingsSections(currentRole);
  const visibleSettingsSections = settingsSections.filter(([label]) => allowedSettingsSections.includes(label));
  const activeSection = allowedSettingsSections.includes(section) ? section : allowedSettingsSections[0];
  const content = activeSection === "组织与权限" ? <OrganizationSettings role={currentRole} onNotify={onNotify} pageActionHost={pageActionHost} activeTab={organizationTab} onTabChange={(tab) => onRouteChange("组织与权限", tab)} /> : activeSection === "流程与评价模板" ? <TemplateSettings role={currentRole} onNotify={onNotify} activeTab={templateTab} onTabChange={(tab) => onRouteChange("流程与评价模板", tab)} /> : activeSection === "AI 设置" ? <AiSettings role={currentRole} onNotify={onNotify} onDirtyChange={setAiDirty} /> : activeSection === "飞书集成" ? <FeishuIntegrationSettings onNotify={onNotify} /> : activeSection === "审计与数据治理" ? <AuditSettings key={currentRole} role={currentRole} onNotify={onNotify} /> : <section className="settings-denied"><LockKeyhole size={31} /><h3>无设置权限</h3><p>当前账号未获得系统设置访问权限。</p></section>;
  useEffect(() => {
    if (!aiDirty) return undefined;
    const preventUnsavedExit = (event) => { event.preventDefault(); event.returnValue = ""; };
    window.addEventListener("beforeunload", preventUnsavedExit);
    return () => window.removeEventListener("beforeunload", preventUnsavedExit);
  }, [aiDirty]);
  function openSection(nextSection) {
    if (activeSection === "AI 设置" && aiDirty && nextSection !== activeSection) {
      setPendingSection(nextSection);
      return;
    }
    onRouteChange(nextSection, settingsDefaultTabs[nextSection]);
  }
  function leaveAiSettings() {
    setAiDirty(false);
    onRouteChange(pendingSection, settingsDefaultTabs[pendingSection]);
    setPendingSection(null);
  }
  return <div className="settings-page"><RoleSwitch value={currentRole} onChange={onRoleChange} /><div className="settings-layout"><nav className="settings-subnav" aria-label="设置导航">{visibleSettingsSections.map(([label, Icon]) => <button type="button" key={label} className={activeSection === label ? "active" : ""} aria-current={activeSection === label ? "page" : undefined} onClick={() => openSection(label)}><Icon size={17} />{label}</button>)}</nav><main className="settings-content">{content}</main></div>{pendingSection && <div className="ux07-dialog-backdrop"><section className="ux07-dialog" role="dialog" aria-modal="true" aria-label="AI 设置尚未保存"><header><div><h3>AI 设置尚未保存</h3><p>离开将放弃 LLM 或 OCR 中尚未保存的配置修改。</p></div><button className="icon-button" type="button" aria-label="关闭" onClick={() => setPendingSection(null)}><X size={19} /></button></header><div className="ux07-danger-impact"><AlertTriangle size={22} /><span>未保存的 Provider、Base URL、模型和 API Key 替换内容都会被清除。</span></div><footer><button className="button secondary" type="button" onClick={() => setPendingSection(null)}>继续编辑</button><button className="button danger" type="button" onClick={leaveAiSettings}>放弃修改并离开</button></footer></section></div>}</div>;
}
