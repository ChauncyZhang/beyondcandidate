import test from "node:test";
import assert from "node:assert/strict";
import { buildWeekDays, moveWeek, selectUpcomingInterviews, upcomingInterviewWindow, weekRange } from "./interviewDateUtils.js";

test("builds a Monday-to-Sunday week for any reference date", () => {
  const days = buildWeekDays(new Date(2026, 6, 16, 12));
  assert.deepEqual(days.map((day) => day.key), [
    "2026-07-13", "2026-07-14", "2026-07-15", "2026-07-16", "2026-07-17", "2026-07-18", "2026-07-19",
  ]);
  assert.equal(days[0].weekday, "周一");
  assert.equal(days[6].weekday, "周日");
});

test("moves across arbitrary future weeks and exposes an inclusive API range", () => {
  const future = moveWeek(new Date(2026, 6, 16, 12), 7);
  assert.equal(future.getFullYear(), 2026);
  assert.equal(future.getMonth(), 7);
  assert.deepEqual(weekRange(future), { from: "2026-08-31", to: "2026-09-06" });
});

test("builds an exact rolling seven-day interview query window", () => {
  const window = upcomingInterviewWindow(new Date("2026-09-02T08:00:00Z"));
  assert.equal(window.from, "2026-09-02T08:00:00.000Z");
  assert.equal(window.to, "2026-09-09T08:00:00.000Z");
});

test("selects only active interviews overlapping the next seven days and sorts them", () => {
  const records = [
    { id: "past", status: "已安排", startsAt: "2026-07-25T03:30:00Z", endsAt: "2026-07-25T04:30:00Z" },
    { id: "later", status: "已确认", startsAt: "2026-09-06T02:00:00Z", endsAt: "2026-09-06T03:00:00Z" },
    { id: "ongoing", status: "已安排", startsAt: "2026-09-02T07:30:00Z", endsAt: "2026-09-02T08:30:00Z" },
    { id: "cancelled", status: "已取消", startsAt: "2026-09-03T02:00:00Z", endsAt: "2026-09-03T03:00:00Z" },
    { id: "boundary", status: "已安排", startsAt: "2026-09-09T08:00:00Z", endsAt: "2026-09-09T09:00:00Z" },
    { id: "invalid", status: "已安排", startsAt: "", endsAt: "" },
  ];

  assert.deepEqual(
    selectUpcomingInterviews(records, new Date("2026-09-02T08:00:00Z")).map((record) => record.id),
    ["ongoing", "later"],
  );
});

test("awaiting evaluation is not listed as an upcoming interview", () => {
  const record = { id: "feedback", status: "待评价", startsAt: "2026-09-02T08:00:00Z", endsAt: "2026-09-02T09:00:00Z" };
  assert.deepEqual(selectUpcomingInterviews([record], new Date("2026-09-02T08:30:00Z")), []);
});
