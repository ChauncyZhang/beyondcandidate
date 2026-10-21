import { useState } from "react";
import { CheckCircle2, LoaderCircle, LockKeyhole } from "lucide-react";
import { apiClient } from "./apiClient.js";
import { startFeishuAuthorization } from "./feishuIntegration.js";
import { invitationAcceptErrorMessage, passwordResetErrorMessage } from "./invitationErrors.js";
import "./product-theme-admin.css";

export function InviteAcceptView({ token, onAccepted, client = apiClient, mode = "invitation" }) {
  const [form, setForm] = useState({ password: "", confirmation: "" });
  const [status, setStatus] = useState("idle");
  const [error, setError] = useState("");
  const [acceptedEmail, setAcceptedEmail] = useState("");
  const reset = mode === "password-reset";
  const passwordLongEnough = form.password.length >= 12 && form.password.length <= 128;
  const passwordsMatch = form.password === form.confirmation;
  const valid = passwordLongEnough && passwordsMatch;

  async function submit(event) {
    event.preventDefault();
    if (!valid || ["saving", "binding", "success"].includes(status)) return;
    setStatus("saving");
    setError("");
    try {
      const result = await (reset ? client.acceptPasswordReset : client.acceptInvitation)({ token, password: form.password });
      setForm({ password: "", confirmation: "" });
      setAcceptedEmail(result?.email || "");
      // A consumed link must not reappear when returning from an external authorization page.
      window.history.replaceState(null, "", `${window.location.pathname}${window.location.search}`);
      if (reset) {
        setStatus("success");
        return;
      }
      setStatus("binding");
      try {
        await startFeishuAuthorization(() => client.authorizeFeishuLogin(result?.organization_slug || ""));
      } catch {
        setStatus("success");
        onAccepted(result?.email || "");
      }
    } catch (requestError) {
      setStatus("error");
      setError(reset ? passwordResetErrorMessage(requestError) : invitationAcceptErrorMessage(requestError));
    }
  }

  const busy = status === "saving" || status === "binding";
  return <main className="login-page invite-accept-page"><section className="login-card" aria-labelledby="invite-title">
    <header className="login-heading"><div className="login-mark" aria-hidden="true"><img src="/favicon.svg" alt="" /></div><div><p>BeyondCandidate · 候选人全流程招聘平台</p><h1 id="invite-title">{reset ? "重置登录密码" : "设置登录密码"}</h1></div></header>
    {reset && status === "success" ? <>
      <p className="login-intro" role="status">密码已重置</p>
      <p>请使用新密码登录。该账号的旧登录会话已退出。</p>
      <button className="button primary login-submit" type="button" onClick={() => onAccepted(acceptedEmail)}>前往登录</button>
    </> : <>
      <p className="login-intro">{reset ? "设置新密码后，旧密码及所有旧登录会话将失效。" : "完成密码设置后将前往飞书绑定，也可以使用已设置的密码登录。"}</p>
      <form className="login-form" onSubmit={submit} aria-busy={busy}>
        <label><span>新密码</span><div className="login-input"><LockKeyhole size={17} aria-hidden="true" /><input aria-label="新密码" type="password" value={form.password} onChange={(event) => setForm({ ...form, password: event.target.value })} autoComplete="new-password" minLength="12" maxLength="128" required disabled={busy} /></div><small className={form.password && !passwordLongEnough ? "field-error" : ""}>12 至 128 个字符</small></label>
        <label><span>确认新密码</span><div className="login-input"><CheckCircle2 size={17} aria-hidden="true" /><input aria-label="确认新密码" type="password" value={form.confirmation} onChange={(event) => setForm({ ...form, confirmation: event.target.value })} autoComplete="new-password" minLength="12" maxLength="128" required disabled={busy} /></div>{form.confirmation && !passwordsMatch && <small className="field-error">两次输入的密码不一致</small>}</label>
        <div className="login-message" role={error ? "alert" : "status"} aria-live="polite">{error}</div>
        <button className="button primary login-submit" type="submit" disabled={!valid || busy}>{busy && <LoaderCircle className="spin" size={17} aria-hidden="true" />}{status === "binding" ? "正在前往飞书绑定…" : status === "saving" ? "正在设置…" : reset ? "重置密码" : "设置密码"}</button>
        {error && <button className="button secondary" type="button" onClick={() => onAccepted("")}>前往登录</button>}
      </form>
    </>}
  </section></main>;
}
