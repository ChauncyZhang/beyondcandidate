import test from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";

const appSource = readFileSync(new URL("./App.jsx", import.meta.url), "utf8");
const interviewSource = readFileSync(new URL("./InterviewViews.jsx", import.meta.url), "utf8");
const scheduleSource = readFileSync(new URL("./ScheduleWorkspace.jsx", import.meta.url), "utf8");

test("schedule candidate loading follows every server cursor", () => {
  assert.match(appSource, /let cursor = ""/);
  assert.match(appSource, /cursor: cursor \|\| undefined/);
  assert.match(appSource, /cursor = page\.nextCursor \|\| ""/);
  assert.match(appSource, /while \(cursor\)/);
});

test("interview pagination appends the next server page without replacing existing rows", () => {
  assert.match(appSource, /nextCursor: page\.nextCursor/);
  assert.match(appSource, /append \? \[\.\.\.current\.records, \.\.\.page\.records\]/);
  assert.match(appSource, /cursor: interviewState\.nextCursor/);
  assert.match(appSource, /onLoadMore/);
});

test("candidate detail scheduling preserves a server-backed candidate id", () => {
  assert.match(appSource, /setScheduleCandidateId\(candidate\?\.id \|\| candidate\?\.candidateId \|\| null\)/);
});

test("a server candidate list summary never short-circuits a direct detail load", () => {
  assert.match(appSource, /local && local\.serverBacked !== true/);
  assert.match(appSource, /loadServerCandidate\(routeCandidateContext\)/);
  assert.doesNotMatch(appSource, /if \(local\) \{\s*setSelectedCandidate\(local\)/);
});

test("schedule form hydrates an asynchronously loaded candidate without replacing a user selection", () => {
  assert.match(scheduleSource, /resolveScheduleCandidateId\(record, candidateId, fallback\)/);
  assert.match(scheduleSource, /candidateId: resolveScheduleCandidateId/);
});

test("schedule and feedback back links describe the actual origin", () => {
  assert.match(interviewSource, /backLabel/);
  assert.match(interviewSource, /\{backLabel\}/);
});

test("returning from an interview mutation reloads the originating candidate detail", () => {
  assert.match(
    appSource,
    /function requestBackFromInterview\(\)[\s\S]*?interviewOrigin\?\.activeNav === "候选人"[\s\S]*?loadServerCandidate\(candidateDetailState\.context\)/,
  );
});
