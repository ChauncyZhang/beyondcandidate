const RECRUITING_NAV_ITEMS = ["工作台", "职位", "筛选任务", "候选人", "面试", "人才库", "报表", "设置"];
const INTERVIEWER_NAV_ITEMS = ["工作台", "面试"];
const HIRING_MANAGER_NAV_ITEMS = ["工作台", "职位", "候选人", "面试", "报表"];
const ALL_SETTINGS_SECTIONS = ["组织与权限", "流程与评价模板", "AI 设置", "飞书集成", "审计与数据治理"];
const GOVERNANCE_VIEW_ROLES = new Set(["系统管理员", "system_admin", "招聘管理员", "recruiting_admin", "HR 招聘专员", "HR"]);
const GOVERNANCE_EDIT_ROLES = new Set(["系统管理员", "system_admin"]);
const CANDIDATE_GOVERNANCE_READ_ROLES = new Set(["招聘管理员", "recruiting_admin", "HR 招聘专员", "recruiter", "HR", "用人经理", "hiring_manager"]);
const CANDIDATE_DELETION_REQUEST_ROLES = new Set(["招聘管理员", "recruiting_admin", "HR 招聘专员", "recruiter", "HR"]);
const DELETION_APPROVAL_ROLES = new Set(["系统管理员", "system_admin"]);
const LEGAL_HOLD_ROLES = new Set(["招聘管理员", "recruiting_admin"]);

const RECRUITING_ACTIONS = [
  "导入简历",
  "新建职位",
  "编辑职位",
  "候选人搜索",
  "评审候选人",
  "确认录用决策",
  "确认录用结果",
  "安排面试",
  "提交面试反馈",
  "管理人才库",
  "查看报表",
];
const HR_RECRUITING_ACTIONS = RECRUITING_ACTIONS.filter((action) => !["评审候选人", "确认录用决策"].includes(action));

const ROLE_CAPABILITIES = {
  系统管理员: {
    identity: { name: "系统管理员", title: "系统管理员" },
    navItems: ["设置"],
    actions: [],
    settingsAccess: "完整",
  },
  招聘管理员: {
    identity: { name: "周明", title: "招聘管理员" },
    navItems: RECRUITING_NAV_ITEMS,
    actions: RECRUITING_ACTIONS,
    settingsAccess: "完整",
  },
  "HR 招聘专员": {
    identity: { name: "张小北", title: "HR 招聘专员" },
    navItems: RECRUITING_NAV_ITEMS,
    actions: HR_RECRUITING_ACTIONS,
    settingsAccess: "有限",
  },
  HR: {
    identity: { name: "张小北", title: "HR 招聘专员" },
    navItems: RECRUITING_NAV_ITEMS,
    actions: HR_RECRUITING_ACTIONS,
    settingsAccess: "有限",
  },
  用人经理: {
    identity: { name: "用人经理", title: "用人经理" },
    navItems: HIRING_MANAGER_NAV_ITEMS,
    actions: ["候选人搜索", "评审候选人", "确认录用决策", "提交面试反馈", "查看报表"],
    settingsAccess: "无",
  },
  面试官: {
    identity: { name: "王磊", title: "技术面试官" },
    navItems: INTERVIEWER_NAV_ITEMS,
    actions: ["提交面试反馈"],
    settingsAccess: "无",
  },
};

export function getAllowedNavItems(role) {
  return [...(ROLE_CAPABILITIES[role]?.navItems || [])];
}

export function canAccessNav(role, navItem) {
  return getAllowedNavItems(role).includes(navItem);
}

export function getDefaultNavItem(role) {
  return ROLE_CAPABILITIES[role]?.navItems[0] || null;
}

export function canPerformAction(role, action) {
  return ROLE_CAPABILITIES[role]?.actions.includes(action) || false;
}

export function getSettingsAccess(role) {
  return ROLE_CAPABILITIES[role]?.settingsAccess || "无";
}

export function canEditAiSettings(role) {
  return role === "系统管理员";
}

export function getAllowedSettingsSections(role) {
  if (role === "系统管理员") return ["组织与权限", "AI 设置", "飞书集成", "审计与数据治理"];
  if (["招聘管理员", "HR 招聘专员", "HR"].includes(role)) return [...ALL_SETTINGS_SECTIONS];
  return [];
}

export function canEditOrganizationSettings(role) {
  return role === "系统管理员" || role === "招聘管理员";
}

export function canEditAuditSettings(role) {
  return canEditRetentionSettings(role);
}

export function canViewAuditSettings(role) {
  return GOVERNANCE_VIEW_ROLES.has(role);
}

export function canViewRetentionSettings(role) {
  return GOVERNANCE_VIEW_ROLES.has(role);
}

export function canEditRetentionSettings(role) {
  return GOVERNANCE_EDIT_ROLES.has(role);
}

export function canReadCandidateGovernance(role) {
  return CANDIDATE_GOVERNANCE_READ_ROLES.has(role);
}

export function canRequestCandidateDeletion(role) {
  return CANDIDATE_DELETION_REQUEST_ROLES.has(role);
}

export function canViewDeletionApprovalQueue(role) {
  return DELETION_APPROVAL_ROLES.has(role);
}

export function canManageCandidateLegalHold(role) {
  return LEGAL_HOLD_ROLES.has(role);
}

export function getRoleIdentity(role) {
  const identity = ROLE_CAPABILITIES[role]?.identity;
  return identity ? { ...identity } : null;
}
