export function localDateKey(value) {
  const year = value.getFullYear();
  const month = String(value.getMonth() + 1).padStart(2, "0");
  const day = String(value.getDate()).padStart(2, "0");
  return `${year}-${month}-${day}`;
}

export function parseLocalDate(value) {
  const match = /^(\d{4})-(\d{2})-(\d{2})$/.exec(value || "");
  if (!match) return null;
  const date = new Date(Number(match[1]), Number(match[2]) - 1, Number(match[3]), 12);
  return Number.isNaN(date.getTime()) ? null : date;
}

export function startOfWeek(reference = new Date()) {
  const date = reference instanceof Date ? new Date(reference) : parseLocalDate(reference);
  const normalized = date && !Number.isNaN(date.getTime()) ? date : new Date();
  normalized.setHours(12, 0, 0, 0);
  normalized.setDate(normalized.getDate() - ((normalized.getDay() + 6) % 7));
  return normalized;
}

export function buildWeekDays(reference = new Date(), today = new Date()) {
  const monday = startOfWeek(reference);
  const todayKey = localDateKey(today);
  return Array.from({ length: 7 }, (_, index) => {
    const date = new Date(monday);
    date.setDate(monday.getDate() + index);
    const key = localDateKey(date);
    return {
      key,
      date,
      label: `${String(date.getMonth() + 1).padStart(2, "0")}-${String(date.getDate()).padStart(2, "0")}`,
      weekday: new Intl.DateTimeFormat("zh-CN", { weekday: "short" }).format(date),
      isToday: key === todayKey,
    };
  });
}

export function moveWeek(reference, amount) {
  const date = startOfWeek(reference);
  date.setDate(date.getDate() + amount * 7);
  return date;
}

export function weekRange(reference) {
  const days = buildWeekDays(reference);
  return { from: days[0].key, to: days.at(-1).key };
}

export function weekLabel(reference) {
  const days = buildWeekDays(reference);
  const first = days[0].date;
  const last = days.at(-1).date;
  if (first.getFullYear() !== last.getFullYear()) return `${first.getFullYear()}年${first.getMonth() + 1}月 - ${last.getFullYear()}年${last.getMonth() + 1}月`;
  if (first.getMonth() !== last.getMonth()) return `${first.getFullYear()}年${first.getMonth() + 1}月 - ${last.getMonth() + 1}月`;
  return `${first.getFullYear()}年${first.getMonth() + 1}月`;
}
