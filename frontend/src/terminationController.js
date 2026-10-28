import { apiClient } from "./apiClient.js";

export const canManageTermination = (role) => ["招聘管理员", "HR 招聘专员", "HR", "recruiting_admin", "recruiter"].includes(role);
export const hasUnsettledExternal = (termination) => (termination?.external_items || []).some((item) => ["in_flight", "unknown"].includes(item.state));

export function createTerminationController({ client = apiClient } = {}) {
  const path = (id) => `/api/v1/applications/${encodeURIComponent(id)}/termination`;
  return {
    async preview(id) { return (await client.request(path(id))).data; },
    async pending() { return (await client.request("/api/v1/application-terminations/pending")).data; },
    async reconcile(preview, key) {
      return (await client.request(`${path(preview.application_id)}/reconcile-external`, {
        method: "POST", ifMatch: `"${preview.termination.version}"`, idempotencyKey: key,
        body: { reason_text: "核对飞书原请求结果" },
      })).data;
    },
    async terminate(preview, reason, confirmed, key) {
      return (await client.request(path(preview.application_id), {
        method: "POST", ifMatch: `"${preview.version}"`, idempotencyKey: key,
        body: { preview_version: preview.preview_version, reason_text: reason.trim(), confirm_not_onboarded: confirmed },
      })).data;
    },
    async resolve(preview, reason, key) {
      return (await client.request(`${path(preview.application_id)}/external-resolution`, {
        method: "POST", ifMatch: `"${preview.termination.version}"`, idempotencyKey: key,
        body: { reason_text: reason.trim() },
      })).data;
    },
  };
}
export const terminationController = createTerminationController();
