import test from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import vm from "node:vm";

const source = readFileSync(new URL("./ScheduleWorkspace.jsx", import.meta.url), "utf8");
const viewsSource = readFileSync(new URL("./InterviewViews.jsx", import.meta.url), "utf8");
const helpersSource = source.match(/\/\* interview-schedule-helpers:start \*\/([\s\S]*?)\/\* interview-schedule-helpers:end \*\//)?.[1];

test("schedule save relies on the authoritative create or update request instead of a duplicate preflight", () => {
  assert.doesNotMatch(source, /if \(!record\) \{ setStep\(3\); return; \}/);
  assert.doesNotMatch(source, /await onCheckConflicts\(record,/);
  assert.doesNotMatch(viewsSource, /onCheckConflicts=/);
  assert.match(source, /const saved = await onSave\(record,/);
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

test("saved schedule message distinguishes queued email from manual candidate notice", () => {
  assert.ok(helpersSource, "InterviewViews.jsx must expose the interview schedule helper block");
  const { getScheduleSavedMessage } = vm.runInNewContext(`(() => { ${helpersSource.replaceAll("export ", "")} return { getScheduleSavedMessage }; })()`);

  assert.equal(getScheduleSavedMessage(null), "面试安排已保存；候选人邮件已进入发送队列；邀请文件可下载");
  assert.equal(getScheduleSavedMessage({ id: "INT-1" }, false, true), "面试改期已保存；邮件未发送，请人工通知候选人；新的邀请文件可下载");
  assert.match(source, /onNotify\(getScheduleSavedMessage\(record, isAvailabilityUnconfirmed\(availability, form\.interviewerIds\), saved\?\.emailDelivery\?\.manualNotificationRequired\)\)/);
  assert.doesNotMatch(source, /通知已发送/);
});

test("saved schedule message preserves the loaded Feishu confirmation state", () => {
  assert.ok(helpersSource, "ScheduleWorkspace.jsx must expose the interview schedule helper block");
  const { isAvailabilityUnconfirmed } = vm.runInNewContext(`(() => { ${helpersSource.replaceAll("export ", "")} return { isAvailabilityUnconfirmed }; })()`);
  const ready = { status: "ready", data: { participants: [{ participantId: "USER-1", status: "confirmed" }, { participantId: "USER-2", status: "unconfirmed" }] } };

  assert.equal(isAvailabilityUnconfirmed(ready, ["USER-1"]), false);
  assert.equal(isAvailabilityUnconfirmed(ready, ["USER-2"]), true);
  assert.equal(isAvailabilityUnconfirmed({ status: "loading" }, ["USER-1"]), true);
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

test("authoritative save does not override a newly detected soft conflict", () => {
  assert.match(source, /allowSoftConflict: false/);
  assert.doesNotMatch(source, /allowSoftConflict: true/);
  const { getScheduleSaveError } = vm.runInNewContext(`(() => { ${helpersSource.replaceAll("export ", "")} return { getScheduleSaveError }; })()`);
  assert.equal(getScheduleSaveError({ code: "schedule_hard_conflict" }), "该时段存在面试冲突，请调整后重试。");
  assert.equal(getScheduleSaveError({ code: "schedule_soft_conflict" }), "该时段与已有安排间隔不足，请调整后重试。");
  assert.equal(getScheduleSaveError({ code: "service_unavailable" }), "无法完成权威冲突检查或保存，当前内容已保留。请重试。");
  assert.match(source, /setSubmitError\(getScheduleSaveError\(error\)\)/);
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

test("disabled and past slots receive an explicit unavailable visual state", () => {
  assert.match(source, /className=\{`\$\{state\}\$\{disabled \? " unavailable" : ""\}`\}/);
  assert.match(source, /disabled=\{disabled\}/);
  assert.match(source, /inPast \? "已过期" : slotLabels\[state\]/);
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
