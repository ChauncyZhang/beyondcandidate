import { apiClient } from "./apiClient.js";

function safeString(value) {
  return typeof value === "string" ? value.trim() : "";
}

function safeRounds(value) {
  return Array.isArray(value) ? value.map(safeString).filter(Boolean) : [];
}

function safeVersion(value) {
  return Number.isInteger(value) && value >= 0 ? value : null;
}

function normalizeTemplate(value) {
  return {
    id: safeString(value?.id),
    name: safeString(value?.name),
    rounds: safeRounds(value?.rounds),
    status: value?.status === "inactive" ? "inactive" : "active",
    version: safeVersion(value?.version),
    createdAt: safeString(value?.created_at),
    updatedAt: safeString(value?.updated_at),
  };
}

function requestOptions(signal, options = {}) {
  return signal ? { ...options, signal } : options;
}

function randomKey() {
  return globalThis.crypto?.randomUUID?.() || `workflow-${Date.now()}`;
}

export function createWorkflowTemplateController({ client = apiClient, idempotencyKey = randomKey } = {}) {
  async function list({ signal } = {}) {
    const response = await client.request("/api/v1/settings/workflow-templates", requestOptions(signal));
    return (Array.isArray(response?.data) ? response.data : []).map(normalizeTemplate).filter((item) => item.id && item.name);
  }

  async function create(values, { signal } = {}) {
    const response = await client.request("/api/v1/settings/workflow-templates", requestOptions(signal, {
      method: "POST",
      body: { name: safeString(values?.name), rounds: safeRounds(values?.rounds) },
      idempotencyKey: idempotencyKey(),
    }));
    return normalizeTemplate(response?.data);
  }

  async function update(template, values, { signal } = {}) {
    const id = safeString(template?.id);
    const version = safeVersion(template?.version);
    if (!id || version === null) throw new Error("workflow template identity required");
    const response = await client.request(`/api/v1/settings/workflow-templates/${encodeURIComponent(id)}`, requestOptions(signal, {
      method: "PATCH",
      body: {
        name: safeString(values?.name),
        rounds: safeRounds(values?.rounds),
        status: values?.status === "inactive" ? "inactive" : "active",
      },
      ifMatch: `"${version}"`,
      idempotencyKey: idempotencyKey(),
    }));
    return normalizeTemplate(response?.data);
  }

  return { list, create, update };
}

export const workflowTemplateController = createWorkflowTemplateController();
export default workflowTemplateController;
