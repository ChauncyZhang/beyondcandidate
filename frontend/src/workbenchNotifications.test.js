import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import test from "node:test";

import { buildWorkbenchNotificationGroups, countWorkbenchNotifications, removeWorkbenchNotification } from "./workbenchNotifications.js";

const candidate = { applicationId: "application-1", notificationVersion: "a".repeat(64), name: "李嘉明", position: "AI 工程师", city: "北京" };

const tasks = {
  review: { count: 2, items: [candidate] },
  interviewPending: { count: 3, items: [{ ...candidate, applicationId: "application-2" }] },
  decision: { count: 1, items: [{ ...candidate, applicationId: "application-3" }] },
  passed: { count: 4, items: [{ ...candidate, applicationId: "application-4" }] },
};

test("notification groups expose only actionable work for the current role", () => {
  const hrGroups = buildWorkbenchNotificationGroups(tasks, "HR 招聘专员");
  assert.deepEqual(hrGroups.map(({ key, label, stage, count }) => ({ key, label, stage, count })), [
    { key: "interviewPending", label: "待安排面试", stage: "待安排", count: 3 },
    { key: "passed", label: "待录用确认", stage: "已通过", count: 4 },
  ]);
  assert.equal(countWorkbenchNotifications(hrGroups), 7);

  const managerGroups = buildWorkbenchNotificationGroups(tasks, "用人经理");
  assert.deepEqual(managerGroups.map((group) => group.key), ["review", "decision"]);
  assert.equal(countWorkbenchNotifications(managerGroups), 3);
});

test("notification groups ignore empty and malformed task counts", () => {
  const groups = buildWorkbenchNotificationGroups({
    review: { count: -1, items: null },
    interviewPending: { count: 0, items: [candidate] },
    decision: { count: "2", items: [candidate] },
    passed: { count: 1, items: [candidate] },
  }, "招聘管理员");

  assert.deepEqual(groups.map((group) => group.key), ["passed"]);
  assert.deepEqual(groups[0].items, [candidate]);
  assert.equal(countWorkbenchNotifications(groups), 1);
});

test("read notification disappears without changing the business task", () => {
  const notifications = {
    review: { count: 2, items: [candidate, { ...candidate, applicationId: "application-2", notificationVersion: "b".repeat(64) }] },
    interviewPending: { count: 0, items: [] },
    decision: { count: 0, items: [] },
    passed: { count: 0, items: [] },
  };
  const businessTasks = structuredClone(notifications);

  assert.deepEqual(removeWorkbenchNotification(notifications, candidate), {
    ...notifications,
    review: { count: 1, items: [{ ...candidate, applicationId: "application-2", notificationVersion: "b".repeat(64) }] },
  });
  assert.equal(businessTasks.review.count, 2);
});

test("notification menu is an operable accessible popover", () => {
  const source = readFileSync(new URL("./NotificationMenu.jsx", import.meta.url), "utf8");
  assert.match(source, /aria-expanded=\{open\}/);
  assert.match(source, /role="dialog"/);
  assert.match(source, /event\.key === "Escape"/);
  assert.match(source, /onOpenCandidate\(item\)/);
  assert.match(source, /onOpenGroup\(group\.stage\)/);
});

test("review notifications use the dedicated workbench review navigation", () => {
  const appSource = readFileSync(new URL("./App.jsx", import.meta.url), "utf8");
  assert.match(appSource, /<NotificationMenu[^>]*onOpenCandidate=\{openWorkbenchNotificationCandidate\}/);
  assert.match(appSource, /workbenchController\.markNotificationRead\(candidate/);
  assert.match(appSource, /removeWorkbenchNotification\(current\.data\.notifications, candidate\)/);
  assert.match(appSource, /candidate\.taskId \? openWorkbenchReviewCandidate\(candidate\) : openCandidate\(candidate\)/);
});

test("manager review rows and notifications render the controller AI availability label", () => {
  const appSource = readFileSync(new URL("./App.jsx", import.meta.url), "utf8");
  const menuSource = readFileSync(new URL("./NotificationMenu.jsx", import.meta.url), "utf8");
  assert.match(appSource, /candidate\.aiLabel && <small[^>]*>\{candidate\.aiLabel\}<\/small>/);
  assert.match(menuSource, /item\.aiLabel && <small[^>]*>\{item\.aiLabel\}<\/small>/);
});
