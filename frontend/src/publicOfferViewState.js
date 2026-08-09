export function publicOfferState(offer, decision = "", startDate = "") {
  if (!offer) return "invalid";
  if (decision === "accepted" && !startDate) return "start-date-required";
  return offer.status === "sent" ? "active" : offer.status || "invalid";
}
export function publicOfferLoadState(error) { return error?.status === 404 ? "invalid" : "error"; }
export function responseBody(decision, startDate, reason) { if (decision === "accepted") return startDate ? { decision, expected_start_date: startDate } : null; return decision === "declined" ? { decision, reason_text: reason || null } : null; }
