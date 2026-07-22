import test from "node:test";
import assert from "node:assert/strict";

import {
  addTalentMemberships,
  applyScreeningResults,
  reactivateTalentCandidate,
  saveInterview,
  submitInterviewFeedback,
  transitionCandidate,
  validateWorkflowState,
} from "./ux08Workflow.js";

function stateFixture() {
  return {
    positions: [
      { id: "JOB-AI", name: "AI 工程师", candidates: 0, review: 0, interview: 0, owner: "张小北" },
      { id: "JOB-JAVA", name: "Java 后端工程师", candidates: 0, review: 0, interview: 0, owner: "陈雨" },
    ],
    candidates: [],
    interviews: [],
    pools: [{ id: "POOL-1", name: "长期关注", memberIds: [] }],
    memberships: [],
  };
}

test("screening results create one candidate per successful file", () => {
  const task = { id: "SCR-1", position: "AI 工程师", source: "合成测试", creator: "张小北" };
  const files = [
    { id: "SYN-1", candidate: "林启舟", email: "lin@example.com", status: "success", ruleScore: 88, llmScore: 84, recommendation: "优先沟通" },
    { id: "SYN-1-V2", candidate: "林启舟", email: "lin@example.com", status: "success", ruleScore: 90, llmScore: 86, recommendation: "优先沟通" },
    { id: "SYN-2", candidate: "唐予安", email: "tang@example.com", status: "failed" },
  ];
  const next = applyScreeningResults(stateFixture(), { task, files, targetStage: "待复核" });
  assert.equal(next.candidates.length, 1);
  assert.equal(next.candidates[0].applications.length, 1);
  assert.equal(next.candidates[0].stage, "待复核");
  assert.equal(next.positions[0].candidates, 1);
  assert.equal(next.positions[0].review, 1);
});

test("scheduling moves a candidate to interview and updates position counts", () => {
  const base = applyScreeningResults(stateFixture(), {
    task: { id: "SCR-1", position: "AI 工程师", source: "合成测试", creator: "张小北" },
    files: [{ id: "SYN-1", candidate: "林启舟", email: "lin@example.com", status: "success", ruleScore: 88 }],
    targetStage: "待安排",
  });
  const next = saveInterview(base, { id: "INT-1", candidateId: base.candidates[0].id, round: "一面", dateLabel: "明天", time: "10:00", interviewers: ["王磊"], status: "已安排", feedbackStatus: "待提交" });
  assert.equal(next.candidates[0].stage, "面试中");
  assert.equal(next.positions[0].interview, 1);
  assert.equal(next.interviews.length, 1);
});

test("submitted feedback moves the candidate to decision", () => {
  const base = saveInterview(applyScreeningResults(stateFixture(), {
    task: { id: "SCR-1", position: "AI 工程师", source: "合成测试", creator: "张小北" },
    files: [{ id: "SYN-1", candidate: "林启舟", email: "lin@example.com", status: "success" }],
    targetStage: "待安排",
  }), { id: "INT-1", candidateId: "CAN-SYN-1", round: "一面", dateLabel: "今天", time: "10:00", interviewers: ["王磊"], status: "已完成", feedbackStatus: "待提交" });
  const next = submitInterviewFeedback(base, "INT-1", { conclusion: "推荐", strengths: "技术基础扎实", submittedBy: "王磊" });
  assert.equal(next.candidates[0].stage, "待决策");
  assert.equal(next.interviews[0].feedbackStatus, "已提交");
});

test("talent membership preserves the original application", () => {
  const base = applyScreeningResults(stateFixture(), {
    task: { id: "SCR-1", position: "AI 工程师", source: "合成测试", creator: "张小北" },
    files: [{ id: "SYN-1", candidate: "林启舟", email: "lin@example.com", status: "success" }],
    targetStage: "待复核",
  });
  const before = structuredClone(base.candidates[0].applications);
  const next = addTalentMemberships(base, { candidateIds: [base.candidates[0].id], poolId: "POOL-1", actor: "张小北" });
  assert.deepEqual(next.candidates[0].applications, before);
  assert.equal(next.memberships.length, 1);
  assert.deepEqual(next.pools[0].memberIds, [base.candidates[0].id]);
});

test("reactivation creates a linked application and blocks an active duplicate", () => {
  const base = addTalentMemberships(applyScreeningResults(stateFixture(), {
    task: { id: "SCR-1", position: "AI 工程师", source: "合成测试", creator: "张小北" },
    files: [{ id: "SYN-1", candidate: "林启舟", email: "lin@example.com", status: "success" }],
    targetStage: "已淘汰",
  }), { candidateIds: ["CAN-SYN-1"], poolId: "POOL-1", actor: "张小北" });
  const created = reactivateTalentCandidate(base, { candidateId: "CAN-SYN-1", position: base.positions[1], poolId: "POOL-1", resumeVersion: "v2" });
  assert.equal(created.created, true);
  assert.equal(created.state.candidates[0].applications[0].sourceApplicationId, "APP-SCR-1-SYN-1");
  const duplicate = reactivateTalentCandidate(created.state, { candidateId: "CAN-SYN-1", position: base.positions[1], poolId: "POOL-1", resumeVersion: "v2" });
  assert.equal(duplicate.created, false);
  assert.equal(duplicate.reason, "active-duplicate");
});

test("reactivation counts every active application under its own position", () => {
  const base = applyScreeningResults(stateFixture(), {
    task: { id: "SCR-1", position: "AI 工程师", source: "合成测试", creator: "张小北" },
    files: [{ id: "SYN-1", candidate: "林启舟", email: "lin@example.com", status: "success" }],
    targetStage: "待复核",
  });
  const created = reactivateTalentCandidate(base, {
    candidateId: "CAN-SYN-1",
    position: base.positions[1],
    poolId: "POOL-1",
    resumeVersion: "v2",
  });

  assert.equal(created.state.positions[0].candidates, 1);
  assert.equal(created.state.positions[0].review, 1);
  assert.equal(created.state.positions[1].candidates, 1);
  assert.equal(created.state.positions[1].review, 0);
});

test("validator reports application-derived position count mismatches", () => {
  const base = applyScreeningResults(stateFixture(), {
    task: { id: "SCR-1", position: "AI 工程师", source: "合成测试", creator: "张小北" },
    files: [{ id: "SYN-1", candidate: "林启舟", email: "lin@example.com", status: "success" }],
    targetStage: "待复核",
  });
  const created = reactivateTalentCandidate(base, {
    candidateId: "CAN-SYN-1",
    position: base.positions[1],
    poolId: "POOL-1",
    resumeVersion: "v2",
  });
  const inconsistent = {
    ...created.state,
    positions: created.state.positions.map((position) => position.id === "JOB-AI"
      ? { ...position, candidates: 0, review: 0 }
      : position),
  };

  assert.match(validateWorkflowState(inconsistent).join("\n"), /JOB-AI/);
});

test("feedback updates only the current application and adds one feedback event", () => {
  const original = applyScreeningResults(stateFixture(), {
    task: { id: "SCR-1", position: "AI 工程师", source: "合成测试", creator: "张小北" },
    files: [{ id: "SYN-1", candidate: "林启舟", email: "lin@example.com", status: "success" }],
    targetStage: "待复核",
  });
  const reactivated = reactivateTalentCandidate(original, {
    candidateId: "CAN-SYN-1",
    position: original.positions[1],
    poolId: "POOL-1",
    resumeVersion: "v2",
  }).state;
  const scheduled = saveInterview(reactivated, {
    id: "INT-2",
    candidateId: "CAN-SYN-1",
    round: "一面",
    dateLabel: "明天",
    time: "10:00",
    interviewers: ["王磊"],
    status: "已安排",
    feedbackStatus: "待提交",
  });
  const beforeTimelineLength = scheduled.candidates[0].timeline.length;
  const next = submitInterviewFeedback(scheduled, "INT-2", {
    conclusion: "推荐",
    strengths: "技术基础扎实",
    submittedBy: "王磊",
  });
  const candidate = next.candidates[0];

  assert.equal(candidate.applications.find((application) => application.position === "Java 后端工程师").state, "待决策");
  assert.equal(candidate.applications.find((application) => application.position === "AI 工程师").state, "待复核");
  assert.equal(candidate.timeline.length, beforeTimelineLength + 1);
  assert.equal(candidate.timeline.filter((event) => event.action.includes("更新一面安排")).length, 1);
  assert.equal(candidate.timeline.filter((event) => event.action.includes("收到一面反馈")).length, 1);
});

test("candidate workflow updates leave an older application for the same position unchanged", () => {
  const original = applyScreeningResults(stateFixture(), {
    task: { id: "SCR-OLD", position: "AI 工程师", source: "合成测试", creator: "张小北" },
    files: [{ id: "SYN-1", candidate: "林启舟", email: "lin@example.com", status: "success" }],
    targetStage: "已淘汰",
  });
  const reactivated = reactivateTalentCandidate(original, {
    candidateId: "CAN-SYN-1",
    position: original.positions[0],
    poolId: "POOL-1",
    resumeVersion: "v2",
  }).state;
  const transitioned = transitionCandidate(reactivated, "CAN-SYN-1", "待安排");
  const scheduled = saveInterview(transitioned, {
    id: "INT-SAME-POSITION",
    candidateId: "CAN-SYN-1",
    round: "一面",
    dateLabel: "明天",
    time: "10:00",
    interviewers: ["王磊"],
    status: "已安排",
    feedbackStatus: "待提交",
  });
  const next = submitInterviewFeedback(scheduled, "INT-SAME-POSITION", {
    conclusion: "推荐",
    strengths: "技术基础扎实",
    submittedBy: "王磊",
  });
  const [currentApplication, oldApplication] = next.candidates[0].applications;

  assert.equal(currentApplication.state, "待决策");
  assert.equal(oldApplication.state, "已淘汰");
});

test("validator reports no dangling interview or membership references", () => {
  const base = applyScreeningResults(stateFixture(), {
    task: { id: "SCR-1", position: "AI 工程师", source: "合成测试", creator: "张小北" },
    files: [{ id: "SYN-1", candidate: "林启舟", email: "lin@example.com", status: "success" }],
    targetStage: "待安排",
  });
  const next = addTalentMemberships(saveInterview(base, { id: "INT-1", candidateId: "CAN-SYN-1", round: "一面", dateLabel: "明天", time: "10:00", interviewers: ["王磊"], status: "已安排", feedbackStatus: "待提交" }), { candidateIds: ["CAN-SYN-1"], poolId: "POOL-1", actor: "张小北" });
  assert.deepEqual(validateWorkflowState(next), []);
  assert.match(validateWorkflowState({ ...next, interviews: [{ id: "INT-X", candidateId: "MISSING" }] })[0], /INT-X/);
});
