function text(value) { return typeof value === "string" ? value.trim() : ""; }
function unwrap(value) { return value?.data && typeof value.data === "object" ? value.data : value || {}; }

export function createPublicOfferController({ fetchImpl = globalThis.fetch } = {}) {
  function normalize(payload) {
    const value = unwrap(payload); const content = value.content && typeof value.content === "object" ? value.content : {};
    return { status: text(value.status) || "invalid", jobTitle: text(value.job_title ?? value.role_title), companyName: text(value.company_name ?? value.organization_name) || "BeyondCandidate", deadline: text(value.candidate_response_deadline ?? value.deadline), summary: text(value.offer_summary ?? content.summary ?? content.body), location: text(value.location ?? value.job_location), contact: text(value.hr_contact ?? value.contact), response: value.response && typeof value.response === "object" ? { status: text(value.response.status) } : null };
  }
  async function request(path, options = {}) {
    const response = await fetchImpl(path, { credentials: "omit", ...options, headers: { "Content-Type": "application/json", ...(options.headers || {}) } });
    if (!response.ok) { const error = new Error("public_offer_request_failed"); error.status = response.status; throw error; }
    return response.status === 204 ? null : response.json();
  }
  return { normalize, load: async (token) => normalize(await request(`/api/public/v1/offers/${encodeURIComponent(token)}`, { method: "GET" })), pdfUrl: (token) => `/api/public/v1/offers/${encodeURIComponent(token)}/pdf`, respond: async (token, body) => normalize(await request(`/api/public/v1/offers/${encodeURIComponent(token)}/responses`, { method: "POST", body: JSON.stringify(body) })) };
}
export const publicOfferController = createPublicOfferController();
