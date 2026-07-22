import { useEffect, useState } from "react";
import { AlertTriangle, CheckCircle2, RefreshCw } from "lucide-react";
import { apiClient } from "./apiClient.js";
import { buildFeishuConfigPayload, getFeishuConfigErrorMessage, normalizeFeishuConfig } from "./feishuIntegration.js";

const emptySecrets = { app_secret: "", verification_token: "", encrypt_key: "" };

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
      setConfig(next); setMessage(succeeded ? "连接测试成功" : "连接测试失败"); setStatus(succeeded ? "ready" : "error");
    }
    catch { setMessage("连接测试失败，现有招聘功能不受影响。"); setStatus("error"); }
  }

  if (status === "loading") return <div className="organization-state" role="status"><RefreshCw size={18} />正在加载飞书配置…</div>;
  return <form className="settings-section password-settings-form feishu-integration-form" onSubmit={save}>
    <div className="settings-section-heading"><div><h2>飞书集成</h2><p>默认关闭。秘密字段只可替换，服务端不会回传明文。</p></div></div>
    {message && <div className={status === "error" ? "settings-error" : "profile-success"} role="status">{status === "error" ? <AlertTriangle size={17} /> : <CheckCircle2 size={17} />}{message}</div>}
    <label>App ID<input value={draft.app_id} onChange={(event) => update("app_id", event.target.value)} required /></label>
    <label>App Secret<input type="password" autoComplete="new-password" placeholder={config.appSecretConfigured ? "已配置；留空保持不变" : "请输入 App Secret"} value={draft.app_secret} onChange={(event) => update("app_secret", event.target.value)} /></label>
    <label>Redirect URI<input type="url" value={draft.redirect_uri} onChange={(event) => update("redirect_uri", event.target.value)} required /></label>
    <label>Calendar ID（可选）<input value={draft.calendar_id} onChange={(event) => update("calendar_id", event.target.value)} /></label>
    <label>Verification Token<input type="password" autoComplete="new-password" placeholder={config.verificationTokenConfigured ? "已配置；留空保持不变" : "请输入 Verification Token"} value={draft.verification_token} onChange={(event) => update("verification_token", event.target.value)} /></label>
    <label>Encrypt Key<input type="password" autoComplete="new-password" placeholder={config.encryptKeyConfigured ? "已配置；留空保持不变" : "请输入 Encrypt Key"} value={draft.encrypt_key} onChange={(event) => update("encrypt_key", event.target.value)} /></label>
    <div className="feishu-form-footer">
      <label className="feishu-enabled-control"><input type="checkbox" checked={draft.enabled} onChange={(event) => update("enabled", event.target.checked)} /><span>启用飞书集成</span></label>
      <div className="feishu-form-actions"><button className="button primary" type="submit" disabled={status === "saving" || status === "testing"}>{status === "saving" ? "保存中…" : "保存配置"}</button><button className="button secondary" type="button" disabled={!config.configured || status === "saving" || status === "testing"} onClick={testConnection}>{status === "testing" ? "测试中…" : "测试连接"}</button></div>
    </div>
  </form>;
}
