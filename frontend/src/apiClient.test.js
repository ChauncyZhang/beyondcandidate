import test from "node:test";
import assert from "node:assert/strict";
import { ApiError, createApiClient } from "./apiClient.js";
import { createSessionController } from "./session.js";

function jsonResponse(body, { status = 200, headers = {} } = {}) {
  return new Response(body === null ? null : JSON.stringify(body), {
    status,
    headers: { "Content-Type": "application/json", ...headers },
  });
}

function barrier() {
  let release;
  const promise = new Promise((resolve) => { release = resolve; });
  return { promise, release };
}

for (const operation of ["request", "download"]) {
  for (const epochChange of ["before refresh", "during refresh"]) {
    test(`${operation} does not refresh or replay a stale write after login ${epochChange}`, async () => {
      const writeResponse = barrier();
      const refreshStarted = barrier();
      const refreshResponse = barrier();
      const calls = [];
      let meCalls = 0;
      const oldUser = { id: "old-user", roles: ["recruiter"] };
      const newUser = { id: "new-user", roles: ["recruiter"] };
      const client = createApiClient({ fetchImpl: async (path, options) => {
        calls.push({ path, options });
        if (path === "/api/v1/auth/login") {
          return jsonResponse({ data: { authenticated: true } }, { headers: { "X-CSRF-Token": "new-login-csrf" } });
        }
        if (path === "/api/v1/me") {
          meCalls += 1;
          if (meCalls === 1) {
            return jsonResponse({ data: oldUser }, { headers: { "X-CSRF-Token": "old-csrf" } });
          }
          if (epochChange === "during refresh" && meCalls === 2) {
            refreshStarted.release();
            await refreshResponse.promise;
            return jsonResponse({ data: oldUser }, { headers: { "X-CSRF-Token": "late-old-csrf" } });
          }
          return jsonResponse({ data: newUser }, { headers: { "X-CSRF-Token": "new-session-csrf" } });
        }
        if (path === "/api/v1/stale-write") {
          await writeResponse.promise;
          return jsonResponse({ code: "csrf_validation_failed", title: "Forbidden", detail: "Invalid CSRF token." }, {
            status: 403, headers: { "Content-Type": "application/problem+json" },
          });
        }
        assert.equal(path, "/api/v1/current-write");
        return jsonResponse({ data: { ok: true } });
      } });
      const session = createSessionController(client);
      try {
        await session.bootstrap();
        const outcome = client[operation]("/api/v1/stale-write", { method: "POST", body: { action: "old-action" } })
          .then((value) => ({ value }), (error) => ({ error }));
        if (epochChange === "during refresh") {
          writeResponse.release();
          await refreshStarted.promise;
        }

        await session.login({ email: "new@example.test", password: "test-password" });
        assert.equal(client.getAuthEpoch(), 1);
        const authenticated = session.getSnapshot();
        writeResponse.release();
        refreshResponse.release();

        const { error } = await outcome;
        assert.ok(error instanceof ApiError);
        assert.equal(error.status, 403);
        assert.equal(error.code, "csrf_validation_failed");
        assert.equal(error.title, "Forbidden");
        assert.equal(error.detail, "Invalid CSRF token.");
        assert.equal(session.getSnapshot(), authenticated);
        assert.equal(authenticated.user.id, "new-user");
        assert.equal(calls.filter(({ path }) => path === "/api/v1/stale-write").length, 1);
        assert.equal(meCalls, epochChange === "before refresh" ? 2 : 3);

        await client.request("/api/v1/current-write", { method: "POST", body: {} });
        assert.equal(calls.at(-1).options.headers.get("X-CSRF-Token"), "new-session-csrf");
      } finally {
        session.dispose();
      }
    });
  }
}

for (const operation of ["request", "download"]) {
  for (const heldPhase of ["login", "me"]) {
    test(`${operation} cannot refresh or replay an old write while new ${heldPhase} is held`, async () => {
      const writeResponse = barrier();
      const phaseStarted = barrier();
      const phaseResponse = barrier();
      const calls = [];
      let meCalls = 0;
      const client = createApiClient({ fetchImpl: async (path, options) => {
        calls.push({ path, options });
        if (path === "/api/v1/auth/login") {
          if (heldPhase === "login") {
            phaseStarted.release();
            await phaseResponse.promise;
          }
          return jsonResponse({ data: { authenticated: true } }, { headers: { "X-CSRF-Token": "new-login-csrf" } });
        }
        if (path === "/api/v1/me") {
          meCalls += 1;
          if (meCalls === 1) {
            return jsonResponse({ data: { id: "old-user", roles: ["recruiter"] } }, { headers: { "X-CSRF-Token": "old-csrf" } });
          }
          if (heldPhase === "me" && meCalls === 2) {
            phaseStarted.release();
            await phaseResponse.promise;
          }
          return jsonResponse({ data: { id: "new-user", roles: ["recruiter"] } }, { headers: { "X-CSRF-Token": "new-session-csrf" } });
        }
        if (path === "/api/v1/stale-write") {
          await writeResponse.promise;
          return jsonResponse({ code: "csrf_validation_failed", title: "Forbidden", detail: "Invalid CSRF token." }, {
            status: 403, headers: { "Content-Type": "application/problem+json" },
          });
        }
        assert.equal(path, "/api/v1/current-write");
        return jsonResponse({ data: { ok: true } });
      } });
      const session = createSessionController(client);
      let loginPromise;
      try {
        await session.bootstrap();
        const outcome = client[operation]("/api/v1/stale-write", { method: "POST", body: { action: "old-action" } })
          .then((value) => ({ value }), (error) => ({ error }));
        loginPromise = session.login({ email: "new@example.test", password: "test-password" });
        await phaseStarted.promise;
        const submitting = session.getSnapshot();
        assert.equal(submitting.status, "anonymous");
        assert.equal(submitting.submitting, true);
        writeResponse.release();

        const { error } = await outcome;
        assert.ok(error instanceof ApiError);
        assert.equal(error.status, 403);
        assert.equal(error.code, "csrf_validation_failed");
        assert.equal(error.title, "Forbidden");
        assert.equal(error.detail, "Invalid CSRF token.");
        assert.equal(session.getSnapshot(), submitting);
        assert.equal(client.getAuthEpoch(), 1);
        assert.equal(calls.filter(({ path }) => path === "/api/v1/stale-write").length, 1);
        assert.equal(meCalls, heldPhase === "login" ? 1 : 2);

        phaseResponse.release();
        await loginPromise;
        assert.equal(session.getSnapshot().user.id, "new-user");
        assert.equal(session.getSnapshot().status, "authenticated");
        assert.equal(client.getAuthEpoch(), 1);
        await client.request("/api/v1/current-write", { method: "POST", body: {} });
        assert.equal(calls.at(-1).options.headers.get("X-CSRF-Token"), "new-session-csrf");
      } finally {
        writeResponse.release();
        phaseResponse.release();
        await loginPromise;
        session.dispose();
      }
    });
  }
}

test("Feishu approver options encode the draft code in a credentialed read and unwrap nodes and members", async () => {
  const calls = [];
  const data = { nodes: [{ node_id: "approval", name: "HR", need_approver: true, allow_multiple: false }], members: [{ user_id: "00000000-0000-4000-8000-000000000001", display_name: "HR", available: false, unavailable_reason: "feishu_approver_unbound" }] };
  const client = createApiClient({ fetchImpl: async (url, options) => {
    calls.push({ url, options });
    return jsonResponse({ data });
  } });
  assert.deepEqual(await client.getFeishuOnboardingApproverOptions("draft /?&中文"), data);
  const url = new URL(calls[0].url, "https://example.test");
  assert.equal(url.pathname, "/api/v1/settings/integrations/feishu/onboarding-approval/approver-options");
  assert.deepEqual([...url.searchParams], [["approval_code", "draft /?&中文"]]);
  assert.equal(calls[0].options.method, "GET");
  assert.equal(calls[0].options.credentials, "include");
  assert.equal(calls[0].options.body, undefined);
});

test("登录上下文只读取公开的部署组织信息", async () => {
  const calls = [];
  const client = createApiClient({ fetchImpl: async (url, options) => {
    calls.push({ url, options });
    return jsonResponse({ data: { default_organization: { slug: "acme", name: "Acme" } } });
  } });

  assert.deepEqual(await client.getAuthContext(), { organization_slug: "acme", organization_name: "Acme" });
  assert.equal(calls[0].url, "/api/v1/auth/config");
  assert.equal(calls[0].options.method, "GET");
  assert.equal(calls[0].options.headers.get("X-CSRF-Token"), null);
});

test("API 请求携带会话凭据，并仅在登录后为写请求附加内存 CSRF", async () => {
  const calls = [];
  const responses = [
    jsonResponse({ data: { authenticated: true } }, { headers: { "X-CSRF-Token": "login-csrf" } }),
    new Response(null, { status: 204 }),
  ];
  const client = createApiClient({ fetchImpl: async (url, options) => {
    calls.push({ url, options });
    return responses.shift();
  } });

  await client.login({ organization_slug: "acme", email: "hr@example.test", password: "secret" });
  await client.logout();

  assert.equal(calls[0].options.credentials, "include");
  assert.equal(calls[0].options.headers.get("X-CSRF-Token"), null);
  assert.equal(calls[1].options.credentials, "include");
  assert.equal(calls[1].options.headers.get("X-CSRF-Token"), "login-csrf");
});

test("GET /me 轮换 CSRF，后续请求支持幂等键与 If-Match", async () => {
  const calls = [];
  const responses = [
    jsonResponse({ data: { display_name: "林岚", roles: ["recruiter"] } }, { headers: { "X-CSRF-Token": "rotated-csrf" } }),
    jsonResponse({ data: { ok: true } }),
  ];
  const client = createApiClient({ fetchImpl: async (url, options) => {
    calls.push({ url, options });
    return responses.shift();
  } });

  await client.getMe();
  await client.request("/api/v1/screening-runs", {
    method: "POST",
    body: { job_id: "job-1" },
    idempotencyKey: "request-1",
    ifMatch: '"7"',
  });

  const headers = calls[1].options.headers;
  assert.equal(headers.get("X-CSRF-Token"), "rotated-csrf");
  assert.equal(headers.get("Idempotency-Key"), "request-1");
  assert.equal(headers.get("If-Match"), '"7"');
});

test("写请求遇到失效 CSRF 时刷新令牌并以相同幂等键和版本重试一次", async () => {
  const calls = [];
  const responses = [
    jsonResponse({ data: { display_name: "林岚" } }, { headers: { "X-CSRF-Token": "stale-csrf" } }),
    jsonResponse({ code: "csrf_validation_failed", title: "Forbidden" }, { status: 403, headers: { "Content-Type": "application/problem+json" } }),
    jsonResponse({ data: { display_name: "林岚" } }, { headers: { "X-CSRF-Token": "fresh-csrf" } }),
    jsonResponse({ data: { id: "application-1", stage: "interview_pending" } }),
  ];
  const client = createApiClient({ fetchImpl: async (url, options) => {
    calls.push({ url, options });
    return responses.shift();
  } });

  await client.getMe();
  const result = await client.request("/api/v1/applications/application-1/workflow-actions", {
    method: "POST",
    body: { action: "review_approved" },
    idempotencyKey: "review-once",
    ifMatch: '"3"',
  });

  assert.equal(result.data.stage, "interview_pending");
  assert.deepEqual(calls.map((call) => call.url), [
    "/api/v1/me",
    "/api/v1/applications/application-1/workflow-actions",
    "/api/v1/me",
    "/api/v1/applications/application-1/workflow-actions",
  ]);
  assert.equal(calls[1].options.headers.get("X-CSRF-Token"), "stale-csrf");
  assert.equal(calls[3].options.headers.get("X-CSRF-Token"), "fresh-csrf");
  assert.equal(calls[3].options.headers.get("Idempotency-Key"), "review-once");
  assert.equal(calls[3].options.headers.get("If-Match"), '"3"');
  assert.equal(calls[3].options.body, calls[1].options.body);
});

test("POST 下载遇到失效 CSRF 时刷新令牌并重试一次", async () => {
  const calls = [];
  const responses = [
    jsonResponse({ data: { display_name: "林岚" } }, { headers: { "X-CSRF-Token": "stale-download-csrf" } }),
    jsonResponse({ code: "csrf_validation_failed" }, { status: 403, headers: { "Content-Type": "application/problem+json" } }),
    jsonResponse({ data: { display_name: "林岚" } }, { headers: { "X-CSRF-Token": "fresh-download-csrf" } }),
    new Response(new Blob(["resume"]), { status: 200, headers: { "Content-Disposition": 'attachment; filename="resume.pdf"' } }),
  ];
  const client = createApiClient({ fetchImpl: async (url, options) => {
    calls.push({ url, options });
    return responses.shift();
  } });

  await client.getMe();
  const result = await client.download("/api/v1/download-tickets/consume", {
    method: "POST",
    body: { token: "one-time" },
  });

  assert.equal(result.filename, "resume.pdf");
  assert.equal(calls[1].options.headers.get("X-CSRF-Token"), "stale-download-csrf");
  assert.equal(calls[3].options.headers.get("X-CSRF-Token"), "fresh-download-csrf");
  assert.equal(calls[3].options.body, calls[1].options.body);
});

test("markNotificationRead sends the opaque application and event version with CSRF", async () => {
  const calls = [];
  const responses = [
    jsonResponse({ data: { display_name: "林岚" } }, { headers: { "X-CSRF-Token": "notification-csrf" } }),
    jsonResponse({ data: { application_id: "application/1", version: "a".repeat(64), read_at: "2026-07-22T01:00:00Z" } }),
  ];
  const client = createApiClient({ fetchImpl: async (url, options) => {
    calls.push({ url, options });
    return responses.shift();
  } });

  await client.getMe();
  const result = await client.markNotificationRead("application/1", "a".repeat(64));

  assert.equal(calls[1].url, "/api/v1/notifications/workbench/application%2F1/read");
  assert.equal(calls[1].options.method, "PUT");
  assert.equal(calls[1].options.headers.get("X-CSRF-Token"), "notification-csrf");
  assert.deepEqual(JSON.parse(calls[1].options.body), { version: "a".repeat(64) });
  assert.equal(result.read_at, "2026-07-22T01:00:00Z");
});

test("成功的 /me 缺少响应头时保留已有 CSRF", async () => {
  const calls = [];
  const responses = [
    jsonResponse({ data: { display_name: "林岚" } }, { headers: { "X-CSRF-Token": "existing-csrf" } }),
    jsonResponse({ data: { display_name: "林岚" } }),
    jsonResponse({ data: { ok: true } }),
  ];
  const client = createApiClient({ fetchImpl: async (url, options) => {
    calls.push({ url, options });
    return responses.shift();
  } });

  await client.getMe();
  await client.getMe();
  await client.request("/api/v1/jobs", { method: "POST", body: {} });

  assert.equal(calls[2].options.headers.get("X-CSRF-Token"), "existing-csrf");
});

test("登录尝试仍会在请求前清除已有 CSRF", async () => {
  const calls = [];
  const responses = [
    jsonResponse({ data: { display_name: "林岚" } }, { headers: { "X-CSRF-Token": "old-csrf" } }),
    jsonResponse({ data: { authenticated: true } }),
    jsonResponse({ data: { ok: true } }),
  ];
  const client = createApiClient({ fetchImpl: async (url, options) => {
    calls.push({ url, options });
    return responses.shift();
  } });

  await client.getMe();
  await client.login({ organization_slug: "acme", email: "hr@example.test", password: "secret" });
  await client.request("/api/v1/jobs", { method: "POST", body: {} });

  assert.equal(calls[1].options.headers.get("X-CSRF-Token"), null);
  assert.equal(calls[2].options.headers.get("X-CSRF-Token"), null);
});

test("application/problem+json 被限制为安全的类型化错误字段", async () => {
  const client = createApiClient({ fetchImpl: async () => new Response(JSON.stringify({
    type: "about:blank",
    title: "Request denied",
    status: 401,
    detail: "Invalid credentials or account unavailable.",
    code: "authentication_failed",
    trace_id: "internal-trace",
    stack: "secret internals",
  }), { status: 401, headers: { "Content-Type": "application/problem+json" } }) });

  await assert.rejects(client.getMe(), (error) => {
    assert.ok(error instanceof ApiError);
    assert.equal(error.status, 401);
    assert.equal(error.code, "authentication_failed");
    assert.equal(error.detail, "Invalid credentials or account unavailable.");
    assert.equal("trace_id" in error, false);
    assert.equal("stack" in error, true);
    assert.equal(error.stack.includes("secret internals"), false);
    return true;
  });
});

test("登录锁定响应只保留安全的剩余等待秒数", async () => {
  const client = createApiClient({ fetchImpl: async () => new Response(JSON.stringify({
    type: "about:blank",
    title: "Request denied",
    status: 429,
    detail: "Too many failed login attempts.",
    code: "account_temporarily_locked",
    retry_after_seconds: 601,
    trace_id: "internal-trace",
  }), { status: 429, headers: { "Content-Type": "application/problem+json", "Retry-After": "601" } }) });

  await assert.rejects(client.login({}), (error) => {
    assert.ok(error instanceof ApiError);
    assert.equal(error.status, 429);
    assert.equal(error.code, "account_temporarily_locked");
    assert.equal(error.retryAfterSeconds, 601);
    assert.equal("trace_id" in error, false);
    return true;
  });
});

test("网络失败被标记为服务不可用错误且不透传底层消息", async () => {
  const client = createApiClient({ fetchImpl: async () => { throw new Error("connect ECONNREFUSED 127.0.0.1"); } });

  await assert.rejects(client.getMe(), (error) => {
    assert.ok(error instanceof ApiError);
    assert.equal(error.kind, "unavailable");
    assert.equal(error.message.includes("ECONNREFUSED"), false);
    return true;
  });
});

test("fetch AbortError 原样向调用方抛出", async () => {
  const expected = new DOMException("request cancelled", "AbortError");
  const client = createApiClient({ fetchImpl: async () => { throw expected; } });

  await assert.rejects(client.getMe(), (error) => {
    assert.equal(error, expected);
    return true;
  });
});

test("request 将 AbortSignal 原样传给 fetch", async () => {
  const controller = new AbortController();
  let receivedSignal;
  const client = createApiClient({ fetchImpl: async (_url, options) => {
    receivedSignal = options.signal;
    return jsonResponse({ data: [] });
  } });

  await client.request("/api/v1/jobs?limit=100", { signal: controller.signal });

  assert.equal(receivedSignal, controller.signal);
});

test("download 保留 CSRF 防护并以附件返回二进制内容", async () => {
  const calls = [];
  const client = createApiClient({
    fetchImpl: async (path, options) => {
      calls.push({ path, options });
      if (path === "/api/v1/me") {
        return new Response(JSON.stringify({ data: { id: "user-1" } }), {
          status: 200,
          headers: { "Content-Type": "application/json", "X-CSRF-Token": "csrf-token" },
        });
      }
      return new Response(new Blob(["resume bytes"], { type: "application/pdf" }), {
        status: 200,
        headers: { "Content-Type": "application/pdf", "Content-Disposition": "attachment; filename*=UTF-8''candidate.pdf" },
      });
    },
  });

  await client.getMe();
  const result = await client.download("/api/v1/download-tickets/consume", { method: "POST", body: { token: "one-time" } });

  assert.equal(await result.blob.text(), "resume bytes");
  assert.equal(result.filename, "candidate.pdf");
  assert.equal(calls[1].options.headers.get("X-CSRF-Token"), "csrf-token");
  assert.equal(calls[1].options.headers.get("Content-Type"), "application/json");
  assert.equal(calls[1].options.body, JSON.stringify({ token: "one-time" }));
});

test("normal request 401 clears CSRF and notifies the unauthorized handler once", async () => {
  const calls = [];
  const responses = [
    jsonResponse({ data: { id: "user-1" } }, { headers: { "X-CSRF-Token": "csrf-token" } }),
    jsonResponse({ code: "authentication_required" }, { status: 401 }),
    jsonResponse({ data: { ok: true } }),
  ];
  let notifications = 0;
  const client = createApiClient({ fetchImpl: async (path, options) => {
    calls.push({ path, options });
    return responses.shift();
  } });
  assert.equal(typeof client.setUnauthorizedHandler, "function");
  client.setUnauthorizedHandler(() => { notifications += 1; });

  await client.getMe();
  await assert.rejects(client.request("/api/v1/jobs", { method: "POST", body: {} }), ApiError);
  await client.request("/api/v1/jobs", { method: "POST", body: {} });

  assert.equal(notifications, 1);
  assert.equal(calls[1].options.headers.get("X-CSRF-Token"), "csrf-token");
  assert.equal(calls[2].options.headers.get("X-CSRF-Token"), null);
});

test("401 notification carries the auth epoch captured when the request started", async () => {
  let resolveOldRequest;
  const epochs = [];
  const client = createApiClient({
    fetchImpl: () => new Promise((resolve) => { resolveOldRequest = resolve; }),
  });
  client.setUnauthorizedHandler((requestEpoch) => { epochs.push(requestEpoch); });

  const oldRequest = client.request("/api/v1/jobs");
  assert.equal(client.advanceAuthEpoch(), 1);
  resolveOldRequest(jsonResponse({ code: "authentication_required" }, { status: 401 }));

  await assert.rejects(oldRequest, ApiError);
  assert.deepEqual(epochs, [0]);
  assert.equal(client.getAuthEpoch(), 1);
});

test("download 401 notifies the unauthorized handler once", async () => {
  let notifications = 0;
  const client = createApiClient({ fetchImpl: async () => jsonResponse(null, { status: 401 }) });
  assert.equal(typeof client.setUnauthorizedHandler, "function");
  client.setUnauthorizedHandler(() => { notifications += 1; });

  await assert.rejects(client.download("/api/v1/download-tickets/consume", { method: "POST" }), ApiError);

  assert.equal(notifications, 1);
});

test("login 401 does not notify the unauthorized handler", async () => {
  let notifications = 0;
  const client = createApiClient({ fetchImpl: async () => jsonResponse(null, { status: 401 }) });
  assert.equal(typeof client.setUnauthorizedHandler, "function");
  client.setUnauthorizedHandler(() => { notifications += 1; });

  await assert.rejects(client.login({ email: "hr@example.test", password: "wrong" }), ApiError);

  assert.equal(notifications, 0);
});

test("GET /me 401 does not notify the unauthorized handler", async () => {
  let notifications = 0;
  const client = createApiClient({ fetchImpl: async () => jsonResponse(null, { status: 401 }) });
  assert.equal(typeof client.setUnauthorizedHandler, "function");
  client.setUnauthorizedHandler(() => { notifications += 1; });

  await assert.rejects(client.getMe(), ApiError);

  assert.equal(notifications, 0);
});

test("unauthorized handler exceptions do not mask the safe API error", async () => {
  const client = createApiClient({ fetchImpl: async () => jsonResponse({
    code: "authentication_required",
    title: "Authentication required",
    detail: "Sign in again.",
  }, { status: 401, headers: { "Content-Type": "application/problem+json" } }) });
  client.setUnauthorizedHandler(() => { throw new Error("subscriber internals"); });

  await assert.rejects(client.request("/api/v1/jobs"), (error) => {
    assert.ok(error instanceof ApiError);
    assert.equal(error.status, 401);
    assert.equal(error.code, "authentication_required");
    assert.equal(error.detail, "Sign in again.");
    assert.equal(error.message.includes("subscriber internals"), false);
    return true;
  });
});

test("unauthorized unregister only clears the handler it registered", async () => {
  let firstNotifications = 0;
  let secondNotifications = 0;
  const client = createApiClient({ fetchImpl: async () => jsonResponse(null, { status: 401 }) });
  const unregisterFirst = client.setUnauthorizedHandler(() => { firstNotifications += 1; });
  const unregisterSecond = client.setUnauthorizedHandler(() => { secondNotifications += 1; });

  assert.equal(typeof unregisterFirst, "function");
  assert.equal(typeof unregisterSecond, "function");
  unregisterFirst();
  await assert.rejects(client.request("/api/v1/jobs"), ApiError);

  assert.equal(firstNotifications, 0);
  assert.equal(secondNotifications, 1);

  unregisterSecond();
  await assert.rejects(client.request("/api/v1/jobs"), ApiError);
  assert.equal(secondNotifications, 1);
});

test("organization and account methods follow the fixed API contract", async () => {
  const calls = [];
  const responses = [
    jsonResponse({ data: [{ id: "dep-1", name: "技术部" }] }),
    jsonResponse({ data: { id: "dep-2", name: "产品部" } }),
    jsonResponse({ data: { id: "dep-2", name: "产品部", status: "active" } }),
    jsonResponse({ data: { id: "dep-2", name: "平台部", status: "inactive" } }),
    jsonResponse({ data: [{ id: "user-1", display_name: "林岚" }] }),
    jsonResponse({ data: { user: { id: "user-2" }, invitation: { token: "once" } } }),
    jsonResponse({ data: { id: "user-1", recruiting_scope_type: "departments", recruiting_department_ids: ["dep-1"] } }),
    jsonResponse({ data: { email: "lin@example.test", organization_slug: "acme" } }),
    new Response(null, { status: 204 }),
  ];
  const client = createApiClient({ fetchImpl: async (url, options) => {
    calls.push({ url, options });
    return responses.shift();
  } });

  assert.deepEqual(await client.listDepartments(), [{ id: "dep-1", name: "技术部" }]);
  assert.deepEqual(await client.createDepartment({ name: "产品部", parent_id: null }), { id: "dep-2", name: "产品部" });
  assert.deepEqual(await client.getDepartment("dep/2"), { id: "dep-2", name: "产品部", status: "active" });
  assert.deepEqual(await client.updateDepartment("dep/2", { name: "平台部", status: "inactive" }), { id: "dep-2", name: "平台部", status: "inactive" });
  assert.deepEqual(await client.listUsers(), [{ id: "user-1", display_name: "林岚" }]);
  assert.deepEqual(await client.inviteUser({ display_name: "周宁", email: "zhou@example.test", department_id: "dep-1", role: "recruiter" }, { idempotencyKey: "invite-key" }), { user: { id: "user-2" }, invitation: { token: "once" } });
  assert.deepEqual(await client.updateRecruitingScope("user/1", { recruiting_scope_type: "departments", recruiting_department_ids: ["dep-1"] }), { id: "user-1", recruiting_scope_type: "departments", recruiting_department_ids: ["dep-1"] });
  assert.deepEqual(await client.acceptInvitation({ token: "once", password: "a-secure-password" }), { email: "lin@example.test", organization_slug: "acme" });
  assert.equal(await client.changePassword({ current_password: "old-password", new_password: "new-secure-password" }), null);

  assert.deepEqual(calls.map(({ url, options }) => [url, options.method]), [
    ["/api/v1/settings/departments", "GET"],
    ["/api/v1/settings/departments", "POST"],
    ["/api/v1/settings/departments/dep%2F2", "GET"],
    ["/api/v1/settings/departments/dep%2F2", "PATCH"],
    ["/api/v1/settings/users", "GET"],
    ["/api/v1/settings/users", "POST"],
    ["/api/v1/settings/users/user%2F1/recruiting-scope", "PATCH"],
    ["/api/v1/auth/invitations/accept", "POST"],
    ["/api/v1/me/password", "POST"],
  ]);
  assert.equal(calls[5].options.headers.get("Idempotency-Key"), "invite-key");
  assert.deepEqual(JSON.parse(calls[6].options.body), { recruiting_scope_type: "departments", recruiting_department_ids: ["dep-1"] });
  assert.deepEqual(JSON.parse(calls[7].options.body), { token: "once", password: "a-secure-password" });
  assert.deepEqual(JSON.parse(calls[8].options.body), { current_password: "old-password", new_password: "new-secure-password" });
});
