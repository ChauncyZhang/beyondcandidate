export function publicOfferState(offer, decision = "", startDate = "") {
  if (!offer) return "invalid";
  if (decision === "accepted" && !startDate) return "start-date-required";
  return offer.status === "sent" ? "active" : offer.status || "invalid";
}
