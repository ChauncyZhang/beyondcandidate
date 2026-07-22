import test from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";

const source = readFileSync(new URL("./InterviewViews.jsx", import.meta.url), "utf8");
const candidateSource = readFileSync(new URL("./CandidateViews.jsx", import.meta.url), "utf8");

test("feedback page loads interview-scoped materials and opens a redacted preview", () => {
  assert.match(source, /controller\.getMaterials\(record\.id/);
  assert.match(source, /function FeedbackMaterials/);
  assert.match(source, /materialsState\.data\?\.resume\?\.previewText/);
  assert.match(source, /脱敏简历预览/);
  assert.doesNotMatch(source, /onNotify\("候选人简历已打开"\)/);
});

test("materials failures are retryable without hiding the feedback form", () => {
  assert.match(source, /材料加载失败/);
  assert.match(source, /onRetry/);
});

test("candidate evidence presents persisted LLM evidence and a collapsed historical rule result", () => {
  assert.match(candidateSource, /AI 筛选结果/);
  assert.match(candidateSource, /核心能力匹配/);
  assert.match(candidateSource, /证据/);
  assert.match(candidateSource, /缺口/);
  assert.match(candidateSource, /优势/);
  assert.match(candidateSource, /风险/);
  assert.match(candidateSource, /建议问题/);
  assert.match(candidateSource, /<details[^>]*className="historical-rule"/);
  assert.match(candidateSource, /旧版规则结果/);
});

test("candidate evidence removes obsolete rule, auxiliary LLM, and conclusion controls", () => {
  assert.doesNotMatch(candidateSource, />规则评分</);
  assert.doesNotMatch(candidateSource, /LLM 辅助评分/);
  assert.doesNotMatch(candidateSource, /保存人工结论/);
  assert.doesNotMatch(candidateSource, /controller\.saveConclusion/);
});
