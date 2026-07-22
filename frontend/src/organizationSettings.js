import { apiClient } from "./apiClient.js";

const ROLE_LABELS = new Map([
  ["system_admin", "系统管理员"],
  ["recruiting_admin", "招聘管理员"],
  ["recruiter", "HR 招聘专员"],
  ["hiring_manager", "用人经理"],
  ["interviewer", "面试官"],
]);
const ALL_INVITE_ROLES = [...ROLE_LABELS].map(([value, label]) => ({ value, label }));
const RECRUITING_ADMIN_INVITE_ROLES = ALL_INVITE_ROLES.filter(({ value }) => ["recruiter", "hiring_manager", "interviewer"].includes(value));

function safeString(value) {
  return typeof value === "string" ? value.trim() : "";
}

function safeCount(value) {
  return Number.isInteger(value) && value >= 0 ? value : 0;
}

function normalizeDepartment(value) {
  return {
    id: safeString(value?.id),
    name: safeString(value?.name),
    parentId: safeString(value?.parent_id) || null,
    status: value?.status === "inactive" ? "inactive" : "active",
    memberCount: safeCount(value?.member_count),
    jobCount: safeCount(value?.job_count),
  };
}

function normalizeDepartmentDetail(value) {
  const department = normalizeDepartment(value);
  return {
    ...department,
    members: Array.isArray(value?.members) ? value.members.map((member) => ({
      id: safeString(member?.id),
      name: safeString(member?.name),
      roles: Array.isArray(member?.roles) ? member.roles.map((role) => ROLE_LABELS.get(role) || safeString(role)).filter(Boolean) : [],
      status: member?.status === "active" ? "启用" : member?.status === "invited" ? "待激活" : "停用",
    })).filter((member) => member.id) : [],
    jobs: Array.isArray(value?.jobs) ? value.jobs.map((job) => ({
      id: safeString(job?.id),
      name: safeString(job?.title),
      status: safeString(job?.status),
    })).filter((job) => job.id) : [],
  };
}

function normalizeUser(value) {
  const roleValues = Array.isArray(value?.roles) ? value.roles : [];
  const roles = roleValues.map((role) => ROLE_LABELS.get(role) || safeString(role)).filter(Boolean);
  const status = value?.status === "invited" || value?.status === "pending"
    ? "待激活"
    : value?.status === "active" ? "启用" : value?.status === "disabled" ? "停用" : safeString(value?.status) || "未知";
  return {
    id: safeString(value?.id),
    name: safeString(value?.display_name),
    email: safeString(value?.email),
    departmentId: safeString(value?.department_id),
    department: safeString(value?.department_name) || "未分配部门",
    roles,
    role: roles.join("、") || "未分配角色",
    status,
  };
}

export function getInviteRoleOptions(currentRole) {
  return currentRole === "系统管理员" ? ALL_INVITE_ROLES.map((item) => ({ ...item })) : RECRUITING_ADMIN_INVITE_ROLES.map((item) => ({ ...item }));
}

export function createOrganizationSettingsController({ client = apiClient, createIdempotencyKey = () => globalThis.crypto.randomUUID() } = {}) {
  let state = Object.freeze({ status: "idle", users: [], departments: [], error: "", actionStatus: "idle", actionError: "", invitation: null, departmentDetailStatus: "idle", departmentDetail: null });
  const listeners = new Set();
  const setState = (next) => {
    state = Object.freeze(next);
    listeners.forEach((listener) => listener());
  };
  const patchState = (patch) => setState({ ...state, ...patch });

  return {
    getSnapshot: () => state,
    subscribe(listener) { listeners.add(listener); return () => listeners.delete(listener); },
    async load() {
      patchState({ status: "loading", error: "" });
      try {
        const [users, departments] = await Promise.all([client.listUsers(), client.listDepartments()]);
        patchState({ status: "ready", users: users.map(normalizeUser), departments: departments.map(normalizeDepartment), error: "" });
      } catch (error) {
        patchState({ status: "error", error: error?.kind === "unavailable" ? "组织信息暂时无法加载，请稍后重试。" : "组织信息加载失败，请检查权限后重试。" });
      }
      return state;
    },
    async inviteMember(form) {
      patchState({ actionStatus: "saving", actionError: "", invitation: null });
      try {
        const result = await client.inviteUser({
          display_name: safeString(form?.displayName),
          email: safeString(form?.email),
          department_id: safeString(form?.departmentId),
          role: safeString(form?.role),
        }, { idempotencyKey: createIdempotencyKey() });
        const invitation = { token: safeString(result?.invitation?.token), expiresAt: safeString(result?.invitation?.expires_at) };
        patchState({ actionStatus: "success", users: [normalizeUser(result?.user), ...state.users.filter((user) => user.id !== result?.user?.id)], invitation });
        return invitation;
      } catch (error) {
        patchState({ actionStatus: "error", actionError: error?.kind === "unavailable" ? "邀请暂时无法发送，请稍后重试。" : "邀请发送失败，请核对信息后重试。" });
        throw error;
      }
    },
    async addDepartment(name) {
      patchState({ actionStatus: "saving", actionError: "" });
      try {
        const department = normalizeDepartment(await client.createDepartment({ name: safeString(name), parent_id: null }));
        patchState({ actionStatus: "success", departments: [...state.departments, department] });
        return department;
      } catch (error) {
        patchState({ actionStatus: "error", actionError: error?.kind === "unavailable" ? "部门暂时无法创建，请稍后重试。" : "部门创建失败，请核对名称后重试。" });
        throw error;
      }
    },
    async loadDepartment(id) {
      patchState({ departmentDetailStatus: "loading", departmentDetail: null, actionError: "" });
      try {
        const departmentDetail = normalizeDepartmentDetail(await client.getDepartment(id));
        patchState({ departmentDetailStatus: "ready", departmentDetail });
        return departmentDetail;
      } catch (error) {
        patchState({ departmentDetailStatus: "error", departmentDetail: null, actionError: "部门详情加载失败，请稍后重试。" });
        throw error;
      }
    },
    async updateDepartment(id, changes) {
      patchState({ actionStatus: "saving", actionError: "" });
      try {
        const departmentDetail = normalizeDepartmentDetail(await client.updateDepartment(id, changes));
        patchState({
          actionStatus: "success",
          departmentDetailStatus: "ready",
          departmentDetail,
          departments: state.departments.map((department) => department.id === departmentDetail.id ? {
            id: departmentDetail.id,
            name: departmentDetail.name,
            parentId: departmentDetail.parentId,
            status: departmentDetail.status,
            memberCount: departmentDetail.memberCount,
            jobCount: departmentDetail.jobCount,
          } : department),
        });
        return departmentDetail;
      } catch (error) {
        patchState({ actionStatus: "error", actionError: error?.kind === "unavailable" ? "部门暂时无法更新，请稍后重试。" : "部门更新失败，请核对名称后重试。" });
        throw error;
      }
    },
    clearDepartment() { patchState({ departmentDetailStatus: "idle", departmentDetail: null, actionError: "", actionStatus: "idle" }); },
    dismissInvitation() { patchState({ invitation: null, actionStatus: "idle", actionError: "" }); },
  };
}

export const organizationSettingsController = createOrganizationSettingsController();
