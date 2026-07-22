import test from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import vm from "node:vm";

const source = readFileSync(new URL("./InterviewViews.jsx", import.meta.url), "utf8");
const appSource = readFileSync(new URL("./App.jsx", import.meta.url), "utf8");
const stylesSource = readFileSync(new URL("./styles.css", import.meta.url), "utf8");
const helpersSource = source.match(/\/\* feedback-draft-helpers:start \*\/([\s\S]*?)\/\* feedback-draft-helpers:end \*\//)?.[1];
assert.ok(helpersSource, "InterviewViews.jsx must expose the feedback draft helper block");
const {
  clearInterviewFeedbackDraft,
  getFeedbackSubmitError,
  isAmbiguousFeedbackSubmitError,
  getInterviewFeedbackDraftKey,
  loadInterviewFeedbackDraft,
  resolveInterviewFeedbackDraft,
  saveInterviewFeedbackDraft,
} = vm.runInNewContext(`(() => { ${helpersSource.replaceAll("export ", "")} return { clearInterviewFeedbackDraft, getFeedbackSubmitError, getInterviewFeedbackDraftKey, isAmbiguousFeedbackSubmitError, loadInterviewFeedbackDraft, resolveInterviewFeedbackDraft, saveInterviewFeedbackDraft }; })()`);

function createStorage(initial = {}) {
  const values = new Map(Object.entries(initial));
  return {
    getItem(key) { return values.has(key) ? values.get(key) : null; },
    setItem(key, value) { values.set(key, value); },
    removeItem(key) { values.delete(key); },
  };
}

const form = {
  ratings: { professional: "优秀", problem: "良好", communication: "良好", fit: "优秀" },
  strengths: "技术基础扎实",
  risks: "管理经验待确认",
  conclusion: "推荐",
  notes: "进入下一轮",
};

test("feedback drafts are isolated by interview id", () => {
  const storage = createStorage();
  saveInterviewFeedbackDraft("USER-001", "INT-001", form, storage);

  assert.equal(JSON.stringify(loadInterviewFeedbackDraft("USER-001", { id: "INT-001" }, storage)), JSON.stringify(form));
  assert.equal(loadInterviewFeedbackDraft("USER-001", { id: "INT-002" }, storage), null);
  assert.equal(loadInterviewFeedbackDraft("USER-002", { id: "INT-001" }, storage), null);
});

test("submitted feedback never loads a local draft", () => {
  const storage = createStorage();
  saveInterviewFeedbackDraft("USER-001", "INT-001", form, storage);

  assert.equal(loadInterviewFeedbackDraft("USER-001", { id: "INT-001", feedback: { canEdit: false } }, storage), null);
});

test("invalid or unavailable storage falls back without throwing", () => {
  const invalidStorage = createStorage({ [getInterviewFeedbackDraftKey("USER-001", "INT-001")]: "not-json" });
  const unavailableStorage = { getItem() { throw new Error("blocked"); } };

  assert.equal(loadInterviewFeedbackDraft("USER-001", { id: "INT-001" }, invalidStorage), null);
  assert.equal(loadInterviewFeedbackDraft("USER-001", { id: "INT-001" }, unavailableStorage), null);
});

test("clearing a submitted draft removes only that interview draft", () => {
  const storage = createStorage();
  saveInterviewFeedbackDraft("USER-001", "INT-001", form, storage);
  saveInterviewFeedbackDraft("USER-001", "INT-002", { ...form, conclusion: "保留" }, storage);

  clearInterviewFeedbackDraft("USER-001", "INT-001", storage);

  assert.equal(loadInterviewFeedbackDraft("USER-001", { id: "INT-001" }, storage), null);
  assert.equal(loadInterviewFeedbackDraft("USER-001", { id: "INT-002" }, storage).conclusion, "待补充评估");
});

test("a versioned server draft wins over a stale local draft", () => {
  const local = { ...form, conclusion: "保留" };
  const server = { ...form, id: "FDB-001", version: 3, conclusion: "推荐" };

  const resolved = resolveInterviewFeedbackDraft(local, server);

  assert.equal(resolved.source, "server");
  assert.equal(resolved.form.conclusion, "推荐");
  assert.equal(resolveInterviewFeedbackDraft(local, { status: "draft", version: 0 }).source, "local");
});

test("feedback version conflicts explain that the local draft was preserved", () => {
  assert.equal(
    getFeedbackSubmitError({ code: "resource_version_conflict" }),
    "服务端草稿已在其他页面或设备更新。本机内容已保留，请刷新后核对再提交。",
  );
  assert.equal(getFeedbackSubmitError({ code: "service_unavailable" }), "网络请求失败，表单和本机草稿均已保留。请重试提交。");
});

test("invalid feedback state transitions explain when submission becomes available", () => {
  assert.equal(
    getFeedbackSubmitError({ code: "invalid_state_transition" }),
    "当前面试状态暂不允许提交反馈，本机草稿已保留。请刷新面试状态后重试。",
  );
});

test("feedback submit retry distinguishes ambiguous transport failures", () => {
  assert.equal(isAmbiguousFeedbackSubmitError({ status: 0, kind: "unavailable" }), true);
  assert.equal(isAmbiguousFeedbackSubmitError({ status: 503 }), true);
  assert.equal(isAmbiguousFeedbackSubmitError({ status: 409 }), false);
});

test("schedule workspace loads pending applications from the server without fixture fallback", () => {
  assert.match(appSource, /candidateController\.listCandidates\(\{ stage: "待安排", limit: 100, cursor: cursor \|\| undefined \}/);
  assert.match(appSource, /selectSchedulableCandidates\(interviewCandidateRecords\)/);
  assert.doesNotMatch(appSource, /if \(!selectedCandidateWithInterviews\) return candidateRecords/);
});

test("the interview table treats calendar export as a secondary utility instead of the next task", () => {
  assert.match(source, />待办</);
  assert.match(source, /className="interview-calendar-action"/);
  assert.match(source, />添加到日历</);
  assert.match(source, /getInterviewPrimaryAction/);
  assert.doesNotMatch(source, /interview-row-actions[^\n]*onDownload/);
  assert.match(stylesSource, /\.interview-calendar-action\s*\{[^}]*background:\s*transparent[^}]*text-decoration:\s*underline/s);
  assert.match(stylesSource, /\.interview-primary-action\s*\{[^}]*font-weight:\s*600/s);
});

test("the candidate cell is an explicit keyboard-operable materials and feedback entry", () => {
  assert.match(source, /<button className="interview-person" type="button" aria-label=\{`查看\$\{record\.candidate\}的面试材料与评价`\} onClick=\{\(\) => onFeedback\(record\)\}>/);
  assert.match(stylesSource, /\.interview-person\s*\{[^}]*cursor:\s*pointer/s);
  assert.match(stylesSource, /\.interview-person:focus-visible\s*\{/);
  assert.match(source, /\(!\(mineOnly \|\| !canSchedule\) \|\| isMyInterview\(item, interviewerId\)\)/);
});

test("assigned participants can submit feedback before a scheduled or confirmed interview starts", () => {
  assert.match(source, /const \[editing, setEditing\] = useState\(ownsFeedback\)/);
  assert.match(source, /disabled=\{submitting \|\| loading \|\| !ownsFeedback\}/);
  assert.doesNotMatch(source, /submitEligibilityTime|feedback-submit-gate|面试开始后可提交/);
});
