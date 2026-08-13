import test from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";

const source = readFileSync(new URL("./InterviewViews.jsx", import.meta.url), "utf8");
const scheduleSource = readFileSync(new URL("./ScheduleWorkspace.jsx", import.meta.url), "utf8");

test("schedule workspace loads participant options for the selected application", () => {
  assert.match(source, /controller\.listParticipantOptions\(participantApplicationId/);
  assert.match(source, /setParticipantDirectory\(options\)/);
  assert.doesNotMatch(source, /if \(actorId\) people\.set/);
  assert.doesNotMatch(source, /records\.flatMap\(\(record\) => record\.participants/);
});

test("the authoritative schedule save carries the selected application identity", () => {
  assert.match(scheduleSource, /const saved = await onSave\(record, \{ \.\.\.form, applicationId: candidate\?\.applicationId \|\| candidate\?\.application\?\.id \|\| ""/);
  assert.doesNotMatch(scheduleSource, /await onCheckConflicts/);
});
