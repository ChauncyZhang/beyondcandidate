import test from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";

import { ApiError } from "./apiClient.js";

const governance = await import("./governanceSettings.js").catch(() => ({}));
const {
  buildAuditLogsPath,
  createGovernanceSettingsController,
  getGovernanceErrorMessage,
  normalizeAuditRecord,
  normalizeDeletionRequest,
  buildDeletionRequestsPath,
  normalizeRetentionPolicy,
  releaseGovernanceSettingsSubscription,
} = governance;

const deletionCounts = {
  contacts: 1, resumes: 2, applications: 3, screening_records: 4, interviews: 5,
  feedback_records: 6, talent_memberships: 7, resume_objects: 8, temporary_exports: 9,
};
const deletionRequest = {
  id: "22222222-2222-4222-8222-222222222222", status: "requested", version: 4,
  reason_code: "administrator_request", requested_at: "2026-07-15T01:00:00Z", approved_at: null,
  safe_error_code: null, active_application_count: 0, impact: { schema_version: 1, candidate_ref: "11111111-1111-4111-8111-111111111111", candidate_version: 8, policy_version: 3, counts: deletionCounts, backup_window_ends_at: "2026-08-15T01:00:00Z" },
};

const policy = {
  id: "policy-1",
  version: 7,
  terminal_days: 730,
  talent_pool_days: 1095,
  backup_window_days: 90,
  updated_at: "2026-07-14T01:00:00Z",
  updated_by: { id: "user-1", display_name: "系统管理员", email: "private@example.com" },
  raw_metadata: { secret: true },
};
const previewNow = () => new Date("2026-07-14T02:00:00Z");

function requireFunction(value, name) {
  assert.equal(typeof value, "function", `${name} should be implemented`);
}

function createClient(handler) {
  const calls = [];
  return {
    calls,
    async request(path, options = {}) {
      calls.push({ path, options });
      return handler(path, options, calls.length);
    },
  };
}

function deferred() {
  let resolve;
  let reject;
  const promise = new Promise((resolvePromise, rejectPromise) => {
    resolve = resolvePromise;
    reject = rejectPromise;
  });
  return { promise, resolve, reject };
}

test("audit URL encodes only documented filters and cursor", () => {
  requireFunction(buildAuditLogsPath, "buildAuditLogsPath");
  const path = buildAuditLogsPath({
    from: "2026-07-01T00:00:00+08:00",
    to: "2026-07-14T12:00:00+08:00",
    actorId: "actor/id",
    eventType: "candidate.created & reviewed",
    resourceType: "candidate",
    resourceId: "resource?1",
    outcome: "success",
    ignored: "must-not-leak",
  }, { cursor: "cursor+/=", limit: 25 });

  const url = new URL(path, "https://example.test");
  assert.equal(url.pathname, "/api/v1/audit-logs");
  assert.deepEqual(Object.fromEntries(url.searchParams), {
    from: "2026-07-01T00:00:00+08:00",
    to: "2026-07-14T12:00:00+08:00",
    actor_id: "actor/id",
    event_type: "candidate.created & reviewed",
    resource_type: "candidate",
    resource_id: "resource?1",
    outcome: "success",
    cursor: "cursor+/=",
    limit: "25",
  });
});

test("normalization retains only the documented safe audit and retention projection", () => {
  requireFunction(normalizeAuditRecord, "normalizeAuditRecord");
  requireFunction(normalizeRetentionPolicy, "normalizeRetentionPolicy");
  const row = normalizeAuditRecord({
    id: "audit-1",
    created_at: "2026-07-14T02:00:00Z",
    actor: { id: "actor-1", display_name: "周明", email: "secret@example.com" },
    category: "recruiting",
    event_type: "candidate.created",
    resource: { type: "candidate", id: "candidate-1", label: "授权候选人", contacts: ["13800000000"] },
    outcome: "success",
    network_ref: "abcdef012345",
    trace_id: "trace-1",
    summary: "创建候选人",
    metadata: { resume_text: "raw resume", ip: "10.0.0.1" },
    detail: "private server detail",
  });

  assert.deepEqual(row, {
    id: "audit-1",
    createdAt: "2026-07-14T02:00:00Z",
    actor: { id: "actor-1", displayName: "周明" },
    category: "recruiting",
    eventType: "candidate.created",
    resource: { type: "candidate", id: "candidate-1", label: "授权候选人" },
    outcome: "success",
    networkRef: "abcdef012345",
    traceId: "trace-1",
    summary: "创建候选人",
  });
  assert.deepEqual(normalizeRetentionPolicy(policy), {
    id: "policy-1",
    version: 7,
    terminalDays: 730,
    talentPoolDays: 1095,
    backupWindowDays: 90,
    updatedAt: "2026-07-14T01:00:00Z",
    updatedBy: { id: "user-1", displayName: "系统管理员" },
  });
  assert.equal(JSON.stringify(row).includes("resume"), false);
  assert.equal(JSON.stringify(normalizeRetentionPolicy(policy)).includes("private@example.com"), false);
});

test("audit reload clears old scope immediately and exposes loading then empty", async () => {
  requireFunction(createGovernanceSettingsController, "createGovernanceSettingsController");
  const second = deferred();
  const client = createClient((_path, _options, count) => count === 1
    ? { data: [{ id: "audit-1", actor: { display_name: "周明" } }], meta: { next_cursor: null, limit: 50 } }
    : second.promise);
  const controller = createGovernanceSettingsController({ client, now: previewNow });
  await controller.loadAudit({ outcome: "success" });
  assert.equal(controller.getState().audit.status, "ready");
  assert.equal(controller.getState().audit.rows.length, 1);

  const loading = controller.loadAudit({ outcome: "denied" });
  assert.equal(controller.getState().audit.status, "loading");
  assert.deepEqual(controller.getState().audit.rows, []);
  second.resolve({ data: [], meta: { next_cursor: null, limit: 50 } });
  await loading;
  assert.equal(controller.getState().audit.status, "empty");
});

test("audit cursor paging appends and deduplicates by audit id", async () => {
  requireFunction(createGovernanceSettingsController, "createGovernanceSettingsController");
  const client = createClient((_path, _options, count) => count === 1
    ? { data: [{ id: "audit-1" }, { id: "audit-2" }], meta: { next_cursor: "next token", limit: 2 } }
    : { data: [{ id: "audit-2" }, { id: "audit-3" }], meta: { next_cursor: null, limit: 2 } });
  const controller = createGovernanceSettingsController({ client });
  await controller.loadAudit({ eventType: "candidate.created" });
  await controller.loadMoreAudit();

  assert.deepEqual(controller.getState().audit.rows.map((row) => row.id), ["audit-1", "audit-2", "audit-3"]);
  assert.equal(controller.getState().audit.nextCursor, null);
  assert.equal(client.calls[1].path.includes("cursor=next+token"), true);
});

test("audit reload aborts the previous request and stale success or failure cannot replace current state", async () => {
  requireFunction(createGovernanceSettingsController, "createGovernanceSettingsController");
  const first = deferred();
  const second = deferred();
  const client = createClient((_path, _options, count) => count === 1 ? first.promise : second.promise);
  const controller = createGovernanceSettingsController({ client });
  const oldLoad = controller.loadAudit({ outcome: "failure" });
  const oldSignal = client.calls[0].options.signal;
  const newLoad = controller.loadAudit({ outcome: "success" });
  assert.equal(oldSignal.aborted, true);

  second.resolve({ data: [{ id: "new" }], meta: { next_cursor: null, limit: 50 } });
  await newLoad;
  first.reject(new ApiError({ code: "service_unavailable", detail: "stale private detail" }));
  await oldLoad;
  assert.deepEqual(controller.getState().audit.rows.map((row) => row.id), ["new"]);
  assert.equal(controller.getState().audit.error, "");
});

test("audit and retention independently expose denied and safe retryable error states", async () => {
  requireFunction(createGovernanceSettingsController, "createGovernanceSettingsController");
  const client = createClient((path) => {
    if (path.includes("audit-logs")) throw new ApiError({ status: 404, code: "resource_not_found", detail: "private row scope" });
    throw new ApiError({ status: 503, code: "service_unavailable", detail: "database password" });
  });
  const controller = createGovernanceSettingsController({ client });
  await Promise.all([controller.loadAudit(), controller.loadRetention()]);

  assert.equal(controller.getState().audit.status, "denied");
  assert.equal(controller.getState().retention.status, "error");
  assert.equal(controller.getState().retention.error.includes("password"), false);
  assert.equal(getGovernanceErrorMessage({ code: "unexpected", detail: "secret" }).includes("secret"), false);
});

test("deletion approval errors map real server codes to safe actionable guidance", () => {
  const expectedGuidance = new Map([
    ["self_approval_forbidden", /其他系统管理员审批/],
    ["active_application_exists", /终止申请并批准删除/],
    ["legal_hold_active", /确认并解除/],
    ["invalid_deletion_state_transition", /刷新请求后核对最新状态/],
  ]);

  for (const [code, expected] of expectedGuidance) {
    const message = getGovernanceErrorMessage({ code, detail: `private detail for ${code}` });
    assert.match(message, expected, code);
    assert.doesNotMatch(message, /private detail|raw detail/i, code);
  }
});

test("audit and retention latest-operation generations do not abort each other", async () => {
  requireFunction(createGovernanceSettingsController, "createGovernanceSettingsController");
  const audit = deferred();
  const retention = deferred();
  const client = createClient((path) => path.includes("audit-logs") ? audit.promise : retention.promise);
  const controller = createGovernanceSettingsController({ client });
  const auditLoad = controller.loadAudit();
  const auditSignal = client.calls[0].options.signal;
  const retentionLoad = controller.loadRetention();
  assert.equal(auditSignal.aborted, false);
  retention.resolve({ data: policy });
  audit.resolve({ data: [], meta: { next_cursor: null, limit: 50 } });
  await Promise.all([auditLoad, retentionLoad]);
  assert.equal(controller.getState().retention.status, "ready");
  assert.equal(controller.getState().audit.status, "empty");
});

test("forced retention reload freezes stale draft and quick save cannot abort GET or start a mutation", async () => {
  requireFunction(createGovernanceSettingsController, "createGovernanceSettingsController");
  const forcedGet = deferred();
  const latest = { ...policy, version: 8, terminal_days: 900 };
  const client = createClient((path, _options, count) => {
    if (count === 1) return { data: policy };
    if (count === 2) return forcedGet.promise;
    if (path.endsWith("/previews")) return { data: { current_version: 8, shortening: true, affected_candidate_count: 2, impact_token: "new-version-token", expires_at: "2026-07-14T04:00:00Z" } };
    throw new Error("unexpected request");
  });
  const controller = createGovernanceSettingsController({ client, now: previewNow });
  await controller.loadRetention();
  controller.updateRetentionDraft({ terminalDays: 365 });

  const reloading = controller.loadRetention();
  const forcedGetSignal = client.calls[1].options.signal;
  assert.equal(controller.getState().retention.status, "loading");
  assert.equal(controller.getState().retention.policy, null);
  assert.equal(controller.getState().retention.draft, null);

  assert.equal(await controller.saveRetention(), false);
  assert.equal(await controller.confirmRetentionSave(), false);
  assert.equal(forcedGetSignal.aborted, false);
  assert.equal(client.calls.length, 2);

  forcedGet.resolve({ data: latest });
  await reloading;
  controller.updateRetentionDraft({ terminalDays: 500 });
  await controller.saveRetention();

  assert.equal(client.calls.length, 3);
  assert.equal(client.calls[2].path, "/api/v1/settings/retention-policy/previews");
  assert.deepEqual(client.calls[2].options.body, { terminal_days: 500, talent_pool_days: 1095, backup_window_days: 90 });
  assert.equal(controller.getState().retention.preview.affectedCandidateCount, 2);
});

test("unchanged or increased retention saves directly with quoted If-Match and exact body", async () => {
  requireFunction(createGovernanceSettingsController, "createGovernanceSettingsController");
  const client = createClient((path, options) => {
    if (options.method === "PATCH") return { data: { ...policy, version: 8, terminal_days: 800 } };
    return { data: policy };
  });
  const controller = createGovernanceSettingsController({ client, createIdempotencyKey: () => "save-key-1" });
  await controller.loadRetention();
  controller.updateRetentionDraft({ terminalDays: 800 });
  const result = await controller.saveRetention();

  assert.equal(result, true);
  assert.deepEqual(client.calls[1], {
    path: "/api/v1/settings/retention-policy",
    options: {
      method: "PATCH",
      body: { terminal_days: 800, talent_pool_days: 1095, backup_window_days: 90 },
      ifMatch: '"7"',
      idempotencyKey: "save-key-1",
      signal: client.calls[1].options.signal,
    },
  });
  assert.equal(controller.getState().retention.policy.version, 8);
});

test("shortening requires a server preview and explicit confirmation before PATCH", async () => {
  requireFunction(createGovernanceSettingsController, "createGovernanceSettingsController");
  const client = createClient((path, options) => {
    if (path.endsWith("/previews")) return { data: { current_version: 7, shortening: true, affected_candidate_count: 23, impact_token: "token-secret", expires_at: "2026-07-14T02:10:00Z" } };
    if (options.method === "PATCH") return { data: { ...policy, version: 8, terminal_days: 365 } };
    return { data: policy };
  });
  const controller = createGovernanceSettingsController({ client, createIdempotencyKey: () => "shorten-key", now: previewNow });
  await controller.loadRetention();
  controller.updateRetentionDraft({ terminalDays: 365 });
  const prepared = await controller.saveRetention();

  assert.equal(prepared, false);
  assert.equal(client.calls.some((call) => call.options.method === "PATCH"), false);
  assert.deepEqual(controller.getState().retention.preview, {
    affectedCandidateCount: 23,
    expiresAt: "2026-07-14T02:10:00Z",
  });
  assert.equal(JSON.stringify(controller.getState()).includes("token-secret"), false);

  const confirmed = await controller.confirmRetentionSave();
  assert.equal(confirmed, true);
  const patch = client.calls.find((call) => call.options.method === "PATCH");
  assert.equal(patch.options.body.impact_token, "token-secret");
});

test("draft change after preview invalidates the token and confirmation gate", async () => {
  requireFunction(createGovernanceSettingsController, "createGovernanceSettingsController");
  const client = createClient((path) => path.endsWith("/previews")
    ? { data: { current_version: 7, shortening: true, affected_candidate_count: 4, impact_token: "token", expires_at: "2026-07-14T02:10:00Z" } }
    : { data: policy });
  const controller = createGovernanceSettingsController({ client, now: previewNow });
  await controller.loadRetention();
  controller.updateRetentionDraft({ terminalDays: 365 });
  await controller.saveRetention();
  controller.updateRetentionDraft({ terminalDays: 364 });

  assert.equal(controller.getState().retention.preview, null);
  assert.equal(await controller.confirmRetentionSave(), false);
  assert.equal(client.calls.some((call) => call.options.method === "PATCH"), false);
});

test("malformed preview projections fail safely without exposing a confirmation", async () => {
  requireFunction(createGovernanceSettingsController, "createGovernanceSettingsController");
  const valid = {
    current_version: 7,
    shortening: true,
    affected_candidate_count: 2,
    impact_token: "preview-token",
    expires_at: "2026-07-14T02:10:00Z",
  };
  const malformed = [
    { ...valid, affected_candidate_count: -1 },
    { ...valid, affected_candidate_count: 1.5 },
    { ...valid, affected_candidate_count: "2" },
    { ...valid, affected_candidate_count: undefined },
    { ...valid, expires_at: "not-a-date" },
    { ...valid, expires_at: "2026-07-14 02:10:00" },
    { ...valid, expires_at: "2026-07-14T02:00:00Z" },
  ];

  for (const projection of malformed) {
    const client = createClient((path) => path.endsWith("/previews") ? { data: projection } : { data: policy });
    const controller = createGovernanceSettingsController({ client, now: previewNow });
    await controller.loadRetention();
    controller.updateRetentionDraft({ terminalDays: 365 });

    await controller.saveRetention();

    assert.equal(controller.getState().retention.status, "error");
    assert.equal(controller.getState().retention.preview, null);
    assert.equal(await controller.confirmRetentionSave(), false);
    assert.equal(client.calls.some((call) => call.options.method === "PATCH"), false);
  }
});

test("mutation-denied preview and PATCH clear stale policy, draft, preview, and save capability", async () => {
  requireFunction(createGovernanceSettingsController, "createGovernanceSettingsController");
  for (const mutation of ["preview", "patch"]) {
    const client = createClient((path, options) => {
      if (path.endsWith("/previews") || options.method === "PATCH") {
        throw new ApiError({ status: 404, code: "resource_not_found", detail: "private authorization scope" });
      }
      return { data: policy };
    });
    const controller = createGovernanceSettingsController({ client, createIdempotencyKey: () => `${mutation}-key` });
    await controller.loadRetention();
    controller.updateRetentionDraft({ terminalDays: mutation === "preview" ? 365 : 800 });

    await controller.saveRetention();

    assert.deepEqual(controller.getState().retention, {
      status: "denied",
      policy: null,
      draft: null,
      dirty: false,
      preview: null,
      error: "当前账号无权访问此治理设置。",
      message: "",
    }, mutation);
    const callCount = client.calls.length;
    assert.equal(await controller.saveRetention(), false);
    assert.equal(await controller.confirmRetentionSave(), false);
    assert.equal(client.calls.length, callCount, `${mutation} denied must freeze mutations`);
  }
});

test("stale or expired preview failure clears confirmation and requires a fresh preview", async () => {
  requireFunction(createGovernanceSettingsController, "createGovernanceSettingsController");
  let previews = 0;
  const client = createClient((path, options) => {
    if (path.endsWith("/previews")) {
      previews += 1;
      return { data: { current_version: 7, shortening: true, affected_candidate_count: 5, impact_token: `token-${previews}`, expires_at: "2026-07-14T02:10:00Z" } };
    }
    if (options.method === "PATCH") throw new ApiError({ status: 409, code: "retention_preview_stale", detail: "candidate private data" });
    return { data: policy };
  });
  const controller = createGovernanceSettingsController({ client, now: previewNow });
  await controller.loadRetention();
  controller.updateRetentionDraft({ terminalDays: 365 });
  await controller.saveRetention();
  await controller.confirmRetentionSave();

  assert.equal(controller.getState().retention.preview, null);
  assert.equal(controller.getState().retention.error.includes("private"), false);
  await controller.saveRetention();
  assert.equal(previews, 2);
});

test("ambiguous save failure reuses key and body while terminal failure creates a fresh intent", async () => {
  requireFunction(createGovernanceSettingsController, "createGovernanceSettingsController");
  const keys = ["key-1", "key-2", "key-3"];
  let attempts = 0;
  const client = createClient((_path, options) => {
    if (options.method !== "PATCH") return { data: policy };
    attempts += 1;
    if (attempts === 1) throw new ApiError({ status: 503, code: "service_unavailable", kind: "unavailable" });
    if (attempts === 2) throw new ApiError({ status: 422, code: "validation_failed" });
    return { data: { ...policy, version: 8, terminal_days: 800 } };
  });
  const controller = createGovernanceSettingsController({ client, createIdempotencyKey: () => keys.shift() });
  await controller.loadRetention();
  controller.updateRetentionDraft({ terminalDays: 800 });
  await controller.saveRetention();
  await controller.saveRetention();
  await controller.saveRetention();

  const patches = client.calls.filter((call) => call.options.method === "PATCH");
  assert.equal(patches[0].options.idempotencyKey, "key-1");
  assert.equal(patches[1].options.idempotencyKey, "key-1");
  assert.deepEqual(patches[1].options.body, patches[0].options.body);
  assert.equal(patches[2].options.idempotencyKey, "key-2");
  assert.equal(controller.getState().retention.status, "ready");
});

test("successful save resets idempotency and the next logical save gets a fresh key", async () => {
  requireFunction(createGovernanceSettingsController, "createGovernanceSettingsController");
  const keys = ["key-1", "key-2"];
  let version = 7;
  const client = createClient((_path, options) => {
    if (options.method !== "PATCH") return { data: { ...policy, version } };
    version += 1;
    return { data: { ...policy, version, terminal_days: options.body.terminal_days } };
  });
  const controller = createGovernanceSettingsController({ client, createIdempotencyKey: () => keys.shift() });
  await controller.loadRetention();
  controller.updateRetentionDraft({ terminalDays: 800 });
  await controller.saveRetention();
  controller.updateRetentionDraft({ terminalDays: 900 });
  await controller.saveRetention();

  const patches = client.calls.filter((call) => call.options.method === "PATCH");
  assert.deepEqual(patches.map((call) => call.options.idempotencyKey), ["key-1", "key-2"]);
});

test("version conflict reloads the current policy and asks the user to review", async () => {
  requireFunction(createGovernanceSettingsController, "createGovernanceSettingsController");
  const latest = { ...policy, version: 8, terminal_days: 900 };
  const client = createClient((_path, options, count) => {
    if (options.method === "PATCH") throw new ApiError({ status: 409, code: "resource_version_conflict", detail: "private conflict detail" });
    return { data: count === 1 ? policy : latest };
  });
  const controller = createGovernanceSettingsController({ client });
  await controller.loadRetention();
  controller.updateRetentionDraft({ terminalDays: 800 });
  const saved = await controller.saveRetention();

  assert.equal(saved, false);
  assert.equal(controller.getState().retention.policy.version, 8);
  assert.equal(controller.getState().retention.draft.terminalDays, 900);
  assert.equal(controller.getState().retention.message.includes("检查"), true);
  assert.equal(controller.getState().retention.message.includes("private"), false);
});

test("version-conflict reload remains authoritative until a fresh-version save intent is created", async () => {
  requireFunction(createGovernanceSettingsController, "createGovernanceSettingsController");
  const forcedGet = deferred();
  const keys = ["old-version-key", "new-version-key"];
  const latest = { ...policy, version: 8, terminal_days: 900 };
  let patchAttempt = 0;
  const client = createClient((_path, options, count) => {
    if (options.method === "PATCH") {
      patchAttempt += 1;
      if (patchAttempt === 1) throw new ApiError({ status: 409, code: "resource_version_conflict" });
      return { data: { ...latest, version: 9, terminal_days: options.body.terminal_days } };
    }
    if (count === 1) return { data: policy };
    return forcedGet.promise;
  });
  const controller = createGovernanceSettingsController({ client, createIdempotencyKey: () => keys.shift() });
  await controller.loadRetention();
  controller.updateRetentionDraft({ terminalDays: 800 });

  const conflictedSave = controller.saveRetention();
  await new Promise((resolve) => setImmediate(resolve));
  const forcedGetSignal = client.calls[2].options.signal;
  assert.equal(controller.getState().retention.status, "loading");
  assert.equal(await controller.saveRetention(), false);
  assert.equal(forcedGetSignal.aborted, false);
  assert.equal(client.calls.length, 3);

  forcedGet.resolve({ data: latest });
  assert.equal(await conflictedSave, false);
  controller.updateRetentionDraft({ terminalDays: 1000 });
  assert.equal(await controller.saveRetention(), true);

  const patches = client.calls.filter((call) => call.options.method === "PATCH");
  assert.deepEqual(patches.map((call) => call.options.idempotencyKey), ["old-version-key", "new-version-key"]);
  assert.equal(patches[1].options.ifMatch, '"8"');
});

test("Strict Mode release aborts requests and clears preview without permanently disposing the controller", async () => {
  requireFunction(createGovernanceSettingsController, "createGovernanceSettingsController");
  requireFunction(releaseGovernanceSettingsSubscription, "releaseGovernanceSettingsSubscription");
  const pending = deferred();
  const client = createClient((_path, _options, count) => count === 1 ? pending.promise : { data: policy });
  const controller = createGovernanceSettingsController({ client });
  const unsubscribe = controller.subscribe(() => {});
  const loading = controller.loadAudit();
  const signal = client.calls[0].options.signal;
  releaseGovernanceSettingsSubscription(controller, unsubscribe);
  assert.equal(signal.aborted, true);
  pending.resolve({ data: [{ id: "late" }], meta: { next_cursor: null, limit: 50 } });
  await loading;

  const observed = [];
  controller.subscribe((state) => observed.push(state.retention.status));
  await controller.loadRetention();
  assert.equal(controller.getState().retention.status, "ready");
  assert.equal(observed.includes("ready"), true);
});

test("dispose aborts in-flight operations and suppresses late state updates", async () => {
  requireFunction(createGovernanceSettingsController, "createGovernanceSettingsController");
  const pending = deferred();
  const client = createClient(() => pending.promise);
  const controller = createGovernanceSettingsController({ client });
  let notifications = 0;
  controller.subscribe(() => { notifications += 1; });
  const loading = controller.loadRetention();
  const stateBeforeDispose = controller.getState();
  const signal = client.calls[0].options.signal;
  controller.dispose();
  assert.equal(signal.aborted, true);
  pending.resolve({ data: policy });
  await loading;
  assert.equal(controller.getState(), stateBeforeDispose);
  assert.equal(notifications, 1);
});

test("dialog focus manager sets safe initial focus, traps Tab, gates Escape while busy, and restores focus", async () => {
  const { createServer } = await import("vite");
  const vite = await createServer({ root: process.cwd(), logLevel: "silent", server: { middlewareMode: true }, appType: "custom" });
  try {
    const { createDialogFocusManager } = await vite.ssrLoadModule("/src/SettingsViews.jsx");
    assert.equal(typeof createDialogFocusManager, "function");

    const focused = [];
    const documentRef = { activeElement: null };
    function control(name, { disabled = false } = {}) {
      return {
        name,
        disabled,
        isConnected: true,
        getAttribute: () => null,
        focus() { documentRef.activeElement = this; focused.push(name); },
      };
    }
    const close = control("close");
    const cancel = control("cancel");
    const confirm = control("confirm");
    const disabled = control("disabled", { disabled: true });
    const trigger = control("trigger");
    const controls = [close, cancel, confirm, disabled];
    let busy = false;
    let closes = 0;
    const dialog = {
      querySelector(selector) { return selector === "[data-dialog-initial-focus]" ? cancel : null; },
      querySelectorAll() { return controls; },
      contains(value) { return controls.includes(value); },
    };
    const manager = createDialogFocusManager({
      dialog,
      restoreTarget: trigger,
      documentRef,
      isBusy: () => busy,
      onClose: () => { closes += 1; },
    });

    manager.focusInitial();
    assert.equal(documentRef.activeElement, cancel);
    documentRef.activeElement = confirm;
    let prevented = 0;
    manager.handleKeyDown({ key: "Tab", shiftKey: false, preventDefault: () => { prevented += 1; } });
    assert.equal(documentRef.activeElement, close);
    documentRef.activeElement = close;
    manager.handleKeyDown({ key: "Tab", shiftKey: true, preventDefault: () => { prevented += 1; } });
    assert.equal(documentRef.activeElement, confirm);
    assert.equal(focused.includes("disabled"), false);

    busy = true;
    manager.handleKeyDown({ key: "Escape", preventDefault: () => { prevented += 1; } });
    assert.equal(closes, 0);
    busy = false;
    manager.handleKeyDown({ key: "Escape", preventDefault: () => { prevented += 1; } });
    assert.equal(closes, 1);
    manager.restoreFocus();
    assert.equal(documentRef.activeElement, trigger);
    assert.equal(prevented, 4);
  } finally {
    await vite.close();
  }
});

test("Settings UI uses the real governance controller and safe SET-04 projection", () => {
  const source = readFileSync(new URL("./SettingsViews.jsx", import.meta.url), "utf8");

  assert.match(source, /createGovernanceSettingsController/);
  assert.match(source, /loadMoreAudit/);
  assert.match(source, /terminalDays/);
  assert.match(source, /talentPoolDays/);
  assert.match(source, /backupWindowDays/);
  assert.match(source, /网络标识/);
  assert.match(source, /confirmDisabled/);
  assert.match(source, /<AuditSettings key=\{currentRole\}/);
  assert.match(source, /createDialogFocusManager/);
  assert.match(source, /data-dialog-initial-focus/);
  assert.match(source, /onKeyDown=\{handleDialogKeyDown\}/);
  assert.match(source, /retention\.status !== "denied" && retention\.policy && retention\.draft/);
  assert.match(source, /retention\.status === "loading" \|\| retention\.status === "saving" \|\| retention\.status === "previewing"/);
  assert.doesNotMatch(source, /const auditRows/);
  assert.doesNotMatch(source, /来源 IP/);
  assert.doesNotMatch(source, /raw_metadata|error\.detail/);
  assert.match(source, /loadDeletionRequests/);
  assert.match(source, /loadMoreDeletionRequests/);
  assert.match(source, /approveDeletionRequest/);
  assert.match(source, /删除请求审批/);
  assert.match(source, /候选人版本/);
  assert.match(source, /<option value="requested">待审批<\/option>/);
  assert.match(source, /<option value="approved">已批准<\/option>/);
  assert.match(source, /<option value="executing">执行中<\/option>/);
  assert.match(source, /<option value="completed">已完成<\/option>/);
  assert.match(source, /<option value="failed">失败<\/option>/);
  assert.match(source, /title === "确认批准删除请求" \? "deletion-approval-dialog"/);
  assert.doesNotMatch(source, /queued|processing/);
  assert.doesNotMatch(source, /候选人姓名|candidateName|candidate\.name/);
});

test("deletion queue path sends every real status and rejects obsolete values", () => {
  requireFunction(buildDeletionRequestsPath, "buildDeletionRequestsPath");
  for (const status of ["requested", "approved", "executing", "completed", "failed"]) {
    assert.equal(buildDeletionRequestsPath(status), `/api/v1/deletion-requests?status=${status}&limit=50`);
  }
  assert.equal(buildDeletionRequestsPath("failed", { cursor: "next-token", limit: 25 }), "/api/v1/deletion-requests?status=failed&cursor=next-token&limit=25");
  assert.equal(buildDeletionRequestsPath("", {}), "/api/v1/deletion-requests?limit=50");
  assert.equal(buildDeletionRequestsPath("queued"), "/api/v1/deletion-requests?limit=50");
  assert.equal(buildDeletionRequestsPath("processing"), "/api/v1/deletion-requests?limit=50");
});

test("new governance surfaces keep body, queue, detail, counts and actions at 16px without 390px overflow", () => {
  const styles = readFileSync(new URL("./styles.css", import.meta.url), "utf8");
  for (const selector of [
    "candidate-governance", "candidate-governance h3", "candidate-governance dl div",
    "governance-dialog", "governance-dialog header p", "governance-dialog-body small",
    "governance-impact > small", "deletion-queue", "deletion-queue \\.status-muted",
    "deletion-queue \\.governance-empty span", "deletion-request-list small",
    "deletion-impact-grid", "deletion-request-drawer", "deletion-request-drawer header p",
    "deletion-approval-dialog",
  ]) {
    assert.match(styles, new RegExp(`\\.${selector}\\s*\\{[^}]*font-size:\\s*16px`), selector);
  }
  for (const selector of [
    "candidate-governance button", "governance-dialog button",
    "governance-dialog-body textarea", "deletion-queue-toolbar select",
    "deletion-queue button", "deletion-request-drawer button",
    "deletion-approval-dialog button",
  ]) {
    assert.match(styles, new RegExp(`\\.${selector}\\s*\\{[^}]*font-size:\\s*16px\\s*!important`), selector);
  }
  assert.match(styles, /@media \(max-width: 600px\)[\s\S]*?\.deletion-queue \{ margin: 10px 8px; \}/);
  assert.match(styles, /\.deletion-request-list > button \{[^}]*min-width: 0;/);
  assert.match(styles, /\.deletion-request-drawer \.settings-drawer-body dl div \{ grid-template-columns: 1fr;/);
});

test("deletion request normalization keeps only the safe detail projection", () => {
  requireFunction(normalizeDeletionRequest, "normalizeDeletionRequest");
  const normalized = normalizeDeletionRequest({ ...deletionRequest, object_key: "private", impact_manifest: { secret: true } });
  assert.equal(normalized.id, deletionRequest.id);
  assert.equal(normalized.impact.counts.temporaryExports, 9);
  assert.equal(normalized.activeApplicationCount, 0);
  assert.equal(Object.hasOwn(normalized, "object_key"), false);
  assert.equal(Object.hasOwn(normalized, "impact_manifest"), false);
});

test("deletion queue loads, paginates, de-duplicates and fetches selected detail", async () => {
  const second = { ...deletionRequest, id: "33333333-3333-4333-8333-333333333333", status: "failed" };
  const client = createClient((path) => {
    if (path.includes(`/deletion-requests/${second.id}`)) return { data: second };
    if (path.includes("cursor=next")) return { data: [deletionRequest, second], meta: { next_cursor: null, limit: 50 } };
    return { data: [deletionRequest], meta: { next_cursor: "next", limit: 50 } };
  });
  const controller = createGovernanceSettingsController({ client });
  await controller.loadDeletionRequests("requested");
  await controller.loadMoreDeletionRequests();
  await controller.loadDeletionRequest(second.id);
  assert.deepEqual(controller.getState().deletionQueue.rows.map((row) => row.id), [deletionRequest.id, second.id]);
  assert.equal(controller.getState().deletionQueue.selected.id, second.id);
  assert.deepEqual(client.calls.map((call) => call.path), [
    "/api/v1/deletion-requests?status=requested&limit=50",
    "/api/v1/deletion-requests?status=requested&cursor=next&limit=50",
    `/api/v1/deletion-requests/${second.id}`,
  ]);
});

test("approval posts exact transition contract and rotates key after success", async () => {
  const keys = ["approve-key-1", "approve-key-2"];
  let version = 4;
  const client = createClient((path, options) => {
    if (options.method === "POST") {
      version += 1;
      return { data: { ...deletionRequest, version, status: "approved", approved_at: "2026-07-15T03:00:00Z" } };
    }
    return { data: { ...deletionRequest, version }, meta: { next_cursor: null, limit: 50 } };
  });
  const controller = createGovernanceSettingsController({ client, createIdempotencyKey: () => keys.shift() });
  await controller.loadDeletionRequests();
  await controller.loadDeletionRequest(deletionRequest.id);
  assert.equal(await controller.approveDeletionRequest(), true);
  assert.equal(controller.getState().deletionQueue.selected.status, "approved");
  assert.equal(await controller.approveDeletionRequest(), false);
  await controller.loadDeletionRequest(deletionRequest.id);
  controller.getState().deletionQueue.selected.status = "failed";
  assert.equal(await controller.approveDeletionRequest(), true);
  const posts = client.calls.filter((call) => call.options.method === "POST");
  assert.deepEqual(posts.map((call) => call.path), Array(2).fill(`/api/v1/deletion-requests/${deletionRequest.id}/transitions`));
  assert.deepEqual(posts.map((call) => call.options.body), Array(2).fill({ target_status: "approved", terminate_active_applications: false }));
  assert.deepEqual(posts.map((call) => call.options.ifMatch), ['"4"', '"5"']);
  assert.deepEqual(posts.map((call) => call.options.idempotencyKey), ["approve-key-1", "approve-key-2"]);
});

test("approval explicitly authorizes terminating active applications", async () => {
  const activeRequest = { ...deletionRequest, active_application_count: 2 };
  const client = createClient((_path, options) => {
    if (options.method === "POST") return { data: { ...activeRequest, status: "approved", version: 5, active_application_count: 0 } };
    return { data: activeRequest };
  });
  const controller = createGovernanceSettingsController({ client, createIdempotencyKey: () => "terminate-key" });
  await controller.loadDeletionRequest(activeRequest.id);
  assert.equal(await controller.approveDeletionRequest({ terminateActiveApplications: true }), true);
  const post = client.calls.find((call) => call.options.method === "POST");
  assert.deepEqual(post.options.body, {
    target_status: "approved",
    terminate_active_applications: true,
  });
  assert.equal(post.options.idempotencyKey, "terminate-key");
});

test("stale manifest and version conflict refresh detail and require a new confirmation", async () => {
  let gets = 0;
  const refreshed = { ...deletionRequest, version: 5, status: "failed", impact: { ...deletionRequest.impact, counts: { ...deletionCounts, contacts: 2 } } };
  const client = createClient((_path, options) => {
    if (options.method === "POST") throw new ApiError({ status: 409, code: "stale_manifest" });
    gets += 1;
    return { data: gets === 1 ? deletionRequest : refreshed };
  });
  const controller = createGovernanceSettingsController({ client, createIdempotencyKey: () => "stale-key" });
  await controller.loadDeletionRequest(deletionRequest.id);
  assert.equal(await controller.approveDeletionRequest(), false);
  const state = controller.getState().deletionQueue;
  assert.equal(state.selected.version, 5);
  assert.equal(state.impactChanged, true);
  assert.equal(state.confirmationRequired, true);
  assert.equal(state.approving, false);
  assert.equal(client.calls.filter((call) => call.options.method === "POST").length, 1);
});

test("ambiguous approval retries reuse the key while definitive 4xx rotates it", async () => {
  const keys = ["key-1", "key-2"];
  let attempts = 0;
  const client = createClient((_path, options) => {
    if (options.method !== "POST") return { data: deletionRequest };
    attempts += 1;
    if (attempts === 1) throw new ApiError({ status: 503, code: "service_unavailable", kind: "unavailable" });
    throw new ApiError({ status: 422, code: "validation_failed" });
  });
  const controller = createGovernanceSettingsController({ client, createIdempotencyKey: () => keys.shift() });
  await controller.loadDeletionRequest(deletionRequest.id);
  await controller.approveDeletionRequest();
  await controller.approveDeletionRequest();
  controller.getState().deletionQueue.selected.status = "failed";
  await controller.approveDeletionRequest();
  const posts = client.calls.filter((call) => call.options.method === "POST");
  assert.deepEqual(posts.map((call) => call.options.idempotencyKey), ["key-1", "key-1", "key-2"]);
});
