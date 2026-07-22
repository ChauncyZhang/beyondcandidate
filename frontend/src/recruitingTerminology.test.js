import assert from "node:assert/strict";
import test from "node:test";

import {
  aiRecommendationLabel,
  applicationStageLabel,
  interviewStatusLabel,
} from "./recruitingTerminology.js";

test("application stages identify the responsible actor and next business outcome", () => {
  assert.equal(applicationStageLabel("待复核"), "待用人经理评审");
  assert.equal(applicationStageLabel("待沟通"), "待确认候选人意向");
  assert.equal(applicationStageLabel("待安排"), "待安排面试");
  assert.equal(applicationStageLabel("面试中"), "面试流程中");
  assert.equal(applicationStageLabel("待决策"), "待用人经理录用决策");
  assert.equal(applicationStageLabel("已通过"), "待录用确认");
});

test("AI recommendations describe routing rather than a hiring decision", () => {
  assert.equal(aiRecommendationLabel("优先评审"), "进入评审 · 高优先级");
  assert.equal(aiRecommendationLabel("建议评审"), "进入评审");
  assert.equal(aiRecommendationLabel("暂缓"), "暂不进入评审");
  assert.equal(aiRecommendationLabel("AI评分不可用"), "AI评分失败 · 已保护性转交评审");
});

test("interview labels distinguish interview, invitation, and feedback state", () => {
  assert.equal(interviewStatusLabel("待确认"), "待 HR 确认排期");
  assert.equal(interviewStatusLabel("待发送"), "面试邀请未发送");
  assert.equal(interviewStatusLabel("待反馈"), "待面试官反馈");
  assert.equal(interviewStatusLabel("已提交"), "反馈已提交");
});
