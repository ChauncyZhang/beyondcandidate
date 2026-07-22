import test from "node:test";
import assert from "node:assert/strict";
import { buildWeekDays, moveWeek, weekRange } from "./interviewDateUtils.js";

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
