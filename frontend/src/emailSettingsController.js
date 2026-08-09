import { apiClient } from "./apiClient.js";

function safeString(value) {
  return typeof value === "string" ? value : "";
}

function codedError(code, message) {
  const error = new Error(message);
  error.code = code;
  return error;
}

function normalizeConfig(value) {
  return {
    configured: value?.configured === true,
    host: safeString(value?.host),
    port: Number.isInteger(value?.port) ? value.port : 587,
    tlsMode: value?.tls_mode === "tls" ? "tls" : "starttls",
    username: safeString(value?.username),
    enabled: value?.enabled === true,
    version: Number.isInteger(value?.version) ? value.version : 0,
    senderName: safeString(value?.sender_name),
    senderAddress: safeString(value?.sender_address),
    senderSource: safeString(value?.sender_source),
    defaultReplyToEmail: safeString(value?.default_reply_to_email),
    defaultReplyToName: safeString(value?.default_reply_to_name),
  };
}

export function createEmailSettingsController({ client = apiClient, idempotencyKey = () => globalThis.crypto.randomUUID() } = {}) {
  async function load({ signal } = {}) {
    const result = await client.request("/api/v1/settings/email", signal ? { signal } : {});
    return normalizeConfig(result?.data);
  }

  async function save(settings, passwordReplacement = "", { signal } = {}) {
    const version = Number(settings?.version);
    if (!Number.isInteger(version) || version < 0) throw codedError("EMAIL_SETTINGS_VERSION_REQUIRED", "email settings version required");
    const password = safeString(passwordReplacement);
    const body = {
      host: safeString(settings?.host).trim(),
      port: Number(settings?.port),
      tls_mode: settings?.tlsMode === "tls" ? "tls" : "starttls",
      username: safeString(settings?.username).trim(),
      sender_name: safeString(settings?.senderName).trim(),
      sender_address: safeString(settings?.senderAddress).trim(),
      enabled: settings?.enabled === true,
      default_reply_to_email: safeString(settings?.defaultReplyToEmail).trim(),
      default_reply_to_name: safeString(settings?.defaultReplyToName).trim(),
      ...(password ? { password } : {}),
    };
    const result = await client.request("/api/v1/settings/email", {
      method: "PUT",
      ifMatch: `"${version}"`,
      idempotencyKey: idempotencyKey(),
      body,
      ...(signal ? { signal } : {}),
    });
    return normalizeConfig(result?.data);
  }

  async function testSavedConfiguration(values, { dirty = false, signal } = {}) {
    if (dirty) throw codedError("EMAIL_SETTINGS_DIRTY", "save email settings before testing");
    const result = await client.request("/api/v1/settings/email/test", {
      method: "POST",
      idempotencyKey: idempotencyKey(),
      body: { recipient: safeString(values?.recipient).trim() },
      ...(signal ? { signal } : {}),
    });
    return result?.data ?? null;
  }

  return { load, save, testSavedConfiguration };
}

export const emailSettingsController = createEmailSettingsController();
