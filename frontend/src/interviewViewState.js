function localDateKey(value) {
  const year = value.getFullYear();
  const month = String(value.getMonth() + 1).padStart(2, "0");
  const day = String(value.getDate()).padStart(2, "0");
  return `${year}-${month}-${day}`;
}

export function getLocalDateInputMin(reference = new Date()) {
  return localDateKey(reference);
}

export function isInterviewStartStrictlyFuture(date, time, now = new Date()) {
  if (!date || !time) return false;
  const startsAt = new Date(`${date}T${time}:00`).getTime();
  const reference = now instanceof Date ? now.getTime() : new Date(now).getTime();
  return Number.isFinite(startsAt) && Number.isFinite(reference) && startsAt > reference;
}

export function buildWorkweekColumns(reference = new Date()) {
  const today = new Date(reference.getFullYear(), reference.getMonth(), reference.getDate());
  const monday = new Date(today);
  monday.setDate(today.getDate() - ((today.getDay() + 6) % 7));
  const todayKey = localDateKey(today);
  const tomorrow = new Date(today);
  tomorrow.setDate(today.getDate() + 1);
  const tomorrowKey = localDateKey(tomorrow);
  return Array.from({ length: 5 }, (_, index) => {
    const date = new Date(monday);
    date.setDate(monday.getDate() + index);
    const key = localDateKey(date);
    const label = `${String(date.getMonth() + 1).padStart(2, "0")}-${String(date.getDate()).padStart(2, "0")}`;
    const weekday = key === todayKey ? "今天" : key === tomorrowKey ? "明天" : new Intl.DateTimeFormat("zh-CN", { weekday: "short" }).format(date);
    return [key, label, weekday];
  });
}

export function isInWorkweek(date, columns) {
  return Boolean(date && columns.length && date >= columns[0][0] && date <= columns.at(-1)[0]);
}

export function isMyInterview(record, userId) {
  return Boolean(userId && Array.isArray(record?.interviewerIds) && record.interviewerIds.includes(userId));
}

export function canSubmitInterviewFeedback(record) {
  if (record?.feedbackStatus === "待反馈") return true;
  return ["待确认", "已安排", "已确认"].includes(record?.status);
}

export function getInterviewPrimaryAction(record, { canSchedule = true, userId = "", now = new Date() } = {}) {
  if (!record) return null;
  const assigned = isMyInterview(record, userId);
  if (assigned && record.feedbackStatus === "已提交") return { kind: "feedback", label: "查看评价" };
  if (assigned && canSubmitInterviewFeedback(record, now)) return { kind: "feedback", label: "填写评价" };
  if (canSchedule && ["待确认", "已安排"].includes(record.status)) return { kind: "confirm", label: "确认面试" };
  if (canSchedule && record.status === "已确认") return { kind: "complete", label: "完成面试" };
  if (assigned && ["待确认", "已安排", "已确认"].includes(record.status)) return { kind: "feedback", label: "查看材料" };
  return null;
}

function scheduleCandidateKey(candidate) {
  return candidate?.applicationId || candidate?.candidateId || candidate?.id || "";
}

export function mergeScheduleCandidateOptions(candidates, pinnedCandidate) {
  const pinnedKey = scheduleCandidateKey(pinnedCandidate);
  if (!pinnedKey) return candidates;
  return [pinnedCandidate, ...candidates.filter((candidate) => scheduleCandidateKey(candidate) !== pinnedKey)];
}

export function resolveScheduleCandidateId(record, requestedCandidateId, fallbackCandidate) {
  return record?.candidateId || requestedCandidateId || fallbackCandidate?.candidateId || fallbackCandidate?.id || "";
}

export function shouldHydrateScheduleCandidate(currentCandidateId, requestedCandidateId) {
  return !currentCandidateId || currentCandidateId === requestedCandidateId;
}

export function isScheduleCandidateEligible(candidate) {
  return ["新简历", "待复核", "待沟通", "待安排", "面试中", "待决策"].includes(candidate?.stage);
}
