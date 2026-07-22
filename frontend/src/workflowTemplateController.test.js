import assert from "node:assert/strict";
import test from "node:test";

import { createWorkflowTemplateController } from "./workflowTemplateController.js";

const TEMPLATE_ID = "55555555-5555-4555-8555-555555555555";

function resource(changes = {}) {
  return {
    id: TEMPLATE_ID,
    name: "技术岗位流程",
    rounds: ["技术一面", "技术二面"],
    status: "active",
    version: 3,
    created_at: "2026-07-21T01:00:00Z",
    updated_at: "2026-07-21T02:00:00Z",
    ...changes,
  };
}

test("workflow templates list and normalize server records", async () => {
  const calls = [];
  const signal = new AbortController().signal;
  const controller = createWorkflowTemplateController({ client: { async request(path, options) {
    calls.push({ path, options });
    return { data: [resource(), { id: "", name: "无效" }] };
  } } });

  const records = await controller.list({ signal });

  assert.deepEqual(calls, [{ path: "/api/v1/settings/workflow-templates", options: { signal } }]);
  assert.deepEqual(records, [{
    id: TEMPLATE_ID,
    name: "技术岗位流程",
    rounds: ["技术一面", "技术二面"],
    status: "active",
    version: 3,
    createdAt: "2026-07-21T01:00:00Z",
    updatedAt: "2026-07-21T02:00:00Z",
  }]);
});

test("workflow templates create and update with mutation guards", async () => {
  const calls = [];
  const responses = [{ data: resource({ version: 1 }) }, { data: resource({ name: "技术岗位三轮", rounds: ["一面", "二面", "终面"], version: 4 }) }];
  const controller = createWorkflowTemplateController({
    client: { async request(path, options) { calls.push({ path, options }); return responses.shift(); } },
    idempotencyKey: () => "workflow-key",
  });

  await controller.create({ name: " 技术岗位流程 ", rounds: [" 一面 ", "", " 二面 "] });
  const updated = await controller.update(resource(), { name: "技术岗位三轮", rounds: ["一面", "二面", "终面"], status: "active" });

  assert.deepEqual(calls, [
    { path: "/api/v1/settings/workflow-templates", options: { method: "POST", body: { name: "技术岗位流程", rounds: ["一面", "二面"] }, idempotencyKey: "workflow-key" } },
    { path: `/api/v1/settings/workflow-templates/${TEMPLATE_ID}`, options: { method: "PATCH", body: { name: "技术岗位三轮", rounds: ["一面", "二面", "终面"], status: "active" }, ifMatch: '"3"', idempotencyKey: "workflow-key" } },
  ]);
  assert.equal(updated.version, 4);
  assert.deepEqual(updated.rounds, ["一面", "二面", "终面"]);
});
