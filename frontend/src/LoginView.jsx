import { useEffect, useState } from "react";
import { Building2, Clock3, LoaderCircle, LockKeyhole, LogIn, LogOut, Mail, MessageCircle, ShieldX } from "lucide-react";
import { apiClient } from "./apiClient.js";
import { getSessionMessage } from "./session.js";
import { getFeishuCallbackErrorCode, getFeishuLoginErrorMessage, startFeishuAuthorization } from "./feishuIntegration.js";
import "./product-theme-admin.css";

export function SessionLoadingView() {
  return (
    <main className="session-loading" aria-busy="true" aria-live="polite">
      <LoaderCircle className="spin" size={24} aria-hidden="true" />
      <span>正在确认登录状态…</span>
    </main>
  );
}

export function AccessDeniedView({ displayName, error, loggingOut, onLogout }) {
  const message = getSessionMessage(error);
  return (
    <main className="access-denied-page">
      <section className="access-denied-card" aria-labelledby="access-denied-title">
        <span className="access-denied-icon" aria-hidden="true"><ShieldX size={25} /></span>
        <p className="access-denied-identity">{displayName}</p>
        <h1 id="access-denied-title">当前账号暂无访问权限</h1>
        <p>你的账号已完成验证，但尚未分配可使用的系统角色。请联系组织管理员处理。</p>
        {message && <div className="access-denied-error" role="alert">{message}</div>}
        <button className="button secondary" type="button" disabled={loggingOut} onClick={() => { void onLogout().catch(() => {}); }}>
          {loggingOut ? <LoaderCircle className="spin" size={17} aria-hidden="true" /> : <LogOut size={17} aria-hidden="true" />}
          {loggingOut ? "正在退出…" : "退出登录"}
        </button>
      </section>
    </main>
  );
}

export function formatLockoutDuration(totalSeconds) {
  const seconds = Math.max(0, Math.ceil(Number(totalSeconds) || 0));
  const minutes = Math.floor(seconds / 60);
  const remainder = seconds % 60;
  return minutes > 0 ? `${minutes} 分 ${remainder} 秒` : `${remainder} 秒`;
}

export function LoginView({ error, retryAfterSeconds, submitting, onLogin, initialEmail = "", loadAuthContext = apiClient.getAuthContext, client = apiClient }) {
  const [form, setForm] = useState({ organization_slug: "", email: initialEmail, password: "" });
  const [authContextStatus, setAuthContextStatus] = useState("loading");
  const [feishuStatus, setFeishuStatus] = useState("idle");
  const [feishuError, setFeishuError] = useState("");
  const [lockoutUntil, setLockoutUntil] = useState(null);
  const [lockoutRemaining, setLockoutRemaining] = useState(0);
  const [lockoutDismissed, setLockoutDismissed] = useState(false);
  const [recoveryOpen, setRecoveryOpen] = useState(false);
  const [recoveryStatus, setRecoveryStatus] = useState("idle");
  const [recoveryError, setRecoveryError] = useState("");
  const [recoveryCooldownUntil, setRecoveryCooldownUntil] = useState(0);
  const [recoveryRemaining, setRecoveryRemaining] = useState(0);
  const message = getSessionMessage(error);
  const callbackErrorCode = getFeishuCallbackErrorCode();
  const callbackError = callbackErrorCode ? getFeishuLoginErrorMessage({ code: callbackErrorCode }) : "";

  useEffect(() => {
    if (initialEmail) setForm((current) => ({ ...current, email: initialEmail }));
  }, [initialEmail]);

  useEffect(() => {
    let active = true;
    void loadAuthContext()
      .then((context) => {
        if (!active) return;
        const organizationSlug = typeof context?.organization_slug === "string" ? context.organization_slug.trim() : "";
        if (organizationSlug) setForm((current) => ({ ...current, organization_slug: organizationSlug }));
        setAuthContextStatus(organizationSlug ? "configured" : "manual");
      })
      .catch(() => {
        if (active) setAuthContextStatus("manual");
      });
    return () => { active = false; };
  }, [loadAuthContext]);

  useEffect(() => {
    if (error !== "locked" || !Number.isInteger(retryAfterSeconds) || retryAfterSeconds <= 0) {
      if (error !== "locked") {
        setLockoutUntil(null);
        setLockoutRemaining(0);
        setLockoutDismissed(false);
      }
      return;
    }
    const until = Date.now() + retryAfterSeconds * 1000;
    setLockoutUntil(until);
    setLockoutRemaining(retryAfterSeconds);
    setLockoutDismissed(false);
  }, [error, retryAfterSeconds]);

  useEffect(() => {
    if (!lockoutUntil || lockoutDismissed) return undefined;
    const updateRemaining = () => setLockoutRemaining(Math.max(0, Math.ceil((lockoutUntil - Date.now()) / 1000)));
    updateRemaining();
    const timer = window.setInterval(updateRemaining, 1000);
    return () => window.clearInterval(timer);
  }, [lockoutUntil, lockoutDismissed]);

  useEffect(() => {
    if (!recoveryCooldownUntil) return undefined;
    const updateRemaining = () => setRecoveryRemaining(Math.max(0, Math.ceil((recoveryCooldownUntil - Date.now()) / 1000)));
    updateRemaining();
    const timer = window.setInterval(updateRemaining, 1000);
    return () => window.clearInterval(timer);
  }, [recoveryCooldownUntil]);

  function update(field, value) {
    if (field === "email" && value !== form.email) {
      setLockoutDismissed(true);
      setLockoutUntil(null);
      setLockoutRemaining(0);
    }
    setForm((current) => ({ ...current, [field]: value }));
  }

  async function submit(event) {
    event.preventDefault();
    try {
      await onLogin(form);
    } catch {
      // The session controller exposes only a safe, categorized error to this view.
    }
  }

  async function loginWithFeishu() {
    if (!form.organization_slug || feishuStatus === "loading") return;
    setFeishuError(""); setFeishuStatus("loading");
    try { await startFeishuAuthorization(() => client.authorizeFeishuLogin(form.organization_slug)); }
    catch (error) { setFeishuError(getFeishuLoginErrorMessage(error)); setFeishuStatus("error"); }
  }

  function openRecovery() {
    setForm((current) => ({ ...current, password: "" }));
    setRecoveryError("");
    setRecoveryOpen(true);
  }

  async function submitRecovery(event) {
    event.preventDefault();
    if (recoveryStatus === "sending" || recoveryRemaining > 0 || !form.organization_slug.trim()) return;
    setRecoveryStatus("sending");
    setRecoveryError("");
    try {
      await client.requestPasswordResetEmail({ organization_slug: form.organization_slug.trim(), email: form.email.trim() });
      setRecoveryRemaining(60);
      setRecoveryCooldownUntil(Date.now() + 60_000);
      setRecoveryStatus("success");
    } catch (error) {
      setRecoveryStatus("error");
      setRecoveryError(error?.status === 429 ? "请求过于频繁，请稍后重试。" : error?.code === "csrf_validation_failed" ? "安全验证失败，请刷新页面后重试。" : error?.status === 422 ? "请检查组织标识和邮箱格式后重试。" : "重置邮件暂时无法申请，请稍后重试，或联系管理员协助。原密码和已有登录不会因此改变。");
    }
  }

  const lockoutActive = !lockoutDismissed && lockoutUntil !== null && lockoutRemaining > 0;
  const lockoutExpired = !lockoutDismissed && lockoutUntil !== null && lockoutRemaining === 0;
  const lockoutTime = lockoutUntil ? new Intl.DateTimeFormat("zh-CN", { hour: "2-digit", minute: "2-digit", second: "2-digit", hourCycle: "h23" }).format(new Date(lockoutUntil)) : "";
  const loginMessage = lockoutActive
    ? `${message} 请于 ${lockoutTime} 后重试，剩余 ${formatLockoutDuration(lockoutRemaining)}。`
    : lockoutExpired
      ? "锁定时间已结束，请重新登录。"
      : lockoutDismissed
        ? ""
        : message;

  return (
    <main className="login-page">
      <section className="login-card" aria-labelledby="login-title">
        <header className="login-heading">
          <div className="login-mark" aria-hidden="true"><img src="/favicon.svg" alt="" /></div>
          <div>
            <p>BeyondCandidate · 候选人全流程招聘平台</p>
            <h1 id="login-title">{recoveryOpen ? "找回密码" : "登录工作台"}</h1>
          </div>
        </header>
        <p className="login-intro">{recoveryOpen ? "通过注册的工作邮箱接收密码重置链接。" : "使用工作账号继续。"}</p>

        <form className="login-form" onSubmit={recoveryOpen ? submitRecovery : submit} aria-busy={submitting || recoveryStatus === "sending"}>
          {authContextStatus === "manual" && <label>
            <span>组织标识</span>
            <div className="login-input">
              <Building2 size={17} aria-hidden="true" />
              <input
                name="organization_slug"
                value={form.organization_slug}
                onChange={(event) => update("organization_slug", event.target.value)}
                autoComplete="organization"
                placeholder="例如：acme"
                required
                disabled={submitting || (recoveryOpen && recoveryStatus === "sending")}
              />
            </div>
          </label>}
          <label>
            <span>工作邮箱</span>
            <div className="login-input">
              <Mail size={17} aria-hidden="true" />
              <input
                name="email"
                type="email"
                value={form.email}
                onChange={(event) => update("email", event.target.value)}
                autoComplete="username"
                placeholder="name@company.com"
                required
                disabled={submitting || (recoveryOpen && recoveryStatus === "sending")}
              />
            </div>
          </label>
          {!recoveryOpen && <label>
            <span>密码</span>
            <div className="login-input">
              <LockKeyhole size={17} aria-hidden="true" />
              <input
                name="password"
                type="password"
                value={form.password}
                onChange={(event) => update("password", event.target.value)}
                autoComplete="current-password"
                placeholder="请输入密码"
                required
                disabled={submitting}
              />
            </div>
          </label>}

          {recoveryOpen ? <>
            <div className={`login-message${recoveryStatus === "success" ? " login-recovery-success" : ""}`} role={recoveryError ? "alert" : "status"} aria-live="polite">
              {recoveryError || (recoveryStatus === "success" ? "如果该邮箱对应可用账号，我们会发送密码重置邮件。请检查收件箱和垃圾邮件；链接在 1 小时内有效。若未收到，请稍后重试或联系管理员。" : "")}
            </div>
            <button className="button primary login-submit" type="submit" disabled={recoveryStatus === "sending" || recoveryRemaining > 0 || authContextStatus === "loading" || !form.organization_slug.trim()}>
              {recoveryStatus === "sending" ? <LoaderCircle className="spin" size={17} aria-hidden="true" /> : <Mail size={17} aria-hidden="true" />}
              {recoveryStatus === "sending" ? "正在发送…" : recoveryRemaining > 0 ? `重新发送（${recoveryRemaining} 秒）` : recoveryStatus === "success" ? "重新发送" : "发送重置邮件"}
            </button>
            <button className="button secondary login-submit" type="button" disabled={recoveryStatus === "sending"} onClick={() => { setRecoveryOpen(false); setRecoveryError(""); }}>返回登录</button>
          </> : <>
          <button className="login-forgot-password text-button" type="button" disabled={submitting || authContextStatus === "loading"} onClick={openRecovery}>忘记密码</button>
          <div className={`login-message${lockoutActive ? " login-lockout-message" : ""}`} role={loginMessage || callbackError ? "alert" : "status"} aria-live="polite">
            {lockoutActive && <Clock3 size={17} aria-hidden="true" />}
            <span>{loginMessage || callbackError || (authContextStatus === "loading" ? "正在读取部署配置…" : "")}</span>
          </div>
          <button className="button primary login-submit" type="submit" disabled={submitting || authContextStatus === "loading" || lockoutActive}>
            {submitting ? <LoaderCircle className="spin" size={17} aria-hidden="true" /> : <LogIn size={17} aria-hidden="true" />}
            {submitting ? "正在登录…" : lockoutActive ? `请等待 ${formatLockoutDuration(lockoutRemaining)}` : "登录"}
          </button>
          <button className="button secondary login-submit" type="button" disabled={submitting || feishuStatus === "loading" || authContextStatus === "loading" || !form.organization_slug} onClick={loginWithFeishu}><MessageCircle size={17} aria-hidden="true" />{feishuStatus === "loading" ? "正在跳转…" : "飞书登录"}</button>
          {feishuStatus === "error" && <div className="login-message" role="alert">{feishuError}</div>}
          </>}
        </form>
      </section>
      <p className="login-footnote">账号权限由组织管理员统一配置</p>
    </main>
  );
}
