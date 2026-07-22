import { canPerformAction } from "./roleCapabilities.js";

const GROUP_DEFINITIONS = [
  { key: "review", label: "待用人经理评审", stage: "待复核", action: "评审候选人", tone: "red" },
  { key: "interviewPending", label: "待安排面试", stage: "待安排", action: "安排面试", tone: "orange" },
  { key: "decision", label: "待决策", stage: "待决策", action: "确认录用决策", tone: "blue" },
  { key: "passed", label: "待录用确认", stage: "已通过", action: "确认录用结果", tone: "green" },
];

export function buildWorkbenchNotificationGroups(tasks, role) {
  return GROUP_DEFINITIONS
    .filter(({ action }) => canPerformAction(role, action))
    .map((definition) => {
      const group = tasks?.[definition.key];
      const count = Number.isInteger(group?.count) && group.count > 0 ? group.count : 0;
      return { ...definition, count, items: Array.isArray(group?.items) ? group.items : [] };
    })
    .filter((group) => group.count > 0);
}

export function countWorkbenchNotifications(groups) {
  return (groups || []).reduce((total, group) => total + (Number.isInteger(group?.count) ? group.count : 0), 0);
}

export function removeWorkbenchNotification(notifications, item) {
  if (!notifications || !item?.applicationId || !item?.notificationVersion) return notifications;
  return Object.fromEntries(Object.entries(notifications).map(([key, group]) => {
    const items = Array.isArray(group?.items) ? group.items : [];
    const remaining = items.filter((candidate) => !(
      candidate.applicationId === item.applicationId
      && candidate.notificationVersion === item.notificationVersion
    ));
    if (remaining.length === items.length) return [key, group];
    return [key, { ...group, count: Math.max(0, group.count - 1), items: remaining }];
  }));
}
