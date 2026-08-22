function text(value) { return typeof value === "string" ? value.trim() : ""; }
function unwrap(value) { return value?.data && typeof value.data === "object" ? value.data : value || {}; }

export function createPublicOfferController({ fetchImpl = globalThis.fetch } = {}) {
  const responseFlights = new Map();
  function normalize(payload) {
    const value = unwrap(payload); const content = value.content && typeof value.content === "object" ? value.content : {};
    const prefill = value.onboarding_prefill && typeof value.onboarding_prefill === "object" ? value.onboarding_prefill : {};
    const candidateName = text(prefill.candidate_name) || text(value.candidate_name ?? content.candidate_name ?? value.greeting ?? content.greeting);
    const jobTitle = text(prefill.job_title) || text(value.job_title ?? value.role_title ?? content.job_title);
    return { status: text(value.status ?? value.display_status) || "invalid", candidateName, jobTitle, companyName: text(value.company_name ?? value.organization_name ?? content.company_name) || "企业招聘", deadline: text(value.candidate_response_deadline ?? value.deadline ?? content.deadline), title: text(content.title ?? value.offer_title), body: text(content.body ?? value.offer_summary ?? content.summary), compensation: text(content.compensation), benefits: text(content.benefits), location: text(value.location ?? value.job_location ?? content.location), contact: text(value.hr_contact ?? value.contact ?? content.hr_contact), pdfAvailable: value.pdf_available === true, response: value.response && typeof value.response === "object" ? { status: text(value.response.status) } : null, onboardingPrefill: { candidateName, email: text(prefill.email), phone: text(prefill.phone), departmentName: text(prefill.department_name), jobTitle } };
  }
  async function request(path, options = {}) {
    const response = await fetchImpl(path, { credentials: "omit", ...options, headers: { "Content-Type": "application/json", ...(options.headers || {}) } });
    if (!response.ok) { const error = new Error("public_offer_request_failed"); error.status = response.status; throw error; }
    return response.status === 204 ? null : response.json();
  }
  function respond(token, body) {
    if (responseFlights.has(token)) return responseFlights.get(token);
    const flight = request(`/api/public/v1/offers/${encodeURIComponent(token)}/responses`, { method: "POST", body: JSON.stringify(body) }).then(normalize).finally(() => { responseFlights.delete(token); });
    responseFlights.set(token, flight); return flight;
  }
  async function fetchPdf(token) { if (!text(token)) throw Object.assign(new Error("public_offer_request_failed"), { status: 404 }); const response = await fetchImpl(`/api/public/v1/offers/${encodeURIComponent(token)}/pdf`, { method: "GET", credentials: "omit" }); if (!response.ok) { const error = new Error("public_offer_request_failed"); error.status = response.status; throw error; } return response.blob(); }
  return { normalize, load: async (token) => text(token) ? normalize(await request(`/api/public/v1/offers/${encodeURIComponent(token)}`, { method: "GET" })) : normalize({ status: "invalid" }), fetchPdf, respond };
}
export const publicOfferController = createPublicOfferController();
