import test from "node:test";
import assert from "node:assert/strict";
import { createOrganizationSettingsController, getInviteRoleOptions, getRecruitingScopeLabel, isRecruitingScopeValid } from "./organizationSettings.js";

test("limits recruiting administrators to roles they are allowed to invite", () => {
  assert.deepEqual(getInviteRoleOptions("招聘管理员"), [
    { value: "recruiter", label: "HR 招聘专员" },
    { value: "hiring_manager", label: "用人经理" },
    { value: "interviewer", label: "面试官" },
  ]);
  assert.deepEqual(getInviteRoleOptions("系统管理员").map((item) => item.value), [
    "system_admin", "recruiting_admin", "recruiter", "hiring_manager", "interviewer",
  ]);
});

test("loads server-backed members and departments without seed data", async () => {
  const client = {
    async listUsers() {
      return [{ id: "user-1", display_name: "林岚", email: "lin@example.test", department_id: "dep-1", department_name: "技术部", roles: ["recruiter"], status: "invited" }];
    },
    async listDepartments() {
      return [{ id: "dep-1", name: "技术部", parent_id: null, member_count: 6, job_count: 3 }];
    },
  };
  const controller = createOrganizationSettingsController({ client });

  await controller.load();

  assert.deepEqual(controller.getSnapshot().users, [{
    id: "user-1", name: "林岚", email: "lin@example.test", departmentId: "dep-1", department: "技术部", roleValues: ["recruiter"], roles: ["HR 招聘专员"], role: "HR 招聘专员", recruitingScopeType: "jobs", recruitingDepartmentIds: [], status: "待激活",
  }]);
  assert.deepEqual(controller.getSnapshot().departments, [{ id: "dep-1", name: "技术部", parentId: null, status: "active", memberCount: 6, jobCount: 3 }]);
  assert.equal(controller.getSnapshot().status, "ready");
});

test("formats recruiting scope only for recruiting administrators and recruiters", () => {
  assert.equal(getRecruitingScopeLabel({ roleValues: ["recruiting_admin"], recruitingScopeType: "jobs", recruitingDepartmentIds: [] }), "全公司");
  assert.equal(getRecruitingScopeLabel({ roleValues: ["recruiter"], recruitingScopeType: "jobs", recruitingDepartmentIds: [] }), "指定职位");
  assert.equal(getRecruitingScopeLabel({ roleValues: ["recruiter"], recruitingScopeType: "departments", recruitingDepartmentIds: ["dep-1", "dep-2"] }), "2 个部门");
  assert.equal(getRecruitingScopeLabel({ roleValues: ["recruiter"], recruitingScopeType: "organization", recruitingDepartmentIds: [] }), "全公司");
  assert.equal(getRecruitingScopeLabel({ roleValues: ["hiring_manager"], recruitingScopeType: "organization", recruitingDepartmentIds: [] }), "-");
  assert.equal(isRecruitingScopeValid("departments", []), false);
  assert.equal(isRecruitingScopeValid("departments", ["dep-1"]), true);
});

test("invites an invited-status member with a fresh idempotency key and exposes the one-time token", async () => {
  let received;
  const client = {
    async inviteUser(body, options) {
      received = { body, options };
      return {
        user: { id: "user-2", display_name: "周宁", email: "zhou@example.test", department_id: "dep-1", department_name: "技术部", roles: ["interviewer"], status: "invited" },
        invitation: { token: "invite-once", expires_at: "2026-07-18T08:00:00Z" },
      };
    },
  };
  const controller = createOrganizationSettingsController({ client, createIdempotencyKey: () => "invite-key-1" });

  const invitation = await controller.inviteMember({ displayName: " 周宁 ", email: " zhou@example.test ", departmentId: "dep-1", role: "interviewer" });

  assert.deepEqual(received, {
    body: { display_name: "周宁", email: "zhou@example.test", department_id: "dep-1", role: "interviewer" },
    options: { idempotencyKey: "invite-key-1" },
  });
  assert.equal(controller.getSnapshot().users[0].status, "待激活");
  assert.deepEqual(invitation, { token: "invite-once", expiresAt: "2026-07-18T08:00:00Z" });
  controller.dismissInvitation();
  assert.equal(controller.getSnapshot().invitation, null);
});

test("invites a recruiter with an explicit department recruiting scope", async () => {
  let received;
  const client = {
    async inviteUser(body) {
      received = body;
      return {
        user: { id: "user-3", display_name: "陈晨", email: "chen@example.test", department_id: "dep-1", department_name: "技术部", roles: ["recruiter"], status: "invited", recruiting_scope_type: "departments", recruiting_department_ids: ["dep-2"] },
        invitation: { token: "invite-recruiter", expires_at: "2026-07-18T08:00:00Z" },
      };
    },
  };
  const controller = createOrganizationSettingsController({ client, createIdempotencyKey: () => "invite-key-2" });

  await controller.inviteMember({ displayName: "陈晨", email: "chen@example.test", departmentId: "dep-1", role: "recruiter", recruitingScopeType: "departments", recruitingDepartmentIds: ["dep-2", "dep-2"] });

  assert.deepEqual(received, {
    display_name: "陈晨",
    email: "chen@example.test",
    department_id: "dep-1",
    role: "recruiter",
    recruiting_scope_type: "departments",
    recruiting_department_ids: ["dep-2"],
  });
  assert.equal(controller.getSnapshot().users[0].recruitingScopeType, "departments");
});

test("rejects an empty department scope before sending and updates the member list after save", async () => {
  let updateBody;
  const client = {
    async listUsers() {
      return [{ id: "user-1", display_name: "林岚", email: "lin@example.test", department_id: "dep-1", department_name: "技术部", roles: ["recruiter"], status: "active", recruiting_scope_type: "jobs", recruiting_department_ids: [] }];
    },
    async listDepartments() { return []; },
    async inviteUser() { assert.fail("invalid scope must not be sent"); },
    async updateRecruitingScope(id, body) {
      assert.equal(id, "user-1");
      updateBody = body;
      return { id, display_name: "林岚", email: "lin@example.test", department_id: "dep-1", department_name: "技术部", roles: ["recruiter"], status: "active", ...body };
    },
  };
  const controller = createOrganizationSettingsController({ client });
  await controller.load();

  await assert.rejects(() => controller.inviteMember({ role: "recruiter", recruitingScopeType: "departments", recruitingDepartmentIds: [] }), { code: "RECRUITING_DEPARTMENT_REQUIRED" });
  assert.equal(controller.getSnapshot().actionError, "负责招聘部门至少选择一项");

  await controller.updateRecruitingScope("user-1", { recruitingScopeType: "organization", recruitingDepartmentIds: ["dep-1"] });
  assert.deepEqual(updateBody, { recruiting_scope_type: "organization", recruiting_department_ids: [] });
  assert.equal(getRecruitingScopeLabel(controller.getSnapshot().users[0]), "全公司");
});

test("creates a root department and appends the server resource", async () => {
  let received;
  const client = {
    async createDepartment(body) {
      received = body;
      return { id: "dep-2", name: "产品部", parent_id: null, member_count: 0, job_count: 0 };
    },
  };
  const controller = createOrganizationSettingsController({ client });

  const department = await controller.addDepartment(" 产品部 ");

  assert.deepEqual(received, { name: "产品部", parent_id: null });
  assert.deepEqual(department, { id: "dep-2", name: "产品部", parentId: null, status: "active", memberCount: 0, jobCount: 0 });
});

test("loads department details and keeps renamed inactive department in the directory", async () => {
  const client = {
    async getDepartment(id) {
      assert.equal(id, "dep-1");
      return {
        id, name: "技术部", status: "active", member_count: 1, job_count: 1,
        members: [{ id: "user-1", name: "林岚", roles: ["recruiting_admin"], status: "active" }],
        jobs: [{ id: "job-1", title: "平台工程师", status: "open" }],
      };
    },
    async updateDepartment(id, body) {
      assert.equal(id, "dep-1");
      assert.deepEqual(body, { name: "平台部", status: "inactive" });
      return {
        id, name: "平台部", status: "inactive", member_count: 1, job_count: 1,
        members: [{ id: "user-1", name: "林岚", roles: ["recruiting_admin"], status: "active" }],
        jobs: [{ id: "job-1", title: "平台工程师", status: "open" }],
      };
    },
  };
  const controller = createOrganizationSettingsController({ client });

  const detail = await controller.loadDepartment("dep-1");
  assert.equal(detail.members[0].roles[0], "招聘管理员");
  assert.equal(detail.jobs[0].name, "平台工程师");

  const updated = await controller.updateDepartment("dep-1", { name: "平台部", status: "inactive" });
  assert.equal(updated.status, "inactive");
  assert.equal(controller.getSnapshot().departmentDetail.name, "平台部");
});
