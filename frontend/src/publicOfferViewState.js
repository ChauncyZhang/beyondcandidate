function text(value) { return typeof value === "string" ? value.trim() : ""; }

export function publicOfferState(offer, decision = "", startDate = "") {
  if (!offer) return "invalid";
  if (decision === "accepted" && !startDate) return "start-date-required";
  return offer.status === "sent" ? "active" : offer.status || "invalid";
}
export function publicOfferLoadState(error) { return error?.status === 404 ? "invalid" : "error"; }
export function responseBody(decision, startDate, reason, onboardingData = {}) {
  if (decision === "accepted") {
    const normalized = {
      gender: text(onboardingData.gender),
      phone: text(onboardingData.phone),
      email: text(onboardingData.email),
      home_address: text(onboardingData.home_address),
    };
    if (!text(startDate) || !["male", "female"].includes(normalized.gender) || Object.values(normalized).some((value) => !value)) return null;
    return { decision, expected_start_date: text(startDate), onboarding_data: normalized };
  }
  return decision === "declined" ? { decision, reason_text: text(reason) || null } : null;
}
