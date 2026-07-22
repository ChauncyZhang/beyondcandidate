import { useEffect, useRef, useState } from "react";
import { CheckCircle2, KeyRound, Link2, UserRound, X } from "lucide-react";
import { apiClient } from "./apiClient.js";
import { createDialogFocusManager } from "./SettingsViews.jsx";
import { normalizeFeishuBinding, startFeishuAuthorization } from "./feishuIntegration.js";
import "./product-theme-admin.css";

export function ProfileSettings({ user, role, onClose, client = apiClient }) {
  const drawerRef = useRef(null);
  const restoreTargetRef = useRef(typeof document === "undefined" ? null : document.activeElement);
  const busyRef = useRef(false);
  const closeRef = useRef(onClose);
  const [tab, setTab] = useState("profile");
  const [form, setForm] = useState({ currentPassword: "", newPassword: "", confirmation: "" });
  const [status, setStatus] = useState("idle");
  const [error, setError] = useState("");
  const [binding, setBinding] = useState(normalizeFeishuBinding());
  const [feishuStatus, setFeishuStatus] = useState("idle");
  const [feishuError, setFeishuError] = useState("");
  busyRef.current = status === "saving";
  closeRef.current = onClose;

  useEffect(() => {
    if (!drawerRef.current || typeof document === "undefined") return undefined;
    const manager = createDialogFocusManager({ dialog: drawerRef.current, restoreTarget: restoreTargetRef.current, documentRef: document, isBusy: () => busyRef.current, onClose: () => closeRef.current() });
    manager.focusInitial();
    const handleKeyDown = (event) => manager.handleKeyDown(event);
    document.addEventListener("keydown", handleKeyDown);
    return () => { document.removeEventListener("keydown", handleKeyDown); manager.restoreFocus(); };
  }, []);

  useEffect(() => {
    if (tab !== "feishu" || feishuStatus !== "idle") return;
    setFeishuStatus("loading");
    client.getFeishuBinding().then((value) => { setBinding(normalizeFeishuBinding(value)); setFeishuStatus("ready"); }).catch(() => { setFeishuError("绑定状态暂时无法读取。"); setFeishuStatus("error"); });
  }, [client, feishuStatus, tab]);

  async function bindFeishu() {
    setFeishuError(""); setFeishuStatus("binding");
    try { await startFeishuAuthorization(() => client.authorizeFeishuBinding()); }
    catch { setFeishuError("暂时无法发起飞书绑定。"); setFeishuStatus("error"); }
  }
  async function unbindFeishu() {
    setFeishuError(""); setFeishuStatus("unbinding");
    try { await client.unbindFeishu(); setBinding(normalizeFeishuBinding()); setFeishuStatus("ready"); }
    catch { setFeishuError("解绑失败，请稍后重试。"); setFeishuStatus("error"); }
  }

  const valid = Boolean(form.currentPassword && form.newPassword.length >= 12 && form.newPassword === form.confirmation);
  async function changePassword(event) {
    event.preventDefault();
    if (!valid || status === "saving") return;
    setStatus("saving");
    setError("");
    try {
      await client.changePassword({ current_password: form.currentPassword, new_password: form.newPassword });
      setForm({ currentPassword: "", newPassword: "", confirmation: "" });
      setStatus("success");
    } catch (requestError) {
      setStatus("error");
      setError(requestError?.kind === "unavailable" ? "服务暂时不可用，请稍后重试。" : "密码修改失败，请核对当前密码后重试。");
    }
  }

  const name = typeof user?.display_name === "string" && user.display_name.trim() ? user.display_name.trim() : "当前用户";
  const email = typeof user?.email === "string" && user.email.trim() ? user.email.trim() : "未提供";
  const organization = typeof user?.organization?.name === "string" && user.organization.name.trim() ? user.organization.name.trim() : "未提供";
  const department = typeof user?.department?.name === "string" && user.department.name.trim() ? user.department.name.trim() : "";
  return <div className="profile-settings-backdrop"><aside ref={drawerRef} className="settings-drawer profile-settings-drawer" role="dialog" aria-modal="true" aria-label="个人设置"><header><div><h2>个人设置</h2><p>{name} · {role}</p></div><button className="icon-button" type="button" aria-label="关闭" disabled={status === "saving"} onClick={onClose}><X size={20} /></button></header><div className="profile-settings-tabs" role="tablist" aria-label="个人设置分类"><button data-dialog-initial-focus aria-label="个人资料" type="button" role="tab" aria-selected={tab === "profile"} className={tab === "profile" ? "active" : ""} onClick={() => setTab("profile")}><UserRound size={17} />个人资料</button><button aria-label="账号安全" type="button" role="tab" aria-selected={tab === "security"} className={tab === "security" ? "active" : ""} onClick={() => setTab("security")}><KeyRound size={17} />账号安全</button><button aria-label="飞书账号" type="button" role="tab" aria-selected={tab === "feishu"} className={tab === "feishu" ? "active" : ""} onClick={() => setTab("feishu")}><Link2 size={17} />飞书账号</button></div><div className="settings-drawer-body">{tab === "profile" ? <dl className="profile-facts-list"><div><dt>姓名</dt><dd>{name}</dd></div><div><dt>邮箱</dt><dd>{email}</dd></div><div><dt>组织</dt><dd>{organization}</dd></div>{department && <div><dt>部门</dt><dd>{department}</dd></div>}<div><dt>角色</dt><dd>{role}</dd></div></dl> : tab === "feishu" ? <section className="password-settings-form">{feishuError && <div className="settings-error" role="alert">{feishuError}</div>}<h3>{binding.bound ? "已绑定飞书账号" : "未绑定飞书账号"}</h3><p>{binding.bound ? `Open ID：${binding.openId || "已记录"}` : "绑定后可使用飞书登录；系统不会即时创建新用户。"}</p>{binding.bound ? <button className="button secondary" type="button" disabled={feishuStatus === "unbinding"} onClick={unbindFeishu}>{feishuStatus === "unbinding" ? "解绑中…" : "解除绑定"}</button> : <button className="button primary" type="button" disabled={["loading", "binding"].includes(feishuStatus)} onClick={bindFeishu}>{feishuStatus === "binding" ? "正在跳转…" : "绑定飞书账号"}</button>}</section> : <form className="password-settings-form" onSubmit={changePassword}>{error && <div className="settings-error" role="alert">{error}</div>}{status === "success" && <div className="profile-success" role="status"><CheckCircle2 size={17} />密码已修改</div>}<label>当前密码<input aria-label="当前密码" type="password" autoComplete="current-password" value={form.currentPassword} onChange={(event) => { setStatus("idle"); setForm({ ...form, currentPassword: event.target.value }); }} /></label><label>新密码<input aria-label="新密码" type="password" autoComplete="new-password" minLength="12" value={form.newPassword} onChange={(event) => { setStatus("idle"); setForm({ ...form, newPassword: event.target.value }); }} /><small>至少 12 个字符</small></label><label>确认新密码<input aria-label="确认新密码" type="password" autoComplete="new-password" minLength="12" value={form.confirmation} onChange={(event) => { setStatus("idle"); setForm({ ...form, confirmation: event.target.value }); }} />{form.confirmation && form.confirmation !== form.newPassword && <small className="field-error">两次输入的密码不一致</small>}</label><button className="button primary" type="submit" disabled={!valid || status === "saving"}>{status === "saving" ? "正在修改…" : "修改密码"}</button></form>}</div><footer><button className="button secondary" type="button" disabled={status === "saving"} onClick={onClose}>关闭</button></footer></aside></div>;
}
