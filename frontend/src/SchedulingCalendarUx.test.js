import test from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";

const scheduleSource = readFileSync(new URL("./ScheduleWorkspace.jsx", import.meta.url), "utf8");
const calendarSource = readFileSync(new URL("./InterviewCalendar.jsx", import.meta.url), "utf8");
const viewsSource = readFileSync(new URL("./InterviewViews.jsx", import.meta.url), "utf8");

test("scheduling starts with candidate round and duration without guessing a time", () => {
  assert.match(scheduleSource, /候选人与轮次/);
  assert.match(scheduleSource, /选择面试官与忙闲/);
  assert.match(scheduleSource, /选择日期时间/);
  assert.match(scheduleSource, /确认邀请/);
  assert.match(scheduleSource, /time:\s*record\?\.time\s*\|\|\s*""/);
});

test("availability is privacy safe in the UI and final save rechecks conflicts", () => {
  assert.match(scheduleSource, /已有安排/);
  assert.match(scheduleSource, /可排/);
  assert.match(scheduleSource, /冲突/);
  assert.match(scheduleSource, /缓冲不足/);
  assert.match(scheduleSource, /无法确认/);
  assert.match(scheduleSource, /await onCheckConflicts/);
});

test("calendar owns complete range loading and full week navigation", () => {
  assert.match(calendarSource, /onLoadRange/);
  assert.match(calendarSource, /上一周/);
  assert.match(calendarSource, /下一周/);
  assert.match(calendarSource, />今天</);
  assert.match(calendarSource, /type="date"/);
  assert.match(calendarSource, /mobile-date-strip/);
  assert.match(calendarSource, /selectedDay/);
});

test("InterviewViews delegates scheduling and calendar without moving feedback", () => {
  assert.match(viewsSource, /ScheduleWorkspace/);
  assert.match(viewsSource, /InterviewCalendar/);
  assert.doesNotMatch(viewsSource, /function ScheduleInterview/);
  assert.match(viewsSource, /function FeedbackForm/);
});
