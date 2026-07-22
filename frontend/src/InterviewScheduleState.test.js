import test from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import vm from "node:vm";

const source = readFileSync(new URL("./ScheduleWorkspace.jsx", import.meta.url), "utf8");
const viewsSource = readFileSync(new URL("./InterviewViews.jsx", import.meta.url), "utf8");
const helpersSource = source.match(/\/\* interview-schedule-helpers:start \*\/([\s\S]*?)\/\* interview-schedule-helpers:end \*\//)?.[1];

test("new schedules run the same conflict check as reschedules", () => {
  assert.doesNotMatch(source, /if \(!record\) \{ setStep\(3\); return; \}/);
  assert.match(source, /await onCheckConflicts\(record,/);
  assert.match(viewsSource, /controller\.checkConflicts\(record\?\.id, form\)/);
});

test("hard conflicts block while soft conflicts require an explicit override", () => {
  assert.ok(helpersSource, "ScheduleWorkspace.jsx must expose the interview schedule helper block");
  const { getScheduleConflictType } = vm.runInNewContext(`(() => { ${helpersSource.replaceAll("export ", "")} return { getScheduleConflictType }; })()`);

  assert.equal(getScheduleConflictType({ hard: ["INT-1"], soft: ["INT-2"] }, true), "hard");
  assert.equal(getScheduleConflictType({ hard: [], soft: ["INT-2"] }, false), "soft");
  assert.equal(getScheduleConflictType({ hard: [], soft: ["INT-2"] }, true), null);
  assert.equal(getScheduleConflictType({ hard: [], soft: [], calendarHard: ["USER-1"] }, false), "hard");
});

test("unconfirmed Feishu availability remains selectable while known busy windows still block", () => {
  assert.match(source, /return unconfirmed \? "unconfirmed" : "available"/);
  assert.match(source, /\["available", "unconfirmed"\]\.includes/);
  assert.match(source, /飞书未绑定或查询失败，可继续安排/);
});

test("saved schedule message describes downloadable invitation and pending notifications", () => {
  assert.ok(helpersSource, "InterviewViews.jsx must expose the interview schedule helper block");
  const { getScheduleSavedMessage } = vm.runInNewContext(`(() => { ${helpersSource.replaceAll("export ", "")} return { getScheduleSavedMessage }; })()`);

  assert.equal(getScheduleSavedMessage(null), "面试安排已保存；邀请文件可下载；面试邀请尚未发送");
  assert.equal(getScheduleSavedMessage({ id: "INT-1" }), "面试改期已保存；新的邀请文件可下载；面试邀请尚未发送");
  assert.match(source, /onNotify\(getScheduleSavedMessage\(record, Boolean\(finalConflict\?\.unconfirmed\?\.length\)\)\)/);
  assert.doesNotMatch(source, /通知已发送/);
});

test("copy helper writes the invitation text to the clipboard", async () => {
  assert.ok(helpersSource, "InterviewViews.jsx must expose the interview schedule helper block");
  const { copyInterviewText } = vm.runInNewContext(`(() => { ${helpersSource.replaceAll("export ", "")} return { copyInterviewText }; })()`);
  let copied = "";

  await copyInterviewText("邀请内容", { async writeText(value) { copied = value; } });

  assert.equal(copied, "邀请内容");
  assert.match(source, /await copyInterviewText\(text,/);
  assert.match(source, /copyInvitation\(form\.candidateMessage,/);
  assert.match(source, /copyInvitation\(form\.interviewerMessage,/);
});

test("copy helper rejects when clipboard access is unavailable or fails", async () => {
  assert.ok(helpersSource, "InterviewViews.jsx must expose the interview schedule helper block");
  const { copyInterviewText } = vm.runInNewContext(`(() => { ${helpersSource.replaceAll("export ", "")} return { copyInterviewText }; })()`);

  await assert.rejects(copyInterviewText("邀请内容", null), /clipboard unavailable/);
  await assert.rejects(copyInterviewText("邀请内容", { async writeText() { throw new Error("denied"); } }), /denied/);
});

test("final save does not override a newly detected soft conflict", () => {
  assert.match(source, /allowSoftConflict: false/);
  assert.match(source, /const finalConflict = await onCheckConflicts/);
});

test("past slots on the current day are unavailable in the selected timezone", () => {
  assert.ok(helpersSource, "ScheduleWorkspace.jsx must expose the interview schedule helper block");
  const { isScheduleSlotInPast } = vm.runInNewContext(`(() => { ${helpersSource.replaceAll("export ", "")} return { isScheduleSlotInPast }; })()`);
  const now = Date.parse("2026-07-16T15:40:00Z");

  assert.equal(isScheduleSlotInPast("2026-07-16", "23:30", "Asia/Shanghai", now), true);
  assert.equal(isScheduleSlotInPast("2026-07-16", "23:40", "Asia/Shanghai", now), true);
  assert.equal(isScheduleSlotInPast("2026-07-16", "23:41", "Asia/Shanghai", now), false);
  assert.equal(isScheduleSlotInPast("2026-07-17", "09:00", "Asia/Shanghai", now), false);
});

test("schedule defaults to the configured next round and recommends an extra round after the template", () => {
  assert.ok(helpersSource, "ScheduleWorkspace.jsx must expose the interview schedule helper block");
  const { recommendedInterviewRound } = vm.runInNewContext(`(() => { ${helpersSource.replaceAll("export ", "")} return { recommendedInterviewRound }; })()`);

  assert.equal(recommendedInterviewRound({ nextRound: "二面", interviews: [{ round: "一面" }] }), "二面");
  assert.equal(recommendedInterviewRound({ nextRound: "", interviews: [{ round: "一面" }, { round: "二面" }] }), "三面");
  assert.equal(recommendedInterviewRound({ nextRound: "", interviews: [{ round: "技术一面" }, { round: "技术二面" }] }), "三面");
  assert.equal(recommendedInterviewRound({ nextRound: "", interviews: [] }), "一面");
  assert.match(source, /流程轮次已完成，可追加三面、终面或加面/);
});

test("reschedules keep the existing candidate visible and immutable", () => {
  assert.match(source, /const recordCandidate = record \? \{ id: record\.candidateId, candidateId: record\.candidateId, name: record\.candidate/);
  assert.match(source, /disabled=\{Boolean\(record\)\}/);
  assert.match(source, /candidateOptions\.map/);
});

test("HR can cancel eligible interviews and mark arrived interviews as no-show", () => {
  assert.match(viewsSource, /record\.status === "待确认"/);
  assert.match(viewsSource, /target: "no_show", label: "标记未到场"/);
  assert.match(viewsSource, /请填写操作原因/);
  assert.match(viewsSource, /onTransition\(transitionDraft\.record, transitionDraft\.target, reason\.trim\(\)\)/);
});
