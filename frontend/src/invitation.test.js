import test from "node:test";
import assert from "node:assert/strict";
import { createApiClient } from "./apiClient.js";
import { canReinviteUser, createOrganizationSettingsController } from "./organizationSettings.js";
import { invitationAcceptErrorMessage, reinvitationErrorMessage } from "./invitationErrors.js";

test("reinvite uses encoded member id and existing credentialed CSRF request", async () => {
  const calls = [];
  const invitation = { token: "fresh", expires_at: "2026-10-02T10:00:00Z" };
  const client = createApiClient({ fetchImpl: async (url, options) => {
    calls.push({ url, options });
    return new Response(JSON.stringify({ data: url === "/api/v1/me" ? {} : { invitation } }), {
      headers: { "Content-Type": "application/json", "X-CSRF-Token": "existing-csrf" },
    });
  } });
  await client.getMe();
  assert.deepEqual(await client.reinviteUser("user /?"), { invitation });
  const { url, options } = calls[1];
  assert.equal(url, "/api/v1/settings/users/user%20%2F%3F/invitation");
  assert.equal(options.method, "POST");
  assert.equal(options.credentials, "include");
  assert.equal(options.headers.get("X-CSRF-Token"), "existing-csrf");
  assert.equal(options.body, undefined);
});

test("reinvite policy requires exact invited status and original invite role authority", () => {
  const user = { id: "u", rawStatus: "invited", roleValues: ["interviewer"] };
  assert.equal(canReinviteUser("招聘管理员", user), true);
  assert.equal(canReinviteUser("系统管理员", { ...user, roleValues: ["system_admin"] }), true);
  for (const role of ["HR", "HR 招聘专员", "面试官", "未知角色"]) assert.equal(canReinviteUser(role, user), false);
  for (const rawStatus of ["active", "disabled", "pending", "unknown"]) assert.equal(canReinviteUser("系统管理员", { ...user, rawStatus }), false);
  for (const roleValues of [["system_admin"], ["recruiting_admin"], ["interviewer", "system_admin"], [], ["unknown"]]) {
    assert.equal(canReinviteUser("招聘管理员", { ...user, roleValues }), false);
  }
});

async function controllerFor(reinviteUser, status = "invited") {
  const controller = createOrganizationSettingsController({ client: {
    listUsers: async () => [{ id: "u", roles: ["interviewer"], status }],
    listDepartments: async () => [], reinviteUser,
  } });
  await controller.load();
  return controller;
}

test("reissue keeps user data, exposes fresh one-time token, suppresses double submit and clears on dismiss", async () => {
  let resolve;
  let calls = 0;
  const controller = await controllerFor(async (id) => { assert.equal(id, "u"); calls++; return new Promise((done) => { resolve = done; }); });
  const users = controller.getSnapshot().users;
  const pending = controller.reinviteMember("u", "招聘管理员");
  assert.equal(controller.getSnapshot().actionStatus, "saving");
  assert.equal(await controller.reinviteMember("u", "招聘管理员"), null);
  assert.equal(calls, 1);
  resolve({ invitation: { token: "new-token", expires_at: "2026-10-02T10:00:00Z" } });
  assert.deepEqual(await pending, { token: "new-token", expiresAt: "2026-10-02T10:00:00Z" });
  assert.equal(controller.getSnapshot().users, users);
  assert.equal(controller.getSnapshot().actionStatus, "success");
  controller.dismissInvitation();
  assert.equal(controller.getSnapshot().invitation, null);
});

test("reinvite rejects non-invited locally and preserves actionable API failures without a stale token", async () => {
  const active = await controllerFor(() => assert.fail("must not send"), "active");
  await assert.rejects(active.reinviteMember("u", "系统管理员"), { code: "user_not_invited" });
  for (const error of [{ code: "user_not_found", status: 404 }, { code: "user_not_invited", status: 409 }, { code: "role_assignment_forbidden", status: 403 }, { kind: "unavailable" }]) {
    const controller = await controllerFor(async () => { throw error; });
    await assert.rejects(controller.reinviteMember("u", "招聘管理员"));
    assert.equal(controller.getSnapshot().actionError, reinvitationErrorMessage(error));
    assert.equal(controller.getSnapshot().invitation, null);
    assert.equal(controller.getSnapshot().actionStatus, "error");
  }
});

test("only the invitation-specific acceptance code claims invalid or expired", () => {
  assert.match(invitationAcceptErrorMessage({ code: "invitation_invalid_or_expired", status: 422 }), /过期/);
  for (const error of [{ code: "csrf_validation_failed", status: 403 }, { status: 422 }, { status: 429 }, { status: 400 }, { status: 404 }, { kind: "unavailable" }]) {
    assert.doesNotMatch(invitationAcceptErrorMessage(error), /过期|邀请无效/);
  }
  assert.match(invitationAcceptErrorMessage({ status: 422 }), /密码/);
  assert.match(invitationAcceptErrorMessage({ kind: "unavailable" }), /服务/);
});

test("duplicate creation guides to the existing pending row without creating another user", async () => {
  const controller = createOrganizationSettingsController({ client: { inviteUser: async () => { throw { code: "user_email_already_exists", status: 409 }; } } });
  await assert.rejects(controller.inviteMember({ email: "existing@example.test", role: "interviewer" }));
  assert.match(controller.getSnapshot().actionError, /成员列表.*搜索.*待激活.*重新邀请/);
  assert.equal(controller.getSnapshot().invitation, null);
  assert.deepEqual(controller.getSnapshot().users, []);
});
