import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import test from "node:test";

import workbenchController, { createWorkbenchController } from "./workbenchController.js";

const JOB_ID = "11111111-1111-4111-8111-111111111111";
const CANDIDATE_ID = "22222222-2222-4222-8222-222222222222";
const APPLICATION_ID = "33333333-3333-4333-8333-333333333333";
const TASK_ID = "66666666-6666-4666-8666-666666666666";

function item(changes = {}) {
  return {
    application_id: APPLICATION_ID,
    candidate_id: CANDIDATE_ID,
    job_id: JOB_ID,
    display_name: "李嘉明",
    current_title: "AI 算法工程师",
    location: "北京",
    source: "upload",
    stage: "review",
    updated_at: "2026-07-13T03:05:00Z",
    ...changes,
  };
}

function reviewTask(changes = {}) {
  const candidate = item(changes);
  return {
    ...candidate,
    task_id: TASK_ID,
    ai_status: "succeeded",
    config_warning: false,
    candidate_link: `/candidates/${candidate.candidate_id}?tab=evidence&application=${candidate.application_id}&job=${candidate.job_id}`,
    ...changes,
  };
}

function notification(candidate) {
  return { ...candidate, notification_version: "a".repeat(64) };
}

function response(changes = {}) {
  return {
    data: {
      generated_at: "2026-07-13T04:00:00Z",
      jobs: [{
        id: JOB_ID,
        title: "AI 工程师",
        department_name: "技术部",
        status: "open",
        updated_at: "2026-07-13T03:30:00Z",
        active_count: 9,
        stages: {
          new: { count: 0, items: [] },
          review: { count: 1, items: [item()] },
          contact: { count: 0, items: [] },
          interview_pending: { count: 7, items: [item({ stage: "interview_pending", application_id: "44444444-4444-4444-8444-444444444444", next_interview_round: "二面" })] },
          interviewing: { count: 0, items: [] },
          decision: { count: 0, items: [] },
          passed: { count: 1, items: [item({ stage: "passed", application_id: "55555555-5555-4555-8555-555555555555" })] },
        },
      }],
      tasks: {
        review: { count: 1, items: [reviewTask()] },
        interview_pending: { count: 7, items: [item({ stage: "interview_pending", application_id: "44444444-4444-4444-8444-444444444444", next_interview_round: "二面" })] },
        decision: { count: 0, items: [] },
        passed: { count: 1, items: [item({ stage: "passed", application_id: "55555555-5555-4555-8555-555555555555" })] },
      },
      notifications: {
        review: { count: 1, items: [notification(reviewTask())] },
        interview_pending: { count: 1, items: [notification(item({ stage: "interview_pending", application_id: "44444444-4444-4444-8444-444444444444", next_interview_round: "二面" }))] },
        decision: { count: 0, items: [] },
        passed: { count: 1, items: [notification(item({ stage: "passed", application_id: "55555555-5555-4555-8555-555555555555" }))] },
      },
      interviews: { available: false, upcoming: [], pending_feedback: [] },
      ...changes,
    },
  };
}

test("exports the workbench controller factory and default controller", () => {
  assert.equal(typeof createWorkbenchController, "function");
  assert.equal(typeof workbenchController.load, "function");
});

test("load requests the scoped workbench once and maps stages and candidate navigation context", async () => {
  const calls = [];
  const client = { async request(path, options) { calls.push({ path, options }); return response(); } };
  const signal = new AbortController().signal;

  const result = await createWorkbenchController({ client }).load({ signal });

  assert.deepEqual(calls, [{ path: "/api/v1/workbench", options: { signal } }]);
  assert.equal(result.generatedAt, "2026-07-13T04:00:00Z");
  assert.equal(result.jobs[0].name, "AI 工程师");
  assert.equal(result.jobs[0].department, "技术部");
  assert.equal(result.jobs[0].activeCount, 9);
  assert.equal(result.jobs[0].stages["待复核"].count, 1);
  assert.equal(result.jobs[0].stages["已通过"].count, 1);
  assert.deepEqual(result.jobs[0].stages["待复核"].items[0], {
    id: APPLICATION_ID,
    applicationId: APPLICATION_ID,
    candidateId: CANDIDATE_ID,
    jobId: JOB_ID,
    serverBacked: true,
    name: "李嘉明",
    role: "AI 算法工程师",
    company: "",
    position: "AI 工程师",
    stage: "待复核",
    nextRound: "",
    source: "upload",
    city: "北京",
    lastActivity: new Intl.DateTimeFormat("zh-CN", { month: "2-digit", day: "2-digit", hour: "2-digit", minute: "2-digit", hour12: false }).format(new Date("2026-07-13T03:05:00Z")),
    evidence: {},
  });
  assert.equal(result.tasks.interviewPending.count, 7);
  assert.equal(result.tasks.review.count, 1);
  assert.deepEqual(result.tasks.review.items[0], {
    ...result.jobs[0].stages["待复核"].items[0],
    taskId: TASK_ID,
    aiStatus: "succeeded",
    aiLabel: "",
    configWarning: false,
    candidateLink: `/candidates/${CANDIDATE_ID}?tab=evidence&application=${APPLICATION_ID}&job=${JOB_ID}`,
  });
  assert.equal(result.tasks.passed.count, 1);
  assert.equal(result.tasks.interviewPending.items[0].position, "AI 工程师");
  assert.equal(result.tasks.interviewPending.items[0].nextRound, "二面");
  assert.equal(result.notifications.review.items[0].notificationVersion, "a".repeat(64));
  assert.equal(result.notifications.interviewPending.count, 1);
  assert.deepEqual(result.interviews, { available: false, upcoming: [], pendingFeedback: [] });
});

test("markNotificationRead uses the versioned idempotent notification endpoint", async () => {
  const calls = [];
  const client = { async markNotificationRead(applicationId, version) { calls.push({ applicationId, version }); return { read_at: "2026-07-22T01:00:00Z" }; } };
  const candidate = { applicationId: APPLICATION_ID, notificationVersion: "a".repeat(64) };

  const result = await createWorkbenchController({ client }).markNotificationRead(candidate);

  assert.deepEqual(calls, [{ applicationId: APPLICATION_ID, version: "a".repeat(64) }]);
  assert.deepEqual(result, { read_at: "2026-07-22T01:00:00Z" });
});

test("refresh restores server read state without removing the business task", async () => {
  const client = { async request() {
    return response({
      notifications: {
        review: { count: 0, items: [] },
        interview_pending: { count: 0, items: [] },
        decision: { count: 0, items: [] },
        passed: { count: 0, items: [] },
      },
    });
  } };

  const result = await createWorkbenchController({ client }).load();

  assert.equal(result.notifications.review.count, 0);
  assert.equal(result.notifications.passed.count, 0);
  assert.equal(result.tasks.review.count, 1);
  assert.equal(result.tasks.passed.count, 1);
});

test("load applies safe display fallbacks to valid nullable workbench fields", async () => {
  const client = { async request() {
    return response({
      jobs: [
        {
          id: JOB_ID,
          title: "AI 工程师",
          department_name: null,
          status: "open",
          updated_at: "2026-07-13T03:30:00Z",
          active_count: 1,
          stages: {
            new: { count: 0, items: [] },
            review: { count: 1, items: [item({ current_title: null, location: null })] },
            contact: { count: 0, items: [] },
            interview_pending: { count: 0, items: [] },
            interviewing: { count: 0, items: [] },
            decision: { count: 0, items: [] },
            passed: { count: 0, items: [] },
          },
        },
      ],
      tasks: {
        review: { count: 1, items: [reviewTask({ current_title: null, location: null })] },
        interview_pending: { count: 0, items: [] },
        decision: { count: 0, items: [] },
        passed: { count: 0, items: [] },
      },
      interviews: { available: false, upcoming: [], pending_feedback: [] },
    });
  } };

  const result = await createWorkbenchController({ client }).load();

  assert.equal(result.jobs.length, 1);
  assert.equal(result.jobs[0].department, "部门未设置");
  assert.equal(result.jobs[0].stages["待复核"].items[0].role, "当前职称未填写");
  assert.equal(result.jobs[0].stages["待复核"].items[0].city, "地点未填写");
  assert.deepEqual(result.tasks, {
    review: { count: 1, items: [{
      ...result.jobs[0].stages["待复核"].items[0],
      taskId: TASK_ID,
      aiStatus: "succeeded",
      aiLabel: "",
      configWarning: false,
      candidateLink: `/candidates/${CANDIDATE_ID}?tab=evidence&application=${APPLICATION_ID}&job=${JOB_ID}`,
    }] },
    interviewPending: { count: 0, items: [] },
    decision: { count: 0, items: [] },
    passed: { count: 0, items: [] },
  });
  assert.deepEqual(result.interviews, { available: false, upcoming: [], pendingFeedback: [] });
});

test("review tasks keep their persisted contract independently of visible jobs and stage totals", async () => {
  const hiddenJobId = "77777777-7777-4777-8777-777777777777";
  const hiddenApplicationId = "88888888-8888-4888-8888-888888888888";
  const hiddenCandidateId = "99999999-9999-4999-8999-999999999999";
  const client = { async request() {
    return response({
      tasks: {
        review: {
          count: 2,
          items: [reviewTask({
            application_id: hiddenApplicationId,
            candidate_id: hiddenCandidateId,
            job_id: hiddenJobId,
            task_id: "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa",
            ai_status: "failed",
            config_warning: true,
            candidate_link: `/candidates/${hiddenCandidateId}?tab=evidence&application=${hiddenApplicationId}&job=${hiddenJobId}`,
          })],
        },
        interview_pending: response().data.tasks.interview_pending,
        decision: response().data.tasks.decision,
        passed: response().data.tasks.passed,
      },
    });
  } };

  const result = await createWorkbenchController({ client }).load();

  assert.equal(result.jobs[0].stages["待复核"].count, 1);
  assert.equal(result.tasks.review.count, 2);
  assert.equal(result.tasks.review.items.length, 1);
  assert.deepEqual(result.tasks.review.items[0], {
    id: hiddenApplicationId,
    applicationId: hiddenApplicationId,
    candidateId: hiddenCandidateId,
    jobId: hiddenJobId,
    serverBacked: true,
    name: "李嘉明",
    role: "AI 算法工程师",
    company: "",
    position: "职位信息不可用",
    stage: "待复核",
    nextRound: "",
    source: "upload",
    city: "北京",
    lastActivity: new Intl.DateTimeFormat("zh-CN", { month: "2-digit", day: "2-digit", hour: "2-digit", minute: "2-digit", hour12: false }).format(new Date("2026-07-13T03:05:00Z")),
    evidence: {},
    taskId: "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa",
    aiStatus: "failed",
    aiLabel: "AI评分不可用",
    configWarning: true,
    candidateLink: `/candidates/${hiddenCandidateId}?tab=evidence&application=${hiddenApplicationId}&job=${hiddenJobId}`,
  });
});

test("review task fields fail closed when the persisted navigation contract is malformed", async () => {
  for (const review of [
    reviewTask({ task_id: null }),
    reviewTask({ ai_status: "pending" }),
    reviewTask({ config_warning: "true" }),
    reviewTask({ candidate_link: "/candidates/wrong?tab=evidence" }),
  ]) {
    const client = { async request() {
      return response({ tasks: { ...response().data.tasks, review: { count: 1, items: [review] } } });
    } };
    await assert.rejects(
      createWorkbenchController({ client }).load(),
      (error) => error?.code === "WORKBENCH_INVALID_RESPONSE",
    );
  }
});

test("load rejects a malformed successful response instead of presenting it as an empty workbench", async () => {
  for (const malformed of [
    {},
    { data: null },
    response({ jobs: null }),
    response({ generated_at: null }),
    response({ jobs: [{ id: JOB_ID, title: "AI 工程师", status: "draft", updated_at: "2026-07-13T03:30:00Z", active_count: 4, stages: {} }] }),
    response({ jobs: [{ id: JOB_ID, title: "AI 工程师", status: "open", updated_at: "invalid", active_count: 4, stages: {} }] }),
    response({ tasks: null }),
    response({ tasks: { review: {}, interview_pending: {}, decision: null, passed: {} } }),
    response({ tasks: { review: { count: 1, items: null }, interview_pending: { count: 0, items: [] }, decision: { count: 0, items: [] }, passed: { count: 0, items: [] } } }),
    response({ jobs: [{ ...response().data.jobs[0], active_count: 1, stages: { ...response().data.jobs[0].stages, review: { count: 0, items: [item()] } } }] }),
    response({ jobs: [{ ...response().data.jobs[0], stages: { ...response().data.jobs[0].stages, review: { count: 1, items: [item({ job_id: "99999999-9999-4999-8999-999999999999" })] } } }] }),
    response({ interviews: null }),
    response({ interviews: { available: true, upcoming: [], pending_feedback: [] } }),
    response({ interviews: { available: false, upcoming: [{ id: "fixture" }], pending_feedback: [] } }),
  ]) {
    const client = { async request() { return malformed; } };
    await assert.rejects(
      createWorkbenchController({ client }).load(),
      (error) => error?.code === "WORKBENCH_INVALID_RESPONSE",
    );
  }
});

test("unavailable interview data rejects unexpected rows", async () => {
  const client = { async request() {
    return response({ interviews: { available: false, upcoming: [{ id: "fake" }], pending_feedback: [{ id: "fake" }] } });
  } };
  await assert.rejects(
    createWorkbenchController({ client }).load(),
    (error) => error?.code === "WORKBENCH_INVALID_RESPONSE",
  );
});

test("load preserves abort errors", async () => {
  const expected = new DOMException("aborted", "AbortError");
  const client = { async request() { throw expected; } };
  await assert.rejects(createWorkbenchController({ client }).load(), (error) => error === expected);
});

test("workbench shell keeps real tasks accessible across loading and narrow layouts", () => {
  const appSource = readFileSync(new URL("./App.jsx", import.meta.url), "utf8");
  const stylesSource = readFileSync(new URL("./styles.css", import.meta.url), "utf8");
  const themeSource = readFileSync(new URL("./product-theme.css", import.meta.url), "utf8");

  assert.match(appSource, /className="page-body workbench-skeleton" role="status" aria-live="polite"/);
  assert.match(appSource, /const workbenchBoardStageMeta = stageMeta\.filter\(\(stage\) => !\["新简历", "待沟通"\]\.includes\(stage\)\)/);
  assert.match(appSource, /workbenchBoardStageMeta\.map\(\(stage\) => <section className="stage"/);
  assert.match(appSource, /workbenchBoardStages\.map\(\(\[name, count, loadedCount, candidates\]\) =>/);
  assert.match(appSource, /stages\.flat\(\)\.slice\(0, 10\)\.map\(\(candidate\) =>/);
  assert.match(appSource, /const workbenchCandidateCount = visibleStageMeta\.reduce/);
  assert.match(appSource, /aria-pressed=\{activeWorkbenchJob\.id === job\.id\}/);
  assert.match(appSource, /navigate\(candidateListPath\(\{ jobId: activeWorkbenchJob\.id, stage: name \}\)\)/);
  assert.match(appSource, /candidateOrigin\?\.activeNav === "工作台" \? "返回工作台"/);
  assert.match(appSource, /candidateDetailPath\(candidate, "筛选证据", "\/workbench"\)/);
  assert.match(appSource, /candidate\.configWarning && <small>岗位未配置用人经理<\/small>/);
  assert.match(appSource, /onReferralComplete=\{\(\) => void loadWorkbench\(\)\}/);
  assert.match(appSource, /role="alert"/);
  assert.doesNotMatch(appSource, /面试日历（未来 7 天）<\/h3><button/);
  assert.doesNotMatch(appSource, /key=\{candidate\.name\}/);
  assert.match(appSource, /className="calendar-interview-item"/);
  assert.match(appSource, /className="calendar-interview-title"/);
  assert.match(appSource, /className="calendar-interview-meta"/);
  assert.match(appSource, /"已通过"/);
  assert.doesNotMatch(stylesSource, /\.page-body \{ grid-template-columns: minmax\(720px, 1fr\); \}\.right-rail \{ display: none; \}/);
  assert.match(stylesSource, /\.right-rail \{ grid-template-columns: minmax\(0,1fr\) minmax\(260px,/);
  assert.match(stylesSource, /\.calendar-interview-item\s*\{[^}]*padding:\s*11px 12px[^}]*border-top:\s*1px solid/s);
  assert.match(appSource, /className="calendar-interview-title" title=\{record\.candidate\}/);
  assert.match(appSource, /className="calendar-interview-round">\{record\.round\}/);
  assert.match(stylesSource, /\.calendar-interview-title strong\s*\{[^}]*text-overflow:\s*ellipsis[^}]*white-space:\s*nowrap/s);
  assert.match(stylesSource, /\.calendar-interview-meta\s*\{[^}]*flex-wrap:\s*wrap/s);
  assert.match(stylesSource, /\.kanban\s*\{[^}]*grid-template-columns:\s*repeat\(5, minmax\(112px, 1fr\)\)/);
  assert.match(themeSource, /\.kanban\s*\{[^}]*grid-template-columns:\s*repeat\(5, minmax\(0, 1fr\)\)/);
  assert.match(themeSource, /@media \(max-width: 1280px\)\s*\{[\s\S]*?\.kanban\s*\{[^}]*grid-template-columns:\s*repeat\(5, minmax\(160px, 1fr\)\)/);
  assert.match(themeSource, /@media \(max-width: 640px\)\s*\{[\s\S]*?\.kanban\s*\{[^}]*grid-template-columns:\s*repeat\(5, clamp\(260px, 78vw, 300px\)\)/);
});
