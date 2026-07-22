import test from "node:test";
import assert from "node:assert/strict";
import * as interviewViewState from "./interviewViewState.js";

const { buildWorkweekColumns, canSubmitInterviewFeedback, getInterviewPrimaryAction, getLocalDateInputMin, isInterviewStartStrictlyFuture, isInWorkweek, isMyInterview, isScheduleCandidateEligible, mergeScheduleCandidateOptions, resolveScheduleCandidateId, shouldHydrateScheduleCandidate } = interviewViewState;

test("workweek columns follow the current Monday through Friday across month boundaries", () => {
  const columns = buildWorkweekColumns(new Date("2026-08-01T09:00:00+08:00"));
  assert.deepEqual(columns.map((item) => item[0]), ["2026-07-27", "2026-07-28", "2026-07-29", "2026-07-30", "2026-07-31"]);
  assert.equal(isInWorkweek("2026-07-31", columns), true);
  assert.equal(isInWorkweek("2026-08-01", columns), false);
});

test("my interview filtering uses the immutable user id instead of a display name", () => {
  const record = { interviewerIds: ["user-1"], interviewers: ["同名用户"] };
  assert.equal(isMyInterview(record, "user-1"), true);
  assert.equal(isMyInterview(record, "user-2"), false);
  assert.equal(isMyInterview(record, ""), false);
});

test("a candidate opened from detail takes precedence over a fallback candidate", () => {
  const fallback = { id: "fallback-candidate", candidateId: "fallback-candidate" };

  assert.equal(resolveScheduleCandidateId(null, "requested-candidate", fallback), "requested-candidate");
  assert.equal(resolveScheduleCandidateId({ candidateId: "record-candidate" }, "requested-candidate", fallback), "record-candidate");
});

test("async candidate hydration preserves only an explicit user selection", () => {
  assert.equal(shouldHydrateScheduleCandidate("", "requested-candidate"), true);
  assert.equal(shouldHydrateScheduleCandidate("requested-candidate", "requested-candidate"), true);
  assert.equal(shouldHydrateScheduleCandidate("user-selected-candidate", "requested-candidate"), false);
});

test("the candidate opened from detail remains visible ahead of server suggestions", () => {
  const pinned = { id: "candidate-1", applicationId: "application-1", stage: "新简历" };
  const suggestions = [
    { id: "candidate-2", applicationId: "application-2", stage: "待安排" },
    { id: "candidate-1", applicationId: "application-1", stage: "待安排" },
  ];

  assert.deepEqual(mergeScheduleCandidateOptions(suggestions, pinned), [pinned, suggestions[0]]);
});

test("active applications and decision-stage extra rounds can continue to scheduling", () => {
  assert.equal(isScheduleCandidateEligible({ stage: "新简历" }), true);
  assert.equal(isScheduleCandidateEligible({ stage: "待复核" }), true);
  assert.equal(isScheduleCandidateEligible({ stage: "待沟通" }), true);
  assert.equal(isScheduleCandidateEligible({ stage: "待安排" }), true);
  assert.equal(isScheduleCandidateEligible({ stage: "面试中" }), true);
  assert.equal(isScheduleCandidateEligible({ stage: "待决策" }), true);
});

test("an assigned hiring manager can submit feedback for a future scheduled interview", () => {
  const record = { interviewerIds: ["manager-1"], status: "已安排", feedbackStatus: "未开始", startsAt: "2026-07-16T04:00:00Z" };

  assert.deepEqual(
    getInterviewPrimaryAction(record, { canSchedule: false, userId: "manager-1", now: new Date("2026-07-16T03:00:00Z") }),
    { kind: "feedback", label: "填写评价" },
  );
  assert.equal(canSubmitInterviewFeedback(record, new Date("2026-07-16T03:00:00Z")), true);
});

test("an assigned hiring manager can submit feedback for a future confirmed interview", () => {
  const record = { interviewerIds: ["manager-1"], status: "已确认", feedbackStatus: "未开始", startsAt: "2026-07-17T04:00:00Z" };

  assert.deepEqual(
    getInterviewPrimaryAction(record, { canSchedule: false, userId: "manager-1", now: new Date("2026-07-16T03:00:00Z") }),
    { kind: "feedback", label: "填写评价" },
  );
  assert.equal(canSubmitInterviewFeedback(record, new Date("2026-07-16T03:00:00Z")), true);
});

test("schedule dates use the browser-local current day as their minimum", () => {
  assert.equal(typeof getLocalDateInputMin, "function");
  assert.equal(getLocalDateInputMin(new Date(2026, 6, 16, 23, 59)), "2026-07-16");
});

test("schedule date and time must be strictly later than now", () => {
  const now = new Date(2026, 6, 16, 10, 30, 0);

  assert.equal(typeof isInterviewStartStrictlyFuture, "function");
  assert.equal(isInterviewStartStrictlyFuture("2026-07-16", "10:45", now), true);
  assert.equal(isInterviewStartStrictlyFuture("2026-07-16", "10:30", now), false);
  assert.equal(isInterviewStartStrictlyFuture("2026-07-16", "10:15", now), false);
  assert.equal(isInterviewStartStrictlyFuture("", "10:45", now), false);
});

test("recruiting management actions remain available to an unassigned administrator", () => {
  const scheduled = { interviewerIds: ["interviewer-1"], status: "已安排", feedbackStatus: "未开始", startsAt: "2026-07-16T02:00:00Z" };
  const confirmed = { ...scheduled, status: "已确认" };

  assert.deepEqual(getInterviewPrimaryAction(scheduled, { canSchedule: true, userId: "admin-1", now: new Date("2026-07-16T03:00:00Z") }), { kind: "confirm", label: "确认面试" });
  assert.deepEqual(getInterviewPrimaryAction(confirmed, { canSchedule: true, userId: "admin-1", now: new Date("2026-07-16T03:00:00Z") }), { kind: "complete", label: "完成面试" });
});
