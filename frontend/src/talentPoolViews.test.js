import test from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";

const source = readFileSync(new URL("./TalentPoolViews.jsx", import.meta.url), "utf8");

test("talent members select recommended jobs from real positions instead of entering free text", () => {
  assert.match(source, /aria-label="推荐岗位"/);
  assert.match(source, /type="checkbox"/);
  assert.doesNotMatch(source, /编辑适合岗位<input/);
});

test("talent tags can be removed and reactivation is exposed as a direct action", () => {
  assert.match(source, /aria-label={`移除标签：\$\{item\}`}/);
  assert.match(source, /重新激活到职位/);
  assert.match(source, /initialReactivateOpen/);
});

test("the system deferred pool is selected by system key and exposes recovery evidence", () => {
  assert.match(source, /pool\.systemKey === "ai_screening_deferred"/);
  assert.match(source, /原岗位/);
  assert.match(source, /AI 匹配分/);
  assert.match(source, /进入人才库时间/);
  assert.match(source, /主要缺口/);
  assert.match(source, /跟进负责人/);
  assert.doesNotMatch(source, /pool\.name === [^\n]*ai_screening_deferred/);
});

test("deferred rows refer once while ordinary pools keep reactivation", () => {
  assert.match(source, /转交用人经理/);
  assert.match(source, /已转交用人经理/);
  assert.match(source, /member\.sourceStage === "用人经理复核"/);
  assert.match(source, /disabled=\{member\.sourceStage === "用人经理复核" \|\| Boolean\(referringMemberId\)\}/);
  assert.match(source, /重新激活/);
});

test("deferred talent rows expose table semantics and mobile field labels", () => {
  assert.match(source, /className="talent-table" role="table" aria-label="AI 初筛未进入评审人才"/);
  assert.match(source, /className="talent-table-head" role="row"/);
  assert.equal((source.match(/role="columnheader"/g) || []).length >= 8, true);
  for (const label of ["人才", "原岗位", "AI 匹配分", "进入人才库时间", "主要缺口", "跟进负责人", "状态", "操作"]) {
    assert.match(source, new RegExp(`role="cell" data-label="${label}"`));
  }
  const css = readFileSync(new URL("./product-theme-people.css", import.meta.url), "utf8");
  assert.match(css, /\.talent-table-row > \[role="cell"\]::before[\s\S]*content: attr\(data-label\)/);
});

test("membership request generations reject a late response from the previous pool", async () => {
  const support = await import("./talentController.js");
  assert.equal(typeof support.createLatestMembershipRequest, "function");
  const requests = support.createLatestMembershipRequest();
  const accepted = [];
  let resolveA;
  const responseA = new Promise((resolve) => { resolveA = resolve; });
  const a = requests.start();
  const completionA = responseA.then((value) => { if (a.isCurrent()) accepted.push(value); });
  const b = requests.start();
  accepted.push(b.isCurrent() ? "pool-b" : "invalid-b");
  resolveA("pool-a");
  await completionA;

  assert.equal(a.signal.aborted, true);
  assert.deepEqual(accepted, ["pool-b"]);
  assert.match(source, /memberLoadRef\.current\.start\(\)/);
  assert.match(source, /signal: operation\.signal/);
  assert.match(source, /operation\.isCurrent\(\)/);
});

test("referral replaces the membership in place and refreshes the workbench without changing route state", () => {
  assert.match(source, /controller\.referToReview\(member\.id, member\.version\)/);
  assert.match(source, /memberships: current\.memberships\.map\(\(item\) => item\.id === result\.membership\.id \? result\.membership : item\)/);
  assert.match(source, /onReferralComplete\(result\.application\)/);
  assert.doesNotMatch(source, /referToReview[\s\S]{0,500}setSelectedPoolId/);
  assert.doesNotMatch(source, /referToReview[\s\S]{0,500}setMode/);
});

test("referral status is perceivable and the primary action does not depend on hover", () => {
  assert.match(source, /role="status"/);
  assert.match(source, /aria-live="polite"/);
  assert.match(source, /aria-label=\{`转交用人经理：\$\{candidate\.name\}`\}/);
  assert.doesNotMatch(source, /onMouseEnter|onMouseLeave/);
});
