import { useState } from "react";
import { CheckCircle2, LoaderCircle, LockKeyhole } from "lucide-react";
import { apiClient } from "./apiClient.js";
import { startFeishuAuthorization } from "./feishuIntegration.js";
import "./product-theme-admin.css";

export function InviteAcceptView({ token, onAccepted, client = apiClient }) {
  const [form, setForm] = useState({ password: "", confirmation: "" });
  const [status, setStatus] = useState("idle");
  const [error, setError] = useState("");
  const passwordLongEnough = form.password.length >= 12;
  const passwordsMatch = form.password === form.confirmation;
  const valid = passwordLongEnough && passwordsMatch;

  async function submit(event) {
    event.preventDefault();
    if (!valid || status === "saving") return;
    setStatus("saving");
    setError("");
    try {
      const result = await client.acceptInvitation({ token, password: form.password });
      setStatus("binding");
      try {
        await startFeishuAuthorization(() => client.authorizeFeishuLogin(result?.organization_slug || ""));
      } catch {
        setStatus("success");
        onAccepted(result?.email || "");
      }
    } catch (requestError) {
      setStatus("error");
      setError(requestError?.kind === "unavailable" ? "服务暂时不可用，请稍后重试。" : "邀请无效或已过期，请联系管理员重新邀请。");
    }
  }

  const busy = status === "saving" || status === "binding";
  return <main className="login-page invite-accept-page"><section className="login-card" aria-labelledby="invite-title"><header className="login-heading"><div className="login-mark" aria-hidden="true"><img src="/favicon.svg" alt="" /></div><div><p>BeyondCandidate · 候选人全流程招聘平台</p><h1 id="invite-title">设置登录密码</h1></div></header><p className="login-intro">完成密码设置后将前往飞书绑定，绑定成功即可进入工作台。</p><form className="login-form" onSubmit={submit} aria-busy={busy}><label><span>新密码</span><div className="login-input"><LockKeyhole size={17} aria-hidden="true" /><input aria-label="新密码" type="password" value={form.password} onChange={(event) => setForm({ ...form, password: event.target.value })} autoComplete="new-password" minLength="12" required disabled={busy} /></div><small className={form.password && !passwordLongEnough ? "field-error" : ""}>至少 12 个字符</small></label><label><span>确认新密码</span><div className="login-input"><CheckCircle2 size={17} aria-hidden="true" /><input aria-label="确认新密码" type="password" value={form.confirmation} onChange={(event) => setForm({ ...form, confirmation: event.target.value })} autoComplete="new-password" minLength="12" required disabled={busy} /></div>{form.confirmation && !passwordsMatch && <small className="field-error">两次输入的密码不一致</small>}</label><div className="login-message" role={error ? "alert" : "status"} aria-live="polite">{error}</div><button className="button primary login-submit" type="submit" disabled={!valid || busy}>{busy && <LoaderCircle className="spin" size={17} aria-hidden="true" />}{status === "binding" ? "正在前往飞书绑定…" : status === "saving" ? "正在设置…" : "设置密码"}</button></form></section></main>;
}
