import test from "node:test";
import assert from "node:assert/strict";
import { createApiClient } from "./apiClient.js";
import { createMemberOffboardingController, canOffboardUser, matchesMemberFilter, createOrganizationSettingsController } from "./organizationSettings.js";

const preview = (successor = null, version = "v1") => ({ user_id: "source", user_name: "原姓名", removed_at: null, successor_user_id: successor, handover_status: "not_started", preview_version: version, counts: [], items: [], successors: [{ id: "next", display_name: "接任人", eligible: true, reason: null }] });
const deferred = () => { let resolve; const promise = new Promise((done) => { resolve = done; }); return { promise, resolve }; };

test("offboarding API uses exact encoded endpoints, data envelope and explicit body", async () => {
  const calls = [];
  const client = createApiClient({ fetchImpl: async (url, options) => { calls.push({ url, ...options }); return new Response(JSON.stringify({ data: preview() }), { headers: { "Content-Type": "application/json" } }); } });
  assert.equal((await client.getMemberOffboarding("source/a", { successorId: "next+b" })).preview_version, "v1");
  await client.submitMemberOffboarding("source/a", { successor_user_id: null, preview_version: "v1", revoke_only: true });
  assert.equal(calls[0].url, "/api/v1/settings/users/source%2Fa/offboarding?successor_id=next%2Bb");
  assert.equal(calls[1].method, "POST");
  assert.deepEqual(JSON.parse(calls[1].body), { successor_user_id: null, preview_version: "v1", revoke_only: true });
});

test("target authorization mirrors reset matrix, guards self, supports removed retry", () => {
  const user = { id: "source", rawStatus: "active", roleValues: ["recruiter"] };
  assert.equal(canOffboardUser("招聘管理员", user, "actor"), true);
  assert.equal(canOffboardUser("招聘管理员", { ...user, roleValues: ["system_admin"] }, "actor"), false);
  assert.equal(canOffboardUser("系统管理员", user, "source"), false);
  assert.equal(canOffboardUser("HR", user, "actor"), false);
  assert.equal(canOffboardUser("系统管理员", { ...user, rawStatus: "disabled", offboarding: { handover_status: "pending" } }, "actor"), true);
  for (const rawStatus of ["active", "invited", "disabled"]) {
    const target = { ...user, rawStatus };
    assert.equal(canOffboardUser("系统管理员", target, "actor"), true);
    assert.equal(canOffboardUser("招聘管理员", target, "actor"), true);
    assert.equal(canOffboardUser("系统管理员", target, "source"), false);
    assert.equal(canOffboardUser("招聘管理员", { ...target, roleValues: ["recruiting_admin"] }, "actor"), false);
    assert.equal(canOffboardUser("HR", target, "actor"), false);
  }
  assert.equal(canOffboardUser("系统管理员", { ...user, rawStatus: "unknown" }, "actor"), false);
});

test("summary filters retain ordinary invited/disabled states and historical name", async () => {
  const controller = createOrganizationSettingsController({ client: { listUsers: async () => [{ id: "source", display_name: "原姓名", status: "disabled", offboarding: { removed_at: "2026-10-26", successor_user_id: "next", handover_status: "pending", pending_count: 2 } }, { id: "invited", status: "invited" }, { id: "disabled", status: "disabled" }], listDepartments: async () => [] } });
  await controller.load();
  const users = controller.getSnapshot().users;
  assert.equal(users[0].name, "原姓名");
  assert.equal(users[0].status, "已移除");
  assert.equal(users[1].status, "待激活");
  assert.equal(users[2].status, "停用");
  assert.deepEqual(users.filter((u) => matchesMemberFilter(u, "pending")).map((u) => u.id), ["source"]);
  assert.equal(users.filter((u) => matchesMemberFilter(u, "active")).length, 2);
  assert.equal(users.filter((u) => matchesMemberFilter(u, "all")).length, 3);
});

test("opening only reads; stale selection response cannot overwrite latest preview", async () => {
  const older = deferred();
  let writes = 0;
  const controller = createMemberOffboardingController({ client: { getMemberOffboarding: async (_, { successorId }) => successorId === "old" ? older.promise : preview(successorId), submitMemberOffboarding: async () => { writes++; } } });
  await controller.load("source");
  assert.equal(writes, 0);
  const first = controller.load("source", "old");
  await controller.load("source", "next");
  older.resolve(preview("old")); await first;
  assert.equal(controller.getSnapshot().successorId, "next");
  assert.equal(controller.getSnapshot().preview.successor_user_id, "next");
});

test("revoke only works with no eligible successor and blocks duplicate submission/cancel", async () => {
  const pending = deferred(); const calls = [];
  const controller = createMemberOffboardingController({ client: { getMemberOffboarding: async () => ({ ...preview(), successors: [] }), submitMemberOffboarding: async (_, body) => { calls.push(body); return pending.promise; } } });
  await controller.load("source");
  assert.equal(await controller.submit(false), null);
  const saving = controller.submit(true);
  assert.equal(controller.cancel(), false);
  assert.equal(await controller.submit(true), null);
  pending.resolve({ ...preview(), removed_at: "2026-10-26", handover_status: "pending", transferred_count: 0 }); await saving;
  assert.deepEqual(calls, [{ successor_user_id: null, preview_version: "v1", revoke_only: true }]);
  assert.equal(controller.getSnapshot().status, "success");
});

test("409 requires explicit refresh preserving successor and never retries a mutation", async () => {
  let writes = 0; let reads = 0;
  const controller = createMemberOffboardingController({ client: { getMemberOffboarding: async (_, { successorId }) => { reads++; return preview(successorId, `v${reads}`); }, submitMemberOffboarding: async () => { writes++; throw { status: 409, code: "offboarding_preview_changed" }; } } });
  await controller.load("source", "next");
  await controller.submit(false);
  assert.equal(controller.getSnapshot().status, "conflict");
  assert.equal(reads, 1); assert.equal(writes, 1);
  await controller.submit(false); assert.equal(writes, 1);
  await controller.refresh();
  assert.equal(controller.getSnapshot().successorId, "next");
  assert.equal(controller.getSnapshot().preview.preview_version, "v2");
});

test("revoke only retains preview successor in payload without transferring responsibilities", async () => {
  let body;
  const controller = createMemberOffboardingController({ client: { getMemberOffboarding: async (_, { successorId }) => preview(successorId), submitMemberOffboarding: async (_, value) => { body = value; return preview("next"); } } });
  await controller.load("source", "next");
  await controller.submit(true);
  assert.deepEqual(body, { successor_user_id: "next", preview_version: "v1", revoke_only: true });
});

test("summary update preserves historical identity and counts all remaining work", async () => {
  const controller = createOrganizationSettingsController({ client: { listUsers: async () => [{ id: "source", display_name: "原姓名", status: "active", roles: ["recruiter"], recruiting_scope_type: "jobs" }], listDepartments: async () => [] } });
  await controller.load();
  controller.applyOffboardingResult({ ...preview("next"), removed_at: "2026-10-26", handover_status: "pending", items: [{ status: "transferable" }, { status: "blocked" }] });
  const user = controller.getSnapshot().users[0];
  assert.equal(user.name, "原姓名");
  assert.equal(user.offboarding.pending_count, 2);
  assert.deepEqual(user.roleValues, ["recruiter"]);
  assert.equal(user.recruitingScopeType, "jobs");
});

test("resuming adopts server saved successor; failed selection retains choices for recovery", async () => {
  const controller = createMemberOffboardingController({ client: { getMemberOffboarding: async (_, { successorId }) => { if (successorId === "stale") throw { code: "offboarding_successor_invalid" }; return preview("next"); } } });
  await controller.load("source");
  assert.equal(controller.getSnapshot().successorId, "next");
  await controller.load("source", "stale");
  assert.equal(controller.getSnapshot().status, "error");
  assert.equal(controller.getSnapshot().preview.successors.length, 1);
  await controller.load("source", "next");
  assert.equal(controller.getSnapshot().status, "ready");
});

test("cancel invalidates outstanding read and failure is visible", async () => {
  const pending = deferred();
  const controller = createMemberOffboardingController({ client: { getMemberOffboarding: async () => pending.promise } });
  const loading = controller.load("source");
  assert.equal(controller.cancel(), true);
  pending.resolve(preview()); await loading;
  assert.equal(controller.getSnapshot().status, "idle");
  const failed = createMemberOffboardingController({ client: { getMemberOffboarding: async () => { throw { status: 403 }; } } });
  await failed.load("source");
  assert.match(failed.getSnapshot().error, /权限不足/);
  assert.equal(failed.getSnapshot().preview, null);
});
