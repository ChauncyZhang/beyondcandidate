import test from "node:test";
import assert from "node:assert/strict";
import { createApiClient } from "./apiClient.js";
import { createOrganizationSettingsController } from "./organizationSettings.js";

test("password reset uses a separate credentialed CSRF endpoint and encoded member id", async () => {
  const calls = [];
  const client = createApiClient({ fetchImpl: async (url, options) => {
    calls.push({ url, options });
    return new Response(JSON.stringify({ data: { password_reset: { token: "reset-token", expires_at: "2026-10-21T11:00:00Z" } } }), {
      headers: { "Content-Type": "application/json", "X-CSRF-Token": "existing-csrf" },
    });
  } });
  await client.getMe();
  await client.requestPasswordReset("user /?");
  assert.equal(calls[1].url, "/api/v1/settings/users/user%20%2F%3F/password-reset");
  assert.equal(calls[1].options.method, "POST");
  assert.equal(calls[1].options.credentials, "include");
  assert.equal(calls[1].options.headers.get("X-CSRF-Token"), "existing-csrf");
  await client.acceptPasswordReset({ token: "reset-token", password: "new-password-123" });
  assert.equal(calls[2].url, "/api/v1/auth/password-reset/accept");
  assert.deepEqual(JSON.parse(calls[2].options.body), { token: "reset-token", password: "new-password-123" });
});

async function controllerFor(reset, status = "active", roles = ["interviewer"]) {
  const controller = createOrganizationSettingsController({ client: {
    listUsers: async () => [{ id: "target", roles, status }],
    listDepartments: async () => [], requestPasswordReset: reset,
  } });
  await controller.load();
  return controller;
}

test("reset link generation preserves members, suppresses duplicate clicks and clears on dismiss", async () => {
  let release;
  let calls = 0;
  const controller = await controllerFor(async (id) => { assert.equal(id, "target"); calls++; return new Promise((resolve) => { release = resolve; }); });
  const users = controller.getSnapshot().users;
  const pending = controller.resetMemberPassword("target", "招聘管理员", "admin");
  assert.equal(controller.getSnapshot().actionStatus, "saving");
  assert.equal(await controller.resetMemberPassword("target", "招聘管理员", "admin"), null);
  assert.equal(calls, 1);
  release({ password_reset: { token: "reset-token", expires_at: "2026-10-21T11:00:00Z" } });
  assert.deepEqual(await pending, { token: "reset-token", expiresAt: "2026-10-21T11:00:00Z" });
  assert.equal(controller.getSnapshot().users, users);
  assert.equal(controller.getSnapshot().invitation, null);
  controller.dismissInvitation();
  assert.equal(controller.getSnapshot().passwordReset, null);
});

test("reset locally rejects inactive, privileged, self and read-only targets without external calls", async () => {
  for (const [status, roles, role, actor] of [
    ["invited", ["interviewer"], "招聘管理员", "admin"],
    ["disabled", ["interviewer"], "系统管理员", "admin"],
    ["active", ["system_admin"], "招聘管理员", "admin"],
    ["active", ["interviewer"], "HR 招聘专员", "admin"],
    ["active", ["interviewer"], "系统管理员", "target"],
  ]) {
    const controller = await controllerFor(() => assert.fail("must not request reset"), status, roles);
    await assert.rejects(controller.resetMemberPassword("target", role, actor));
    assert.equal(controller.getSnapshot().passwordReset, null);
  }
});

test("failed reset generation exposes a safe actionable error without leaking server details", async () => {
  const controller = await controllerFor(async () => { throw { status: 409, code: "user_not_active", detail: "private detail" }; });
  await assert.rejects(controller.resetMemberPassword("target", "招聘管理员", "admin"));
  assert.match(controller.getSnapshot().actionError, /刷新成员列表/);
  assert.doesNotMatch(controller.getSnapshot().actionError, /private/);
  assert.equal(controller.getSnapshot().passwordReset, null);
});
