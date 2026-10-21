import test from "node:test";
import assert from "node:assert/strict";
import { createApiClient } from "./apiClient.js";

test("self-service reset sends organization and email without credentials or a password", async () => {
  const calls = [];
  const client = createApiClient({ fetchImpl: async (url, options) => {
    calls.push({ url, options });
    return new Response(JSON.stringify({ data: { requested: true } }), { status: 202, headers: { "Content-Type": "application/json" } });
  } });
  assert.deepEqual(await client.requestPasswordResetEmail({ organization_slug: "acme", email: "member@example.test" }), { requested: true });
  assert.equal(calls[0].url, "/api/v1/auth/password-reset/request");
  assert.equal(calls[0].options.method, "POST");
  assert.equal(calls[0].options.headers.has("X-CSRF-Token"), false);
  assert.deepEqual(JSON.parse(calls[0].options.body), { organization_slug: "acme", email: "member@example.test" });
});
