const OFFER_TIME_ZONE = "Asia/Shanghai";
const OFFER_UTC_OFFSET = "+08:00";

function dateParts(value) {
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return null;
  const parts = new Intl.DateTimeFormat("en-US", {
    timeZone: OFFER_TIME_ZONE,
    year: "numeric",
    month: "2-digit",
    day: "2-digit",
  }).formatToParts(date);
  return Object.fromEntries(parts.filter((part) => part.type !== "literal").map((part) => [part.type, part.value]));
}

export function offerDeadlineDateValue(value) {
  const parts = dateParts(value);
  return parts ? `${parts.year}-${parts.month}-${parts.day}` : "";
}

export function offerDeadlineEndOfDay(value) {
  if (!/^\d{4}-\d{2}-\d{2}$/.test(value || "")) return new Date(Number.NaN);
  return new Date(`${value}T23:59:59.999${OFFER_UTC_OFFSET}`);
}

export function displayOfferDeadline(value) {
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return value || "未记录";
  return date.toLocaleDateString("zh-CN", {
    timeZone: OFFER_TIME_ZONE,
    year: "numeric",
    month: "long",
    day: "numeric",
  });
}
