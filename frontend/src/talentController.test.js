import test from "node:test";
import assert from "node:assert/strict";

import { ApiError } from "./apiClient.js";
import {
  buildReactivatedCandidateSummary,
  createTalentController,
  canAddCandidateToTalentPool,
  normalizeTalentMembership,
  normalizeTalentPool,
  selectExactTalentPool,
  selectServerTalentCandidates,
} from "./talentController.js";


test("talent pool and membership normalization keeps only the server projection", () => {
  const pool = normalizeTalentPool({
    id: "pool-1", name: "AI 人才", purpose: "AI", visibility: "recruiting_team",
    system_key: "ai_screening_deferred",
    owner: { id: "user-1", display_name: "HR" }, suitable_roles: ["AI 工程师"],
    retention_days: 730, member_count: 1, version: 2, updated_at: "2026-07-14T08:00:00Z",
  });
  const membership = normalizeTalentMembership({
    id: "member-1", pool_id: "pool-1", candidate: { id: "candidate-1", display_name: "候选人", current_title: "算法工程师", location: "北京", email: "must-not-pass@example.test" },
    source_application: { id: "application-1", job_id: "job-1", job_title: "AI 工程师", stage: "rejected", human_conclusion: "后续联系" },
    owner: { id: "user-1", display_name: "HR" }, suitable_roles: ["AI 工程师"], tags: ["RAG"], reason: "保留", status: "active", version: 1,
    retention_until: "2028-07-14T00:00:00Z", created_at: "2026-07-14T00:00:00Z", updated_at: "2026-07-14T00:00:00Z",
  });

  assert.equal(pool.memberCount, 1);
  assert.equal(pool.visibility, "招聘团队可见");
  assert.equal(pool.systemKey, "ai_screening_deferred");
  assert.equal(membership.candidate.name, "候选人");
  assert.equal(membership.sourceApplicationId, "application-1");
  assert.equal(membership.candidate.applicationId, "application-1");
  assert.equal(membership.candidate.jobId, "job-1");
  assert.equal(membership.candidate.position, "AI 工程师");
  assert.equal(membership.candidate.applications[0].state, "已淘汰");
  assert.equal(membership.candidate.email, "");
  assert.doesNotMatch(JSON.stringify(membership), /must-not-pass/);
});

test("deferred membership normalization keeps the durable review recovery projection", () => {
  const membership = normalizeTalentMembership({
    id: "member-1",
    pool_id: "pool-1",
    candidate: { id: "candidate-1", display_name: "候选人" },
    source_application: {
      id: "application-1",
      job_id: "job-1",
      job_title: "平台工程师",
      stage: "deferred",
      human_conclusion: "AI 初筛暂缓",
    },
    deferred_screening: {
      final_score: 58,
      deferred_at: "2026-07-20T03:04:05Z",
      main_gaps: ["系统设计深度不足", "缺少带队经验"],
    },
    owner: { id: "owner-1", display_name: "招聘负责人" },
    suitable_roles: [], tags: [], reason: "AI 初筛暂缓", status: "active", version: 3,
    retention_until: "2028-07-20T00:00:00Z",
  });

  assert.deepEqual(membership.originalJob, { id: "job-1", title: "平台工程师" });
  assert.equal(membership.sourceStage, "AI 初筛暂缓");
  assert.equal(membership.finalScore, 58);
  assert.equal(membership.deferredAt, "2026-07-20");
  assert.deepEqual(membership.mainGaps, ["系统设计深度不足", "缺少带队经验"]);
  assert.equal(membership.ownerId, "owner-1");
  assert.equal(membership.owner, "招聘负责人");
});

test("talent membership maps every active recovery stage", () => {
  const expected = {
    review: "用人经理复核",
    deferred: "AI 初筛暂缓",
    contact: "待沟通",
    interview_pending: "待安排",
    interviewing: "面试中",
    passed: "已通过",
  };

  for (const [stage, label] of Object.entries(expected)) {
    const membership = normalizeTalentMembership({
      id: `member-${stage}`,
      pool_id: "pool-1",
      candidate: { id: `candidate-${stage}`, display_name: "候选人" },
      source_application: { id: `application-${stage}`, job_id: "job-1", job_title: "平台工程师", stage },
      owner: { id: "owner-1", display_name: "招聘负责人" },
      suitable_roles: [], tags: [], status: "active", version: 1,
      retention_until: "2028-07-20T00:00:00Z",
    });
    assert.equal(membership.sourceStage, label);
    assert.equal(membership.candidate.applications[0].state, label);
  }
});

test("reactivation success opens the newly created application, not its source", () => {
  const sourceCandidate = {
    id: "candidate-1",
    candidateId: "candidate-1",
    applicationId: "application-source",
    jobId: "job-source",
    position: "历史职位",
    serverBacked: true,
  };

  assert.deepEqual(buildReactivatedCandidateSummary(sourceCandidate, {
    id: "application-new",
    candidate_id: "candidate-1",
    job_id: "job-new",
  }, { name: "新职位" }), {
    ...sourceCandidate,
    applicationId: "application-new",
    jobId: "job-new",
    position: "新职位",
  });
});


test("talent controller uses cursor APIs and versioned mutations", async () => {
  const calls = [];
  const client = {
    async request(path, options = {}) {
      calls.push({ path, options });
      if (path.startsWith("/api/v1/talent-pools?")) return { data: [], meta: { next_cursor: "next" } };
      if (path.endsWith("/memberships?limit=25")) return { data: [], meta: { next_cursor: null } };
      if (options.method === "PATCH") return { data: { id: "member-1", pool_id: "pool-1", candidate: { id: "candidate-1", display_name: "候选人" }, owner: { id: "user-1", display_name: "HR" }, suitable_roles: ["AI"], tags: [], reason: "保留", retention_until: "2028-01-01T00:00:00Z", status: "active", version: 4 } };
      if (path.endsWith("/reactivations")) return { data: { id: "application-2" } };
      return null;
    },
  };
  const controller = createTalentController({ client, idSource: () => "idem-1" });

  assert.equal((await controller.listPools({ limit: 25 })).nextCursor, "next");
  await controller.listMemberships("pool-1", { limit: 25 });
  const updated = await controller.updateMembership({ id: "member-1", version: 3, ownerId: "user-1", suitableRoles: ["AI"], tags: [], reason: "保留", retentionUntil: "2028-01-01", status: "正常" });
  await controller.removeMembership(updated, "岗位方向变化");
  await controller.reactivate("member-1", "job-2");

  assert.equal(calls[0].path, "/api/v1/talent-pools?limit=25");
  assert.equal(calls[1].path, "/api/v1/talent-pools/pool-1/memberships?limit=25");
  assert.equal(calls[2].options.ifMatch, '"3"');
  assert.equal(calls[3].options.method, "DELETE");
  assert.equal(calls[3].options.ifMatch, '"4"');
  assert.equal(calls[4].options.idempotencyKey, "idem-1");
});


test("create and add commands use opaque server identities and idempotency keys", async () => {
  const calls = [];
  const client = { async request(path, options) { calls.push({ path, options }); return { data: null }; } };
  const controller = createTalentController({ client, idSource: () => "idem-fixed" });

  await controller.createPool({ name: "AI", purpose: "AI", visibility: "招聘团队可见", suitableRoles: ["AI 工程师"], retentionDays: 730 }, "user-1");
  await controller.addMembership("pool-1", { candidateId: "candidate-1", applicationId: "application-1", position: "AI 工程师", tags: [] }, "user-1");

  assert.equal(calls[0].options.body.owner_id, "user-1");
  assert.equal(calls[0].options.idempotencyKey, "idem-fixed");
  assert.equal(calls[1].options.body.source_application_id, "application-1");
  assert.equal(calls[1].options.idempotencyKey, "idem-fixed");
});


test("server candidates require an explicitly selected exact talent pool", () => {
  const candidates = [
    { id: "local-1", serverBacked: false },
    { id: "server-1", candidateId: "candidate-1", applicationId: "application-1", serverBacked: true },
    { id: "server-2", candidateId: "candidate-2", serverBacked: true },
  ];
  const pools = [{ id: "pool-1", name: "AI" }, { id: "pool-2", name: "Backend" }];

  assert.deepEqual(selectServerTalentCandidates(candidates, ["server-1"]), [candidates[1]]);
  assert.equal(canAddCandidateToTalentPool(candidates[1]), true);
  assert.equal(canAddCandidateToTalentPool(candidates[0]), false);
  assert.equal(canAddCandidateToTalentPool(candidates[2]), false);
  assert.deepEqual(selectServerTalentCandidates(candidates, ["server-2"]), []);
  assert.equal(selectExactTalentPool(pools, "pool-2"), pools[1]);
  assert.equal(selectExactTalentPool(pools, "missing"), null);
  assert.equal(selectExactTalentPool(pools, null), null);
});


test("redacted source applications do not reconstruct restricted job details", () => {
  const membership = normalizeTalentMembership({
    id: "member-1",
    pool_id: "pool-1",
    candidate: { id: "candidate-1", display_name: "Candidate" },
    source_application: { id: "restricted-application", redacted: true },
    owner: { id: "user-1", display_name: "HR" },
    deferred_screening: { final_score: 99, deferred_at: "2026-07-20T03:04:05Z", main_gaps: ["secret-gap"] },
    suitable_roles: ["cached-secret-job"],
    tags: ["score-99", "secret-gap"],
    reason: "retain",
    retention_until: "2028-07-14T00:00:00Z",
    status: "active",
    version: 1,
  });

  assert.equal(membership.sourceApplicationId, "");
  assert.deepEqual(membership.candidate.applications, []);
  assert.equal(membership.source, "来源申请不可见");
  assert.equal(membership.latestConclusion, "来源申请不可见");
  assert.deepEqual(membership.originalJob, { id: "", title: "" });
  assert.equal(membership.sourceStage, "来源申请不可见");
  assert.equal(membership.finalScore, null);
  assert.equal(membership.deferredAt, "");
  assert.deepEqual(membership.mainGaps, []);
  assert.doesNotMatch(JSON.stringify({
    originalJob: membership.originalJob,
    sourceStage: membership.sourceStage,
    finalScore: membership.finalScore,
    deferredAt: membership.deferredAt,
    mainGaps: membership.mainGaps,
  }), /restricted-application|cached-secret-job|score-99|secret-gap/);
});

test("review referral retries ambiguously with one key and body, then returns the same source application", async () => {
  const calls = [];
  let sequence = 0;
  let attempt = 0;
  const controller = createTalentController({
    idSource: () => `referral-${++sequence}`,
    client: {
      async request(path, options) {
        calls.push({ path, options });
        attempt += 1;
        if (attempt === 1) throw new ApiError({ status: 0, kind: "unavailable", code: "service_unavailable" });
        return {
          data: {
            application: { id: "application-1", stage: "review", version: 4 },
            membership: {
              id: "member-1", pool_id: "pool-1", candidate: { id: "candidate-1", display_name: "候选人" },
              source_application: { id: "application-1", job_id: "job-1", job_title: "平台工程师", stage: "review" },
              deferred_screening: { final_score: 58, deferred_at: "2026-07-20T03:04:05Z", main_gaps: ["系统设计深度不足"] },
              owner: { id: "owner-1", display_name: "招聘负责人" }, suitable_roles: [], tags: [], status: "active", version: 4,
              retention_until: "2028-07-20T00:00:00Z",
            },
          },
        };
      },
    },
  });

  await assert.rejects(controller.referToReview("member-1", 3));
  const result = await controller.referToReview("member-1", 3);

  assert.equal(calls[0].path, "/api/v1/talent-pool-memberships/member-1/review-referrals");
  assert.equal(calls[0].options.method, "POST");
  assert.equal(calls[0].options.ifMatch, '"3"');
  assert.deepEqual(calls[0].options.body, {});
  assert.equal(calls[0].options.idempotencyKey, calls[1].options.idempotencyKey);
  assert.equal(calls[0].options.body, calls[1].options.body);
  assert.equal(result.membership.sourceApplicationId, result.application.id);
  assert.equal(result.membership.sourceStage, "用人经理复核");
});

test("review referral clears its idempotency key after an explicit failure", async () => {
  const keys = [];
  let sequence = 0;
  let fail = true;
  const controller = createTalentController({
    idSource: () => `referral-${++sequence}`,
    client: { async request(_path, options) {
      keys.push(options.idempotencyKey);
      if (fail) { fail = false; throw new ApiError({ status: 409, kind: "conflict", code: "version_conflict" }); }
      return { data: { application: { id: "application-1" }, membership: null } };
    } },
  });

  await assert.rejects(controller.referToReview("member-1", 3));
  await controller.referToReview("member-1", 3);
  assert.notEqual(keys[0], keys[1]);
});


test("granted visibility is rejected until grantees can be selected", async () => {
  let requested = false;
  const controller = createTalentController({
    client: { async request() { requested = true; } },
    idSource: () => "unused",
  });

  await assert.rejects(
    controller.createPool({ name: "AI", purpose: "AI", visibility: "指定成员可见", suitableRoles: ["AI"], retentionDays: 730 }, "user-1"),
    (error) => error.code === "talent_grants_required",
  );
  assert.equal(requested, false);
});


test("unavailable ApiError retries reuse one idempotency key and body while distinct intents receive fresh keys", async () => {
  const calls = [];
  let failOnce = true;
  let sequence = 0;
  const controller = createTalentController({
    idSource: () => `idem-${++sequence}`,
    client: {
      async request(path, options) {
        calls.push({ path, options });
        if (failOnce) {
          failOnce = false;
          throw new ApiError({ status: 0, kind: "unavailable", code: "service_unavailable" });
        }
        return { data: null };
      },
    },
  });
  const candidate = { candidateId: "candidate-1", applicationId: "application-1", position: "AI", tags: [] };

  await assert.rejects(controller.addMembership("pool-1", candidate, "user-1"));
  await controller.addMembership("pool-1", candidate, "user-1");
  await controller.addMembership("pool-2", candidate, "user-1");

  assert.equal(calls[0].options.idempotencyKey, calls[1].options.idempotencyKey);
  assert.equal(calls[0].options.body.retention_until, calls[1].options.body.retention_until);
  assert.notEqual(calls[1].options.idempotencyKey, calls[2].options.idempotencyKey);
});
